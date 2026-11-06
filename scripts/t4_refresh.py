"""T4 + T5: gated extract refresh. Refresh a published extract only when ALL required pipelines of ALL its tables
succeeded today, wait for the Tableau job, log the outcome when it finishes.

Scope (D-035, --scope / env T4_REFRESH_SCOPE): during the pilot the default 'takeover' refreshes ONLY extracts behind workbooks that
have a subscription taken over by t11_pause.py (Tableau_SubscriptionBackup TakenOver = 1, not restored); an empty takeover set
means nothing is refreshed. 'all' (set after the pilot) includes every workbook with an active subscription.
Refresh set (D-006, D-046): distinct published extracts (ExtractRefreshTargetId, RefreshKind = Extract only) behind
workbooks that are Ready in vw_Tableau_WorkbookReadiness AND have an active subscription (suspended or not: D-046, a
subscription that must not be sent is deleted in Tableau by the user).

Readiness of a target (pipeline level, D-016: "today" = Singapore date):
  every required pipeline of every table behind the target has, as its LATEST Integration.ETL_Master_Log row today,
  Success = 1. Unknown pipeline, unmapped table, no required pipeline, or a target that is not a published extract
  on the server -> skip and log (fail closed).

ETL_Master_Log (checked read-only on dev, 2026-10-23): columns JobID, PipelineName, EndTime (datetime2, DWH local time
= Singapore, no start time, no timezone), Success (bit). Only 3 pipelines; every row found so far has Success = 1, so
failed runs may not be logged at all. A pipeline that failed without a row simply has no row today -> pending.
We take the latest row of the day per pipeline (EndTime is NOT NULL, so a failure row always has a time), so a later
Success = 0 row blocks (fail closed).
Known limit (pipeline level, PLAN_REVIEW G12): the gate only sees COMPLETED ETL rows. A rerun in progress, or a run that
crashed without writing a row, is invisible; the earlier success of the day still counts. No cut-off time is applied:
ETLs do not run after 16:30 (D-029) and the scheduler window handles the rest.
Not covered (functional gap): NotChecked sources (positively non-DWH: Excel/CSV files, other
databases, D-053) and extracts whose tables are all static have no required pipeline, so they are never refreshed by this script. In
particular an embedded Tableau EXTRACT of an Excel/CSV file is never refreshed by us: the report is sent with whatever that extract holds,
which is stale unless Tableau's own schedule refreshes it.

Tableau extract refresh schedules (D-067, D-068). The daily mapping sync stores the Tableau extract refresh tasks and their schedules;
extract_takeover.py deletes a task in Tableau on purpose (backup first) so Tableau no longer refreshes that extract by itself. For such a
TAKEN-OVER extract this script refreshes on the days its Tableau schedule says it is due (same occurrence, catch-up window and history rule
as the subscription schedules, schedule_due.py: a Succeeded or in-flight refresh job of the extract inside the window satisfies the
occurrence), once all its required pipelines succeeded (the gate below). It refreshes on those days OR on the days a dependent subscription
is due, so a report is never held for a refresh that the extract's own schedule would skip. An extract
with no taken-over task (no Tableau schedule, or Tableau still owns the task) is refreshed only for a due subscription, as before. --schedule-mode
ignore turns the schedule rule off.

Idempotency across 15-minute runs, from Integration.Tableau_RefreshJob (this script writes only there):
  - a Succeeded job counts only if it was submitted AFTER the latest end of the required pipelines' runs today; if an
    ETL finished after our last refresh was submitted, the target is refreshed again.
  - MAX_ATTEMPTS counts the day's attempts that did NOT succeed (Failed / Cancelled / TimedOut), then a human has to look. Successful
    refreshes do not count: each needs a later ETL run first, so they cannot loop.
  - a job in flight (row 'Submitted', or 'TimedOut' whose Tableau job is still running) -> skip. At start every such
    row is reconciled with the real Tableau job, so a run that was killed mid-wait, or a job that finished after we gave
    up, gets its true outcome logged. A Tableau job that cannot be read counts as possibly running for HARD_LIMIT_H.
  - a 'Submitted' row with no job id (crash between refresh() and storing the id) is NOT written off blindly: Tableau
    is searched (read-only) for a refresh job of that data source created since the row, and that job is adopted.
  - before every submit, Tableau is checked for an already queued/running refresh job of the data source (the old
    refresh job's, D-017); if there is one the target is skipped. If Tableau cannot be read, the target is skipped.
  - Failed / Cancelled / TimedOut (finished) -> retried next run, within the cap.
A 'Submitted' row is inserted BEFORE the submit (it is the in-flight marker), updated with the Tableau job id right
after, and finished (Succeeded / Failed / Cancelled / TimedOut + FinishedAt) when the job ends. Success is never
logged at submit time. Only finish code 0 is success; any other code is Failed.

Usage:
  python scripts/t4_refresh.py                 # dry run (default): reads DWH + Tableau, writes nothing
  python scripts/t4_refresh.py --execute       # REAL run: submits refreshes to Tableau, writes Tableau_RefreshJob
Dry run only: --date YYYY-MM-DD evaluates readiness as if that were today (for inspection).
Reads TABLEAU_PAT_NAME / TABLEAU_PAT_SECRET from .env (a PAT of its own, D-017). Only one script may use it at a time.
Exit code 1 if any refresh failed or timed out, if sign-in fails, or if every target whose ETL gate was open was
skipped because Tableau could not be read. Each run ends with one 'RUN SUMMARY ...' log line.
"""
import argparse
import datetime as dt
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pyodbc
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
import schedule_due as sd  # noqa: E402
import ops  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Dev DWH guard: the only server/database this script may touch. From .env; unset = refuse to run (fail closed).
DEV_SERVER = (os.environ.get("DEV_DWH_SERVER") or "").strip().lower()
DEV_DB = (os.environ.get("DEV_DWH_DATABASE") or "").strip().lower()
SGT = dt.timezone(dt.timedelta(hours=8))
LOCK_NAME = "T4_refresh"
TIMEOUT_MIN = 40        # total wait per run; below the 1 h Task Scheduler limit (D-029)
POLL_SECONDS = 20
MAX_ATTEMPTS = 3        # unsuccessful attempts per target per day (Succeeded rows do not count), then leave it to a human
SUBMIT_UNKNOWN_MIN = 10  # a 'Submitted' row with no Tableau job id older than this is written off as Failed
SUCCESS_CODES = (0,)     # REST finishCode 0 = success; 1 error, 2 cancelled; anything else is Failed (fail closed)
HARD_LIMIT_H = 3         # a Tableau job we cannot read is assumed possibly running until its row is this old
MAX_CONCURRENT_REFRESHES = 1   # R1: refresh jobs of THIS script in flight at once (Tableau server load); env MAX_CONCURRENT_REFRESHES
T4_MAX_LOCK_MIN = 10           # R1 review: t4 holds the shared PAT lock at most this long per run (env T4_MAX_LOCK_MIN); the rest waits for the next run
JOBS_TTL_S = 60                # R1 review: the 24 h background-job list is fetched once and reused for this long
REFRESH_STAGGER_SEC = 60       # R1: minimum seconds between two job starts when more than one may run; env REFRESH_STAGGER_SEC

