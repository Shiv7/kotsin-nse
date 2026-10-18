# Start here

Read **`docs/HANDOFF.md`** first: what this project is, every strategy tried across the Kotsin modules
and its verdict, the research protocol (the 2025-10 → 2026-10 holdout is untouched), the current
candidate strategy and the open work. Then `docs/ARCHITECTURE.md`, `docs/LEARNINGS.md` and
`docs/FIVEPAISA_FACTS.md`.

Rules in short: broker is 5paisa; never print credentials (`backend/.env`); commit as
`Shiv7 <yshiv866@gmail.com>`; run heavy jobs one at a time (8 GB laptop); ask before arming LIVE,
restarting the live engine or pushing somewhere new. Checks: `cd backend && uv run pytest -q &&
uv run ruff check kotsin_nse tests && uv run lint-imports`.
