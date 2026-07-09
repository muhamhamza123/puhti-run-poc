"""
Complete /run-code API router — submit, status, logs, results.
"""
import io, os, uuid, shutil, subprocess, sqlite3, zipfile, contextlib, base64, re
import urllib.request, urllib.error, json
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Header
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
DEFAULT_CONTAINER = 'general-compute'

GITHUB_TOKEN  = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO   = os.environ.get('GITHUB_REPO', 'muhamhamza123/puhti-run-poc')
JUPYTERHUB_URL = os.environ.get('JUPYTERHUB_URL', 'https://diwa-data-lab-vre.rahtiapp.fi')


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
    # migrate existing tables that lack username column
    cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
    if 'username' not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN username TEXT DEFAULT ''")
    conn.commit()
    return conn


def _insert(job_id, slurm_id, partition, username=''):
    with contextlib.closing(_db()) as db:
        db.execute("INSERT INTO runs (job_id, slurm_id, partition, username) VALUES (?,?,?,?)",
                   (job_id, slurm_id, partition, username))
        db.commit()


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

def _ssh(cmd: str, timeout: int = 30):
    return subprocess.run(
        ['ssh', '-i', SSH_KEY,
         '-o', 'StrictHostKeyChecking=no',
         '-o', 'BatchMode=yes',
         '-o', 'ConnectTimeout=15',
         f'{PUHTI_USER}@{PUHTI_HOST}', cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def _rsync_to(src: str, dst: str, timeout: int = 300):
    subprocess.run([
        'rsync', '-az',
        '-e', f'ssh -i {SSH_KEY} -o StrictHostKeyChecking=no -o BatchMode=yes',
        src, f'{PUHTI_USER}@{PUHTI_HOST}:{dst}',
    ], check=True, timeout=timeout)


def _rsync_from(src: str, dst: str, timeout: int = 300):
    os.makedirs(dst, exist_ok=True)
    subprocess.run([
        'rsync', '-az',
        '-e', f'ssh -i {SSH_KEY} -o StrictHostKeyChecking=no -o BatchMode=yes',
        f'{PUHTI_USER}@{PUHTI_HOST}:{src}', dst,
    ], check=True, timeout=timeout)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get('/containers')
def list_containers():
    """Return available container names (any .sif in PUHTI_RUNS)."""
    try:
        r = _ssh(f'ls {PUHTI_RUNS}/*.sif 2>/dev/null', timeout=15)
        names = []
        for line in r.stdout.splitlines():
            base = os.path.basename(line)
            if base.endswith('.sif'):
                names.append(base[:-4])
        return {'containers': names or [DEFAULT_CONTAINER]}
    except Exception:
        return {'containers': [DEFAULT_CONTAINER], 'puhti_unreachable': True}


@router.post('/run-notebook')
async def run_notebook(
    notebook:     UploadFile = File(...),
    requirements: Optional[UploadFile] = File(None),
    partition:    str = Form('small'),
    cpus:         int = Form(4),
    memory_gb:    int = Form(16),
    container:    str = Form(DEFAULT_CONTAINER),
    username:     str = Form(''),
    x_jupyterhub_token: Optional[str] = Header(None),
):
    """Accept a .ipynb file, convert it to script.py on the head node, then submit."""
    if partition not in VALID_PARTITIONS:
        raise HTTPException(400, f'Unknown partition: {partition}')

    if x_jupyterhub_token:
        username = _validate_jupyterhub_token(x_jupyterhub_token)

    job_id  = str(uuid.uuid4())
    job_dir = os.path.join(NFS_RUNS, job_id)
    os.makedirs(job_dir, exist_ok=True)

    # Save notebook
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

    return await _submit_job(job_id, job_dir, partition, cpus, memory_gb, container, username)


@router.post('/run-code')
async def run_code(
    script:       UploadFile = File(...),
    requirements: Optional[UploadFile] = File(None),
    partition:    str = Form('small'),
    cpus:         int = Form(4),
    memory_gb:    int = Form(16),
    container:    str = Form(DEFAULT_CONTAINER),
    username:     str = Form(''),
    x_jupyterhub_token: Optional[str] = Header(None),
):
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

    return await _submit_job(job_id, job_dir, partition, cpus, memory_gb, container, username)


@router.get('/my-jobs/{username}')
def my_jobs(username: str):
    """Return all jobs submitted by a given username, newest first."""
    with contextlib.closing(_db()) as db:
        rows = db.execute(
            "SELECT job_id, slurm_id, status, partition, created FROM runs "
            "WHERE username=? ORDER BY created DESC LIMIT 50",
            (username,)
        ).fetchall()
    return {'jobs': [dict(r) for r in rows]}


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


LABEL_RULES = {
    'hydrology':  {'xarray', 'netcdf4', 'rasterio', 'geopandas', 'fiona', 'shapely', 'pysheds', 'hydromt'},
    'ml':         {'torch', 'tensorflow', 'keras', 'scikit-learn', 'sklearn', 'xgboost', 'lightgbm', 'transformers', 'huggingface'},
    'geospatial': {'gdal', 'rasterio', 'geopandas', 'cartopy', 'pyproj', 'shapely', 'fiona'},
}


def _detect_labels(packages: list[str]) -> list[str]:
    pkg_set = {p.lower().split('==')[0].split('>=')[0].strip() for p in packages}
    labels = ['container-request']
    for label, keywords in LABEL_RULES.items():
        if pkg_set & keywords:
            labels.append(label)
    if not any(l in labels for l in LABEL_RULES):
        labels.append('general')
    return labels


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


def _store_container_request(username: str, container: str, pr_url: str, pr_number: int):
    with contextlib.closing(_db()) as db:
        db.execute(
            "INSERT INTO container_requests (username, container, pr_url, pr_number) VALUES (?,?,?,?)",
            (username, container, pr_url, pr_number)
        )
        db.commit()


def _generate_def(name: str, packages: list[str]) -> bytes:
    pkg_lines = '\n        '.join(packages)
    template = f"""Bootstrap: docker
From: python:3.11-slim

%post
    export TMPDIR=/scratch/project_2014823/tmp
    export PIP_NO_CACHE_DIR=1
    mkdir -p $TMPDIR

    apt-get update -q && apt-get install -y --no-install-recommends \\
        gcc g++ git curl libgeos-dev \\
        && rm -rf /var/lib/apt/lists/*

    pip install --no-cache-dir \\
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
    export PYTHONUNBUFFERED=1
    export MPLBACKEND=Agg

%labels
    Name {name}
    Version 1.0

%help
    Custom container: {name}
    Extra packages: {', '.join(packages)}
"""
    return template.encode()


@router.post('/request-container-simple')
async def request_container_simple(
    name:        str = Form(...),
    packages:    str = Form(...),
    description: str = Form(''),
    username:    str = Form(''),
):
    """Generate a .def from a package list and open a PR."""
    if not re.match(r'^[a-z0-9-]+$', name):
        raise HTTPException(400, 'Container name must be lowercase letters, numbers, and hyphens only')

    pkg_list = [p.strip() for p in packages.splitlines() if p.strip()]
    if not pkg_list:
        raise HTTPException(400, 'At least one package is required')

    content = _generate_def(name, pkg_list)
    return await _open_container_pr(name, content, description, pkg_list, username)


async def _open_container_pr(name: str, content: bytes, description: str, packages: list[str] = [], username: str = '') -> dict:
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

    # Auto-label
    labels = _detect_labels(packages)
    try:
        _gh('POST', f'/issues/{pr["number"]}/labels', {'labels': labels})
    except Exception:
        pass

    # Store request for notification tracking
    if username:
        _store_container_request(username, name, pr['html_url'], pr['number'])

    return {'pr_url': pr['html_url'], 'container_name': name, 'branch': branch}


@router.post('/request-container')
async def request_container(
    def_file:    UploadFile = File(...),
    description: str = Form(''),
    username:    str = Form(''),
):
    content  = await def_file.read()
    name     = re.sub(r'[^a-z0-9-]', '-', (def_file.filename or 'custom').replace('.def', '').lower())
    packages = _parse_packages_from_def(content)
    return await _open_container_pr(name, content, description, packages, username)


@router.get('/my-container-requests/{username}')
def my_container_requests(username: str):
    """Return container requests for a user, checking live PR status from GitHub."""
    with contextlib.closing(_db()) as db:
        rows = db.execute(
            "SELECT id, container, pr_url, pr_number, status, created FROM container_requests "
            "WHERE username=? ORDER BY created DESC LIMIT 20",
            (username,)
        ).fetchall()
    if not rows:
        return {'requests': []}

    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github+json',
    }

    results = []
    for row in rows:
        r = dict(row)
        if r['status'] == 'pending' and GITHUB_TOKEN:
            try:
                url = f'https://api.github.com/repos/{GITHUB_REPO}/pulls/{r["pr_number"]}'
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    pr = json.loads(resp.read())
                if pr.get('merged'):
                    r['status'] = 'merged'
                    with contextlib.closing(_db()) as db:
                        db.execute("UPDATE container_requests SET status='merged' WHERE id=?", (r['id'],))
                        db.commit()
                elif pr.get('state') == 'closed':
                    r['status'] = 'closed'
            except Exception:
                pass
        results.append(r)

    return {'requests': results}


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
    user_slug  = re.sub(r'[^a-z0-9_-]', '_', (job.get('username') or '').lower()) or 'anonymous'
    remote_dir = f'{PUHTI_RUNS}/{user_slug}/{job_id}'
    try:
        for fname in ('stdout.txt', 'stderr.txt'):
            _rsync_from(
                f'{remote_dir}/{fname}',
                local_job + '/',
            )
    except Exception:
        pass

    return {
        'stdout': _read_tail(os.path.join(local_job, 'stdout.txt')),
        'stderr': _read_tail(os.path.join(local_job, 'stderr.txt')),
    }


# ── Utilities ─────────────────────────────────────────────────────────────────

MAX_CONCURRENT = int(os.environ.get('MAX_CONCURRENT_JOBS', '3'))


async def _submit_job(job_id: str, job_dir: str, partition: str,
                      cpus: int, memory_gb: int,
                      container: str = DEFAULT_CONTAINER,
                      username: str = '') -> dict:
    """Rsync job dir to Puhti and sbatch it. Shared by /run-code and /run-notebook."""
    if username and _active_count(username) >= MAX_CONCURRENT:
        raise HTTPException(429, f'Too many active jobs. Max {MAX_CONCURRENT} concurrent jobs per user.')

    user_slug  = re.sub(r'[^a-z0-9_-]', '_', username.lower()) if username else 'anonymous'
    remote_dir = f'{PUHTI_RUNS}/{user_slug}/{job_id}'
    _ssh(f'mkdir -p {remote_dir}')
    _rsync_to(job_dir + '/', remote_dir + '/')

    use_gpu   = '1' if partition in GPU_PARTITIONS else '0'
    gres      = '--gres=gpu:v100:1' if partition in GPU_PARTITIONS else ''
    sif_path  = f'{PUHTI_RUNS}/{container}.sif'
    cmd = (
        f'sbatch'
        f' --partition={partition}'
        f' --cpus-per-task={cpus}'
        f' --mem={memory_gb}G'
        f'{" " + gres if gres else ""}'
        f' --export=ALL,JOB_DIR={remote_dir},USE_GPU={use_gpu},SIF_PATH={sif_path}'
        f' {SLURM_SH}'
    )
    r = _ssh(cmd, timeout=30)
    if r.returncode != 0:
        raise HTTPException(500, f'sbatch failed: {r.stderr.strip()}')

    slurm_id = r.stdout.strip().split()[-1]
    _insert(job_id, slurm_id, partition, username)
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