log = logging.getLogger("t4_refresh")


# ---------- DB helpers ----------
def connect():
    server, db = os.environ["CONN_SERVER"], os.environ["CONN_DATABASE"]
    if not DEV_SERVER or not DEV_DB or server.lower() != DEV_SERVER or db.lower() != DEV_DB:
        sys.exit("Refusing to run: only the dev DWH (D-026) is allowed, got %s / %s" % (server, db))
    drv = os.environ["CONN_DRIVER"].strip("{}")
    conn = pyodbc.connect("Driver={%s};Server=%s;Database=%s;Trusted_Connection=yes;TrustServerCertificate=yes"
                          % (drv, server, db), autocommit=True)
    sname, dname = conn.cursor().execute("SELECT @@SERVERNAME, DB_NAME()").fetchone()
    if not sname.lower().startswith(DEV_SERVER.split(".")[0]) or dname.lower() != DEV_DB:
        conn.close()
        sys.exit("Refusing to run: connected to %s / %s, not the dev DWH" % (sname, dname))
    return conn


def dwh_today(cur):
    """Singapore date according to the DWH clock. ETL_Master_Log.EndTime is assumed to be DWH local = Singapore."""
    off, today = cur.execute("SELECT DATEPART(TZoffset, SYSDATETIMEOFFSET()), CAST(CAST(SYSDATETIMEOFFSET() "
                             "AT TIME ZONE 'Singapore Standard Time' AS date) AS varchar(10))").fetchone()
    if off != 480:
        sys.exit("ABORT: the DWH clock is not UTC+8 (offset %s min); ETL_Master_Log.EndTime would not be Singapore time" % off)
    return dt.date.fromisoformat(today)


@dataclass
class Target:
    id: str
    name: str
    workbooks: list = field(default_factory=list)   # [(workbook_id, name)]
    pipelines: dict = field(default_factory=dict)   # pipeline -> PipelineKnown (bool)
    via: set = field(default_factory=set)           # why it is a candidate: "due subscription" and/or "extract schedule"


TAKEN_OVER = ("EXISTS (SELECT 1 FROM Integration.Tableau_SubscriptionBackup b WHERE b.Subscription_id = s.Subscription_id "
              "AND b.TakenOver = 1 AND b.RestoredAt IS NULL)")
# Which subscriptions make a workbook's extracts worth refreshing (D-035):
#   all      = every active subscription, suspended or not (D-046). Use after the pilot.
#   takeover = active and taken over only. Default: during the pilot, workbooks whose subscriptions Tableau still sends itself
#              are not touched by the script at all (fence by takeover, not by recipient).
SCOPES = {"all": "1 = 1", "takeover": TAKEN_OVER}
CAND_TEMPLATE = """
SELECT r.ExtractRefreshTargetId, r.DataSource_id, r.DataSource_Name, r.ReadyStatus, w.Workbook_id, w.Workbook_Name,
       wr.WorkbookStatus,
       (SELECT COUNT(*) FROM Integration.Tableau_Subscription s
         WHERE s.Workbook_id = w.Workbook_id AND s.Active_Flag = 1 AND %s) AS ActiveSubs,
       (SELECT COUNT(*) FROM Integration.Tableau_Subscription s
         WHERE s.Workbook_id = w.Workbook_id AND s.Active_Flag = 1) AS Subs
FROM Integration.vw_Tableau_DataSourceReadiness r
JOIN Integration.Tableau_WorkbookDataSource w ON w.DataSource_id = r.DataSource_id AND w.Active_Flag = 1
JOIN Integration.vw_Tableau_WorkbookReadiness wr ON wr.Workbook_id = w.Workbook_id
WHERE r.RefreshKind = 'Extract'"""
CAND_SQL = CAND_TEMPLATE % SCOPES["all"]
PIPE_SQL = "SELECT DataSource_id, PipelineName, PipelineKnown FROM Integration.vw_Tableau_DataSourcePipeline"


def due_workbooks(cur, today, scope="all", catch_up=None, max_age_h=26):
    """R1 / D-036: workbook ids with at least one in-scope subscription that is DUE today (schedule day or catch-up day, not yet
    sent). Only those workbooks' extracts are worth refreshing. Unknown or unsupported schedules are not due (fail closed)."""
    eng = sd.DueEngine.load(cur, today, catch_up, max_age_h)
    out = set()
    for sid, wid, sched, paused_at, taken in cur.execute(
            "SELECT s.Subscription_id, s.Workbook_id, s.ScheduleId, CAST(SWITCHOFFSET(b.PausedAt, '+08:00') AS datetime2(0)), "
            "CASE WHEN b.Backup_id IS NULL THEN 0 ELSE 1 END FROM Integration.Tableau_Subscription s "
            "LEFT JOIN Integration.Tableau_SubscriptionBackup b ON b.Subscription_id = s.Subscription_id AND b.TakenOver = 1 "
            "AND b.RestoredAt IS NULL AND b.PausedAt IS NOT NULL AND ISNULL(b.Halted, 0) = 0 "
            "WHERE s.Active_Flag = 1 AND " + SCOPES[scope]).fetchall():
        if eng.status(sid, sched, paused_at if taken else None).status == "due":
            out.add(wid)
    return out


SCHEDULE_SQL = ("SELECT Target_id, Schedule_id, CAST(SWITCHOFFSET(NeutralisedAt, '+08:00') AS datetime2(0)) "
                "FROM Integration.vw_Tableau_ExtractSchedule WHERE TakenOver = 1")


def extract_engine(cur, today, catch_up=None, max_age_h=26):
    """DueEngine for extracts: schedules from the DWH, history = our Succeeded or in-flight refresh jobs per data source (target id)."""
    c = sd.catch_up_default() if catch_up is None else catch_up
    hist = {}
    for tid, d in cur.execute("SELECT DataSource_id, SubmittedDate FROM Integration.Tableau_RefreshJob WHERE Status IN ('Succeeded', 'Submitted') "
                              "AND SubmittedDate >= ?", (today - dt.timedelta(days=c + 3)).isoformat()).fetchall():
        hist.setdefault(tid, set()).add(d)
    return sd.DueEngine(today, sd.load_schedules(cur), hist, c, max_age_h, dt.datetime.now(SGT).replace(tzinfo=None))


