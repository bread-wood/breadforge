# Minesweeper v0.1.0 — Playable GUI Game

## Overview
Minesweeper as a desktop GUI game using pygame. Click to reveal, right-click to flag.

## Success Criteria
- [ ] Game launches with a 9x9 board and 10 mines
- [ ] Left-click reveals a cell; right-click flags/unflags
- [ ] Revealing a mine shows all mines and ends the game
- [ ] Revealing all safe cells shows a win screen
- [ ] Flood-fill reveals adjacent empty cells automatically
- [ ] `uv run pytest` passes

## Scope
### Included
- pygame window with grid rendering
- Mine placement, reveal logic, flood-fill
- Win/loss detection and end screen

### Excluded
- Difficulty settings, timer, high scores

## Key Unknowns
- **[P3]** Cell size and color scheme — agent decides

## Modules
- game: board state, mine placement, reveal, flag logic
- ui: pygame rendering and event loop
