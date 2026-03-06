# Pull Request Standards

## Format

```markdown
## Summary

- What was implemented (1-3 bullets)
- Why this approach was chosen if non-obvious

## What's already on mainline

If any of the required work was already implemented by a previous PR, list it here:
- `feat(module-foo)` in PR #N: covered X and Y
- What remained and what this PR adds

## Test plan

- [ ] `uv run pytest` — N passed, 0 failed
- [ ] `uv run ruff check` — clean
- [ ] `uv run ruff format --check` — clean
- [ ] Specific behaviors verified: <describe manual checks if any>

Closes #<N>
```

## Before Opening

Check all of the following before running `gh pr create`:

1. `uv run pytest` — all tests pass
2. `uv run ruff check` — clean (excluding pre-existing E402 in cli.py files)
3. `uv run ruff format --check` — clean
4. `git diff --name-only origin/<default-branch>` — only files in your allowed scope
5. The diff is non-trivial — if it's small, explain why in the summary

## CI

Watch CI after opening:

```bash
gh pr checks <PR-number> --watch
```

A PR is not done until CI is green. Do not stop after creating the PR if CI is running.
Wait for it. If CI fails, read the logs and fix the issue.

## Feedback Triage

After CI passes, check for review feedback:

```bash
gh pr view <PR-number> --json reviews,comments
gh api repos/<owner>/<repo>/pulls/<PR-number>/comments
```

For each piece of feedback:

| Verdict | Criteria | Action |
|---------|----------|--------|
| **Fix now** | In scope, clear, correct | Fix, push, re-check CI |
| **File issue** | Valid feedback but out of your scope | `gh issue create --repo <repo>`, note in PR comment |
| **Skip** | False positive, style preference, disagree with rationale | Explain why in a PR comment |

Never leave feedback unaddressed without a comment.

## What Makes a Bad PR

Do not open a PR that:

- **Only touches README** — if the work is done elsewhere, close the issue directly
- **Has no tests** — every new or modified module needs tests
- **Fails CI** — do not ask for review on a red PR
- **Touches out-of-scope files** — the pre-commit hook will catch this; fix it
- **Has no issue reference** — every PR closes an issue
- **Bundles multiple issues** — one issue, one PR