def extract_due(cur, today, catch_up=None, max_age_h=26):
    """D-068: extracts whose TAKEN-OVER Tableau refresh schedule is due today. Returns (due {target id}, notes {target id: text}) where
    notes explains every taken-over extract that is not due. A refresh job of the extract that Succeeded (or is in flight) on a day inside
    the occurrence window satisfies it; occurrences before the takeover belong to Tableau's old task. Fails closed: a schedule that is
    missing, stale or not understood is not due."""
    eng = extract_engine(cur, today, catch_up, max_age_h)
    due, notes = set(), {}
    for tid, sched_id, taken_at in cur.execute(SCHEDULE_SQL).fetchall():
        st = eng.status(tid, sched_id, taken_at)
        if st.status == "due":
            due.add(tid)
        else:
            notes.setdefault(tid, []).append("%s%s" % (st.status, " (%s)" % st.note if st.note else ""))
    return due, {t: "Tableau extract schedule: " + "; ".join(v) for t, v in notes.items() if t not in due}


TAKEN_ROWS_SQL = ("SELECT Target_id, ISNULL(TargetName, Target_id), ScheduleId, CAST(SWITCHOFFSET(NeutralisedAt, '+08:00') AS datetime2(0)) "
                  "FROM Integration.Tableau_ExtractTaskBackup WHERE TakenOver = 1 AND NeutralisedAt IS NOT NULL AND RestoredAt IS NULL")


def taken_over_rows(cur):
    """D-074: the confirmed, not restored takeovers: [(target id, name, schedule id, taken over at)]."""
    return cur.execute(TAKEN_ROWS_SQL).fetchall()


def taken_over_problems(cur, today, catch_up=None, max_age_h=26):
    """D-074: [(target id, name, code, text)] for every taken-over extract (its Tableau task is deleted, so only we refresh it) that nothing
    would refresh: it is not a t4 candidate (data source left the mapping, registry row removed, UnmappedTable / UnknownPipeline, no
    pipeline), its schedule is stale / not understood (never due), or the last due occurrence has no successful refresh."""
    rows = taken_over_rows(cur)
    if not rows:
        return []
    eng = extract_engine(cur, today, catch_up, max_age_h)
    ids = {r[0] for r in rows}
    cands, excluded = load_targets(cur, "all", due_wbs=set(), sched_due=ids)   # as if every one were due today: is it a candidate at all?
    cand = {t.id: t for t in cands}
    out = []
    for tid, name, sched, taken_at in rows:
        t = cand.get(tid)
        if t is None:
            out.append((tid, name, "takeover_orphaned", "taken over (its Tableau task is deleted) but not a refresh candidate any more: %s. Nothing refreshes "
                        "it; fix the mapping or restore the task (extract_takeover.py --restore)" % excluded.get(tid, (name, "not in the mapping any more"))[1]))
            continue
        if not t.pipelines or not all(t.pipelines.values()):
            out.append((tid, name, "takeover_orphaned", "taken over but %s: t4 cannot refresh it. Fix the registry or restore the task"
                        % ("no required pipeline" if not t.pipelines else "a pipeline is unknown to the ETL log")))
            continue
        st = eng.status(tid, sched, taken_at)
        if st.status == "unsupported":
            out.append((tid, name, "takeover_schedule", "taken over but its schedule is not usable (%s: %s): it is never due, nothing refreshes it"
                        % (st.status, st.note)))
        elif st.missed:
            out.append((tid, name, "takeover_missed", "taken over and the occurrence of %s had no successful refresh in its window (ended yesterday)" % st.missed))
    return out


def load_targets(cur, scope="all", due_wbs=None, sched_due=None, sched_notes=None):
    """Returns (targets to consider, excluded {target_id: (name, reason)}). A target qualifies when at least one of its
    workbook links is Ready with an active subscription that counts for `scope` (see SCOPES) and its source row is Ready,
    or (D-068) when its taken-over Tableau extract schedule is due today (sched_due) and its source row is Ready."""
    pipes = {}
    for ds, p, known in cur.execute(PIPE_SQL).fetchall():
        pipes.setdefault(ds, {})[p] = bool(known)
    targets, seen = {}, {}
    for tid, dsid, dsname, rstatus, wid, wname, wstatus, active_subs, subs in cur.execute(CAND_TEMPLATE % SCOPES[scope]).fetchall():
        seen.setdefault(tid, {"name": dsname, "reasons": set()})
        if sched_due and tid in sched_due:
            if rstatus != "Ready":
                seen[tid]["reasons"].add("source %s" % rstatus)
            else:
                t = targets.setdefault(tid, Target(tid, dsname))
                t.via.add("extract schedule")
                if (wid, wname) not in t.workbooks:
                    t.workbooks.append((wid, wname))
                for p, known in pipes.get(dsid, {}).items():
                    t.pipelines[p] = t.pipelines.get(p, True) and known
        elif wstatus != "Ready":
            seen[tid]["reasons"].add("workbook %s" % wstatus)
        elif active_subs == 0:   # before the schedule test: due_wbs only holds in-scope subscriptions, so "not due" would be a wrong reason here
            seen[tid]["reasons"].add("workbook has no active subscription" if subs == 0
                                     else "no taken-over subscription (pilot scope)" if scope == "takeover"
                                     else "no active subscription in scope")
        elif due_wbs is not None and wid not in due_wbs:
            seen[tid]["reasons"].add("no subscription of the workbook is due today (schedule, D-036)")
        elif rstatus != "Ready":
            seen[tid]["reasons"].add("source %s" % rstatus)
        else:
            t = targets.setdefault(tid, Target(tid, dsname))
            t.via.add("due subscription")
            if (wid, wname) not in t.workbooks:
                t.workbooks.append((wid, wname))
            for p, known in pipes.get(dsid, {}).items():
                t.pipelines[p] = t.pipelines.get(p, True) and known
    for tid, v in seen.items():
        if tid not in targets and sched_notes and tid in sched_notes:
            v["reasons"].add(sched_notes[tid])
    excluded = {tid: (v["name"], "; ".join(sorted(v["reasons"]))) for tid, v in seen.items() if tid not in targets}
    return list(targets.values()), excluded


ETL_SQL = """
SELECT PipelineName, EndTime, Success FROM (
  SELECT PipelineName, EndTime, Success,
         ROW_NUMBER() OVER (PARTITION BY PipelineName ORDER BY EndTime DESC, JobID DESC) AS rn
  FROM Integration.ETL_Master_Log WHERE EndTime >= ? AND EndTime < ?) x
WHERE rn = 1"""


