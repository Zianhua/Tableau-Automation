"""R1 / F-07: ONE summary mail at the end of the day window (run it once, for example at 17:00 Singapore).

Tells the team: which scripts did not run today (last-run markers, scripts/ops.py), what was sent, what was due but NOT sent and why,
which extracts were refreshed, which subscriptions were re-paused or halted, and every error. It reads only the dev DWH and the marker
files, never Tableau, so it works when Tableau or the PAT is the problem. Counters of the last t6 run (no_allowed_recipient, rejected,
halted, ...) are copied from its RUN SUMMARY line.

Mail goes to ALERT_TO (default OWNER_EMAIL) through t6's mail path and allow-list rules: the LOCAL test SMTP unless
--execute --smtp (or MAIL_TRANSPORT=smtp). Without --execute it prints the text and sends nothing. At most one mail per Singapore day (state file) unless --force.

Usage:
  python scripts/daily_summary.py                      # print the summary
  python scripts/daily_summary.py --execute            # mail it to the local test sink
  python scripts/daily_summary.py --execute --smtp     # REAL mail (needs SMTP_* and SEND_ALLOWED_RECIPIENTS containing ALERT_TO; on UAT SEND_ALLOW_ALL_UAT=1 replaces the list, ALERT_TO must still be a valid address)
Exit code 0 normally; 1 when the summary itself could not be built or sent.
"""
import argparse
import datetime as dt
import email.policy
import json
import logging
import os
import re
import sys
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ops  # noqa: E402
import schedule_due as sd  # noqa: E402
import t4_refresh as t4  # noqa: E402
import t6_send as t6  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("daily_summary")
SCRIPTS = [("t2", "mapping sync", False), ("t11", "pause/takeover", True), ("t4", "extract refresh", True), ("t6", "report send", True)]
WATCH = ("no_allowed_recipient", "rejected", "halted", "schedule_unknown", "ambiguous", "send_failed", "export_failed", "oversize",
         "timed_out", "mapping_stale", "blocked", "not_ready", "deferred", "deferred_breaker", "breaker", "needs_human")


def kv(line):
    return {k: v for k, v in re.findall(r"(\w+)=(\S+)", line or "")}


def script_status(now, stale_min, env=os.environ):
    """[(script, label, text, problem?)] from the last-run markers."""
    out = []
    today = now.date().isoformat()
    for name, label, repeating in SCRIPTS:
        mk = ops.read_marker(name, env)
        day = ((mk or {}).get("days", {}).get(today, {})) if mk else {}
        ex = day.get("execute")
        if not ex:
            dry = day.get("dry-run")
            out.append((name, label, "DID NOT RUN today (no execute run recorded)" + (" - %d dry run(s) only" % dry["runs"] if dry else ""), True))
            continue
        try:   # the last EXECUTE run (a later dry run must not hide that the scheduled task stopped)
            last = dt.datetime.fromisoformat(ex["last_ts"]).replace(tzinfo=None)
        except Exception:
            last = None
        txt = "%d run(s), first %s, last %s, %d with a non-zero exit, %d skipped (PAT busy / kill switch)" % (
            ex["runs"], ex["first"], ex["last"], ex["bad"], ex["skipped"])
        bad = ex["bad"] > 0
        if repeating and last is not None and (now - last).total_seconds() / 60 > stale_min:
            txt += "; LAST RUN WAS %d MIN AGO - it seems to have stopped" % ((now - last).total_seconds() / 60)
            bad = True
        out.append((name, label, txt, bad))
    return out


