"""
compile_kb.py — Knowledge Base preprocessor.

Reads Claude Code conversation JSONLs from ~/.claude/projects/<slug>/
and produces clean per-day per-project markdown digests under
<project>/obsidian/_compiled/_raw/.

This is a PURE PREPROCESSOR. No LLM calls. No external deps. Stdlib only.
After running this, ask Claude Opus "compile" to synthesize concepts.

Usage:
    python scripts/compile_kb.py                # incremental: only new days
    python scripts/compile_kb.py --all          # rebuild all digests
    python scripts/compile_kb.py --dry-run      # show what would be written
    python scripts/compile_kb.py --project myproject   # only one project
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Map of Claude Code project directory name → short project label used in
# digest filenames.
#
# Two modes:
#   1. Single project (auto-detect): leave as None and the script will compute
#      the slug from the project root and look it up in ~/.claude/projects/.
#      This is the default for new projects (zero config).
#
#   2. Multi-project ecosystem (manual dict): explicitly list every project
#      whose conversation history should feed into this one shared KB.
#      Use this when you want sibling projects to share a single compiled KB.
#
# To find a project's slug: ls ~/.claude/projects/
ECOSYSTEM_PROJECTS = None

# Example multi-project ecosystem config (uncomment and customize):
# ECOSYSTEM_PROJECTS = {
#     "d--dev-projects-frontend": "frontend",
#     "d--dev-projects-backend": "backend",
#     "d--dev-projects-shared-lib": "shared-lib",
# }

# Resolve home & paths once
HOME = Path.home()
CLAUDE_PROJECTS_DIR = HOME / ".claude" / "projects"

# Digest output goes to <project>/obsidian/_compiled/_raw/ relative to this script
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent  # scripts/ → <project>/
RAW_DIR = PROJECT_DIR / "obsidian" / "_compiled" / "_raw"
STATE_FILE = RAW_DIR / ".state.json"

# Message types we want to keep from JSONL
KEEP_MESSAGE_TYPES = {"user", "assistant"}

# Message types we explicitly skip (Claude Code internals, not conversation content)
SKIP_MESSAGE_TYPES = {
    "queue-operation",
    "attachment",
    "file-history-snapshot",
    "ai-title",
    "summary",
    "system",
}

# Tool input render: keep first N chars of the tool input dict for context
TOOL_INPUT_PREVIEW_CHARS = 80

# Strip system reminders from user messages (they're Claude Code internal noise)
SYSTEM_REMINDER_RE = re.compile(
    r"<system-reminder>.*?</system-reminder>", re.DOTALL
)


# ---------------------------------------------------------------------------
# Project slug auto-detection
# ---------------------------------------------------------------------------
def path_to_slug(path) -> str:
    """
    Convert a filesystem path to the slug Claude Code uses in
    ~/.claude/projects/<slug>/. Each non-alphanumeric character becomes a
    single dash. Verified against 7 real entries.
    """
    return ''.join(ch if ch.isalnum() else '-' for ch in str(path))


def autodetect_projects() -> dict:
    """
    Determine which Claude Code project(s) feed this KB by inspecting the
    project root that contains this script.

    Returns: {slug: short_label} dict with one entry, ready to use as
             ECOSYSTEM_PROJECTS.

    Resolution order (3 tiers):
      1. Exact slug match — apply Claude Code's slug algorithm to project root
      2. Case-insensitive match — Windows path case quirks
      3. Substring fallback — project moved or renamed

    Exits with a clear error if nothing matches.
    """
    project_root = SCRIPT_DIR.parent  # scripts/ → project root
    exact_slug = path_to_slug(project_root)

    # Tier 1: exact match
    if (CLAUDE_PROJECTS_DIR / exact_slug).exists():
        return {exact_slug: project_root.name}

    # Tier 2: case-insensitive match
    target_lower = exact_slug.lower()
    if CLAUDE_PROJECTS_DIR.exists():
        for d in CLAUDE_PROJECTS_DIR.iterdir():
            if d.is_dir() and d.name.lower() == target_lower:
                return {d.name: project_root.name}

        # Tier 3: substring fallback (project name with _/- normalization)
        project_name_norm = project_root.name.lower().replace('_', '-')
        for d in CLAUDE_PROJECTS_DIR.iterdir():
            if d.is_dir() and d.name.lower().endswith('-' + project_name_norm):
                return {d.name: project_root.name}

    # Nothing matched
    print("ERROR: Could not auto-detect Claude Code project slug.", file=sys.stderr)
    print(f"  Project root:        {project_root}", file=sys.stderr)
    print(f"  Computed slug:       {exact_slug}", file=sys.stderr)
    print(f"  Looked up under:     {CLAUDE_PROJECTS_DIR}", file=sys.stderr)
    print(f"  Hint: open this project in Claude Code at least once so it",
          file=sys.stderr)
    print(f"        creates ~/.claude/projects/<slug>/, OR set",
          file=sys.stderr)
    print(f"        ECOSYSTEM_PROJECTS = {{...}} explicitly in {Path(__file__).name}",
          file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------
def parse_jsonl_message(line_obj: dict) -> dict | None:
    """Convert one JSONL line dict into a clean message dict, or None to skip."""
    msg_type = line_obj.get("type")
    if msg_type not in KEEP_MESSAGE_TYPES:
        return None

    message = line_obj.get("message") or {}
    role = message.get("role") or msg_type
    raw_content = message.get("content")
    timestamp = line_obj.get("timestamp") or ""

    if not raw_content:
        return None

    text_blocks: list[str] = []

    if isinstance(raw_content, str):
        cleaned = clean_text(raw_content)
        if cleaned:
            text_blocks.append(cleaned)
    elif isinstance(raw_content, list):
        for block in raw_content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")

            if block_type == "text":
                text = clean_text(block.get("text") or "")
                if text:
                    text_blocks.append(text)

            elif block_type == "tool_use":
                tool_name = block.get("name") or "?"
                tool_input = block.get("input") or {}
                preview = render_tool_input_preview(tool_name, tool_input)
                text_blocks.append(f"[TOOL: {tool_name}({preview})]")

            elif block_type == "tool_result":
                # Skip tool_result content — huge and recoverable from the
                # previous tool_use line. We just acknowledge it happened.
                text_blocks.append("[TOOL_RESULT]")

            elif block_type == "thinking":
                # Drop thinking blocks. Too big, internal reasoning.
                continue

            elif block_type == "image":
                text_blocks.append("[IMAGE]")
            # Unknown types: silently skip

    if not text_blocks:
        return None

    return {
        "ts": timestamp,
        "role": role,
        "text_blocks": text_blocks,
    }


def clean_text(text: str) -> str:
    """Strip system reminders and normalize whitespace."""
    if not text:
        return ""
    text = SYSTEM_REMINDER_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def render_tool_input_preview(tool_name: str, tool_input: dict) -> str:
    """Produce a one-line summary of a tool call's input args."""
    if not isinstance(tool_input, dict):
        return ""

    primary_field_by_tool = {
        "Read": "file_path",
        "Write": "file_path",
        "Edit": "file_path",
        "Glob": "pattern",
        "Grep": "pattern",
        "Bash": "command",
        "WebFetch": "url",
        "Agent": "description",
        "Task": "description",
        "TodoWrite": None,
        "AskUserQuestion": None,
    }

    primary = primary_field_by_tool.get(tool_name, "")
    if primary and primary in tool_input:
        value = str(tool_input[primary])
        if len(value) > TOOL_INPUT_PREVIEW_CHARS:
            value = value[:TOOL_INPUT_PREVIEW_CHARS] + "..."
        return value

    s = json.dumps(tool_input, ensure_ascii=False)
    if len(s) > TOOL_INPUT_PREVIEW_CHARS:
        s = s[:TOOL_INPUT_PREVIEW_CHARS] + "..."
    return s


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"WARN: could not parse state file ({exc}). Treating as empty.", file=sys.stderr)
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Core preprocessing
# ---------------------------------------------------------------------------
def parse_iso_ts(ts: str) -> datetime | None:
    """Parse an ISO 8601 timestamp from a JSONL message."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def collect_messages_from_jsonl(jsonl_path: Path) -> list[dict]:
    """Read a JSONL file and return a list of cleaned message dicts."""
    messages: list[dict] = []
    try:
        with jsonl_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = parse_jsonl_message(obj)
                if msg is None:
                    continue
                messages.append(msg)
    except Exception as exc:
        print(f"WARN: failed reading {jsonl_path.name}: {exc}", file=sys.stderr)
    return messages


def group_by_day(messages: list[dict]) -> dict[str, list[dict]]:
    """Group messages by YYYY-MM-DD (UTC)."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for m in messages:
        dt = parse_iso_ts(m.get("ts", ""))
        if dt is None:
            continue
        day_key = dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        by_day[day_key].append(m)
    return by_day


