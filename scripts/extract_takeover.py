"""Take over the Tableau extract refresh schedules (D-069), like t11_pause.py does for subscriptions.

WHAT IT DOES. For an extract data source that t4_refresh.py can refresh itself, the Tableau extract refresh TASK is backed up and then
DELETED in Tableau, so Tableau no longer refreshes that extract on its own; t4 refreshes it on the days the task's schedule is due, once its
pipelines succeeded (t4_refresh.py, "Tableau extract refresh schedules"). Restore re-creates the task on the same schedule.
WHY DELETE. The REST API has no per-task suspend. The other ways: suspend the whole SCHEDULE (shared by many other tasks that are not
ours), or move the extract to a parked schedule
(that is a delete and a re-create as well, plus a new schedule to look after). Deleting one task touches exactly that one task.
WHAT A RESTORE LOSES. The new task has a new id, and the task priority (the old one could differ from its schedule's), the count of
consecutive failures and the last run time are not restored (all saved in the backup row and out/extract_backup/*.json). The REST task
listing does not say full or incremental refresh: look at the task in Tableau before the first takeover; a re-created task is a normal
(full) refresh. Nothing else changes: the schedule, the data source and the extract stay as they are.

Takeover set: tasks of Integration.Tableau_ExtractTask (read by the daily mapping sync) on a published extract that t4 can refresh (the
data source is Ready in vw_Tableau_DataSourceReadiness, has at least one required pipeline and all of them exist in the ETL log), named in
an EXPLICIT allow-list (--datasource-ids, env EXTRACT_TAKEOVER_DATASOURCE_IDS; empty = nothing new is taken over and Tableau is not even
signed in to). The schedule's enabled / disabled state is never looked at (D-080). Never taken over: a schedule t4 cannot evaluate, a workbook task
(embedded extracts are not refreshed by us), a task that differs from the last sync, an extract that is not in the mapping (we cannot
refresh it, so the Tableau task is left alone), a task whose sync is older than --max-mapping-age-h.
Fences: --execute with an allow-list REQUIRES --owner-user-id (the Tableau owner of the data source; env EXTRACT_TAKEOVER_OWNER_USER_IDS);
at most --max-new (env EXTRACT_TAKEOVER_MAX_NEW, default 5) NEW takeovers per run; --execute needs data source ids (luids), not names.

For each new takeover, in this order (nothing is deleted before the backup row is committed):
  1. backup row in Integration.Tableau_ExtractTaskBackup (TakenOver = 1, NeutralisedAt NULL) and out/extract_backup/<id>.json;
  2. delete the task: server.tasks.delete(task id); Tableau is read again: exactly this task must be gone and no other;
  3. NeutralisedAt is set. If step 2 or 3 fails, the next run checks Tableau: task still there = delete again; gone = confirm.
Old refresh job (D-080). The old job (ITSC_0001 main.py) refreshes every data source with a row in Integration.ETL_Tableau_ExtractMapping
where Active_Flag = 1, as soon as ONE of its ETLs succeeded. Step 1 therefore also sets Active_Flag = 0 on exactly the rows of the taken-over
data source (DataSource_id = its luid) that are 1, in the SAME transaction as the backup row, and stores their Mapping_ids and the original
value (1) in the backup row (OldJobMappingIds, OldJobOriginalFlag, OldJobSwitchedAt). Restore sets exactly those rows back to the stored
value (only if they still differ) in the same transaction that marks the backup row restored. A data source with no row there: nothing is
done. A table that is absent: nothing is done (logged). Rows we did not switch off are never touched. Every run also repeats the switch-off
for an open takeover (a row added later for the same data source, or a takeover from before this feature): idempotent.
Only a confirmed takeover (NeutralisedAt set) switches t4's schedule rule on for the extract (vw_Tableau_ExtractSchedule.TakenOver).

Restore (rollback): --restore <original task id> (repeatable) or --restore-all. Order: if a task for the same extract AND schedule already
exists it is adopted (a task on the same extract on another schedule is only warned about), otherwise schedules.add_to_schedule(schedule,
data source) creates one; Tableau is read again to find it; only then is the backup row marked restored (a crash in between never leaves
the extract without any refresh; re-running the restore is safe). The restored row gets NoRetake = 1 (D-076): the daily run does not take
that extract over again, even while its id is still in the allow-list, until a person clears NoRetake.

Orphans (D-074): every run, also with an empty allow-list, checks each confirmed takeover (TakenOver = 1, RestoredAt NULL): not a t4 candidate
any more, schedule unusable, or no successful refresh on its last due occurrence => one alert item per extract and day (mail + Windows
Event Log), also in a dry run (the scheduled task is a dry run until enabled) and whatever the allow-list says; the mail uses the same transport as
the other monitors (local test sink unless --smtp / MAIL_TRANSPORT=smtp). A new takeover is also held back while a table of the extract comes from an Auto registry row younger than 1 day (D-075).

Usage:
  python scripts/extract_takeover.py                                     # dry run (default): lists candidates, writes nothing
  python scripts/extract_takeover.py --datasource-ids <luid> --owner-user-id <id> --execute
  python scripts/extract_takeover.py --restore <task id> --execute
Exit code 1 for a failed takeover or restore, a lookup failure, a changed task list or an orphaned takeover. Each run ends with one 'RUN SUMMARY' line.
Reads TABLEAU_PAT_NAME / TABLEAU_PAT_SECRET from .env; dev DWH only (guard in t4_refresh.connect). NEVER run --execute without the user's go-ahead.
"""
import argparse
import dataclasses
import datetime as dt
import json
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import t4_refresh as t4  # noqa: E402
import t6_send as t6  # noqa: E402
import schedule_due as sd  # noqa: E402
import extract_tasks as et  # noqa: E402
import ops  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOCK_NAME = "ExtractTakeover"
ENV_IDS = "EXTRACT_TAKEOVER_DATASOURCE_IDS"
ENV_OWNERS = "EXTRACT_TAKEOVER_OWNER_USER_IDS"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ALERT_INTRO = "These extract refresh tasks need a look (%s):"
ORPHAN_CODES = ("takeover_orphaned", "takeover_schedule", "takeover_missed", "old_job_unusable")   # t4.taken_over_problems
ALERT_SUBJECT = "Tableau subscription automation: %d extract refresh takeover problem(s) (%s)"

