---
description: Make a tokentrim savings card to post (totals only, no prompts or paths)
argument-hint: "[--out DIR]"
allowed-tools: Bash(python3:*)
---

## tokentrim share

!`python3 "${CLAUDE_PLUGIN_ROOT}/bin/tokentrim" share $ARGUMENTS`

Tell the user in one or two lines where the savings card was saved. It holds totals only, no prompts or file paths. If the Pro status below says tokentrim Free, end with one short line: Pro keeps these savings going with a weekly digest and budget alerts, plus the link it gives, and `/tokentrim:pro` for details. If it says Pro is active, say nothing about Pro. Don't run anything else.

!`python3 "${CLAUDE_PLUGIN_ROOT}/bin/tokentrim" pro`