def collect(cur, today, now, stale_min=90, env=os.environ):
    d = {"today": today, "scripts": script_status(now, stale_min, env), "allow_all_uat": ops.allow_all_uat(env)[1]}
    d["kill"] = ops.kill_switch_active(env)
    d["sent"] = cur.execute(
        "SELECT ISNULL((SELECT TOP 1 w.Workbook_Name FROM Integration.Tableau_WorkbookDataSource w WHERE w.Workbook_id = s.Workbook_id "
        "ORDER BY w.Active_Flag DESC, w.LastSeenAt DESC), '(unnamed)'), s.Subject, l.Status, CONVERT(varchar(5), SWITCHOFFSET(l.SentAt, '+08:00'), 108) "
        "FROM Integration.Tableau_SubscriptionSendLog l JOIN Integration.Tableau_Subscription s ON s.Subscription_id = l.Subscription_id "
        "WHERE l.SendDate = ? AND l.Status = 'Sent' ORDER BY 1, 2", today.isoformat()).fetchall()
    d["released"] = cur.execute("SELECT COUNT(*) FROM Integration.Tableau_SubscriptionSendLog WHERE SendDate = ? AND Status = 'Released'",
                                today.isoformat()).fetchone()[0]
    d["old_claims"] = cur.execute(
        "SELECT l.Subscription_id, l.SendDate, l.Status FROM Integration.Tableau_SubscriptionSendLog l WHERE l.SendDate < ? AND l.SendDate >= ? "
        "AND l.Status IN ('Claimed', 'TimedOut') ORDER BY l.SendDate", today.isoformat(),
        (today - dt.timedelta(days=sd.catch_up_default(env) + 4)).isoformat()).fetchall()
    d["errors_send"] = cur.execute(
        "SELECT l.Subscription_id, l.Status, LEFT(ISNULL(l.Message, ''), 160) FROM Integration.Tableau_SubscriptionSendLog l "
        "WHERE l.SendDate = ? AND l.Status IN ('Failed', 'Claimed', 'TimedOut') ORDER BY l.SendLog_id", today.isoformat()).fetchall()
    d["refresh"] = cur.execute(
        "SELECT ISNULL((SELECT TOP 1 x.DataSource_Name FROM Integration.Tableau_DataSource x WHERE x.ExtractRefreshTargetId = r.DataSource_id "
        "OR x.DataSource_id = r.DataSource_id), r.DataSource_id), r.Status, LEFT(ISNULL(r.Message, ''), 120) "
        "FROM Integration.Tableau_RefreshJob r WHERE r.SubmittedDate = ? ORDER BY r.RefreshJob_id", today.isoformat()).fetchall()
    d["repaused"] = cur.execute(
        "SELECT b.Subscription_id, b.RepauseCount FROM Integration.Tableau_SubscriptionBackup b "
        "WHERE b.LastRepausedDate = ? AND b.RestoredAt IS NULL", today.isoformat()).fetchall()
    d["halted"] = cur.execute(
        "SELECT b.Subscription_id, LEFT(ISNULL(b.Note, ''), 160) FROM Integration.Tableau_SubscriptionBackup b "
        "WHERE b.Halted = 1 AND b.RestoredAt IS NULL").fetchall()
    # due but not sent: only subscriptions the script owns (taken over, pause confirmed)
    eng = sd.DueEngine.load(cur, today, sd.catch_up_default(env), t6.default_max_age_h(env))
    gate = t6.Gate(cur, today)
    sent_ids = {r[0] for r in cur.execute("SELECT Subscription_id FROM Integration.Tableau_SubscriptionSendLog WHERE SendDate = ? "
                                         "AND Status = 'Sent'", today.isoformat()).fetchall()}
    state = {}
    for sid, st, msg in d["errors_send"]:
        state.setdefault(sid, []).append((st, msg))
    d["not_sent"] = []
    mode_honour = ops.schedule_mode(env) == "honour"
    for sub in t6.load_subscriptions(cur):
        if not (sub.taken_over and sub.paused) or sub.id in sent_ids:
            continue
        due = eng.status(sub.id, sub.sched_id, sub.paused_at)
        if mode_honour and due.status != "due":
            if due.status == "unsupported":
                d["not_sent"].append((sub.wb_name, sub.subject, "schedule unknown or stale: %s" % due.note))
            if due.missed:
                d["not_sent"].append((sub.wb_name, sub.subject, "MISSED: the occurrence of %s was never sent" % due.missed))
            continue
        if sub.halted:
            why = "takeover HALTED, needs a person"
        elif sub.id in state:
            why = "; ".join("%s: %s" % x for x in state[sub.id])
        else:
            ok, kind, why = gate.check(sub.wb_id)
            why = ("not sent although the data looks ready - check t6's log" if ok else "%s (%s)" % (why, kind))
        d["not_sent"].append((sub.wb_name, sub.subject, why))
    t6m = ops.read_marker("t6", env)
    last = kv((t6m or {}).get("summary", "")) if t6m and t6m.get("mode") == "execute" else {}
    d["t6_counters"] = {k: v for k, v in last.items() if k in WATCH and v not in ("0", "0(would)")}
    try:   # D-046: informational only (not an alert, not in attention())
        d["awaiting_sync"] = int(last.get("awaiting_sync", 0))
    except ValueError:
        d["awaiting_sync"] = 0
    return d


