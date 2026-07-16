#!/bin/bash
#SBATCH --job-name=puhti_run
#SBATCH --account=project_2014823
#SBATCH --ntasks=1
# --time is passed at submit time via sbatch --time flag
# --partition / --cpus-per-task / --mem / --gres passed at submit time

export MODULEPATH=/appl/modulefiles:$MODULEPATH
module load python-data 2>/dev/null || true

mkdir -p "${JOB_DIR}/output"
mkdir -p "${JOB_DIR}/.packages"
mkdir -p "${JOB_DIR}/.mplconfig"

# Redirect stdout/stderr into the job dir so they rsync back with results
exec > "${JOB_DIR}/stdout.txt" 2> "${JOB_DIR}/stderr.txt"

echo "[run] slurm_job=${SLURM_JOB_ID} host=$(hostname) started=$(date)"
echo "[run] VENV_PATH=${VENV_PATH}"
echo "[run] USE_GPU=${USE_GPU}"
echo "[run] JOB_DIR=${JOB_DIR}"
echo "[run] USERDATA_PATH=${USERDATA_PATH}"

PYTHON="${VENV_PATH}/bin/python"
PIP="${VENV_PATH}/bin/pip"

if [ ! -f "$PYTHON" ]; then
    echo "[run] ERROR: venv not found at ${VENV_PATH}" >&2
    exit 1
fi

echo "[run] python=$($PYTHON --version 2>&1)"

# Install user dependencies into job-local dir on scratch (avoids home dir space limits)
if [ -f "${JOB_DIR}/requirements.txt" ]; then
    echo "[run] installing job requirements..."
    SHARED_CACHE=/scratch/project_2014823/pip-cache
    mkdir -p "${SHARED_CACHE}"
    "$PIP" install --quiet \
        --cache-dir "${SHARED_CACHE}" \
        -r "${JOB_DIR}/requirements.txt" \
        --target "${JOB_DIR}/.packages" 2>&1 || true
fi

echo "[run] running script.py..."
cd "${JOB_DIR}"

PYTHONPATH="${JOB_DIR}/.packages${PYTHONPATH:+:$PYTHONPATH}" \
MPLCONFIGDIR="${JOB_DIR}/.mplconfig" \
MPLBACKEND="Agg" \
XDG_CACHE_HOME="${JOB_DIR}/.cache" \
XDG_CONFIG_HOME="${JOB_DIR}/.config" \
HOME="${JOB_DIR}" \
    "$PYTHON" "${JOB_DIR}/script.py"

EXIT_CODE=$?
echo "[run] finished exit=${EXIT_CODE} at $(date)"
exit ${EXIT_CODE}
