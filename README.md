# Puhti Run POC

Submit any Python script from a JupyterHub notebook to Puhti supercomputer and get results back.

## How it works

See `docs/architecture.svg` for the full flow diagram.

1. User writes a Python script in JupyterHub
2. Clicks **Run on Puhti** in the notebook widget
3. Script + requirements.txt are uploaded to the head node API (port 8002)
4. API rsyncs files to Puhti scratch and SSH submits a Slurm job
5. Puhti installs dependencies and runs the script inside Apptainer container
6. Results are rsynced back and downloaded as a ZIP into `./puhti_output/`

---

## One-time setup on the head node

```bash
# Clone the repo
sudo git clone https://github.com/muhamhamza123/puhti-run-poc /opt/hbv/puhti-run
sudo chown -R hbv:hbv /opt/hbv/puhti-run

# Create NFS runs directory
sudo mkdir -p /data/hbv/runs
sudo chown hbv:hbv /data/hbv/runs

# Install Python deps into the existing venv (reuse HBV venv)
sudo /opt/hbv/venv/bin/pip install fastapi uvicorn python-multipart

# Install and start the service
sudo cp /opt/hbv/puhti-run/deploy/puhti-run.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable puhti-run
sudo systemctl start puhti-run

# Check it's running
sudo systemctl status puhti-run
curl http://localhost:8002/health
```

## One-time setup on Puhti scratch

```bash
# SSH into Puhti as javedham
ssh javedham@puhti.csc.fi

mkdir -p /scratch/project_2014823/runs
```

Copy the generic Slurm script to Puhti:

```bash
# From the head node
scp -i /home/hbv/.ssh/id_puhti \
    /opt/hbv/puhti-run/generic_run.sh \
    javedham@puhti.csc.fi:/scratch/project_2014823/generic_run.sh

chmod +x /scratch/project_2014823/generic_run.sh
```

---

## End-to-end test

### Step 1 — open a notebook in JupyterHub

Open any notebook on the Puhti JupyterHub. Make sure `run_button_widget.py` is in the same directory.

### Step 2 — paste this into a cell and run it

```python
from run_button_widget import PuhtiRunWidget

PuhtiRunWidget(
    script_path='demo_user_script.py',
    requirements_path='requirements.txt',
).show()
```

You should see a widget with partition / CPU / RAM dropdowns and a green **Run on Puhti** button.

### Step 3 — click Run on Puhti

Watch the status label change:
- `Submitting...` — uploading files to head node
- `Submitted — Slurm job 12345 — polling...` — job is in the queue
- `running` — job is executing on a compute node
- `done` — results downloaded to `./puhti_output/`

### Step 4 — check results

```python
import os
print(os.listdir('puhti_output'))
# ['plot.png', 'results.csv']

import pandas as pd
pd.read_csv('puhti_output/results.csv').head()
```

### Step 5 — check logs if something went wrong

```python
import requests
r = requests.get('http://hbv.we3data.com:8002/run-logs/<job_id>')
print(r.json()['stdout'])
print(r.json()['stderr'])
```

Or on the head node:
```bash
sudo journalctl -u puhti-run -f
```

---

## Writing your own script

Rules for scripts that run on Puhti:

1. **Save all outputs to `./output/`** — only this folder is rsynced back
2. **List all pip dependencies in `requirements.txt`** — installed at runtime
3. **No hardcoded paths** — script runs from its own scratch directory
4. **Standard Python only** — no sudo, no system installs

Example:

```python
# my_analysis.py
import os
import numpy as np

os.makedirs('output', exist_ok=True)

data = np.random.randn(1000)
np.save('output/data.npy', data)
print(f'saved {len(data)} values')
```

```
# requirements.txt
numpy
```

---

## API reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/run-code` | Submit script + requirements |
| GET  | `/run-status/{job_id}` | Poll job status |
| GET  | `/run-results/{job_id}` | Download output ZIP |
| GET  | `/run-logs/{job_id}` | Get stdout + stderr |
| GET  | `/health` | Health check |

API runs on port **8002** (separate from HBV API on 8001).