def etl_status_today(cur, today):
    """{pipeline: (end_time, success)}: the latest ETL_Master_Log row of the Singapore day per pipeline."""
    start = dt.datetime.combine(today, dt.time())
    return {p: (end, bool(ok)) for p, end, ok in cur.execute(ETL_SQL, start, start + dt.timedelta(days=1)).fetchall()}


# ---------- decisions (pure) ----------
def pipeline_text(p, known, etl):
    if not known:
        return "%s: UNKNOWN pipeline" % p
    if p not in etl:
        return "%s: no run today" % p
    end, ok = etl[p]
    return "%s: %s at %s" % (p, "OK" if ok else "latest run FAILED", end.strftime("%H:%M"))


def decide(t, etl, jobs):
    """Returns (action, reason). action is 'refresh' or 'skip'. jobs: this target's RefreshJob rows for today (plus any
    still-running ones), after reconcile, as dicts with Status, 'submitted' (aware datetime) and 'running' (Tableau job
    still running). Order: in flight, then the ETL gate, then freshness of our last Succeeded job, then the attempt cap."""
    if any(j["running"] for j in jobs):
        return "skip", "refresh job in flight"
    if not t.pipelines:
        return "skip", "no required pipeline (cannot gate): skipped"
    bad = [p for p, known in t.pipelines.items() if not known]
    if bad:
        return "skip", "%d unknown pipeline(s)" % len(bad)
    notyet = [p for p in t.pipelines if p not in etl]
    failed = [p for p in t.pipelines if p in etl and not etl[p][1]]
    if failed:
        return "skip", "%d pipeline(s) whose latest run today failed" % len(failed)
    if notyet:
        return "skip", "waiting for %d pipeline(s) with no run today" % len(notyet)
    # Every required pipeline's latest run today is a success. A Succeeded job counts only if it was submitted AFTER
    # the last of those ETL runs ended (EndTime is Singapore local time); otherwise the extract may be stale.
    last_etl = max(etl[p][0] for p in t.pipelines).replace(tzinfo=SGT)
    succeeded = [j for j in jobs if j["Status"] == "Succeeded"]
    if any(j["submitted"] > last_etl for j in succeeded):
        return "skip", "already refreshed today after the last required ETL"
    unsuccessful = [j for j in jobs if j["Status"] != "Succeeded"]
    if len(unsuccessful) >= MAX_ATTEMPTS:
        return "skip", "gave up after %d failed attempts today, needs a human" % len(unsuccessful)
    if succeeded:
        return "refresh", "an ETL finished at %s, after our last refresh: refreshing again" % last_etl.strftime("%H:%M")
    return "refresh", "all %d required pipeline(s) succeeded today" % len(t.pipelines)


# ---------- RefreshJob log ----------
def utc_text(d):
    """tz-aware datetime -> ISO text for CAST(? AS datetimeoffset(0))."""
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)  # Tableau returns UTC
    return d.astimezone(dt.timezone.utc).isoformat()


def finish_row(cur, row_id, status, msg, when=None):
    when = when or dt.datetime.now(dt.timezone.utc)
    cur.execute("UPDATE Integration.Tableau_RefreshJob SET Status = ?, FinishedAt = CAST(? AS datetimeoffset(0)), "
                "Message = ? WHERE RefreshJob_id = ?", status, utc_text(when), (msg or "")[:1000], row_id)


def job_outcome(job):
    """(status, message) of a COMPLETED Tableau job. Only finish code 0 is success; anything else is Failed (fail
    closed), code 2 is Cancelled."""
    if job.finish_code in SUCCESS_CODES:
        return "Succeeded", "Tableau job finished OK"
    notes = job.notes if isinstance(job.notes, str) else "; ".join(str(n) for n in (job.notes or []))
    return ("Cancelled" if job.finish_code == 2 else "Failed"), "finish code %s %s" % (job.finish_code, notes)


JOBS_SQL = """
SELECT RefreshJob_id, DataSource_id, TableauJobId, Status, CAST(SubmittedDate AS varchar(10)),
       CAST(SWITCHOFFSET(SubmittedAt, '+00:00') AS datetime2(0))
FROM Integration.Tableau_RefreshJob
WHERE (Status = 'Submitted' OR SubmittedDate = ?) AND DataSource_id LIKE ?"""


def load_jobs(cur, today, ds_like="%"):
    return [{"id": r[0], "ds": r[1], "job": r[2], "Status": r[3], "date": r[4],
             "submitted": r[5].replace(tzinfo=dt.timezone.utc), "running": False}
            for r in cur.execute(JOBS_SQL, today.isoformat(), ds_like).fetchall()]


# ---------- Tableau job lookups (read-only) ----------
_jobs_cache = {"t": None, "since": None, "items": None, "tab": None}


def clear_jobs_cache():
    _jobs_cache.update(t=None, since=None, items=None, tab=None)


