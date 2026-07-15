"""
Complete /run-code API router — submit, status, logs, results.
"""
import io, os, uuid, time, shutil, subprocess, sqlite3, zipfile, contextlib, base64, re, logging
import urllib.request, urllib.error, json, smtplib
from logging.handlers import RotatingFileHandler
from email.mime.text import MIMEText
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Header, Request
import collections, threading
from fastapi.responses import StreamingResponse
from typing import Optional

router = APIRouter()

NFS_RUNS    = os.environ.get('RUNS_ROOT',    '/data/hbv/runs')
PUHTI_RUNS  = os.environ.get('PUHTI_RUNS',   '/scratch/project_2014823/runs')
SLURM_SH    = os.environ.get('SLURM_SH',     '/scratch/project_2014823/generic_run.sh')
SSH_KEY     = os.environ.get('PUHTI_SSH_KEY', '/home/hbv/.ssh/id_puhti')
PUHTI_USER  = os.environ.get('PUHTI_USER',    'javedham')
PUHTI_HOST  = os.environ.get('PUHTI_HOST',    'puhti.csc.fi')
DB_PATH     = os.environ.get('RUN_DB_PATH',   '/data/hbv/runs/runs.db')

VALID_PARTITIONS  = {'small', 'large', 'longrun', 'gpu', 'gpumedium'}
GPU_PARTITIONS    = {'gpu', 'gpumedium'}
DEFAULT_CONTAINER = 'general'
SSH_CONTROL_PATH  = os.environ.get('SSH_CONTROL_PATH', '/tmp/ssh-puhti-%h-%p-%r')
PUHTI_ENVS        = os.environ.get('PUHTI_ENVS', '/scratch/project_2014823/envs')

LOG_FILE = os.environ.get('API_LOG_FILE', '/var/log/puhti-run/api.log')
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
_log = logging.getLogger('puhti-run')
try:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    _h = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5)
    _h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    if not any(isinstance(h, RotatingFileHandler) for h in _log.handlers):
        _log.addHandler(_h)
    _log.info('puhti-run api_run_endpoint loaded, logging to %s', LOG_FILE)
except OSError as e:
    _log.warning('Could not open log file %s: %s', LOG_FILE, e)

GITHUB_TOKEN   = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO    = os.environ.get('GITHUB_REPO', 'muhamhamza123/puhti-run-poc')
JUPYTERHUB_URL = os.environ.get('JUPYTERHUB_URL', 'https://diwa-data-lab-vre.rahtiapp.fi')
SMTP_HOST      = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT      = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER      = os.environ.get('SMTP_USER', '')
SMTP_PASSWORD  = os.environ.get('SMTP_PASSWORD', '')
EMAIL_FROM     = os.environ.get('EMAIL_FROM', SMTP_USER)


