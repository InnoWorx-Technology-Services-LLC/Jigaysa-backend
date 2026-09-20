#!/usr/bin/env bash
#
# Run one of this project's scheduled management commands and append its
# output, timestamped, to logs/<command>.log.
#
#     scripts/run_job.sh publish_due_campaigns
#     scripts/run_job.sh generate_trainer_payouts --dry-run
#
# Why a wrapper rather than four cron lines calling manage.py directly:
#
#   * cron runs with a near-empty environment and / as the working directory,
#     so the interpreter and project path have to be absolute — spelling them
#     out four times is four places for a venv rebuild to break the schedule.
#   * the previous logs had no timestamps at all, which made them useless for
#     answering "when did this last run", the only question anyone asks of them.
#   * ``flock`` stops a slow run from stacking. The database is remote, so a
#     five-minute job occasionally takes longer than five minutes, and cron
#     will happily start the next one on top of it.
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$HERE/ENV/bin/python"
LOG_DIR="$HERE/logs"
MAX_BYTES=$((5 * 1024 * 1024))

if [ $# -lt 1 ]; then
    echo "usage: $(basename "$0") <management-command> [args...]" >&2
    exit 2
fi

COMMAND="$1"
shift

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/${COMMAND}.log"
LOCK="$LOG_DIR/${COMMAND}.lock"

# Keep one generation. Without this the files grow without bound — the old
# expire_unpaid_bookings.log reached 1.5 MB of the same "nothing to do" line.
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt "$MAX_BYTES" ]; then
    mv -f "$LOG" "${LOG}.1"
fi

exec 9>"$LOCK"
if ! flock -n 9; then
    echo "[$(TZ="${JOB_TZ:-Asia/Kolkata}" date '+%Y-%m-%d %H:%M:%S %Z')] ${COMMAND}: previous run still going, skipped." >>"$LOG"
    exit 0
fi

# Stamp in the same clock as CRON_TZ and Django's TIME_ZONE. A log in UTC
# beside a schedule in IST is how "it didn't run" gets diagnosed twice.
stamp() { TZ="${JOB_TZ:-Asia/Kolkata}" date '+%Y-%m-%d %H:%M:%S %Z'; }

{
    echo "[$(stamp)] ── ${COMMAND} ${*} start"
    "$PYTHON" "$HERE/manage.py" "$COMMAND" "$@" 2>&1 | sed 's/^/    /'
    rc=${PIPESTATUS[0]}
    echo "[$(stamp)] ── ${COMMAND} end (exit ${rc})"
} >>"$LOG"

exit "${rc:-0}"
