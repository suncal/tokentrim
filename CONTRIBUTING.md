# Contributing to tokentrim

Thanks for helping make Claude Code cheaper to run. Issues, rules and measurements are all welcome.

## Ground rules

- **Stdlib only.** tokentrim installs with `git clone`; no `pip install`, no dependencies.
- **Fail open.** A hook that errors must let Claude Code carry on as if tokentrim weren't there.
- **Local only.** Nothing about a user's prompts, code or transcripts leaves their machine.
- **Measure it.** If a change claims to save tokens, show before/after from `bin/tokentrim replay` or `audit` in the PR.
- **Lossless where possible.** If a rule drops content, keep a full copy on disk and say where.

## Getting started

```bash
git clone https://github.com/suncal/tokentrim && cd tokentrim
python3 -m unittest discover -s tests    # Python 3.9+
bin/tokentrim audit                      # try it on your own sessions
```

Rules live in `tokentrim/filters.py` (output trimming), `tokentrim/hook.py` (hook entry points) and `tokentrim/config.py` (thresholds). Each new rule needs a test in `tests/test_tokentrim.py`.

## Sharing your numbers

Ran `bin/tokentrim replay`? Post your result in [Discussions → Show and tell](https://github.com/suncal/tokentrim/discussions). Real numbers from different workflows are the most useful contribution of all.

By contributing you agree your work is licensed under Apache-2.0 and that you'll follow the [code of conduct](CODE_OF_CONDUCT.md).
