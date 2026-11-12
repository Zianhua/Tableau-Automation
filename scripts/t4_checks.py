"""T4/T5 checks. No Tableau access at all: a fake server object stands in for tableauserverclient.
Dev DWH only: read-only queries, plus fixture rows in Integration.Tableau_RefreshJob under DataSource_id 'T4TEST-%'
(deleted at the start and at the end). Every run() in the tests is restricted to those rows (ds_like), and a checksum of
all other RefreshJob rows is compared before and after. ETL_Master_Log is only READ (real query, real dev data).

Usage: python scripts/t4_checks.py
"""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent))
import t4_refresh as m  # noqa: E402
from check_fixtures import known_pipelines  # noqa: E402

fails = []
LIKE = "T4TEST-%"


def check(name, ok, detail=""):
    print("%s  %s %s" % ("PASS" if ok else "FAIL", name, detail))
    if not ok:
        fails.append(name)


def sgt_now():
    return dt.datetime.now(m.SGT).replace(tzinfo=None)


# ---------- fake Tableau ----------
class FakeTableau:
    """Callable like LazyTableau: tableau() -> server. Jobs finish after `polls_to_finish` polls (None = never).
    bg = background jobs visible to jobs.get (inject external/old-script jobs here)."""

    def __init__(self, polls_to_finish=1, finish_code=0, fail_submit=False, published=True):
        self.polls_to_finish, self.finish_code, self.fail_submit, self.published = polls_to_finish, finish_code, fail_submit, published
        self.submitted, self.polls, self.job_seq, self.done, self.unreadable, self.bg = [], {}, 0, set(), set(), []
        self.jobs_get_fails = False
        self.foreign = {}   # job id -> JobItem-like detail of a job someone else submitted (old refresh job)
        self.already_queued = set()  # ds ids for which refresh returns None (Tableau 409 "already queued")
        self.datasources = NS(get_by_id=self._get_ds, refresh=self._refresh)
        self.jobs = NS(get_by_id=self._get_job, get=self._jobs_get)
        self.owned_tasks = []        # data source ids that have a Tableau extract refresh task on an Active schedule
        self.tasks_fail = False
        self.tasks_empty = False
        self.no_sched = set()        # data source ids whose task comes back without a schedule id
        self.sched_days = None       # set of weekday numbers for the fake schedule; None = daily
        self.tasks = NS(get=self._tasks_get)
        self.schedules = NS(get_by_id=lambda sid: NS(id=sid, name="fake", state="Active", interval_item=NS(
            _frequency="Weekly" if self.sched_days is not None else "Daily",
            interval=tuple(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][d] for d in sorted(self.sched_days or ())),
            start_time=None)))

    def __call__(self):
        return self

    def _get_ds(self, i):
        if not self.published:
            e = Exception("404")
            e.code = "404006"
            raise e
        return NS(id=i, name="DS-" + i, has_extracts=True)

    def _refresh(self, ds):
        if self.fail_submit:
            raise RuntimeError("boom")
        if ds.id in self.already_queued:
            return None
        self.job_seq += 1
        jid = "job-%d-%d" % (id(self) % 1000, self.job_seq)
        self.submitted.append(ds.id)
        self.polls[jid] = 0
        return NS(id=jid)

    def _get_job(self, jid):
        if jid in self.foreign:
            return self.foreign[jid]
        if jid in self.unreadable or jid not in self.polls:
            raise KeyError("no such job")
        self.polls[jid] += 1
        fin = jid in self.done or (self.polls_to_finish is not None and self.polls[jid] >= self.polls_to_finish)
        return NS(id=jid, completed_at=dt.datetime.now(dt.timezone.utc) if fin else None,
                  finish_code=self.finish_code if fin else None, notes=["fake note"])

    def _tasks_get(self, req_options=None):
        if self.tasks_fail:
            raise RuntimeError("tasks endpoint down")
        items = [NS(id="task-" + d, target=NS(id=d, type="datasource"), schedule_id=None if d in self.no_sched else "sched-1", priority=50,
                    consecutive_failed_count=0) for d in list(self.owned_tasks) + ["unrelated-ds"]]
        if self.tasks_empty:
            items = []
        return items, NS(total_available=len(items))

    def _jobs_get(self, req_options=None):
        if self.jobs_get_fails:
            raise RuntimeError("jobs endpoint down")
        return list(self.bg), NS(page_number=1, page_size=1000, total_available=len(self.bg))


def bg_job(tid, jid, ended=False):
    return NS(id=jid, title="DS-" + tid, type="RefreshExtracts", created_at=dt.datetime.now(dt.timezone.utc),
              ended_at=dt.datetime.now(dt.timezone.utc) if ended else None)


def target(tid, pipes, known=True):
    return m.Target(tid, "name-" + tid, [("wb1", "WB 1")], {p: known for p in pipes})


# ---------- pure decision tests ----------
def test_decide():
    A, B = "CQGlobalSGDW: A", "CQGlobalSGDW: B"
    t = target("x", [A, B])
    end = dt.datetime(2026, 10, 22, 10, 30)
    ok = {A: (end, True), B: (end - dt.timedelta(hours=1), True)}
    last = end.replace(tzinfo=m.SGT)

    def job(status, minutes_after_etl=1, running=False):
        return {"Status": status, "running": running, "submitted": last + dt.timedelta(minutes=minutes_after_etl)}

    act = lambda e, jobs=(), tt=t: m.decide(tt, e, list(jobs))[0]
    check("decide: all pipelines OK -> refresh", act(ok) == "refresh")
    check("decide: one of two pipelines missing today -> skip (the old script's defect)", act({A: (end, True)}) == "skip")
    check("decide: one pipeline latest run failed -> skip", act({A: (end, True), B: (end, False)}) == "skip")
    check("decide: unknown pipeline -> skip", act(ok, tt=target("x", [A, B], known=False)) == "skip")
    check("decide: no pipelines -> skip", act({}, tt=target("x", [])) == "skip")
    check("decide: Succeeded AFTER last ETL -> skip", act(ok, [job("Succeeded", 1)]) == "skip")
    check("decide: Succeeded BEFORE last ETL ended (stale) -> refresh again", act(ok, [job("Succeeded", -20)]) == "refresh")
    check("decide: in flight -> skip", act(ok, [job("Submitted", 1, running=True)]) == "skip")
    check("decide: one failed attempt -> retry", act(ok, [job("Failed")]) == "refresh")
    check("decide: timed-out and finished -> retry", act(ok, [job("TimedOut")]) == "refresh")
    check("decide: cap counts failures only (2 Succeeded-stale + 1 Failed) -> retry",
          act(ok, [job("Succeeded", -30), job("Succeeded", -20), job("Failed")]) == "refresh")
    check("decide: 3 failed attempts -> skip, needs a human",
          act(ok, [job("Failed"), job("Cancelled"), job("TimedOut")]) == "skip")
    check("decide: cap not reached with 2 attempts -> retry", act(ok, [job("Succeeded", -30), job("Failed")]) == "refresh")
    check("decide: pipeline failed later beats an earlier success", act({A: (end, True), B: (end, False)}, [job("Succeeded", -20)]) == "skip")
    # finish codes
    f = lambda code: m.job_outcome(NS(finish_code=code, notes=[]))[0]
    check("finish code 0 -> Succeeded; 1 -> Failed; 2 -> Cancelled; 3 and unknown -> Failed",
          [f(0), f(1), f(2), f(3), f(99), f(None)] == ["Succeeded", "Failed", "Cancelled", "Failed", "Failed", "Failed"])
    # exit codes and summary line
    base = {"refresh": [], "skip": {}, "results": {}, "excluded": {}, "waiting": 0, "lookup_failed": 0}
    ex = lambda **kw: m.exit_code(dict(base, **kw))
    check("exit: clean run -> 0", ex() == 0)
    check("exit: failed result -> 1", ex(results={"a": "Failed"}) == 1)
    check("exit: every open target blocked by lookup failure -> 1", ex(lookup_failed=2) == 1)
    check("exit: one lookup failure but another target refreshes -> 0", ex(lookup_failed=1, refresh=["a"]) == 0)
    line = m.summary_line(dict(base, refresh=["a"], skip={"b": "x", "c": "waiting for 1"}, waiting=1), False)
    check("summary line is one parseable line", line.startswith("RUN SUMMARY") and "\n" not in line and "waiting=1" in line, line)


