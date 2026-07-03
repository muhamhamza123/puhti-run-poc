"""
POC: /run-code endpoint to add to the existing FastAPI gateway.

Accepts a Python script + optional requirements.txt + optional input files,
rsyncs them to Puhti scratch, submits a generic Slurm job, and returns a job_id.
Status polling and result download reuse the existing /status and /download logic.
"""
import os, uuid, shutil, subprocess
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from typing import Optional

router = APIRouter()

NFS_RUNS    = os.environ.get('HBV_RUNS_ROOT', '/data/hbv/runs')
PUHTI_RUNS  = '/scratch/project_2014823/runs'
SLURM_SH    = '/scratch/project_2014823/generic_run.sh'  # uploaded once

PARTITION_GPU_MAP = {
    'gpu':      '--gres=gpu:v100:1',
    'gpumedium':'--gres=gpu:v100:2',
    'small':    '',
    'large':    '',
    'longrun':  '',
}


@router.post('/run-code')
async def run_code(
    script:       UploadFile = File(...),
    requirements: Optional[UploadFile] = File(None),
    partition:    str  = Form('small'),
    cpus:         int  = Form(4),
    memory_gb:    int  = Form(16),
    inputs:       list[UploadFile] = File(default=[]),
):
    if partition not in PARTITION_GPU_MAP:
        raise HTTPException(400, f'Unknown partition: {partition}')

    job_id  = str(uuid.uuid4())
    job_dir = os.path.join(NFS_RUNS, job_id)
    os.makedirs(job_dir, exist_ok=True)

    # Save uploaded files to NFS
    _save(script,       os.path.join(job_dir, 'script.py'))
    if requirements:
        _save(requirements, os.path.join(job_dir, 'requirements.txt'))
    for f in inputs:
        _save(f, os.path.join(job_dir, f.filename))

    # Rsync job dir to Puhti scratch
    remote_dir = f'{PUHTI_RUNS}/{job_id}'
    _ssh(f'mkdir -p {remote_dir}')
    _rsync(job_dir + '/', f'javedham@puhti.csc.fi:{remote_dir}/')

    # Build sbatch command
    gpu_flag = PARTITION_GPU_MAP[partition]
    env_flag = f'APPTAINER_GPU_FLAG={"--nv" if gpu_flag else ""}'
    cmd = (
        f'sbatch'
        f' --partition={partition}'
        f' --cpus-per-task={cpus}'
        f' --mem={memory_gb}G'
        f' {"" + gpu_flag if gpu_flag else ""}'
        f' --export=ALL,{env_flag},SLURM_JOB_ID_HINT={job_id}'
        f' {SLURM_SH}'
    )
    r = _ssh(cmd)
    if r.returncode != 0:
        raise HTTPException(500, f'sbatch failed: {r.stderr.strip()}')

    slurm_id = r.stdout.strip().split()[-1]

    # Persist job record (reuse existing db)
    # await db.insert_run(job_id, slurm_id, partition, user=...)

    return {'job_id': job_id, 'slurm_id': slurm_id, 'status': 'queued'}


# ── helpers ──────────────────────────────────────────────────────────────────

SSH_KEY = '/home/hbv/.ssh/id_puhti'

def _ssh(cmd: str):
    return subprocess.run(
        ['ssh', '-i', SSH_KEY,
         '-o', 'StrictHostKeyChecking=no',
         '-o', 'BatchMode=yes',
         'javedham@puhti.csc.fi', cmd],
        capture_output=True, text=True, timeout=30,
    )

def _rsync(src: str, dst: str):
    subprocess.run([
        'rsync', '-az',
        '-e', f'ssh -i {SSH_KEY} -o StrictHostKeyChecking=no -o BatchMode=yes',
        src, dst,
    ], check=True, timeout=300)

async def _save(upload: UploadFile, path: str):
    with open(path, 'wb') as f:
        shutil.copyfileobj(upload.file, f)
