# Contributing

Thanks for looking. This project has one rule that matters more than the others:

**A claim is not evidence.** If a change makes the system report success without a
file, an exit code and a hash that prove it, the change is wrong regardless of how
elegant it looks.

## Before you open a PR

```bash
python selftest.py     # must be 30/30, exit code 0
python e2e.py          # end-to-end pipeline must finish green
```

Both scripts use only the standard library and write their state to a temporary
directory, so they are safe to run anywhere.

## Adding an anti-pattern

The playbook (`playbook/docs/避雷清单.md`) only accepts entries with four fields:

1. **Symptom** — what you actually saw
2. **Consequence** — what broke, in money, time or trust
3. **Fix** — the smallest change that prevents it
4. **Source** — where it bit you (issue, commit, incident)

"No source" means it is a preference, not a lesson. Preferences belong in a blog post.

## Code style

- Python 3.11+, standard library only. No new dependencies without a strong reason.
- Every new hard constraint gets a selftest assertion, including the negative case.
- Keep the evidence in `verification.json` written by the runner, never by the agent.

## Reporting a security issue

Open an issue without the sensitive details, or contact the maintainer privately if
the report itself contains a credential. Do not paste live API keys into an issue.