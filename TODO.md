# Puhti Run POC — Production TODO

## Security
- [x] JupyterHub token auth: validate token against Rahti JupyterHub API, extract username server-side (v16, done)
- [ ] Rotate test GitHub token `ghp_qZJf...` — update in head node `override.conf` AND GitHub repo secret `GITHUB_TOKEN`

## Reliability
- [x] `ExecStartPre` `-` prefix so service starts when Puhti is down
- [x] `ConnectTimeout=10` on scp in service file
- [x] Service managed by systemd with `Restart=on-failure` — no manual uvicorn needed
- [ ] Health monitoring/alerting — e.g. cron that curls `/health` and emails on failure
- [ ] `sudo systemctl enable puhti-run` — enable service to start on boot (not done yet)

## Scale
- [ ] SQLite WAL mode + connection timeout — prevents lock errors under concurrent writes with 4 uvicorn workers
- [ ] SQLite → PostgreSQL when user count grows beyond ~50 concurrent (fine for now)
- [ ] Cleanup old job dirs on Puhti scratch (`/scratch/project_2014823/runs/{user}/{job}/`) — cron after X days
- [ ] Cleanup old job dirs on head node NFS (`/data/hbv/runs/`) — cron after X days
- [ ] CSC billing quota per user — no cap currently, one user can drain project_2014823 quota

## User Experience
- [x] Email/notification when job finishes — optional email field in Submit tab, sent via smtp.csc.fi on done/failed (v18)
- [x] Resubmit failed job — ↺ Resubmit button per job row (v17)
- [x] Show job output log in UI — 📋 Log button per job, shows stdout/stderr (empty while queued, live once running) (v17)

## Admin
- [ ] Admin panel — job history across all users, per-user quota display, approve/reject container requests from UI
- [x] GitHub labels — removed, unnecessary complexity for the PR review workflow
- [x] CI: confirm build trigger is merge-only (already verified — triggers on push to main, correct)

## Container Request Workflow
- [x] Simple form — users describe packages, API generates `.def` and opens PR
- [x] Auto-label PRs by package type (hydrology/ml/geospatial/general)
- [x] Container request status visible in UI under "My Container Requests"
- [ ] Admin approve/reject container requests from UI (currently only via GitHub PR merge)
