"""Checks for the automatic registry, the extract refresh schedules and the extract schedule takeover (D-066 to D-071).
NO write to Tableau and no real mail: a fake Tableau server object (real tableauserverclient TaskItem / ScheduleItem objects) and a fake
mailer stand in. Dev DWH only: every database test runs inside a transaction on its own connection that is ROLLED BACK at the end of the
scenario (fixture rows 'EXTRTEST-%' / schema 'EXTRTEST'); a checksum of the real registry, task and backup tables proves nothing else changed.
The registry derivation also reads the REAL lineage tables and ETL_Master_Log (read-only).

Usage:  python scripts/extract_checks.py
"""
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

import tableauserverclient as TSC
from tableauserverclient.models import TaskItem
from tableauserverclient.models.target import Target

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_takeover as m  # noqa: E402
import extract_tasks as et  # noqa: E402
import registry_sync as rs  # noqa: E402
import schedule_due as sd  # noqa: E402
import t2_mapping_sync as t2  # noqa: E402
import t4_refresh as t4  # noqa: E402
import t6_checks as c6  # noqa: E402  (FakeMailer)
import t6_send as t6  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="extractchecks_"))
DS, TASK, SCHED = "EXTRTEST-DS1", "EXTRTEST-TASK1", "EXTRTEST-S1"
LEAD = "CQGlobalSGDW: LeadGenETL"
fails = []
_n = [0]


def check(name, ok, detail=""):
    print("%s  %s %s" % ("PASS" if ok else "FAIL", name, detail))
    if not ok:
        fails.append(name)


def txn():
    c = t4.connect()
    c.autocommit = False
    return c


def real_checksum(cur):
    return [tuple(cur.execute("SELECT COUNT(*), CHECKSUM_AGG(CHECKSUM(*)) FROM Integration.%s" % t).fetchone())
            for t in ("Tableau_PipelineTable", "Tableau_ExtractTask", "Tableau_ExtractTaskBackup", "Tableau_Schedule", "Tableau_RefreshJob", "ETL_Tableau_ExtractMapping")]


# ---------- registry: the derivation rules (pure) ----------
def E(source, raw, loads=14, **matched):
    e = rs.Entry(source, raw, loads)
    e.matched = {("CQGlobalSGDW: %s" % k): v for k, v in matched.items()}
    return e


DEFAULTS = {"Lineage": "CQGlobalSGDW: DailyETL", "DW_NovaLineage": "CQGlobalSGDW: NovaDailyETL", "OAO_Lineage": LEAD}


def plan_for(tables, entries, registry=None, static=(), schemas=None):
    schemas = schemas or {}
    return rs.make_plan(set(tables), entries, DEFAULTS, registry or {}, set(static), lambda n: schemas.get(n, ["Dimension"]))


def test_names():
    check("parse_name: qualified, suffix, bare",
          rs.parse_name("[Fact].[DailyNovaTradeVolume] - CFD Data") == ("Fact", "DailyNovaTradeVolume")
          and rs.parse_name("[Dimension].[Account]") == ("Dimension", "Account") and rs.parse_name("Client_Bg_K2") == (None, "Client_Bg_K2")
          and rs.parse_name("[Integration].[OAO_Platform_Staging]") == ("Integration", "OAO_Platform_Staging"))


def test_plan_rules():
    p = plan_for([("Dimension", "Client")], [E("Lineage", "Client", DailyETL=14)])
    check("bare lineage name with log evidence for the source default: that is the one candidate", p.insert and p.insert[0][2] == DEFAULTS["Lineage"], p.insert)
    p = plan_for([("Dimension", "Client")], [E("Lineage", "Client")])
    check("ZERO log evidence: UNCONFIRMED, reported, NOT inserted (the default would be a guess)",
          not p.insert and len(p.unconfirmed) == 1 and p.unconfirmed[0][:3] == ("Dimension", "Client", DEFAULTS["Lineage"]), p.unconfirmed)
    p = plan_for([("Dimension", "Client")], [E("Lineage", "Client")], {("Dimension", "Client"): {LEAD: "Auto"}})
    check("ZERO log evidence for a changed pipeline: the Auto row is NOT moved, reported as unconfirmed", not p.remap and len(p.unconfirmed) == 1, (p.remap, p.unconfirmed))
    p = plan_for([("Dimension", "Client")], [E("Lineage", "Client")], {("Dimension", "Client"): {DEFAULTS["Lineage"]: "Auto"}})
    check("ZERO log evidence but an existing Auto row already has that pipeline: unchanged, not touched", len(p.unchanged) == 1 and not p.unconfirmed)
    p = plan_for([("Dimension", "Client")], [E("Lineage", "Client", LeadGenETL=2)])
    check("log matches of ANOTHER pipeline only (the default D was never seen): UNCONFIRMED, not inserted",
          not p.insert and len(p.unconfirmed) == 1 and p.unconfirmed[0][2] == DEFAULTS["Lineage"], (p.insert, p.unconfirmed))
    p = plan_for([("Dimension", "Client")], [E("Lineage", "Client", LeadGenETL=2, DailyETL=1)])
    check("the derived pipeline matched at least once (even with another pipeline matched twice): evidence, inserted", p.insert and not p.unconfirmed, (p.insert, p.unconfirmed))
    p = plan_for([("Dimension", "Client")], [E("Lineage", "Client", LeadGenETL=1)], {("Dimension", "Client"): {LEAD: "Auto"}})
    check("... an Auto row is not moved to a default that was never seen in the log (reported unconfirmed)", not p.remap and len(p.unconfirmed) == 1, (p.remap, p.unconfirmed))
    p = plan_for([("Dimension", "Client_Bg_K2")], [E("Lineage", "Client_Bg_K2", LeadGenETL=14)])
    check("strong log evidence (14 of 14) beats the source default", p.insert and p.insert[0][2] == LEAD and not p.ambiguous, p.insert)
    p = plan_for([("Dimension", "Client_Bg_K2")], [E("Lineage", "Client_Bg_K2", LeadGenETL=6)])
    check("weak log evidence that disagrees with the default: AMBIGUOUS, nothing inserted", not p.insert and len(p.ambiguous) == 1, p.ambiguous)
    p = plan_for([("Dimension", "Client")], [E("Lineage", "Client", LeadGenETL=2, DailyETL=2)])
    check("noise (2 matches of another pipeline) does not beat the default that was itself seen", p.insert and p.insert[0][2] == DEFAULTS["Lineage"])
    p = plan_for([("Fact", "DailyNovaTradeVolume")], [E("DW_NovaLineage", "[Fact].[DailyNovaTradeVolume] - CFD Data", NovaDailyETL=14),
                                                       E("DW_NovaLineage", "[Fact].[DailyNovaTradeVolume] - Fx Data", NovaDailyETL=14)])
    check("five load steps of one table collapse to one pipeline", len(p.insert) == 1 and p.insert[0][2] == DEFAULTS["DW_NovaLineage"], p.insert)
    p = plan_for([("Dimension", "OAO_Platform")], [E("OAO_Lineage", "[Integration].[OAO_Platform_Staging]", LeadGenETL=14)])
    check("OAO staging name stands for the Dimension table of the same name", p.insert and p.insert[0][2] == LEAD, p.insert)
    p = plan_for([("Integration", "OAO_Platform_Staging")], [E("OAO_Lineage", "[Integration].[OAO_Platform_Staging]", LeadGenETL=14)])
    check("the staging table itself matches by its own name", p.insert and p.insert[0][:2] == ("Integration", "OAO_Platform_Staging"), p.insert)
    p = plan_for([("Dimension", "X")], [E("Lineage", "X", DailyETL=14), E("DW_NovaLineage", "[Dimension].[X]", NovaDailyETL=14)])
    check("two lineage tables name it with different pipelines: AMBIGUOUS", len(p.ambiguous) == 1 and not p.insert, p.ambiguous)
    p = plan_for([("Dimension", "Client")], [E("Lineage", "Client")], schemas={"Client": ["Dimension", "Fact"]})
    check("a bare name that exists in two schemas is AMBIGUOUS", len(p.ambiguous) == 1 and not p.insert, p.ambiguous)
    p = plan_for([("Dimension", "Nothing")], [E("Lineage", "Client")])
    check("no lineage entry: reported as without pipeline, nothing inserted", p.unmapped == [("Dimension", "Nothing", "no lineage entry names this table")] and not p.insert)
    p = plan_for([("dbo", "SalesChannel_Excel")], [E("Lineage", "SalesChannel_Excel")], static={("dbo", "SalesChannel_Excel")})
    check("a static table is never derived", not (p.insert or p.ambiguous or p.unmapped))
    reg = {("Dimension", "Client"): {DEFAULTS["Lineage"]: "Manual"}}
    p = plan_for([("Dimension", "Client")], [E("Lineage", "Client")], reg)
    check("manual row agrees: counted, nothing to do", len(p.manual_agree) == 1 and not (p.insert or p.remap or p.manual_differs))
    reg = {("Dimension", "Client"): {LEAD: "Manual"}}
    p = plan_for([("Dimension", "Client")], [E("Lineage", "Client")], reg)
    check("manual row differs from the lineage: REPORTED, never moved", len(p.manual_differs) == 1 and not (p.remap or p.insert or p.drop_auto), p.manual_differs)
    p = plan_for([("Dimension", "Client")], [E("Lineage", "Client", LeadGenETL=6)], reg)
    check("manual row + ambiguous lineage: left alone", not (p.ambiguous or p.remap or p.drop_auto or p.manual_differs))
    reg = {("Dimension", "Client"): {LEAD: "Auto"}}
    p = plan_for([("Dimension", "Client")], [E("Lineage", "Client", DailyETL=14)], reg)
    check("auto row whose derived pipeline changed: MOVE, with old and new", p.remap and p.remap[0][2:4] == (LEAD, DEFAULTS["Lineage"]), p.remap)
    reg = {("Dimension", "Client"): {DEFAULTS["Lineage"]: "Auto"}}
    p = plan_for([("Dimension", "Client")], [E("Lineage", "Client")], reg)
    check("auto row, same pipeline: unchanged", len(p.unchanged) == 1 and not (p.remap or p.insert))
    p = plan_for([("Dimension", "Client")], [E("Lineage", "Client", LeadGenETL=6)], reg)
    check("auto row that became ambiguous: REMOVED (fail closed) and reported", len(p.drop_auto) == 1 and len(p.ambiguous) == 1, p.drop_auto)
    p = plan_for([("Dimension", "Gone")], [], {("Dimension", "Gone"): {LEAD: "Auto"}})
    check("auto row no longer named by lineage: kept, warned", len(p.auto_unconfirmed) == 1 and not p.drop_auto)