# ---------- DWH tests ----------
def rows(cur, prefix=LIKE):
    return cur.execute("SELECT DataSource_id, TableauJobId, Status, CASE WHEN FinishedAt IS NULL THEN 0 ELSE 1 END, Message, "
                       "RefreshJob_id FROM Integration.Tableau_RefreshJob WHERE DataSource_id LIKE ? ORDER BY RefreshJob_id",
                       prefix).fetchall()


def cleanup(cur):
    cur.execute("DELETE FROM Integration.Tableau_RefreshJob WHERE DataSource_id LIKE 'T4TEST-%'")


def others_checksum(cur):
    r = cur.execute("SELECT COUNT(*), CHECKSUM_AGG(CHECKSUM(*)) FROM Integration.Tableau_RefreshJob "
                    "WHERE DataSource_id NOT LIKE 'T4TEST-%'").fetchone()
    return tuple(r)


def run_once(cur, tab, targets, today, execute=True, e=None, timeout_s=5, poll_s=0, max_concurrent=99):
    # max_concurrent is high here so the rows other checks leave "running" do not hold back unrelated checks; see test_concurrency_cap
    return m.run(cur, tab, execute=execute, today=today, timeout_s=timeout_s, poll_s=poll_s, targets=targets,
                 excluded={}, etl_lookup=lambda c, d: e or {}, ds_like=LIKE, max_concurrent=max_concurrent)


