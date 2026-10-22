# Changelog

## Unreleased

- **Install:** `tokentrim install` now adds the `lean` and `lean-browser` sub-agents to `~/.claude/agents/` and their CLAUDE.md lines. Existing files you wrote are left alone; uninstall removes only unchanged copies.
- **Screenshots:** default scale 0.4; opt-in `screenshot_scale_cap` caps explicit scales too.
- **browser_batch:** opt-in `batch_last_shot_only` keeps only the last screenshot of a batch (off by default).
- **Docs:** [benchmarks page](https://suncal.github.io/tokentrim/benchmarks/) with every measured number and its sample size.
- `scripts/measure_browser.py`: per-day screenshot and sub-agent counts from your transcripts.

## v0.1.0 — 2026-10-18

First public release.

- **Hooks:** half-scale browser screenshots, image downscaling, outline-first reads of huge files, head/tail trimming of long command output, minified connector results, context meter. All fail open; full output is spooled to disk.
- **Profiles:** `balanced`, `strong` (default) and `max` compaction windows.
- **Measure:** `audit` (where tokens went) and `replay` (what tokentrim would have saved, on your own sessions).
- **Dashboard:** `serve`, a plain-English live dashboard: with vs without tokentrim.
- **Share:** `share`, a savings card with totals only.
- **Pro:** `digest` (Monday report), `budget` (weekly token budget with alerts), Slack totals; license keys via Polar.