# ---------- registry: the real lineage tables ----------
def test_apply_caps():
    """Inserts are mailed once and counted against the per-run cap; the very first run has a higher limit (explicit flag)."""
    plan = plan_for([("EXTRTEST", "T%d" % i) for i in range(8)], [E("Lineage", "T%d" % i, DailyETL=14) for i in range(8)],
                    schemas={"T%d" % i: ["EXTRTEST"] for i in range(8)})
    check("8 new tables with log evidence: 8 inserts planned", len(plan.insert) == 8 and not plan.unconfirmed, len(plan.insert))
    c = txn()
    try:
        cc = c.cursor()
        fake = NS(cursor=lambda: cc, commit=lambda: None, rollback=lambda: None)
        counts, notes = rs.apply(fake, plan)   # default limit
        n = cc.execute("SELECT COUNT(*) FROM Integration.Tableau_PipelineTable WHERE SchemaName = 'EXTRTEST'").fetchone()[0]
        check("inserts count against the cap: only %d are written, the rest is held" % rs.MAX_CHANGES, counts["insert"] == rs.MAX_CHANGES and counts["held"] == 8 - rs.MAX_CHANGES
              and n == rs.MAX_CHANGES, (counts, n))
        check("every applied insert gives one alert line (mailed once like moves and removals)", len(notes) == rs.MAX_CHANGES and all("registered on" in x for x in notes), notes)
        cc.execute("DELETE FROM Integration.Tableau_PipelineTable WHERE SchemaName = 'EXTRTEST'")
        counts, notes = rs.apply(fake, plan, rs.change_limit(True))
        check("the very first run (flag) may apply more: all 8", rs.change_limit(True) > rs.change_limit(False) and counts["insert"] == 8 and counts["held"] == 0, counts)
        check("the flag is an explicit option of the daily sync", "--registry-first-run" in (Path(__file__).resolve().parent / "t2_mapping_sync.py").read_text(encoding="utf-8")
              and "rs.change_limit(" in (Path(__file__).resolve().parent / "t2_mapping_sync.py").read_text(encoding="utf-8"))
    finally:
        c.rollback()
        c.close()


def test_real_derivation(cur):
    tabs = rs.stored_tables(cur)
    before = real_checksum(cur)
    plan = rs.derive(cur, tabs)
    check("real derivation runs on dev and reads every lineage table", not plan.problems, plan.problems)
    check("real derivation: the hand-made registry rows agree with the lineage (>= 20) and none moves", len(plan.manual_agree) >= 20 and not plan.remap, "%d agree" % len(plan.manual_agree))
    check("a dry derivation writes nothing", before == real_checksum(cur))
    c = txn()
    try:
        cc = c.cursor()
        gone = [("Dimension", "Platform"), ("Dimension", "Client_Bg_K2"), ("Dimension", "OAO_Client_Bg")]   # Nova, LeadGen via log timing, OAO via staging name
        for s, t in gone:
            cc.execute("DELETE FROM Integration.Tableau_PipelineTable WHERE SchemaName = ? AND TableName = ?", s, t)
        plan = rs.derive(cc, tabs)
        got = {(s, t): p for s, t, p, _ in plan.insert}
        check("tables with no registry row get a derived pipeline from the REAL lineage: Nova by source, LeadGen K2 by log timing, OAO by staging name",
              got == {("Dimension", "Platform"): "CQGlobalSGDW: NovaDailyETL", ("Dimension", "Client_Bg_K2"): LEAD, ("Dimension", "OAO_Client_Bg"): LEAD}, got)
        counts, notes = rs.apply(NS(cursor=lambda: cc, commit=lambda: None, rollback=lambda: None), plan)
        rows = cc.execute("SELECT SchemaName, TableName, PipelineName, Source FROM Integration.Tableau_PipelineTable WHERE Source = 'Auto'").fetchall()
        check("apply inserts them as Source = 'Auto'", counts["insert"] == 3 and len(rows) == 3 and all(r[3] == "Auto" for r in rows), counts)
        plan2 = rs.derive(cc, tabs)
        check("second derivation after apply: nothing left to do (idempotent)", not (plan2.insert or plan2.remap or plan2.drop_auto) and len(plan2.unchanged) == 3)
        cc.execute("UPDATE Integration.Tableau_PipelineTable SET PipelineName = ? WHERE Source = 'Auto' AND TableName = 'Platform'", LEAD)
        plan3 = rs.derive(cc, tabs)
        check("an Auto row that was moved by hand is moved back, reported", len(plan3.remap) == 1 and plan3.remap[0][3] == "CQGlobalSGDW: NovaDailyETL", plan3.remap)
        counts, notes = rs.apply(NS(cursor=lambda: cc, commit=lambda: None, rollback=lambda: None), plan3)
        check("apply moves it and returns an alert line", counts["move"] == 1 and len(notes) == 1 and "moved from" in notes[0], notes)
        counts, _ = rs.apply(NS(cursor=lambda: cc, commit=lambda: None, rollback=lambda: None), plan3, max_changes=0)
        check("moves and removals beyond the per-run cap are held, not applied", counts["held"] == 1 and counts["move"] == 0, counts)
        man = cc.execute("SELECT COUNT(*) FROM Integration.Tableau_PipelineTable WHERE Source = 'Manual'").fetchone()[0]
        check("manual rows were never touched by any of this", man == cc.execute("SELECT COUNT(*) FROM Integration.Tableau_PipelineTable").fetchone()[0] - 3)
    finally:
        c.rollback()
        c.close()
    check("registry transaction rolled back", before == real_checksum(cur))


# ---------- t4: the extract schedule rule ----------
def fixture_schedule(cc, state="Active", weekdays="Tue,Wed,Thu,Fri,Sat", age_h=0):
    cc.execute("INSERT Integration.Tableau_Schedule (Schedule_id, Name, State, Frequency, WeekDays, StartTime, IntervalHours, LastSeenAt) "
               "VALUES (?, 'EXTRTEST schedule', ?, 'Daily', ?, '09:00:00', 24, DATEADD(hour, -?, SYSDATETIME()))", SCHED, state, weekdays, age_h)


def fixture_task(cc, taken_at="2026-10-20T12:00:00+08:00", neutralised=True):
    cc.execute("INSERT Integration.Tableau_ExtractTask (Task_id, Target_id, TargetType, Schedule_id, Priority, ConsecutiveFailedCount) VALUES (?, ?, 'datasource', ?, 50, 0)", TASK, DS, SCHED)
    cc.execute("INSERT Integration.Tableau_ExtractTaskBackup (Task_id, Target_id, ScheduleId, TakenOver, NeutralisedAt) VALUES (?, ?, ?, 1, %s)"
               % ("CAST(? AS datetimeoffset(0))" if neutralised else "NULL"), *([TASK, DS, SCHED] + ([taken_at] if neutralised else [])))


def fixture_job(cc, day, status="Succeeded"):
    cc.execute("INSERT Integration.Tableau_RefreshJob (DataSource_id, SubmittedAt, SubmittedDate, Status) VALUES (?, CAST(? AS datetimeoffset(0)), ?, ?)",
               DS, "%sT10:00:00+08:00" % day, day, status)


TUE, SUN, MON = dt.date(2026, 11, 3), dt.date(2026, 11, 1), dt.date(2026, 11, 2)


def due_on(cc, day, **kw):
    due, notes = t4.extract_due(cc, day, **kw)
    return DS in due, notes.get(DS, "")


def test_extract_due():
    c = txn()
    try:
        cc = c.cursor()
        fixture_schedule(cc)
        fixture_task(cc)
        v = cc.execute("SELECT TakenOver FROM Integration.vw_Tableau_ExtractSchedule WHERE Target_id = ?", DS).fetchone()
        check("view: a confirmed takeover shows TakenOver = 1", v is not None and v[0] == 1)
        check("taken-over extract, schedule fires on a Tuesday: DUE", due_on(cc, TUE)[0])
        check("Sunday: the Saturday occurrence is still inside its catch-up window: DUE", due_on(cc, SUN)[0])
        check("Monday: last day of the Saturday window: DUE", due_on(cc, MON)[0])
        check("Tuesday with catch-up 0 and a Saturday occurrence only: Tuesday is itself an occurrence", due_on(cc, TUE, catch_up=0)[0])
        check("Sunday with catch-up 0: not due", not due_on(cc, SUN, catch_up=0)[0])
        fixture_job(cc, SUN.isoformat(), "Failed")
        check("a FAILED refresh does not satisfy the occurrence", due_on(cc, MON)[0])
        fixture_job(cc, SUN.isoformat(), "Succeeded")
        ok, note = due_on(cc, MON)
        check("a Succeeded refresh inside the window satisfies it: Monday not due", not ok and "satisfied" in note, note)
        check("... and the Tuesday occurrence is a new one: due again", due_on(cc, TUE)[0])
        cc.execute("DELETE FROM Integration.Tableau_RefreshJob WHERE DataSource_id = ?", DS)
        cc.execute("UPDATE Integration.Tableau_ExtractTaskBackup SET NeutralisedAt = CAST('2026-11-02T12:00:00+08:00' AS datetimeoffset(0)) WHERE Task_id = ?", TASK)
        check("the Saturday occurrence before the takeover belongs to Tableau's old task: Sunday not due", not due_on(cc, SUN)[0])
        check("... but Tuesday after the takeover is due", due_on(cc, TUE)[0])
        cc.execute("UPDATE Integration.Tableau_ExtractTaskBackup SET NeutralisedAt = NULL WHERE Task_id = ?", TASK)
        v = cc.execute("SELECT TakenOver FROM Integration.vw_Tableau_ExtractSchedule WHERE Target_id = ?", DS).fetchone()
        check("an unconfirmed takeover (delete not confirmed) is NOT taken over: no schedule rule", v is not None and v[0] == 0 and not due_on(cc, TUE)[0])
        cc.execute("UPDATE Integration.Tableau_ExtractTaskBackup SET NeutralisedAt = CAST('2026-10-20T12:00:00+08:00' AS datetimeoffset(0)) WHERE Task_id = ?", TASK)
        cc.execute("UPDATE Integration.Tableau_Schedule SET State = 'Suspended' WHERE Schedule_id = ?", SCHED)
        check("a disabled schedule is treated like an enabled one: due on its days", due_on(cc, TUE)[0])
        cc.execute("UPDATE Integration.Tableau_Schedule SET State = 'Active', LastSeenAt = DATEADD(hour, -30, SYSDATETIME()) WHERE Schedule_id = ?", SCHED)
        ok, note = due_on(cc, TUE)
        check("a schedule not synced for 30 h is not trusted (fail closed)", not ok and "synced" in note, note)
        cc.execute("DELETE FROM Integration.Tableau_Schedule WHERE Schedule_id = ?", SCHED)
        ok, note = due_on(cc, TUE)
        check("no schedule row at all: not due", not ok and "no schedule" in note, note)
        cc.execute("UPDATE Integration.Tableau_ExtractTask SET Active_Flag = 0 WHERE Task_id = ?", TASK)
        check("a taken-over task stays in the view after the sync deactivated it (it is gone from Tableau on purpose)",
              cc.execute("SELECT COUNT(*) FROM Integration.vw_Tableau_ExtractSchedule WHERE Target_id = ?", DS).fetchone()[0] == 1)
    finally:
        c.rollback()
        c.close()


