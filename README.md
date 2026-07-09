# Puhti Run API

FastAPI backend that accepts notebook/script submissions from JupyterHub users and runs them on the Puhti supercomputer via Slurm.

See [`FEATURES.md`](FEATURES.md) for the full feature list and [`docs/architecture.svg`](docs/architecture.svg) for the system diagram.

---

## Components

| Component | Location | Description |
|-----------|----------|-------------|
| FastAPI app | `main.py` + `api_run_endpoint.py` | All endpoints |
| Slurm script | `generic_run.sh` | Runs on Puhti compute nodes |
| Apptainer defs | `apptainer/*.def` | Container definitions |
| Container CI | `.github/workflows/build-container.yml` | Builds .sif on PR merge |
| Service file | `deploy/puhti-run.service` | systemd unit |

---

## Head Node Setup

```bash
sudo git clone https://github.com/muhamhamza123/puhti-run-poc /opt/hbv/puhti-run
sudo chown -R hbv:hbv /opt/hbv/puhti-run
sudo mkdir -p /data/hbv/runs && sudo chown hbv:hbv /data/hbv/runs
sudo /opt/hbv/venv/bin/pip install fastapi uvicorn python-multipart
sudo cp deploy/puhti-run.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable puhti-run
sudo systemctl start puhti-run
curl http://localhost:8002/health
```

### Service override (secrets)

`/etc/systemd/system/puhti-run.service.d/override.conf`:

```ini
[Service]
Environment="GITHUB_TOKEN=..."
Environment="GITHUB_REPO=muhamhamza123/puhti-run-poc"
Environment="JUPYTERHUB_URL=https://diwa-data-lab-vre.rahtiapp.fi"
Environment="SMTP_USER=hamzasahi72000@gmail.com"
Environment="SMTP_PASSWORD=..."
Environment="EMAIL_FROM=hamzasahi72000@gmail.com"
Environment="MAX_CONCURRENT_JOBS=3"
```

---

## Nginx Config

`/etc/nginx/conf.d/hbv.conf` — add the `/puhti/` location block:

```nginx
upstream puhti_api {
    server 127.0.0.1:8002;
    keepalive 32;
}
location /puhti/ {
    proxy_pass         http://puhti_api/;
    proxy_http_version 1.1;
    proxy_set_header   Host              $host;
    proxy_set_header   X-Real-IP         $remote_addr;
    proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_set_header   Connection        "";
    proxy_buffering    off;
    proxy_pass_header  Access-Control-Allow-Origin;
    proxy_pass_header  Access-Control-Allow-Methods;
    proxy_pass_header  Access-Control-Allow-Headers;
}
```

---

## Puhti Setup (one-time)

```bash
ssh javedham@puhti.csc.fi
mkdir -p /scratch/project_2014823/runs
mkdir -p /scratch/project_2014823/tmp
mkdir -p /scratch/project_2014823/pip-cache
```

The `generic_run.sh` script is copied to Puhti automatically on service start via `ExecStartPre` in the service file.

---

## Container Build Flow

1. User requests a container from JupyterLab (simple form or .def upload)
2. API opens a GitHub PR with the `.def` file in `apptainer/`
3. Admin reviews and merges the PR
4. GitHub Actions SSHs into Puhti and runs `sbatch apptainer build`
5. `.sif` file becomes available at `/scratch/project_2014823/runs/{name}.sif`
6. Container appears in the JupyterLab extension dropdown

---

## API Reference

| Method | Endpoint | Auth |
|--------|----------|------|
| GET | `/health` | none |
| GET | `/containers` | none |
| POST | `/run-notebook` | X-JupyterHub-Token |
| POST | `/run-code` | X-JupyterHub-Token |
| GET | `/run-status/{job_id}` | none |
| GET | `/run-logs/{job_id}` | none |
| GET | `/run-results/{job_id}` | none |
| POST | `/resubmit/{job_id}` | X-JupyterHub-Token |
| POST | `/cancel-job/{job_id}` | none |
| GET | `/my-jobs/{username}` | none |
| POST | `/request-container` | none |
| POST | `/request-container-simple` | none |
| GET | `/my-container-requests/{username}` | none |

---

## Database

SQLite at `/data/hbv/runs/runs.db`

**runs table:** `job_id, slurm_id, status, partition, username, email, created`

**container_requests table:** `id, username, container, pr_url, pr_number, status, created`
