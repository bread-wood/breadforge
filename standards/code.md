# Code Standards

## Readability

Code must be readable by someone who hasn't seen it before.

- **Names are self-documenting** — `fetch_candidate_signals()` not `get_data()`.
  Variables describe what they hold, not how they're computed.
- **Functions do one thing** — if a function needs an internal comment to explain
  a section of it, that section is a separate function.
- **No magic numbers** — name constants. `MAX_RETRIES = 3` not `for _ in range(3)`.
- **Fail loudly at the boundary** — validate inputs at public API boundaries.
  Inside a module, trust your own invariants and don't over-defensively check.
- **Comments explain why, not what** — the code says what it does. A comment says
  why the unusual choice was made or what constraint the code works around.

## Testability

Code must be structured so it can be tested without real infrastructure.

- **Inject dependencies** — pass `store`, `logger`, `config` as parameters rather
  than importing globals inside functions. This makes mocking trivial.
- **Side effects at the edges** — pure transformation logic in the core, I/O at
  the outer layer. A function that fetches data AND transforms it is two functions.
- **No hidden globals** — do not read `os.environ` deep inside business logic.
  Read config at startup and pass it down.
- **Deterministic** — no `datetime.now()` or `random` calls in business logic without
  an injectable clock or seed. Tests must produce the same result on every run.

## Extensibility

Code must be easy to change without rewriting.

- **Open to extension, closed to modification** — add new behaviors by adding code
  (new handler, new strategy, new node type), not by adding `if` branches to existing
  functions.
- **Small, composable units** — prefer a pipeline of small functions over one large
  orchestrating function. Each step can be tested and replaced independently.
- **Avoid premature abstraction** — three callers justifies an abstraction. One or
  two callers: just write the code directly.

## Efficiency

Code must not do unnecessary work.

- **Fetch once** — if you need the same data in multiple places, fetch it once and
  pass it. Do not re-query GitHub or the bead store for the same object repeatedly.
- **Fail fast** — check preconditions early. Don't do expensive work before a check
  that would have rejected it cheaply.
- **Batch where possible** — prefer one API call that returns N results over N calls
  that return one result each.

## Hygiene

- **No junk files** — never stage `__pycache__/`, `*.pyc`, `.coverage`, `.DS_Store`,
  `*.egg-info/`, `.pytest_cache/`. Run `git status` before committing; if these appear,
  they are already excluded — do not `git add` them.
- **Never `--no-verify`** — the pre-commit hook enforces scope. If it fires, fix the
  commit. The hook exists for a reason.
- **Ruff clean** — `uv run ruff check` and `uv run ruff format --check` must pass on
  the full source tree, not just your files.
- **Typed public interfaces** — every public function and method must have type
  annotations on all parameters and the return type.
- **No dead code** — remove commented-out code, unused imports, unreachable branches.
  Dead code is a maintenance burden and a source of confusion.
- **No secrets** — never commit API keys, tokens, passwords, or `.env` files.
  Reference them via environment variables named in documentation.