def test_flow(cur):
    today = m.dwh_today(cur)
    P = "CQGlobalSGDW: A"
    e = {P: (sgt_now() - dt.timedelta(minutes=5), True)}
    # 1. dry run writes nothing and never touches refresh
    tab = FakeTableau()
    s = run_once(cur, tab, [target("T4TEST-1", [P])], today, execute=False, e=e)
    check("dry run: decides REFRESH", s["refresh"] == ["T4TEST-1"])
    check("dry run: no submit, no rows", tab.submitted == [] and rows(cur) == [])
    # 2. execute: success logged when the job finishes; second run does nothing
    tab = FakeTableau(polls_to_finish=3)
    run_once(cur, tab, [target("T4TEST-1", [P])], today, e=e)
    r = rows(cur)
    check("execute: one job submitted, row Succeeded with FinishedAt", len(r) == 1 and r[0][2] == "Succeeded" and r[0][3] == 1
          and r[0][1] is not None, str([x[:5] for x in r]))
    check("execute: polled until completion (3 polls)", list(tab.polls.values()) == [3])
    tab2 = FakeTableau()
    run_once(cur, tab2, [target("T4TEST-1", [P])], today, e=e)
    check("rerun same day: no second refresh, no new row", tab2.submitted == [] and len(rows(cur)) == 1)
    # 3. gate
    tab = FakeTableau()
    run_once(cur, tab, [target("T4TEST-2", [P, "CQGlobalSGDW: B"])], today, e=e)
    check("gate: second pipeline missing -> no submit, no row", tab.submitted == [] and not rows(cur, "T4TEST-2"))
    # 4. failed job then retry
    tab = FakeTableau(finish_code=1)
    s = run_once(cur, tab, [target("T4TEST-3", [P])], today, e=e)
    r = rows(cur, "T4TEST-3")
    check("failed job: row Failed with FinishedAt", len(r) == 1 and r[0][2] == "Failed" and r[0][3] == 1 and "finish code 1" in r[0][4])
    check("failed job: exit code 1", m.exit_code(s) == 1)
    check("failed job: one refresh_failed alert event for that extract",
          [(x["sub"], x["code"]) for x in s["alerts"]] == [("T4TEST-3", "refresh_failed")], str(s["alerts"]))
    tab = FakeTableau()
    run_once(cur, tab, [target("T4TEST-3", [P])], today, e=e)
    check("retry next run after failure: new job, now Succeeded", len(tab.submitted) == 1
          and [x[2] for x in rows(cur, "T4TEST-3")] == ["Failed", "Succeeded"])
    # 5. submit error
    run_once(cur, FakeTableau(fail_submit=True), [target("T4TEST-4", [P])], today, e=e)
    r = rows(cur, "T4TEST-4")
    check("submit error: row Failed, no job id", len(r) == 1 and r[0][2] == "Failed" and r[0][1] is None)
    # 5a. Tableau answers "already queued" (client returns None): that target Failed, the next target still submitted
    tab = FakeTableau()
    tab.already_queued.add("T4TEST-17")
    s = run_once(cur, tab, [target("T4TEST-17", [P]), target("T4TEST-18", [P])], today, e=e)
    r8, r9 = rows(cur, "T4TEST-17"), rows(cur, "T4TEST-18")
    check("already queued on Tableau (409): no crash, row Cancelled with the 'already queued' message, no job id",
          len(r8) == 1 and r8[0][2] == "Cancelled" and r8[0][3] == 1 and r8[0][1] is None and r8[0][4] == m.QUEUED_MSG,
          str([x[:5] for x in r8]))
    check("already queued: next target still submitted and Succeeded", tab.submitted == ["T4TEST-18"]
          and [x[2] for x in r9] == ["Succeeded"])
    check("already queued: NOT a failure (failed=0, exit 0, no refresh_failed alert)",
          "failed=0" in m.summary_line(s, True) and m.exit_code(s) == 0 and not s["alerts"], m.summary_line(s, True))
    tab = FakeTableau()
    s = run_once(cur, tab, [target("T4TEST-17", [P])], today, e=e)
    check("already queued: the next run does not count it as a failed attempt (no alert)",
          not any(a["code"].startswith("refresh_failed") for a in s["alerts"]), str(s["alerts"]))
    # 6. timeout, still running, then reconciled
    tab = FakeTableau(polls_to_finish=None)
    run_once(cur, tab, [target("T4TEST-5", [P])], today, e=e, timeout_s=0)
    r = rows(cur, "T4TEST-5")
    check("timeout: row TimedOut with FinishedAt", len(r) == 1 and r[0][2] == "TimedOut" and r[0][3] == 1)
    jid = r[0][1]
    run_once(cur, tab, [target("T4TEST-5", [P])], today, e=e, timeout_s=0)
    check("timed out but still running on Tableau: no second refresh", len(tab.submitted) == 1 and len(rows(cur, "T4TEST-5")) == 1)
    tab.done.add(jid)
    run_once(cur, tab, [target("T4TEST-5", [P])], today, e=e)
    check("timed-out job later finished: reconciled to Succeeded, still one job", len(tab.submitted) == 1
          and [x[2] for x in rows(cur, "T4TEST-5")] == ["Succeeded"])
    # 6a. the concurrency cap counts jobs left running by earlier runs
    P2 = P
    tab = FakeTableau(polls_to_finish=None)
    run_once(cur, tab, [target("T4TEST-19", [P2])], today, e=e, timeout_s=0, max_concurrent=1)
    check("cap 1: first long extract submitted, timed out but still running", len(tab.submitted) == 1
          and [x[2] for x in rows(cur, "T4TEST-19")] == ["TimedOut"])
    cur.execute("UPDATE Integration.Tableau_RefreshJob SET Status = 'Succeeded' WHERE DataSource_id LIKE 'T4TEST-%' AND DataSource_id <> 'T4TEST-19' AND Status IN ('Submitted', 'TimedOut')")
    s = run_once(cur, tab, [target("T4TEST-19", [P2]), target("T4TEST-20", [P2])], today, e=e, timeout_s=0, max_concurrent=1)
    check("cap 1: a later run does not start a second extract while the first still runs",
          tab.submitted == ["T4TEST-19"] and not rows(cur, "T4TEST-20") and "still running" in s["skip"].get("T4TEST-20", ""), str(tab.submitted))
    run_once(cur, tab, [target("T4TEST-20", [P2])], today, e=e, timeout_s=0, max_concurrent=2)
    check("cap 2: a second extract may run next to the first", "T4TEST-20" in tab.submitted)
    cur.execute("UPDATE Integration.Tableau_RefreshJob SET Status = 'Succeeded' WHERE DataSource_id IN ('T4TEST-19', 'T4TEST-20')")
    # 7. crashed run leftovers
    cur.execute("INSERT Integration.Tableau_RefreshJob (DataSource_id, TableauJobId) VALUES ('T4TEST-6', 'job-x-1')")
    tab = FakeTableau(polls_to_finish=None)
    tab.polls["job-x-1"] = 0
    run_once(cur, tab, [target("T4TEST-6", [P])], today, e=e)
    check("leftover Submitted row, job running: no new submit", tab.submitted == [] and rows(cur, "T4TEST-6")[0][2] == "Submitted")
    tab.done.add("job-x-1")
    run_once(cur, tab, [target("T4TEST-6", [P])], today, e=e)
    check("leftover Submitted row, job finished: reconciled to Succeeded, no new submit",
          tab.submitted == [] and rows(cur, "T4TEST-6")[0][2] == "Succeeded")
    # 7a. null job id, old, nothing on Tableau -> Failed, retried
    cur.execute("INSERT Integration.Tableau_RefreshJob (DataSource_id, SubmittedAt) VALUES ('T4TEST-7', "
                "DATEADD(minute, -30, SYSDATETIMEOFFSET()))")
    tab = FakeTableau()
    run_once(cur, tab, [target("T4TEST-7", [P])], today, e=e)
    check("no job id, searched Tableau, nothing found: written off Failed, then retried",
          [x[2] for x in rows(cur, "T4TEST-7")] == ["Failed", "Succeeded"] and len(tab.submitted) == 1)
    # 7b. null job id, old, a matching job IS on Tableau -> adopted, no resubmit
    cur.execute("INSERT Integration.Tableau_RefreshJob (DataSource_id, SubmittedAt) VALUES ('T4TEST-11', "
                "DATEADD(minute, -30, SYSDATETIMEOFFSET()))")
    tab = FakeTableau(polls_to_finish=None)
    tab.bg = [bg_job("T4TEST-11", "adopted-1")]
    tab.polls["adopted-1"] = 0
    run_once(cur, tab, [target("T4TEST-11", [P])], today, e=e, timeout_s=3600)
    r = rows(cur, "T4TEST-11")
    check("no job id, matching running job on Tableau: adopted, in flight, no resubmit",
          tab.submitted == [] and len(r) == 1 and r[0][1] == "adopted-1" and r[0][2] == "Submitted")
    tab.done.add("adopted-1")
    run_once(cur, tab, [target("T4TEST-11", [P])], today, e=e)
    check("adopted job finished: row Succeeded, still no resubmit", tab.submitted == [] and rows(cur, "T4TEST-11")[0][2] == "Succeeded")
    # 7c. null job id, young row -> in flight
    cur.execute("INSERT Integration.Tableau_RefreshJob (DataSource_id) VALUES ('T4TEST-13')")
    tab = FakeTableau()
    run_once(cur, tab, [target("T4TEST-13", [P])], today, e=e)
    check("no job id, row younger than %d min: in flight, no submit" % m.SUBMIT_UNKNOWN_MIN, tab.submitted == [])
    # 7d. TimedOut row whose job cannot be read: possibly running until the hard limit
    cur.execute("INSERT Integration.Tableau_RefreshJob (DataSource_id, TableauJobId, Status, FinishedAt, SubmittedAt) "
                "VALUES ('T4TEST-14', 'gone-1', 'TimedOut', SYSDATETIMEOFFSET(), DATEADD(minute, -2, SYSDATETIMEOFFSET()))")
    tab = FakeTableau()
    run_once(cur, tab, [target("T4TEST-14", [P])], today, e=e)
    check("TimedOut row, job unreadable, younger than hard limit: treated as running", tab.submitted == [])
    old = m.HARD_LIMIT_H
    m.HARD_LIMIT_H = 1 / 60  # one minute
    try:
        run_once(cur, tab, [target("T4TEST-14", [P])], today, e=e)
    finally:
        m.HARD_LIMIT_H = old
    check("TimedOut row, job unreadable, older than hard limit: retried", len(tab.submitted) == 1)
    # 8. attempt cap (all attempts)
    for _ in range(m.MAX_ATTEMPTS):
        run_once(cur, FakeTableau(finish_code=1), [target("T4TEST-8", [P])], today, e=e)
    tab = FakeTableau()
    run_once(cur, tab, [target("T4TEST-8", [P])], today, e=e)
    s = run_once(cur, FakeTableau(), [target("T4TEST-8", [P])], today, e=e)
    check("attempt cap: a refresh_gave_up alert event is raised", [x["code"] for x in s["alerts"]] == ["refresh_gave_up"], str(s["alerts"]))
    check("attempt cap: stops after %d failures" % m.MAX_ATTEMPTS, tab.submitted == [] and len(rows(cur, "T4TEST-8")) == m.MAX_ATTEMPTS)
    # 8a. successful refreshes do not use up the cap (repeated ETL re-runs)
    for i in range(m.MAX_ATTEMPTS):
        cur.execute("INSERT Integration.Tableau_RefreshJob (DataSource_id, TableauJobId, Status, FinishedAt, SubmittedAt) "
                    "VALUES ('T4TEST-21', ?, 'Succeeded', SYSDATETIMEOFFSET(), DATEADD(hour, ?, SYSDATETIMEOFFSET()))", "ok-%d" % i, -3 - i)
    tab = FakeTableau()
    run_once(cur, tab, [target("T4TEST-21", [P])], today, e=e)
    check("%d earlier Succeeded refreshes today, ETL ran again since: refreshed again (cap counts failures only)" % m.MAX_ATTEMPTS,
          len(tab.submitted) == 1)
    # 9. not published
    tab = FakeTableau(published=False)
    s = run_once(cur, tab, [target("T4TEST-9", [P])], today, e=e)
    check("target not published on server: skipped, no row, not a lookup failure", tab.submitted == [] and not rows(cur, "T4TEST-9")
          and "not a published" in s["skip"]["T4TEST-9"] and s["lookup_failed"] == 0)
    # 10. yesterday's success
    cur.execute("INSERT Integration.Tableau_RefreshJob (DataSource_id, Status, SubmittedDate, FinishedAt) "
                "VALUES ('T4TEST-10', 'Succeeded', DATEADD(day, -1, ?), SYSDATETIMEOFFSET())", today.isoformat())
    tab = FakeTableau()
    run_once(cur, tab, [target("T4TEST-10", [P])], today, e=e)
    check("yesterday's Succeeded row does not block today", len(tab.submitted) == 1)
    # 11. stale refresh: a Succeeded job from before the ETL finished -> refresh again, then fresh -> skip
    cur.execute("INSERT Integration.Tableau_RefreshJob (DataSource_id, TableauJobId, Status, FinishedAt, SubmittedAt) "
                "VALUES ('T4TEST-15', 'old-ok', 'Succeeded', SYSDATETIMEOFFSET(), DATEADD(hour, -2, SYSDATETIMEOFFSET()))")
    tab = FakeTableau()
    run_once(cur, tab, [target("T4TEST-15", [P])], today, e=e)
    check("stale: Succeeded job older than the last ETL end -> refreshed again", len(tab.submitted) == 1
          and [x[2] for x in rows(cur, "T4TEST-15")] == ["Succeeded", "Succeeded"])
    tab = FakeTableau()
    run_once(cur, tab, [target("T4TEST-15", [P])], today, e=e)
    check("stale: after the new refresh nothing more is submitted", tab.submitted == [])
    # 12. another refresh job already queued/running on Tableau (old job, D-017)
    tab = FakeTableau()
    tab.bg = [bg_job("T4TEST-12", "other-1")]
    s = run_once(cur, tab, [target("T4TEST-12", [P])], today, e=e)
    check("already queued/running refresh on Tableau: skipped, nothing written",
          tab.submitted == [] and not rows(cur, "T4TEST-12") and "already queued" in s["skip"]["T4TEST-12"])
    tab.bg = [bg_job("T4TEST-12", "other-1", ended=True)]
    tab.foreign = {"other-1": NS(id="other-1", datasource_id="T4TEST-12", completed_at=dt.datetime.now(dt.timezone.utc), finish_code=1, notes=[])}
    run_once(cur, tab, [target("T4TEST-12", [P])], today, e=e)
    check("an ended FAILED refresh job of someone else does not block", len(tab.submitted) == 1)
    tab = FakeTableau()
    tab.jobs_get_fails = True
    s = run_once(cur, tab, [target("T4TEST-16", [P])], today, e=e)
    check("Tableau job list unreadable: skipped (fail closed), counted as lookup failure, exit 1",
          tab.submitted == [] and s["lookup_failed"] == 1 and m.exit_code(s) == 1)