OLD_TABLE = "Integration.ETL_Tableau_ExtractMapping"   # the OLD refresh job's table (D-080); its main.py reads it with WHERE active_flag = 1
log = logging.getLogger("extract_takeover")


@dataclass
class Config:
    execute: bool = False
    ids: frozenset = frozenset()             # lower-cased data source luids or task ids
    owner_user_ids: frozenset = frozenset()
    max_new: int = 5
    max_mapping_age_h: float = ops.default_max_age_h()
    backup_dir: Path = ROOT / "out" / "extract_backup"
    id_like: str = "%"                       # tests only
    alert: object = None
    ses: bool = False


def parse_list(values):
    out = set()
    for v in values or []:
        out |= {x.strip().lower() for x in re.split(r"[,;]", v) if x.strip()}
    return frozenset(out)


def build_config(a, env):
    ids = parse_list(a.datasource_ids) if a.datasource_ids else parse_list([env.get(ENV_IDS, "")])
    owners = parse_list(list(a.owner_user_id or []) + [env.get(ENV_OWNERS, "")])
    restoring = bool(a.restore or a.restore_all)
    if a.execute and not restoring:
        bad = sorted(i for i in ids if not UUID_RE.match(i))
        if bad:
            raise t6.ConfigError("--execute needs data source ids (luids) in the allow-list, not names: %s" % ", ".join(bad))
        if ids and not owners:
            raise t6.ConfigError("--execute with an allow-list needs --owner-user-id <Tableau user id of the data source owner> "
                                 "(repeatable, or %s): an id alone takes over the extract whoever owns it." % ENV_OWNERS)
    try:
        max_new = int(a.max_new if a.max_new is not None else env.get("EXTRACT_TAKEOVER_MAX_NEW", 5))
    except ValueError:
        raise t6.ConfigError("--max-new / EXTRACT_TAKEOVER_MAX_NEW must be a whole number")
    if max_new < 0:
        raise t6.ConfigError("--max-new / EXTRACT_TAKEOVER_MAX_NEW must be 0 or more")
    if a.ses and not a.execute:
        raise t6.ConfigError("--smtp / --ses only makes sense with --execute")
    # The alert transport is built as for a live run (like t4 and the watchdog): local test sink unless --smtp or MAIL_TRANSPORT=smtp, so the
    # orphan alert (mailed in a dry run too, see send_alerts) uses the same transport rules as every other monitor.
    alert = ops.alert_config(a.ses, env, "extract_takeover", execute=True, local_smtp=getattr(a, "local_smtp", False))
    return Config(execute=a.execute, ids=ids, owner_user_ids=owners, max_new=max_new, max_mapping_age_h=a.max_mapping_age_h,
                  alert=alert, ses=alert.ses)


# ---------- DWH ----------
OPEN_SQL = """SELECT Backup_id, Task_id, Target_id, ISNULL(TargetName, Target_id), ScheduleId, CASE WHEN NeutralisedAt IS NULL THEN 0 ELSE 1 END
FROM Integration.Tableau_ExtractTaskBackup WHERE RestoredAt IS NULL AND Task_id LIKE ? ORDER BY Backup_id"""
CAND_SQL = """
WITH young AS (   -- extracts with a table from an Auto registry row younger than 1 day (a person should see the mail first, D-075)
  SELECT DISTINCT r2.ExtractRefreshTargetId AS tid FROM Integration.vw_Tableau_DataSourceReadiness r2
  JOIN Integration.vw_Tableau_DataSourceTableStatus ts ON ts.DataSource_id = r2.DataSource_id
  JOIN Integration.Tableau_PipelineTable pt ON pt.SchemaName = ts.SchemaName AND pt.TableName = ts.TableName
  WHERE pt.Source = 'Auto' AND pt.ConfirmedAt > DATEADD(day, -1, SYSDATETIME())),
barred AS (       -- restored once: not taken over again until a person clears NoRetake (D-076)
  SELECT DISTINCT Target_id AS tid FROM Integration.Tableau_ExtractTaskBackup WHERE NoRetake = 1)
SELECT t.Task_id, t.Target_id, t.Schedule_id, t.Priority, t.ConsecutiveFailedCount, t.LastSeenAt, MAX(r.DataSource_Name),
       MIN(CASE WHEN r.ReadyStatus = 'Ready' THEN 1 ELSE 0 END), COUNT(DISTINCT p.PipelineName), MIN(CAST(p.PipelineKnown AS int)),
       MAX(CASE WHEN y.tid IS NULL THEN 0 ELSE 1 END), MAX(CASE WHEN b.tid IS NULL THEN 0 ELSE 1 END)
FROM Integration.Tableau_ExtractTask t
JOIN Integration.vw_Tableau_DataSourceReadiness r ON r.ExtractRefreshTargetId = t.Target_id
LEFT JOIN Integration.vw_Tableau_DataSourcePipeline p ON p.DataSource_id = r.DataSource_id
LEFT JOIN young y ON y.tid = t.Target_id
LEFT JOIN barred b ON b.tid = t.Target_id
WHERE t.TargetType = 'datasource' AND t.Active_Flag = 1 AND t.Task_id LIKE ?
  AND NOT EXISTS (SELECT 1 FROM Integration.Tableau_ExtractTaskBackup b WHERE b.Task_id = t.Task_id AND b.RestoredAt IS NULL)
GROUP BY t.Task_id, t.Target_id, t.Schedule_id, t.Priority, t.ConsecutiveFailedCount, t.LastSeenAt
ORDER BY 7, t.Task_id"""


@dataclass
class Cand:
    task_id: str
    target_id: str
    sched_id: object
    priority: object
    failed: object
    synced_at: object
    name: str
    all_ready: bool
    pipes: int
    all_known: object
    young_auto: int = 0
    no_retake: int = 0


