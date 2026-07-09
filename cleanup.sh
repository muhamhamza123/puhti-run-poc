#!/bin/bash
# Cleanup old job dirs on Puhti scratch and head node NFS.
# Run daily via cron as the hbv user.

SSH_KEY="${PUHTI_SSH_KEY:-/home/hbv/.ssh/id_puhti}"
PUHTI_USER="${PUHTI_USER:-javedham}"
PUHTI_HOST="${PUHTI_HOST:-puhti.csc.fi}"
PUHTI_RUNS="${PUHTI_RUNS:-/scratch/project_2014823/runs}"
NFS_RUNS="${RUNS_ROOT:-/data/hbv/runs}"

PUHTI_DAYS=7    # delete Puhti job dirs older than this
NFS_DAYS=30     # delete NFS job dirs older than this

echo "[cleanup] started at $(date)"

# ── Puhti scratch ─────────────────────────────────────────────────────────────
echo "[cleanup] pruning Puhti scratch dirs older than ${PUHTI_DAYS} days..."
ssh -i "$SSH_KEY" \
    -o StrictHostKeyChecking=no \
    -o BatchMode=yes \
    -o ConnectTimeout=15 \
    "${PUHTI_USER}@${PUHTI_HOST}" \
    "find ${PUHTI_RUNS} -mindepth 2 -maxdepth 2 -type d -mtime +${PUHTI_DAYS} -print -exec rm -rf {} + 2>/dev/null; echo done"

if [ $? -ne 0 ]; then
    echo "[cleanup] WARNING: Puhti SSH failed — skipping scratch cleanup"
fi

# ── Head node NFS ─────────────────────────────────────────────────────────────
echo "[cleanup] pruning NFS job dirs older than ${NFS_DAYS} days..."
find "${NFS_RUNS}" -mindepth 1 -maxdepth 1 -type d -mtime "+${NFS_DAYS}" -print -exec rm -rf {} +

# ── SQLite DB row pruning ──────────────────────────────────────────────────────
DB_PATH="${RUN_DB_PATH:-/data/hbv/runs/runs.db}"
DB_KEEP_DAYS=90
if [ -f "$DB_PATH" ]; then
    echo "[cleanup] pruning DB rows older than ${DB_KEEP_DAYS} days..."
    sqlite3 "$DB_PATH" "DELETE FROM runs WHERE created < date('now', '-${DB_KEEP_DAYS} days');"
    sqlite3 "$DB_PATH" "VACUUM;"
    echo "[cleanup] DB rows deleted: $?"
else
    echo "[cleanup] WARNING: DB not found at $DB_PATH — skipping row pruning"
fi

echo "[cleanup] done at $(date)"