def foreign_job(tid, jid, minutes_after_etl=5, finish_code=0, ds_id=None, ended=True):
    """A refresh job somebody else submitted (old refresh job, Tableau schedule): (background list item, job detail).
    The test ETL ended 5 minutes ago; minutes_after_etl places the job's creation relative to that."""
    created = (sgt_now() - dt.timedelta(minutes=5)).replace(tzinfo=m.SGT) + dt.timedelta(minutes=minutes_after_etl)
    midnight = dt.datetime.combine(sgt_now().date(), dt.time()).replace(tzinfo=m.SGT)
    created = max(created, midnight + dt.timedelta(seconds=30))   # just after midnight "an hour before the ETL" would be yesterday
    created = created.astimezone(dt.timezone.utc)
    bg = NS(id=jid, title="DS-" + tid, type="RefreshExtracts", created_at=created, ended_at=created if ended else None)
    det = NS(id=jid, datasource_id=ds_id or tid, completed_at=created if ended else None, finish_code=finish_code if ended else None, notes=[])
    return bg, det


def test_adopt(cur):
    today = m.dwh_today(cur)
    P = "CQGlobalSGDW: A"
    e = {P: (sgt_now() - dt.timedelta(minutes=5), True)}

    def case(tid, jobs=(), execute=True, owned=False, unreadable=(), **kw):
        tab = FakeTableau()
        for bg, det in jobs:
            tab.bg.append(bg)
            tab.foreign[bg.id] = det
        tab.unreadable.update(unreadable)
        for u in unreadable:
            tab.foreign.pop(u, None)
        if owned:
            tab.owned_tasks.append(tid)
        s = run_once(cur, tab, [target(tid, [P])], today, execute=execute, e=e, **kw)
        return tab, s

    # --- extracts nobody else refreshes: strict adoption (after the last ETL + margin), else we refresh
    tab, s = case("T4TEST-AD30", [foreign_job("T4TEST-AD30", "old-1")])
    r = rows(cur, "T4TEST-AD30")
    check("adopt: someone's finished refresh after the ETL (+margin) -> nothing submitted", tab.submitted == [] and s["refresh"] == [])
    check("adopt: one Succeeded row with that job's id and a FinishedAt", len(r) == 1 and r[0][2] == "Succeeded" and r[0][1] == "old-1"
          and r[0][3] == 1 and r[0][4] == m.ADOPT_MSG, str([x[:5] for x in r]))
    tab2 = FakeTableau()
    run_once(cur, tab2, [target("T4TEST-AD30", [P])], today, e=e)
    check("adopt: next run sees it as fresh (no refresh, no second row)", tab2.submitted == [] and len(rows(cur, "T4TEST-AD30")) == 1)
    check("adopt: summary line counts it", "adopted=1" in m.summary_line(s, True), m.summary_line(s, True))
    tab, s = case("T4TEST-AD31", [foreign_job("T4TEST-AD31", "old-2")], execute=False)
    check("adopt dry run: decides adopt, writes nothing", tab.submitted == [] and not rows(cur, "T4TEST-AD31") and len(s["adopted"]) == 1)
    tab, s = case("T4TEST-AD32", [foreign_job("T4TEST-AD32", "old-3", minutes_after_etl=-3)])
    check("created BEFORE the ETL ended: not adopted, we refresh", tab.submitted == ["T4TEST-AD32"])
    tab, s = case("T4TEST-AD40", [foreign_job("T4TEST-AD40", "old-9", minutes_after_etl=1)])
    check("created within the clock-skew margin after the ETL: not adopted, we refresh", tab.submitted == ["T4TEST-AD40"])
    tab, s = case("T4TEST-AD33", [foreign_job("T4TEST-AD33", "old-4", finish_code=1)])
    check("other job failed: not adopted, we refresh", tab.submitted == ["T4TEST-AD33"])
    tab, s = case("T4TEST-AD34", [foreign_job("T4TEST-AD34", "old-5", ds_id="some-other-datasource")])
    check("same title but another data source id: not adopted, we refresh", tab.submitted == ["T4TEST-AD34"])
    tab, s = case("T4TEST-AD35", [foreign_job("T4TEST-AD35", "old-6", ended=False)])
    check("other job still running: we wait, nothing submitted, nothing written", tab.submitted == [] and not rows(cur, "T4TEST-AD35")
          and s["waiting"] == 1)
    tab, s = case("T4TEST-AD36")
    check("nobody else refreshes it and no other job: refresh as before", tab.submitted == ["T4TEST-AD36"])
    os.environ["T4_ADOPT"] = "0"
    try:
        tab, s = case("T4TEST-AD37", [foreign_job("T4TEST-AD37", "old-7")])
    finally:
        del os.environ["T4_ADOPT"]
    check("T4_ADOPT=0: adoption off for extracts nobody else owns, we refresh", tab.submitted == ["T4TEST-AD37"] and not s["adopted"])
    bg, det = foreign_job("T4TEST-AD38", "old-8")
    tab, s = case("T4TEST-AD38", [(bg, det)], unreadable=["old-8"])
    check("someone's job detail unreadable: cannot prove there was no refresh, so WAIT (no second refresh)",
          tab.submitted == [] and s["waiting"] == 1 and not rows(cur, "T4TEST-AD38"))
    tab = FakeTableau()
    tab.jobs_get_fails = True
    s = run_once(cur, tab, [target("T4TEST-AD39", [P])], today, e=e)
    check("job list unreadable: fail closed (skip, lookup_failed), nothing submitted", tab.submitted == [] and s["lookup_failed"] >= 1)

    # --- extracts another refresher owns (old job / Tableau schedule): never submitted, whatever ran today is adopted
    tab, s = case("T4TEST-AD50", owned=True)
    check("owned, owner has not refreshed yet: nothing submitted, waiting, nothing written",
          tab.submitted == [] and s["waiting"] == 1 and not rows(cur, "T4TEST-AD50") and "never refreshes it" in s["skip"]["T4TEST-AD50"],
          str(s["skip"]))
    tab, s = case("T4TEST-AD51", [foreign_job("T4TEST-AD51", "own-1", minutes_after_etl=-60)], owned=True)
    r = rows(cur, "T4TEST-AD51")
    check("owned, owner refreshed today BEFORE the ETL: adopted as is (no extra refresh), owner row written",
          tab.submitted == [] and len(r) == 1 and r[0][2] == "Succeeded" and r[0][4].startswith(m.OWNER_PREFIX), str([x[:5] for x in r]))
    tab2 = FakeTableau()
    tab2.owned_tasks.append("T4TEST-AD51")
    tab2.bg = [foreign_job("T4TEST-AD51", "own-1", minutes_after_etl=-60)[0]]
    tab2.foreign = {"own-1": foreign_job("T4TEST-AD51", "own-1", minutes_after_etl=-60)[1]}
    s2 = run_once(cur, tab2, [target("T4TEST-AD51", [P])], today, e=e)
    check("owned, already adopted today: next run neither refreshes nor adopts again", tab2.submitted == [] and len(rows(cur, "T4TEST-AD51")) == 1
          and not s2["adopted"])
    tab, s = case("T4TEST-AD52", [foreign_job("T4TEST-AD52", "own-2", finish_code=1)], owned=True)
    check("owned, owner's refresh FAILED: still never submitted by us (waiting)", tab.submitted == [] and not rows(cur, "T4TEST-AD52"))
    os.environ["T4_ADOPT"] = "0"
    try:
        tab, s = case("T4TEST-AD53", owned=True)
    finally:
        del os.environ["T4_ADOPT"]
    check("owned with T4_ADOPT=0: still never submitted", tab.submitted == [])
    real = m.other_refreshers

    def with_owner(owner):
        def fake(cur_, tab_, today_=None, env=os.environ):
            o, p = real(cur_, tab_, today_, env)
            o.update(owner)
            return o, p
        return fake

    # old job: only ITS refresh counts = created after its own ETL (Etl_Name of the old table) ended
    m.other_refreshers = with_owner({"t4test-ad54": m.Owner("the old refresh job", {P})})
    try:
        tab, s = case("T4TEST-AD54", [foreign_job("T4TEST-AD54", "early-1", minutes_after_etl=-60)])
        check("old job owned: a refresh from BEFORE its ETL ended (e.g. a manual one) is not the old job's: not adopted, never submitted, waiting",
              tab.submitted == [] and not rows(cur, "T4TEST-AD54") and "old refresh job" in s["skip"]["T4TEST-AD54"], str(s["skip"]))
        tab, s = case("T4TEST-AD54", [foreign_job("T4TEST-AD54", "old-ok", minutes_after_etl=1)])
        r = rows(cur, "T4TEST-AD54")
        check("old job owned: its refresh created after its ETL ended is adopted (no margin needed)", tab.submitted == [] and len(r) == 1
              and r[0][1] == "old-ok" and r[0][4].startswith(m.OWNER_PREFIX), str([x[:5] for x in r]))
    finally:
        m.other_refreshers = real
    m.other_refreshers = with_owner({"t4test-ad57": m.Owner("the old refresh job", {"CQGlobalSGDW: SomeOtherETL"})})
    try:
        tab, s = case("T4TEST-AD57", [foreign_job("T4TEST-AD57", "old-x", minutes_after_etl=1)])
        check("old job owned, its ETL has not succeeded today: the old job cannot have run, nothing adopted or submitted",
              tab.submitted == [] and not rows(cur, "T4TEST-AD57"))
    finally:
        m.other_refreshers = real
    # taken over today: soft ownership
    m.other_refreshers = with_owner({"t4test-ad58": m.Owner("its refresher before today's takeover", soft=True),
                                     "t4test-ad59": m.Owner("its refresher before today's takeover", soft=True)})
    try:
        tab, s = case("T4TEST-AD58", [foreign_job("T4TEST-AD58", "prev-1", minutes_after_etl=-60)])
        check("taken over today, the previous refresher already ran today: adopted, no second refresh", tab.submitted == []
              and len(rows(cur, "T4TEST-AD58")) == 1)
        tab, s = case("T4TEST-AD58")
        check("... and the next run does not refresh it either", tab.submitted == [] and len(rows(cur, "T4TEST-AD58")) == 1)
        tab, s = case("T4TEST-AD59")
        check("taken over today, the previous refresher did NOT run today: t4 refreshes it (reports are not held a day)", tab.submitted == ["T4TEST-AD59"])
    finally:
        m.other_refreshers = real
    # fallback list when the old table cannot be read (fake cursor), and 'none'
    import pyodbc

    class Cur:
        def __init__(self, extract_tasks=5):
            self.n, self.r = extract_tasks, []

        def execute(self, sql, *a):
            if "ETL_Tableau_ExtractMapping" in sql:
                raise pyodbc.ProgrammingError("42000", "The SELECT permission was denied")
            self.r = [(self.n,)] if "COUNT(*)" in sql else []
            return self

        def fetchall(self):
            return self.r

        def fetchone(self):
            return self.r[0]
    tab = FakeTableau()
    o, p = m.other_refreshers(Cur(), tab, today, {"OLD_JOB_DATASOURCE_IDS": "AA-1, bb-2"})
    check("old table unreadable: OLD_JOB_DATASOURCE_IDS is used (case-insensitive), no problem", not p and o["aa-1"].text == "the old refresh job" and "bb-2" in o, (o, p))
    o, p = m.other_refreshers(Cur(), tab, today, {})
    check("old table unreadable and no OLD_JOB_DATASOURCE_IDS: a problem (nothing will be submitted)", len(p) == 1 and "OLD_JOB_DATASOURCE_IDS" in p[0], p)
    o, p = m.other_refreshers(Cur(), tab, today, {"OLD_JOB_DATASOURCE_IDS": "none"})
    check("OLD_JOB_DATASOURCE_IDS=none: no old job here, no problem", not p and "aa-1" not in o)
    tab = FakeTableau()
    tab.tasks_empty = True
    o, p = m.other_refreshers(Cur(extract_tasks=5), tab, today, {"OLD_JOB_DATASOURCE_IDS": "none"})
    check("Tableau returns NO extract task while the last sync saw some: treated as a partial read (problem)", any("partial read" in x for x in p), p)
    tab = FakeTableau()
    tab.owned_tasks.append("ds-weekly")
    tab.sched_days = {(today.weekday() + 1) % 7}
    o, _ = m.other_refreshers(Cur(), tab, today, {"OLD_JOB_DATASOURCE_IDS": "none"})
    tab.sched_days = {today.weekday()}
    o2, _ = m.other_refreshers(Cur(), tab, today, {"OLD_JOB_DATASOURCE_IDS": "none"})
    check("a Tableau schedule owns its extract only on the days it fires (weekly: not today -> t4 may refresh; today -> owned)",
          "ds-weekly" not in o and "ds-weekly" in o2)
    # incremental refresh jobs are seen too
    tab = FakeTableau()
    bg, det = foreign_job("T4TEST-AD60", "inc-1")
    bg.type = "IncrementExtract"
    tab.bg, tab.foreign = [bg], {"inc-1": det}
    s = run_once(cur, tab, [target("T4TEST-AD60", [P])], today, e=e)
    check("an incremental refresh job (type IncrementExtract) counts like a full one: adopted", tab.submitted == [] and len(rows(cur, "T4TEST-AD60")) == 1)
    # repeated 'already queued' answers
    for i in range(3):
        tab = FakeTableau()
        tab.already_queued.add("T4TEST-AD61")
        s = run_once(cur, tab, [target("T4TEST-AD61", [P])], today, e=e)
    codes = [a["code"] for a in s["alerts"]]
    check("'already queued' twice: alerted; three times: no further submit, refresh_queued_stuck alert", "refresh_queued_stuck" in codes
          or "refresh_queued_twice" in codes, codes)
    tab = FakeTableau()
    s = run_once(cur, tab, [target("T4TEST-AD61", [P])], today, e=e)
    check("... after 3 'already queued' answers t4 stops submitting for the day (needs a human)", tab.submitted == []
          and "refresh_queued_stuck" in [a["code"] for a in s["alerts"]], str(s["alerts"]))
    bg, det = foreign_job("T4TEST-AD61", "late-ok")
    tab = FakeTableau()
    tab.bg, tab.foreign = [bg], {"late-ok": det}
    s = run_once(cur, tab, [target("T4TEST-AD61", [P])], today, e=e)
    check("... but a refresh someone else finishes later is still adopted (the report is not held all day)",
          tab.submitted == [] and any(r[2] == "Succeeded" and r[1] == "late-ok" for r in rows(cur, "T4TEST-AD61")))
    # soft owner (taken over today) while the other refreshers cannot be read: nothing submitted
    def soft_with_problem(cur_, tab_, today_=None, env=os.environ):
        return {"t4test-ad62": m.Owner("its refresher before today's takeover", soft=True)}, ["cannot read the Tableau extract refresh tasks (Test)"]
    m.other_refreshers = soft_with_problem
    try:
        tab, s = case("T4TEST-AD62")
    finally:
        m.other_refreshers = real
    check("taken over today but the other refreshers could not be read: nothing submitted (no double refresh)",
          tab.submitted == [] and "could not be read" in s["skip"].get("T4TEST-AD62", ""), str(s["skip"]))
    # a data-source task whose schedule cannot be resolved counts as an owner (fail closed)
    tab = FakeTableau()
    tab.owned_tasks.append("ds-nosched")
    tab.no_sched.add("ds-nosched")
    o, p = m.other_refreshers(Cur(), tab, today, {"OLD_JOB_DATASOURCE_IDS": "none"})
    check("a Tableau task without a resolvable schedule still owns its extract (fail closed)", "ds-nosched" in o, (sorted(o), p))
    tab = FakeTableau()
    tab.tasks_fail = True
    s = run_once(cur, tab, [target("T4TEST-AD55", [P])], today, e=e)
    check("other refreshers unreadable: nothing submitted at all, one refresh_owners_unknown alert",
          tab.submitted == [] and [a["code"] for a in s["alerts"]] == ["refresh_owners_unknown"], str(s["alerts"]))

    # --- the t6 side: an owner-adopted row counts as fresh even though it is older than the last ETL
    import t6_send as t6
    g = t6.Gate.__new__(t6.Gate)
    g.today = today
    g.wb = {"w": ("Ready", 1)}
    g.sources = {"w": [{"id": "T4TEST-AD51", "name": "x", "status": "Ready", "kind": "Extract", "target": "T4TEST-AD51"}]}
    g.target_ds = {}
    g.pipes = {"T4TEST-AD51": {P: True}}
    g.etl = {P: (e[P][0], True)}
    g.refreshed = {"T4TEST-AD51": e[P][0] - dt.timedelta(minutes=60)}
    g.owner_refreshed = set()
    ok1 = g.check("w")[0]
    g.owner_refreshed = {"T4TEST-AD51"}
    ok2 = g.check("w")[0]
    check("t6: a refresh older than the ETL holds the report, unless it was adopted from the extract's own refresher",
          ok1 is False and ok2 is True)


