# tokentrim

**Cut what Claude Code re-reads on every step, measured on your own sessions.**

Most "save 90% tokens" tools shrink tool output. That's the wrong target. In an agent loop, about 98% of the input tokens you're billed for are **cache reads**: Claude Code re-sends the *entire* context on every API call. A session carrying 400k tokens of context pays for those 400k again on each step.

tokentrim does three things:

1. **Measures** where your tokens actually went, from the transcripts Claude Code already keeps on your machine.
2. **Keeps context small**: it sets an earlier compaction window and adds hooks that stop screenshots, images, huge files and long command output from bloating it.
3. **Replays** your real past sessions through those rules, so you get your own number instead of a marketing one.

No dependencies (Python 3.9+ stdlib), nothing leaves your machine, and every hook fails open.

## The honest numbers

From the author's 103 real sessions (24,706 API calls, 10.9 billion context tokens):

| | context tokens re-sent | cut |
|---|---|---|
| as it happened | 10,879,692,996 | — |
| output trimming only (hooks) | 10,171,940,933 | **6.5%** |
| + `balanced` profile (300k window) | | **66.5%** |
| + `strong` profile (200k window, default) | | **74.5%** |
| + `max` profile (150k window) | | **78.0%** |

Why compaction dominates: the median API call carried **408k tokens** of context. 25 sessions that ran 300+ calls produced 93% of all cache reads. Context grows, and every later step re-pays for all of it.

**Why it isn't 90%:** Claude Code's own system prompt, tool schemas and app instructions are about 40–60k tokens on every call, and no setting removes them. Once sessions are capped, that floor is most of what's left. Anyone promising 90% across *all* work is either measuring a single noisy `npm install` or not measuring.

**Tokens vs. cost:** cache reads are cheap per token (about 0.1× input price), and output tokens aren't touched. So the drop in *billed cost* / usage-limit burn is smaller than the token drop. On the same data it works out to roughly **55–70%**, depending on how much of your cache-write spend comes from cache misses on long sessions.

**The trade-off:** earlier compaction means Claude summarises sooner and forgets detail sooner. `strong` triggers around 150k tokens, which is close to how Claude Code behaved with a 200k window, and is the default. `max` is cheaper but can thrash on sessions that read huge files.

## What it changes

| Rule | Hook | What it does | Lossy? |
|---|---|---|---|
| Compaction window | `env` | `CLAUDE_CODE_AUTO_COMPACT_WINDOW`; compaction fires at ~75% of it (measured) | summarises old turns |
| Screenshots | PreToolUse | Browser `screenshot`/`zoom` (Claude in Chrome, built-in browser, `browser_batch`) get `scale: 0.5`, so **¼ the tokens**. Coordinates stay in full-res space. Explicit `scale` and `save_to_disk` are respected | lower-res image |
| Images via Read | PreToolUse | Images over 1024px are downscaled with `sips` to a cached JPEG first | lower-res image |
| Huge files | PreToolUse | Whole-file Read of a file over 40 KB returns an **outline** (defs/headings + line numbers) first. Asking again lets the full read through | no: retry gets everything |
| Long Bash output | PostToolUse | Over 6k chars → head + tail; identical-line runs collapsed; ANSI/progress bars stripped. **Full output spooled to disk**, path included | no: full copy on disk |
| MCP results | PostToolUse | JSON minified, null/empty fields dropped, base64 blobs removed, list items never dropped; over 12k chars clipped with full copy on disk | no: full copy on disk |
| Context meter | UserPromptSubmit | Past 120k tokens, tells **you** (not Claude, 0 tokens) to `/compact` or start fresh | — |
| Guidance | `~/.claude/CLAUDE.md` | ~110 tokens: prefer page text over screenshots, Edit over Write, ranged reads | — |

## Install

```bash
git clone https://github.com/suncal/tokentrim && cd tokentrim
bin/tokentrim audit                  # where your tokens went
bin/tokentrim replay                 # what tokentrim would have saved, on your sessions
bin/tokentrim install                # strong profile; or --profile balanced|max, --no-guidance
bin/tokentrim status                 # what's installed + recent hook activity
bin/tokentrim uninstall              # removes exactly what install added
```

`install` backs up `~/.claude/settings.json` first, keeps all your existing hooks and env, and is idempotent. New sessions pick it up.

Tune any threshold in `~/.config/tokentrim/config.json` (see `tokentrim/config.py`). Set `"enabled": false` to switch every hook off instantly.

## Pairs well with

- **[graphify](https://github.com/Graphify-Labs/graphify)**: query a code graph instead of grepping. Measured 13k → 130 tokens for "what calls X?" on a 290 KB codebase.
- **[mcpslim](https://github.com/suncal/mcpslim)**: the same JSON shrinking as a standalone MCP proxy, for non-Claude-Code clients.

## Tests

```bash
python3 -m unittest discover -s tests
```

The hooks were also verified live in headless Claude Code 2.1.299 (Bash trimming, outline-first reads, image downscaling, compaction trigger point).

## License

Apache-2.0
