"""Tableau extract refresh tasks and their schedules (D-067), read-only. Used by the daily mapping sync (t2_mapping_sync.py) and
by extract_takeover.py. Nothing here writes to Tableau.

A task = one data source (or workbook) on one server schedule of type Extract. The task list carries only the schedule id and
name, not the weekdays; the schedule itself is read with schedules.get_by_id and parsed by schedule_due.from_tsc, exactly like a
subscription schedule.
"""
import logging
import sys
from pathlib import Path

import tableauserverclient as TSC

sys.path.insert(0, str(Path(__file__).resolve().parent))
import schedule_due as sd  # noqa: E402

log = logging.getLogger("t2_sync")
PAGE = 1000


def list_tasks(server):
    """All extract refresh tasks of the site as tableauserverclient TaskItems. The task endpoint may answer without paging details."""
    out, page = [], 1
    while True:
        opts = TSC.RequestOptions(pagesize=PAGE)
        opts.pagenumber = page
        items, pag = server.tasks.get(opts)
        out += items
        total = getattr(pag, "total_available", None)
        if not items or total is None or len(out) >= int(total):
            return out
        page += 1


def read_tasks(server):
    """Returns (tasks, schedules, problems). tasks: [{'id','target','ttype','sched','priority','failed'}]; schedules: {id: Schedule}
    for every schedule a task uses. A schedule that cannot be read is a problem and is left out (its extracts count as having no schedule)."""
    tasks, schedules, problems = [], {}, []
    for t in list_tasks(server):
        if t.target is None:
            problems.append(("extract task", t.id, "no target (data source or workbook) in the response"))
            continue
        tasks.append({"id": t.id, "target": t.target.id, "ttype": t.target.type, "sched": t.schedule_id,
                      "priority": t.priority, "failed": t.consecutive_failed_count})
    for sid in sorted({x["sched"] for x in tasks if x["sched"]}):
        try:
            schedules[sid] = sd.from_tsc(server.schedules.get_by_id(sid))
        except Exception as e:
            problems.append(("extract schedule", sid, "%s: %s" % (type(e).__name__, str(e)[:120])))
            log.error("EXTRACT SCHEDULE SKIPPED %s: %s: %s", sid, type(e).__name__, str(e)[:120])
    return tasks, schedules, problems


def read_backup_schedules(server, cur, schedules):
    """D-073: the schedule of every open takeover row (Tableau_ExtractTaskBackup, restored is NULL). The task is deleted in Tableau, so the task
    list no longer names its schedule; without this the schedule is never read again, Tableau_Schedule.LastSeenAt goes stale and after 26 h
    the extract is never due. schedules: {id: Schedule} already read for the live tasks (not read twice). Returns ({id: Schedule}, problems);
    a schedule that cannot be read (deleted?) is a problem: the taken-over extract is not refreshed on it."""
    out, problems = {}, []
    for (sid,) in cur.execute("SELECT DISTINCT ScheduleId FROM Integration.Tableau_ExtractTaskBackup WHERE TakenOver = 1 AND RestoredAt IS NULL").fetchall():
        sc = schedules.get(sid)
        if sc is None:
            try:
                sc = out[sid] = sd.from_tsc(server.schedules.get_by_id(sid))
            except Exception as e:
                problems.append(("schedule of a taken-over extract task", sid, "cannot be read (deleted in Tableau?): %s: %s" % (type(e).__name__, str(e)[:100])))
                log.error("SCHEDULE OF A TAKEN-OVER TASK NOT READ %s: %s: %s", sid, type(e).__name__, str(e)[:100])
                continue
    return out, problems
