"""Agent prompt templates — build, research, and plan prompts."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Build agent prompt (ported from runner.py)
# ---------------------------------------------------------------------------


def build_agent_prompt(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    branch: str,
    repo: str,
    allowed_scope: list[str] | None = None,
    workspace_ready: bool = False,
) -> str:
    """Build the standard sub-agent prompt for impl work."""
    if workspace_ready:
        scope_lines = ""
        if allowed_scope:
            file_list = "\n".join(f"  - {f}" for f in allowed_scope)
            scope_lines = f"""
SCOPE ENFORCEMENT IS ACTIVE. A pre-commit hook will REJECT any commit that modifies
files outside the allowed list. Do not use --no-verify or --no-gpg-sign to bypass it.

Allowed files (create or modify ONLY these):
{file_list}

If you need a file not on this list, check whether it belongs to another module.
Do not modify pyproject.toml, CLAUDE.md, or README.md unless they are on the list above.
"""
        setup_step = f"""\
1. The repo is already cloned in the current directory and branch `{branch}` is already
   created and pushed to origin. Do NOT run `git clone` or `git checkout -b`.
   Verify you are on the right branch: `git branch --show-current` (should print `{branch}`)."""
    else:
        scope_lines = ""
        if allowed_scope:
            scope_lines = f"\nAllowed scope (only modify files within): {', '.join(allowed_scope)}"
        setup_step = f"""\
1. Clone the repo and create your branch:
   ```
   gh repo clone {repo} .
   git checkout -b {branch}
   git push -u origin {branch}
   ```"""

    return f"""You are implementing GitHub issue #{issue_number} in repo `{repo}` on branch `{branch}`.

Issue: {issue_title}

{issue_body}
{scope_lines}
Steps:
{setup_step}
2. Read the full issue: `gh issue view {issue_number} --repo {repo}`
3. Before writing any code, reason through the approach, identify constraints, and plan the implementation.
4. Implement the changes.{" Only modify files within: " + ", ".join(allowed_scope) if allowed_scope and not workspace_ready else ""}
5. Write comprehensive tests for every file you create or modify. For each module:
   - Unit test every public function and class method with meaningful inputs
   - Cover edge cases, error paths, and boundary conditions
   - Do not write trivial smoke tests that only check a function runs without error
   - Aim for full branch coverage of the logic you implement
6. Run the FULL test suite — not just your new tests:
   ```
   uv run pytest
   ```
   All tests must pass. If existing tests fail:
   - Check if they were already failing before your changes: `git stash && uv run pytest && git stash pop`
   - If pre-existing: note it in the PR description and file a follow-up issue, but do NOT leave your own
     changes in a state that makes it worse
   - If your changes caused the regression: fix it before pushing
7. Run lint on the FULL source tree — not just your new files:
   ```
   uv run ruff check
   uv run ruff format --check
   ```
8. Commit referencing the issue: `git commit -m "feat: <description> (closes #{issue_number})"`
9. `git push`
10. Create PR: `gh pr create --repo {repo} --title "<title>" --body "Closes #{issue_number}"`
11. Watch CI: `gh pr checks <PR-number> --watch`
12. Read feedback: `gh pr view <PR-number> --json reviews,comments`
    Inline comments: `gh api repos/{repo}/pulls/<PR-number>/comments`
13. Triage feedback:
    - Fix now: in scope and clear → fix, push, re-check
    - File issue: valid but out of scope → `gh issue create --repo {repo}`
    - Skip: false positive → note in PR comment
14. STOP. Do not merge.

IMPORTANT — if you find that some or all of the work described in this issue has already
been implemented by a previous PR, you MUST leave a comment on the issue explaining:
- Which parts were already done and by which PR(s) (check `gh pr list --repo {repo} --state merged`)
- Which parts (if any) remained and what you implemented
- Why the diff is small if that is the case

Example comment:
  `gh issue comment {issue_number} --repo {repo} --body "Most of this was already implemented in PR #X (module-foo) and PR #Y (module-bar). The remaining gap was [describe]. This PR adds [describe]."`

Do this BEFORE creating the PR so the context is visible on the issue."""


# ---------------------------------------------------------------------------
# Research prompt
# ---------------------------------------------------------------------------

RESEARCH_PROMPT = """You are a research agent. Your job is to investigate the following unknowns
and produce a concise markdown report with your findings.

Repo: {repo}
Milestone: {milestone}
Unknowns to investigate:
{unknowns}

Guidelines:
- Use WebSearch and WebFetch ONLY. Do not access the codebase.
- For each unknown: state the question, summarize findings, and give a recommendation.
- Keep the report under 500 words.
- End with a ## Confidence section: rate your confidence (0.0-1.0) in the findings.

Output ONLY the markdown report. No preamble.
"""


# ---------------------------------------------------------------------------
# Plan prompt
# ---------------------------------------------------------------------------

PLAN_PROMPT = """You are a planning agent. Read the spec and codebase context below,
then produce a structured plan as JSON.

The spec may be minimal (title + free text only) or fully structured. Either is valid.
If sections are absent, infer reasonable structure from the description.

Spec:
{spec_text}

Codebase context:
{codebase_context}

Research findings (if any):
{research_findings}

Produce a JSON object matching this schema exactly (no markdown fences):
{{
  "milestone": "<short slug from the spec title, e.g. v0.1.0 or the milestone name>",
  "modules": ["<module1>", "<module2>"],
  "files_per_module": {{
    "<module1>": ["<path/to/file.py>", ...],
    "<module2>": ["<path/to/file.py>", ...]
  }},
  "approach": "<1-3 sentence high-level summary of the overall implementation strategy>",
  "module_approaches": {{
    "<module1>": "<1-2 sentences: exactly what this ticket must implement — specific to this module only>",
    "<module2>": "<1-2 sentences: exactly what this ticket must implement — specific to this module only>"
  }},
  "confidence": <0.0-1.0>,
  "unknowns": ["<open question>", ...],
  "risk_flags": ["novel-domain" | "security" | "multi-module-coordination" | ...],
  "module_dependencies": {{
    "<module-that-must-build-last>": ["<module-it-depends-on>", ...]
  }}
}}

Rules:
- milestone: use the version slug (e.g. "v0.1.0") or a short identifier — NOT the full title
- confidence < 0.6 means you need more research before planning; set unknowns accordingly
- files_per_module must list the ACTUAL files to create/modify (not directories)
- modules must map 1:1 to buildable units that can be parallelized
- if the spec has no Modules section, propose your own based on the description
- if the spec has no Goals/Scope, infer from the overview what "done" means
- risk_flags: include "security" if auth/crypto involved, "novel-domain" if unfamiliar domain,
  "multi-module-coordination" if modules share mutable state
- module_approaches: every module in "modules" must have an entry; describe only what that
  specific ticket does — not work belonging to other modules
- module_dependencies: list any modules that must be fully merged before another can build;
  omit the key entirely for modules with no dependencies (do not emit empty lists);
  integration-test modules should always depend on all other modules
"""