def recent_refresh_jobs(tableau, since, clock=time.monotonic):
    """Background jobs created since `since` (aware datetime), all pages. BackgroundJobItem has no datasource id, only
    the title (= data source name), so callers match on the name. The list is cached for JOBS_TTL_S (R1 review #5): one listing
    per run instead of one per data source. clear_jobs_cache() at the start of a run and after every submit."""
    c = _jobs_cache
    if c["items"] is not None and c["tab"] is tableau and c["t"] is not None and clock() - c["t"] < JOBS_TTL_S and c["since"] <= since:
        try:
            return [j for j in c["items"] if j.created_at is None or j.created_at >= since]
        except TypeError:
            return list(c["items"])
    import tableauserverclient as TSC
    opts = TSC.RequestOptions(pagesize=1000)
    opts.filter.add(TSC.Filter(TSC.RequestOptions.Field.CreatedAt, TSC.RequestOptions.Operator.GreaterThanOrEqual,
                               since.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    out, page = [], 1
    while True:
        opts.pagenumber = page
        items, pag = tableau().jobs.get(req_options=opts)
        out += items
        if pag.page_number * pag.page_size >= pag.total_available or not items:
            break
        page += 1
    _jobs_cache.update(t=clock(), since=since, items=out, tab=tableau)
    return out


def refresh_jobs_for(tableau, ds_name, since):
    """Extract-refresh jobs of the data source with this name, newest first (matched on title, conservative)."""
    js = [j for j in recent_refresh_jobs(tableau, since)
          if j.title == ds_name and "refresh" in (j.type or "").lower()]
    return sorted(js, key=lambda j: j.created_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)


def active_refresh_job(tableau, ds_name):
    """A queued or running refresh job of this data source on Tableau (e.g. the old refresh job's, D-017), or None.
    'Active' = not ended. Raises if Tableau cannot be read: the caller then skips (fail closed)."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
    for j in refresh_jobs_for(tableau, ds_name, since):
        if j.ended_at is None:
            return j
    return None


BUSY_STALE_HOURS = 6   # D-057: a Pending job older than this is ignored by the "Tableau busy" check (env TABLEAU_BUSY_STALE_HOURS)


def tableau_busy(tableau, own_ids=(), env=os.environ, now=None):
    """D-057: is Tableau busy with someone else's background jobs? Returns None when the check is switched off
    (TABLEAU_BUSY_CHECK=0/off/false/no), else (queued, running, ignored): jobs of any type that are Pending / InProgress, not
    in own_ids (the job ids we recorded). A Pending job older than TABLEAU_BUSY_STALE_HOURS (default 6) is not counted (stale
    leftover) and is returned in `ignored`. Uses the cached 24 h job list; raises if Tableau cannot be read (caller fails closed)."""
    if str(env.get("TABLEAU_BUSY_CHECK", "1")).strip().lower() in ("0", "off", "false", "no"):
        return None
    stale_h = float(env.get("TABLEAU_BUSY_STALE_HOURS") or BUSY_STALE_HOURS)
    now = now or dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(hours=max(24.0, stale_h))
    own = {str(i) for i in own_ids if i}
    queued = running = ignored = 0
    for j in recent_refresh_jobs(tableau, since):
        st = str(getattr(j, "status", "") or "").lower()
        if st not in ("pending", "inprogress") or str(j.id) in own:
            continue
        if st == "inprogress":
            running += 1
        elif j.created_at is not None and now - j.created_at > dt.timedelta(hours=stale_h):
            ignored += 1
        else:
            queued += 1
    if ignored:
        log.warning("Tableau busy check: ignored %d Pending job(s) older than %g h (stale; cancel them on Tableau Server)", ignored, stale_h)
    return queued, running, ignored


def reconcile(cur, tableau, jobs, today, execute, timeout_s):
    """Fix up rows whose outcome is not final: 'Submitted' (any date) and today's 'TimedOut'. Sets j['running'] when the
    Tableau job may still be running. In a dry run nothing is written.
    - Submitted row with no job id (crash between refresh() and storing the id): after SUBMIT_UNKNOWN_MIN look on
      Tableau for a refresh job of that data source created since the row; adopt it if found, else write off Failed.
    - Tableau job unreadable: treated as possibly running until the row is older than HARD_LIMIT_H."""
    now = dt.datetime.now(dt.timezone.utc)
    for j in jobs:
        if j["Status"] not in ("Submitted", "TimedOut"):
            continue
        age = (now - j["submitted"]).total_seconds()
        if j["job"] is None:
            if age <= SUBMIT_UNKNOWN_MIN * 60:
                j["running"] = True
                continue
            try:
                name = tableau().datasources.get_by_id(j["ds"]).name
                found = refresh_jobs_for(tableau, name, j["submitted"] - dt.timedelta(minutes=2))
            except Exception as e:
                log.warning("Row %s (%s): no job id and Tableau cannot be searched (%s)", j["id"], j["ds"], type(e).__name__)
                if age > HARD_LIMIT_H * 3600:
                    if execute:
                        finish_row(cur, j["id"], "Failed", "no job id recorded and Tableau unreadable past the hard limit")
                    j["Status"] = "Failed"
                else:
                    j["running"] = True
                continue
            if not found:
                log.warning("Row %s (%s): no job id and no matching job on Tableau; written off as Failed", j["id"], j["ds"])
                if execute:
                    finish_row(cur, j["id"], "Failed", "submit outcome unknown: no job id recorded, none found on Tableau")
                j["Status"] = "Failed"
                continue
            j["job"] = found[0].id
            log.warning("Row %s (%s): adopted Tableau job %s found by search", j["id"], j["ds"], j["job"])
            if execute:
                cur.execute("UPDATE Integration.Tableau_RefreshJob SET TableauJobId = ?, Message = 'job id recovered by search' "
                            "WHERE RefreshJob_id = ?", j["job"], j["id"])
        try:
            job = tableau().jobs.get_by_id(j["job"])
        except Exception as e:  # not found or transient: assume still running until the hard limit
            log.warning("Row %s (%s): cannot read Tableau job %s (%s)", j["id"], j["ds"], j["job"], type(e).__name__)
            j["running"] = age <= HARD_LIMIT_H * 3600
            if not j["running"] and j["Status"] == "Submitted":
                if execute:
                    finish_row(cur, j["id"], "TimedOut", "Tableau job unreadable past the hard limit")
                j["Status"] = "TimedOut"
            continue
        if job.completed_at is not None:
            status, msg = job_outcome(job)
            log.info("Reconciled row %s (%s): was %s, Tableau job finished -> %s", j["id"], j["ds"], j["Status"], status)
            if execute:
                finish_row(cur, j["id"], status, "reconciled: " + msg, job.completed_at)
            j["Status"] = status
        elif j["Status"] == "Submitted" and age > timeout_s:
            log.warning("Row %s (%s): Tableau job %s still running after %d min; marked TimedOut but kept in flight",
                        j["id"], j["ds"], j["job"], age // 60)
            if execute:
                finish_row(cur, j["id"], "TimedOut", "left running by an earlier run; still running on Tableau")
            j["Status"] = "TimedOut"
            j["running"] = True
        else:
            j["running"] = True


# ---------- run ----------
def check_target(tableau, t):
    """The target must be a published extract on the server. Returns (datasource_item or None, reason, lookup_failed)."""
    try:
        ds = tableau().datasources.get_by_id(t.id)
    except Exception as e:
        code = getattr(e, "code", "")
        if str(code).startswith("404") or type(e).__name__ == "NotFoundError":
            return None, "not a published data source on the server (embedded extract? workbook refresh is not built)", False
        return None, "Tableau lookup failed (%s): skipped" % type(e).__name__, True
    if ds.has_extracts is not True:
        return None, "published data source has no extract (has_extracts=%s): skipped" % ds.has_extracts, False
    return ds, "", False


def submit_and_wait(cur, tableau, ready, timeout_s, poll_s, max_concurrent=MAX_CONCURRENT_REFRESHES,
                    stagger_s=REFRESH_STAGGER_SEC, clock=time.monotonic, sleep=time.sleep):
    """ready: [(Target, datasource_item)]. CASCADE (R1, Tableau server load): at most `max_concurrent` of our jobs in flight; the
    next one starts when a slot is free AND (nothing is in flight OR `stagger_s` seconds have passed since the last start).
    Targets that did not get to start before the budget ran out are left alone (no row), the next run retries them."""
    queue = list(ready)
    pending = {}  # job id -> (row id, target)
    results = {}
    deadline = clock() + timeout_s
    last_start = None
    while queue or pending:
        if queue and ops.killed_midrun("t4"):
            log.error("Kill switch: %d target(s) not started", len(queue))
            queue = []
        # start jobs while allowed
        while queue and len(pending) < max_concurrent and (clock() < deadline or last_start is None) and (
                not pending or last_start is None or clock() - last_start >= stagger_s):
            t, ds = queue.pop(0)
            row_id = cur.execute("SET NOCOUNT ON; INSERT Integration.Tableau_RefreshJob (DataSource_id, Message) "
                                 "OUTPUT inserted.RefreshJob_id VALUES (?, 'about to submit')", t.id).fetchone()[0]
            last_start = clock()
            clear_jobs_cache()
            try:
                job = tableau().datasources.refresh(ds)
            except Exception as e:
                log.error("Submit failed for %s: %s", t.name, type(e).__name__)
                finish_row(cur, row_id, "Failed", "submit error: %s" % str(e)[:300])
                results[t.id] = "Failed"
                continue
            if job is None:  # tableauserverclient returns None (no exception) when Tableau answers 409 "already queued"
                log.error("Tableau reports a refresh of %s is already queued; nothing was submitted by us", t.name)
                finish_row(cur, row_id, "Failed", "Tableau reports a refresh of this data source is already queued; "
                           "nothing was submitted by us (cancel the stale Pending jobs on Tableau Server)")
                results[t.id] = "Failed"
                continue
            cur.execute("UPDATE Integration.Tableau_RefreshJob SET TableauJobId = ?, Message = 'submitted' "
                        "WHERE RefreshJob_id = ?", job.id, row_id)
            log.info("Submitted refresh of %s (job %s); %d in flight, %d waiting", t.name, job.id, len(pending) + 1, len(queue))
            pending[job.id] = (row_id, t)
        if queue and clock() >= deadline and last_start is not None:
            log.warning("Run budget used up: %d target(s) not started, the next run retries them: %s", len(queue),
                        ", ".join(t.name or t.id for t, _ in queue))
            queue = []
        for jid in list(pending):
            row_id, t = pending[jid]
            try:
                job = tableau().jobs.get_by_id(jid)
            except Exception as e:
                log.warning("Polling job %s failed (%s), will retry", jid, type(e).__name__)
                getattr(tableau, "reset", lambda: None)()  # session may have been invalidated: sign in again
                continue
            if job.completed_at is not None:
                status, msg = job_outcome(job)
                finish_row(cur, row_id, status, msg, job.completed_at)
                log.info("%s: %s", t.name, status + " - " + msg)
                results[t.id] = status
                del pending[jid]
        if pending and clock() >= deadline:
            break
        if pending or queue:
            sleep(poll_s)
    for jid, (row_id, t) in pending.items():
        finish_row(cur, row_id, "TimedOut", "gave up waiting after %d s; the job may still be running" % timeout_s)
        log.error("%s: TimedOut (job %s); retried or reconciled on the next run", t.name, jid)
        results[t.id] = "TimedOut"
    return results


def run(cur, tableau, *, execute, today, timeout_s=TIMEOUT_MIN * 60, poll_s=POLL_SECONDS, targets=None,
        excluded=None, etl_lookup=etl_status_today, ds_like="%", scope="all", due_wbs=None,
        max_concurrent=MAX_CONCURRENT_REFRESHES, stagger_s=REFRESH_STAGGER_SEC, sched_due=None, sched_notes=None):
    """One scheduler run. tableau is a callable returning a signed-in server (signs in lazily). Returns a summary.
    ds_like restricts which RefreshJob rows are read/reconciled (tests only, so they never touch real rows)."""
    clear_jobs_cache()
    if targets is None:
        targets, excluded = load_targets(cur, scope, due_wbs, sched_due, sched_notes)
    excluded = excluded or {}
    log.info("%s run for Singapore date %s: %d candidate extract target(s), %d excluded before the ETL check",
             "EXECUTE" if execute else "DRY", today, len(targets), len(excluded))
    for tid, (name, why) in sorted(excluded.items(), key=lambda x: x[1][0] or ""):
        log.info("  not in refresh set: %s [%s]: %s", name, tid, why)
    taken = taken_over_rows(cur)
    if taken:   # D-074: a taken-over extract that is not a candidate is only refreshed by nobody; list it every run
        cand_ids = {t.id for t in targets}
        off = sorted({(r[1], r[0]) for r in taken if r[0] not in cand_ids})
        for name, tid in off:
            log.warning("  TAKEN OVER but not a candidate now: %s [%s]: %s", name, tid, excluded.get(tid, (name, "not in the mapping any more"))[1])
    else:
        off = []
    jobs = load_jobs(cur, today, ds_like)
    reconcile(cur, tableau, jobs, today, execute, timeout_s)
    etl = etl_lookup(cur, today)
    log.info("ETL_Master_Log latest rows today: %s", {p: ("OK" if ok else "FAILED") + " " + e.strftime("%H:%M")
                                                       for p, (e, ok) in sorted(etl.items())} or "none")
    summary = {"refresh": [], "skip": {}, "results": {}, "excluded": excluded, "waiting": 0, "lookup_failed": 0, "alerts": [],
               "taken_over": len({r[0] for r in taken}), "taken_over_excluded": len(off)}
    ready = []
    busy = {}   # D-057: the "Tableau busy" check runs once, when the first target is otherwise ready to refresh
    for t in sorted(targets, key=lambda x: ((x.name or "").lower(), str(x.id))):   # run order: by name
        mine = [j for j in jobs if j["ds"] == t.id and (j["date"] == today.isoformat() or j["running"])]
        action, reason = decide(t, etl, mine)
        n_bad = sum(j["Status"] in ("Failed", "Cancelled") for j in mine)
        if reason.startswith("gave up"):
            summary["alerts"].append({"sub": t.id, "wb": t.name, "code": "refresh_gave_up", "text":
                                      "the refresh of extract '%s' failed %d times today; no more attempts today, reports that need it are NOT sent" % (t.name, n_bad)})
        elif n_bad:
            summary["alerts"].append({"sub": t.id, "wb": t.name, "code": "refresh_failed", "text":
                                      "the refresh of extract '%s' failed or was cancelled (%d failed attempt(s) today, retried on the next run, up to %d)" % (t.name, n_bad, MAX_ATTEMPTS)})
        if action == "refresh" and "r" not in busy:
            try:
                busy["r"] = tableau_busy(tableau, [j["job"] for j in jobs])
            except Exception as e:
                busy["r"] = e
            if isinstance(busy["r"], tuple) and busy["r"][0] + busy["r"][1]:
                log.info("Tableau busy: %d queued, %d running; %s", *busy["r"][:2], "waiting" if execute else "would wait (dry run)")
        if action == "refresh":
            b = busy["r"]
            if isinstance(b, Exception):
                action, reason = "skip", "Tableau job lookup failed (%s): skipped" % type(b).__name__
                summary["lookup_failed"] += 1
            elif b and b[0] + b[1]:
                action, reason = "skip", "waiting: Tableau busy (%d queued, %d running)" % b[:2]
        if action == "refresh":
            ds, why, failed = check_target(tableau, t)
            if ds is None:
                action, reason = "skip", why
                summary["lookup_failed"] += failed
            else:
                try:
                    other = active_refresh_job(tableau, ds.name)
                except Exception as e:
                    action, reason = "skip", "Tableau job lookup failed (%s): skipped" % type(e).__name__
                    summary["lookup_failed"] += 1
                else:
                    if other is not None:
                        action = "skip"
                        reason = "a refresh job is already queued/running on Tableau (job %s, started by someone else?)" % other.id
        if action == "skip" and reason.startswith("waiting"):
            summary["waiting"] += 1
        detail = "; ".join(pipeline_text(p, k, etl) for p, k in sorted(t.pipelines.items()))
        log.info("%s [%s] workbooks=%d via=%s -> %s: %s | %s", t.name, t.id, len(t.workbooks), "+".join(sorted(t.via)) or "-",
                 "REFRESH" if action == "refresh" else "skip", reason, detail)
        if action == "refresh":
            summary["refresh"].append(t.id)
            summary["scheduled"] = summary.get("scheduled", 0) + ("extract schedule" in t.via)
            ready.append((t, ds))
        else:
            summary["skip"][t.id] = reason
    # The concurrency cap counts jobs of earlier runs that are still running (a long extract outlives its run), not only this run's.
    in_flight = len({j["job"] or ("row", j["id"]) for j in jobs if j["running"]})
    slots = max(0, max_concurrent - in_flight)
    if len(ready) > slots:
        for t, _ in ready[slots:]:
            summary["refresh"].remove(t.id)
            summary["skip"][t.id] = "waiting: %d of our refresh job(s) still running (limit %d at once)" % (in_flight, max_concurrent)
            summary["waiting"] += 1
        log.info("Concurrency cap: %d of our refresh job(s) still running, limit %d; %d target(s) wait for the next run",
                 in_flight, max_concurrent, len(ready) - slots)
        ready = ready[:slots]
    if not execute:
        log.info("DRY RUN: would submit %d refresh(es); nothing was written to Tableau or the DWH", len(ready))
        return summary
    if ready:
        summary["results"] = submit_and_wait(cur, tableau, ready, timeout_s, poll_s, slots, stagger_s)
        names = {t.id: t.name for t, _ in ready}
        have = {e["sub"] for e in summary["alerts"]}
        for tid, res in summary["results"].items():
            if res in ("Failed", "Cancelled") and tid not in have:
                summary["alerts"].append({"sub": tid, "wb": names.get(tid), "code": "refresh_failed", "text":
                                          "the refresh of extract '%s' just failed or was cancelled; it is retried on the next run, up to %d attempts a day" % (names.get(tid), MAX_ATTEMPTS)})
    return summary


def summary_line(s, execute):
    """One parseable line per run for later alerting."""
    res = list(s["results"].values())
    failed = sum(v in ("Failed", "TimedOut", "Cancelled") for v in res) + s["lookup_failed"]
    refreshed = sum(v == "Succeeded" for v in res) if execute else len(s["refresh"])
    skipped = len(s["skip"]) - s["waiting"]
    return "RUN SUMMARY mode=%s refreshed=%d%s waiting=%d skipped=%d failed=%d lookup_failed=%d due_by_extract_schedule=%d taken_over=%d taken_over_excluded=%d" % (
        "execute" if execute else "dry-run", refreshed, "" if execute else "(would)", s["waiting"], skipped, failed,
        s["lookup_failed"], s.get("scheduled", 0), s.get("taken_over", 0), s.get("taken_over_excluded", 0))


def exit_code(s):
    if any(v in ("Failed", "TimedOut", "Cancelled") for v in s["results"].values()):
        return 1
    gated = len(s["refresh"]) + s["lookup_failed"]  # targets whose ETL gate was open
    if s["lookup_failed"] and s["lookup_failed"] >= gated:  # every open target blocked by a Tableau read failure
        return 1
    return 0


PAT_LOCK = "TABLEAU_PAT_SESSION"   # shared by t4_refresh.py and t6_send.py: one PAT, a new sign-in kills the older session
HTTP_TIMEOUT_S = int(os.environ.get("TABLEAU_HTTP_TIMEOUT_S", 1500))  # per Tableau HTTP request, 25 min: heavy PDF exports are slow


class PatBusy(BaseException):
    """Another script holds the PAT session lock. Not an Exception on purpose: per-lookup handlers do not swallow it.
    The caller skips this run (exit 0); the scheduler retries in 15 minutes."""


class LazyTableau:
    """Signs in on first use, signs out on close. A failed sign-in is fatal: logged with the traceback, exit code 1.
    With lock_cur (a DWH cursor) the shared PAT session applock is taken before the first sign-in and held until close();
    if another script holds it, PatBusy is raised instead of signing in."""

    def __init__(self, lock_cur=None, owner="?", lock_name=None):
        self.server = None
        self.lock_cur = lock_cur
        self.locked = False
        self.owner = owner
        self.lock_name = lock_name or PAT_LOCK   # tests pass their own name so they never touch the real lock

    def _lock(self):
        if self.lock_cur is None or self.locked:
            return
        got = self.lock_cur.execute("DECLARE @r int; EXEC @r = sp_getapplock @Resource = ?, @LockMode = 'Exclusive', "
                                    "@LockOwner = 'Session', @LockTimeout = 0; SELECT @r", self.lock_name).fetchone()[0]
        if got < 0:
            log.warning("Another script is using the Tableau PAT session (lock %s); skipping this run, it retries next time", PAT_LOCK)
            raise PatBusy()
        self.locked = True
        ops.write_holder(self.owner)   # lets a skipped script tell an ordinary backlog from a stuck holder

    def _unlock(self):
        if self.locked:
            ops.clear_holder(self.owner)
            try:
                self.lock_cur.execute("EXEC sp_releaseapplock @Resource = ?, @LockOwner = 'Session'", self.lock_name)
            except Exception:
                pass
            self.locked = False

    def __call__(self):
        if self.server is None:
            import tableauserverclient as TSC
            self._lock()
            auth = TSC.PersonalAccessTokenAuth(os.environ["TABLEAU_PAT_NAME"], os.environ["TABLEAU_PAT_SECRET"],
                                               os.environ.get("TABLEAU_SITE_ID", ""))
            try:
                server = TSC.Server(os.environ["TABLEAU_SERVER_URL"].strip(), use_server_version=True)
                server.add_http_options({"timeout": HTTP_TIMEOUT_S})
                server.auth.sign_in(auth)
            except Exception:
                log.exception("FATAL: Tableau sign-in failed (another script using the same PAT? expired PAT?)")
                self._unlock()
                raise SystemExit(1)  # not an Exception: the per-lookup "except Exception" handlers do not swallow it
            self.server = server
        return self.server

    def _sign_out(self):
        if self.server is not None:
            try:
                self.server.auth.sign_out()
            except Exception:
                pass

    def reset(self):
        self._sign_out()
        self.server = None   # the lock stays held, the next call signs in again

    def close(self):
        self._sign_out()
        self.server = None
        self._unlock()


def setup_logging():
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    fh = ops.log_file_handler("t4_refresh")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    ops.log.setLevel(logging.INFO)
    if not ops.log.handlers:
        ops.log.addHandler(sh)
        ops.log.addHandler(fh)

    def hook(et, ev, tb):  # unhandled errors go to the log file too
        log.critical("Unhandled error", exc_info=(et, ev, tb))
    sys.excepthook = hook


def send_refresh_alerts(events, ses, local_smtp):
    """Mail ALERT_TO about failed extract refreshes: once per extract and code per Singapore day (alert state file). Never raises."""
    try:
        import t6_send as t6   # imported late: t6_send imports this module
        cfg = ops.alert_config(ses, os.environ, "t4", True, local_smtp)
        ops.check_alert_recipient(cfg, "t4")
        t6.deliver_alerts(cfg, t6.make_mailer(cfg, "127.0.0.1", 1025, os.environ), ops.sg_now().date(), events,
                          intro="An extract refresh failed. Reports that depend on it are held until it succeeds:", footer=False,
                          subject="Tableau subscription automation: %d extract refresh problem(s) (%s)")
    except Exception as e:   # an alert problem must not fail the refresh run
        log.error("Refresh-failure alert could not be sent (%s)", type(e).__name__)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="default: evaluate and print, write nothing")
    g.add_argument("--execute", action="store_true", help="REAL run: refresh on Tableau and write Tableau_RefreshJob")
    ap.add_argument("--date", help="dry run only: evaluate as if this Singapore date were today (YYYY-MM-DD)")
    ap.add_argument("--timeout-min", type=int, default=int(os.environ.get("T4_MAX_LOCK_MIN", T4_MAX_LOCK_MIN)),
                    help="how long this run waits for refresh jobs = how long it holds the PAT lock (default 10, env T4_MAX_LOCK_MIN); "
                         "targets not started and jobs not finished are handled by the next run")
    ap.add_argument("--scope", choices=sorted(SCOPES), default=os.environ.get("T4_REFRESH_SCOPE", "takeover"),
                    help="takeover (default, pilot): refresh only extracts behind workbooks with a subscription taken over by "
                         "t11_pause.py; all: every workbook with an active subscription (after the pilot). Env T4_REFRESH_SCOPE")
    ap.add_argument("--schedule-mode", choices=("honour", "ignore"), default=None,
                    help="honour (default, env SCHEDULE_MODE): refresh only workbooks that have a subscription DUE today (D-036); "
                         "ignore: every workbook with an in-scope subscription")
    ap.add_argument("--catch-up-days", type=int, default=None)
    ap.add_argument("--max-concurrent", type=int, default=int(os.environ.get("MAX_CONCURRENT_REFRESHES", MAX_CONCURRENT_REFRESHES)),
                    help="refresh jobs in flight at once (default 1, env MAX_CONCURRENT_REFRESHES)")
    ap.add_argument("--stagger-sec", type=float, default=float(os.environ.get("REFRESH_STAGGER_SEC", REFRESH_STAGGER_SEC)),
                    help="seconds between two job starts when several may run (default 60, env REFRESH_STAGGER_SEC)")
    ap.add_argument("--smtp", "--ses", dest="ses", action="store_true",
                    help="operational alert mails (kill switch) through the real SMTP server instead of the local test sink "
                         "(or set MAIL_TRANSPORT=smtp)")
    ap.add_argument("--local-smtp", action="store_true", help="force the local test sink even if MAIL_TRANSPORT=smtp")
    a = ap.parse_args()
    if a.date and a.execute:
        sys.exit("--date is for dry runs only")
    setup_logging()
    try:
        a.schedule_mode = ops.schedule_mode(os.environ, a.schedule_mode)
        a.ses = ops.effective_ses(a.ses, a.execute, os.environ, a.local_smtp)
    except ValueError as e:
        sys.exit(str(e))
    ops.log_transport("t4", a.ses, a.execute)
    if ops.check_kill_switch("t4", a.execute, a.ses):
        return 0
    conn = connect()
    cur = conn.cursor()
    today = dt.date.fromisoformat(a.date) if a.date else dwh_today(cur)
    if a.date:
        dwh_today(cur)  # still enforces the time zone check
    if a.execute:
        got = cur.execute("DECLARE @r int; EXEC @r = sp_getapplock @Resource = ?, @LockMode = 'Exclusive', "
                          "@LockOwner = 'Session', @LockTimeout = 0; SELECT @r", LOCK_NAME).fetchone()[0]
        if got < 0:
            log.warning("Another run holds the lock; exiting")
            return 0
    tableau = LazyTableau(cur, owner="t4")
    mode = "execute" if a.execute else "dry-run"
    try:
        due = due_workbooks(cur, today, a.scope, a.catch_up_days, ops.default_max_age_h()) if a.schedule_mode == "honour" else None
        sdue, snotes = extract_due(cur, today, a.catch_up_days, ops.default_max_age_h()) if a.schedule_mode == "honour" else (None, None)
        s = run(cur, tableau, execute=a.execute, today=today, timeout_s=a.timeout_min * 60, scope=a.scope, due_wbs=due,
                max_concurrent=max(1, a.max_concurrent), stagger_s=a.stagger_sec, sched_due=sdue, sched_notes=snotes)
    except PatBusy:
        ops.on_pat_busy("t4", a.execute, ses=a.ses)
        log.info("RUN SUMMARY mode=%s skipped=pat_busy", mode)
        ops.write_marker("t4", mode, 0, "skipped=pat_busy", outcome="pat_busy")
        return 0
    except SystemExit:
        ops.write_marker("t4", mode, 1, "FATAL Tableau sign-in failed", outcome="fatal")
        raise
    finally:
        tableau.close()
    ops.pat_ok("t4")
    if a.execute and s.get("alerts"):
        send_refresh_alerts(s["alerts"], a.ses, a.local_smtp)
    line = summary_line(s, a.execute) + " schedule_mode=%s" % a.schedule_mode + ops.allow_all_marker()
    log.info(line)
    code = exit_code(s)
    ops.write_marker("t4", mode, code, line)
    return code


if __name__ == "__main__":
    sys.exit(main())
