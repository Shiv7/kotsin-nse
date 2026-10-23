# Running the research in Claude Code's cloud

The **research** (`optsig`, `regimes`, `daytype`, `power`, `condor`, `dbcpr-backtest`, `confirm-backtest`,
`swing-backtest`…) runs in a cloud session on a copy of the data. The **live engine, the 5paisa
history download and anything with the broker login stay on the laptop** — never put `backend/.env`,
the 5paisa credentials or the TOTP secret in a cloud environment, its variables or its setup script.

Facts from the Claude Code docs (2026-10-23): https://code.claude.com/docs/en/claude-code-on-the-web.md
— a cloud VM is about 4 vCPU / 16 GB RAM / 30 GB disk; it clones the branch **from GitHub** (push
first); `uv` is pre-installed; foreground commands stop after 10 min and background ones after about
30 more unless `BASH_DEFAULT_TIMEOUT_MS` is raised; an idle VM pauses, and if it is reclaimed running
commands are not restored.

## Once

1. **Pack the data** (git-ignored, ~1.7 GB): `scripts/pack_research_data.sh` → `~/Downloads/kotsin-research-data.tar`
   and its sha256.
2. **Upload it** somewhere the VM can download with one URL. Google Drive: upload the file, then
   Share → General access → *Anyone with the link* (it is public market data), copy the file id from the
   link (`/d/<FILE_ID>/`). The direct link is
   `https://drive.usercontent.google.com/download?id=<FILE_ID>&export=download&confirm=t`.
3. **Create a cloud environment** at claude.ai/code (selector under the message box):
   - Network: **Custom** with the defaults included, plus `drive.usercontent.google.com` and
     `drive.google.com` (Drive is not in the Trusted defaults as far as the docs list them; S3 / R2 / GCS are).
     Add `nsearchives.nseindia.com` too if `fetch-oi` should run there (untested from cloud IPs).
   - Variables: `KOTSIN_DATA_URL=<the direct link>`, `KOTSIN_DATA_SHA256=<from step 1>`,
     `BASH_DEFAULT_TIMEOUT_MS=3600000`.
   - Setup script: `cd backend && uv sync`.
4. `.claude/settings.json` (in the repo) already does the rest: `KN_DATA_DIR=./data-live`, and a
   SessionStart hook that — in cloud sessions only — downloads and unpacks the archive in the background
   (`scripts/cloud_data.sh`; log `/tmp/kotsin_data.log`; done when `backend/data-live/.ready` exists).

## Each time

```
git push                                   # the cloud clones GitHub, not this folder
claude --cloud "Wait for backend/data-live/.ready, then cd backend && uv run kotsin-nse power; put the table in docs/HANDOFF.md and push"
```
Several `claude --cloud "…"` at once run in parallel (shared usage limits). Results come back through
git (ask the session to commit them) or `claude --teleport` (local git state must be clean). Run files
under `data-live/` are git-ignored and stay in the VM unless copied into `docs/`.

When the data changes (new recordings, a new history fetch): run the pack script again, upload, update
`KOTSIN_DATA_SHA256` (and the URL if the file id changed).

The research protocol does not change in the cloud: the holdout 2025-10-01 → 2026-10-16 stays
untouched; write the pass rule down before a run (`docs/research/`).