def render_day_digest(
    project_label: str,
    day_key: str,
    messages: list[dict],
    source_jsonl: Path,
) -> str:
    """Produce the markdown text for one day's digest."""
    lines: list[str] = []
    lines.append(f"# Conversation Digest — {project_label} — {day_key}")
    lines.append("")
    lines.append(f"**Source JSONL:** `{source_jsonl}`")
    lines.append(f"**Messages:** {len(messages)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for m in messages:
        dt = parse_iso_ts(m.get("ts", ""))
        hhmm = dt.astimezone(timezone.utc).strftime("%H:%M") if dt else "??:??"
        role = m["role"].upper()
        lines.append(f"## {hhmm} {role}")
        for block in m["text_blocks"]:
            lines.append(block)
            lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def safe_filename(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", s)


def process_project(
    project_dir: Path,
    project_label: str,
    state: dict,
    args: argparse.Namespace,
) -> dict:
    """Process all JSONL files for one ecosystem project."""
    stats = {"written": 0, "skipped_existing": 0, "days": 0, "latest_ts": ""}
    if not project_dir.exists():
        print(f"  ({project_label}) project dir not found, skipping.")
        return stats

    jsonl_files = sorted(project_dir.glob("*.jsonl"))
    if not jsonl_files:
        print(f"  ({project_label}) no JSONL files, skipping.")
        return stats

    all_messages: list[dict] = []
    source_paths: list[Path] = []
    for jsonl in jsonl_files:
        msgs = collect_messages_from_jsonl(jsonl)
        if msgs:
            all_messages.extend(msgs)
            source_paths.append(jsonl)

    if not all_messages:
        print(f"  ({project_label}) no usable messages.")
        return stats

    all_messages.sort(key=lambda m: m.get("ts") or "")

    by_day = group_by_day(all_messages)
    stats["days"] = len(by_day)

    last_ts = state.get(project_label, {}).get("last_processed_ts", "")
    last_date = ""
    if last_ts:
        last_dt = parse_iso_ts(last_ts)
        if last_dt:
            last_date = last_dt.astimezone(timezone.utc).strftime("%Y-%m-%d")

    written_dates: list[str] = []
    for day_key in sorted(by_day.keys()):
        if not args.all and last_date and day_key < last_date:
            stats["skipped_existing"] += 1
            continue

        msgs_for_day = by_day[day_key]
        if not msgs_for_day:
            continue

        out_name = f"{day_key}-{safe_filename(project_label)}.md"
        out_path = RAW_DIR / out_name
        digest_text = render_day_digest(
            project_label, day_key, msgs_for_day, source_paths[0]
        )

        if args.dry_run:
            print(f"    [dry-run] would write {out_name} ({len(digest_text)} chars, {len(msgs_for_day)} msgs)")
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(digest_text, encoding="utf-8")
            print(f"    wrote {out_name} ({len(digest_text)} chars, {len(msgs_for_day)} msgs)")

        stats["written"] += 1
        written_dates.append(day_key)

    if all_messages:
        stats["latest_ts"] = all_messages[-1].get("ts") or ""

    if not args.dry_run and stats["written"] > 0:
        state[project_label] = {
            "last_processed_ts": stats["latest_ts"],
            "last_run": datetime.now(timezone.utc).isoformat(),
            "written_days": written_dates,
            "source_jsonls": [str(p) for p in source_paths],
        }

    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true",
                        help="Rebuild all digests (ignore state file)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be written, do not touch disk")
    parser.add_argument("--project", type=str, default="",
                        help="Process only this project label")
    args = parser.parse_args()

    # Resolve which projects to process: explicit dict OR auto-detect
    projects = ECOSYSTEM_PROJECTS
    detection_mode = "explicit"
    if projects is None:
        projects = autodetect_projects()
        detection_mode = "auto-detect"

    print(f"compile_kb.py — preprocessor")
    print(f"  Claude projects dir: {CLAUDE_PROJECTS_DIR}")
    print(f"  Output dir:          {RAW_DIR}")
    print(f"  Mode:                {'DRY RUN' if args.dry_run else 'LIVE'}{' (rebuild all)' if args.all else ''}")
    print(f"  Project resolution:  {detection_mode} ({len(projects)} project{'s' if len(projects) != 1 else ''})")
    print()

    if not CLAUDE_PROJECTS_DIR.exists():
        print(f"ERROR: Claude projects dir not found: {CLAUDE_PROJECTS_DIR}", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    total = {"written": 0, "skipped_existing": 0, "days": 0}

    for proj_dirname, proj_label in projects.items():
        if args.project and args.project != proj_label:
            continue
        print(f"[{proj_label}]")
        proj_dir = CLAUDE_PROJECTS_DIR / proj_dirname
        s = process_project(proj_dir, proj_label, state, args)
        total["written"] += s["written"]
        total["skipped_existing"] += s["skipped_existing"]
        total["days"] += s["days"]
        print()

    if not args.dry_run:
        save_state(state)

    print("=" * 60)
    print(f"Wrote {total['written']} digests across "
          f"{sum(1 for k in projects.values() if (not args.project or args.project == k))} projects")
    print(f"  Days seen total:  {total['days']}")
    print(f"  Skipped existing: {total['skipped_existing']}")
    if not args.dry_run:
        print(f"  State file:       {STATE_FILE}")


if __name__ == "__main__":
    main()
