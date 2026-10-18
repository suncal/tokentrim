# Security policy

tokentrim runs as Claude Code hooks, so it sees every tool call. We take that seriously.

## Reporting a vulnerability

Please **don't open a public issue**. Report it privately through [GitHub security advisories](https://github.com/suncal/tokentrim/security/advisories/new). Or email support@boltak.com. You'll get a reply within 3 days.

## What tokentrim does and doesn't do

- Reads Claude Code transcripts **locally** to measure usage; never uploads them.
- Pro's license check sends only the license key and a machine label to Polar.
- Slack posting is opt-in and sends weekly totals only.
- `bin/tokentrim uninstall` removes exactly what `install` added; `"enabled": false` in `~/.config/tokentrim/config.json` disables every hook instantly.

## Supported versions

Only the latest release on `main` gets security fixes.