class Cur:
    def __init__(self, rows, pipes=()):
        self.rows, self.pipes, self.last = rows, pipes, ""

    def execute(self, sql, *a):
        self.last = sql
        return self

    def fetchall(self):
        return list(self.pipes) if "vw_Tableau_DataSourcePipeline" in self.last else self.rows


def test_load_targets():
    pipes = [("D", "P1", 1)]
    row = lambda rstatus="Ready", wstatus="Ready", active=1: ("T", "D", "extract", rstatus, "W", "wb", wstatus, active, 2)   # noqa: E731
    t, ex = t4.load_targets(Cur([row(wstatus="NotReady", active=0)], pipes), "takeover", due_wbs=set(), sched_due={"T"})
    check("schedule due: the extract is a candidate even when no subscription is due and the workbook is not Ready",
          len(t) == 1 and t[0].via == {"extract schedule"} and t[0].pipelines == {"P1": True}, ex)
    t, ex = t4.load_targets(Cur([row(rstatus="UnmappedTable")], pipes), "takeover", due_wbs=set(), sched_due={"T"})
    check("schedule due but the source is not Ready: still fail closed", not t and "source UnmappedTable" in ex["T"][1], ex)
    t, ex = t4.load_targets(Cur([row()], pipes), "takeover", due_wbs={"W"}, sched_due=set())
    check("subscription rule unchanged: due subscription makes it a candidate", len(t) == 1 and t[0].via == {"due subscription"})
    t, ex = t4.load_targets(Cur([row()], pipes), "takeover", due_wbs=set(), sched_due=set(), sched_notes={"T": "Tableau extract schedule: satisfied"})
    check("neither due: excluded, with both reasons", not t and "due today" in ex["T"][1] and "satisfied" in ex["T"][1], ex)
    t, ex = t4.load_targets(Cur([row()], pipes), "takeover", due_wbs={"W"})
    check("no schedule arguments = old behaviour", len(t) == 1 and t[0].via == {"due subscription"})
    s = {"results": {}, "refresh": ["a"], "skip": {}, "waiting": 0, "lookup_failed": 0, "scheduled": 1}
    check("RUN SUMMARY counts the extracts due by their schedule", "due_by_extract_schedule=1" in t4.summary_line(s, False))


# ---------- t2: reading and storing the tasks ----------
def task_item(tid, target, sched, ttype="datasource", priority=50, failed=0):
    return TaskItem(tid, "extractRefresh", priority, failed, sched, None, None, Target(target, ttype))


def sched_item(sid="SCH-1", state="Active", days=("Tuesday", "Wednesday")):
    it = TSC.ScheduleItem("S " + sid, 50, TSC.ScheduleItem.Type.Extract, TSC.ScheduleItem.ExecutionOrder.Parallel,
                          TSC.DailyInterval(dt.time(9, 0), *days))
    it._id, it._state = sid, state
    return it


class NotFound(Exception):
    code = "404"


class FakeServer:
    def __init__(self, tasks, scheds=("SCH-1",), has_extracts=True, owner="OWNER-1"):
        self.items = {t.id: t for t in tasks}
        self.scheds = {s: sched_item(s) for s in scheds}
        self.deleted, self.added, self.fail_delete, self.delete_extra, self.on_delete, self.seq = [], [], None, None, None, 0
        self.tasks = NS(get=self._get, delete=self._delete)
        self.schedules = NS(get_by_id=self._sched, add_to_schedule=self._add)
        self.datasources = NS(get_by_id=lambda i: NS(id=i, name="DS " + i, has_extracts=has_extracts, owner_id=owner))
        self.refused = False

    def __call__(self):
        return self

    def _get(self, opts=None):
        return list(self.items.values()), NS(total_available=None)

    def _sched(self, i):
        if i not in self.scheds:
            raise NotFound()
        return self.scheds[i]

    def _delete(self, tid, *a):
        if self.on_delete:
            self.on_delete(tid)
        if self.fail_delete:
            raise self.fail_delete
        self.deleted.append(tid)
        self.items.pop(tid, None)
        if self.delete_extra:
            self.items.pop(self.delete_extra, None)

    def _add(self, sched, datasource=None, **kw):
        if self.refused:
            return ["refused"]
        self.seq += 1
        self.added.append((sched, datasource.id))
        t = task_item("NEW-%d" % self.seq, datasource.id, sched)
        self.items[t.id] = t
        return []


def test_t2_reading():
    srv = FakeServer([task_item("A", "D1", "SCH-1"), task_item("B", "W1", "SCH-1", "workbook")])
    tasks, scheds, problems = et.read_tasks(srv)
    check("read_tasks: tasks (data source and workbook) and their schedules", len(tasks) == 2 and set(scheds) == {"SCH-1"} and not problems
          and scheds["SCH-1"].weekdays == {1, 2} and tasks[1]["ttype"] == "workbook")
    srv = FakeServer([task_item("A", "D1", "SCH-1"), task_item("B", "D2", "SCH-X")])
    tasks, scheds, problems = et.read_tasks(srv)
    check("a schedule that cannot be read is a problem and left out, the other work goes on", len(tasks) == 2 and set(scheds) == {"SCH-1"} and len(problems) == 1, problems)
    cur = NS(execute=lambda sql, *a: NS(fetchone=lambda: (3,), fetchall=lambda: []))
    probs = []
    ds_rows = {"D1": ("D1", "x", None, 1)}
    stats = {"schedules": {}}
    res = t2.read_extract_tasks(cur, FakeServer([]), ds_rows, stats, probs)
    check("an empty answer while tasks are stored is a partial read: nothing is written", res is None and probs and "partial" in probs[0][2], probs)
    probs = []
    res = t2.read_extract_tasks(NS(execute=lambda sql, *a: NS(fetchone=lambda: (0,), fetchall=lambda: [])), FakeServer([task_item("A", "D1", "SCH-1")]), ds_rows, stats, probs)
    check("read_extract_tasks returns the tasks and adds the schedules to the sync's schedule set", res and len(res) == 1 and "SCH-1" in stats["schedules"], probs)

    class Boom:
        tasks = NS(get=lambda o=None: (_ for _ in ()).throw(RuntimeError("boom")))
    probs = []
    check("a failing task read does not stop the sync: problem + None", t2.read_extract_tasks(cur, Boom, ds_rows, {"schedules": {}}, probs) is None and probs)


def test_t2_merge():
    c = txn()
    try:
        cc = c.cursor()
        cc.execute("CREATE TABLE #task (Task_id nvarchar(128), Target_id nvarchar(128), TargetType varchar(12), Schedule_id nvarchar(128), Priority int, Failed int)")
        cc.execute("INSERT Integration.Tableau_ExtractTask (Task_id, Target_id, TargetType, Schedule_id) VALUES ('EXTRTEST-OLD', 'x', 'datasource', 's')")
        cc.execute("INSERT Integration.Tableau_ExtractTask (Task_id, Target_id, TargetType, Schedule_id) VALUES ('EXTRTEST-OWN', 'x', 'datasource', 's')")
        cc.execute("INSERT Integration.Tableau_ExtractTaskBackup (Task_id, Target_id, ScheduleId, TakenOver, NeutralisedAt) VALUES ('EXTRTEST-OWN', 'x', 's', 1, SYSDATETIMEOFFSET())")
        cc.execute("INSERT #task VALUES ('EXTRTEST-NEW', 'x', 'datasource', 's', 7, 2)")
        acts = [r[0] for r in cc.execute(t2.MERGE_SQL["task"]).fetchall()]
        st = {r[0]: r[1] for r in cc.execute("SELECT Task_id, Active_Flag FROM Integration.Tableau_ExtractTask WHERE Task_id LIKE 'EXTRTEST-%'").fetchall()}
        check("t2 MERGE: new task inserted, vanished task deactivated, a taken-over task stays active",
              st == {"EXTRTEST-NEW": 1, "EXTRTEST-OLD": 0, "EXTRTEST-OWN": 1} and "INSERT" in acts, st)
    finally:
        c.rollback()
        c.close()


# ---------- the takeover ----------
def fixture_candidate(cc, pipeline=LEAD, state="Active", synced_h=0):
    cc.execute("INSERT Integration.Tableau_DataSource (DataSource_id, DataSource_Name, IsExtract) VALUES (?, 'EXTRTEST extract', 1)", DS)
    cc.execute("INSERT Integration.Tableau_DataSourceTable (DataSource_id, SchemaName, TableName) VALUES (?, 'EXTRTEST', 'T1')", DS)
    cc.execute("INSERT Integration.Tableau_WorkbookDataSource (Workbook_id, Workbook_Name, DataSource_id) VALUES ('EXTRTEST-WB1', 'EXTRTEST workbook', ?)", DS)
    cc.execute("INSERT Integration.Tableau_PipelineTable (PipelineName, SchemaName, TableName, Evidence) VALUES (?, 'EXTRTEST', 'T1', 'test')", pipeline)
    cc.execute("INSERT Integration.Tableau_ExtractTask (Task_id, Target_id, TargetType, Schedule_id, Priority, ConsecutiveFailedCount, LastSeenAt) "
               "VALUES (?, ?, 'datasource', ?, 50, 0, DATEADD(hour, -?, SYSDATETIME()))", TASK, DS, SCHED, synced_h)
    fixture_schedule(cc, state)


def cfg(execute=True, **kw):
    _n[0] += 1
    alert = t6.Config(execute=execute, alert_state=TMP / ("alerts%d.json" % _n[0]), cutoff=dt.time(23, 59, 59), local_release=False)
    base = dict(execute=execute, ids=frozenset({DS.lower()}), owner_user_ids=frozenset(), backup_dir=TMP / "backup", id_like="EXTRTEST-%", alert=alert)
    base.update(kw)
    return m.Config(**base)


def go(cc, server, **kw):
    ctx = m.Ctx(cfg(**kw), server, cc, TUE)
    mail = c6.FakeMailer()
    m.run(ctx, mail)
    return ctx, mail