def _validate_jupyterhub_token(token: str) -> str:
    """Validate a JupyterHub API token and return the username, or raise 401."""
    if not token:
        raise HTTPException(401, 'Missing JupyterHub token')
    try:
        url = f'{JUPYTERHUB_URL}/hub/api/user'
        req = urllib.request.Request(url, headers={'Authorization': f'token {token}'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return data['name']
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(401, f'Invalid or expired JupyterHub token: {e}')


# ── Database ──────────────────────────────────────────────────────────────────

def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            job_id    TEXT PRIMARY KEY,
            slurm_id  TEXT,
            status    TEXT DEFAULT 'queued',
            partition TEXT,
            username  TEXT DEFAULT '',
            created   TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS container_requests (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            username     TEXT,
            container    TEXT,
            pr_url       TEXT,
            pr_number    INTEGER,
            status       TEXT DEFAULT 'pending',
            created      TEXT DEFAULT (datetime('now'))
        )
    """)
    cr_cols = [r[1] for r in conn.execute("PRAGMA table_info(container_requests)").fetchall()]
    if 'email' not in cr_cols:
        conn.execute("ALTER TABLE container_requests ADD COLUMN email TEXT DEFAULT ''")
    if 'packages' not in cr_cols:
        conn.execute("ALTER TABLE container_requests ADD COLUMN packages TEXT DEFAULT ''")
    if 'build_job_id' not in cr_cols:
        conn.execute("ALTER TABLE container_requests ADD COLUMN build_job_id TEXT DEFAULT ''")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
    if 'username' not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN username TEXT DEFAULT ''")
    if 'email' not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN email TEXT DEFAULT ''")
    # Fix 6: backfill NULL/empty usernames so reports don't show (anon)
    conn.execute("UPDATE runs SET username='unknown' WHERE username IS NULL OR username=''")
    conn.commit()
    return conn


def _insert(job_id, slurm_id, partition, username='', email='', cpus=0, memory_gb=0):
    with contextlib.closing(_db()) as db:
        db.execute(
            "INSERT INTO runs (job_id, slurm_id, partition, username, email, cpus, memory_gb) VALUES (?,?,?,?,?,?,?)",
            (job_id, slurm_id, partition, username, email, cpus, memory_gb)
        )
        db.commit()


def _send_email(to: str, subject: str, body: str) -> None:
    if not to or not SMTP_USER or not SMTP_PASSWORD:
        return
    try:
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = EMAIL_FROM
        msg['To'] = to
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
            s.ehlo()
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(EMAIL_FROM, [to], msg.as_string())
    except Exception as e:
        import logging
        logging.getLogger('puhti-run').error(f'Email to {to} failed: {e}')


def _get(job_id):
    with contextlib.closing(_db()) as db:
        row = db.execute("SELECT * FROM runs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def _set_status(job_id, status):
    with contextlib.closing(_db()) as db:
        db.execute("UPDATE runs SET status=? WHERE job_id=?", (status, job_id))
        db.commit()


def _active_count(username: str) -> int:
    with contextlib.closing(_db()) as db:
        row = db.execute(
            "SELECT COUNT(*) FROM runs WHERE username=? AND status IN ('queued','running')",
            (username,)
        ).fetchone()
        return row[0] if row else 0


# ── SSH / rsync helpers ───────────────────────────────────────────────────────

_SSH_OPTS = [
    '-o', 'StrictHostKeyChecking=no',
    '-o', 'BatchMode=yes',
    '-o', 'ConnectTimeout=15',
    '-o', 'ControlMaster=auto',
    '-o', f'ControlPath={SSH_CONTROL_PATH}',
    '-o', 'ControlPersist=300',
]

def _ssh(cmd: str, timeout: int = 30):
    return subprocess.run(
        ['ssh', '-i', SSH_KEY] + _SSH_OPTS + [f'{PUHTI_USER}@{PUHTI_HOST}', cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def _ssh_write(remote_path: str, content: str, timeout: int = 30):
    """Write a string to a file on Puhti via SSH stdin → cat."""
    return subprocess.run(
        ['ssh', '-i', SSH_KEY] + _SSH_OPTS + [f'{PUHTI_USER}@{PUHTI_HOST}', f'cat > {remote_path}'],
        input=content, capture_output=True, text=True, timeout=timeout,
    )


def _rsync_to(src: str, dst: str, timeout: int = 300):
    subprocess.run([
        'rsync', '-az',
        '-e', f'ssh -i {SSH_KEY} -o StrictHostKeyChecking=no -o BatchMode=yes -o ControlMaster=auto -o ControlPath={SSH_CONTROL_PATH} -o ControlPersist=300',
        src, f'{PUHTI_USER}@{PUHTI_HOST}:{dst}',
    ], check=True, timeout=timeout)


def _rsync_from(src: str, dst: str, timeout: int = 300):
    os.makedirs(dst, exist_ok=True)
    subprocess.run([
        'rsync', '-az',
        '-e', f'ssh -i {SSH_KEY} -o StrictHostKeyChecking=no -o BatchMode=yes -o ControlMaster=auto -o ControlPath={SSH_CONTROL_PATH} -o ControlPersist=300',
        f'{PUHTI_USER}@{PUHTI_HOST}:{src}', dst,
    ], check=True, timeout=timeout)


# ── Venv build helpers ────────────────────────────────────────────────────────

def _build_venv_script(name: str, packages: list[str]) -> str:
    torch_pkgs  = [p for p in packages if p.lower() in ('torch', 'torchvision', 'torchaudio')]
    other_pkgs  = [p for p in packages if p.lower() not in ('torch', 'torchvision', 'torchaudio')]
    torch_line  = (
        f'    "$VENV/bin/pip" install --no-cache-dir \\\n'
        f'        --index-url https://download.pytorch.org/whl/cu121 \\\n'
        f'        {" ".join(torch_pkgs)}\n'
    ) if torch_pkgs else ''
    other_line  = (
        f'    "$VENV/bin/pip" install --no-cache-dir {" ".join(other_pkgs)}\n'
    ) if other_pkgs else ''
    return f"""#!/bin/bash
#SBATCH --job-name=build-venv-{name}
#SBATCH --account=project_2014823
#SBATCH --partition=small
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=00:30:00
#SBATCH --output={PUHTI_ENVS}/build-{name}-%j.out

export MODULEPATH=/appl/modulefiles:$MODULEPATH

# Find a Python >= 3.8 — try modules first, then known fixed paths
PYTHON3=""
for attempt in \
    "module load python-data && python3" \
    "module load python/3.11 && python3" \
    "module load python/3.10 && python3" \
    "/appl/soft/ai/tykky/python-data-2024-01/bin/python3" \
    "/appl/soft/ai/tykky/python-data-2023-08/bin/python3" \
    "/usr/bin/python3.11" \
    "/usr/bin/python3.10" \
    "/usr/bin/python3.9" \
    "/usr/bin/python3.8"; do
    if eval "$attempt --version" &>/dev/null 2>&1; then
        VER=$(eval "$attempt --version 2>&1" | awk '{{print $2}}')
        MAJOR=$(echo $VER | cut -d. -f1)
        MINOR=$(echo $VER | cut -d. -f2)
        if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 8 ]; then
            PYTHON3="$(eval "which $attempt" 2>/dev/null || echo "$attempt")"
            echo "[build] using Python $VER from: $attempt"
            break
        fi
    fi
done

# Simpler fallback: eval each attempt and grab the working one
if [ -z "$PYTHON3" ]; then
    for mod in python-data "python/3.11" "python/3.10"; do
        if module load $mod 2>/dev/null && python3 -c "import sys; exit(0 if sys.version_info>=(3,8) else 1)" 2>/dev/null; then
            PYTHON3=$(which python3)
            echo "[build] loaded module $mod → $PYTHON3 $(python3 --version)"
            break
        fi
    done
fi

if [ -z "$PYTHON3" ]; then
    echo "[build] ERROR: no Python >= 3.8 found"
    exit 1
fi

echo "[build] Python: $($PYTHON3 --version)"
mkdir -p {PUHTI_ENVS}
export TMPDIR=/scratch/project_2014823/tmp
export PIP_CACHE_DIR=/scratch/project_2014823/pip-cache
mkdir -p "$TMPDIR" "$PIP_CACHE_DIR"

VENV={PUHTI_ENVS}/{name}
rm -rf "$VENV"
"$PYTHON3" -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip

{torch_line}{other_line}
echo "VENV_BUILD_SUCCESS"
"""


def _commit_env_record(name: str, packages: list[str], username: str, slurm_job_id: str) -> None:
    """Commit a record file to GitHub so env requests are tracked in the repo."""
    if not GITHUB_TOKEN:
        return
    from datetime import datetime, timezone
    content = (
        f"name: {name}\n"
        f"requested_by: {username}\n"
        f"approved_at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"slurm_build_job: {slurm_job_id}\n"
        f"packages:\n" + ''.join(f"  - {p}\n" for p in packages)
    )
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
    }
    path = f'envs/{name}.txt'
    url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{path}'
    try:
        # Check if file already exists (need its sha to update)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                existing = json.loads(resp.read())
            sha = existing.get('sha')
        except urllib.error.HTTPError:
            sha = None

        body: dict = {
            'message': f'Track env: {name} (approved for {username})',
            'content': base64.b64encode(content.encode()).decode(),
        }
        if sha:
            body['sha'] = sha

        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method='PUT')
        with urllib.request.urlopen(req) as resp:
            resp.read()
        _log.info('env_record_committed name=%s', name)
    except Exception as e:
        _log.warning('env_record_commit_failed name=%s err=%s', name, e)


def _submit_venv_build(name: str, packages: list[str]) -> str:
    """Write build script to Puhti and sbatch it. Returns Slurm job ID."""
    script = _build_venv_script(name, packages)
    script_path = f'{PUHTI_ENVS}/build-{name}.sh'
    _ssh(f'mkdir -p {PUHTI_ENVS}')
    r = _ssh_write(script_path, script)
    if r.returncode != 0:
        raise HTTPException(500, f'Could not write build script: {r.stderr.strip()}')
    _ssh(f'chmod +x {script_path}')
    r = _ssh(f'sbatch {script_path}', timeout=30)
    if r.returncode != 0:
        raise HTTPException(500, f'sbatch venv build failed: {r.stderr.strip()}')
    job_id = r.stdout.strip().split()[-1]
    _log.info('venv_build_submitted name=%s slurm_job=%s', name, job_id)
    return job_id


# ── Container build poller ────────────────────────────────────────────────────

def _container_build_poller():
    """Background thread: poll Slurm for venv build jobs every 60 s."""
    while True:
        time.sleep(60)
        try:
            with contextlib.closing(_db()) as db:
                rows = db.execute(
                    "SELECT id, username, container, email, build_job_id "
                    "FROM container_requests WHERE status='building'"
                ).fetchall()
            if not rows:
                continue
            for row in rows:
                slurm_id = row['build_job_id']
                if not slurm_id:
                    continue
                state_r = _ssh(
                    f"sacct -j {slurm_id} --format=State --noheader 2>/dev/null | head -1 | tr -d ' '",
                    timeout=15
                )
                state = state_r.stdout.strip()
                if state == 'COMPLETED':
                    # Verify the venv python exists
                    chk = _ssh(f'test -f {PUHTI_ENVS}/{row["container"]}/bin/python && echo OK', timeout=10)
                    if 'OK' in chk.stdout:
                        with contextlib.closing(_db()) as db:
                            db.execute("UPDATE container_requests SET status='ready' WHERE id=?", (row['id'],))
                            db.commit()
                        _log.info('venv_ready container=%s user=%s', row['container'], row['username'])
                        if row['email']:
                            _send_email(
                                row['email'],
                                f'Your Puhti environment "{row["container"]}" is ready ✓',
                                f'Good news! Your Python environment has finished building on Puhti.\n\n'
                                f'Environment: {row["container"]}\n\n'
                                f'Open JupyterLab → Run on Puhti → select "{row["container"]}" from the environment dropdown.'
                            )
                    else:
                        with contextlib.closing(_db()) as db:
                            db.execute("UPDATE container_requests SET status='failed' WHERE id=?", (row['id'],))
                            db.commit()
                        _log.warning('venv_build_completed_no_python container=%s', row['container'])
                elif state in ('FAILED', 'CANCELLED', 'TIMEOUT', 'NODE_FAIL'):
                    with contextlib.closing(_db()) as db:
                        db.execute("UPDATE container_requests SET status='failed' WHERE id=?", (row['id'],))
                        db.commit()
                    _log.warning('venv_build_failed container=%s state=%s', row['container'], state)
        except Exception as e:
            _log.warning('container_build_poller error: %s', e)


threading.Thread(target=_container_build_poller, daemon=True, name='container-poller').start()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get('/billing')
def get_billing():
    """Return remaining billing units for the project."""
    try:
        out = _ssh('csc-projects --show-billing-units 2>/dev/null | grep -A5 project_2014823', timeout=20)
        # Parse lines like: "Billing units remaining: 4231.5"
        remaining = None
        for line in out.splitlines():
            line = line.strip()
            if 'remaining' in line.lower():
                parts = line.split(':')
                if len(parts) == 2:
                    try:
                        remaining = float(parts[1].strip())
                    except ValueError:
                        pass
        return {'raw': out, 'remaining': remaining}
    except Exception:
        return {'raw': 'unavailable', 'remaining': None}


@router.get('/containers')
def list_containers():
    """Return available environment names (venv dirs in PUHTI_ENVS)."""
    try:
        r = _ssh(
            f'for d in {PUHTI_ENVS}/*/bin/python; do '
            f'[ -f "$d" ] && basename $(dirname $(dirname "$d")); done 2>/dev/null',
            timeout=15
        )
        names = [n.strip() for n in r.stdout.splitlines() if n.strip()]
        return {'containers': names or [DEFAULT_CONTAINER]}
    except Exception:
        return {'containers': [DEFAULT_CONTAINER], 'puhti_unreachable': True}


@router.post('/run-notebook')
async def run_notebook(
    request:      Request,
    notebook:     UploadFile = File(...),
    requirements: Optional[UploadFile] = File(None),
    partition:    str = Form('small'),
    cpus:         int = Form(4),
    memory_gb:    int = Form(16),
    container:    str = Form(DEFAULT_CONTAINER),
    username:     str = Form(''),
    email:        str = Form(''),
    time_hours:   int = Form(2),
    x_jupyterhub_token: Optional[str] = Header(None),
):
    """Accept a .ipynb file, convert it to script.py on the head node, then submit."""
    _check_rate_limit(request)
    if partition not in VALID_PARTITIONS:
        raise HTTPException(400, f'Unknown partition: {partition}')

    if x_jupyterhub_token:
        username = _validate_jupyterhub_token(x_jupyterhub_token)

    job_id  = str(uuid.uuid4())
    job_dir = os.path.join(NFS_RUNS, job_id)
    os.makedirs(job_dir, exist_ok=True)

    nb_path = os.path.join(job_dir, notebook.filename or 'notebook.ipynb')
    _write_upload(notebook, nb_path)
    if requirements:
        _write_upload(requirements, os.path.join(job_dir, 'requirements.txt'))

    # Convert notebook → script.py using nbconvert
    result = subprocess.run(
        ['/opt/hbv/venv/bin/jupyter', 'nbconvert', '--to', 'script', nb_path,
         '--output', os.path.join(job_dir, 'script')],
        capture_output=True, text=True,
    )
    script_path = os.path.join(job_dir, 'script.py')
    if result.returncode != 0 or not os.path.exists(script_path):
        raise HTTPException(500, f'nbconvert failed: {result.stderr.strip()}')

    # Strip ipython magic lines that won't run as plain python
    _strip_magics(script_path)

    return await _submit_job(job_id, job_dir, partition, cpus, memory_gb, container, username, email, time_hours)


@router.post('/run-code')
async def run_code(
    request:      Request,
    script:       UploadFile = File(...),
    requirements: Optional[UploadFile] = File(None),
    partition:    str = Form('small'),
    cpus:         int = Form(4),
    memory_gb:    int = Form(16),
    container:    str = Form(DEFAULT_CONTAINER),
    username:     str = Form(''),
    email:        str = Form(''),
    time_hours:   int = Form(2),
    x_jupyterhub_token: Optional[str] = Header(None),
):
    _check_rate_limit(request)
    if partition not in VALID_PARTITIONS:
        raise HTTPException(400, f'Unknown partition: {partition}. '
                                 f'Choose from {sorted(VALID_PARTITIONS)}')

    if x_jupyterhub_token:
        username = _validate_jupyterhub_token(x_jupyterhub_token)

    job_id  = str(uuid.uuid4())
    job_dir = os.path.join(NFS_RUNS, job_id)
    os.makedirs(job_dir, exist_ok=True)

    _write_upload(script, os.path.join(job_dir, 'script.py'))
    if requirements:
        _write_upload(requirements, os.path.join(job_dir, 'requirements.txt'))

    return await _submit_job(job_id, job_dir, partition, cpus, memory_gb, container, username, email, time_hours)


@router.get('/my-jobs/{username}')
def my_jobs(username: str, x_jupyterhub_token: Optional[str] = Header(None)):
    """Return all jobs submitted by a given username, newest first."""
    token_user = _validate_jupyterhub_token(x_jupyterhub_token) if x_jupyterhub_token else None
    if token_user and token_user != username:
        raise HTTPException(403, 'Token does not match requested username')
    if not token_user:
        raise HTTPException(401, 'Missing JupyterHub token')
    with contextlib.closing(_db()) as db:
        rows = db.execute(
            "SELECT job_id, slurm_id, status, partition, created FROM runs "
            "WHERE username=? ORDER BY created DESC LIMIT 50",
            (username,)
        ).fetchall()
    return {'jobs': [dict(r) for r in rows]}


@router.get('/my-jobs-status/{username}')
def my_jobs_status(username: str, x_jupyterhub_token: Optional[str] = Header(None)):
    """Return all jobs for a user with statuses refreshed in a single Slurm SSH call."""""
    token_user = _validate_jupyterhub_token(x_jupyterhub_token) if x_jupyterhub_token else None
    if token_user and token_user != username:
        raise HTTPException(403, 'Token does not match requested username')
    if not token_user:
        raise HTTPException(401, 'Missing JupyterHub token')
    with contextlib.closing(_db()) as db:
        rows = db.execute(
            "SELECT job_id, slurm_id, status, partition, created FROM runs "
            "WHERE username=? ORDER BY created DESC LIMIT 50",
            (username,)
        ).fetchall()
    jobs = [dict(r) for r in rows]

    active = [j for j in jobs if j['status'] in ('queued', 'running')]
    if active:
        slurm_ids = ','.join(j['slurm_id'] for j in active)
        r = _ssh(f"squeue -j {slurm_ids} -h --format='%i %T' 2>/dev/null", timeout=20)
        live = {}
        for line in r.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) == 2:
                live[parts[0]] = parts[1]

        for job in active:
            slurm_state = live.get(job['slurm_id'], '')
            if not slurm_state:
                user_slug  = re.sub(r'[^a-z0-9_-]', '_', username.lower()) if username else 'anonymous'
                remote_dir = f'{PUHTI_RUNS}/{user_slug}/{job["job_id"]}'
                try:
                    _rsync_from(f'{remote_dir}/', os.path.join(NFS_RUNS, job['job_id']) + '/')
                except Exception:
                    pass
                output_dir = os.path.join(NFS_RUNS, job['job_id'], 'output')
                new_status = 'done' if (os.path.isdir(output_dir) and os.listdir(output_dir)) else 'failed'
            elif slurm_state in ('RUNNING', 'COMPLETING'):
                new_status = 'running'
            elif slurm_state in ('PENDING', 'CONFIGURING'):
                new_status = 'queued'
            elif slurm_state in ('FAILED', 'TIMEOUT', 'NODE_FAIL'):
                new_status = 'failed'
            elif slurm_state == 'CANCELLED':
                new_status = 'cancelled'
            else:
                new_status = 'running'

            if new_status != job['status']:
                _log.info('job_status_change job_id=%s slurm_id=%s %s->%s',
                          job['job_id'], job['slurm_id'], job['status'], new_status)
                _set_status(job['job_id'], new_status)
                job['status'] = new_status
                if new_status in ('done', 'failed'):
                    full = _get(job['job_id'])
                    email = full.get('email', '') if full else ''
                    slurm_id = job['slurm_id']
                    if new_status == 'done':
                        _send_email(email, f'Puhti job {slurm_id} completed ✓',
                            f'Your Puhti job has finished successfully.\n\nJob ID:   {job["job_id"]}\nSlurm ID: {slurm_id}\n\n'
                            f'Open JupyterLab → Jobs tab → click "↓ Get" to save results to your files.')
                    else:
                        stderr = _read_tail(os.path.join(NFS_RUNS, job['job_id'], 'stderr.txt'), lines=50)
                        _send_email(email, f'Puhti job {slurm_id} failed ✗',
                            f'Your Puhti job failed.\n\nJob ID:   {job["job_id"]}\nSlurm ID: {slurm_id}\n\n'
                            + (f'--- Error output ---\n{stderr}\n\n' if stderr.strip() else '')
                            + f'Open JupyterLab → Jobs tab → click "📋 Log" to see the full output.\n'
                            f'You can resubmit with the "↺ Resubmit" button.')

    return {'jobs': jobs}


@router.get('/run-status/{job_id}')
def run_status(job_id: str):
    job = _get(job_id)
    if not job:
        raise HTTPException(404, 'Job not found')

    if job['status'] in ('done', 'failed', 'cancelled'):
        return {'job_id': job_id, 'slurm_id': job['slurm_id'], 'status': job['status']}

    # Poll Slurm
    r = _ssh(f"squeue -j {job['slurm_id']} -h --format=%T", timeout=15)
    slurm_state = r.stdout.strip()

    if not slurm_state:
        # No longer in queue — rsync first, then check if output exists
        user_slug  = re.sub(r'[^a-z0-9_-]', '_', (job.get('username') or '').lower()) or 'anonymous'
        remote_dir = f'{PUHTI_RUNS}/{user_slug}/{job_id}'
        try:
            _rsync_from(
                f'{remote_dir}/',
                os.path.join(NFS_RUNS, job_id) + '/',
            )
        except Exception as e:
            import logging
            logging.getLogger('puhti-run').error(f'rsync failed for {job_id}: {e}')
        output_dir = os.path.join(NFS_RUNS, job_id, 'output')
        has_output = os.path.isdir(output_dir) and bool(os.listdir(output_dir))
        new_status = 'done' if has_output else 'failed'
    elif slurm_state in ('RUNNING', 'COMPLETING'):
        new_status = 'running'
    elif slurm_state in ('PENDING', 'CONFIGURING'):
        new_status = 'queued'
    elif slurm_state in ('FAILED', 'TIMEOUT', 'NODE_FAIL'):
        new_status = 'failed'
    elif slurm_state == 'CANCELLED':
        new_status = 'cancelled'
    else:
        new_status = 'running'

    if new_status != job['status']:
        _set_status(job_id, new_status)
        if new_status in ('done', 'failed'):
            email = job.get('email', '')
            slurm_id = job['slurm_id']
            if new_status == 'done':
                _send_email(email,
                    f'Puhti job {slurm_id} completed ✓',
                    f'Your Puhti job has finished successfully.\n\n'
                    f'Job ID:   {job_id}\nSlurm ID: {slurm_id}\n\n'
                    f'Open JupyterLab → Jobs tab → click "↓ Get" to save results to your files.')
            else:
                stderr = _read_tail(os.path.join(NFS_RUNS, job_id, 'stderr.txt'), lines=50)
                _send_email(email,
                    f'Puhti job {slurm_id} failed ✗',
                    f'Your Puhti job failed.\n\n'
                    f'Job ID:   {job_id}\nSlurm ID: {slurm_id}\n\n'
                    + (f'--- Error output ---\n{stderr}\n\n' if stderr.strip() else '')
                    + f'Open JupyterLab → Jobs tab → click "📋 Log" to see the full output.\n'
                    f'You can resubmit with the "↺ Resubmit" button.')

    return {'job_id': job_id, 'slurm_id': job['slurm_id'], 'status': new_status}


@router.get('/run-results/{job_id}')
def run_results(job_id: str):
    job = _get(job_id)
    if not job:
        raise HTTPException(404, 'Job not found')

    output_dir = os.path.join(NFS_RUNS, job_id, 'output')
    if not os.path.isdir(output_dir) or not os.listdir(output_dir):
        raise HTTPException(404, 'No results yet — job may still be running')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(output_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                zf.write(fpath, os.path.relpath(fpath, output_dir))
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type='application/zip',
        headers={'Content-Disposition': f'attachment; filename=results_{job_id[:8]}.zip'},
    )



def _parse_packages_from_def(content: bytes) -> list[str]:
    packages = []
    in_pip = False
    for line in content.decode(errors='ignore').splitlines():
        stripped = line.strip()
        if 'pip install' in stripped:
            in_pip = True
        if in_pip:
            pkg = stripped.rstrip('\\').strip()
            if pkg and not pkg.startswith('#') and not pkg.startswith('pip') and not pkg.startswith('--'):
                packages.append(pkg)
            if not stripped.endswith('\\'):
                in_pip = False
    return packages


def _store_container_request(username: str, container: str, pr_url: str, pr_number: int, email: str = ''):
    with contextlib.closing(_db()) as db:
        db.execute(
            "INSERT INTO container_requests (username, container, pr_url, pr_number, email) VALUES (?,?,?,?,?)",
            (username, container, pr_url, pr_number, email)
        )
        db.commit()


def _generate_def(name: str, packages: list[str]) -> bytes:
    pkg_lines = ' \\\n        '.join(packages)
    template = f"""Bootstrap: docker
From: ubuntu:22.04

%post
    export TMPDIR=/scratch/project_2014823/tmp
    export PIP_NO_CACHE_DIR=1
    export DEBIAN_FRONTEND=noninteractive
    mkdir -p $TMPDIR

    apt-get update -q && apt-get install -y --no-install-recommends \\
        python3 python3-dev python3-pip python3-venv \\
        gcc g++ git curl libgeos-dev \\
        && rm -rf /var/lib/apt/lists/*

    # Install into /opt/venv so Puhti's apptainer bind of /usr/local does not
    # overwrite our packages. Use explicit /opt/venv/bin/pip everywhere.
    python3 -m venv /opt/venv
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip

    # torch/torchvision: use cu121 index-url (not extra) to force CUDA 12.x
    # wheels — compatible with Puhti CUDA driver 12.2.
    /opt/venv/bin/pip install --no-cache-dir \\
        --index-url https://download.pytorch.org/whl/cu121 \\
        torch torchvision

    /opt/venv/bin/pip install --no-cache-dir \\
        numpy \\
        pandas \\
        matplotlib \\
        seaborn \\
        requests \\
        tqdm \\
        ipykernel \\
        nbformat \\
        {pkg_lines}

    mkdir -p /app /output

%environment
    export PATH=/opt/venv/bin:$PATH
    export VIRTUAL_ENV=/opt/venv
    export PYTHONUNBUFFERED=1
    export MPLBACKEND=Agg

%labels
    Name {name}
    Version 1.0

%help
    Custom container: {name}
    Base: ubuntu:22.04, venv at /opt/venv (avoids Puhti /usr/local bind)
    Extra packages: {', '.join(packages)}
"""
    return template.encode()


@router.post('/request-container-simple')
async def request_container_simple(
    name:        str = Form(...),
    packages:    str = Form(...),
    description: str = Form(''),
    username:    str = Form(''),
    email:       str = Form(''),
):
    """Store a venv build request; admin approves it in the admin panel."""
    if not re.match(r'^[a-z0-9-]+$', name):
        raise HTTPException(400, 'Container name must be lowercase letters, numbers, and hyphens only')

    pkg_list = [p.strip() for p in packages.splitlines() if p.strip()]
    if not pkg_list:
        raise HTTPException(400, 'At least one package is required')

    with contextlib.closing(_db()) as db:
        db.execute(
            "INSERT INTO container_requests (username, container, packages, email, status) VALUES (?,?,?,?,'pending')",
            (username, name, ' '.join(pkg_list), email)
        )
        db.commit()

    _log.info('venv_requested user=%s name=%s packages=%s', username, name, pkg_list)
    return {'container_name': name, 'status': 'pending', 'message': 'Request submitted — an admin will approve it shortly.'}


async def _open_container_pr(name: str, content: bytes, description: str, packages: list[str] = [], username: str = '', email: str = '') -> dict:
    if not GITHUB_TOKEN:
        raise HTTPException(500, 'GITHUB_TOKEN not configured on server')

    branch = f'container/{name}-{uuid.uuid4().hex[:6]}'
    path   = f'apptainer/{name}.def'

    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
    }

    def _gh(method, endpoint, body=None):
        url = f'https://api.github.com/repos/{GITHUB_REPO}{endpoint}'
        data = json.dumps(body).encode() if body else None
        req  = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise HTTPException(502, f'GitHub API error: {e.read().decode()}')

    repo_info  = _gh('GET', '')
    default_br = repo_info['default_branch']
    ref_info   = _gh('GET', f'/git/ref/heads/{default_br}')
    base_sha   = ref_info['object']['sha']

    _gh('POST', '/git/refs', {'ref': f'refs/heads/{branch}', 'sha': base_sha})

    file_body = {
        'message': f'Add {name} container definition',
        'content': base64.b64encode(content).decode(),
        'branch':  branch,
    }
    try:
        existing = _gh('GET', f'/contents/{path}?ref={branch}')
        file_body['sha'] = existing['sha']
    except HTTPException:
        pass
    _gh('PUT', f'/contents/{path}', file_body)

    pr_body = f'## New Container Request: `{name}`\n\n'
    if description:
        pr_body += f'**Description:** {description}\n\n'
    if packages:
        pr_body += f'**Packages:** `{"`, `".join(packages)}`\n\n'
    pr_body += f'This PR adds `apptainer/{name}.def`. Merging will trigger an automatic build of `{name}.sif` on Puhti.'

    pr = _gh('POST', '/pulls', {
        'title': f'Add container: {name}',
        'body':  pr_body,
        'head':  branch,
        'base':  default_br,
    })

    # Store request for notification tracking
    if username:
        _store_container_request(username, name, pr['html_url'], pr['number'], email)

    return {'pr_url': pr['html_url'], 'container_name': name, 'branch': branch}


@router.post('/request-container')
async def request_container(
    def_file:    UploadFile = File(...),
    description: str = Form(''),
    username:    str = Form(''),
    email:       str = Form(''),
):
    content  = await def_file.read()
    name     = re.sub(r'[^a-z0-9-]', '-', (def_file.filename or 'custom').replace('.def', '').lower())
    packages = _parse_packages_from_def(content)
    return await _open_container_pr(name, content, description, packages, username, email)


@router.get('/my-container-requests/{username}')
def my_container_requests(username: str):
    """Return venv build requests for a user."""
    with contextlib.closing(_db()) as db:
        rows = db.execute(
            "SELECT id, container, packages, status, created FROM container_requests "
            "WHERE username=? ORDER BY created DESC LIMIT 20",
            (username,)
        ).fetchall()
    return {'requests': [dict(r) for r in rows]}


@router.post('/cancel-job/{job_id}')
def cancel_job(job_id: str):
    job = _get(job_id)
    if not job:
        raise HTTPException(404, 'Job not found')
    if job['status'] in ('done', 'failed', 'cancelled'):
        return {'job_id': job_id, 'status': job['status'], 'message': 'Job already finished'}
    r = _ssh(f"scancel {job['slurm_id']}", timeout=15)
    if r.returncode != 0:
        raise HTTPException(500, f'scancel failed: {r.stderr.strip()}')
    _set_status(job_id, 'cancelled')
    return {'job_id': job_id, 'slurm_id': job['slurm_id'], 'status': 'cancelled'}


@router.get('/run-logs/{job_id}')
def run_logs(job_id: str):
    job = _get(job_id)
    if not job:
        raise HTTPException(404, 'Job not found')

    local_job  = os.path.join(NFS_RUNS, job_id)
    if job['status'] in ('queued', 'running'):
        user_slug  = re.sub(r'[^a-z0-9_-]', '_', (job.get('username') or '').lower()) or 'anonymous'
        remote_dir = f'{PUHTI_RUNS}/{user_slug}/{job_id}'
        try:
            for fname in ('stdout.txt', 'stderr.txt'):
                _rsync_from(f'{remote_dir}/{fname}', local_job + '/')
        except Exception:
            pass

    return {
        'stdout': _read_tail(os.path.join(local_job, 'stdout.txt')),
        'stderr': _read_tail(os.path.join(local_job, 'stderr.txt')),
    }


# ── Utilities ─────────────────────────────────────────────────────────────────

MAX_CONCURRENT = int(os.environ.get('MAX_CONCURRENT_JOBS', '3'))

PARTITION_MAX_HOURS = {'small': 72, 'large': 72, 'longrun': 336, 'gpu': 72, 'gpumedium': 72}

# Simple sliding-window rate limiter: max N submissions per IP per minute
_rate_lock = threading.Lock()
_rate_hits: dict = collections.defaultdict(list)
_RATE_LIMIT = int(os.environ.get('SUBMIT_RATE_LIMIT', '10'))  # requests per minute per IP

def _check_rate_limit(request: Request):
    ip = request.client.host if request.client else 'unknown'
    now = time.time()
    with _rate_lock:
        hits = _rate_hits[ip]
        # keep only hits in the last 60s
        _rate_hits[ip] = [t for t in hits if now - t < 60]
        if len(_rate_hits[ip]) >= _RATE_LIMIT:
            _log.warning('rate_limit_hit ip=%s', ip)
            raise HTTPException(429, f'Too many requests. Max {_RATE_LIMIT} submissions per minute.')
        _rate_hits[ip].append(now)


@router.post('/resubmit/{job_id}')
async def resubmit_job(job_id: str, x_jupyterhub_token: Optional[str] = Header(None)):
    """Resubmit a previous job using its saved params."""
    job = _get(job_id)
    if not job:
        raise HTTPException(404, 'Job not found')

    params_path = os.path.join(NFS_RUNS, job_id, 'params.json')
    if not os.path.exists(params_path):
        raise HTTPException(400, 'No params saved for this job — cannot resubmit')

    with open(params_path) as f:
        params = json.load(f)

    username = params.get('username', job.get('username', ''))
    email    = params.get('email', job.get('email', ''))
    if x_jupyterhub_token:
        username = _validate_jupyterhub_token(x_jupyterhub_token)

    new_job_id  = str(uuid.uuid4())
    old_job_dir = os.path.join(NFS_RUNS, job_id)
    new_job_dir = os.path.join(NFS_RUNS, new_job_id)
    shutil.copytree(old_job_dir, new_job_dir,
                    ignore=shutil.ignore_patterns('output', 'stdout.txt', 'stderr.txt', 'params.json'))

    return await _submit_job(
        new_job_id, new_job_dir,
        params['partition'], params['cpus'], params['memory_gb'],
        params.get('container', DEFAULT_CONTAINER),
        username, email,
        params.get('time_hours', 2),
    )


async def _submit_job(job_id: str, job_dir: str, partition: str,
                      cpus: int, memory_gb: int,
                      container: str = DEFAULT_CONTAINER,
                      username: str = '', email: str = '',
                      time_hours: int = 2) -> dict:
    """Rsync job dir to Puhti and sbatch it. Shared by /run-code and /run-notebook."""
    if username and _active_count(username) >= MAX_CONCURRENT:
        raise HTTPException(429, f'Too many active jobs. Max {MAX_CONCURRENT} concurrent jobs per user.')

    max_h = PARTITION_MAX_HOURS.get(partition, 72)
    time_hours = max(1, min(time_hours, max_h))

    with open(os.path.join(job_dir, 'params.json'), 'w') as f:
        json.dump({'partition': partition, 'cpus': cpus, 'memory_gb': memory_gb,
                   'container': container, 'username': username, 'email': email,
                   'time_hours': time_hours}, f)

    user_slug  = re.sub(r'[^a-z0-9_-]', '_', username.lower()) if username else 'anonymous'
    remote_dir = f'{PUHTI_RUNS}/{user_slug}/{job_id}'
    _ssh(f'mkdir -p {remote_dir}')
    _rsync_to(job_dir + '/', remote_dir + '/')

    use_gpu   = '1' if partition in GPU_PARTITIONS else '0'
    gres      = '--gres=gpu:v100:1' if partition in GPU_PARTITIONS else ''
    venv_path = f'{PUHTI_ENVS}/{container}'
    time_str  = f'{time_hours:02d}:00:00'
    cmd = (
        f'sbatch'
        f' --partition={partition}'
        f' --cpus-per-task={cpus}'
        f' --mem={memory_gb}G'
        f' --time={time_str}'
        f'{" " + gres if gres else ""}'
        f' --export=ALL,JOB_DIR={remote_dir},USE_GPU={use_gpu},VENV_PATH={venv_path}'
        f' {SLURM_SH}'
    )
    r = _ssh(cmd, timeout=30)
    if r.returncode != 0:
        _log.error('sbatch_failed user=%s job_id=%s stderr=%s', username or 'anon', job_id, r.stderr.strip())
        raise HTTPException(500, f'sbatch failed: {r.stderr.strip()}')

    slurm_id = r.stdout.strip().split()[-1]
    _insert(job_id, slurm_id, partition, username, email, cpus, memory_gb)
    _log.info('job_submitted user=%s job_id=%s slurm_id=%s partition=%s cpus=%d mem=%dG time=%sh',
              username or 'anon', job_id, slurm_id, partition, cpus, memory_gb, time_hours)
    return {'job_id': job_id, 'slurm_id': slurm_id, 'status': 'queued'}


def _strip_magics(script_path: str) -> None:
    """Remove IPython magic lines (%magic, !shell) that break plain python."""
    with open(script_path) as f:
        lines = f.readlines()
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('%') or stripped.startswith('!'):
            cleaned.append(f'# {line}')  # comment it out rather than delete
        else:
            cleaned.append(line)
    with open(script_path, 'w') as f:
        f.writelines(cleaned)


def _write_upload(upload: UploadFile, path: str):
    with open(path, 'wb') as f:
        shutil.copyfileobj(upload.file, f)


def _read_tail(path: str, lines: int = 100) -> str:
    if not os.path.exists(path):
        return ''
    with open(path) as f:
        return ''.join(f.readlines()[-lines:])
