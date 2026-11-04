"""R3b: review list of every in-scope subscription, for the pre-go-live clean-up (D-046).

Purpose: the user reads out/review_list.csv and deletes in Tableau every subscription that must not be sent (stale, test,
duplicate). From then on the script treats every remaining in-scope subscription as sendable (suspended or not).

READ-ONLY: dev DWH SELECTs only, Tableau reads only (one sign-in, workbooks and subscriptions listed once, nothing written,
no export, no refresh, no mail). Takes the shared PAT session lock like the other scripts (busy = exits without signing in).
No email address is read or written; the owner is a Tableau user id.

One row per active subscription in Integration.Tableau_Subscription (= subscribed workbooks with more than one data source,
as last synced by T2), sorted by workbook, then schedule:
  workbook, workbook_id, project, subscription_id, subject, target (Workbook/View), format, owner_user_id, schedule, schedule_type,
  suspended_now (live in Tableau; 'unknown' if the lookup failed), taken_over (open takeover row), next_due (the first date the
  SCRIPT would send if it took the subscription over right now: today's occurrence counts only if the schedule time has not passed yet,
  takeover-day guard; per scripts/schedule_due.py), next_fire_date (the plain next date the schedule fires, from today),
  retry_until (last day the script would still try that occurrence, catch-up window), next_due_note (why next_due differs from
  next_fire_date), due_in_14_days (based on next_due), readiness (vw_Tableau_WorkbookReadiness),
  not_ready_why, last_sent (latest Sent date in the send log), found_in_tableau (no = already deleted since the last T2 sync).

Usage:  python scripts/review_list.py [--out out/review_list.csv] [--days 14]
"""
import argparse
import csv
import datetime as dt
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import t4_refresh as t4  # noqa: E402
import schedule_due as sd  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
COLUMNS = ["workbook", "workbook_id", "project", "subscription_id", "subject", "target", "format", "owner_user_id", "schedule",
           "schedule_type", "schedule_state", "suspended_now", "taken_over", "next_due", "next_fire_date", "retry_until", "next_due_note", "due_in_14_days", "readiness", "not_ready_why", "last_sent",
           "found_in_tableau"]

SUBS_SQL = """
SELECT s.Subscription_id, s.Workbook_id,
       (SELECT TOP 1 w.Workbook_Name FROM Integration.Tableau_WorkbookDataSource w WHERE w.Workbook_id = s.Workbook_id
        ORDER BY w.Active_Flag DESC, w.LastSeenAt DESC, w.Workbook_Name),
       s.TargetType, s.Format, s.Subject, s.ScheduleType, s.ScheduleId, s.IsSuspended,
       wr.WorkbookStatus,
       CASE WHEN EXISTS (SELECT 1 FROM Integration.Tableau_SubscriptionBackup b WHERE b.Subscription_id = s.Subscription_id
                         AND b.TakenOver = 1 AND b.RestoredAt IS NULL) THEN 1 ELSE 0 END,
       (SELECT MAX(l.SendDate) FROM Integration.Tableau_SubscriptionSendLog l WHERE l.Subscription_id = s.Subscription_id AND l.Status = 'Sent')
FROM Integration.Tableau_Subscription s
LEFT JOIN Integration.vw_Tableau_WorkbookReadiness wr ON wr.Workbook_id = s.Workbook_id
WHERE s.Active_Flag = 1"""

# why a workbook is NotReady: its data sources that are not Ready (names only)
WHY_SQL = """
SELECT w.Workbook_id, r.DataSource_Name, r.ReadyStatus, r.RefreshKind
FROM Integration.Tableau_WorkbookDataSource w
JOIN Integration.vw_Tableau_DataSourceReadiness r ON r.DataSource_id = w.DataSource_id
WHERE w.Active_Flag = 1 AND r.ReadyStatus NOT IN ('Ready', 'NoReadinessCheck', 'NotChecked')"""

WHY_TEXT = {"NoTables": "no warehouse table found and no file / other-database connection identified (custom SQL or read problem, D-021/D-053)", "Unmapped": "table not in the pipeline registry",
            "UnknownPipeline": "pipeline unknown"}


def next_due(s, today, now=None, catch_up=None):
    """(first send date, next fire date, retry-until date, note). Never raises. The dates are None when there is none.
    next fire date = first date >= today on which the schedule fires. First send date = what the script would do if it took the
    subscription over at `now` (naive Singapore time): an occurrence whose schedule time has already passed today belongs to the old
    Tableau subscription (takeover-day guard, schedule_due._counts), so the first send is the next occurrence. retry-until = the last
    day the script still retries that occurrence if it could not send it (catch-up window, schedule_due.window_end)."""
    if s is None:
        return None, None, None, "no schedule stored"
    now = now or dt.datetime.now(sd.SGT).replace(tzinfo=None)
    catch_up = sd.catch_up_default() if catch_up is None else catch_up
    try:
        fire = today if sd.fires_on(s, today) else sd.next_occurrence(s, today)
        first, note = fire, ""
        if fire is not None and fire == today and not sd._counts(s, today, now):
            first = sd.next_occurrence(s, today)
            note = ("takeover-day guard: today's %s schedule time has passed, so today is NOT sent; first send is the next occurrence"
                    % (s.start.strftime("%H:%M") if s.start else "end-of-day"))
        until = sd.window_end(s, first, catch_up) if first is not None else None
        if first is not None and until is not None and until > first:
            note = (note + "; " if note else "") + "if a send fails it is retried until %s (catch-up window)" % until.isoformat()
        return first, fire, until, note
    except sd.Unsupported as e:
        return None, None, None, "unsupported: %s" % e


