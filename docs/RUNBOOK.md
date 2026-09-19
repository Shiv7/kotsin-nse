# Runbook

## Start

```bash
cd backend && uv run kotsin-nse       # http://127.0.0.1:8500
```

The boot banner prints the mode, the universe size, every cap (and `OFF` where a cap is off) and
any boot notes. **Read the notes.** They are where "no credentials", "no holiday list" and
"arming expired" appear.

## Modes

| Mode | What it does |
|---|---|
| `SHADOW` | every gate runs, every decision is recorded, nothing is placed |
| `PAPER` | filled against the live 20-level book, with the real cost model |
| `LIVE_CAPPED` | real orders under hard caps — requires an explicit arming window |
| `LIVE` | real orders, caps off — requires an arming window |

```bash
curl -X POST localhost:8500/api/control/mode -H 'Content-Type: application/json' \
     -d '{"mode":"PAPER"}'

curl -X POST localhost:8500/api/control/mode -H 'Content-Type: application/json' \
     -d '{"mode":"LIVE_CAPPED","armed_minutes":60}'
```

A live mode **without** `armed_minutes` is refused. A restart after the window expires boots into
PAPER and says so in the boot notes. This is the direct guard against the failure where a book ran
in paper for eight weeks because a restart dropped an environment variable — and against its
mirror image.

## Stopping things

| Action | Effect |
|---|---|
| `POST /api/control/halt {"halted":true}` | no new entries. Exits still work |
| `POST /api/control/kill` | halt **and** square off through the broker's bulk endpoint |
| `SIGTERM` | graceful: persists wallets, closes the ledger, removes the pid file |

Kill deliberately uses the broker's own square-off rather than our position list: the moment that
button is pressed is exactly when our view of the book is least trustworthy.

## When entries stop but nothing looks wrong

Check, in this order:

1. **Mode** — the banner, on every page.
2. **Halt** — `/api/health` → `halted`, and the reason.
3. **Reconciliation freeze** — `/api/health` → the `reconciled` check. A mismatch or a *failed*
   reconcile freezes entries by design. `POST /api/control/reconcile` to retry,
   `POST /api/control/acknowledge` to accept the state and resume.
4. **Gateway breaker** — three consecutive rejects trips it. `POST /api/control/reset-breaker`.
5. **Wallet halt** — the daily-loss or drawdown breaker, per book, on the Overview page. It lifts
   on the next IST day.
6. **Which gate is binding** — the Strategies page. A conjunction of gates can strangle a book
   silently; this is the counter that says which one.

## When there are no signals at all

* `/api/universe` — are bars warm? A symbol needs 21 bars of 30m to be evaluated and 25 daily bars
  to have pivot zones.
* `/api/health` → `feed` — connected? How long since the last message? How many subscriptions?
* Is it a trading day? With an empty `data/holidays.txt` every weekday counts as one, and the boot
  notes say so.

## Restarting

```bash
kill $(cat backend/data/engine.pid)     # by PID. `pkill -f kotsin-nse` kills the shell running it
```

Wait for `shutdown.done` in the log, then start again. Open positions are re-hydrated from the
ledger and reconciled against the broker before entries resume.

## Before the first real order

1. Rotate the 5paisa credentials. The previous stack committed all six plus the TOTP seed to git.
2. Set `KN_FP_PUBLIC_IP` to the box's real outbound address, or confirm the auto-detected value in
   the log. A wrong IP makes the broker's RMS reject every order.
3. Run a full session in PAPER and check the Trades page: are charges plausible? Is the paper
   slippage plausible against the book?
4. Read `docs/strategies/*.md` §Artefact. **Both books currently say "none".** No parameter in this
   repo has been validated by a backtest run against this implementation.
5. Arm `LIVE_CAPPED` for a short window with `KN_LIVE_MAX_QTY_RUPEES` small enough that the whole
   day's worth of caps is money you would not mind losing, and watch the first order end to end.
