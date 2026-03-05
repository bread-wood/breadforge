"""breadforge CLI."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

if TYPE_CHECKING:
    from breadforge.graph.executor import ExecutionGraph

import typer
from rich.console import Console, Group
from rich.table import Table

from breadforge.beads import BeadStore
from breadforge.config import Config, Registry, RepoEntry
from breadforge.health import run_health_checks
from breadforge.logger import Logger
from breadforge.spec import parse_campaign, parse_spec

app = typer.Typer(
    name="breadforge",
    help="Platform build orchestrator — spec-driven, bead-tracked, multi-repo.",
    no_args_is_help=True,
)
console = Console()

repo_app = typer.Typer(help="Manage platform repo registry.")
app.add_typer(repo_app, name="repo")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_repo(repo: str | None) -> str:
    if repo:
        return repo
    # Try to detect from git remote
    r = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    console.print("[red]error:[/red] --repo is required (or run from inside a git repo)")
    raise typer.Exit(1)


_BREADFORGE_BOT = "yeast-bot"

_CI_WORKFLOW_TEMPLATE = """\
name: CI

on:
  push:
    branches: ["{branch}"]
  pull_request:
    branches: ["{branch}"]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - name: Run tests
        run: uv run pytest
      - name: Lint
        run: uv run ruff check
"""

_REQUIRED_LABELS = [
    ("stage/impl", "0075ca", "Implementation task"),
    ("stage/research", "0075ca", "Research task"),
    ("stage/design", "0075ca", "Design task"),
    ("P0", "b60205", "Release blocker"),
    ("P1", "d93f0b", "High priority"),
    ("P2", "e4e669", "Normal priority"),
    ("P3", "0e8a16", "Low priority"),
    ("P4", "c5def5", "Backlog"),
    ("in-progress", "f9d0c4", "Claimed by an agent"),
    ("bug", "d73a4a", "Something isn't working"),
    ("triage", "e4e669", "Pending triage"),
]


def _accept_bot_invitation(repo: str, token: str) -> None:
    """Accept the pending collaborator invitation for *repo* as yeast-bot.

    Uses *token* (BREADFORGE_GH_TOKEN) to authenticate as the bot user and
    calls the GitHub Invitations API.  Silently no-ops if no invitation exists.
    """
    import base64 as _base64  # noqa: F401 — kept for future use; suppress unused warning

    list_r = subprocess.run(
        ["curl", "-s", "-H", f"Authorization: token {token}",
         "https://api.github.com/user/repository_invitations"],
        capture_output=True,
        text=True,
    )
    try:
        invitations = json.loads(list_r.stdout)
        matching = [
            inv["id"] for inv in invitations
            if inv.get("repository", {}).get("full_name", "") == repo
        ]
    except (json.JSONDecodeError, KeyError, TypeError):
        return

    for inv_id in matching:
        subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "PATCH",
             "-H", f"Authorization: token {token}",
             f"https://api.github.com/user/repository_invitations/{inv_id}"],
            capture_output=True,
            text=True,
        )


def _add_bot_collaborator(repo: str) -> None:
    """Add yeast-bot as a push collaborator on *repo* and auto-accept the invitation.

    The PUT call runs as the repo owner (ambient gh credentials — GH_TOKEN stripped
    so we don't accidentally auth as the bot).  The invitation is then accepted via
    BREADFORGE_GH_TOKEN.
    """
    import os as _os

    env = {k: v for k, v in _os.environ.items() if k != "GH_TOKEN"}
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/collaborators/{_BREADFORGE_BOT}",
         "-X", "PUT", "-f", "permission=push"],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        console.print(
            f"  [yellow]warning:[/yellow] could not add {_BREADFORGE_BOT} to {repo}: "
            f"{result.stderr.strip()}"
        )
        return

    console.print(f"  {_BREADFORGE_BOT} added as collaborator on {repo}")

    token = _os.environ.get("BREADFORGE_GH_TOKEN") or ""
    if not token:
        console.print(
            f"  [yellow]warning:[/yellow] BREADFORGE_GH_TOKEN not set; "
            f"cannot auto-accept invitation for {_BREADFORGE_BOT}"
        )
        return

    _accept_bot_invitation(repo, token)
    console.print(f"  {_BREADFORGE_BOT} accepted invitation to {repo}")


def _install_ci_workflow(repo: str, branch: str = "mainline") -> None:
    """Install a basic CI workflow on *repo* if one does not already exist."""
    import base64

    # Check if ci.yml already exists
    r = subprocess.run(
        ["gh", "api", f"repos/{repo}/contents/.github/workflows/ci.yml"],
        capture_output=True,
        text=True,
    )
    if r.returncode == 0:
        return  # already exists — _ensure_ci_auth will patch it if needed

    content = _CI_WORKFLOW_TEMPLATE.format(branch=branch)
    encoded = base64.b64encode(content.encode()).decode()
    subprocess.run(
        ["gh", "api", f"repos/{repo}/contents/.github/workflows/ci.yml",
         "-X", "PUT",
         "-f", "message=ci: install breadforge CI workflow",
         "-f", f"content={encoded}"],
        capture_output=True,
        text=True,
    )


def _init_empty_repo(repo: str) -> None:
    """Create an initial empty commit on repos with no commits, using 'mainline' as default."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="breadforge-init-") as tmpdir:
        tmppath = Path(tmpdir)
        subprocess.run(
            ["gh", "repo", "clone", repo, "."],
            cwd=tmppath,
            capture_output=True,
            text=True,
        )
        subprocess.run(["git", "checkout", "-b", "mainline"], cwd=tmppath, capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "chore: initialize repository"],
            cwd=tmppath,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "push", "-u", "origin", "mainline"],
            cwd=tmppath,
            capture_output=True,
            text=True,
        )
        # Set the GitHub default branch to mainline
        subprocess.run(
            ["gh", "api", f"repos/{repo}", "-X", "PATCH", "-f", "default_branch=mainline"],
            capture_output=True,
            text=True,
        )


