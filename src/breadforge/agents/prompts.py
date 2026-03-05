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
) -> str:
    """Build the standard sub-agent prompt for impl work."""
    scope_note = ""
    if allowed_scope:
        scope_note = f"\nAllowed scope (only modify files within): {', '.join(allowed_scope)}"

    return f"""You are implementing GitHub issue #{issue_number} in repo `{repo}` on branch `{branch}`.

Issue: {issue_title}

{issue_body}
{scope_note}

Steps:
1. Clone the repo and create your branch:
   ```
   gh repo clone {repo} .
   git checkout -b {branch}
   git push -u origin {branch}
   ```
2. Read the full issue: `gh issue view {issue_number} --repo {repo}`
3. Before writing any code, reason through the approach, identify constraints, and plan the module breakdown.
4. Implement the changes.{" Only modify files within: " + ", ".join(allowed_scope) if allowed_scope else ""}
5. Run tests — all must pass.
6. Run lint — must be clean.
7. Commit referencing the issue: `git commit -m "feat: <description> (closes #{issue_number})"`
8. `git push`
9. Create PR: `gh pr create --repo {repo} --title "<title>" --body "Closes #{issue_number}"`
10. Watch CI: `gh pr checks <PR-number> --watch`
11. Read feedback: `gh pr view <PR-number> --json reviews,comments`
    Inline comments: `gh api repos/{repo}/pulls/<PR-number>/comments`
12. Triage feedback:
    - Fix now: in scope and clear → fix, push, re-check
    - File issue: valid but out of scope → `gh issue create --repo {repo}`
    - Skip: false positive → note in PR comment
13. STOP. Do not merge.
"""


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
  "approach": "<1-3 sentence summary of the implementation approach>",
  "confidence": <0.0-1.0>,
  "unknowns": ["<open question>", ...],
  "risk_flags": ["novel-domain" | "security" | "multi-module-coordination" | ...]
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
"""