def test_run_order(cur):
    """Extracts refresh in name order."""
    today = m.dwh_today(cur)
    P = "CQGlobalSGDW: A"
    e = {P: (sgt_now() - dt.timedelta(minutes=5), True)}
    ids = ["T4TEST-PA", "T4TEST-PB", "T4TEST-PC", "T4TEST-PD"]

    def order(label, want):
        cleanup_jobs = "DELETE FROM Integration.Tableau_RefreshJob WHERE DataSource_id LIKE 'T4TEST-P%'"
        cur.execute(cleanup_jobs)
        tab = FakeTableau()
        run_once(cur, tab, [target(i, [P]) for i in reversed(ids)], today, e=e)
        check("run order: " + label, tab.submitted == want, str(tab.submitted))
    order("by name", ids)
    cleanup(cur)


def busy_job(jid, status, hours_old=0.1, title="Someone else's job", jtype="SubscriptionTask"):
    return NS(id=jid, title=title, type=jtype, status=status, ended_at=None,
              created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_old))


def test_tableau_busy(cur):
    """D-057: wait while Tableau has other people's Pending/InProgress jobs; own jobs and stale Pending jobs do not count."""
    today = m.dwh_today(cur)
    P = "CQGlobalSGDW: A"
    e = {P: (sgt_now() - dt.timedelta(minutes=5), True)}
    saved = {k: os.environ.get(k) for k in ("TABLEAU_BUSY_CHECK", "TABLEAU_BUSY_STALE_HOURS")}
    os.environ.pop("TABLEAU_BUSY_CHECK", None)
    os.environ.pop("TABLEAU_BUSY_STALE_HOURS", None)
    try:
        # busy: 1 queued + 1 running of someone else (a subscription job and a refresh) -> nothing submitted, counted as waiting
        tab = FakeTableau()
        tab.bg = [busy_job("o-1", "Pending"), busy_job("o-2", "InProgress", jtype="RefreshExtracts")]
        s = run_once(cur, tab, [target("T4TEST-30", [P])], today, e=e)
        check("busy: no submit, no row, target counted as waiting, exit 0",
              tab.submitted == [] and not rows(cur, "T4TEST-30") and s["waiting"] == 1 and m.exit_code(s) == 0
              and "Tableau busy (1 queued, 1 running)" in s["skip"]["T4TEST-30"], str(s["skip"]))
        check("busy: summary line shows waiting=1 failed=0", "waiting=1" in m.summary_line(s, True) and "failed=0" in m.summary_line(s, True))
        # dry run reports the same decision
        s = run_once(cur, tab, [target("T4TEST-30", [P])], today, execute=False, e=e)
        check("busy: dry run also waits (would refresh nothing)", s["refresh"] == [] and s["waiting"] == 1)
        # our own recorded job is not 'someone else'
        cur.execute("INSERT Integration.Tableau_RefreshJob (DataSource_id, TableauJobId) VALUES ('T4TEST-31', 'own-1')")
        tab = FakeTableau(polls_to_finish=None)
        tab.polls["own-1"] = 0
        tab.bg = [busy_job("own-1", "InProgress", jtype="RefreshExtracts", title="DS-T4TEST-31")]
        run_once(cur, tab, [target("T4TEST-32", [P])], today, e=e)
        check("own job running: not busy, other target submitted", tab.submitted == ["T4TEST-32"])
        # stale Pending ignored (warning), fresh Pending counts
        tab = FakeTableau()
        tab.bg = [busy_job("old-1", "Pending", hours_old=7)]
        run_once(cur, tab, [target("T4TEST-33", [P])], today, e=e)
        check("Pending job older than 6 h is ignored: submitted", tab.submitted == ["T4TEST-33"])
        tab = FakeTableau()
        tab.bg = [busy_job("old-2", "Pending", hours_old=5)]
        run_once(cur, tab, [target("T4TEST-34", [P])], today, e=e)
        check("Pending job 5 h old still counts: nothing submitted", tab.submitted == [])
        os.environ["TABLEAU_BUSY_STALE_HOURS"] = "4"
        tab = FakeTableau()
        tab.bg = [busy_job("old-3", "Pending", hours_old=5)]
        run_once(cur, tab, [target("T4TEST-35", [P])], today, e=e)
        check("stale age is configurable (4 h): the 5 h old job is ignored", tab.submitted == ["T4TEST-35"])
        os.environ.pop("TABLEAU_BUSY_STALE_HOURS")
        # switch off
        os.environ["TABLEAU_BUSY_CHECK"] = "0"
        tab = FakeTableau()
        tab.bg = [busy_job("o-9", "InProgress")]
        run_once(cur, tab, [target("T4TEST-36", [P])], today, e=e)
        check("TABLEAU_BUSY_CHECK=0 switches the check off", tab.submitted == ["T4TEST-36"])
        os.environ.pop("TABLEAU_BUSY_CHECK")
        # idle: finished jobs of others do not count
        tab = FakeTableau()
        tab.bg = [busy_job("d-1", "Success"), busy_job("d-2", "Failed")]
        run_once(cur, tab, [target("T4TEST-37", [P])], today, e=e)
        check("idle (only finished jobs): normal refresh", tab.submitted == ["T4TEST-37"])
        # job list unreadable: fail closed
        tab = FakeTableau()
        tab.jobs_get_fails = True
        s = run_once(cur, tab, [target("T4TEST-38", [P])], today, e=e)
        check("busy check cannot read the job list: nothing submitted, counted as lookup failure",
              tab.submitted == [] and s["lookup_failed"] == 1)
        # target not ready for an ETL reason never touches the job list
        tab = FakeTableau()
        tab.jobs_get_fails = True
        s = run_once(cur, tab, [target("T4TEST-39", [P, "CQGlobalSGDW: B"])], today, e=e)
        check("targets waiting for their ETL do not need the job list", s["lookup_failed"] == 0 and s["waiting"] == 1)
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_signin_fatal():
    saved = {k: os.environ.get(k) for k in ("TABLEAU_SERVER_URL", "TABLEAU_PAT_NAME", "TABLEAU_PAT_SECRET")}
    os.environ.update(TABLEAU_SERVER_URL="http://127.0.0.1:1", TABLEAU_PAT_NAME="x", TABLEAU_PAT_SECRET="x")  # nothing listens
    lt = m.LazyTableau()
    raised = None
    try:
        lt()
    except BaseException as ex:  # noqa: BLE001
        raised = ex
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    check("sign-in failure is fatal (SystemExit 1, not swallowed by 'except Exception') and leaves no half-set server",
          isinstance(raised, SystemExit) and raised.code == 1 and lt.server is None, repr(raised))