@dataclass
class Ctx:
    cfg: Config
    server: object            # callable returning a signed-in server (LazyTableau)
    cur: object
    today: dt.date
    run_id: str = field(default_factory=lambda: __import__("uuid").uuid4().hex[:12])
    n: Counter = field(default_factory=Counter)
    events: list = field(default_factory=list)
    live: dict = field(default_factory=dict)    # task id -> TaskItem, as read at the start (and after each delete)
    old_job: str = None                         # 'ok' | 'absent' | 'unexpected' | 'no_permission' (tests may set it); read once by old_job_state

    def event(self, task_id, name, code, text):
        self.events.append({"sub": task_id, "wb": name, "code": code, "text": text})

# ---------- the old refresh job's table (D-080) ----------
def old_job_state(ctx):
    """'ok': the table exists with the columns the old job uses and we may UPDATE it. 'absent' (dev may not have it): nothing to switch off.
    'unexpected' (columns differ) / 'no_permission': a NEW takeover is refused, because the old job would keep refreshing the extract."""
    if ctx.old_job is None:
        cur = ctx.cur
        if cur.execute("SELECT OBJECT_ID(?)", OLD_TABLE).fetchone()[0] is None:
            ctx.old_job = "absent"
            log.info("%s does not exist here: no old-job row to switch off or back", OLD_TABLE)
        elif cur.execute("SELECT COUNT(*) FROM sys.columns WHERE object_id = OBJECT_ID(?) AND name IN ('Mapping_id', 'DataSource_id', 'Active_Flag')", OLD_TABLE).fetchone()[0] != 3:
            ctx.old_job = "unexpected"
        elif not cur.execute("SELECT HAS_PERMS_BY_NAME(?, 'OBJECT', 'UPDATE')", OLD_TABLE).fetchone()[0]:
            ctx.old_job = "no_permission"
        else:
            ctx.old_job = "ok"
    return ctx.old_job


def old_job_rows(cur, target):
    """Mapping_ids of the old job's ACTIVE rows (Active_Flag = 1) for this data source: what a takeover switches off."""
    return [r[0] for r in cur.execute("SELECT Mapping_id FROM %s WHERE DataSource_id = ? AND Active_Flag = 1 ORDER BY Mapping_id" % OLD_TABLE, target).fetchall()]


# One batch = one transaction (XACT_ABORT: any error rolls every statement of it back). @bid is the backup row; the rows switched off are appended to it.
SWITCH_OFF_TAIL = """
DECLARE @ids TABLE (id int);
UPDATE m SET Active_Flag = 0 OUTPUT deleted.Mapping_id INTO @ids
  FROM Integration.ETL_Tableau_ExtractMapping m WHERE m.DataSource_id = @ds AND m.Active_Flag = 1;
IF EXISTS (SELECT 1 FROM @ids)
  UPDATE Integration.Tableau_ExtractTaskBackup
     SET OldJobMappingIds = CONCAT(OldJobMappingIds + ',', (SELECT STRING_AGG(CAST(id AS varchar(12)), ',') FROM @ids)),
         OldJobOriginalFlag = 1, OldJobSwitchedAt = ISNULL(OldJobSwitchedAt, SYSDATETIMEOFFSET()), OldJobRestoredAt = NULL
   WHERE Backup_id = @bid;
COMMIT;
SELECT @bid, (SELECT COUNT(*) FROM @ids);"""
BACKUP_INSERT = """SET NOCOUNT ON; SET XACT_ABORT ON;
DECLARE @ds nvarchar(256) = ?, @bid int; BEGIN TRAN;
INSERT Integration.Tableau_ExtractTaskBackup (Task_id, Target_id, TargetName, OwnerUserId, ScheduleId, ScheduleName, Priority,
  ConsecutiveFailedCount, LastRunAt, TakenOver, RunId) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CAST(? AS datetimeoffset(0)), 1, ?);
SET @bid = SCOPE_IDENTITY();"""
SWITCH_ONLY = "SET NOCOUNT ON; SET XACT_ABORT ON;\nDECLARE @ds nvarchar(256) = ?, @bid int = ?; BEGIN TRAN;"


def reconcile_old_job(ctx, bid, target, name):
    """Idempotent: switch off any still-active old-job row of an open takeover (a row added later, or a takeover from before D-080)."""
    state = old_job_state(ctx)
    if state in ("unexpected", "no_permission"):
        ctx.n["failed"] += 1
        why = "%s has other columns than expected" % OLD_TABLE if state == "unexpected" else "this account may not UPDATE %s" % OLD_TABLE
        log.error("'%s': taken over, but %s, so the old refresh job cannot be kept switched off", name, why)
        ctx.event(target, name, "old_job_unusable", "taken over, but " + why + ": the old refresh job may refresh it again")
        return
    if state != "ok":
        return
    rows = old_job_rows(ctx.cur, target)
    if not rows:
        return
    if not ctx.cfg.execute:
        log.info("DRY RUN: would switch off %d old-job row(s) of '%s' in %s (Mapping_id %s)", len(rows), name, OLD_TABLE, ",".join(map(str, rows)))
        return
    n = ctx.cur.execute(SWITCH_ONLY + SWITCH_OFF_TAIL, target, bid).fetchone()[1]
    ctx.n["old_job_off"] += n
    log.info("OLD REFRESH JOB switched off for '%s': %d more row(s) in %s", name, n, OLD_TABLE)


def old_job_ids_stored(cur, bid):
    return cur.execute("SELECT OldJobMappingIds FROM Integration.Tableau_ExtractTaskBackup WHERE Backup_id = ?", bid).fetchone()[0]


def read_live(ctx, allow_empty=False):
    """The task list of the site. An EMPTY answer while tasks are stored (or were listed a moment ago) is a partial read, like in the sync: it
    raises, so a transient empty list can never confirm a takeover. allow_empty: the one case where empty is right (the last task was just deleted)."""
    tasks = et.list_tasks(ctx.server())
    if not tasks and not allow_empty:
        stored = ctx.cur.execute("SELECT COUNT(*) FROM Integration.Tableau_ExtractTask t WHERE t.Active_Flag = 1 AND t.Task_id LIKE ? AND NOT EXISTS "
                                 "(SELECT 1 FROM Integration.Tableau_ExtractTaskBackup b WHERE b.Task_id = t.Task_id AND b.RestoredAt IS NULL)",
                                 ctx.cfg.id_like).fetchone()[0]
        if stored or ctx.live:
            raise RuntimeError("no extract refresh task came back but %d are stored / %d were listed before: partial read, nothing is confirmed" % (stored, len(ctx.live)))
    ctx.live = {t.id: t for t in tasks}


