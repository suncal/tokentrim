# Changelog

## v0.1.0 — 2026-10-18

First public release.

- **Hooks:** half-scale browser screenshots, image downscaling, outline-first reads of huge files, head/tail trimming of long command output, minified connector results, context meter. All fail open; full output is spooled to disk.
- **Profiles:** `balanced`, `strong` (default) and `max` compaction windows.
- **Measure:** `audit` (where tokens went) and `replay` (what tokentrim would have saved, on your own sessions).
- **Dashboard:** `serve`, a plain-English live dashboard: with vs without tokentrim.
- **Share:** `share`, a savings card with totals only.
- **Pro:** `digest` (Monday report), `budget` (weekly token budget with alerts), Slack totals; license keys via Polar.
