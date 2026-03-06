# Validation Standards

Validation is the automated QE process that runs after a milestone ships. It checks
that what was built matches what was specified — not just that the code runs, but that
it is accurate, usable, and actionable.

## Two Levels of Validation

**Level 1 — Spec assertions** (in every `## Validation` block)

Shell commands that verify the feature exists and returns expected structure.
These run as part of the `validate` node in the breadforge graph. They must:

- Exit 0 on success
- Use `--dry-run` or `--json` to avoid side effects
- Use `jq -e` to assert specific fields, not just that the command runs
- Be runnable without live API credentials (dry-run or mocked data)

Example:
```bash
breadwinner performance --json | jq -e '.summary | has("alpha_pct") and has("portfolio_return_pct")'
breadwinner speculation-performance --json | jq -e 'has("equity") and has("snipe")'
```

**Level 2 — Quality assertions** (in the `## QE` block, optional but recommended)

Deeper checks that validate accuracy, usability, and actionability. These may require
live data or real API calls and are run manually or in a separate QE pass.

## QE Block Format

Add a `## QE` section to any spec that produces user-facing output:

```markdown
## QE

### Accuracy
- [ ] Output values match what you'd compute by hand from the underlying data
- [ ] Stale/missing data is flagged, not silently treated as zero
- [ ] No off-by-one errors in date ranges or index calculations

### Usability
- [ ] Output is readable without documentation
- [ ] Column headers and field names are self-explanatory
- [ ] Error messages name the problem and suggest a fix
- [ ] `--help` output matches actual behavior

### Actionability
- [ ] A human reading the output can make a decision without needing additional context
- [ ] The "what to do" is clear from the output — not buried in intermediate values
- [ ] Edge case outputs (empty data, insufficient history) guide the user forward
```

## What the QE Agent Does

When breadforge dispatches a QE agent for a milestone:

1. **Read the spec** — understand what the feature is supposed to do
2. **Read the code** — understand what it actually does
3. **Run the feature** — with real or realistic data, not just the validation assertions
4. **Stress test edge cases**:
   - Empty database (no trades, no picks, no history)
   - Single data point (one trade, one day of history)
   - Maximum realistic data (hundreds of picks, years of history)
   - Malformed or missing input (bad strategy ID, missing config file)
   - API failures (Alpaca unreachable, rate limited, returning 500)
5. **Check accuracy** — compare output against manual calculation for a known dataset
6. **Check usability** — does the output tell a human what they need to know?
7. **Check actionability** — can a human act on this output without additional research?
8. **File bugs for each gap** — one issue per gap, labeled `bug`, with:
   - Expected behavior (from spec)
   - Actual behavior (from observation)
   - Reproduction steps
   - Severity (P0 = wrong output, P1 = confusing output, P2 = missing edge case handling)

## Accuracy Checks

For financial output specifically:

- **Verify P&L calculations** — compute expected return by hand for a known position
  and compare against the output. Differences of > 0.01% are bugs.
- **Verify benchmark comparisons** — fetch benchmark price for the same date range
  independently and verify alpha calculation.
- **Verify date ranges** — check that `--since` is inclusive, that "today" uses market
  close not UTC midnight, and that partial days are handled correctly.
- **Verify aggregation** — sum individual position returns manually and compare to the
  portfolio total. Check for double-counting.

## Usability Checks

- Can you understand what each column means without reading the code?
- Are numbers formatted consistently (2 decimal places, % signs, $ signs)?
- Are empty states handled gracefully with a message, not a traceback?
- Does `--help` describe what the command actually does (not aspirational behavior)?

## Actionability Checks

- After reading the output, do you know what to do next?
- If the system recommends a trade, does it give you enough detail to place it?
- If the system reports a problem, does it tell you how to fix it?
- If data is insufficient, does it say how much more is needed and when?