def write_backup(ctx, c, ds_name, owner, sc, live):
    """JSON file first, then the DWH row, committed in ONE transaction with the old-job switch-off (D-080). Returns the Backup_id."""
    cfg = ctx.cfg
    cfg.backup_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(t4.SGT)
    last_run = live.last_run_at.isoformat() if getattr(live, "last_run_at", None) else None
    f = cfg.backup_dir / ("%s_%s.json" % (now.strftime("%Y%m%dT%H%M%S"), re.sub(r"[^\w.-]", "_", c.task_id)))
    f.write_text(json.dumps({"task_id": c.task_id, "target_id": c.target_id, "target_type": "datasource", "target_name": ds_name, "owner_user_id": owner,
                             "schedule_id": c.sched_id, "schedule": dict(zip(("id", "name", "state", "frequency", "weekdays", "month_days", "start", "interval_hours"), sd.to_row(sc))),
                             "priority": live.priority, "consecutive_failed_count": live.consecutive_failed_count, "last_run_at": last_run,
                             "backed_up_at": now.isoformat(), "note": "definition before takeover; restore re-creates the task on this schedule"},
                            indent=2, default=str), encoding="utf-8")
    args = (c.task_id, c.target_id, (ds_name or "")[:256], owner, c.sched_id, (sc.name or "")[:200], live.priority, live.consecutive_failed_count, last_run, ctx.run_id)
    if old_job_state(ctx) == "ok":   # backup row + old-job switch-off: ONE transaction (D-080)
        bid, switched = ctx.cur.execute(BACKUP_INSERT + SWITCH_OFF_TAIL, c.target_id, *args).fetchone()
        ctx.n["old_job_off"] += switched
    else:
        bid, switched = ctx.cur.execute(BACKUP_INSERT + "\nCOMMIT; SELECT @bid, 0;", c.target_id, *args).fetchone()
    if switched:
        ids = old_job_ids_stored(ctx.cur, bid)
        f.write_text(json.dumps({**json.loads(f.read_text(encoding="utf-8")), "old_job_table": OLD_TABLE, "old_job_mapping_ids": ids, "old_job_original_flag": 1},
                                indent=2), encoding="utf-8")
        log.info("OLD REFRESH JOB switched off for '%s': %d row(s) in %s (Mapping_id %s), stored in the backup row", ds_name, switched, OLD_TABLE, ids)
    if ctx.cur.execute("SELECT COUNT(*) FROM Integration.Tableau_ExtractTaskBackup WHERE Backup_id = ? AND TakenOver = 1 AND RestoredAt IS NULL", bid).fetchone()[0] != 1:
        raise RuntimeError("backup row %s not found after writing" % bid)
    return bid


def delete_and_verify(ctx, task_id):
    """THE write to Tableau. Returns None when exactly this task is gone afterwards, else a text describing what is wrong."""
    before = set(ctx.live)
    ctx.server().tasks.delete(task_id)
    read_live(ctx, allow_empty=before == {task_id})
    gone = before - set(ctx.live)
    if task_id in ctx.live:
        return "Tableau still lists the task after the delete"
    if gone != {task_id}:
        return "the delete removed more than this task: %s" % ", ".join(sorted(gone - {task_id}))
    return None


def confirm(ctx, backup_id):
    ctx.cur.execute("UPDATE Integration.Tableau_ExtractTaskBackup SET NeutralisedAt = SYSDATETIMEOFFSET() WHERE Backup_id = ? "
                    "AND RestoredAt IS NULL AND NeutralisedAt IS NULL", backup_id)


def handle_open(ctx, backup_id, task_id, name, neutralised, target=None, sched=None):
    """A task with an open takeover row: finish an unconfirmed one, keep a confirmed one confirmed."""
    ctx.n["owned"] += 1
    if target is not None:
        reconcile_old_job(ctx, backup_id, target, name)
    if task_id not in ctx.live:
        if not neutralised:
            ctx.n["confirmed"] += 1
            log.info("%s '%s': gone from Tableau, confirming the takeover%s", task_id, name, "" if ctx.cfg.execute else " (dry run: not written)")
            if ctx.cfg.execute:
                confirm(ctx, backup_id)
        else:
            ctx.n["already_neutralised"] += 1
        return
    # the task is still there: the delete never happened or failed
    ctx.n["retry_delete"] += 1
    if not ctx.cfg.execute:
        log.info("DRY RUN: would delete %s '%s' again (backup row exists, task still in Tableau)", task_id, name)
        return
    read_live(ctx)   # D-076: the definition may have changed since the backup; never delete a task that is not the one we backed up
    if task_id not in ctx.live:
        confirm(ctx, backup_id)
        ctx.n["confirmed"] += 1
        log.info("%s '%s': gone from Tableau when read again, takeover confirmed", task_id, name)
        return
    live = ctx.live[task_id]
    if target is not None and (live.target is None or live.target.id != target or live.schedule_id != sched):
        ctx.n["failed"] += 1
        log.error("delete of %s '%s' REFUSED: the live task differs from the backup (target or schedule changed); a person must look at it", task_id, name)
        ctx.event(task_id, name, "takeover_changed", "the task in Tableau no longer matches its backup (target or schedule changed): not deleted again; "
                  "check it and close or restore the backup row")
        return
    try:
        bad = delete_and_verify(ctx, task_id)
    except Exception as e:
        ctx.n["failed"] += 1
        log.error("delete of %s FAILED (%s: %s)", task_id, type(e).__name__, t6.sanitize(e, 200))
        ctx.event(task_id, name, "takeover_failed", "the delete did not complete (%s)" % type(e).__name__)
        return
    if bad:
        ctx.n["failed"] += 1
        log.error("delete of %s: %s", task_id, bad)
        ctx.event(task_id, name, "takeover_failed", bad)
        return
    confirm(ctx, backup_id)
    ctx.n["neutralised"] += 1
    log.info("TAKEN OVER %s '%s' (delete retried)", task_id, name)