def attention(d):
    return (sum(1 for x in d["scripts"] if x[3]) + len(d["not_sent"]) + len(d["errors_send"]) + len(d["halted"]) + len(d["old_claims"])
            + sum(1 for r in d["refresh"] if r[1] in ("Failed", "TimedOut", "Cancelled")) + len(d["t6_counters"]) + int(d["kill"][0]))


def render(d):
    L = ["Tableau subscription automation - daily summary for %s" % d["today"], ""]
    if d["kill"][0]:
        L += ["!! KILL SWITCH IS ACTIVE (%s): the scripts do nothing and taken-over subscriptions stay paused." % d["kill"][1], ""]
    if d.get("allow_all_uat"):
        L += ["NOTE: SEND_ALLOW_ALL_UAT is ON (UAT Tableau): the recipient allow-list is not applied; every subscriber of a taken-over subscription gets the mail.", ""]
    L.append("SCRIPTS (from the last-run markers on this host)")
    for name, label, txt, bad in d["scripts"]:
        L.append("  %s%-4s %-16s %s" % ("!! " if bad else "   ", name, label, txt))
    L += ["", "SENT TODAY: %d%s" % (len(d["sent"]), ("  (plus %d local test delivery(ies) marked Released, not counted)" % d["released"]) if d["released"] else "")]
    L += ["   %s | %s | %s %s" % (w, (sub or "")[:60], st, at or "") for w, sub, st, at in d["sent"]]
    L += ["", "DUE BUT NOT SENT: %d" % len(d["not_sent"])]
    L += ["   %s | %s | %s" % (w, (sub or "")[:50], why) for w, sub, why in d["not_sent"]]
    L += ["", "EXTRACT REFRESHES TODAY: %d" % len(d["refresh"])]
    L += ["   %s | %s %s" % (n, st, ("- " + m) if m and st not in ("Succeeded",) else "") for n, st, m in d["refresh"]]
    L += ["", "RE-PAUSED today (someone resumed them): %d" % len(d["repaused"])]
    L += ["   subscription %s (x%d in total)" % (sid, n) for sid, n in d["repaused"]]
    if d["halted"]:
        L += ["", "HALTED takeovers (a person must clear them): %d" % len(d["halted"])] + ["   %s: %s" % tuple(x) for x in d["halted"]]   # a pyodbc Row is not a tuple: % would take it as ONE argument
    L += ["", "ERRORS in the send log today: %d" % len(d["errors_send"])]
    L += ["   %s | %s | %s" % tuple(x) for x in d["errors_send"]]
    if d["old_claims"]:
        L += ["", "CLAIMED / TIMED-OUT rows from EARLIER days (they still count as 'sent' for their occurrence until a person releases them): %d"
              % len(d["old_claims"])] + ["   %s | %s | %s" % (sid, day, st) for sid, day, st in d["old_claims"]]
    if d.get("awaiting_sync"):
        L += ["", "WAITING FOR THE NEXT DAILY SYNC (workbook republished after this morning's T2 sync; no action needed, see t6 log for the names): %d"
              % d["awaiting_sync"]]
    if d["t6_counters"]:
        L += ["", "Counters of the last t6 run that need a look: " + ", ".join("%s=%s" % kv_ for kv_ in sorted(d["t6_counters"].items()))]
    return "\n".join(L) + "\n"