def backup(cc):
    return cc.execute("SELECT TakenOver, CAST(NeutralisedAt AS datetime2), CAST(RestoredAt AS datetime2), NewTaskId, ScheduleId, Priority, ConsecutiveFailedCount, TargetName, OwnerUserId "
                      "FROM Integration.Tableau_ExtractTaskBackup WHERE Task_id = ?", TASK).fetchall()


def fresh_server(**kw):
    return FakeServer([task_item(TASK, DS, SCHED, priority=25, failed=3), task_item("OTHER-1", "OTHERDS", SCHED)], scheds=(SCHED,), **kw)


def test_takeover():
    class NoTableau:
        def __call__(self):
            raise AssertionError("Tableau must not be touched")

    def scenario(body):
        c = txn()
        try:
            cc = c.cursor()
            fixture_candidate(cc)
            body(cc)
        finally:
            c.rollback()
            c.close()

    def dry(cc):
        srv = fresh_server()
        ctx, _ = go(cc, srv, execute=False)
        check("dry run: would take over, deletes nothing, writes no backup row", ctx.n["would_take_over"] == 1 and not srv.deleted and backup(cc) == [], dict(ctx.n))
    scenario(dry)

    def empty(cc):
        ctx = m.Ctx(cfg(execute=True, ids=frozenset()), NoTableau(), cc, TUE)
        m.run(ctx, c6.FakeMailer())
        check("empty allow-list: nothing is taken over and Tableau is not even signed in to", ctx.n["neutralised"] == 0 and ctx.n["candidates"] == 1)
    scenario(empty)

    def full(cc):
        srv = fresh_server()
        seen = {}
        srv.on_delete = lambda tid: seen.update(backup=len(backup(cc)), neutralised=(backup(cc) or [(0, 1)])[0][1])
        ctx, _ = go(cc, srv)
        b = backup(cc)
        check("execute: the backup row was committed BEFORE the delete, and the delete confirmed afterwards",
              seen == {"backup": 1, "neutralised": None} and srv.deleted == [TASK] and b and b[0][1] is not None, (seen, b))
        check("backup row holds the definition (schedule, priority 25, failed 3, owner), no recipients", b[0][4] == SCHED and b[0][5] == 25 and b[0][6] == 3 and b[0][8] == "OWNER-1")
        files = list((TMP / "backup").glob("*%s*.json" % TASK))
        js = json.loads(files[-1].read_text(encoding="utf-8")) if files else {}
        check("JSON backup written, with the schedule definition", js.get("schedule_id") == SCHED and js["schedule"]["frequency"] == "Daily" and js["priority"] == 25)
        check("only the one task is gone (the other task of the schedule is untouched)", set(srv.items) == {"OTHER-1"})
        v = cc.execute("SELECT TakenOver FROM Integration.vw_Tableau_ExtractSchedule WHERE Target_id = ?", DS).fetchone()
        check("view now says TakenOver = 1: t4's schedule rule applies", v and v[0] == 1)
        ctx2, _ = go(cc, srv)
        check("a second run changes nothing (already neutralised, no second delete)", ctx2.n["already_neutralised"] == 1 and srv.deleted == [TASK] and len(backup(cc)) == 1, dict(ctx2.n))
        line = m.summary_line(ctx)
        check("RUN SUMMARY line", "neutralised=1" in line and "needs_human=0" in line, line)
    scenario(full)

    def fail_then_retry(cc):
        srv = fresh_server()
        srv.fail_delete = RuntimeError("boom")
        ctx, mail = go(cc, srv)
        b = backup(cc)
        check("delete fails: backup row stays unconfirmed, failure counted, alert event raised, exit 1",
              ctx.n["failed"] == 1 and b and b[0][1] is None and m.exit_code(ctx) == 1 and any(e["code"] == "takeover_failed" for e in ctx.events), dict(ctx.n))
        v = cc.execute("SELECT TakenOver FROM Integration.vw_Tableau_ExtractSchedule WHERE Target_id = ?", DS).fetchone()
        check("... and t4's schedule rule stays OFF (the task is still in Tableau)", v and v[0] == 0)
        srv.fail_delete = None
        ctx2, _ = go(cc, srv)
        b = backup(cc)
        check("next run retries the delete and confirms", ctx2.n["retry_delete"] == 1 and srv.deleted == [TASK] and b[0][1] is not None and len(b) == 1, dict(ctx2.n))
    scenario(fail_then_retry)

    def crash_after_delete(cc):
        srv = fresh_server()
        ctx, _ = go(cc, srv, execute=True)
        cc.execute("UPDATE Integration.Tableau_ExtractTaskBackup SET NeutralisedAt = NULL WHERE Task_id = ?", TASK)   # crash before the confirm
        ctx2, _ = go(cc, srv)
        check("deleted in Tableau but not confirmed: the next run just confirms, no second delete", ctx2.n["confirmed"] == 1 and srv.deleted == [TASK] and backup(cc)[0][1] is not None)
    scenario(crash_after_delete)

    def extra(cc):
        srv = fresh_server()
        srv.items["THIRD-1"] = task_item("THIRD-1", "THIRDDS", SCHED)
        srv.delete_extra = "OTHER-1"
        ctx, _ = go(cc, srv)
        check("a delete that removes another task too is reported as a failure", ctx.n["failed"] == 1 and any("more than" in e["text"] for e in ctx.events), ctx.events)
    scenario(extra)

    def disabled(cc):
        srv = fresh_server()
        srv.scheds[SCHED]._state = "Suspended"
        ctx, _ = go(cc, srv)
        check("a task on a disabled schedule is taken over exactly like one on an enabled schedule", ctx.n["neutralised"] == 1 and srv.deleted == [TASK] and len(backup(cc)) == 1, dict(ctx.n))
        check("... no alert item is raised for it", not ctx.events and not [x for x in t4.taken_over_problems(cc, TUE) if x[0] == DS and x[2] != "takeover_missed"])
        due, _n = t4.extract_due(cc, dt.date(2026, 11, 10))   # a Tuesday after the takeover (which happens 'now')
        check("... t4 computes its due days from the schedule's recurrence (Tuesday is a day of the fixture schedule)", DS in due)
        ctx2 = m.Ctx(cfg(execute=True), srv, cc, TUE)
        failed = m.restore(ctx2, [TASK], False)
        new = [t for t in srv.items.values() if t.id.startswith("NEW-")]
        check("restore re-creates the task on the same schedule", failed == 0 and srv.added == [(SCHED, DS)] and len(new) == 1 and new[0].schedule_id == SCHED, (srv.added, failed))
    scenario(disabled)

    def skips(cc):
        srv = fresh_server()
        srv.scheds[SCHED] = TSC.ScheduleItem("odd", 50, TSC.ScheduleItem.Type.Extract, TSC.ScheduleItem.ExecutionOrder.Parallel, TSC.WeeklyInterval(dt.time(9)))
        srv.scheds[SCHED]._id, srv.scheds[SCHED]._state = SCHED, "Active"
        ctx, _ = go(cc, srv)
        check("a schedule t4 cannot evaluate (weekly without weekdays) is not taken over", ctx.n["skip_schedule_unsupported"] == 1 and not srv.deleted)
        srv = fresh_server(owner="SOMEONE-ELSE")
        ctx, _ = go(cc, srv, owner_user_ids=frozenset({"owner-1"}))
        check("owner fence: a data source of another owner is not taken over", ctx.n["skip_not_owner"] == 1 and not srv.deleted)
        srv = fresh_server(has_extracts=False)
        ctx, _ = go(cc, srv)
        check("a data source without an extract is not taken over", ctx.n["skip_no_extract"] == 1 and not srv.deleted)
        srv = fresh_server()
        ctx, _ = go(cc, srv, max_new=0)
        check("per-run cap: nothing taken over, event raised", ctx.n["skip_cap"] == 1 and not srv.deleted and any(e["code"] == "takeover_cap" for e in ctx.events))
        srv = fresh_server()
        srv.items[TASK] = task_item(TASK, DS, "SOME-OTHER-SCHEDULE")
        ctx, _ = go(cc, srv)
        check("a task that differs from the last sync (schedule changed) is not taken over", ctx.n["skip_definition_changed"] == 1 and not srv.deleted)
        srv = fresh_server()
        ctx, _ = go(cc, srv, ids=frozenset({"some-other-datasource"}))
        check("not in the allow-list: untouched, not even looked at", ctx.n["candidates"] == 1 and not srv.deleted and backup(cc) == [] and ctx.n["new_attempts"] == 0)
    scenario(skips)

    def stale(cc):
        cc.execute("UPDATE Integration.Tableau_ExtractTask SET LastSeenAt = DATEADD(hour, -30, SYSDATETIME()) WHERE Task_id = ?", TASK)
        srv = fresh_server()
        ctx, _ = go(cc, srv)
        check("a task the sync has not seen for 30 h is not taken over", ctx.n["skip_stale"] == 1 and not srv.deleted)
    scenario(stale)

    def unknown(cc):
        cc.execute("UPDATE Integration.Tableau_PipelineTable SET PipelineName = 'CQGlobalSGDW: NoSuchETL' WHERE SchemaName = 'EXTRTEST'")
        srv = fresh_server()
        ctx, _ = go(cc, srv)
        check("an extract t4 could not refresh (pipeline unknown to the ETL log) keeps its Tableau task", ctx.n["skip_not_refreshable"] == 1 and not srv.deleted)
    scenario(unknown)

    def not_mapped(cc):
        cc.execute("INSERT Integration.Tableau_ExtractTask (Task_id, Target_id, TargetType, Schedule_id, LastSeenAt) VALUES ('EXTRTEST-TASK9', 'EXTRTEST-NOTMAPPED', 'datasource', ?, SYSDATETIME())", SCHED)
        cc.execute("INSERT Integration.Tableau_ExtractTask (Task_id, Target_id, TargetType, Schedule_id, LastSeenAt) VALUES ('EXTRTEST-TASK8', 'EXTRTEST-WB', 'workbook', ?, SYSDATETIME())", SCHED)
        srv = fresh_server()
        ctx, _ = go(cc, srv, ids=frozenset({"extrtest-notmapped", "extrtest-wb"}))
        check("an extract that is not in the mapping and a workbook task are never candidates", ctx.n["candidates"] == 1 and not srv.deleted)
    scenario(not_mapped)

    def restore_flow(cc):
        srv = fresh_server()
        go(cc, srv)
        ctx = m.Ctx(cfg(execute=False), srv, cc, TUE)
        m.restore(ctx, [TASK], False)
        check("restore dry run: nothing created", not srv.added and ctx.n["restore_targets"] == 1 and backup(cc)[0][2] is None)
        ctx = m.Ctx(cfg(execute=True), srv, cc, TUE)
        failed = m.restore(ctx, [TASK], False)
        b = backup(cc)
        new = [t for t in srv.items.values() if t.id.startswith("NEW-")]
        check("restore re-creates exactly one task on the same schedule for the same data source and marks the row restored (with the new id)",
              failed == 0 and srv.added == [(SCHED, DS)] and len(new) == 1 and b[0][2] is not None and b[0][3] == new[0].id, (srv.added, b))
        row = cc.execute("SELECT Active_Flag, Schedule_id FROM Integration.Tableau_ExtractTask WHERE Task_id = ?", new[0].id).fetchone()
        check("the new task is stored for the sync, the old row is inactive",
              row and row[0] == 1 and row[1] == SCHED and cc.execute("SELECT Active_Flag FROM Integration.Tableau_ExtractTask WHERE Task_id = ?", TASK).fetchone()[0] == 0)
        check("after the restore the view no longer says taken over (t4 back to the subscription rule)",
              cc.execute("SELECT COUNT(*) FROM Integration.vw_Tableau_ExtractSchedule WHERE Target_id = ? AND TakenOver = 1", DS).fetchone()[0] == 0)
        ctx = m.Ctx(cfg(execute=True), srv, cc, TUE)
        m.restore(ctx, [TASK], False)
        check("restoring again is safe: no second task", len(srv.added) == 1)
    scenario(restore_flow)

    def restore_adopt_and_fail(cc):
        srv = fresh_server()
        go(cc, srv)
        srv.items["HUMAN-1"] = task_item("HUMAN-1", DS, SCHED)   # a person already re-created it by hand
        ctx = m.Ctx(cfg(execute=True), srv, cc, TUE)
        m.restore(ctx, [TASK], False)
        check("restore adopts an existing task for the same extract and schedule instead of creating a duplicate", not srv.added and backup(cc)[0][3] == "HUMAN-1", backup(cc))
    scenario(restore_adopt_and_fail)

    def restore_fails(cc):
        srv = fresh_server()
        go(cc, srv)
        srv.scheds.clear()
        ctx = m.Ctx(cfg(execute=True), srv, cc, TUE)
        failed = m.restore(ctx, [TASK], False)
        check("restore when the schedule is gone fails loudly and keeps the row OPEN", failed == 1 and backup(cc)[0][2] is None and any(e["code"] == "restore_failed" for e in ctx.events))
        srv.scheds[SCHED] = sched_item(SCHED)
        srv.refused = True
        ctx = m.Ctx(cfg(execute=True), srv, cc, TUE)
        failed = m.restore(ctx, [TASK], False)
        check("restore refused by Tableau keeps the row open", failed == 1 and backup(cc)[0][2] is None)
        ctx = m.Ctx(cfg(execute=True), srv, cc, TUE)
        failed = m.restore(ctx, ["EXTRTEST-NO-SUCH"], False)
        check("restore of an id that was never taken over is refused", failed == 1)
    scenario(restore_fails)