def consider(ctx, c):
    """A task without an open row: take it over if everything allows it."""
    cfg = ctx.cfg
    if c.target_id.lower() not in cfg.ids and c.task_id.lower() not in cfg.ids:
        ctx.n["skip_not_enabled"] += 1
        return
    name = c.name or c.target_id
    if c.no_retake:
        ctx.n["skip_restored"] += 1
        log.info("%s '%s': skipped, it was restored by a person (NoRetake = 1 on its backup row); not taken over again until that is cleared", c.task_id, name)
        return
    if not c.all_ready or not c.pipes or not c.all_known:
        ctx.n["skip_not_refreshable"] += 1
        log.warning("%s '%s' [task %s]: skipped, t4 could not refresh this extract itself (mapping not Ready, no required pipeline, or a pipeline unknown to the ETL log); "
                    "Tableau keeps refreshing it", c.target_id, name, c.task_id)
        return
    if c.young_auto:
        ctx.n["skip_young_auto"] += 1
        log.warning("%s '%s': skipped for now, a table of this extract comes from an Auto registry row younger than 1 day (a person should see the registry mail first)", c.task_id, name)
        return
    age_h = (t6.sgt_now().replace(tzinfo=None) - c.synced_at).total_seconds() / 3600 if c.synced_at else None
    if age_h is None or age_h > cfg.max_mapping_age_h:
        ctx.n["skip_stale"] += 1
        log.warning("%s '%s': skipped, the sync last saw the task %s ago (limit %.0f h)", c.task_id, name, "?" if age_h is None else "%.1f h" % age_h, cfg.max_mapping_age_h)
        return
    live = ctx.live.get(c.task_id)
    if live is None or live.target is None or live.target.id != c.target_id or live.schedule_id != c.sched_id:
        ctx.n["skip_definition_changed"] += 1
        log.warning("%s '%s': skipped, the task is gone or differs from the last sync (target or schedule)", c.task_id, name)
        return
    try:
        sc = sd.from_tsc(ctx.server().schedules.get_by_id(c.sched_id))
        ds = ctx.server().datasources.get_by_id(c.target_id)
    except Exception as e:
        ctx.n["lookup_failed"] += 1
        log.warning("Tableau lookup failed for task %s (%s)", c.task_id, type(e).__name__)
        return
    try:
        sd.fires_on(sc, ctx.today)
    except sd.Unsupported as e:
        ctx.n["skip_schedule_unsupported"] += 1
        log.warning("%s '%s': skipped, t4 could not evaluate its schedule '%s' (%s)", c.task_id, name, sc.name, e)
        return
    if ds.has_extracts is not True:
        ctx.n["skip_no_extract"] += 1
        log.warning("%s '%s': skipped, the published data source has no extract (has_extracts=%s)", c.task_id, name, ds.has_extracts)
        return
    if cfg.owner_user_ids and (ds.owner_id or "").lower() not in cfg.owner_user_ids:
        ctx.n["skip_not_owner"] += 1
        log.info("%s '%s': skipped, the data source owner is not one of --owner-user-id", c.task_id, name)
        return
    if ctx.n["new_attempts"] >= cfg.max_new:
        ctx.n["skip_cap"] += 1
        log.warning("%s '%s': NOT taken over in this run, the cap of %d new takeovers per run is reached; the next run continues", c.task_id, name, cfg.max_new)
        return
    if old_job_state(ctx) in ("unexpected", "no_permission"):
        ctx.n["failed"] += 1
        why = ("%s has other columns than the old job uses (Mapping_id, DataSource_id, Active_Flag)" % OLD_TABLE if ctx.old_job == "unexpected"
               else "this account may not UPDATE %s" % OLD_TABLE)
        log.error("%s '%s': NOT taken over, %s, so the old refresh job could not be switched off and would keep refreshing it", c.task_id, name, why)
        ctx.event(c.task_id, name, "takeover_failed", "not taken over: " + why)
        return
    ctx.n["new_attempts"] += 1
    if not cfg.execute:
        ctx.n["would_take_over"] += 1
        rows = old_job_rows(ctx.cur, c.target_id) if ctx.old_job == "ok" else []
        log.info("DRY RUN: old refresh job: %s", ("would set Active_Flag = 0 on %d row(s) of %s (Mapping_id %s)" % (len(rows), OLD_TABLE, ",".join(map(str, rows))))
                 if rows else "no active row for this data source in %s (%s): nothing to switch off" % (OLD_TABLE, "table absent" if ctx.old_job == "absent" else "no row"))
        log.info("DRY RUN: would back up and delete task %s of '%s' (schedule '%s' %s; priority %s, failed %s times)", c.task_id, name, sc.name,
                 sc.frequency, live.priority, live.consecutive_failed_count)
        return
    if ops.killed_midrun("extract_takeover"):
        ctx.n["killed"] += 1
        return
    try:
        bid = write_backup(ctx, c, name, ds.owner_id, sc, live)
        bad = delete_and_verify(ctx, c.task_id)
    except Exception as e:
        ctx.n["failed"] += 1
        log.error("takeover of %s FAILED (%s: %s); the next run checks Tableau and finishes or retries", c.task_id, type(e).__name__, t6.sanitize(e, 200))
        ctx.event(c.task_id, name, "takeover_failed", "the backup or the delete did not complete (%s)" % type(e).__name__)
        return
    if bad:
        ctx.n["failed"] += 1
        log.error("takeover of %s: %s", c.task_id, bad)
        ctx.event(c.task_id, name, "takeover_failed", bad)
        return
    try:
        confirm(ctx, bid)
    except Exception as e:
        ctx.n["failed"] += 1
        log.error("%s is deleted in Tableau but the backup row could not be updated (%s); the next run confirms it", c.task_id, type(e).__name__)
        return
    ctx.n["neutralised"] += 1
    log.info("TAKEN OVER task %s of '%s' (schedule '%s'): deleted in Tableau, t4 refreshes it on that schedule from now on", c.task_id, name, sc.name)


