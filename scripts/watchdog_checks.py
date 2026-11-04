"""Offline checks for scripts/watchdog.py (no DWH, no Tableau, no mail). Run: python scripts/watchdog_checks.py"""
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ops  # noqa: E402
import watchdog as w  # noqa: E402

fails = 0


def check(name, ok):
    global fails
    fails += 0 if ok else 1
    print("%s  %s" % ("PASS" if ok else "FAIL", name))


def codes(found):
    return sorted((n, c) for n, c, _ in found)


with tempfile.TemporaryDirectory() as d:
    env = {"LASTRUN_DIR": d}
    day = dt.datetime(2026, 10, 27, 12, 0)

    def marker(script, last):
        ops.write_marker(script, "execute", 0, "ok", env=env, now=last)

    for s in ("t2", "t11", "t4", "t6"):
        marker(s, dt.datetime(2026, 10, 27, 11, 50))
    check("all scripts ran 10 min ago: nothing found", w.file_findings(day, env) == [])

    check("t4 silent for 60 min: script_stopped", codes(w.file_findings(day.replace(hour=13), env)) ==
          [("t11", "script_stopped"), ("t4", "script_stopped"), ("t6", "script_stopped")])
    check("outside the window nothing is reported as stopped", w.file_findings(day.replace(hour=20), env) == [])

    marker("t4", dt.datetime(2026, 10, 27, 11, 55))
    ops.mark_started("t6", "execute", env)
    run = ops.read_running("t6", env)
    check("start marker written", run is not None and run["mode"] == "execute")
    ops.write_marker("t6", "execute", 0, "ok", env=env, now=dt.datetime(2026, 10, 27, 11, 59))
    check("end marker removes it", ops.read_running("t6", env) is None)

    (Path(d) / "t6.running").write_text(json.dumps({"script": "t6", "mode": "execute", "pid": 1, "since": "2026-10-27T10:00:00"}))
    found = w.file_findings(day, env)
    check("t6 running since 10:00 (limit 55): run_stuck", ("t6", "run_stuck") in codes(found))
    marker("t6", dt.datetime(2026, 10, 27, 11, 0))
    (Path(d) / "t6.running").write_text(json.dumps({"script": "t6", "mode": "execute", "pid": 1, "since": "2026-10-27T11:30:00"}))
    check("t6 running 30 min: a long run in progress is not reported", w.file_findings(day, env) == [])
    (Path(d) / "t6.running").write_text(json.dumps({"script": "t6", "mode": "dry-run", "pid": 1, "since": "2026-10-27T08:00:00"}))
    check("a dry-run leftover is ignored", ("t6", "run_stuck") not in codes(w.file_findings(day, env)))

with tempfile.TemporaryDirectory() as d:
    env = {"LASTRUN_DIR": d}
    check("t2 has not run by 09:00: script_stopped", ("t2", "script_stopped") in codes(w.file_findings(dt.datetime(2026, 10, 27, 9, 5), env)))
    check("t2 before 09:00: quiet", ("t2", "script_stopped") not in codes(w.file_findings(dt.datetime(2026, 10, 27, 8, 50), env)))

print("ALL CHECKS PASSED" if not fails else "%d CHECK(S) FAILED" % fails)
sys.exit(1 if fails else 0)