OLD = "Integration.ETL_Tableau_ExtractMapping"


def old_fixture(cc):
    """Old-job rows: two active rows of our data source, one inactive row of it (a person switched it off earlier), one active row of ANOTHER data source."""
    for etl, ds, flag in (("EXTRTEST-ETL-A", DS, 1), ("EXTRTEST-ETL-B", DS, 1), ("EXTRTEST-ETL-C", DS, 0), ("EXTRTEST-ETL-A", "EXTRTEST-OTHERDS", 1)):
        cc.execute("INSERT %s (Etl_Name, DataSource_id, DataSource_Name, Active_Flag, Refresh_Frequency) VALUES (?, ?, 'EXTRTEST', ?, 'DAILY')" % OLD, etl, ds, flag)


def old_flags(cc):
    return {(r[0], r[1]): r[2] for r in cc.execute("SELECT DataSource_id, Etl_Name, CAST(Active_Flag AS int) FROM %s WHERE Etl_Name LIKE 'EXTRTEST-%%'" % OLD).fetchall()}


def old_backup(cc):
    return cc.execute("SELECT OldJobMappingIds, OldJobOriginalFlag, CAST(OldJobSwitchedAt AS datetime2), CAST(OldJobRestoredAt AS datetime2) "
                      "FROM Integration.Tableau_ExtractTaskBackup WHERE Task_id = ?", TASK).fetchone()


def test_old_job():
    """The old refresh job's rows are switched off with the takeover and back on with the restore."""
    A, B, C, O = (DS, "EXTRTEST-ETL-A"), (DS, "EXTRTEST-ETL-B"), (DS, "EXTRTEST-ETL-C"), ("EXTRTEST-OTHERDS", "EXTRTEST-ETL-A")
    START = {A: 1, B: 1, C: 0, O: 1}

    def with_old(body):
        c = txn()
        try:
            cc = c.cursor()
            fixture_candidate(cc)
            old_fixture(cc)
            body(cc)
        finally:
            c.rollback()
            c.close()

    def dry(cc):
        srv = fresh_server()
        ctx, _ = go(cc, srv, execute=False)
        check("dry run: the old job's rows are listed, nothing is changed", ctx.n["would_take_over"] == 1 and old_flags(cc) == START and backup(cc) == [])
    with_old(dry)

    def full(cc):
        srv = fresh_server()
        seen = {}
        srv.on_delete = lambda tid: seen.update(flags=old_flags(cc), bk=old_backup(cc))
        ctx, _ = go(cc, srv)
        ob = old_backup(cc)
        check("takeover switches off exactly the active old-job rows of the taken-over data source (the inactive row and the other data source stay)",
              old_flags(cc) == {A: 0, B: 0, C: 0, O: 1} and ctx.n["old_job_off"] == 2, (old_flags(cc), dict(ctx.n)))
        check("... Mapping_ids and original value (1) are in the backup row, written BEFORE the Tableau delete",
              ob[0] and len(ob[0].split(",")) == 2 and ob[1] == 1 and ob[2] is not None and ob[3] is None and seen["flags"][A] == 0 and seen["bk"][0] == ob[0], (ob, seen))
        js = json.loads(sorted((TMP / "backup").glob("*%s*.json" % TASK))[-1].read_text(encoding="utf-8"))
        check("... and in the JSON backup", js.get("old_job_mapping_ids") == ob[0] and js.get("old_job_original_flag") == 1)
        ctx2, _ = go(cc, srv)
        check("a second run changes nothing (idempotent)", ctx2.n["old_job_off"] == 0 and old_flags(cc) == {A: 0, B: 0, C: 0, O: 1} and old_backup(cc)[0] == ob[0])
        line = m.summary_line(ctx)
        check("RUN SUMMARY names the old-job rows switched off", "old_job_off=2" in line, line)
        cc.execute("INSERT %s (Etl_Name, DataSource_id, DataSource_Name, Active_Flag, Refresh_Frequency) VALUES ('EXTRTEST-ETL-D', ?, 'x', 1, 'DAILY')" % OLD, DS)
        ctx3, _ = go(cc, srv)
        check("a row added later for a taken-over data source is switched off by the next run and appended to the stored list",
              ctx3.n["old_job_off"] == 1 and old_flags(cc)[(DS, "EXTRTEST-ETL-D")] == 0 and len(old_backup(cc)[0].split(",")) == 3 and old_flags(cc)[O] == 1)
        ctx4 = m.Ctx(cfg(execute=True), srv, cc, TUE)
        failed = m.restore(ctx4, [TASK], False)
        check("restore sets exactly the stored rows back to their original value; the row a person had switched off and the other data source stay",
              failed == 0 and old_flags(cc) == {A: 1, B: 1, C: 0, O: 1, (DS, "EXTRTEST-ETL-D"): 1} and ctx4.n["old_job_back"] == 3, (old_flags(cc), dict(ctx4.n)))
        ob2 = old_backup(cc)
        check("... OldJobRestoredAt is set together with RestoredAt", ob2[3] is not None and backup(cc)[0][2] is not None)
        cc.execute("UPDATE %s SET Active_Flag = 0 WHERE DataSource_id = ? AND Etl_Name = 'EXTRTEST-ETL-A'" % OLD, DS)
        ctx5 = m.Ctx(cfg(execute=True), srv, cc, TUE)
        m.restore(ctx5, [TASK], False)
        check("restoring again is a no-op: a row changed after the restore is not touched", old_flags(cc)[A] == 0 and ctx5.n["old_job_back"] == 0)
        cc.execute("UPDATE %s SET Active_Flag = 1 WHERE DataSource_id = ? AND Etl_Name = 'EXTRTEST-ETL-A'" % OLD, DS)
        ctx6, _ = go(cc, srv, id_like="%")
        check("NoRetake: the restored data source is not taken over again, so its old-job rows stay on",
              ctx6.n["skip_restored"] == 1 and ctx6.n["old_job_off"] == 0 and old_flags(cc)[A] == 1 and old_flags(cc)[B] == 1)
    with_old(full)

    def no_row(cc):
        srv = fresh_server()
        cc.execute("DELETE FROM %s WHERE Etl_Name LIKE 'EXTRTEST-%%'" % OLD)
        ctx, _ = go(cc, srv)
        check("no old-job row for the data source: taken over, nothing else done", ctx.n["neutralised"] == 1 and ctx.n["old_job_off"] == 0 and old_backup(cc)[0] is None)
        ctx2 = m.Ctx(cfg(execute=True), srv, cc, TUE)
        check("... and the restore then touches no old-job row", m.restore(ctx2, [TASK], False) == 0 and ctx2.n["old_job_back"] == 0)
    with_old(no_row)

    def absent(cc):
        srv = fresh_server()
        ctx = m.Ctx(cfg(execute=True), srv, cc, TUE)
        ctx.old_job = "absent"
        m.run(ctx, c6.FakeMailer())
        check("old table absent: the extract is taken over, nothing is touched there", ctx.n["neutralised"] == 1 and old_flags(cc) == START and old_backup(cc)[0] is None)
    with_old(absent)

    def refused(cc):
        for state in ("no_permission", "unexpected"):
            srv = fresh_server()
            ctx = m.Ctx(cfg(execute=True), srv, cc, TUE)
            ctx.old_job = state
            m.run(ctx, c6.FakeMailer())
            check("old table %s: a NEW takeover is refused (the old job would keep refreshing), alert event, nothing deleted or written" % state,
                  ctx.n["failed"] == 1 and not srv.deleted and backup(cc) == [] and old_flags(cc) == START and any(e["code"] == "takeover_failed" for e in ctx.events))
    with_old(refused)

    def legacy(cc):
        srv = fresh_server()
        taken_over_fixture(cc)    # an open takeover row from before this feature: no old-job columns
        del srv.items[TASK]
        ctx, _ = go(cc, srv)
        check("an open takeover from before this feature gets its old-job rows switched off on the next run",
              old_flags(cc) == {A: 0, B: 0, C: 0, O: 1} and old_backup(cc)[0] is not None, (old_flags(cc), dict(ctx.n)))
    with_old(legacy)

    def restore_needs_table(state):
        def body(cc):
            srv = fresh_server()
            go(cc, srv)   # takeover with the table usable: rows switched off, ids stored
            ctx = m.Ctx(cfg(execute=True), srv, cc, TUE)
            ctx.old_job = state
            failed = m.restore(ctx, [TASK], False)
            check("old table %s while rows are switched off: restore FAILS, the backup row stays OPEN, alert event, rows stay as they are" % state,
                  failed == 1 and backup(cc)[0][2] is None and any(e["code"] == "restore_failed" for e in ctx.events) and old_flags(cc)[A] == 0, (failed, state))
            ctx2 = m.Ctx(cfg(execute=True), srv, cc, TUE)
            check("... and once the table is usable again the same restore completes", m.restore(ctx2, [TASK], False) == 0 and old_flags(cc)[A] == 1 and backup(cc)[0][2] is not None)
        return body
    for st in ("absent", "no_permission", "unexpected"):
        with_old(restore_needs_table(st))

    def restore_absent_nothing_stored(cc):
        srv = fresh_server()
        cc.execute("DELETE FROM %s WHERE Etl_Name LIKE 'EXTRTEST-%%'" % OLD)
        go(cc, srv)   # nothing switched off, nothing stored
        ctx = m.Ctx(cfg(execute=True), srv, cc, TUE)
        ctx.old_job = "absent"
        check("table absent and nothing was switched off: restore may proceed", m.restore(ctx, [TASK], False) == 0 and backup(cc)[0][2] is not None)
    with_old(restore_absent_nothing_stored)

    def reconcile_alert(cc):
        srv = fresh_server()
        go(cc, srv)
        for state in ("no_permission", "unexpected"):
            ctx = m.Ctx(cfg(execute=True), srv, cc, TUE)
            ctx.old_job = state
            m.run(ctx, c6.FakeMailer())
            check("open takeover and the old table became %s: alert event old_job_unusable, exit code 1" % state,
                  any(e["code"] == "old_job_unusable" for e in ctx.events) and m.exit_code(ctx) == 1)
    with_old(reconcile_alert)

    def atomic(cc):
        ctx = m.Ctx(cfg(execute=True), fresh_server(), cc, TUE)
        cand = m.Cand(TASK, DS, SCHED, 50, 0, None, "x", True, 1, True)
        raised = False
        try:
            m.write_backup(ctx, cand, "x", "o", NS(name="x"), NS(priority="not-a-number", consecutive_failed_count=0, last_run_at=None))
        except Exception:
            raised = True
        check("a failing backup insert leaves the old job's rows alone (one transaction)", raised and old_flags(cc) == START)
    with_old(atomic)


