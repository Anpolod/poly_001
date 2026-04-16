"""Tiny config reader for shell scripts.

Usage (bash):
    ENABLED=$("$PYTHON" "$REPO/scripts/config_get.py" mlb_pitcher_scanner.enabled)
    if [ "$ENABLED" = "true" ]; then ... fi

Prints the value at the dotted path from `config/settings.yaml` (falling back
to `config/settings.example.yaml` if the former does not exist). Booleans are
rendered as the strings `true`/`false`; other values use `str()`. Missing
paths print the empty string and exit 0, so shell callers can test
`[ -z "$X" ]` without special-casing.

T-45: added to gate the MLB pitcher scanner on an explicit config flag in
start_bot.sh + watchdog.sh. Avoids ad-hoc `yaml.safe_load` incantations
inlined into shell scripts, and keeps the fallback-to-example behaviour
in one place.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CANDIDATES = (
    REPO_ROOT / "config" / "settings.yaml",
    REPO_ROOT / "config" / "settings.example.yaml",
)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: config_get.py <dotted.path>", file=sys.stderr)
        return 2
    path = sys.argv[1].split(".")

    cfg_path = next((p for p in CANDIDATES if p.exists()), None)
    if cfg_path is None:
        return 0   # no settings file → treat as empty; caller decides

    try:
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    except Exception:
        return 0

    node = cfg
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return 0   # missing → empty string (exit 0)
        node = node[part]

    if isinstance(node, bool):
        print("true" if node else "false")
    else:
        print(str(node))
    return 0


if __name__ == "__main__":
    sys.exit(main())