def build_mail(cfg, d, to_header):
    msg = EmailMessage()
    n = attention(d)
    msg["Subject"] = "Tableau subscription automation: daily summary %s (%s)" % (d["today"], "%d item(s) need attention" % n if n else "all quiet")
    msg["From"] = formataddr((cfg.sender, cfg.sender))
    msg["To"] = to_header
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.sender.split("@")[-1])
    msg.set_content(render(d))
    return msg


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="send the mail (local test SMTP unless --ses)")
    ap.add_argument("--smtp", "--ses", dest="ses", action="store_true")
    ap.add_argument("--local-smtp", action="store_true", help="force the local test sink even if MAIL_TRANSPORT=smtp")
    ap.add_argument("--force", action="store_true", help="send even if today's summary was already sent")
    ap.add_argument("--stale-min", type=int, default=90, help="a repeating script whose last run is older than this is reported as stopped")
    ap.add_argument("--local-host", default="127.0.0.1")
    ap.add_argument("--local-port", type=int, default=1025)
    a = ap.parse_args(argv)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    log.setLevel(logging.INFO)
    if not log.handlers:
        for h in (logging.StreamHandler(sys.stdout), ops.log_file_handler("daily_summary")):
            h.setFormatter(fmt)
            log.addHandler(h)
            ops.log.addHandler(h)
            ops.log.setLevel(logging.INFO)
    try:
        cfg = ops.alert_config(a.ses, os.environ, "summary", execute=a.execute, local_smtp=a.local_smtp)
        a.ses = cfg.ses
        ops.log_transport("daily_summary", cfg.ses, a.execute)
        ops.check_alert_recipient(cfg, "daily_summary")
        conn = t4.connect()
        cur = conn.cursor()
        today = t4.dwh_today(cur)
        d = collect(cur, today, dt.datetime.now(ops.SGT).replace(tzinfo=None), a.stale_min)
    except (Exception, SystemExit) as e:
        log.error("RUN SUMMARY daily_summary FATAL=%s needs_human=1%s", t6.sanitize(e, 200), ops.allow_all_marker())
        return 1
    text = render(d)
    print(text)
    n = attention(d)
    if not a.execute:
        log.info("RUN SUMMARY daily_summary mode=dry-run attention=%d (not sent)%s", n, ops.allow_all_marker())
        return 0
    state_file = ops.state_dir() / ("daily_summary_state%s.json" % ("" if a.ses else "_local"))
    try:
        sent_days = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        sent_days = {}
    if str(today) in sent_days and not a.force:
        log.info("RUN SUMMARY daily_summary mode=execute already_sent_today=1%s", ops.allow_all_marker())
        return 0
    to, _ = t6.filter_allowed(cfg, [cfg.alert_to])
    if not to:
        log.error("RUN SUMMARY daily_summary FATAL=ALERT_TO is not on SEND_ALLOWED_RECIPIENTS (or not a valid address), summary NOT sent needs_human=1%s", ops.allow_all_marker())
        return 1
    mailer = t6.make_mailer(cfg, a.local_host, a.local_port)
    msg = build_mail(cfg, d, ", ".join(to) if cfg.ses else t6.LOCAL_TO)
    try:
        mailer.send(msg.as_bytes(policy=email.policy.SMTP), cfg.sender if cfg.ses else t6.LOCAL_FROM, to)
    except Exception as e:
        log.error("RUN SUMMARY daily_summary FATAL=mail not sent (%s) needs_human=1%s", t6.sanitize(e, 160), ops.allow_all_marker())
        return 1
    sent_days = {str(today): True}
    try:
        state_file.write_text(json.dumps(sent_days), encoding="utf-8")
    except Exception:
        pass
    log.info("RUN SUMMARY daily_summary mode=execute sent=1 attention=%d%s", n, ops.allow_all_marker())
    ops.write_marker("daily_summary", "execute", 0, "sent attention=%d" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
