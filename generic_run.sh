#!/bin/bash
#SBATCH --job-name=puhti_run
#SBATCH --account=project_2014823
#SBATCH --ntasks=1
#SBATCH --time=02:00:00
# --partition / --cpus-per-task / --mem / --gres passed at submit time

export MODULEPATH=/appl/modulefiles:$MODULEPATH
module load apptainer 2>/dev/null || true

SIF=/scratch/project_2014823/runs/general-compute.sif

mkdir -p "${JOB_DIR}/output"
mkdir -p "${JOB_DIR}/.packages"
mkdir -p "${JOB_DIR}/.mplconfig"

# Redirect stdout/stderr into the job dir so they rsync back with results
exec > "${JOB_DIR}/stdout.txt" 2> "${JOB_DIR}/stderr.txt"

echo "[run] slurm_job=${SLURM_JOB_ID} host=$(hostname) started=$(date)"

# Install user dependencies into job-local dir on scratch (avoids home dir space limits)
if [ -f "${JOB_DIR}/requirements.txt" ]; then
    echo "[run] installing dependencies..."
    apptainer exec \
        --bind /scratch:/scratch \
        --env PIP_CACHE_DIR="${JOB_DIR}/.pip-cache" \
        "${SIF}" \
        pip install --quiet \
            --no-cache-dir \
            -r "${JOB_DIR}/requirements.txt" \
            --target "${JOB_DIR}/.packages"
fi

echo "[run] running script.py..."
cd "${JOB_DIR}"
apptainer exec \
    --bind /scratch:/scratch \
    ${GPU_FLAG:-} \
    --env PYTHONPATH="${JOB_DIR}/.packages" \
    --env MPLCONFIGDIR="${JOB_DIR}/.mplconfig" \
    --env MPLBACKEND="Agg" \
    "${SIF}" \
    python "${JOB_DIR}/script.py"

EXIT_CODE=$?
echo "[run] finished exit=${EXIT_CODE} at $(date)"
exit ${EXIT_CODE}
