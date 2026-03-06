# Commit Standards

## Format

```
<type>(<scope>): <short description>

[optional body]

[optional footer: Closes #N]
```

The subject line must be ≤ 72 characters. Use the imperative mood: "add", "fix",
"remove" — not "added", "fixes", "removed".

## Types

| Type | When to use |
|------|-------------|
| `feat` | New feature or behavior visible to users |
| `fix` | Bug fix |
| `test` | Adding or correcting tests (no production code change) |
| `refactor` | Code restructuring with no behavior change |
| `chore` | Maintenance: dependency updates, config changes, CI |
| `docs` | Documentation only |
| `perf` | Performance improvement |

## Scope

The scope is the module or subsystem being changed. Use the module label from the
issue where applicable:

```
feat(speculation-cli): add --dry-run flag to speculate command
fix(signal-fetcher): handle None momentum when no trades exist
test(execution-service): add snipe routing coverage
chore(infra): update ruff to 0.4.0
```

## Body

Include a body when the change is non-obvious:

- Explain **why**, not what (the diff shows what)
- Reference design decisions, constraints, or tradeoffs
- Note anything the reviewer should pay special attention to

## Footer

Always close the issue on the final commit of a PR:

```
Closes #42
```

Use exactly `Closes #N` (not "closes", "fixes", "resolves") for consistency.

## Rules

- **One logical change per commit** — don't bundle unrelated fixes. If you fix a bug
  while implementing a feature, make two commits.
- **Never commit broken state** — every commit must pass `uv run pytest` and
  `uv run ruff check`. Commits that break CI make bisection impossible.
- **Never amend pushed commits** — if you need to fix a pushed commit, make a new one.
  Force-pushing disrupts CI and confuses reviewers.
- **No merge commits** — rebase onto the default branch, don't merge it in.
  `git fetch origin && git rebase origin/<default-branch>`
