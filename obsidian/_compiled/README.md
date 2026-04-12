# `_compiled/` — Auto-Compiled Knowledge Base

This folder is the **compiled** half of the project's wiki. It is written by **Claude Opus** from raw conversation logs that Claude Code already saves automatically in `~/.claude/projects/<project-slug>/`.

See `~/.claude/templates/compile-knowledge-base.md` for the canonical doc on the pattern.

## How to use

### Compile (~weekly)
```bash
python scripts/compile_kb.py
```
Then say `compile` to Claude Opus.

### Lint (~monthly)
Say `lint` to Claude Opus.

### Read existing knowledge
- `index.md` — catalog of all concept articles
- `concepts/` — atomic encyclopedia articles
- `connections/` — cross-cutting insights
- `log.md` — append-only build log

## Privacy
- `_raw/*.md` digests contain raw conversation content. Gitignored by default.
- `concepts/` and `connections/` are sanitized by Opus. Safe to commit.
