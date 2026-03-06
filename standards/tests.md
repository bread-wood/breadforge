# Test Standards

## What Must Be Tested

For every file you create or modify:

- Every public function and class method
- Every error path (`if result.returncode != 0`, every `except`, every early return)
- Every edge case: empty inputs, None values, zero counts, duplicates, max values
- Every conditional branch: `if/else`, `try/except`, loop exits

A test that only verifies a function runs without raising is not a test.
Assert the actual output, the state change, or the side effect.

## Mocking

Unit tests must pass without credentials and without network access.

Mock at the boundary, not deep inside:

```python
# Good — mock the subprocess call that wraps gh
with patch("breadwinner.services.execution.subprocess.run") as mock_run:
    mock_run.return_value = Mock(returncode=0, stdout='{"number": 42}')
    ...

# Bad — mock an internal helper that wraps subprocess
with patch("breadwinner.services.execution._gh") as mock_gh:
    ...  # now you can't test _gh itself
```

Always mock:
- GitHub API calls (`subprocess.run(["gh", ...])`)
- Alpaca/Finnhub/LLM API calls
- `datetime.now()` when time-dependent behavior is tested
- File I/O when testing logic that reads/writes disk (use `tmp_path` pytest fixture)

Never mock:
- Pure data transformation logic
- Your own domain objects and dataclasses
- The module under test itself

## Structure

```python
class TestFooBar:
    """Tests for foo.bar() — one class per function or behavior group."""

    def test_happy_path(self) -> None:
        """Describe what this test proves in the docstring."""
        ...

    def test_empty_input_returns_empty(self) -> None:
        ...

    def test_api_failure_raises_value_error(self) -> None:
        ...
```

- One test class per function or tightly related group of behaviors
- Test method names describe the scenario: `test_<condition>_<expected_result>`
- Docstrings on tests that aren't self-explanatory
- Use `pytest.raises` for expected exceptions; don't catch and assert manually
- Use `tmp_path` (pytest built-in) for temporary files; never write to the real filesystem

## Running Tests

Always run the full suite before pushing:

```bash
uv run pytest
```

A partial suite that passes but breaks existing tests is a regression.

If existing tests fail before your changes:
```bash
git stash && uv run pytest && git stash pop
```

- Pre-existing failures: document in the PR body, file a follow-up issue, do not worsen
- Failures caused by your changes: fix before pushing

## Coverage Targets

- New modules: aim for full branch coverage of the logic you write
- Modified modules: do not decrease coverage
- Integration tests may be skipped without credentials; they must be tagged:
  ```python
  @pytest.mark.integration
  def test_live_alpaca_order(): ...
  ```
  and excluded from the default run via `pytest.ini` or `pyproject.toml`
