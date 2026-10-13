"""
benchmark_scale.py

Measures the employee-lifecycle module on the scale dataset
(scripts/generate_scale_dataset.py), at two levels:

  service  full status refresh of every employee (the worker's nightly job),
           one employee's live status (what every write triggers), a manager's
           team cards, directory queries over the snapshot (first page, band and
           signal filters, search, the last page), the leader overview, the
           probation tracker
  HTTP     real requests through the portal (session, RBAC, rendering): the
           leader's people directory and performance overview, a 360 profile,
           the quick-view card, a manager's dashboard

Single process, single SQLite file, one laptop: a conservative baseline for the
production stack in the Production-Scale Solution document.

    python scripts/benchmark_scale.py --db db/scale_20k.sqlite
"""

import argparse
import json
import os
import platform
import random
import re
import statistics
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORTAL = ROOT / "Talent360i_phase11C_fixed_remote_hybrid"


def timings(fn, runs: int) -> dict:
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    return {"runs": runs, "p50_ms": round(statistics.median(samples), 1),
            "p95_ms": round(samples[max(0, int(len(samples) * 0.95) - 1)], 1), "max_ms": round(samples[-1], 1)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "db" / "scale_20k.sqlite")
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "scale_benchmark.json")
    args = parser.parse_args()
    if not args.db.exists():
        raise SystemExit(f"{args.db} not found -- run scripts/generate_scale_dataset.py first.")

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    os.environ.update({"TALENT360_DB_PATH": str(args.db.resolve()), "TALENT360_DEBUG": "true",
                       "TALENT360_API_MODE": "remote", "TALENT360_BACKEND_URL": "http://127.0.0.1:9",
                       "TALENT360_SESSION_SECRET": os.getenv("TALENT360_SESSION_SECRET") or "benchmark-session-secret-0123456789"})
    os.environ.setdefault("TALENT360_LOG_DIR", str(ROOT / "logs" / "benchmark"))
    sys.path.insert(0, str(PORTAL))
    from services import db, lifecycle_store as store, performance, probation

    rng = random.Random(7)
    today = date.today()
    employees = store.employee_ids()
    managers = store.user_ids_with_role("manager")
    results: dict = {"dataset": {"employees": len(employees), "managers": len(managers), "db_mb": round(args.db.stat().st_size / 1e6, 1)},
                     "machine": {"python": platform.python_version(), "platform": platform.platform(), "cpu": platform.processor() or platform.machine()},
                     "service": {}, "http": {}}

    print(f"Dataset: {len(employees):,} employees, {len(managers):,} managers ({results['dataset']['db_mb']} MB)")
    started = time.perf_counter()
    performance.refresh_all(today)
    seconds = time.perf_counter() - started
    results["service"]["full_refresh"] = {"seconds": round(seconds, 1), "employees_per_second": round(len(employees) / seconds)}
    print(f"Full status refresh: {seconds:.1f}s ({len(employees) / seconds:,.0f} employees/s)")

    bands: dict[str, int] = {}
    for row in store.status_counts({}, "band"):
        bands[row["band"]] = bands.get(row["band"], 0) + row["n"]
    results["dataset"]["bands"] = bands
    results["dataset"]["escalations"] = store.escalation_counts()

    sample = rng.sample(employees, k=min(args.runs, len(employees)))
    it = iter(sample * 2)
    results["service"]["one_employee_status"] = timings(lambda: performance.status_for(next(it), today), len(sample))
    mit = iter(rng.sample(managers, k=min(args.runs, len(managers))) * 2)
    results["service"]["manager_team_cards"] = timings(lambda: performance.team_cards(next(mit), today), min(args.runs, len(managers)))
    queries = {"directory_first_page": {}, "directory_band_filter": {"band": "Off track"},
               "directory_signal_filter": {"signal": "performance_concern"}, "directory_search": {"q": "Priya"},
               "directory_department": {"dept_id": "DEPT-AP-1"}}
    for name, filters in queries.items():
        results["service"][name] = timings(lambda f=filters: performance.directory(f, 1), args.runs)
    last_page = -(-len(employees) // 24)
    results["service"]["directory_last_page"] = timings(lambda: performance.directory({}, last_page), args.runs)
    results["service"]["leader_overview"] = timings(lambda: performance.org_overview({}), max(5, args.runs // 3))
    results["service"]["probation_tracker"] = timings(lambda: probation.pipeline(None, today), max(5, args.runs // 3))

    # HTTP through the portal: session, RBAC, rendering.
    import importlib.util
    from fastapi.testclient import TestClient
    spec = importlib.util.spec_from_file_location("portal_app", PORTAL / "app.py")
    app_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_module)
    db.DB_PATH = args.db.resolve()
    csrf = re.compile(r'name="csrf_token" value="([^"]+)"')

    def login(username):
        client = TestClient(app_module.app, follow_redirects=False)
        token = csrf.search(client.get("/login").text).group(1)
        r = client.post("/login", data={"username": username, "password": os.getenv("TALENT360_DEMO_PASSWORD", ""), "csrf_token": token})
        if r.status_code != 303 or r.headers.get("location") == "/login":
            raise SystemExit(f"Could not log in as {username}: set TALENT360_DEMO_PASSWORD to the demo password.")
        return client

    leader, manager = login("leader"), login("manager")
    people = iter(rng.sample(employees, k=args.runs * 2))
    pages = {
        "leader_people_directory": (leader, lambda: "/people"),
        "leader_people_filtered": (leader, lambda: "/people?band=At+risk&sort=score_asc"),
        "leader_performance_overview": (leader, lambda: "/leader/performance"),
        "leader_360_profile": (leader, lambda: f"/people/{next(people)}"),
        "manager_dashboard": (manager, lambda: "/manager/dashboard"),
    }
    for name, (client, url) in pages.items():
        def call(c=client, u=url):
            r = c.get(u())
            assert r.status_code == 200, (r.status_code, r.headers.get("location"))
        results["http"][name] = timings(call, max(5, args.runs // 2))
    results["http"]["quick_view_card"] = timings(
        lambda: leader.get(f"/people/{next(people)}/card", headers={"X-Requested-With": "fetch"}), max(5, args.runs // 2))

    print(f"\n{'Measure':34} {'p50 ms':>9} {'p95 ms':>9}")
    for level in ("service", "http"):
        for name, row in results[level].items():
            if "p50_ms" in row:
                print(f"{level + ': ' + name:34} {row['p50_ms']:>9} {row['p95_ms']:>9}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    results["measured_on"] = today.isoformat()
    args.out.write_text(json.dumps(results, indent=2))
    print(f"\nBands: {bands}\nWritten to {args.out}")


if __name__ == "__main__":
    main()
