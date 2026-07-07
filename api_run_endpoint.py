"""
Complete /run-code API router — submit, status, logs, results.
"""
import io, os, uuid, shutil, subprocess, sqlite3, zipfile, contextlib
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
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

VALID_PARTITIONS = {'small', 'large', 'longrun', 'gpu', 'gpumedium'}
GPU_PARTITIONS   = {'gpu', 'gpumedium'}


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
            created   TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def _insert(job_id, slurm_id, partition):
    with contextlib.closing(_db()) as db:
        db.execute("INSERT INTO runs (job_id, slurm_id, partition) VALUES (?,?,?)",
                   (job_id, slurm_id, partition))
        db.commit()


def _get(job_id):
    with contextlib.closing(_db()) as db:
        row = db.execute("SELECT * FROM runs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def _set_status(job_id, status):
    with contextlib.closing(_db()) as db:
        db.execute("UPDATE runs SET status=? WHERE job_id=?", (status, job_id))
        db.commit()


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

@router.post('/run-notebook')
async def run_notebook(
    notebook:     UploadFile = File(...),
    requirements: Optional[UploadFile] = File(None),
    partition:    str = Form('small'),
    cpus:         int = Form(4),
    memory_gb:    int = Form(16),
):
    """Accept a .ipynb file, convert it to script.py on the head node, then submit."""
    if partition not in VALID_PARTITIONS:
        raise HTTPException(400, f'Unknown partition: {partition}')

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

    return await _submit_job(job_id, job_dir, partition, cpus, memory_gb)


@router.post('/run-code')
async def run_code(
    script:       UploadFile = File(...),
    requirements: Optional[UploadFile] = File(None),
    partition:    str = Form('small'),
    cpus:         int = Form(4),
    memory_gb:    int = Form(16),
):
    if partition not in VALID_PARTITIONS:
        raise HTTPException(400, f'Unknown partition: {partition}. '
                                 f'Choose from {sorted(VALID_PARTITIONS)}')

    job_id  = str(uuid.uuid4())
    job_dir = os.path.join(NFS_RUNS, job_id)
    os.makedirs(job_dir, exist_ok=True)

    _write_upload(script, os.path.join(job_dir, 'script.py'))
    if requirements:
        _write_upload(requirements, os.path.join(job_dir, 'requirements.txt'))

    return await _submit_job(job_id, job_dir, partition, cpus, memory_gb)


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
        try:
            _rsync_from(
                f'{PUHTI_RUNS}/{job_id}/',
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


@router.get('/run-logs/{job_id}')
def run_logs(job_id: str):
    job = _get(job_id)
    if not job:
        raise HTTPException(404, 'Job not found')

    local_job = os.path.join(NFS_RUNS, job_id)
    try:
        for fname in ('stdout.txt', 'stderr.txt'):
            _rsync_from(
                f'{PUHTI_RUNS}/{job_id}/{fname}',
                local_job + '/',
            )
    except Exception:
        pass

    return {
        'stdout': _read_tail(os.path.join(local_job, 'stdout.txt')),
        'stderr': _read_tail(os.path.join(local_job, 'stderr.txt')),
    }


# ── Utilities ─────────────────────────────────────────────────────────────────

async def _submit_job(job_id: str, job_dir: str, partition: str,
                      cpus: int, memory_gb: int) -> dict:
    """Rsync job dir to Puhti and sbatch it. Shared by /run-code and /run-notebook."""
    remote_dir = f'{PUHTI_RUNS}/{job_id}'
    _ssh(f'mkdir -p {remote_dir}')
    _rsync_to(job_dir + '/', remote_dir + '/')

    gpu_flag = '--nv' if partition in GPU_PARTITIONS else ''
    gres     = '--gres=gpu:v100:1' if partition in GPU_PARTITIONS else ''
    cmd = (
        f'sbatch'
        f' --partition={partition}'
        f' --cpus-per-task={cpus}'
        f' --mem={memory_gb}G'
        f'{" " + gres if gres else ""}'
        f' --export=ALL,JOB_DIR={remote_dir},GPU_FLAG={gpu_flag}'
        f' {SLURM_SH}'
    )
    r = _ssh(cmd, timeout=30)
    if r.returncode != 0:
        raise HTTPException(500, f'sbatch failed: {r.stderr.strip()}')

    slurm_id = r.stdout.strip().split()[-1]
    _insert(job_id, slurm_id, partition)
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
