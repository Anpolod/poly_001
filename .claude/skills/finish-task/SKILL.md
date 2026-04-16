---
name: finish-task
description: Post-task retro — update TASKS.md, append a one-paragraph lesson to obsidian/00_inbox/session-log.md, update memory if needed. Use immediately after marking a T-## task done.
argument-hint: "[T-##]"
---

# Finish Task

Use immediately after marking a T-## task done in [TASKS.md](TASKS.md). Takes 5 minutes. Captures the non-obvious learnings before conversation context compacts and they're lost.

---

## Checklist

### 1. TASKS.md entry is complete

Open [TASKS.md](TASKS.md) and confirm the entry has:

- ✅ marker + date (`2026-04-15`)
- **What was done** — 3-5 bullets, enough to reconstruct intent without re-reading the diff
- **TODO next session** — any manual follow-up on Mac Mini (schema apply, one-time job, config)

If a `TODO next session` exists, it's a forcing function for the start of the next session — do not skip it.

### 2. Append to `obsidian/00_inbox/session-log.md`

One paragraph per closed task. Template:

```markdown
## 2026-04-15 · T-## <short name>

**What surprised me:** <one or two sentences on a code gotcha, library quirk, API shape,
or DOM change that wasn't documented anywhere — the kind of thing future-me would repeat
the same debugging for>

**Follow-ups that didn't block shipping:** <bullets of things deliberately deferred>

**Verification:** <one line on how it was tested — dry-run output, DB seed script,
live scan result>
```

**Examples of good "surprise" entries:**
- "Rotowire's DOM puts `is-pct-play-0` on players with title='Very Likely To Play' — the class is legacy, trust the title attribute only."
- "`historical_calibration` was referenced by 7 files but never in schema.sql — adding it as part of this task."
- "`datetime.now(timezone.utc)` vs `datetime.utcnow()` — asyncpg reads store as tz-aware, comparisons against naive `datetime.utcnow()` silently fail in pg8000 mode but not in asyncpg."

**Do not** log: what the code does (the diff shows that), which files changed (git shows that), the feature description (TASKS.md has that). Only the non-obvious learnings.

### 3. Memory update (only if the lesson is load-bearing)

Decide whether this lesson belongs in persistent memory:

- **feedback memory** — a new workflow rule the user taught you or confirmed ("always use title attribute for Rotowire status, never the class")
- **project memory** — a fact about current project state ("historical_calibration is now in schema.sql as of 2026-04-15")
- **reference memory** — external resource pointer ("Polymarket `outcomePrices` is JSON-encoded list like `['1','0']`, not a raw array")

**Skip** memory update if:
- The learning is already implied by the code itself
- It's one-off debugging (server was down, network flaky)
- It contradicts a well-established convention rather than teaching a new one

### 4. CLI command index

If the task added a new `python -m analytics.<name>` or `make <target>`, add a one-line entry under the Analytics CLI section of [CLAUDE.md](CLAUDE.md). The bootstrap instructions read CLAUDE.md at session start — missing it means future-me won't discover the new command.

---

## Red flags — do NOT mark a task "finished" if

- TASKS.md entry is missing a **TODO next session** note but there IS manual follow-up (e.g. schema needs apply on Mac Mini)
- You can't fill in the "What surprised me" paragraph — either the task was trivial (skip the paragraph, that's fine), or you haven't actually understood what you did (go reread the diff before closing)
- The dry-run test result was "looked about right" — rerun with explicit pass/fail criteria and capture the output
