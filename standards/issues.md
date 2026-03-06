# Issue Standards

## Format

Every issue body must contain:

```
**Milestone:** <version slug>
**Module:** `<module-name>`

**What to implement:** <1-3 sentences describing exactly what code to write.
Be specific: name the functions, classes, and behaviors — not just the feature.>

**Files to create/modify (your scope only):**
- `path/to/file.py`
- `path/to/test_file.py`
```

Do not include vague descriptions like "add support for X". Write what the agent
must produce: "Add `class Foo` to `bar.py` with methods `baz()` and `qux()`, each
returning a `Result` type. Write unit tests in `tests/test_bar.py`."

## Before Claiming

Before claiming an issue (`in-progress` label), verify:

1. The required files don't already have the implementation:
   ```bash
   gh pr list --repo <repo> --state merged --limit 30 --json number,title
   ```
2. No other agent has the same files in scope right now (check open `in-progress` issues).
3. All prerequisite modules from the spec are already merged.

If the work is already done: close the issue directly with a comment explaining which
PR covered it. Do NOT create a PR with no meaningful changes.

## Scope Rules

- One module per issue. One issue per branch. One branch per PR.
- If you need to touch a file not in your scope, stop. File a new issue for that module.
- Never modify `pyproject.toml`, `CLAUDE.md`, or `README.md` unless explicitly listed
  in your allowed files.
- The `.breadforge-scope` file is managed by breadforge. Never edit it.