def _ensure_ci_auth(repo: str) -> None:
    """Patch ci.yml to authenticate sibling repo clones with GITHUB_TOKEN. Idempotent."""
    import base64

    r = subprocess.run(
        ["gh", "api", f"repos/{repo}/contents/.github/workflows/ci.yml"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return  # no ci.yml — nothing to patch
    try:
        data = json.loads(r.stdout)
        content = base64.b64decode(data["content"]).decode("utf-8")
        sha = data["sha"]
    except (json.JSONDecodeError, KeyError, Exception):
        return

    # Already patched or no unauthenticated sibling clones
    if "x-access-token:${GH_TOKEN}@github.com" in content:
        return
    if "git clone https://github.com/" not in content:
        return

    # Patch: wrap git clone steps with GH_TOKEN env and use token URL
    import re

    def _patch_clone_step(m: re.Match) -> str:
        block = m.group(0)
        # Already has GH_TOKEN env
        if "GH_TOKEN" in block:
            return block
        # Add env block and rewrite URLs
        block = block.replace(
            "git clone https://github.com/",
            "git clone https://x-access-token:${GH_TOKEN}@github.com/",
        )
        # Insert env: block before `run:` in this step
        block = re.sub(
            r"(\s+run:\s*\|)",
            r"\n        env:\n          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}\1",
            block,
            count=1,
        )
        return block

    patched = re.sub(
        r"- name: Clone sibling deps\n(?:[ \t]+.*\n)*?(?=[ \t]*-[ \t]|\Z)",
        _patch_clone_step,
        content,
        flags=re.MULTILINE,
    )

    if patched == content:
        return  # regex didn't match anything — leave it alone

    encoded = base64.b64encode(patched.encode("utf-8")).decode("ascii")
    subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo}/contents/.github/workflows/ci.yml",
            "-X",
            "PUT",
            "-f",
            "message=fix(ci): authenticate sibling dep clones with GITHUB_TOKEN",
            "-f",
            f"content={encoded}",
            "-f",
            f"sha={sha}",
        ],
        capture_output=True,
        text=True,
    )


def _scaffold_repo(repo: str) -> None:
    """Ensure all required labels exist on the repo and the repo has at least one commit. Idempotent."""
    # Initialize empty repos before creating labels (labels fail on empty repos too)
    r = subprocess.run(
        ["gh", "repo", "view", repo, "--json", "isEmpty,defaultBranchRef"],
        capture_output=True,
        text=True,
    )
    try:
        info = json.loads(r.stdout)
        if info.get("isEmpty") or not info.get("defaultBranchRef", {}).get("name"):
            _init_empty_repo(repo)
    except (json.JSONDecodeError, KeyError):
        pass

    # Install CI workflow if missing, then patch auth if needed
    _install_ci_workflow(repo, branch="mainline")
    _ensure_ci_auth(repo)

    # Get existing labels
    r = subprocess.run(
        ["gh", "label", "list", "--repo", repo, "--json", "name", "--limit", "100"],
        capture_output=True,
        text=True,
    )
    try:
        existing = {item["name"] for item in json.loads(r.stdout)}
    except (json.JSONDecodeError, KeyError):
        existing = set()

    for name, color, description in _REQUIRED_LABELS:
        if name not in existing:
            subprocess.run(
                [
                    "gh",
                    "label",
                    "create",
                    name,
                    "--repo",
                    repo,
                    "--color",
                    color,
                    "--description",
                    description,
                ],
                capture_output=True,
                text=True,
            )


def _get_store(config: Config) -> BeadStore:
    return BeadStore(config.beads_dir, config.repo)


def _get_logger(config: Config, run_id: str | None = None) -> Logger:
    log_dir = config.beads_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return Logger(log_dir / f"{config.repo.replace('/', '_')}.jsonl", run_id=run_id)


def _get_open_issues_for_milestone(repo: str, milestone: str) -> list[dict]:
    r = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--milestone",
            milestone,
            "--state",
            "open",
            "--json",
            "number,title,labels",
            "--limit",
            "200",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return []


def _file_issue(repo: str, title: str, body: str, milestone: str, labels: list[str]) -> int | None:
    cmd = [
        "gh",
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        title,
        "--body",
        body,
        "--milestone",
        milestone,
    ]
    for label in labels:
        cmd += ["--label", label]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    # Parse issue number from URL
    url = r.stdout.strip()
    try:
        return int(url.rstrip("/").split("/")[-1])
    except (ValueError, IndexError):
        return None