def test_real_reads(cur):
    off = cur.execute("SELECT DATEPART(TZoffset, SYSDATETIMEOFFSET())").fetchone()[0]
    check("DWH clock is UTC+8 (ETL_Master_Log.EndTime treated as Singapore time)", off == 480, str(off))
    # Fixture rows on a day far in the past, inside a transaction that is always rolled back: the real log is pruned and
    # holds nothing old, so the checks must not depend on whatever happens to be on dev.
    fx = dt.date(2001, 3, 5)
    cur.execute("BEGIN TRAN")
    try:
        cur.execute("INSERT Integration.ETL_Master_Log (PipelineName, EndTime, Success) VALUES "
                    "('CQGlobalSGDW: DailyETL', '2001-03-05 11:46:00', 1), ('CQGlobalSGDW: NovaDailyETL', '2001-03-04 23:03:00', 1), "
                    "('CQGlobalSGDW: LeadGenETL', '2001-03-06 09:01:00', 1), ('CQGlobalSGDW: NovaDailyETL', '2001-03-06 10:01:00', 1)")
        e6 = m.etl_status_today(cur, dt.date(2001, 3, 6))
        check("fixture 2001-03-06: LeadGenETL and NovaDailyETL OK, DailyETL absent",
              e6.get("CQGlobalSGDW: LeadGenETL", (0, False))[1] and e6.get("CQGlobalSGDW: NovaDailyETL", (0, False))[1]
              and "CQGlobalSGDW: DailyETL" not in e6, str({k: v[0].strftime("%H:%M") for k, v in e6.items()}))
        e = m.etl_status_today(cur, fx)
        check("fixture ETL_Master_Log 2001-03-05: DailyETL OK 11:46", e.get("CQGlobalSGDW: DailyETL", (None, False))[1]
              and e["CQGlobalSGDW: DailyETL"][0].strftime("%H:%M") == "11:46")
        check("fixture day filter: the previous evening's NovaDailyETL run (23:03) belongs to its own day, not to 2001-03-05",
              "CQGlobalSGDW: NovaDailyETL" not in e
              and "CQGlobalSGDW: NovaDailyETL" in m.etl_status_today(cur, fx - dt.timedelta(days=1)))
    finally:
        cur.execute("ROLLBACK")
    check("fixture ETL_Master_Log rows rolled back", m.etl_status_today(cur, fx) == {})
    check("real ETL_Master_Log far future day: empty", m.etl_status_today(cur, dt.date(2030, 1, 1)) == {})
    nullable = cur.execute("SELECT is_nullable FROM sys.columns WHERE object_id = OBJECT_ID('Integration.ETL_Master_Log') "
                           "AND name = 'EndTime'").fetchone()[0]
    check("ETL_Master_Log.EndTime is NOT NULL (so a failure row always has a day)", nullable == 0)
    # The real mapping, with every registered pipeline known to the ETL log inside a transaction that is always rolled back (dev has no
    # DailyETL row, so today the real extracts show UnknownPipeline; the check must not depend on that).
    cur.execute("BEGIN TRAN")
    try:
        known_pipelines(cur)
        targets, excluded = m.load_targets(cur)
        ids = [t.id for t in targets]
        check("real set: distinct targets (no duplicates), extract only", len(ids) == len(set(ids)) and len(ids) >= 1, "%d targets" % len(ids))
        bad = cur.execute("SELECT COUNT(*) FROM Integration.vw_Tableau_DataSourceReadiness WHERE RefreshKind <> 'Extract' "
                          "AND ExtractRefreshTargetId IS NOT NULL").fetchone()[0]
        check("view gives no refresh target for Live/Unknown sources", bad == 0)
        check("every target has a Ready workbook and a pipeline", all(t.workbooks and t.pipelines for t in targets))
        subs = {r[0] for r in cur.execute("SELECT DISTINCT Workbook_id FROM Integration.Tableau_Subscription WHERE Active_Flag = 1")}
        ready = {r[0] for r in cur.execute("SELECT Workbook_id FROM Integration.vw_Tableau_WorkbookReadiness WHERE WorkbookStatus = 'Ready'")}
        check("target workbooks are all Ready and have an active subscription, suspended or not (D-046)", all(w in subs and w in ready for t in targets for w, _ in t.workbooks))
        check("excluded targets are not in the set", not set(excluded) & set(ids))
    finally:
        cur.execute("ROLLBACK")