def run(ctx, mailer):
    cur, cfg = ctx.cur, ctx.cfg
    open_rows = cur.execute(OPEN_SQL, cfg.id_like).fetchall()
    cands = [Cand(*r) for r in cur.execute(CAND_SQL, cfg.id_like).fetchall()]
    wanted = [c for c in cands if c.target_id.lower() in cfg.ids or c.task_id.lower() in cfg.ids]
    ctx.n["candidates"] = len(cands)
    if open_rows or wanted:
        read_live(ctx)   # signs in (takes the PAT lock); with nothing to do Tableau is not touched
    for bid, tid, target, name, sched, neutralised in open_rows:
        handle_open(ctx, bid, tid, name, bool(neutralised), target, sched)
    for c in wanted:
        consider(ctx, c)
    if not cfg.ids:
        log.info("allow-list is empty: nothing new is taken over. %d task(s) could be (data source ids for --datasource-ids): %s", len(cands),
                 "; ".join("%s (%s)" % (c.target_id, c.name) for c in cands if c.all_ready and c.pipes and c.all_known) or "none")
    if ctx.n["skip_cap"]:
        ctx.event("-", "(whole run)", "takeover_cap", "the cap of %d new takeovers per run was reached; %d more task(s) matched the allow-list and were NOT taken over"
                  % (cfg.max_new, ctx.n["skip_cap"]))
    for tid, name, code, text in t4.taken_over_problems(cur, ctx.today, None, cfg.max_mapping_age_h):   # D-074
        ctx.n["orphaned"] += 1
        log.error("TAKEN OVER but nothing refreshes it: %s [%s]: %s", name, tid, text)
        ctx.event(tid, name, code, text)
        ops.event_log_once("extract_takeover", "%s:%s" % (code, tid), "%s: %s" % (name, text), "ERROR", day=ctx.today)
    ctx.n["alerts"] = send_alerts(ctx, mailer)
    return ctx.n


def send_alerts(ctx, mailer):
    """Mail the new alert items once per day. The orphan alerts (ORPHAN_CODES) go out in a dry run too: the scheduled task runs as a dry run until
    it is enabled, and a takeover that nothing refreshes must be seen either way. The transport is cfg.alert's (local sink unless real SMTP is on)."""
    events = ctx.events if ctx.cfg.execute else [e for e in ctx.events if e["code"] in ORPHAN_CODES]
    fresh, _ = t6.fresh_events(ctx.cfg.alert, ctx.today, events)
    if not fresh:
        return 0
    return t6.deliver_alerts(ctx.cfg.alert, mailer, ctx.today, events, intro=ALERT_INTRO % ctx.today, footer=False, subject=ALERT_SUBJECT)


def summary_line(ctx):
    n, cfg = ctx.n, ctx.cfg
    skips = " ".join("%s=%d" % (k, v) for k, v in sorted(n.items()) if k.startswith("skip_"))
    bad = n["failed"] + n["lookup_failed"] + n["orphaned"]
    return ("RUN SUMMARY mode=%s transport=%s allow_list=%d candidates=%d owned=%d neutralised=%d would_take_over=%d confirmed=%d already_neutralised=%d "
            "failed=%d lookup_failed=%d orphaned=%d old_job_off=%d %s alerts=%d needs_human=%d" % (
                "execute" if cfg.execute else "dry-run", "ses" if cfg.ses else "local", len(cfg.ids), n["candidates"], n["owned"], n["neutralised"],
                n["would_take_over"], n["confirmed"], n["already_neutralised"], n["failed"], n["lookup_failed"], n["orphaned"], n["old_job_off"], skips or "skipped=0",
                n["alerts"], bad)) + ops.allow_all_marker()


def exit_code(ctx):
    return 1 if (ctx.n["failed"] or ctx.n["lookup_failed"] or ctx.n["orphaned"]) else 0


# ---------- restore ----------
RESTORE_ROWS = """SET NOCOUNT ON; SET XACT_ABORT ON;
DECLARE @bid int = ?, @new nvarchar(128) = ?; BEGIN TRAN;
UPDATE Integration.Tableau_ExtractTaskBackup SET RestoredAt = SYSDATETIMEOFFSET(), NewTaskId = @new, NoRetake = 1 WHERE Backup_id = @bid AND RestoredAt IS NULL;
COMMIT; SELECT 0;"""
RESTORE_OLD_JOB = """SET NOCOUNT ON; SET XACT_ABORT ON;
DECLARE @bid int = ?, @new nvarchar(128) = ?, @n int = 0; BEGIN TRAN;
UPDATE Integration.Tableau_ExtractTaskBackup SET RestoredAt = SYSDATETIMEOFFSET(), NewTaskId = @new, NoRetake = 1 WHERE Backup_id = @bid AND RestoredAt IS NULL;
UPDATE m SET Active_Flag = b.OldJobOriginalFlag
  FROM Integration.ETL_Tableau_ExtractMapping m
  JOIN Integration.Tableau_ExtractTaskBackup b ON b.Backup_id = @bid AND b.OldJobMappingIds IS NOT NULL AND b.OldJobOriginalFlag IS NOT NULL
 WHERE m.DataSource_id = b.Target_id AND m.Active_Flag <> b.OldJobOriginalFlag
   AND m.Mapping_id IN (SELECT CAST(value AS int) FROM STRING_SPLIT(b.OldJobMappingIds, ','));
SET @n = @@ROWCOUNT;
UPDATE Integration.Tableau_ExtractTaskBackup SET OldJobRestoredAt = SYSDATETIMEOFFSET() WHERE Backup_id = @bid AND OldJobMappingIds IS NOT NULL;
COMMIT; SELECT @n;"""


def restore_rows(ctx, bid, target, name, new_task_id):
    """Mark the backup row restored and put the old job's rows back, in ONE transaction. Returns the number of old-job rows switched back."""
    ids = old_job_ids_stored(ctx.cur, bid)
    if not ids:
        return ctx.cur.execute(RESTORE_ROWS, bid, new_task_id).fetchone()[0]
    if old_job_state(ctx) != "ok":   # the row stays OPEN: closing it would leave the old job switched off for good
        raise RuntimeError("old-job rows %s were switched off but %s is %s here, so they cannot be switched back" % (
            ids, OLD_TABLE, "absent" if ctx.old_job == "absent" else "not usable (%s)" % ctx.old_job))
    back = ctx.cur.execute(RESTORE_OLD_JOB, bid, new_task_id).fetchone()[0]
    log.info("OLD REFRESH JOB switched back on for '%s': %d of the stored row(s) (Mapping_id %s) set to their original value", name, back, ids)
    return back


