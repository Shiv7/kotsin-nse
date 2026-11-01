# Deploying the engine on AWS (Mumbai)

Why a server: the laptop lost 30 % of week 1 to sleep, battery and a loose charger, and since 2026-04-01 a
live API order must come from a **static IP registered with the broker** (SEBI's retail-algo framework) —
home broadband does not qualify. The engine is one process (~90 MB RSS, ~65 MB of archives a session), so a
small box is enough.

## 1. The instance (owner, in the AWS console)

| item | value | why |
|---|---|---|
| region | **ap-south-1 (Mumbai)** | nearest to NSE / 5paisa |
| image | Ubuntu Server 24.04 LTS (arm64 or x86) | `bootstrap.sh` targets it |
| type | t4g.small or t3a.small (2 vCPU, 2 GB) — Lightsail 2 GB is the same thing cheaper | ~₹1,000–1,500 / month |
| disk | 40 GB gp3 | ~2 GB of archives a month |
| **Elastic IP** | allocate one and associate it | the static IP; it survives stop/start |
| security group | inbound **22 only, from your IP** (`/32`); nothing else | the UI is reached through an SSH tunnel |
| key pair | yours; `ssh -i key.pem ubuntu@<elastic-ip>` | SSH keys only, no passwords |

## 2. Bootstrap (once, on the server)

```
scp deploy/aws/bootstrap.sh ubuntu@<elastic-ip>:
ssh ubuntu@<elastic-ip> bash bootstrap.sh
```
IST clock, 2 GB swap, uv, the app directory, the systemd unit (enabled, not started). It prints the
public IP: that is what goes on 5paisa's Xstream dashboard.

## 3. Deploy (from the laptop)

```
deploy/aws/deploy.sh ubuntu@<elastic-ip> --with-env     # first time: code + UI build + backend/.env
deploy/aws/deploy.sh ubuntu@<elastic-ip>                # later: code + UI only
```
The UI is built on the laptop (no node on the box); `backend/.env` travels over ssh only and is `chmod 600`
on arrival; the venv is synced from `uv.lock`; the unit restarts and the health line is printed. Add to the
server's `.env`:

```
KN_API_HOST=127.0.0.1          # the UI only through the tunnel
KN_LIVE_STATIC_IP=<elastic-ip> # the engine refuses a live mode from any other address
```

## 4. Register the IP with 5paisa

Xstream API dashboard (xstream.5paisa.com/dashboard) → the API key → Static IP: the Elastic IP. The
rule applies to the order APIs only; the feed works from anywhere. A change is allowed once a week.
Non-algo app (≤ 10 orders a second): no AlgoID needed.

## 5. Operate

| what | how |
|---|---|
| status / logs | `ssh … sudo systemctl status kotsin-nse`; `tail -f kotsin-nse/backend/data-live/logs/engine.log` |
| the UI | `ssh -L 8500:127.0.0.1:8500 ubuntu@<elastic-ip>` then http://127.0.0.1:8500 |
| health | `curl -s http://127.0.0.1:8500/api/health` on the box, or through the tunnel |
| the live route's readiness | `/api/control/live` — every blocker named |
| pull the day's data to the laptop | `deploy/aws/sync-archives.sh ubuntu@<elastic-ip>` → `backend/data-live-aws/` |
| restart (outside market hours) | `sudo systemctl restart kotsin-nse` (a fresh 30 s TOTP window is waited for by the engine) |

The midnight re-login, the holidays file, the 15:50 refresh and the archives work exactly as on the laptop.
Nothing here arms a live mode: that is `POST /api/control/mode` with `armed_minutes`, refused until every
blocker in `/api/control/live` is gone, and asked for explicitly every time.
