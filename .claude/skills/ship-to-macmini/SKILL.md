---
name: ship-to-macmini
description: Commit and push local changes from MacBook to GitHub, wait for Mac Mini watchdog to auto-pull, verify bot health via SSH. Use at end of a work session to deploy changes to production.
argument-hint: "[optional-commit-message]"
---

# Ship to Mac Mini

Use at end of session to deploy local changes to the production Mac Mini.

The Mac Mini runs `scripts/watchdog.sh` which auto-pulls from `origin/master` every 5 minutes via its `_git_sync` function. This skill codifies the push-and-verify loop around that auto-pull so "I pushed, I think it worked" becomes "I verified the bot is still running on the new commit".

---

## 1. Local sanity checks

```bash
git status
git diff --stat
```

Run syntax validation on every staged Python file:

```bash
for f in $(git diff --name-only --cached | grep '\.py$'); do
  venv/bin/python -c "import ast; ast.parse(open('$f').read())" && echo "$f: OK"
done
```

If anything fails — **stop here**. Never push broken syntax.

---

## 2. Commit

Follow the repo convention:

- `feat(T-##): <short description>` — new feature
- `fix: <short description>` — bug fix
- `docs: <short description>` — docs/TASKS.md/CLAUDE.md
- `chore: <short description>` — tooling/scripts
- `refactor: <short description>` — structural change without behavior change

**Rules:**
- Stage only the files you intended — avoid `git add .` unless you've inspected every untracked file
- Do **not** use `--no-verify` — if a hook fails, fix the underlying issue
- Do **not** commit secrets (`.env`, credentials.json) even if staged by accident

---

## 3. Push

```bash
git push origin master
```

If the push is rejected due to a non-fast-forward, someone else (or Mac Mini's own watchdog pushing a state commit, which shouldn't happen) updated `master`. Run `git fetch && git log --oneline origin/master..HEAD` to see divergence before force-anything.

---

## 4. Watch Mac Mini auto-pull

The watchdog runs every 5 min, so the new commit will land within that window. To confirm:

```bash
ssh mac-mini "tail -20 /Volumes/SanDisk/dev/projects/polymarket-sports/logs/git_sync.log"
```

Look for a line like:
```
2026-04-15 22:00:12 New commits — pulling...
2026-04-15 22:00:15 Done: <sha> <message>
```

If the log doesn't show the new commit within 6 minutes, something is wrong with the watchdog itself — check that it's still running:

```bash
ssh mac-mini "pgrep -fl watchdog.sh || echo 'WATCHDOG NOT RUNNING'"
```

---

## 5. Verify bot health

```bash
# Is the bot process still alive?
ssh mac-mini "pgrep -fl bot_main || echo 'BOT NOT RUNNING'"

# Last 30 lines of bot.log — look for errors around the restart
ssh mac-mini "cd /Volumes/SanDisk/dev/projects/polymarket-sports && tail -30 logs/bot.log"
```

If the bot crashed on startup (e.g. import error from a half-applied change), the log will show the traceback. Fix and push again — **do not** ssh in and edit files on Mac Mini directly.

---

## 6. Apply schema changes (manual — watchdog does code only)

If this sprint added a `CREATE TABLE` / `ALTER TABLE` to `db/schema.sql`, the watchdog will **not** apply it for you. Do it now via SSH tunnel:

```bash
ssh -f -N -L 15432:localhost:5432 mac-mini
PGPASSWORD=postgres psql -h localhost -p 15432 -U postgres -d polymarket_sports -f db/schema.sql
pkill -f "ssh -f -N -L 15432:localhost:5432"
```

`CREATE TABLE IF NOT EXISTS` makes this idempotent — safe to re-run.

For one-time backfill jobs (e.g. `historical_fetcher.py`), launch them via SSH inside a detached screen or with `nohup`:

```bash
ssh mac-mini "cd /Volumes/SanDisk/dev/projects/polymarket-sports && nohup venv/bin/python -m analytics.historical_fetcher >> logs/historical_fetch.log 2>&1 &"
```

---

## 7. Memory update

If something surprised you during this sprint (API shape, library quirk, Rotowire DOM change) and it isn't already captured, add one line under the right category:

- **feedback memory** — a new rule to apply in future sessions
- **project memory** — a fact about the project state (freeze dates, stakeholder decisions)
- **reference memory** — an external resource pointer

Skip if there's nothing load-bearing to save — stale memories are worse than missing ones.

---

## Gotchas

- **Uncommitted state on Mac Mini** — the `M .obsidian/workspace.json` entries in `git status` on Mac Mini are harmless (SanDisk filemode quirks). The watchdog `reset --hard` advances HEAD correctly despite them.
- **FDA / sshd** — SSH on Mac Mini doesn't have Full Disk Access to `/Volumes/SanDisk`. That's why the watchdog runs from a GUI session, not from cron or LaunchAgent. If you launch any long-running job via SSH, wrap it in a command that `cd`s to the SanDisk path first — SSH sessions themselves can `cd` there, it's only cron/LaunchAgent that can't.
- **Never force-push to master** — the Mac Mini watchdog does `git reset --hard origin/master`, so a force-push would silently overwrite production state.