def _ensure_milestone(repo: str, milestone: str) -> bool:
    # Check if it exists
    r = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo}/milestones",
            "--jq",
            f'[.[] | select(.title=="{milestone}")] | length',
        ],
        capture_output=True,
        text=True,
    )
    try:
        count = int(r.stdout.strip())
        if count > 0:
            return True
    except (ValueError, TypeError):
        pass
    # Create it
    r = subprocess.run(
        ["gh", "api", f"repos/{repo}/milestones", "--method", "POST", "-f", f"title={milestone}"],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def _seed_work_beads(
    store: BeadStore,
    issues: list[dict],
    milestone: str,
    spec_file: str | None = None,
    repo: str = "",
) -> list[int]:
    """Create WorkBeads for issues that don't already have one. Returns new issue numbers."""
    from breadforge.beads import WorkBead

    new_numbers = []
    for issue in issues:
        n = issue["number"]
        existing = store.read_work_bead(n)
        if existing is not None:
            # Always sync the title from GitHub — the issue may have been renamed
            # after the bead was first created (e.g. plan handler renames module issues).
            if existing.title != issue["title"]:
                existing.title = issue["title"]
                store.write_work_bead(existing)
            continue
        bead = WorkBead(
            issue_number=n,
            repo=repo,
            title=issue["title"],
            milestone=milestone,
            spec_file=spec_file,
        )
        store.write_work_bead(bead)
        new_numbers.append(n)
    return new_numbers


def _print_dry_run_summary(
    milestone: str,
    graph: ExecutionGraph,
    store: BeadStore | None,
) -> None:
    """Print a rich summary table of the dry-run plan output."""

    console.print(f"\n[bold yellow][dry-run] Plan summary for {milestone}[/bold yellow]")

    # Find the plan node to get the PlanArtifact
    plan_node = None
    build_nodes = []
    for node in graph.all_nodes():
        if node.type == "plan" and node.state == "done":
            plan_node = node
        elif node.type == "build":
            build_nodes.append(node)

    if plan_node and plan_node.output.get("artifact"):
        artifact = plan_node.output["artifact"]
        console.print(f"[dim]Approach:[/dim] {artifact.get('approach', '')}")
        console.print(
            f"[dim]Confidence:[/dim] {artifact.get('confidence', 0):.0%}  "
            f"[dim]Risk:[/dim] {', '.join(artifact.get('risk_flags', [])) or 'none'}"
        )
        if artifact.get("unknowns"):
            console.print(f"[dim]Unknowns resolved:[/dim] {', '.join(artifact['unknowns'][:3])}")

    if not build_nodes:
        console.print("[yellow]No build nodes emitted — check plan output above.[/yellow]")
        return

    table = Table(title="Work Beads (not dispatched)", show_lines=True)
    table.add_column("Module", style="bold")
    table.add_column("Issue #")
    table.add_column("Files")
    table.add_column("Bead State")

    for node in sorted(build_nodes, key=lambda n: n.id):
        module = node.context.get("module", node.id)
        issue_number = node.context.get("issue_number")
        files = node.context.get("files", [])
        bead_state = "—"
        if issue_number and store:
            bead = store.read_work_bead(issue_number)
            bead_state = bead.state if bead else "missing"
        table.add_row(
            module,
            f"#{issue_number}" if issue_number else "—",
            "\n".join(files[:6]) + ("\n…" if len(files) > 6 else ""),
            bead_state,
        )

    console.print(table)
    console.print(
        f"\n[green]{len(build_nodes)} work bead(s) created[/green] — "
        "run without [bold]--dry-run[/bold] to dispatch agents."
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def run(
    specs: Annotated[list[Path], typer.Argument(help="Spec markdown file(s) to run.")],
    repo: Annotated[str | None, typer.Option(help="owner/repo to operate on.")] = None,
    concurrency: Annotated[int, typer.Option(help="Max parallel agents.")] = 3,
    model: Annotated[str | None, typer.Option(help="Override model for all agents.")] = None,
    milestone: Annotated[str | None, typer.Option(help="GitHub milestone name.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Parse spec(s), file GitHub issues, and dispatch agents."""
    import os as _os

    repo = _require_repo(repo)
    config = Config.from_env(repo)
    if concurrency:
        config.concurrency = concurrency
    if model:
        config.model = model

    # All git ops (issue claims, PRs, comments, merges) run as yeast-bot.
    # Always override GH_TOKEN — the bot token takes precedence over any
    # ambient operator credentials so every gh CLI call in the orchestrator
    # and in build agents authenticates as the service account.
    if config.github_token:
        _os.environ["GH_TOKEN"] = config.github_token

    # Health check
    report = run_health_checks(repo)
    if not report.healthy:
        for c in report.fatal:
            console.print(f"[red]FATAL[/red] {c.name}: {c.message}")
        raise typer.Exit(1)

    # Scaffold repo: labels, default branch protection
    if not dry_run:
        console.print(f"Scaffolding {repo}...")
        _scaffold_repo(repo)

    store = _get_store(config)
    logger = _get_logger(config)

    all_issue_numbers: list[int] = []
    _spec_paths: list[tuple[Path, str, int | None]] = []  # (spec_path, milestone, issue_number)

    for spec_path in specs:
        if not spec_path.exists():
            console.print(f"[red]error:[/red] spec file not found: {spec_path}")
            raise typer.Exit(1)

        # Handle campaign files
        if spec_path.name == "campaign.md" or (
            spec_path.suffix == ".md" and "campaign" in spec_path.stem
        ):
            sub_specs = parse_campaign(spec_path)
            if not sub_specs:
                console.print(f"[yellow]warning:[/yellow] no specs found in campaign {spec_path}")
                continue
            for sub_path in sub_specs:
                if sub_path.exists():
                    issue_numbers = _run_single_spec(
                        sub_path, repo, config, store, logger, milestone, dry_run
                    )
                    all_issue_numbers.extend(issue_numbers)
                    ms = milestone or parse_spec(sub_path).version
                    _spec_paths.append((sub_path, ms, issue_numbers[0] if issue_numbers else None))
        else:
            issue_numbers = _run_single_spec(
                spec_path, repo, config, store, logger, milestone, dry_run
            )
            all_issue_numbers.extend(issue_numbers)
            ms = milestone or parse_spec(spec_path).version
            _spec_paths.append((spec_path, ms, issue_numbers[0] if issue_numbers else None))

    if not all_issue_numbers and not _spec_paths:
        console.print("No issues to dispatch.")
        return

    console.print(
        f"\nDispatching {len(_spec_paths)} spec(s) via graph executor "
        f"(concurrency={config.concurrency})..."
    )

    from breadforge.graph.builder import build_greenfield_graph
    from breadforge.graph.executor import GraphExecutor, make_handlers

    if _spec_paths:
        handlers = make_handlers(store=store, logger=logger)
        executor = GraphExecutor(
            config=config,
            handlers=handlers,
            store=store,
            logger=logger,
            concurrency=config.concurrency,
            watchdog_interval=float(config.watchdog_interval_seconds),
            dry_run=dry_run,
        )

        # Clone the repo once for codebase assessment by the plan handler
        import tempfile

        repo_clone_dir = tempfile.mkdtemp(prefix="breadforge-clone-")
        clone_result = subprocess.run(
            ["gh", "repo", "clone", config.repo, repo_clone_dir, "--", "--depth=1"],
            capture_output=True,
            text=True,
        )
        repo_local_path = repo_clone_dir if clone_result.returncode == 0 else ""
        if not repo_local_path:
            console.print(
                f"[yellow]warning:[/yellow] could not clone {config.repo} for codebase assessment"
            )

        async def _run_graph() -> None:
            for spec_path, ms, milestone_issue in _spec_paths:
                graph = build_greenfield_graph(
                    milestone=ms,
                    spec_file=spec_path,
                    repo=config.repo,
                    repo_local_path=repo_local_path,
                    milestone_issue_number=milestone_issue,
                )
                if dry_run:
                    console.print(
                        f"  [yellow][dry-run][/yellow] planning {ms} (research + plan LLMs will run)..."
                    )
                else:
                    console.print(f"  executing graph for {ms}...")
                result = await executor.run(graph)
                if dry_run:
                    _print_dry_run_summary(ms, graph, store)
                else:
                    console.print(
                        f"  {ms}: done={len(result.done)} failed={len(result.failed)} "
                        f"abandoned={len(result.abandoned)}"
                    )

        asyncio.run(_run_graph())
    else:
        # Legacy: rolling dispatcher for issue-number-only runs
        from breadforge.dispatch import RollingDispatcher
        from breadforge.merge import process_merge_queue

        dispatcher = RollingDispatcher(config, store, logger)

        async def _run() -> None:
            dispatch_task = asyncio.create_task(dispatcher.run(all_issue_numbers))

            heartbeat_interval = config.watchdog_interval_seconds

            async def _heartbeat() -> None:
                while not dispatch_task.done():
                    await asyncio.sleep(heartbeat_interval)
                    queue = store.read_merge_queue()
                    logger.heartbeat(
                        active_agents=dispatcher.active_count,
                        queue_depth=len(queue.items),
                        completed=dispatcher.completed_count,
                        cost_usd=0.0,
                    )
                    merged = process_merge_queue(store, config, logger=logger)
                    if merged:
                        console.print(f"  merged {merged} PR(s)")

            await asyncio.gather(dispatch_task, _heartbeat())
            process_merge_queue(store, config, logger=logger)

        asyncio.run(_run())
        console.print(f"\n[green]Done.[/green] Completed: {dispatcher.completed_count}")


def _run_single_spec(
    spec_path: Path,
    repo: str,
    config: Config,
    store: BeadStore,
    logger: Logger,
    milestone_override: str | None,
    dry_run: bool,
) -> list[int]:
    spec = parse_spec(spec_path)
    ms = milestone_override or f"{spec.version}"

    console.print(f"\n[bold]{spec.title}[/bold] → milestone: {ms}")

    # Ensure milestone exists
    if not dry_run and not _ensure_milestone(repo, ms):
        console.print(f"[red]error:[/red] could not create milestone {ms}")
        return []

    # Build issue body from spec
    body = f"{spec.overview}\n\n"
    if spec.success_criteria:
        body += "## Success Criteria\n"
        for c in spec.success_criteria:
            body += f"- [ ] {c}\n"
    body += f"\n_Spec: `{spec_path.name}`_"

    # File one impl issue (agents reason about decomposition themselves)
    existing = _get_open_issues_for_milestone(repo, ms) if not dry_run else []
    impl_issues = [i for i in existing if "stage/impl" in str(i.get("labels", []))]

    issue_numbers: list[int] = []

    if not impl_issues:
        if dry_run:
            console.print(f"  [dry-run] would file impl issue: {spec.issue_title}")
            return [-1]  # sentinel

        issue_number = _file_issue(
            repo,
            title=spec.issue_title,
            body=body,
            milestone=ms,
            labels=["stage/impl", "P2"],
        )
        if issue_number:
            console.print(f"  filed issue #{issue_number}: {spec.issue_title}")
            issue_numbers.append(issue_number)
    else:
        issue_numbers = [i["number"] for i in impl_issues]
        console.print(f"  using existing issues: {issue_numbers}")

    # Seed WorkBeads
    issues_data = [{"number": n, "title": spec.issue_title} for n in issue_numbers]
    new_beads = _seed_work_beads(store, issues_data, ms, str(spec_path), repo)
    if new_beads:
        console.print(f"  seeded {len(new_beads)} work bead(s)")

    logger.info(f"spec {spec_path.name} → {len(issue_numbers)} issue(s)", milestone=ms)
    return issue_numbers


@app.command()
def plan(
    specs: Annotated[list[Path], typer.Argument(help="Spec file(s) to plan (no dispatch).")],
    repo: Annotated[str | None, typer.Option()] = None,
    milestone_prefix: Annotated[str | None, typer.Option()] = None,
) -> None:
    """File GitHub issues for specs without dispatching agents.

    Seeds WorkBeads and records campaign ordering.
    """
    repo = _require_repo(repo)
    config = Config.from_env(repo)
    store = _get_store(config)

    _scaffold_repo(repo)

    from breadforge.beads import CampaignBead, MilestonePlan

    campaign = store.read_campaign_bead() or CampaignBead(repo=repo)
    total_filed = 0

    for i, spec_path in enumerate(specs):
        if not spec_path.exists():
            console.print(f"[red]error:[/red] spec not found: {spec_path}")
            raise typer.Exit(1)
        spec = parse_spec(spec_path)
        ms = f"{spec.version}"

        # Check ordering (basic semver guard)
        if i > 0 and campaign.milestones:
            pass  # TODO: full semver ordering validation

        console.print(f"Planning {spec.title} → {ms}")
        _ensure_milestone(repo, ms)

        # File impl issue
        body = f"{spec.overview}\n\n_Spec: `{spec_path.name}`_"
        issue_number = _file_issue(
            repo,
            title=spec.issue_title,
            body=body,
            milestone=ms,
            labels=["stage/impl", "P2"],
        )
        if issue_number:
            console.print(f"  filed #{issue_number}")
            total_filed += 1
            bead_data = [{"number": issue_number, "title": spec.issue_title}]
            _seed_work_beads(store, bead_data, ms, str(spec_path), repo)

        # Record in campaign
        if not campaign.get_milestone(ms, repo):
            plan_entry = MilestonePlan(milestone=ms, repo=repo, spec_file=str(spec_path))
            campaign.milestones.append(plan_entry)

    store.write_campaign_bead(campaign)
    console.print(f"\nPlanned {total_filed} issue(s). Campaign updated.")


@app.command()
def init(
    milestone: Annotated[str, typer.Option(help="Milestone name to create.")],
    repo: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Create a GitHub milestone (no issue seeding)."""
    repo = _require_repo(repo)
    if _ensure_milestone(repo, milestone):
        console.print(f"[green]ok[/green] milestone '{milestone}' exists in {repo}")
    else:
        console.print(f"[red]error:[/red] could not create milestone '{milestone}'")
        raise typer.Exit(1)


def _build_status_table(
    store: BeadStore,
    repo: str,
    milestone: str | None,
) -> Group:

    tables = []

    beads = store.list_work_beads(milestone=milestone)
    bead_colors = {
        "open": "dim",
        "claimed": "yellow",
        "pr_open": "blue",
        "merge_ready": "green",
        "closed": "green",
        "abandoned": "red",
    }
    bead_table = Table(
        title=f"Work Beads — {repo}" + (f" / {milestone}" if milestone else ""),
    )
    bead_table.add_column("Issue", style="cyan", justify="right")
    bead_table.add_column("Title")
    bead_table.add_column("State")
    bead_table.add_column("Retries", justify="right")
    bead_table.add_column("Branch")
    bead_table.add_column("PR", justify="right")

    def _model_tier(model: str | None) -> str:
        """Extract human-readable tier: opus / sonnet / haiku / ''."""
        if not model:
            return ""
        m = model.lower()
        if "opus" in m:
            return "opus"
        if "sonnet" in m:
            return "sonnet"
        if "haiku" in m:
            return "haiku"
        return model

    for bead in sorted(beads, key=lambda b: b.issue_number):
        color = bead_colors.get(bead.state, "white")
        bead_table.add_row(
            str(bead.issue_number),
            bead.title[:50],
            f"[{color}]{bead.state}[/{color}]",
            str(bead.retry_count) if bead.retry_count else "",
            bead.branch or "",
            str(bead.pr_number) if bead.pr_number else "",
        )
    tables.append(bead_table)

    nodes = store.list_nodes()
    if nodes:
        node_colors = {
            "pending": "dim",
            "running": "yellow",
            "done": "green",
            "failed": "red",
            "abandoned": "red",
        }
        node_table = Table(title="Graph Nodes")
        node_table.add_column("Node ID")
        node_table.add_column("Type")
        node_table.add_column("State")
        node_table.add_column("Model")
        node_table.add_column("Retries", justify="right")
        for node in sorted(nodes, key=lambda n: n.id):
            color = node_colors.get(node.state, "white")
            if node.type == "merge":
                node_model_display = "[dim]N/A[/dim]"
            else:
                node_model = (node.output or {}).get("model") or node.assigned_model or ""
                node_model_display = _model_tier(node_model)
            node_table.add_row(
                node.id,
                node.type,
                f"[{color}]{node.state}[/{color}]",
                node_model_display,
                str(node.retry_count) if node.retry_count else "",
            )
        tables.append(node_table)

        # Estimated cost across all completed nodes
        total_cost = sum(
            (n.output or {}).get("cost_usd", 0.0)
            for n in nodes
            if n.state == "done" and (n.output or {}).get("cost_usd") is not None
        )
        if total_cost > 0:
            from rich.text import Text

            tables.append(Text(f"  Est. cost: ${total_cost:.4f}", style="dim"))

    return Group(*tables)


@app.command()
def status(
    repo: Annotated[str | None, typer.Option()] = None,
    milestone: Annotated[str | None, typer.Option()] = None,
    watch: Annotated[bool, typer.Option("--watch", "-w", help="Refresh every 3s.")] = False,
) -> None:
    """Show bead state table. Use --watch for live refresh."""
    repo = _require_repo(repo)
    config = Config.from_env(repo)
    store = _get_store(config)

    if watch:
        import time

        from rich.live import Live

        with Live(console=console, refresh_per_second=1) as live:
            while True:
                live.update(_build_status_table(store, repo, milestone))
                time.sleep(3)
        return

    beads = store.list_work_beads(milestone=milestone)
    nodes = store.list_nodes()
    if not beads and not nodes:
        console.print("No beads or graph nodes found.")
        return

    # Campaign summary
    campaign = store.read_campaign_bead()
    if campaign and campaign.milestones:
        console.print(f"\n[bold]Campaign[/bold] ({len(campaign.milestones)} milestones)")
        for m in campaign.milestones:
            status_color = {
                "shipped": "green",
                "implementing": "yellow",
                "blocked": "red",
                "failed": "red",
                "pending": "dim",
            }.get(m.status, "white")
            console.print(f"  [{status_color}]{m.status:15}[/{status_color}] {m.milestone}")

    console.print(_build_status_table(store, repo, milestone))

    # Merge queue
    queue = store.read_merge_queue()
    if queue.items:
        console.print(f"\nMerge queue: {len(queue.items)} item(s)")
        for item in queue.items:
            console.print(
                f"  PR #{item.pr_number} (issue #{item.issue_number}, branch: {item.branch})"
            )


@app.command(name="beads")
def beads_cmd(
    repo: Annotated[str | None, typer.Option()] = None,
    state: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Show all beads for a repo."""
    repo = _require_repo(repo)
    config = Config.from_env(repo)
    store = _get_store(config)

    work_beads = store.list_work_beads(state=state)  # type: ignore
    pr_beads = store.list_pr_beads()

    console.print(f"\n[bold]Work Beads[/bold] ({len(work_beads)})")
    for b in sorted(work_beads, key=lambda x: x.issue_number):
        console.print(f"  #{b.issue_number:4}  {b.state:15}  {b.title[:50]}")

    console.print(f"\n[bold]PR Beads[/bold] ({len(pr_beads)})")
    for b in sorted(pr_beads, key=lambda x: x.pr_number):
        console.print(f"  PR #{b.pr_number:4}  {b.state:15}  issue #{b.issue_number}")


@app.command()
def health(
    repo: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Run preflight health checks."""
    repo = _require_repo(repo)
    report = run_health_checks(repo)

    table = Table(title="Health Checks")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Message")

    for check in report.checks:
        color = {"pass": "green", "fail": "red", "warn": "yellow"}[check.status]
        table.add_row(check.name, f"[{color}]{check.status.upper()}[/{color}]", check.message)

    console.print(table)

    if not report.healthy:
        raise typer.Exit(1)


@app.command()
def monitor(
    repo: Annotated[str | None, typer.Option()] = None,
    once: Annotated[bool, typer.Option("--once")] = False,
    interval: Annotated[int, typer.Option(help="Scan interval in seconds.")] = 300,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Run the anomaly monitor loop."""
    repo = _require_repo(repo)
    config = Config.from_env(repo)
    store = _get_store(config)
    logger = _get_logger(config)

    from breadforge.monitor import run_monitor

    console.print(f"Starting monitor for {repo} (interval={interval}s, once={once})")
    asyncio.run(
        run_monitor(
            store,
            config,
            logger,
            once=once,
            interval_seconds=interval,
            dry_run=dry_run,
        )
    )


@app.command(name="spec")
def spec_cmd(
    description: Annotated[str | None, typer.Argument(help="Feature description.")] = None,
    file: Annotated[Path | None, typer.Option("--file", help="Read description from file.")] = None,
    repo: Annotated[str | None, typer.Option()] = None,
    output_dir: Annotated[Path | None, typer.Option()] = None,
    non_interactive: Annotated[bool, typer.Option("--non-interactive")] = False,
) -> None:
    """Interactive spec-forge: draft a milestone spec from a description."""
    registry = Registry()

    from breadforge.forge import spec_forge

    written = asyncio.run(
        spec_forge(
            description=description,
            file=file,
            registry=registry,
            interactive=not non_interactive,
            output_dir=output_dir or Path.cwd(),
        )
    )

    for path in written:
        console.print(f"[green]wrote:[/green] {path}")


@app.command(name="run-issue")
def run_issue(
    issue: Annotated[int, typer.Option(help="GitHub issue number to dispatch.")],
    repo: Annotated[str | None, typer.Option(help="owner/repo to operate on.")] = None,
    concurrency: Annotated[int, typer.Option(help="Max parallel agents.")] = 1,
    model: Annotated[str | None, typer.Option(help="Override model for all agents.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Dispatch a single issue by number. Used by the GitHub Actions pipeline."""
    repo = _require_repo(repo)
    config = Config.from_env(repo)
    if concurrency:
        config.concurrency = concurrency
    if model:
        config.model = model

    # Fetch issue metadata
    r = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(issue),
            "--repo",
            repo,
            "--json",
            "title,milestone,labels",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        console.print(f"[red]error:[/red] could not fetch issue #{issue}: {r.stderr.strip()}")
        raise typer.Exit(1)
    try:
        issue_data = json.loads(r.stdout)
    except json.JSONDecodeError:
        console.print(f"[red]error:[/red] could not parse issue #{issue} response")
        raise typer.Exit(1) from None

    milestone_title: str = (issue_data.get("milestone") or {}).get("title", "")
    issue_title: str = issue_data.get("title", f"Issue #{issue}")

    console.print(f"Dispatching issue #{issue}: {issue_title}")
    if milestone_title:
        console.print(f"  milestone: {milestone_title}")

    if dry_run:
        console.print("[yellow][dry-run][/yellow] would dispatch issue", issue)
        return

    # Health check
    report = run_health_checks(repo)
    if not report.healthy:
        for c in report.fatal:
            console.print(f"[red]FATAL[/red] {c.name}: {c.message}")
        raise typer.Exit(1)

    store = _get_store(config)
    logger = _get_logger(config)

    # Seed a WorkBead for this issue
    bead_data = [{"number": issue, "title": issue_title}]
    _seed_work_beads(store, bead_data, milestone_title or "unknown", None, repo)

    # Try to find a spec file matching this milestone
    spec_path: Path | None = None
    if milestone_title:
        for candidate in sorted(Path("specs").glob("*.md")):
            try:
                sp = parse_spec(candidate)
                if sp.version == milestone_title:
                    spec_path = candidate
                    break
            except Exception:
                continue

    if spec_path:
        from breadforge.graph.builder import build_greenfield_graph
        from breadforge.graph.executor import GraphExecutor, make_handlers

        handlers = make_handlers(store=store, logger=logger)
        executor = GraphExecutor(
            config=config,
            handlers=handlers,
            store=store,
            logger=logger,
            concurrency=config.concurrency,
            watchdog_interval=float(config.watchdog_interval_seconds),
        )

        import tempfile

        repo_clone_dir = tempfile.mkdtemp(prefix="breadforge-clone-")
        clone_result = subprocess.run(
            ["gh", "repo", "clone", config.repo, repo_clone_dir, "--", "--depth=1"],
            capture_output=True,
            text=True,
        )
        repo_local_path = repo_clone_dir if clone_result.returncode == 0 else ""
        if not repo_local_path:
            console.print(
                f"[yellow]warning:[/yellow] could not clone {config.repo} for codebase assessment"
            )

        async def _run_graph() -> None:
            graph = build_greenfield_graph(
                milestone=milestone_title,
                spec_file=spec_path,
                repo=config.repo,
                repo_local_path=repo_local_path,
                milestone_issue_number=issue,
            )
            result = await executor.run(graph)
            console.print(
                f"done={len(result.done)} failed={len(result.failed)} "
                f"abandoned={len(result.abandoned)}"
            )

        asyncio.run(_run_graph())
    else:
        # Fallback: rolling dispatcher for issue-number-only dispatch
        from breadforge.dispatch import RollingDispatcher

        dispatcher = RollingDispatcher(config, store, logger)
        asyncio.run(dispatcher.run([issue]))
        console.print(f"[green]Done.[/green] Completed: {dispatcher.completed_count}")


@app.command()
def cost(
    repo: Annotated[str | None, typer.Option()] = None,
    period: Annotated[str, typer.Option(help="today|week|month|all")] = "all",
) -> None:
    """Show LLM cost summary via breadmin-llm."""
    try:
        from breadmin_llm.queries import query_costs
    except ImportError as e:
        console.print(
            "[yellow]breadmin-llm not installed — install with: pip install breadforge[llm][/yellow]"
        )
        raise typer.Exit(1) from e

    import os
    from pathlib import Path

    db_path = Path(os.environ.get("BREADMIN_DB_PATH", "data/breadmin.db"))
    if not db_path.exists():
        console.print(f"No cost database found at {db_path}")
        return

    results = query_costs(db_path, period=period, caller_prefix="breadforge")
    if not results:
        console.print("No cost records found.")
        return

    table = Table(title=f"LLM Costs ({period})")
    table.add_column("Provider")
    table.add_column("Model")
    table.add_column("Calls", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost (USD)", justify="right")

    for row in results:
        table.add_row(
            row.get("provider", ""),
            row.get("model", ""),
            str(row.get("call_count", "")),
            str(row.get("total_tokens", "")),
            f"${row.get('total_cost', 0):.4f}",
        )

    console.print(table)


# ---------------------------------------------------------------------------
# Repo subcommands
# ---------------------------------------------------------------------------


@repo_app.command("add")
def repo_add(
    repo_name: Annotated[str, typer.Argument(help="owner/repo")],
    local_path: Annotated[Path, typer.Option(help="Local clone path.")],
    spec_dir: Annotated[Path | None, typer.Option(help="Spec directory within repo.")] = None,
    default_branch: Annotated[str, typer.Option()] = "mainline",
) -> None:
    """Register a repo in the platform registry."""
    registry = Registry()
    entry = RepoEntry(
        repo=repo_name,
        local_path=local_path.expanduser().resolve(),
        spec_dir=(spec_dir or local_path / "specs").expanduser().resolve(),
        default_branch=default_branch,
    )
    registry.add(entry)
    console.print(f"[green]Registered[/green] {repo_name}")

    # Scaffold labels, CI workflow
    _scaffold_repo(repo_name)
    console.print("  labels and CI workflow scaffolded")

    # Add yeast-bot as collaborator and accept invitation
    _add_bot_collaborator(repo_name)


@repo_app.command("remove")
def repo_remove(
    repo_name: Annotated[str, typer.Argument()],
) -> None:
    """Remove a repo from the registry."""
    registry = Registry()
    if registry.remove(repo_name):
        console.print(f"[green]Removed[/green] {repo_name}")
    else:
        console.print(f"[yellow]Not found:[/yellow] {repo_name}")


@repo_app.command("list")
def repo_list() -> None:
    """List all registered repos."""
    registry = Registry()
    repos = registry.list()
    if not repos:
        console.print(
            "No repos registered. Use: breadforge repo add <owner/repo> --local-path <path>"
        )
        return

    table = Table(title="Platform Repo Registry")
    table.add_column("Repo")
    table.add_column("Local Path")
    table.add_column("Spec Dir")
    table.add_column("Branch")

    for entry in repos:
        table.add_row(
            entry.repo,
            str(entry.local_path),
            str(entry.spec_dir),
            entry.default_branch,
        )

    console.print(table)


@app.command("gha-dispatch")
def gha_dispatch(
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Dispatch a breadforge run from a GitHub Actions issues event.

    Reads GITHUB_EVENT_NAME, GITHUB_EVENT_PATH, and GITHUB_REPOSITORY from
    the environment (set automatically by GitHub Actions). Triggers the graph
    executor when an issue is labeled with 'stage/impl' and has a milestone
    whose spec file exists under specs/.
    """
    import json
    import os

    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")

    if not repo:
        console.print("[red]error:[/red] GITHUB_REPOSITORY not set")
        raise typer.Exit(1)

    if event_name != "issues":
        console.print(f"[yellow]skip:[/yellow] event {event_name!r} is not 'issues'")
        return

    event: dict = {}
    if event_path and Path(event_path).exists():
        try:
            event = json.loads(Path(event_path).read_text())
        except (json.JSONDecodeError, OSError) as e:
            console.print(f"[red]error:[/red] could not parse event payload: {e}")
            raise typer.Exit(1) from e

    action = event.get("action", "")
    label_name = (event.get("label") or {}).get("name", "")

    if action != "labeled" or label_name != "stage/impl":
        console.print(f"[yellow]skip:[/yellow] action={action!r} label={label_name!r}")
        return

    issue = event.get("issue") or {}
    milestone_obj = issue.get("milestone") or {}
    milestone = milestone_obj.get("title", "")
    if not milestone:
        console.print("[yellow]skip:[/yellow] issue has no milestone")
        return

    # Find spec file for this milestone under specs/
    specs_dir = Path("specs")
    spec_file: Path | None = None
    if specs_dir.exists():
        for candidate in sorted(specs_dir.glob("*.md")):
            stem = candidate.stem
            # Match by milestone slug: v0.2.0 matches v0-2-0 or v0.2.0 in filename
            normalized = milestone.replace(".", "-").lower()
            if normalized in stem.lower() or milestone.lower() in stem.lower():
                spec_file = candidate
                break

    if not spec_file:
        console.print(f"[yellow]skip:[/yellow] no spec found for milestone {milestone!r} in specs/")
        return

    console.print(f"GHA dispatch: repo={repo} milestone={milestone} spec={spec_file}")

    if dry_run:
        console.print("[yellow][dry-run][/yellow] would dispatch graph executor")
        return

    config = Config.from_env(repo)
    store = _get_store(config)
    logger = _get_logger(config)

    issue_numbers = _run_single_spec(
        spec_file, repo, config, store, logger, milestone, dry_run=False
    )

    if not issue_numbers:
        console.print("No issues to dispatch.")
        return

    from breadforge.graph.builder import build_greenfield_graph
    from breadforge.graph.executor import GraphExecutor, make_handlers

    handlers = make_handlers(store=store, logger=logger)
    executor = GraphExecutor(
        config=config,
        handlers=handlers,
        store=store,
        logger=logger,
        concurrency=config.concurrency,
        watchdog_interval=float(config.watchdog_interval_seconds),
    )

    graph = build_greenfield_graph(
        milestone=milestone,
        spec_file=spec_file,
        repo=repo,
        repo_local_path=str(Path.cwd()),
        milestone_issue_number=issue_numbers[0] if issue_numbers else None,
    )

    result = asyncio.run(executor.run(graph))
    console.print(
        f"GHA dispatch done: done={len(result.done)} failed={len(result.failed)} "
        f"abandoned={len(result.abandoned)}"
    )
    if result.failed or result.abandoned:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