# a spreadsheet runs a cell that starts with one of these as a formula (CSV injection); a leading tab or CR can hide one too
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(v):
    """Neutralise a text cell for Excel: prefix a single quote when it would start a formula."""
    return "'" + v if isinstance(v, str) and v.startswith(FORMULA_START) else v


def load_live(cur, owner="review_list"):
    """One sign-in: ({workbook id: project name}, {subscription id: (user id, suspended)}). Returns (None, None) if Tableau is busy."""
    import tableauserverclient as TSC
    tab = t4.LazyTableau(cur, owner=owner)
    try:
        server = tab()
        projects = {w.id: w.project_name for w in TSC.Pager(server.workbooks)}
        subs = {s.id: (s.user_id, bool(s.suspended)) for s in TSC.Pager(server.subscriptions)}
    finally:
        tab.close()
    return projects, subs


def build_rows(cur, today, projects, live, horizon, now=None):
    scheds = sd.load_schedules(cur)
    sched_name = {r[0]: r[1] for r in cur.execute("SELECT Schedule_id, Name FROM Integration.Tableau_Schedule").fetchall()}
    why = {}
    for wid, dsname, status, kind in cur.execute(WHY_SQL).fetchall():
        why.setdefault(wid, []).append("%s: %s" % (dsname, WHY_TEXT.get(status, status)))
    rows = []
    for (sid, wid, wname, ttype, fmt, subject, stype, schedid, snap, wstatus, taken, last_sent) in cur.execute(SUBS_SQL).fetchall():
        owner, susp = live.get(sid, (None, None)) if live is not None else (None, None)
        found = live is not None and sid in live
        due, fire, until, note = next_due(scheds.get(schedid) if schedid else None, today, now)
        rows.append({
            "workbook": wname or "(unnamed workbook)", "workbook_id": wid, "project": (projects or {}).get(wid, ""),
            "subscription_id": sid, "subject": subject or "", "target": ttype, "format": fmt, "owner_user_id": owner or "",
            "schedule": sched_name.get(schedid, "") or stype or "", "schedule_type": stype or "",
            "schedule_state": (scheds[schedid].state or "unknown") if schedid in scheds else "unknown",   # visibility only: the state does not change what is sent (D-080)
            "suspended_now": ("yes" if susp else "no") if found else "unknown (not found in Tableau; T2 snapshot: %s)" % ("yes" if snap else "no"),
            "taken_over": "yes" if taken else "no",
            "next_due": due.isoformat() if due else (note or "unknown"),
            "next_fire_date": fire.isoformat() if fire else "", "retry_until": until.isoformat() if until and due and until > due else "",
            "next_due_note": note if due else "",
            "due_in_14_days": "yes" if due and (due - today).days <= horizon else "no",
            "readiness": wstatus or "unknown", "not_ready_why": "; ".join(sorted(set(why.get(wid, [])))) if wstatus != "Ready" else "",
            "last_sent": last_sent.isoformat() if last_sent else "", "found_in_tableau": "yes" if found else "no"})
    rows.sort(key=lambda r: (r["workbook"].lower(), r["schedule"].lower(), r["subject"].lower(), r["subscription_id"]))
    return rows


def summary(rows, horizon):
    c = Counter()
    for r in rows:
        c["total"] += 1
        c["suspended"] += r["suspended_now"] == "yes"
        c["due"] += r["due_in_14_days"] == "yes"
        c["due_suspended"] += r["due_in_14_days"] == "yes" and r["suspended_now"] == "yes"
        c["not_ready"] += r["readiness"] != "Ready"
        c["gone"] += r["found_in_tableau"] == "no"
        c["taken"] += r["taken_over"] == "yes"
    wbs = {r["workbook_id"] for r in rows}
    return c, wbs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "out" / "review_list.csv"))
    ap.add_argument("--days", type=int, default=14, help="horizon for 'due soon' (default 14)")
    a = ap.parse_args(argv)
    conn = t4.connect()
    cur = conn.cursor()
    today = t4.dwh_today(cur)
    try:
        projects, live = load_live(cur)
    except t4.PatBusy:
        sys.exit("Another script holds the Tableau PAT session; try again in a few minutes. Nothing was read.")
    rows = build_rows(cur, today, projects, live, a.days)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows({k: safe_cell(v) for k, v in r.items()} for r in rows)
    c, wbs = summary(rows, a.days)
    print("Review list for %s written to %s" % (today, out))
    print("  %d subscriptions on %d workbooks" % (c["total"], len(wbs)))
    print("  currently suspended in Tableau: %d (of which due in the next %d days: %d)" % (c["suspended"], a.days, c["due_suspended"]))
    print("  due in the next %d days: %d" % (a.days, c["due"]))
    print("  on a NotReady workbook (would not be sent): %d" % c["not_ready"])
    print("  not found in Tableau any more (already deleted since the last T2 sync): %d" % c["gone"])
    print("  already taken over by t11: %d" % c["taken"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
