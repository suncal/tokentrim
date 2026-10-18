---
description: What tokentrim would have saved on your past Claude Code sessions
argument-hint: "[--window N]"
allowed-tools: Bash(python3:*)
---

## tokentrim replay

!`python3 "${CLAUDE_PLUGIN_ROOT}/bin/tokentrim" replay $ARGUMENTS`

Summarise the replay above for the user in plain English, in at most five lines: tokens with vs without tokentrim, the saving as a percentage, and the one rule that saved the most. Don't run anything else.