def restore(ctx, task_ids, restore_all):
    """Re-create each task on its schedule (or adopt one that exists), then mark the row restored. Returns the number of problems."""
    cur, cfg, n = ctx.cur, ctx.cfg, ctx.n
    rows = []
    for tid in task_ids:
        r = cur.execute("SELECT TOP 1 Backup_id, Task_id, Target_id, ISNULL(TargetName, Target_id), ScheduleId, CAST(RestoredAt AS datetime2), NewTaskId FROM Integration.Tableau_ExtractTaskBackup "
                        "WHERE Task_id = ? AND Task_id LIKE ? AND TakenOver = 1 ORDER BY Backup_id DESC", tid, cfg.id_like).fetchone()
        if r is None:
            n["failed"] += 1
            log.error("%s: no takeover backup row, so it is not ours; nothing restored", tid)
        else:
            rows.append(r)
    if restore_all:
        rows += [(r[0], r[1], r[2], r[3], r[4], None, None) for r in cur.execute(OPEN_SQL, cfg.id_like).fetchall()]
    if rows and cfg.execute:
        read_live(ctx)   # signs in = takes the PAT lock before anything is written
    for bid, tid, target, name, sched, restored_at, new_id in dict((r[1], r) for r in rows).values():
        n["restore_targets"] += 1
        if restored_at is not None:
            log.info("%s '%s': already restored (new task %s)", tid, name, new_id)
            continue
        if not cfg.execute:
            log.info("DRY RUN: would re-create the extract refresh task of '%s' on schedule %s (or adopt an existing one), then mark %s restored", name, sched, tid)
            ids = old_job_ids_stored(cur, bid)
            if ids:
                log.info("DRY RUN: and set the old refresh job's row(s) Mapping_id %s in %s back to their original value", ids, OLD_TABLE)
            continue
        try:
            same = [t for t in ctx.live.values() if t.target is not None and t.target.id == target and t.schedule_id == sched]
            for t in ctx.live.values():
                if t.target is not None and t.target.id == target and t.schedule_id != sched:
                    log.warning("%s '%s': a task for this extract already exists on ANOTHER schedule (%s, task %s): not adopted (the original schedule is %s); "
                                "the extract will refresh on both until a person removes one", tid, name, t.schedule_id, t.id, sched)
            if same:
                new = same[0]
                log.warning("%s '%s': a task for this extract and schedule already exists (%s): adopted, not created again", tid, name, new.id)
            else:
                srv = ctx.server()
                srv.schedules.get_by_id(sched)   # the schedule must still exist
                failed = srv.schedules.add_to_schedule(sched, datasource=srv.datasources.get_by_id(target))
                if failed:
                    raise RuntimeError("Tableau refused to add the data source to the schedule: %s" % failed)
                before = set(ctx.live)
                read_live(ctx)
                found = [t for t in ctx.live.values() if t.id not in before and t.target is not None and t.target.id == target and t.schedule_id == sched]
                if len(found) != 1:
                    raise RuntimeError("expected exactly one new task for the extract on that schedule, found %d" % len(found))
                new = found[0]
        except Exception as e:
            n["failed"] += 1
            ctx.event(tid, name, "restore_failed", "the task could not be re-created (%s); the backup row stays open; run --restore again" % type(e).__name__)
            log.error("RESTORE %s '%s' FAILED (%s: %s). The row stays open; run --restore again.", tid, name, type(e).__name__, t6.sanitize(e, 200))
            continue
        try:
            back = restore_rows(ctx, bid, target, name, new.id)
        except Exception as e:
            n["failed"] += 1
            ctx.event(tid, name, "restore_failed", "the task is back in Tableau but the backup row / old-job rows could not be updated (%s); run --restore again" % type(e).__name__)
            log.error("RESTORE %s '%s': task %s created, but the database update FAILED (%s: %s). Nothing was changed in the database; run --restore again (the task is adopted).",
                      tid, name, new.id, type(e).__name__, t6.sanitize(e, 200))
            continue
        n["old_job_back"] += back
        try:   # bookkeeping only: the daily sync repairs it; a failure must not stop the rest of --restore-all
            cur.execute("UPDATE Integration.Tableau_ExtractTask SET Active_Flag = 0 WHERE Task_id = ?", tid)
            cur.execute("MERGE Integration.Tableau_ExtractTask AS t USING (SELECT ? AS id) AS s ON t.Task_id = s.id "
                        "WHEN MATCHED THEN UPDATE SET Target_id = ?, TargetType = 'datasource', Schedule_id = ?, Priority = ?, ConsecutiveFailedCount = ?, LastSeenAt = SYSDATETIME(), Active_Flag = 1 "
                        "WHEN NOT MATCHED THEN INSERT (Task_id, Target_id, TargetType, Schedule_id, Priority, ConsecutiveFailedCount) VALUES (?, ?, 'datasource', ?, ?, ?);",
                        new.id, target, sched, new.priority, new.consecutive_failed_count, new.id, target, sched, new.priority, new.consecutive_failed_count)
        except Exception as e:
            log.error("RESTORE %s '%s': restored, but the task list table could not be updated (%s: %s); the next daily sync repairs it", tid, name, type(e).__name__, t6.sanitize(e, 200))
        n["restored"] += 1
        log.info("RESTORED '%s': task %s on schedule %s; Tableau refreshes it again, t4 uses the subscription rule for it. It is NOT taken over "
                 "again (NoRetake = 1) until a person clears that; remove %s from the allow-list too", name, new.id, sched, target)
    return n["failed"]


def restore_summary(ctx):
    n = ctx.n
    return ("RUN SUMMARY mode=%s restore targets=%d restored=%d old_job_back=%d failed=%d needs_human=%d" % (
        "execute" if ctx.cfg.execute else "dry-run", n["restore_targets"], n["restored"], n["old_job_back"], n["failed"], n["failed"])) + ops.allow_all_marker()


