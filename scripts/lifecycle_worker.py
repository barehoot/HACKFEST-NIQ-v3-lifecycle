"""
lifecycle_worker.py

The background half of the employee-lifecycle module. Each pass:

  1. starts probation tracking for new joiners (from HR hire dates)
  2. recalculates every status snapshot older than --stale minutes, in chunks,
     which also applies the escalation rules (manager, then business leader)
  3. reminds managers about probation check-ins due within 3 days
  4. nudges employees with overdue development actions (at most weekly)
  5. refreshes the internal job postings from SmartRecruiters (at most every 30 minutes),
     so no page load ever waits on the job portal
  6. retries Agent 4 evidence reports for simulations submitted while the AI service
     was unavailable, so the manager's review never waits on a person retrying

Every reminder is de-duplicated: the same one reaches a person at most once a
week. Writes during the day already refresh the people they touch, so a pass
mostly catches time-based changes (dates passing) and new hires.

    python scripts/lifecycle_worker.py --once           # one pass: cron, a Container Apps job, a Kubernetes CronJob
    python scripts/lifecycle_worker.py --interval 300   # a pass every 5 minutes until stopped (start.py runs this)

In production the same entry point runs as a scheduled job next to the web
containers (see deploy/README.md); several instances are safe because every
write is idempotent.
"""

import argparse
import logging
import signal
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "Talent360i_phase11C_fixed_remote_hybrid"))

from services import api_client, engagement, lifecycle_store as store, performance, probation, smartrecruiters  # noqa: E402

log = logging.getLogger("talent360i.worker")
REMIND_DAYS = 3
_stop = False


def _week_ago() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")


def run_pass(stale_minutes: int, limit: int) -> dict:
    today = date.today()
    started = time.perf_counter()
    new_joiners = probation.ensure_for_new_joiners(today)
    refreshed = performance.refresh_missing(today, limit) + performance.refresh_stale(today, stale_minutes, limit)

    reminders = 0
    for k in store.checkpoints_due((today + timedelta(days=REMIND_DAYS)).isoformat()):
        link = f"/people/{k['user_id']}#probation"
        if k["manager_id"] and not store.notified_since(k["manager_id"], "probation_reminder", link, _week_ago()):
            due = "overdue" if k["due_date"] < today.isoformat() else f"due {k['due_date']}"
            engagement.notify([k["manager_id"]], "probation_reminder", f"{k['full_name']}: {k['label']} {due}",
                              "Record the check-in so the probation decision rests on documented facts.", link)
            reminders += 1
    nudges = 0
    for user_id, count in store.overdue_action_counts(today.isoformat()):
        link = "/employee/performance#development"
        if not store.notified_since(user_id, "development_overdue", link, _week_ago()):
            engagement.notify([user_id], "development_overdue", f"{count} development action(s) past their due date",
                              "Update them, or agree new dates with your manager.", link)
            nudges += 1
    postings = smartrecruiters.sync()  # no-op while the cached copy is fresh
    evidence = api_client.retry_pending_evidence(limit=3)
    return {"new_joiners": new_joiners, "refreshed": refreshed, "probation_reminders": reminders,
            "action_nudges": nudges, "postings": postings["count"], "postings_error": postings["error"],
            "evidence_reports": evidence,
            "seconds": round(time.perf_counter() - started, 1)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Talent 360i lifecycle worker")
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    parser.add_argument("--interval", type=int, default=300, help="seconds between passes")
    parser.add_argument("--stale", type=int, default=60, help="recalculate snapshots older than this many minutes")
    parser.add_argument("--limit", type=int, default=50_000, help="most employees recalculated per pass")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s worker %(message)s")

    def stop(*_):
        global _stop
        _stop = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        try:
            log.info("pass %s", run_pass(args.stale, args.limit))
        except Exception:  # keep the loop alive; the next pass retries
            log.exception("pass failed")
        if args.once or _stop:
            break
        for _ in range(args.interval):
            if _stop:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
