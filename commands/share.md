---
description: Make a tokentrim savings card to post (totals only, no prompts or paths)
argument-hint: "[--out DIR]"
allowed-tools: Bash(python3:*)
---

## tokentrim share

!`python3 "${CLAUDE_PLUGIN_ROOT}/bin/tokentrim" share $ARGUMENTS`

Tell the user in one or two lines where the savings card was saved. It holds totals only, no prompts or file paths. Don't run anything else.