def scen(body):
    c = txn()
    try:
        cc = c.cursor()
        fixture_candidate(cc)
        body(cc)
    finally:
        c.rollback()
        c.close()


def taken_over_fixture(cc):
    cc.execute("INSERT Integration.Tableau_ExtractTaskBackup (Task_id, Target_id, ScheduleId, TargetName, TakenOver, NeutralisedAt) "
               "VALUES (?, ?, ?, 'EXTRTEST extract', 1, CAST('2026-10-20T12:00:00+08:00' AS datetimeoffset(0)))", TASK, DS, SCHED)


def mine(problems):
    return [x for x in problems if x[0] == DS]


def test_takeover_hardening():
    # --- 4: restore is durable against the daily run ---
    def no_retake(cc):
        srv = fresh_server()
        go(cc, srv)
        ctx = m.Ctx(cfg(execute=True), srv, cc, TUE)
        m.restore(ctx, [TASK], False)
        row = cc.execute("SELECT NoRetake, CAST(RestoredAt AS datetime2) FROM Integration.Tableau_ExtractTaskBackup WHERE Task_id = ?", TASK).fetchone()
        check("restore marks the backup row NoRetake = 1", row and row[0] == 1 and row[1] is not None)
        new = [t for t in srv.items.values() if t.id.startswith("NEW-")][0]
        cc.execute("UPDATE Integration.Tableau_ExtractTask SET LastSeenAt = SYSDATETIME() WHERE Task_id = ?", new.id)
        n_deleted = len(srv.deleted)
        ctx2, _ = go(cc, srv, id_like="%")   # the id is still in the allow-list, the restored task (new id) is live and synced
        check("the daily run does NOT take the restored extract over again while its id is still in the allow-list",
              ctx2.n["skip_restored"] == 1 and len(srv.deleted) == n_deleted and ctx2.n["new_attempts"] == 0 and new.id in srv.items, dict(ctx2.n))
        cc.execute("UPDATE Integration.Tableau_ExtractTaskBackup SET NoRetake = 0 WHERE Task_id = ?", TASK)
        ctx3, _ = go(cc, srv, id_like="%")
        check("after a person clears NoRetake it can be taken over again", ctx3.n["neutralised"] == 1 and new.id not in srv.items, dict(ctx3.n))
    scen(no_retake)

    # --- 5: a retried delete refuses when the live task changed since the backup ---
    def retry_changed(cc):
        srv = fresh_server()
        srv.fail_delete = RuntimeError("boom")
        go(cc, srv)
        srv.fail_delete = None
        srv.items[TASK] = task_item(TASK, DS, "SOME-OTHER-SCHEDULE")   # a person moved the task to another schedule meanwhile
        ctx, _ = go(cc, srv)
        b = backup(cc)
        check("retried delete REFUSED when the live schedule differs from the backup: no delete, alert event, failure counted, row still unconfirmed",
              not srv.deleted and ctx.n["failed"] == 1 and any(e["code"] == "takeover_changed" for e in ctx.events) and b[0][1] is None and TASK in srv.items, dict(ctx.n))
        srv.items[TASK] = task_item(TASK, "ANOTHER-DS", SCHED)
        ctx, _ = go(cc, srv)
        check("... and when the live target differs", not srv.deleted and ctx.n["failed"] == 1)
    scen(retry_changed)

    def retry_gone_on_reread(cc):
        srv = fresh_server()
        srv.fail_delete = RuntimeError("boom")
        go(cc, srv)
        srv.fail_delete = None
        srv.items.pop(TASK)   # a person deleted it meanwhile
        ctx, _ = go(cc, srv)
        check("a retried delete finds the task already gone: it is confirmed, nothing deleted", ctx.n["confirmed"] == 1 and not srv.deleted and backup(cc)[0][1] is not None, dict(ctx.n))
    scen(retry_gone_on_reread)

    # --- 6: an empty answer while tasks exist must not confirm anything ---
    def empty_answer(cc):
        srv = fresh_server()
        srv.fail_delete = RuntimeError("boom")
        go(cc, srv)
        srv.fail_delete = None
        cc.execute("INSERT Integration.Tableau_ExtractTask (Task_id, Target_id, TargetType, Schedule_id) VALUES ('EXTRTEST-TASK7', 'x', 'datasource', ?)", SCHED)   # another stored task
        srv.items.clear()   # a transient empty task list
        try:
            go(cc, srv)
            raised = False
        except RuntimeError as e:
            raised = "partial read" in str(e)
        check("read_live: an empty task list while tasks are stored raises (partial read), nothing is confirmed", raised and backup(cc)[0][1] is None)
    scen(empty_answer)

    def last_task(cc):
        srv = FakeServer([task_item(TASK, DS, SCHED)], scheds=(SCHED,))
        ctx, _ = go(cc, srv)
        check("deleting the only task of the site legitimately leaves an empty list: confirmed", ctx.n["neutralised"] == 1 and srv.deleted == [TASK], dict(ctx.n))
    scen(last_task)

    # --- 6: restore adoption matches target AND schedule; a task on another schedule is only warned about ---
    def adopt_other_schedule(cc):
        srv = fresh_server()
        go(cc, srv)
        srv.scheds["OTHER-SCHED"] = sched_item("OTHER-SCHED")
        srv.items["HUMAN-2"] = task_item("HUMAN-2", DS, "OTHER-SCHED")
        import logging
        seen = []

        class H(logging.Handler):
            def emit(self, record):
                seen.append(record.getMessage())
        h = H()
        m.log.addHandler(h)
        try:
            ctx = m.Ctx(cfg(execute=True), srv, cc, TUE)
            m.restore(ctx, [TASK], False)
        finally:
            m.log.removeHandler(h)
        check("restore does not adopt a task on ANOTHER schedule: it creates one on the original schedule and warns",
              srv.added == [(SCHED, DS)] and backup(cc)[0][3] != "HUMAN-2" and any("ANOTHER schedule" in x for x in seen), (srv.added, seen))
    scen(adopt_other_schedule)

    # --- 3: a table from a young Auto registry row holds the takeover back ---
    def young(cc):
        cc.execute("UPDATE Integration.Tableau_PipelineTable SET Source = 'Auto' WHERE SchemaName = 'EXTRTEST'")   # ConfirmedAt = now
        srv = fresh_server()
        ctx, _ = go(cc, srv)
        check("an extract with a table from an Auto registry row younger than 1 day is NOT taken over yet", ctx.n["skip_young_auto"] == 1 and not srv.deleted and backup(cc) == [], dict(ctx.n))
        cc.execute("UPDATE Integration.Tableau_PipelineTable SET ConfirmedAt = DATEADD(day, -2, SYSDATETIME()) WHERE SchemaName = 'EXTRTEST'")
        ctx, _ = go(cc, srv)
        check("... the same Auto row two days old: taken over", ctx.n["neutralised"] == 1 and srv.deleted == [TASK], dict(ctx.n))
    scen(young)

    def manual_young(cc):
        srv = fresh_server()   # a Manual row (the default) is never held back, however new
        ctx, _ = go(cc, srv)
        check("a Manual registry row is never held back", ctx.n["skip_young_auto"] == 0 and ctx.n["neutralised"] == 1)
    scen(manual_young)


