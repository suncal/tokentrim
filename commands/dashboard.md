---
description: Open the tokentrim usage and savings dashboard in your browser
argument-hint: "[--no-open]"
allowed-tools: Bash(python3:*)
---

## tokentrim dashboard

!`python3 "${CLAUDE_PLUGIN_ROOT}/bin/tokentrim" dashboard $ARGUMENTS`

Tell the user in one or two lines where the dashboard was written and that it opened in their browser. Mention that `python3 "${CLAUDE_PLUGIN_ROOT}/bin/tokentrim" serve` gives a live, auto-refreshing version at http://localhost:4747. If the Pro status below says tokentrim Free, end with one short line: Pro keeps these savings going with a weekly digest and budget alerts, plus the link it gives, and `/tokentrim:pro` for details. If it says Pro is active, say nothing about Pro. Don't run anything else.

!`python3 "${CLAUDE_PLUGIN_ROOT}/bin/tokentrim" pro`