def test_exclusion_reasons():
    """Default scope (takeover): an extract excluded only because nothing is taken over says so, not 'not due today'."""
    class Cur:
        def __init__(self, rows):
            self.rows, self.n = rows, 0

        def execute(self, sql, *a):
            self.n += 1
            self.last = sql
            return self

        def fetchall(self):
            return [] if "vw_Tableau_DataSourcePipeline" in self.last else self.rows
    rows = [("T", "D", "extract", "Ready", "W", "wb", "Ready", 0, 2)]    # 0 in-scope subscriptions of 2 active ones
    _, ex = m.load_targets(Cur(rows), "takeover", due_wbs=set())
    check("excluded because nothing is taken over: the reason says so (not 'not due today')",
          "no taken-over subscription" in ex["T"][1] and "due today" not in ex["T"][1], ex)
    _, ex = m.load_targets(Cur([("T", "D", "extract", "Ready", "W", "wb", "Ready", 1, 2)]), "takeover", due_wbs=set())
    check("a taken-over workbook that is simply not due today still says 'not due today'", "due today" in ex["T"][1], ex)
    _, ex = m.load_targets(Cur([("T", "D", "extract", "NotChecked", "W", "wb", "Ready", 1, 2)]), "takeover", due_wbs={"W"})
    check("a NotChecked (file) extract source is not refreshed by t4 (no pipeline to gate on)", "source NotChecked" in ex["T"][1], ex)


def main():
    import testguard  # noqa: E402  dev warehouse only, whatever .env says
    testguard.require_dev()
    os.environ['EVENTLOG_DISABLED'] = '1'
    os.environ.setdefault('LOG_DIR', tempfile.mkdtemp(prefix='logs_'))   # tests never write the real out/*.log
    os.environ['LASTRUN_DIR'] = tempfile.mkdtemp(prefix='lastrun_')   # tests must not write real last-run markers
    os.environ['OPS_STATE_DIR'] = os.environ['LASTRUN_DIR']
    test_decide()
    test_signin_fatal()
    cur = m.connect().cursor()
    cleanup(cur)
    before = others_checksum(cur)
    try:
        test_real_reads(cur)
        test_flow(cur)
        test_run_order(cur)
        test_adopt(cur)
        test_tableau_busy(cur)
        test_exclusion_reasons()
    finally:
        cleanup(cur)
    check("tests did not touch any non-fixture RefreshJob row (count + checksum)", before == others_checksum(cur), str(before))
    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