def test_orphans():
    """2: a taken-over extract that nothing refreshes is alerted once per day."""
    def prep(cc):
        taken_over_fixture(cc)
        fixture_job(cc, SUN.isoformat())   # the Saturday occurrence has its successful refresh

    def healthy(cc):
        prep(cc)
        check("healthy taken-over extract: no problem", mine(t4.taken_over_problems(cc, TUE)) == [], mine(t4.taken_over_problems(cc, TUE)))
    scen(healthy)

    def missed(cc):
        taken_over_fixture(cc)
        p = mine(t4.taken_over_problems(cc, TUE))
        check("no successful refresh on the last due occurrence (window ended yesterday): takeover_missed", len(p) == 1 and p[0][2] == "takeover_missed", p)
        fixture_job(cc, SUN.isoformat(), "Failed")
        check("... a FAILED refresh does not count", [x[2] for x in mine(t4.taken_over_problems(cc, TUE))] == ["takeover_missed"])
    scen(missed)

    def left_mapping(cc):
        prep(cc)
        cc.execute("DELETE FROM Integration.Tableau_WorkbookDataSource WHERE DataSource_id = ?", DS)
        p = mine(t4.taken_over_problems(cc, TUE))
        check("the data source left the mapping: takeover_orphaned (not a t4 candidate, would be invisible to the t4 view)",
              len(p) == 1 and p[0][2] == "takeover_orphaned" and "not in the mapping" in p[0][3], p)
    scen(left_mapping)

    def unmapped(cc):
        prep(cc)
        cc.execute("DELETE FROM Integration.Tableau_PipelineTable WHERE SchemaName = 'EXTRTEST'")   # the registry removed the row
        p = mine(t4.taken_over_problems(cc, TUE))
        check("the registry row was removed (UnmappedTable): takeover_orphaned with the reason", len(p) == 1 and p[0][2] == "takeover_orphaned" and "UnmappedTable" in p[0][3], p)
    scen(unmapped)

    def unknown(cc):
        prep(cc)
        cc.execute("UPDATE Integration.Tableau_PipelineTable SET PipelineName = 'CQGlobalSGDW: NoSuchETL' WHERE SchemaName = 'EXTRTEST'")
        p = mine(t4.taken_over_problems(cc, TUE))
        check("the pipeline became unknown to the ETL log: takeover_orphaned", len(p) == 1 and p[0][2] == "takeover_orphaned", p)
    scen(unknown)

    def sched_bad(cc):
        prep(cc)
        cc.execute("UPDATE Integration.Tableau_Schedule SET LastSeenAt = DATEADD(hour, -30, SYSDATETIME()) WHERE Schedule_id = ?", SCHED)
        p = mine(t4.taken_over_problems(cc, TUE))
        check("stale schedule (never due): takeover_schedule", len(p) == 1 and p[0][2] == "takeover_schedule", p)
        cc.execute("UPDATE Integration.Tableau_Schedule SET LastSeenAt = SYSDATETIME(), State = 'Suspended' WHERE Schedule_id = ?", SCHED)
        p = mine(t4.taken_over_problems(cc, TUE))
        check("a disabled schedule raises no alert (state is not looked at)", p == [], p)
    scen(sched_bad)

    def restored_not_listed(cc):
        taken_over_fixture(cc)
        cc.execute("UPDATE Integration.Tableau_ExtractTaskBackup SET RestoredAt = SYSDATETIMEOFFSET() WHERE Task_id = ?", TASK)
        check("a restored extract (Tableau owns the task again) is not checked", mine(t4.taken_over_problems(cc, TUE)) == [])
    scen(restored_not_listed)

    def alert_once_a_day(cc):
        taken_over_fixture(cc)
        srv = fresh_server()
        c1 = cfg(execute=True, ids=frozenset())
        mail = c6.FakeMailer()
        ctx = m.Ctx(c1, srv, cc, TUE)
        m.run(ctx, mail)
        mine_ev = [e for e in ctx.events if e["sub"] == DS]
        check("the daily run raises an alert event for the orphan (also with an empty allow-list) and counts it in RUN SUMMARY",
              len(mine_ev) == 1 and mine_ev[0]["code"] == "takeover_missed" and ctx.n["orphaned"] >= 1 and "orphaned=" in m.summary_line(ctx), mine_ev)
        sent1 = len(mail.sent)
        ctx = m.Ctx(c1, srv, cc, TUE)
        m.run(ctx, mail)
        check("one alert mail on the first run, none for the same item on the second run of the day", sent1 == 1 and len(mail.sent) == 1, (sent1, len(mail.sent)))
        ctx = m.Ctx(c1, srv, cc, TUE + dt.timedelta(days=1))
        m.run(ctx, mail)
        check("... and again on the next day while the problem lasts", len(mail.sent) == 2, len(mail.sent))
    scen(alert_once_a_day)

    def dry_run_alert(cc):
        taken_over_fixture(cc)
        srv = fresh_server()
        c1 = cfg(execute=False, ids=frozenset())   # a dry run with an empty allow-list: how the scheduled task runs until it is enabled
        mail = c6.FakeMailer()
        ctx = m.Ctx(c1, srv, cc, TUE)
        m.run(ctx, mail)
        check("D-078: the orphan alert is mailed in a DRY RUN with an empty allow-list (through the alert mailer, nothing deleted in Tableau)",
              len(mail.sent) == 1 and ctx.n["orphaned"] >= 1 and not srv.deleted, (len(mail.sent), dict(ctx.n)))
        check("... and an orphan makes the exit code 1 (counted like the other needs_human items)", m.exit_code(ctx) == 1 and "needs_human=" in m.summary_line(ctx))
        ctx = m.Ctx(c1, srv, cc, TUE)
        m.run(ctx, mail)
        check("... once per day also in a dry run", len(mail.sent) == 1)
        ctx = m.Ctx(c1, srv, cc, TUE)
        ctx.event("T", "x", "takeover_cap", "a dry-run item that is not an orphan")
        check("... but other dry-run items are not mailed", m.send_alerts(ctx, c6.FakeMailer()) == 0)
    scen(dry_run_alert)

    def no_orphan_no_mail(cc):
        prep(cc)
        mail = c6.FakeMailer()
        ctx = m.Ctx(cfg(execute=False, ids=frozenset()), fresh_server(), cc, TUE)
        m.run(ctx, mail)
        check("a healthy takeover: no mail, exit 0", not mail.sent and m.exit_code(ctx) == 0, dict(ctx.n))
    scen(no_orphan_no_mail)

    # t4 lists taken-over extracts that are excluded in its log and RUN SUMMARY
    def t4_lists(cc):
        prep(cc)
        cc.execute("DELETE FROM Integration.Tableau_WorkbookDataSource WHERE DataSource_id = ?", DS)
        import logging
        seen = []

        class H(logging.Handler):
            def emit(self, record):
                seen.append(record.getMessage())
        h = H()
        t4.log.addHandler(h)
        t4.log.setLevel(logging.INFO)
        try:
            s = t4.run(cc, NS(), execute=False, today=TUE, ds_like="EXTRTEST-%")
        finally:
            t4.log.removeHandler(h)
        line = t4.summary_line(s, False)
        check("t4 logs the taken-over extract that is not a candidate, and counts it in RUN SUMMARY",
              any("TAKEN OVER but not a candidate" in x and DS in x for x in seen) and s["taken_over_excluded"] >= 1 and "taken_over_excluded=" in line, line)
    scen(t4_lists)


def test_backup_schedules():
    """1: the schedule of a taken-over task (no live task on it any more) is still read, stays fresh and due."""
    def body(cc):
        fixture_schedule(cc, age_h=30)
        fixture_task(cc)
        cc.execute("UPDATE Integration.Tableau_ExtractTask SET Active_Flag = 0 WHERE Task_id = ?", TASK)
        check("before the fix: a schedule synced 30 h ago makes the taken-over extract never due", not due_on(cc, TUE)[0])
        ds_rows = {"OTHERDS": ("OTHERDS", "x", None, 1)}

        def read(srv):
            probs, stats = [], {"schedules": {}}
            res = t2.read_extract_tasks(cc, srv, ds_rows, stats, probs)
            return res, stats, probs
        srv = FakeServer([task_item("OTHER-1", "OTHERDS", "SCH-1")], scheds=("SCH-1", SCHED))   # no live task on SCHED any more
        res, stats, probs = read(srv)
        check("the daily sync reads the schedule of an open backup row although no live task uses it", res is not None and SCHED in stats["schedules"] and not probs, probs)
        cc.execute("CREATE TABLE #sched (Schedule_id nvarchar(128), Name nvarchar(200), State varchar(12), Frequency varchar(10), WeekDays varchar(40), "
                   "MonthDays varchar(80), StartTime time(0), IntervalHours decimal(6,2))")
        cc.executemany("INSERT #sched VALUES (?,?,?,?,?,?,?,?)", [sd.to_row(x) for x in stats["schedules"].values()])
        cc.execute(t2.MERGE_SQL["sched"]).fetchall()
        age = cc.execute("SELECT DATEDIFF(minute, LastSeenAt, SYSDATETIME()) FROM Integration.Tableau_Schedule WHERE Schedule_id = ?", SCHED).fetchone()[0]
        check("... so Tableau_Schedule.LastSeenAt is refreshed (written by the sync's schedule MERGE)", age is not None and age <= 5, age)
        ok, note = due_on(cc, TUE)
        check("... and the taken-over extract is due again on the schedule's day", ok, note)
        # the schedule is not read twice when a live task uses it too
        calls = []
        srv2 = FakeServer([task_item("A", "OTHERDS", SCHED)], scheds=(SCHED,))
        real = srv2.schedules.get_by_id
        srv2.schedules.get_by_id = lambda i: (calls.append(i), real(i))[1]
        read(srv2)
        check("a schedule used by a live task AND an open backup row is read once", calls == [SCHED], calls)
        # the schedule disappeared
        res, stats, probs = read(FakeServer([task_item("OTHER-1", "OTHERDS", "SCH-1")], scheds=("SCH-1",)))
        check("a schedule that disappeared from Tableau is REPORTED as a problem (and not stored)",
              len(probs) == 1 and "taken-over" in probs[0][0] and probs[0][1] == SCHED and "cannot be read" in probs[0][2] and SCHED not in stats["schedules"], probs)
        # the schedule is disabled: not a problem
        srv3 = FakeServer([task_item("OTHER-1", "OTHERDS", "SCH-1")], scheds=("SCH-1", SCHED))
        srv3.scheds[SCHED]._state = "Suspended"
        res, stats, probs = read(srv3)
        check("a disabled schedule of a taken-over task is read and stored, no problem", not probs and SCHED in stats["schedules"], probs)
        cc.execute("DELETE FROM Integration.Tableau_ExtractTaskBackup WHERE Task_id = ?", TASK)
        res, stats, probs = read(FakeServer([task_item("OTHER-1", "OTHERDS", "SCH-1")], scheds=("SCH-1",)))
        check("without an open backup row nothing extra is read or reported", SCHED not in stats["schedules"] and not probs, probs)
    c = txn()
    try:
        body(c.cursor())
    finally:
        c.rollback()
        c.close()


