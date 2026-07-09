# Puhti Runner — Production TODO

## Must Do Before 100 Users

- [x] **SQLite WAL mode** — done, WAL + 5s busy_timeout on every DB connection
- [x] **Scratch cleanup cron** — `cleanup.sh` runs daily at 02:00 via hbv crontab, deletes Puhti dirs >7 days
- [x] **NFS cleanup cron** — same script deletes `/data/hbv/runs/{job_id}/` dirs >30 days
- [x] **`sudo systemctl enable puhti-run`** — done, service starts on reboot

## Should Do Soon

- [ ] **Admin panel** — web UI at `/puhti/admin` showing all jobs across users, per-user activity, container request management
- [ ] **Health monitoring** — cron that curls `/health` every 5 min and emails if down
- [ ] **CSC billing quota per user** — cap Slurm hours per user per month to prevent one user draining project_2014823

## Nice to Have (not blocking)

- [ ] **SQLite → PostgreSQL** — only needed at ~50+ concurrent users, fine for now
- [ ] **Input data files** — currently only notebook + requirements.txt can be uploaded; no way to pass additional input files to jobs

## Done

### Security
- [x] JupyterHub token auth — validated server-side against JupyterHub API, username cannot be spoofed (v16)
- [x] GitHub test token rotated

### Reliability
- [x] `ExecStartPre` `-` prefix + `ConnectTimeout=10` — service starts even when Puhti is down
- [x] systemd manages uvicorn — auto-restarts on failure, no manual starts needed

### User Experience
- [x] Job history with live status polling every 10s
- [x] 📋 Log button — shows stdout/stderr per job (live while running)
- [x] ↺ Resubmit button — resubmits failed/cancelled jobs with original settings
- [x] ✕ Cancel button — cancels queued/running jobs
- [x] ↓ Get button — saves results to `~/puhti-results/` on PVC
- [x] Email notification on job done/failed via Gmail SMTP (v18/v19)

### Container Workflow
- [x] Simple request form — package list → auto-generate `.def` → open GitHub PR
- [x] Container request status visible in UI (pending/merged/closed)
- [x] CI builds `.sif` on Puhti automatically on PR merge

### Infrastructure
- [x] Nginx `/puhti/` reverse proxy with CORS headers
- [x] Rate limiting — max 3 concurrent jobs per user
- [x] Per-user job directories on Puhti scratch
- [x] `params.json` stored per job for resubmit
