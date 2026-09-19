## What changed

## Evidence

- [ ] `make check` passes (ruff, import contracts, pytest, UI build)
- [ ] If a **threshold** changed: its new value is in the strategy doc's parameter register, with
      the sample size it was fitted on. No calibration on n < 30.
- [ ] If a **strategy** changed: `docs/strategies/<KEY>.md` is updated, including the falsifier.
- [ ] If a **config key** was added: something reads it, and a test asserts that it does.
- [ ] If a **gate** was added: it declares `on_missing`, and its rejections are counted.
- [ ] No secret, token or seed is in the diff.