# ---------- main ----------
def setup_logging():
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    fh = ops.log_file_handler("extract_takeover")
    for h in (sh, fh):
        h.setFormatter(fmt)
    for lg in (log, t4.log, t6.log, ops.log, logging.getLogger("t2_sync")):
        lg.setLevel(logging.INFO)
        if not lg.handlers:
            lg.addHandler(sh)
            lg.addHandler(fh)

    def hook(et_, ev, tb):
        log.critical("Unhandled error", exc_info=(et_, ev, tb))
    sys.excepthook = hook


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="default: print what would happen, write nothing")
    g.add_argument("--execute", action="store_true", help="WRITE: backup rows in the dev DWH and DELETE / re-create tasks in Tableau")
    ap.add_argument("--datasource-ids", action="append", help="allow-list: data source luids or task ids (comma list, repeatable); env " + ENV_IDS)
    ap.add_argument("--owner-user-id", action="append", help="fence: only data sources owned by this Tableau user id (repeatable; env " + ENV_OWNERS + "). REQUIRED for --execute with an allow-list")
    ap.add_argument("--max-new", type=int, default=None, help="cap on NEW takeovers per run (default 5, env EXTRACT_TAKEOVER_MAX_NEW)")
    ap.add_argument("--restore", action="append", help="rollback: re-create this task (original task id, repeatable)")
    ap.add_argument("--restore-all", action="store_true", help="rollback: restore every open takeover")
    ap.add_argument("--smtp", "--ses", dest="ses", action="store_true", help="alert mail through the real SMTP server (needs --execute); or MAIL_TRANSPORT=smtp")
    ap.add_argument("--local-smtp", action="store_true", help="force the local test sink even if MAIL_TRANSPORT=smtp")
    ap.add_argument("--local-host", default="127.0.0.1")
    ap.add_argument("--local-port", type=int, default=1025)
    ap.add_argument("--max-mapping-age-h", type=float, default=ops.default_max_age_h())
    a = ap.parse_args(argv)
    try:
        cfg = build_config(a, os.environ)
    except t6.ConfigError as e:
        sys.exit(str(e))
    setup_logging()
    restoring = bool(a.restore or a.restore_all)
    mode = "execute" if cfg.execute else "dry-run"
    ops.log_transport("extract_takeover", cfg.alert.ses, cfg.execute)
    if not restoring and ops.check_kill_switch("extract_takeover", cfg.execute, cfg.ses, a.local_host, a.local_port):
        return 0
    ops.check_alert_recipient(cfg.alert, "extract_takeover")
    mailer = t6.make_mailer(cfg.alert, a.local_host, a.local_port)

    def fatal(what):
        what = t6.sanitize(what, 200)
        log.error("RUN SUMMARY mode=%s FATAL=%s needs_human=1%s", mode, what, ops.allow_all_marker())
        if cfg.execute:
            try:
                t6.deliver_alerts(cfg.alert, mailer, t6.sgt_now().date(), [{"sub": "-", "wb": "(whole run)", "code": "startup", "text": what}],
                                  intro=ALERT_INTRO % t6.sgt_now().date(), footer=False, subject=ALERT_SUBJECT)
            except Exception as e:
                log.error("Startup alert could not be sent (%s)", type(e).__name__)
        ops.write_marker("extract_takeover", mode, 1, "FATAL " + what, outcome="fatal")
        return 1
    try:
        conn = t4.connect()
        cur = conn.cursor()
        today = t4.dwh_today(cur)
    except (Exception, SystemExit) as e:
        return fatal("DWH connect or guard failed: %s" % (e if isinstance(e, SystemExit) else type(e).__name__))
    if cur.execute("SELECT COUNT(*) FROM sys.tables WHERE name IN ('Tableau_ExtractTask', 'Tableau_ExtractTaskBackup') AND schema_id = SCHEMA_ID('Integration')").fetchone()[0] != 2             or cur.execute("SELECT COL_LENGTH('Integration.Tableau_ExtractTaskBackup', 'NoRetake')").fetchone()[0] is None \
            or cur.execute("SELECT COL_LENGTH('Integration.Tableau_ExtractTaskBackup', 'OldJobMappingIds')").fetchone()[0] is None:
        return fatal("Integration.Tableau_ExtractTask / Tableau_ExtractTaskBackup (with NoRetake and OldJobMappingIds) missing: run scripts/sql/ExtractAuto_schema.sql")
    if cfg.execute:
        got = cur.execute("DECLARE @r int; EXEC @r = sp_getapplock @Resource = ?, @LockMode = 'Exclusive', @LockOwner = 'Session', @LockTimeout = 0; SELECT @r", LOCK_NAME).fetchone()[0]
        if got < 0:
            log.warning("Another extract_takeover run holds the lock; exiting")
            return 1 if restoring else 0
    tableau = t4.LazyTableau(cur, owner="extract_takeover")
    ctx = Ctx(cfg, tableau, cur, today)
    try:
        if restoring:
            restore(ctx, a.restore or [], a.restore_all)
            log.info(restore_summary(ctx))
            if ctx.events:
                send_alerts(ctx, mailer)
            return 1 if ctx.n["failed"] else 0
        run(ctx, mailer)
    except t4.PatBusy:
        log.info("RUN SUMMARY mode=%s skipped=pat_busy (another script holds the Tableau PAT session)", mode)
        ops.on_pat_busy("extract_takeover", cfg.execute, cfg=cfg.alert, mailer=mailer)
        ops.write_marker("extract_takeover", mode, 1 if restoring else 0, "skipped=pat_busy", outcome="pat_busy")
        return 1 if restoring else 0
    except SystemExit:
        return fatal("Tableau sign-in failed")
    except Exception:
        log.exception("Run failed")
        return fatal("run failed")
    finally:
        tableau.close()
    ops.pat_ok("extract_takeover")
    line = summary_line(ctx)
    log.info(line)
    code = exit_code(ctx)
    ops.write_marker("extract_takeover", mode, code, line)
    return code


if __name__ == "__main__":
    sys.exit(main())
