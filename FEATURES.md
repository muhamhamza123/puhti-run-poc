# Puhti Runner — Full Feature List

## JupyterLab Extension (Frontend)

### Submit Tab
- Select any open notebook from the current JupyterHub session
- Choose Apptainer container (fetched live from Puhti scratch)
- Choose Slurm partition: small, large, gpu, gpumedium, longrun
- Set CPUs (1–40) and RAM (1–382 GB) via sliders
- Paste extra pip packages (requirements.txt) inline
- Optional notification email — sent when job finishes or fails
- One-click submit — notebook auto-converted to script.py via nbconvert

### Jobs Tab
- Full job history list (newest first, last 50 jobs)
- Live status polling on load and every 10s — queued/running/done/failed/cancelled
- Per-job buttons:
  - **↓ Get** (done jobs) — saves results to `~/puhti-results/{slurm_id}/` on PVC
  - **📋 Log** — shows stdout and stderr inline (live-tails while running, reads local copy after done)
  - **↺ Resubmit** (failed/cancelled) — resubmits with original container/partition/resources
  - **✕ Cancel** (queued/running) — cancels the Slurm job via scancel

### Containers Tab
- List of available `.sif` containers on Puhti scratch
- **Simple request form** — type container name + package list, API generates `.def` and opens a GitHub PR automatically
- **Upload .def file** — advanced users can upload their own Apptainer definition file
- **My Container Requests** — shows status of all PRs the user has opened (pending/merged/closed), auto-refreshes every 10s

---

## Jupyter Server Extension (Backend, same Docker image)

- `GET /puhti-runner/auth-token` — returns `JUPYTERHUB_API_TOKEN` from server env so frontend can send it on API calls
- `POST /puhti-runner/save-results` — fetches results ZIP from Puhti API and extracts to `~/puhti-results/{slurm_id}/` on the user's PVC (avoids browser download)

---

## Puhti Run API (FastAPI on head node, port 8002)

### Job Endpoints
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/run-notebook` | Convert notebook → script.py, submit to Slurm |
| POST | `/run-code` | Submit raw script.py to Slurm |
| GET | `/run-status/{job_id}` | Poll Slurm state, update DB, trigger rsync on completion |
| GET | `/run-results/{job_id}` | Return output ZIP from NFS |
| GET | `/run-logs/{job_id}` | rsync stdout.txt + stderr.txt from Puhti, return both |
| POST | `/resubmit/{job_id}` | Copy job files to new job_id, resubmit with original params |
| POST | `/cancel-job/{job_id}` | Run scancel on the Slurm job |
| GET | `/my-jobs/{username}` | Return job history for a user (last 50) |

### Container Endpoints
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/containers` | List available .sif files on Puhti scratch |
| POST | `/request-container` | Upload .def file → open GitHub PR |
| POST | `/request-container-simple` | Package list → generate .def → open GitHub PR |
| GET | `/my-container-requests/{username}` | List user's container PRs with live GitHub status |

### Other
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |

---

## Security

- **JupyterHub token auth** — frontend fetches `JUPYTERHUB_API_TOKEN` from server env via `/puhti-runner/auth-token`, sends it as `X-JupyterHub-Token` header; API validates against JupyterHub `/hub/api/user` to get real username — cannot be spoofed by client
- **Rate limiting** — max 3 concurrent jobs per user (configurable via `MAX_CONCURRENT_JOBS` env var)
- **Per-user directories** — jobs isolated under `{PUHTI_RUNS}/{username}/{job_id}/` on Puhti scratch
- **XSRF protection** — server extension POSTs include `X-XSRFToken` header read from cookie
- **HTTPS only** — nginx reverse proxy terminates TLS, API only accessible via `https://hbv.we3data.com/puhti/`

---

## Notifications

- Email sent via Gmail SMTP (STARTTLS) when job transitions to `done` or `failed`
- Email includes job ID, Slurm ID, and next-step instructions
- User provides their email in the Submit tab — different per job, sender account is fixed

---

## Container Build CI

- GitHub PR opened automatically when user requests a container
- PR includes `.def` file in `apptainer/` directory
- On PR merge to `main`, GitHub Actions builds the `.sif` on Puhti via SSH+sbatch
- Build polls until Slurm job completes, fails CI if build fails
- Only changed `.def` files are rebuilt (not all containers on every merge)

---

## Infrastructure

- **Rahti (CSC OpenShift)** — runs JupyterHub + single-user servers as Docker containers (`ghcr.io/muhamhamza123/puhti-extension:v20`)
- **Head node** (`hbv.we3data.com`) — runs FastAPI via systemd (`puhti-run.service`), auto-restarts on failure, starts on boot
- **NFS** — `/data/hbv/runs/{job_id}/` shared between API process and job dirs
- **SQLite** — `/data/hbv/runs/runs.db` stores job history and container requests
- **Nginx** — `/puhti/` proxies to port 8002, passes CORS headers through
- **Puhti scratch** — `/scratch/project_2014823/runs/{username}/{job_id}/`

---

## Known Gaps / Future Work

- No admin panel (job history across all users, quota management, container request approval UI)
- No cleanup of old job dirs on Puhti scratch or head node NFS
- SQLite sufficient for now but should move to PostgreSQL at ~50+ concurrent users
- Health monitoring/alerting not yet implemented
- No way to pass input data files (only notebook + requirements currently)
