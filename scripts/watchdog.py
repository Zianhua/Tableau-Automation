"""Watchdog: mails ALERT_TO when a scheduled run is stuck or dead, without waiting for the 17:30 summary.

Run it every 15 or 30 minutes from Task Scheduler. It reads only the last-run files in out/lastrun and one query on the dev DWH
(never Tableau), so it works when Tableau or the PAT is the problem. It reports, at most once per Singapore day per script and problem:
  run_stuck       out/lastrun/<script>.running is older than the script's task time limit + 3 min: the run was killed by Task Scheduler,
                  the host restarted, or the process crashed before it could report
  script_stopped  between 08:45 and 17:15 a repeating script (t11, t4, t6) has no execute run for 45 min, or t2 has not run by 09:00
  refresh_stuck   a Tableau_RefreshJob row has been 'Submitted' for more than 45 min
Failed runs are already mailed by the scripts themselves. Without --execute it prints what it found and sends nothing.

Usage:
  python scripts/watchdog.py                      # print findings
  python scripts/watchdog.py --execute            # mail them (local test sink unless MAIL_TRANSPORT=smtp / --smtp)
Exit code 0 when nothing is wrong, 1 when something was found or the watchdog itself failed.
"""
import argparse
import datetime as dt
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ops  # noqa: E402
import t4_refresh as t4  # noqa: E402
import t6_send as t6  # noqa: E402

log = logging.getLogger("watchdog")
LIMIT_MIN = {"t2": 30, "t11": 10, "t4": 40, "t6": 55}   # = ExecutionTimeLimit in scripts/schedule/register_tasks.ps1
REPEATING = ("t11", "t4", "t6")
GRACE_MIN = 3
STOPPED_AFTER_MIN = 45
WINDOW = (dt.time(8, 45), dt.time(17, 15))
T2_BY = dt.time(9, 0)
REFRESH_STUCK_MIN = 45


def _since(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=None)


def file_findings(now, env=os.environ):
    """[(script, code, text)] from the last-run files alone."""
    out = []
    running = {}
    for name, limit in LIMIT_MIN.items():
        r = ops.read_running(name, env)
        if not r or r.get("mode") != "execute":
            continue
        age = (now - _since(r["since"])).total_seconds() / 60
        running[name] = age
        if age > limit + GRACE_MIN:
            out.append((name, "run_stuck", "started %s and never finished (%d min, its limit is %d). It was killed by Task Scheduler, the host "
                        "restarted, or it crashed. Check the log in out/ and Task Scheduler; a send claim or refresh row it left behind is "
                        "cleaned up by the next run." % (r["since"][11:16], age, limit)))
    today = now.date().isoformat()
    for name in ("t2",) + REPEATING:
        mk = ops.read_marker(name, env)
        ex = ((mk or {}).get("days", {}).get(today, {})).get("execute")
        last = _since(ex["last_ts"]) if ex and ex.get("last_ts") else None
        if name == "t2":
            if now.time() >= T2_BY and last is None and name not in running:
                out.append((name, "script_stopped", "the daily mapping sync has not run today (expected 07:50). Task disabled, host down, or the "
                            "service account password expired?"))
            continue
        if not (WINDOW[0] <= now.time() <= WINDOW[1]):
            continue
        if name in running and running[name] <= LIMIT_MIN[name]:
            continue   # a long run in progress is not a stopped script
        if last is None or (now - last).total_seconds() / 60 > STOPPED_AFTER_MIN:
            out.append((name, "script_stopped", "no run for %s (it should start every 15 min). Task disabled, host down, or the service account "
                        "password expired?" % ("all day" if last is None else "%d min" % ((now - last).total_seconds() / 60))))
    return out


def refresh_findings(cur):
    rows = cur.execute(
        "SELECT COUNT(*), MIN(CAST(SWITCHOFFSET(SubmittedAt, '+08:00') AS time(0))) FROM Integration.Tableau_RefreshJob "
        "WHERE Status = 'Submitted' AND SubmittedAt < DATEADD(minute, -?, SYSDATETIMEOFFSET())", REFRESH_STUCK_MIN).fetchone()
    if rows[0]:
        return [("t4", "refresh_stuck", "%d extract refresh row(s) have been 'Submitted' for over %d min (oldest since %s). Tableau may be stuck "
                 "or the refresh job lost; t4 keeps retrying but nothing downstream is sent until it resolves." % (rows[0], REFRESH_STUCK_MIN, rows[1]))]
    return []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="send the alert mail")
    ap.add_argument("--smtp", "--ses", dest="ses", action="store_true")
    ap.add_argument("--local-smtp", action="store_true", help="force the local test sink even if MAIL_TRANSPORT=smtp")
    ap.add_argument("--local-host", default="127.0.0.1")
    ap.add_argument("--local-port", type=int, default=1025)
    a = ap.parse_args(argv)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    log.setLevel(logging.INFO)
    if not log.handlers:
        for h in (logging.StreamHandler(sys.stdout), ops.log_file_handler("watchdog")):
            h.setFormatter(fmt)
            log.addHandler(h)
            ops.log.addHandler(h)
            ops.log.setLevel(logging.INFO)
    now = dt.datetime.now(ops.SGT).replace(tzinfo=None)
    found = file_findings(now)
    failed = False
    try:
        conn = t4.connect()
        found += refresh_findings(conn.cursor())
    except (Exception, SystemExit) as e:   # the file checks above still count
        failed = True
        log.error("watchdog could not read the DWH (%s)", t6.sanitize(e, 160))
    for name, code, text in found:
        log.error("WATCHDOG %s %s: %s", name, code, text)
    if not found:
        log.info("RUN SUMMARY watchdog nothing wrong")
        return 1 if failed else 0
    if a.execute:
        try:
            cfg = ops.alert_config(a.ses, os.environ, "watchdog", execute=True, local_smtp=a.local_smtp)
            ops.check_alert_recipient(cfg, "watchdog")
            mailer = t6.make_mailer(cfg, a.local_host, a.local_port)
            t6.deliver_alerts(cfg, mailer, ops.sg_now().date(), [{"sub": n, "wb": "(whole job)", "code": c, "text": t} for n, c, t in found],
                              intro="The Tableau subscription automation has a stuck or stopped job:", footer=False,
                              subject="Tableau subscription automation: %d stuck or stopped job(s) (%s)")
        except Exception as e:
            log.error("watchdog alert could not be sent (%s)", t6.sanitize(e, 160))
    ops.write_marker("watchdog", "execute" if a.execute else "dry-run", 1, "RUN SUMMARY watchdog findings=%d" % len(found))
    return 1


if __name__ == "__main__":
    sys.exit(main())