def test_empty_site():
    """D-078: a task we took over stays Active_Flag = 1 but is gone from Tableau on purpose: it must not make an empty answer a 'partial read',
    and the schedule of an open takeover is read even when the task read fails or comes back empty."""
    ds_rows = {"OTHERDS": ("OTHERDS", "x", None, 1)}

    def read(cc, srv):
        probs, stats = [], {"schedules": {}}
        return t2.read_extract_tasks(cc, srv, ds_rows, stats, probs), stats, probs

    def body(cc):
        cc.execute("DELETE FROM Integration.Tableau_ExtractTask")   # inside this rolled-back transaction: the test does not depend on the real tasks
        cc.execute("DELETE FROM Integration.Tableau_ExtractTaskBackup")
        fixture_schedule(cc, age_h=30)
        fixture_task(cc)   # active task row + OPEN confirmed takeover row
        res, stats, probs = read(cc, FakeServer([], scheds=(SCHED,)))
        check("every remaining task is one we took over: an empty site is accepted (not a partial read), nothing is reported",
              res == [] and not probs, (res, probs))
        check("... and the schedule of the taken-over task is still read (LastSeenAt would otherwise go stale)", SCHED in stats["schedules"], list(stats["schedules"]))
        cc.execute("INSERT Integration.Tableau_ExtractTask (Task_id, Target_id, TargetType, Schedule_id) VALUES ('EXTRTEST-OTHER', 'x', 'datasource', ?)", SCHED)
        res, stats, probs = read(cc, FakeServer([], scheds=(SCHED,)))
        check("an empty site while a task that is NOT taken over is stored is still a partial read: None + problem",
              res is None and len(probs) == 1 and "partial" in probs[0][2], probs)
        check("... and even then the schedule of the open takeover is read", SCHED in stats["schedules"], list(stats["schedules"]))

        class Boom:
            tasks = NS(get=lambda o=None: (_ for _ in ()).throw(RuntimeError("boom")))
            schedules = NS(get_by_id=lambda i: sched_item(i))
        res, stats, probs = read(cc, Boom)
        check("a failing task read: None + problem, and the schedule of the open takeover is read anyway",
              res is None and len(probs) == 1 and SCHED in stats["schedules"], probs)
        cc.execute("DELETE FROM Integration.Tableau_ExtractTask WHERE Task_id = 'EXTRTEST-OTHER'")
        # restore on an empty site
        fixture_candidate_rows = cc.execute("SELECT COUNT(*) FROM Integration.Tableau_ExtractTaskBackup WHERE Task_id = ? AND RestoredAt IS NULL", TASK).fetchone()[0]
        srv = FakeServer([], scheds=(SCHED,))
        ctx = m.Ctx(cfg(execute=True), srv, cc, TUE)
        failed = m.restore(ctx, [TASK], False)
        b = backup(cc)
        check("restore on an EMPTY site (the one task was ours): the task is re-created and the row closed",
              fixture_candidate_rows == 1 and failed == 0 and srv.added == [(SCHED, DS)] and b[0][2] is not None, (srv.added, b))
    c = txn()
    try:
        body(c.cursor())
    finally:
        c.rollback()
        c.close()


def test_fixture_guard():
    """D-078: check_fixtures.known_pipelines refuses to run outside an open transaction (it would insert permanent ETL log rows)."""
    import pyodbc
    from check_fixtures import known_pipelines
    c = t4.connect()
    c.autocommit = True
    try:
        cur = c.cursor()
        try:
            known_pipelines(cur)
            refused = False
        except pyodbc.Error as e:
            refused = "needs an open transaction" in str(e)
        n = cur.execute("SELECT COUNT(*) FROM Integration.ETL_Master_Log WHERE EndTime = '2001-01-01'").fetchone()[0]
        check("known_pipelines without an open transaction THROWS and inserts nothing", refused and n == 0, n)
        cur.execute("BEGIN TRAN")
        try:
            known_pipelines(cur)
            ok = True
        finally:
            cur.execute("IF @@TRANCOUNT > 0 ROLLBACK")
        check("known_pipelines inside BEGIN TRAN runs (and is rolled back)", ok and cur.execute("SELECT COUNT(*) FROM Integration.ETL_Master_Log WHERE EndTime = '2001-01-01'").fetchone()[0] == n)
    finally:
        c.close()


def test_requests():
    body = TSC.server.request_factory.RequestFactory.Schedule.add_datasource_req("DS-LUID", "extractRefresh").decode()
    check("offline: the library's add-to-schedule request carries the data source id and the extractRefresh type", "DS-LUID" in body and "extractRefresh" in body, body[:200])
    # The real library call path: a real tableauserverclient Server whose HTTP session only records the request (nothing leaves this process).
    calls = []

    class Resp:
        status_code, headers, content, text, encoding, url, is_redirect, history, ok = 204, {}, b"", "", None, "x", False, [], True

    class Sess:
        def delete(self, url, **kw):
            calls.append(("DELETE", url))
            return Resp()

        def get(self, *a, **kw):
            calls.append(("GET", a[0]))
            raise AssertionError("only a delete is expected")
    real = TSC.Server("http://example.invalid", use_server_version=False)
    real.version, real._site_id, real._auth_token, real._session = "3.25", "SITE", "TOKEN", Sess()
    srv = FakeServer([task_item(TASK, DS, SCHED), task_item("OTHER-1", "OTHERDS", SCHED)], scheds=(SCHED,))
    srv.tasks = NS(get=srv._get, delete=real.tasks.delete)   # list = fake, delete = the library's real delete call
    ctx = m.Ctx(cfg(), srv, None, TUE)
    m.read_live(ctx)
    srv.items.pop(TASK)   # what Tableau does on the DELETE the library sends
    bad = m.delete_and_verify(ctx, TASK)
    check("real tasks.delete path: exactly one HTTP DELETE of /tasks/extractRefreshes/<task id>, nothing else", bad is None
          and calls == [("DELETE", "http://example.invalid/api/3.25/sites/SITE/tasks/extractRefreshes/" + TASK)], calls)
    rec = FakeServer([task_item(TASK, DS, SCHED), task_item("OTHER-1", "OTHERDS", SCHED)], scheds=(SCHED,))
    ctx = m.Ctx(cfg(), rec, None, TUE)
    m.read_live(ctx)
    m.delete_and_verify(ctx, TASK)
    check("the takeover calls tasks.delete with the task id, once", rec.deleted == [TASK])


def test_config():
    ap = NS(datasource_ids=None, owner_user_id=None, restore=None, restore_all=False, execute=True, ses=False, local_smtp=True, max_new=None, max_mapping_age_h=26.0)
    for name, kw, bad in (("--execute with names instead of luids", dict(datasource_ids=["my extract"], owner_user_id=["u"]), True),
                          ("--execute with an allow-list and no owner fence", dict(datasource_ids=["8bb1fcad-ed2a-4c14-8b9b-7b70f4f7be1f"]), True),
                          ("--execute with luid + owner fence", dict(datasource_ids=["8bb1fcad-ed2a-4c14-8b9b-7b70f4f7be1f"], owner_user_id=["u"]), False),
                          ("--execute with an empty allow-list needs no fence", dict(), False),
                          ("negative cap", dict(datasource_ids=["8bb1fcad-ed2a-4c14-8b9b-7b70f4f7be1f"], owner_user_id=["u"], max_new=-1), True),
                          ("--smtp without --execute", dict(execute=False, ses=True, local_smtp=False), True)):
        a = NS(**{**ap.__dict__, **kw})
        try:
            m.build_config(a, {"TABLEAU_SERVER_URL": "http://x", "ALERT_TO": "a@example.com"})
            got = False
        except t6.ConfigError:
            got = True
        check("config: " + name, got == bad)


def test_sql_files():
    root = Path(__file__).resolve().parent / "sql"
    up, down = (root / "ExtractAuto_schema.sql").read_text(encoding="utf-8"), (root / "ExtractAuto_schema_rollback.sql").read_text(encoding="utf-8")
    check("SQL files carry the dev-server guard like the others", all("<DEV_DWH_SERVER>" in x and "SET NOEXEC ON" in x for x in (up, down)))
    c = txn()
    try:
        c.cursor().execute("DELETE FROM Integration.Tableau_ExtractTaskBackup")   # inside this transaction only (rolled back): the test does not depend on real rows
        for b in re.split(r"^\s*GO\s*$", down, flags=re.M):
            if b.strip():
                c.cursor().execute(b)
        gone = c.cursor().execute("SELECT COUNT(*) FROM sys.tables WHERE name IN ('Tableau_ExtractTask', 'Tableau_ExtractTaskBackup', 'Tableau_LineageSource')").fetchone()[0]
        col = c.cursor().execute("SELECT COL_LENGTH('Integration.Tableau_PipelineTable', 'Source')").fetchone()[0]
        check("the rollback script runs (backup table emptied inside the test transaction) and removes the objects (rolled back here)", gone == 0 and col is None)
    except Exception as e:
        check("the rollback script runs", False, repr(e)[:200])
    finally:
        c.rollback()
        c.close()
    c = txn()
    try:
        cc = c.cursor()
        cc.execute("DELETE FROM Integration.Tableau_ExtractTaskBackup")   # inside the transaction only
        cc.execute("INSERT Integration.Tableau_ExtractTaskBackup (Task_id, Target_id, ScheduleId, TakenOver) VALUES ('EXTRTEST-RB', 'x', 's', 1)")
        refused = False
        try:
            for b in re.split(r"^\s*GO\s*$", down, flags=re.M):
                if b.strip():
                    cc.execute(b)
        except Exception as e:
            refused = "Open extract takeover rows" in str(e)
        check("the rollback script REFUSES while a takeover is open", refused)
    finally:
        c.rollback()
        c.close()


def main():
    os.environ["EVENTLOG_DISABLED"] = "1"
    os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="logs_"))
    os.environ["LASTRUN_DIR"] = tempfile.mkdtemp(prefix="lastrun_")
    os.environ["OPS_STATE_DIR"] = os.environ["LASTRUN_DIR"]
    cur = t4.connect().cursor()
    before = real_checksum(cur)
    test_names()
    test_plan_rules()
    test_real_derivation(cur)
    test_apply_caps()
    test_extract_due()
    test_load_targets()
    test_t2_reading()
    test_t2_merge()
    test_takeover()
    test_takeover_hardening()
    test_orphans()
    test_backup_schedules()
    test_empty_site()
    test_fixture_guard()
    test_requests()
    test_config()
    test_sql_files()
    test_old_job()
    check("tests changed no real row of the registry, task, backup, schedule or refresh-job tables", before == real_checksum(cur), str(before))
    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
