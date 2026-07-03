#!/bin/bash
#SBATCH --job-name=puhti_run
#SBATCH --account=project_2014823
#SBATCH --ntasks=1
#SBATCH --time=02:00:00
#SBATCH --output=/scratch/project_2014823/runs/%j/stdout.txt
#SBATCH --error=/scratch/project_2014823/runs/%j/stderr.txt

# Partition and CPUs/GPUs are passed via --partition / --gres at submit time

export MODULEPATH=/appl/modulefiles:$MODULEPATH
module load apptainer 2>/dev/null || true

SIF=/scratch/project_2014823/hbv/hbv-compute.sif
JOB_DIR=/scratch/project_2014823/runs/${SLURM_JOB_ID}

echo "[run] job=${SLURM_JOB_ID} started on $(hostname) at $(date)"

# Install user dependencies if requirements.txt present
if [ -f "${JOB_DIR}/requirements.txt" ]; then
    echo "[run] installing dependencies..."
    apptainer exec --bind /scratch:/scratch "${SIF}" \
        pip install --quiet -r "${JOB_DIR}/requirements.txt" \
        --target "${JOB_DIR}/.packages"
    export PYTHONPATH="${JOB_DIR}/.packages:${PYTHONPATH}"
fi

# Run user script — all outputs should be written to ./output/
mkdir -p "${JOB_DIR}/output"
cd "${JOB_DIR}"

echo "[run] running script.py..."
apptainer exec \
    --bind /scratch:/scratch \
    ${APPTAINER_GPU_FLAG} \
    "${SIF}" \
    python "${JOB_DIR}/script.py"

EXIT_CODE=$?
echo "[run] finished (exit ${EXIT_CODE}) at $(date)"
exit ${EXIT_CODE}
