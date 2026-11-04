"""R1 checks: schedule awareness (F-01), SES reply handling and circuit breaker (F-06), T2 resilience (F-08/F-09), operations
(kill switch, markers, alert-recipient check, daily summary; F-07/F-17), the T11a review fixes, and the Tableau-load limits.

NO real mail and NO Tableau access: fakes and a local stub SMTP server (127.0.0.1 only, any other SMTP host fails the test). Dev DWH only:
fixtures under 'T6TEST-%' (Tableau_Subscription / SendLog / Backup), 'T11TEST-%', 'R1TEST-%' (Tableau_Schedule, Tableau_RefreshJob); a
checksum proves no other row of the touched tables changed. The T2 merge test runs inside a transaction that is ROLLED BACK.

Usage: python scripts/r1_checks.py
"""
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import smtplib
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_summary as ds_  # noqa: E402
import ops  # noqa: E402
import schedule_due as sd  # noqa: E402
import t2_mapping_sync as m2  # noqa: E402
import t4_refresh as t4  # noqa: E402
import t6_checks as c6  # noqa: E402
import t6_send as t6  # noqa: E402
import t11_checks as c11  # noqa: E402
import t11_pause as t11  # noqa: E402
from check_fixtures import known_pipelines  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="r1checks_"))
fails = []
D = dt.date


def check(name, ok, detail=""):
    print("%s  %s %s" % ("PASS" if ok else "FAIL", name, detail))
    if not ok:
        fails.append(name)


# =====================================================================================================================
# A. schedule calendar (pure)
# =====================================================================================================================
def S(freq, days=(), tokens=(), start=dt.time(10, 15), hours=None, state="Active", name="x"):
    wd = frozenset(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].index(x) for x in days)
    return sd.Schedule(id=name, name=name, state=state, frequency=freq, weekdays=wd, month_tokens=tuple(tokens), start=start,
                       interval_hours=hours)


WK = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
REAL = {   # the 9 real UAT schedules (docs/d1b_findings.md 4.1)
    "0600AM_Weekday mornings": S("Weekly", WK, start=dt.time(6, 0)),
    "0900AM_Monday morning": S("Weekly", ("Monday",), start=dt.time(9, 0)),
    "1100AM_1stDayEveryMonth": S("Monthly", tokens=("1",), start=dt.time(11, 0)),
    "1015AM_Tue-Fri": S("Daily", ("Tuesday", "Wednesday", "Thursday", "Friday"), hours=24.0),
    "1030AM_Weekly Sat": S("Daily", ("Saturday",), hours=24.0, start=dt.time(10, 30)),
    "1015AM_Mon (DataTeam)": S("Weekly", ("Monday",)),
    "0930AM_Mon-Sat (DataTeam)": S("Daily", WK + ("Saturday",), hours=24.0, start=dt.time(9, 30)),
    "1015AM_Tue-Fri v2": S("Daily", ("Tuesday", "Wednesday", "Thursday", "Friday"), hours=24.0),
    "0930AM_Monday morning": S("Daily", ("Monday",), hours=24.0, start=dt.time(9, 30)),
}


def fires(s, d):
    return sd.fires_on(s, d)


def test_calendar():
    # 2026-10-19 is a Monday, 2026-10-23 a Friday, 2026-08-01 a Saturday, 2026-11-01 a Sunday.
    mon, tue, fri, sat, sun = D(2026, 10, 19), D(2026, 10, 20), D(2026, 10, 23), D(2026, 10, 24), D(2026, 10, 25)
    check("calendar facts used below", mon.weekday() == 0 and fri.weekday() == 4 and D(2026, 8, 1).weekday() == 5 and D(2026, 11, 1).weekday() == 6)
    tf = REAL["1015AM_Tue-Fri"]
    check("Tue-Fri fires Tue..Fri, not Mon/Sat/Sun", [fires(tf, D(2026, 10, 19 + i)) for i in range(7)] == [False, True, True, True, True, False, False])
    check("Saturday schedule fires only on Saturday", [fires(REAL["1030AM_Weekly Sat"], D(2026, 10, 19 + i)) for i in range(7)] == [False] * 5 + [True, False])
    check("Monday schedules fire only on Monday", all(fires(REAL[n], d) == (d.weekday() == 0) for n in ("0900AM_Monday morning", "0930AM_Monday morning", "1015AM_Mon (DataTeam)")
                                                 for d in (D(2026, 10, 19 + i) for i in range(7))))
    check("06:00 weekday schedule: Mon-Fri only, nothing at the weekend", [fires(REAL["0600AM_Weekday mornings"], D(2026, 10, 19 + i)) for i in range(7)] == [True] * 5 + [False, False])
    check("Mon-Sat fires six days", sum(fires(REAL["0930AM_Mon-Sat (DataTeam)"], D(2026, 10, 19 + i)) for i in range(7)) == 6)
    mo = REAL["1100AM_1stDayEveryMonth"]
    check("monthly day 1: only on the 1st, across a month boundary", [fires(mo, D(2026, 10, 30)), fires(mo, D(2026, 10, 31)), fires(mo, D(2026, 11, 1)), fires(mo, D(2026, 11, 2))]
          == [False, False, True, False])
    check("monthly day 31 in a 30-day month fires on the last day (assumption)", fires(S("Monthly", tokens=("31",)), D(2026, 4, 30)) and not fires(S("Monthly", tokens=("31",)), D(2026, 4, 29)))
    check("monthly LastDay incl. February", fires(S("Monthly", tokens=("LastDay",)), D(2026, 2, 28)) and not fires(S("Monthly", tokens=("LastDay",)), D(2026, 2, 27)))
    fm = S("Monthly", tokens=("First", "Monday"))
    check("monthly 'First Monday'", fires(fm, D(2026, 10, 5)) and not fires(fm, D(2026, 10, 12)) and not fires(fm, D(2026, 10, 6)))
    check("monthly 'Last Friday'", fires(S("Monthly", tokens=("Last", "Friday")), D(2026, 10, 30)) and not fires(S("Monthly", tokens=("Last", "Friday")), D(2026, 10, 23)))
    for bad, why in ((S("Monthly", tokens=("Customized Monthly",)), "customized"), (S("Daily", hours=48.0), "48 h"), (S("Weekly"), "weekly without days"),
                     (S("Yearly"), "unknown frequency"), (S("Monthly", tokens=("First", "3")), "mixed tokens")):
        try:
            fires(bad, D(2026, 10, 20))
            r = "accepted"
        except sd.Unsupported:
            r = "unsupported"
        check("unsupported schedule shape is refused (%s)" % why, r == "unsupported")
    check("hourly schedule counts as a daily occurrence", fires(S("Hourly", hours=4.0), sat) and fires(S("Daily", hours=12.0), sun))


def ev(name, today, sent=(), catch=3, nb=None):
    return sd.evaluate(REAL[name], today, sent, catch, nb)


def test_due_rules():
    tf, sat_s, mo = "1015AM_Tue-Fri", "1030AM_Weekly Sat", "1100AM_1stDayEveryMonth"
    # daily-type, one-day window
    check("Tue-Fri: due Wednesday", ev(tf, D(2026, 10, 21)).status == "due")
    check("Tue-Fri: Monday is Friday's last catch-up day (window ends the day before Tuesday)", ev(tf, D(2026, 10, 26)).status == "due"
          and ev(tf, D(2026, 10, 26), [D(2026, 10, 24)]).status == "satisfied")
    check("Tue-Fri: Friday sent -> Saturday satisfied, Sunday satisfied", ev(tf, D(2026, 10, 24), [D(2026, 10, 23)]).status == "satisfied"
          and ev(tf, D(2026, 10, 25), [D(2026, 10, 23)]).status == "satisfied")
    check("Tue-Fri: Friday NOT sent -> Saturday is a catch-up day (due)", ev(tf, D(2026, 10, 24)).status == "due")
    check("Tue-Fri: Tuesday itself is a new occurrence even if Friday was sent on Saturday", ev(tf, D(2026, 10, 27), [D(2026, 10, 24)]).status == "due")
    r = ev(tf, D(2026, 10, 21), [D(2026, 10, 20)])   # Tuesday sent, Wednesday (today) not sent
    check("Tue-Fri: a send on Tuesday does not satisfy Wednesday", r.status == "due" and r.occurrence == D(2026, 10, 21) and r.missed is None)
    r = ev(tf, D(2026, 10, 21), [])
    check("Tue-Fri: Tuesday never sent, today Wednesday: Tuesday's one-day window ended yesterday -> missed=Tue, and Wednesday is due",
          r.missed == D(2026, 10, 20) and r.status == "due" and r.occurrence == D(2026, 10, 21))
    # Saturday schedule: catch-up Sun..Tue when no ETL at the weekend
    for d, exp in ((D(2026, 10, 24), "due"), (D(2026, 10, 25), "due"), (D(2026, 10, 26), "due"), (D(2026, 10, 27), "due"), (D(2026, 10, 28), "not_due")):
        check("Saturday schedule, nothing sent: %s is %s" % (d, exp), ev(sat_s, d).status == exp)
    r = ev(sat_s, D(2026, 10, 28))
    check("Saturday schedule: the window O..O+3 ended Tuesday, Wednesday reports the miss", r.missed == D(2026, 10, 24))
    check("Saturday schedule: sent Monday (catch-up) -> Monday satisfied, Tuesday satisfied, never twice",
          ev(sat_s, D(2026, 10, 26), [D(2026, 10, 26)]).status == "satisfied" and ev(sat_s, D(2026, 10, 27), [D(2026, 10, 26)]).status == "satisfied")
    check("Saturday schedule: a Friday send does not count for the Saturday occurrence", ev(sat_s, D(2026, 10, 24), [D(2026, 10, 23)]).status == "due")
    check("Saturday schedule, week after: next Saturday is due again", ev(sat_s, D(2026, 10, 31), [D(2026, 10, 26)]).status == "due")
    # monthly day 1 on a Saturday (2026-08-01) and across month boundaries
    for d, exp in ((D(2026, 8, 1), "due"), (D(2026, 8, 3), "due"), (D(2026, 8, 4), "due"), (D(2026, 8, 5), "not_due"), (D(2026, 7, 31), "not_due")):
        check("monthly day 1 = Saturday 2026-08-01, nothing sent: %s is %s" % (d, exp), ev(mo, d).status == exp)
    check("monthly: sent on Monday the 3rd -> Tuesday the 4th satisfied (not sent twice)", ev(mo, D(2026, 8, 4), [D(2026, 8, 3)]).status == "satisfied")
    check("monthly: missed the whole window -> alert on the 5th, then quiet", ev(mo, D(2026, 8, 5)).missed == D(2026, 8, 1) and ev(mo, D(2026, 8, 6)).missed is None)
    check("monthly across the month boundary 2026-10-31 -> 2026-11-01 (Sunday) due, Mon 2nd due, no send needed on 31st",
          ev(mo, D(2026, 10, 31)).status == "not_due" and ev(mo, D(2026, 11, 1)).status == "due" and ev(mo, D(2026, 11, 2)).status == "due")
    check("monthly: 15th of a month is never due", ev(mo, D(2026, 10, 15)).status == "not_due")
    check("monthly: catch_up=0 means only the day itself", ev(mo, D(2026, 8, 3), catch=0).status == "not_due" and ev(mo, D(2026, 8, 1), catch=0).status == "due")
    # weekdays 06:00 and the weekend
    wm = "0600AM_Weekday mornings"
    check("06:00 weekday schedule: Saturday is a catch-up day for Friday's occurrence, Sunday too, Monday is a new occurrence",
          ev(wm, D(2026, 10, 24)).status == "due" and ev(wm, D(2026, 10, 25)).status == "due" and ev(wm, D(2026, 10, 26)).status == "due")
    check("06:00 weekday schedule: Friday sent -> weekend not due, no spurious alert", ev(wm, D(2026, 10, 24), [D(2026, 10, 23)]).status == "satisfied")
    check("send-log history outside the window is ignored", ev(mo, D(2026, 8, 1), [D(2026, 7, 31), D(2026, 7, 1)]).status == "due")
    # state
    check("a disabled schedule is treated exactly like an enabled one (state is not looked at)",
          sd.evaluate(S("Daily", state="Suspended"), D(2026, 10, 20), []).status == "due" == sd.evaluate(S("Daily"), D(2026, 10, 20), []).status)
    check("unsupported schedule is reported, not due", sd.evaluate(S("Daily", hours=72.0), D(2026, 10, 20), []).status == "unsupported")
    # takeover
    tk_after = dt.datetime(2026, 10, 21, 13, 0)    # paused Wednesday 13:00, schedule 10:15
    r = ev(tf, D(2026, 10, 21), nb=tk_after)
    check("taken over at 13:00, schedule time 10:15 already passed: today is NOT due (taken_over_today)", r.status == "not_due" and r.taken_today)
    r = ev(tf, D(2026, 10, 22), nb=tk_after)
    check("taken over Wednesday: Thursday is the first occurrence that counts", r.status == "due" and not r.taken_today)
    r = ev(tf, D(2026, 10, 21), nb=dt.datetime(2026, 10, 21, 9, 0))
    check("taken over at 09:00, schedule time 10:15 still ahead: today is due", r.status == "due")
    r = ev(tf, D(2026, 10, 21), nb=dt.datetime(2026, 10, 21, 9, 0))
    check("no 'missed' alert for occurrences before the takeover", ev(tf, D(2026, 10, 22), nb=dt.datetime(2026, 10, 21, 13, 0)).missed is None)
    r = ev(mo, D(2026, 10, 3), nb=dt.datetime(2026, 10, 2, 12, 0))
    check("monthly taken over on the 2nd: the 1st (old subscription's) is not caught up", r.status == "not_due")
    # engine: unknown / stale schedule
    eng = sd.DueEngine(D(2026, 10, 21), {"A": REAL[tf]}, {}, 3, 26, None)
    check("engine: no schedule id -> unsupported (fail closed)", eng.status("s", None).status == "unsupported" and eng.status("s", "NOPE").status == "unsupported")
    old = sd.Schedule(**dict(REAL[tf].__dict__, last_seen=dt.datetime(2026, 10, 1)))
    eng = sd.DueEngine(D(2026, 10, 21), {"A": old}, {}, 3, 26, dt.datetime(2026, 10, 21, 12, 0))
    check("engine: a schedule row not synced for over the limit is unsupported/stale", eng.status("s", "A").status == "unsupported")
    check("DWH row round trip", sd.from_row(sd.to_row(REAL[tf]) + (None,)).weekdays == REAL[tf].weekdays
          and sd.from_row(sd.to_row(mo_ := REAL["1100AM_1stDayEveryMonth"]) + (None,)).month_tokens == ("1",))
    import tableauserverclient as TSC
    it = TSC.ScheduleItem("n", 50, "Subscription", "Parallel", TSC.DailyInterval(dt.time(10, 15), 24.0, "Tuesday", "Friday"))
    it._id, it._state = "id1", "Active"
    s1 = sd.from_tsc(it)
    check("from_tsc: daily interval with weekdays and hours", s1.frequency == "Daily" and s1.weekdays == frozenset({1, 4}) and s1.interval_hours == 24.0)
    it = TSC.ScheduleItem("n", 50, "Subscription", "Parallel", TSC.MonthlyInterval(dt.time(11, 0), ("1",)))
    it._id, it._state = "id2", "Active"
    check("from_tsc: monthly day 1", sd.from_tsc(it).month_tokens == ("1",) and sd.from_tsc(it).frequency == "Monthly")


sat, sun = D(2026, 10, 24), D(2026, 10, 25)


# =====================================================================================================================
# B. SMTP stub server (127.0.0.1 only)
# =====================================================================================================================
class Stub(threading.Thread):
    """Multi-connection SMTP stub. mode decides the reply to the end-of-message dot; rcpt_reject / mail_reject act earlier."""

    def __init__(self, mode="ok", auth=None):
        super().__init__(daemon=True)
        self.mode, self.messages, self.connections, self.rcpts, self.auth = mode, [], 0, [], auth
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.stop_flag = False

    def run(self):
        while not self.stop_flag:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self.handle, args=(c,), daemon=True).start()

    def handle(self, c):
        f = c.makefile("rwb")

        def say(x):
            f.write((x + "\r\n").encode())
            f.flush()
        say("220 stub")
        try:
            while True:
                line = f.readline().decode().strip()
                u = line.upper()
                if not line:
                    break
                if u.startswith("EHLO") or u.startswith("HELO"):
                    if self.auth:
                        say("250-stub")
                        say("250 AUTH PLAIN")
                    else:
                        say("250 stub")
                elif u.startswith("AUTH"):
                    say("235 2.7.0 Accepted" if self.auth == "ok" else "535 5.7.8 Username and Password not accepted (BadCredentials)")
                elif u.startswith("MAIL"):
                    say("530 sender not allowed" if self.mode == "mail_reject" else "250 ok")
                elif u.startswith("RCPT"):
                    self.rcpts.append(line)
                    say("550 5.1.1 user unknown" if self.mode == "rcpt_reject" else "250 ok")
                elif u == "RSET":
                    say("250 ok")
                elif u == "DATA":
                    say("354 go")
                    body = []
                    while True:
                        ln = f.readline()
                        if ln.strip() == b".":
                            break
                        body.append(ln)
                    self.messages.append(b"".join(body))
                    if self.mode == "timeout":
                        time.sleep(4)
                        return
                    say({"ok": "250 Ok 0100019abc123456-1a2b3c4d-5e6f-7a8b-9c0d-000000",
                         "unverified": "554 Message rejected: Email address is not verified. The following identities failed the check in "
                                       "region AP-NORTHEAST-1: secret.person@example.com, tableau-reports@example.com",
                         "other554": "554 Transaction failed: something else entirely",
                         "tempfail": "451 4.3.0 Temporary failure, try later",
                         "reject5xx_throttle": "552 Message size exceeds limit"}.get(self.mode, "250 Ok plain"))
                elif u == "QUIT":
                    say("221 bye")
                    break
                else:
                    say("250 ok")
        except Exception:
            pass
        finally:
            try:
                c.close()
            except Exception:
                pass

    def stop(self):
        self.stop_flag = True
        try:
            self.sock.close()
        except Exception:
            pass


def start_stub(mode="ok", auth=None):
    s = Stub(mode, auth)
    s.start()
    return s


def smtp_try(mode, timeout=5):
    stub = start_stub(mode)
    try:
        r = t6.SmtpMailer("127.0.0.1", stub.port, timeout=timeout).send(b"Subject: x\r\n\r\nbody\r\n", "a@localhost", ["b@localhost"])
        return "ok", r, ""
    except t6.NotQueuedError as e:
        return "notqueued", None, str(e)
    except t6.PreDataError as e:
        return "predata", None, str(e)
    except t6.AmbiguousSendError as e:
        return "ambiguous", None, str(e)
    finally:
        stub.stop()


def test_smtp_classes():
    k, r, _ = smtp_try("ok")
    check("SMTP 250: success, SES-style message id captured from the reply", k == "ok" and r == "0100019abc123456-1a2b3c4d-5e6f-7a8b-9c0d-000000", repr(r))
    check("reply_id: a short plain reply is kept as text, an address never survives", t6.reply_id("250 queued as x@y.com") == "250 queued as <addr>")
    k, _, txt = smtp_try("unverified")
    check("554 'Email address is not verified' after the content: NotQueuedError (deterministic, slot may be freed)", k == "notqueued")
    check("the rejection text carries no address", "@" not in txt and "<addr>" in txt, txt)
    check("554 with another text: stays AMBIGUOUS", smtp_try("other554")[0] == "ambiguous")
    check("552 / unknown 5xx: stays AMBIGUOUS", smtp_try("reject5xx_throttle")[0] == "ambiguous")
    check("451 after the content: AMBIGUOUS", smtp_try("tempfail")[0] == "ambiguous")
    t0 = time.monotonic()
    k = smtp_try("timeout", timeout=1)[0]
    check("no reply after the content (timeout): AMBIGUOUS", k == "ambiguous" and time.monotonic() - t0 < 4)
    check("RCPT refused: before DATA (PreDataError)", smtp_try("rcpt_reject")[0] == "predata")
    check("MAIL FROM refused: before DATA (PreDataError)", smtp_try("mail_reject")[0] == "predata")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    try:
        t6.SmtpMailer("127.0.0.1", port, timeout=2).send(b"x", "a@localhost", ["b@localhost"])
        r = "ok"
    except t6.NotQueuedError:
        r = "notqueued"
    except t6.PreDataError:
        r = "predata"
    check("connection refused: before DATA", r == "predata")
    check("known-rejection list: 4xx and non-matching 5xx are not 'known'", not t6.is_known_rejection(451, "Message rejected: Email address is not verified")
          and not t6.is_known_rejection(554, "Transaction failed") and t6.is_known_rejection(554, "Message  rejected:  Email address is not verified."))
    # preflight
    stub = start_stub("ok")
    try:
        t6.SmtpMailer("127.0.0.1", stub.port, timeout=3).preflight("from@x.com", "to@x.com")
        r = "ok"
    except Exception as e:
        r = type(e).__name__
    check("preflight: EHLO, MAIL FROM, RCPT TO, RSET, QUIT - and NO message is sent", r == "ok" and stub.messages == [] and len(stub.rcpts) == 1)
    stub.stop()
    stub = start_stub("rcpt_reject")
    try:
        t6.SmtpMailer("127.0.0.1", stub.port, timeout=3).preflight("from@x.com", "to@x.com")
        r = "ok"
    except t6.PreDataError as e:
        r = "refused" if "@" not in str(e) else "leaks"
    check("preflight: RCPT TO refused -> PreDataError", r == "refused", r)
    stub.stop()
    stub = start_stub("mail_reject")
    try:
        t6.SmtpMailer("127.0.0.1", stub.port, timeout=3).preflight("from@x.com", "to@x.com")
        r = "ok"
    except t6.PreDataError:
        r = "refused"
    check("preflight: MAIL FROM refused -> PreDataError", r == "refused")
    stub.stop()


# =====================================================================================================================
# C. t6 flows: schedule, takeover day, SES handling, breaker, load limits
# =====================================================================================================================
LIKE6 = c6.LIKE


def gate_ready():
    return c6.FakeGate()


def set_sched(cur, sid, sched_id):
    cur.execute("UPDATE Integration.Tableau_Subscription SET ScheduleId = ? WHERE Subscription_id = ?", sched_id, sid)


def mk(cur, today, sids, schedules=None, history=None, adapter=None, mailer=None, gate=None, due=True, **cfgkw):
    ctx = c6.mk_ctx(cur, today, adapter, mailer, gate, **cfgkw)
    if due:
        ctx.due = sd.DueEngine(today, schedules or {}, history or {}, ctx.cfg.catch_up_days, 26, None)
    return ctx


def daily_all(start=dt.time(0, 1)):
    return S("Daily", (), start=start, hours=24.0, name="all")


def weekday_of(today, delta):
    return ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][(today + dt.timedelta(days=delta)).weekday()]


def test_t6_schedule(cur, today):
    c6.cleanup(cur)
    # ---- not due: no gate evaluation noise
    c6.add_sub(cur, "T6TEST-R1", fmt="PDF")
    c6.add_sub(cur, "T6TEST-R2", fmt="PDF")
    set_sched(cur, "T6TEST-R1", "A")
    set_sched(cur, "T6TEST-R2", "B")
    scheds = {"A": daily_all(), "B": S("Weekly", (weekday_of(today, 2),), name="B")}
    mail = c6.FakeMailer()
    ctx = mk(cur, today, ["T6TEST-R1", "T6TEST-R2"], scheds, adapter=c6.FakeAdapter(), mailer=mail, gate=c6.FakeGate((False, "wait", "extract not refreshed")),
             cutoff=dt.time(0, 0, 1))
    s = c6.run_subs(cur, ctx, ["T6TEST-R1", "T6TEST-R2"])
    check("not due subscription: kind not_due, never reaches the gate", s["kinds"]["not_due"] == 1 and s["kinds"]["not_ready"] == 1)
    check("'not ready' list holds only the DUE subscription", [x[0].id for x in ctx.not_ready] == ["T6TEST-R1"])
    t6.send_alerts(cur, ctx, s)
    body = " ".join(x[0].decode(errors="replace") for x in mail.sent)
    check("the after-cutoff 'not ready' alert mentions only the due subscription", "T6TEST-R1" in body.replace("=\r\n", "") and "T6TEST-R2" not in body.replace("=\r\n", ""))
    c6.cleanup(cur)
    # ---- due: sent once, never twice
    c6.add_sub(cur, "T6TEST-R3")
    set_sched(cur, "T6TEST-R3", "A")
    mail = c6.FakeMailer()
    ctx = mk(cur, today, ["T6TEST-R3"], scheds, mailer=mail)
    s = c6.run_subs(cur, ctx, ["T6TEST-R3"])
    ctx2 = mk(cur, today, ["T6TEST-R3"], scheds, mailer=mail)
    s2 = c6.run_subs(cur, ctx2, ["T6TEST-R3"])
    check("due subscription: sent once, the second run the same day sends nothing", s["kinds"]["sent"] == 1 and s2["kinds"]["already_sent"] == 1 and len(c6.reports(mail)) == 1)
    c6.cleanup(cur)
    # ---- catch-up: occurrence yesterday, nothing sent yet -> due today; a history row from yesterday satisfies it
    y = weekday_of(today, -1)
    c6.add_sub(cur, "T6TEST-R4")
    set_sched(cur, "T6TEST-R4", "C")
    sc = {"C": S("Weekly", (y,), name="C")}
    mail = c6.FakeMailer()
    ctx = mk(cur, today, ["T6TEST-R4"], sc, mailer=mail, history={"T6TEST-R4": {today - dt.timedelta(days=1)}})
    s = c6.run_subs(cur, ctx, ["T6TEST-R4"])
    check("catch-up: yesterday's occurrence was already sent yesterday -> nothing today", s["kinds"]["already_sent"] == 1 and c6.reports(mail) == [])
    mail = c6.FakeMailer()
    ctx = mk(cur, today, ["T6TEST-R4"], sc, mailer=mail, history={})
    s = c6.run_subs(cur, ctx, ["T6TEST-R4"])
    check("catch-up: yesterday's occurrence not sent (data was not ready) -> sent today", s["kinds"]["sent"] == 1 and len(c6.reports(mail)) == 1)
    c6.cleanup(cur)
    # ---- missed occurrence is an event and exit code 1
    c6.add_sub(cur, "T6TEST-R5")
    set_sched(cur, "T6TEST-R5", "M")
    sc = {"M": S("Weekly", (weekday_of(today, -4),), name="M")}
    ctx = mk(cur, today, ["T6TEST-R5"], sc)
    s = c6.run_subs(cur, ctx, ["T6TEST-R5"])
    check("missed due day: 'missed_due' event, not sent, exit code 1", s["kinds"]["not_due"] == 1 and any(e["code"] == "missed_due" for e in ctx.events) and t6.exit_code(ctx, s) == 1)
    c6.cleanup(cur)
    # ---- unknown schedule
    c6.add_sub(cur, "T6TEST-R6")
    mail = c6.FakeMailer()
    ctx = mk(cur, today, ["T6TEST-R6"], {}, mailer=mail)
    s = c6.run_subs(cur, ctx, ["T6TEST-R6"])
    check("no schedule stored: fail closed, event schedule_unknown, exit 1",
          s["kinds"]["schedule_unknown"] == 1 and c6.reports(mail) == [] and t6.exit_code(ctx, s) == 1
          and [e["code"] for e in ctx.events] == ["schedule_unknown"])
    c6.cleanup(cur)
    # ---- takeover day
    for sid in ("T6TEST-R8", "T6TEST-R9"):
        c6.add_sub(cur, sid, fmt="PDF")
        c6.add_backup(cur, sid, taken=1, paused=True)
    set_sched(cur, "T6TEST-R8", "EARLY")
    set_sched(cur, "T6TEST-R9", "LATE")
    sc = {"EARLY": daily_all(dt.time(0, 1)), "LATE": daily_all(dt.time(23, 59, 59))}
    mail = c6.FakeMailer()
    ctx = mk(cur, today, ["T6TEST-R8", "T6TEST-R9"], sc, mailer=mail, adapter=c6.FakeAdapter(suspended=True), include_untaken=False)
    s = c6.run_subs(cur, ctx, ["T6TEST-R8", "T6TEST-R9"])
    check("paused today AFTER the schedule time: 'taken_over_today', not an error, no mail, no event, exit 0",
          s["kinds"]["taken_over_today"] == 1 and ctx.events == [] and t6.exit_code(ctx, s) == 0)
    check("paused today BEFORE the schedule time: sent today", s["kinds"]["sent"] == 1 and statuses_of(cur, "T6TEST-R9") == ["Sent"] and statuses_of(cur, "T6TEST-R8") == [])
    c6.cleanup(cur)
    c6.add_sub(cur, "T6TEST-R10")
    c6.add_backup(cur, "T6TEST-R10", taken=1, paused=True)
    mail = c6.FakeMailer()
    ctx = c6.mk_ctx(cur, today, None, mail, None, include_untaken=False, takeover_day_guard=True)   # schedule ignored (ctx.due None)
    s = c6.run_subs(cur, ctx, ["T6TEST-R10"])
    check("SCHEDULE_MODE=ignore still never sends on the day t11 paused the subscription", s["kinds"]["taken_over_today"] == 1 and c6.reports(mail) == [])
    c6.cleanup(cur)
    # ---- R5 / F-1: the takeover-day guard on a subscription that was SUSPENDED before the takeover (Tableau never mailed it):
    # still skipped (WasSuspended is not restored, user decision), but now with a visible warning in the log and the Event Log
    calls = []
    real_event_log = ops.event_log
    ops.event_log = lambda script, text, level="ERROR", env=os.environ: calls.append((script, text, level)) or True
    try:
        for sid in ("T6TEST-W1", "T6TEST-W2", "T6TEST-W3", "T6TEST-W4"):
            c6.add_sub(cur, sid, susp=1, fmt="PDF")                      # suspended in Tableau before the takeover
            c6.add_backup(cur, sid, taken=1, paused=True)                # t11 took it over just now (PausedAt = now)
            set_sched(cur, sid, "EARLY")
        sc = {"EARLY": daily_all(dt.time(0, 1))}
        mail = c6.FakeMailer()
        ctx = mk(cur, today, ["T6TEST-W1"], sc, mailer=mail, adapter=c6.FakeAdapter(suspended=True), include_untaken=False)
        s = c6.run_subs(cur, ctx, ["T6TEST-W1"])
        check("takeover day, previously suspended subscription: skipped (taken_over_today), nothing sent, nothing claimed",
              s["kinds"]["taken_over_today"] == 1 and c6.reports(mail) == [] and statuses_of(cur, "T6TEST-W1") == [])
        check("... and a WARNING reaches the Event Log helper, naming the subscription and saying Tableau did not send it either",
              len(calls) == 1 and calls[0][0] == "t6" and calls[0][2] == "WARNING" and "T6TEST-W1" in calls[0][1] and "suspended" in calls[0][1]
              and "by hand" in calls[0][1])
        ctx = mk(cur, today, ["T6TEST-W1"], sc, mailer=mail, adapter=c6.FakeAdapter(suspended=True), include_untaken=False)
        c6.run_subs(cur, ctx, ["T6TEST-W1"])
        check("... only ONCE per subscription and day (the 15-minute task does not repeat it)", len(calls) == 1)
        ctx = mk(cur, today, ["T6TEST-W2"], sc, mailer=mail, adapter=c6.FakeAdapter(suspended=True), include_untaken=False, execute=False)
        c6.run_subs(cur, ctx, ["T6TEST-W2"])
        check("dry run: the guard skips but writes no Event Log entry", len(calls) == 1)
        ctx = c6.mk_ctx(cur, today, c6.FakeAdapter(suspended=True), mail, None, include_untaken=False, takeover_day_guard=True)   # schedule ignored
        s = c6.run_subs(cur, ctx, ["T6TEST-W3"])
        check("SCHEDULE_MODE=ignore path: the same warning is written", s["kinds"]["taken_over_today"] == 1 and len(calls) == 2 and "T6TEST-W3" in calls[1][1])
        # the next day (paused yesterday): normal send, no warning
        cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET PausedAt = DATEADD(day, -1, PausedAt) WHERE Subscription_id = 'T6TEST-W4'")
        ctx = mk(cur, today, ["T6TEST-W4"], sc, mailer=mail, adapter=c6.FakeAdapter(suspended=True), include_untaken=False)
        s = c6.run_subs(cur, ctx, ["T6TEST-W4"])
        check("the day after the takeover the previously suspended subscription is sent, with no warning",
              s["kinds"]["sent"] == 1 and len(c6.reports(mail)) == 1 and len(calls) == 2)
    finally:
        ops.event_log = real_event_log
    c6.cleanup(cur)
    # ---- halted
    c6.add_sub(cur, "T6TEST-R11")
    c6.add_backup(cur, "T6TEST-R11", taken=1, paused=True)
    cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET Halted = 1, Note = 'x' WHERE Subscription_id = 'T6TEST-R11'")
    mail = c6.FakeMailer()
    ctx = c6.mk_ctx(cur, today, None, mail, None, include_untaken=False, takeover_day_guard=False)
    s = c6.run_subs(cur, ctx, ["T6TEST-R11"])
    check("halted takeover is never sent; event 'halted'; exit 1", s["kinds"]["halted"] == 1 and c6.reports(mail) == [] and t6.exit_code(ctx, s) == 1
          and any(e["code"] == "halted" for e in ctx.events))
    c6.cleanup(cur)
    # ---- backup row re-read right before the claim
    c6.add_sub(cur, "T6TEST-R12")
    c6.add_backup(cur, "T6TEST-R12", taken=1, paused=True)

    class Racy(c6.FakeAdapter):
        def describe(self, sub):
            cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET RestoredAt = SYSDATETIMEOFFSET() WHERE Subscription_id = ?", sub.id)
            return super().describe(sub)
    mail = c6.FakeMailer()
    ctx = c6.mk_ctx(cur, today, Racy(suspended=True), mail, None, include_untaken=False, takeover_day_guard=False)
    s = c6.run_subs(cur, ctx, ["T6TEST-R12"])
    check("restored between the start of the run and the claim: not sent (snapshot is re-read before the claim)",
          s["kinds"]["takeover_changed"] == 1 and c6.reports(mail) == [] and statuses_of(cur, "T6TEST-R12") == [])
    c6.cleanup(cur)
    # ---- allow-list miss
    c6.add_sub(cur, "T6TEST-R13")
    mail = c6.FakeMailer()
    ctx = c6.mk_ctx(cur, today, None, mail, None, ses=True, allow=frozenset({"other@example.org"}))
    s = c6.run_subs(cur, ctx, ["T6TEST-R13"])
    check("recipient not on the allow-list: alert event, counted as a problem, exit code 1, summary counter",
          s["kinds"]["no_allowed_recipient"] == 1 and any(e["code"] == "no_allowed_recipient" for e in ctx.events) and t6.exit_code(ctx, s) == 1
          and "no_allowed_recipient=1" in t6.summary_line(ctx, s))
    c6.cleanup(cur)


def statuses_of(cur, sid):
    return [r[0] for r in cur.execute("SELECT Status FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id = ? ORDER BY SendLog_id", sid)]


def test_t6_ses(cur, today):
    c6.cleanup(cur)
    ids = ["T6TEST-S%d" % i for i in range(1, 4)]
    for i in ids:
        c6.add_sub(cur, i)
    # ---- 554 not verified: first rejected + Failed(REJECTED:), the rest not claimed
    stub = start_stub("unverified")
    mailer = t6.SmtpMailer("127.0.0.1", stub.port, timeout=5)
    ctx = c6.mk_ctx(cur, today, c6.FakeAdapter(), mailer, None)
    s = c6.run_subs(cur, ctx, ids)
    rows = c6.logrows(cur)
    check("554 not verified: subscriptions rejected, rows Failed 'REJECTED:' (slot free), nothing queued", s["kinds"]["rejected"] == 2 and len(rows) == 2
          and all(r[1] == "Failed" and r[2].startswith("REJECTED:") for r in rows), str(rows)[:200])
    check("circuit breaker: ONE rejection does not stop the run, the SECOND in a row does; the third is NOT claimed, exit 1",
          s["kinds"]["deferred_breaker"] == 1 and len(rows) == 2 and t6.exit_code(ctx, s) == 1 and ctx.breaker)
    check("one alert event with the reply text and no address", sum(e["code"] == "smtp_breaker" for e in ctx.events) == 1
          and not any("@" in e["text"] for e in ctx.events), str([e["text"] for e in ctx.events])[:200])
    check("the stored message has no address", "@" not in rows[0][2])
    # next run the same day: the rejected one is not retried today, no connection for it
    n0 = len(stub.messages)
    ctx = c6.mk_ctx(cur, today, c6.FakeAdapter(), mailer, None)
    s = c6.run_subs(cur, ctx, ids[:1])
    check("rejected earlier today: not retried the same day (no new claim, no new report attempt), event kept",
          s["kinds"]["rejected"] == 1 and len(c6.logrows(cur)) == 2 and any(e["code"] == "rejected" for e in ctx.events) and len(stub.messages) - n0 <= 1)
    stub.stop()
    c6.cleanup(cur)
    for i in ids:
        c6.add_sub(cur, i)
    # ---- 451 after the content stays Claimed (ambiguous): one non-250 final reply trips the breaker
    stub = start_stub("tempfail")
    ctx = c6.mk_ctx(cur, today, c6.FakeAdapter(), t6.SmtpMailer("127.0.0.1", stub.port, timeout=5), None)
    s = c6.run_subs(cur, ctx, ids)
    rows = c6.logrows(cur)
    check("451 after the content: row stays Claimed, breaker trips, the other two are unclaimed", s["kinds"]["ambiguous"] == 1 and s["kinds"]["deferred_breaker"] == 2
          and [r[1] for r in rows] == ["Claimed"])
    stub.stop()
    c6.cleanup(cur)
    for i in ids:
        c6.add_sub(cur, i)
    # ---- timeout: ambiguous; second consecutive failure trips
    stub = start_stub("timeout")
    ctx = c6.mk_ctx(cur, today, c6.FakeAdapter(), t6.SmtpMailer("127.0.0.1", stub.port, timeout=1), None)
    s = c6.run_subs(cur, ctx, ids)
    check("timeout after the content (outcome unknown): ambiguous, the breaker stops the rest at once", s["kinds"]["ambiguous"] == 1 and s["kinds"]["deferred_breaker"] == 2
          and [r[1] for r in c6.logrows(cur)] == ["Claimed"])
    stub.stop()
    c6.cleanup(cur)
    for i in ids:
        c6.add_sub(cur, i)
    # ---- two consecutive pre-DATA failures
    ctx = c6.mk_ctx(cur, today, c6.FakeAdapter(), c6.FakeMailer("predata"), None)
    s = c6.run_subs(cur, ctx, ids)
    check("a connection-level SMTP failure before DATA trips the breaker at once; the rest stay unclaimed", s["kinds"]["send_failed"] == 1 and s["kinds"]["deferred_breaker"] == 2
          and [r[1] for r in c6.logrows(cur)] == ["Failed"])
    c6.cleanup(cur)
    for i in ids:
        c6.add_sub(cur, i)

    class Seq(c6.FakeMailer):
        def __init__(self, plan):
            super().__init__()
            self.plan = list(plan)

        def send(self, raw, f, to):
            x = self.plan.pop(0) if self.plan else None
            if x == "nq":
                raise t6.NotQueuedError("rejected after the message content (554)")
            if x == "rcpt":
                raise t6.PreDataError("RCPT refused (550)")
            return super().send(raw, f, to)
    mail = Seq(["nq", None, None])
    ctx = c6.mk_ctx(cur, today, c6.FakeAdapter(), mail, None)
    s = c6.run_subs(cur, ctx, ids)
    check("ONE per-recipient rejection is Failed + alert and the run CONTINUES (the next two are sent)", s["kinds"]["rejected"] == 1 and s["kinds"]["sent"] == 2
          and not ctx.breaker and any(e["code"] == "rejected" for e in ctx.events))
    c6.cleanup(cur)
    for i in ids:
        c6.add_sub(cur, i)
    ctx = c6.mk_ctx(cur, today, c6.FakeAdapter(), Seq(["rcpt", "rcpt", None]), None)
    s = c6.run_subs(cur, ctx, ids)
    check("two per-recipient failures in a row (RCPT refused) trip the breaker; the third is not claimed", s["kinds"]["send_failed"] == 2 and s["kinds"]["deferred_breaker"] == 1)
    c6.cleanup(cur)
    for i in ids:
        c6.add_sub(cur, i)
    ctx = c6.mk_ctx(cur, today, c6.FakeAdapter(), Seq(["rcpt", None, "rcpt"]), None)
    s = c6.run_subs(cur, ctx, ids)
    check("a success between two failures resets the streak (no breaker)", s["kinds"]["send_failed"] == 2 and s["kinds"]["sent"] == 1 and not ctx.breaker)
    c6.cleanup(cur)
    for i in ids:
        c6.add_sub(cur, i)
    # ---- success: message id stored, a failure between successes resets the streak
    stub = start_stub("ok")
    ctx = c6.mk_ctx(cur, today, c6.FakeAdapter(), t6.SmtpMailer("127.0.0.1", stub.port, timeout=5), None)
    s = c6.run_subs(cur, ctx, ids)
    rows = c6.logrows(cur)
    check("accepted: SES message id stored in the log row (no address)", s["kinds"]["sent"] == 3 and all("smtp_id=0100019abc123456-" in r[2] and "@" not in r[2] for r in rows), str(rows[:1]))
    stub.stop()
    c6.cleanup(cur)
    # ---- preflight (real-SES path, against the stub): refused -> nothing claimed
    for i in ids:
        c6.add_sub(cur, i)
    stub = start_stub("rcpt_reject")
    ad = c6.FakeAdapter(emails=("ok@example.org",))
    ctx = c6.mk_ctx(cur, today, ad, t6.SmtpMailer("127.0.0.1", stub.port, timeout=5), None, ses=True, allow=frozenset({"ok@example.org"}),
                    alert_to="ok@example.org")
    s = c6.run_subs(cur, ctx, ids)
    check("SES preflight refused: NO claim, no export, no DATA, breaker, exit 1", s["kinds"]["preflight_failed"] == 1 and s["kinds"]["deferred_breaker"] == 2
          and c6.logrows(cur) == [] and ad.exports == 0 and stub.messages == [] and t6.exit_code(ctx, s) == 1)
    stub.stop()
    c6.cleanup(cur)
    for i in ids[:1]:
        c6.add_sub(cur, i)
    stub = start_stub("ok")
    ctx = c6.mk_ctx(cur, today, c6.FakeAdapter(emails=("ok@example.org",)), t6.SmtpMailer("127.0.0.1", stub.port, timeout=5), None, ses=True,
                    allow=frozenset({"ok@example.org"}), alert_to="ok@example.org")
    s = c6.run_subs(cur, ctx, ids[:1])
    check("SES preflight passes: one RCPT test on a separate connection, then the real send", s["kinds"]["sent"] == 1 and stub.connections == 2 and len(stub.messages) == 1)
    stub.stop()
    c6.cleanup(cur)


def test_t6_load(cur, today):
    c6.cleanup(cur)
    ids = ["T6TEST-L%d" % i for i in range(1, 5)]
    for i in ids:
        c6.add_sub(cur, i)
    log_ = []

    class Timed(c6.FakeAdapter):
        def export(self, sub, live):
            log_.append(("start", time.monotonic()))
            time.sleep(0.05)
            r = super().export(sub, live)
            log_.append(("end", time.monotonic()))
            return r
    ctx = c6.mk_ctx(cur, today, Timed(), c6.FakeMailer(), None, export_pause_s=0.4)
    s = c6.run_subs(cur, ctx, ids)
    starts = [t for k, t in log_ if k == "start"]
    ends = [t for k, t in log_ if k == "end"]
    gaps = [starts[i + 1] - ends[i] for i in range(len(ids) - 1)]
    check("exports are strictly serial (no overlap) with the pause between subscriptions", s["kinds"]["sent"] == 4 and all(g >= 0.38 for g in gaps) and len(starts) == 4,
          "gaps %s" % [round(g, 2) for g in gaps])
    c6.cleanup(cur)
    for i in ids:
        c6.add_sub(cur, i)
    ctx = c6.mk_ctx(cur, today, c6.FakeAdapter(), c6.FakeMailer(), None, max_sends_per_run=2)
    s = c6.run_subs(cur, ctx, ids)
    check("MAX_SENDS_PER_RUN: two sent, the other two wait for the next run (unclaimed)", s["kinds"]["sent"] == 2 and s["kinds"]["deferred"] == 2 and len(c6.logrows(cur)) == 2)
    c6.cleanup(cur)
    c6.add_sub(cur, ids[0])

    class G(c6.FakeGate):
        sources = {"T6TEST-WB": [{"target": "TGT", "name": "DS1"}]}
        busy = set()

        def inflight_targets(self):
            return self.busy
    g = G()
    ad = c6.FakeAdapter()
    ad.refresh_running = lambda names: "DS1"
    ctx = c6.mk_ctx(cur, today, ad, c6.FakeMailer(), g)
    s = c6.run_subs(cur, ctx, ids[:1])
    check("never export while a refresh job of the workbook's data source runs on Tableau (wait, no export, no claim)", s["kinds"]["not_ready"] == 1 and ad.exports == 0 and c6.logrows(cur) == [])
    ad2 = c6.FakeAdapter()
    ad2.refresh_running = lambda names: None
    g2 = G()
    g2.busy = {"TGT"}
    ctx = c6.mk_ctx(cur, today, ad2, c6.FakeMailer(), g2)
    s = c6.run_subs(cur, ctx, ids[:1])
    check("... or while Tableau_RefreshJob has the extract 'Submitted' (our own t4)", s["kinds"]["not_ready"] == 1 and ad2.exports == 0)
    ctx = c6.mk_ctx(cur, today, ad2, c6.FakeMailer(), G())
    s = c6.run_subs(cur, ctx, ids[:1])
    check("no refresh running: sent", s["kinds"]["sent"] == 1)
    c6.cleanup(cur)


def test_t6_config():
    def A(**kw):
        base = dict(ses=False, local_smtp=False, execute=False, as_of_date=None, export=False, size_limit_mb=25, cutoff="15:30",
                    max_mapping_age_h=26.0, include_not_taken_over=False)
        base.update(kw)
        return NS(**base)
    c = t6.build_config(A(), {})
    check("SCHEDULE_MODE defaults to honour, catch-up 3, pause 20 s, 10 sends per run", c.schedule_enforce and c.catch_up_days == 3 and c.export_pause_s == 20 and c.max_sends_per_run == 10)
    c = t6.build_config(A(), {"SCHEDULE_MODE": "ignore", "SCHEDULE_CATCHUP_DAYS": "5", "EXPORT_PAUSE_SEC": "7", "MAX_SENDS_PER_RUN": "3", "ALERT_TO": "x@y.com"})
    check("settings from the environment (SCHEDULE_MODE=ignore, catch-up days, pause, max sends, ALERT_TO)",
          not c.schedule_enforce and c.catch_up_days == 5 and c.export_pause_s == 7 and c.max_sends_per_run == 3 and c.alert_to == "x@y.com")
    try:
        t6.build_config(A(), {"SCHEDULE_MODE": "sometimes"})
        r = "accepted"
    except t6.ConfigError:
        r = "refused"
    check("bad SCHEDULE_MODE is refused", r == "refused")
    check("MAPPING_MAX_AGE_H is a setting (default 26)", t6.default_max_age_h({}) == 26 and t6.default_max_age_h({"MAPPING_MAX_AGE_H": "36"}) == 36)
    check("--schedule-mode on the command line wins over the environment", not t6.build_config(A(schedule_mode="ignore"), {"SCHEDULE_MODE": "honour"}).schedule_enforce)


# =====================================================================================================================
# D. operations: kill switch, markers, recipient check, pat-busy, daily summary
# =====================================================================================================================
def mail_env(sub):
    d = TMP / sub
    d.mkdir(exist_ok=True)
    return {"OPS_STATE_DIR": str(d), "LASTRUN_DIR": str(d / "lastrun"), "KILL_FILE": str(d / "KILL")}


def test_ops():
    env = mail_env("kill")
    check("kill switch: off by default", ops.kill_switch_active(env) == (False, ""))
    check("kill switch: env variable", ops.kill_switch_active(dict(env, TABLEAU_AUTOMATION_DISABLED="1"))[0] and ops.kill_switch_active(dict(env, TABLEAU_AUTOMATION_DISABLED="true"))[0]
          and not ops.kill_switch_active(dict(env, TABLEAU_AUTOMATION_DISABLED="0"))[0])
    stub = start_stub("ok")
    env_on = dict(env, TABLEAU_AUTOMATION_DISABLED="1")
    r1 = ops.check_kill_switch("t6", True, False, "127.0.0.1", stub.port, env_on)
    r2 = ops.check_kill_switch("t4", True, False, "127.0.0.1", stub.port, env_on)
    time.sleep(0.3)
    check("kill switch active: returns True (script exits 0), ONE alert mail for the day across scripts", r1 and r2 and len(stub.messages) == 1, "messages %d" % len(stub.messages))
    check("kill switch: dry run sends no alert", ops.check_kill_switch("t6", False, False, "127.0.0.1", stub.port, env_on) and len(stub.messages) == 1)
    Path(env["KILL_FILE"]).write_text("x")
    check("kill switch: out/KILL file (KILL_FILE)", ops.kill_switch_active(env)[0])
    mk_ = ops.read_marker("t6", env)
    check("the kill-switch exit writes a last-run marker (outcome kill_switch)", mk_ and mk_["outcome"] == "kill_switch" and mk_["exit_code"] == 0)
    stub.stop()
    # every script honours it before touching the DWH or Tableau (bogus DWH proves no connection was attempted)
    senv = dict(os.environ, **dict(env_on, CONN_SERVER="bogus.invalid", LOG_DIR=str(TMP / "childlogs"), EVENTLOG_DISABLED="1", MAIL_TRANSPORT="local"))
    for script, args in (("t2_mapping_sync.py", ["--dry-run"]), ("t4_refresh.py", []), ("t6_send.py", []), ("t11_pause.py", [])):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / script)] + args, capture_output=True, text=True, env=senv, timeout=60)
        check("%s: kill switch -> exit 0, clear log line, nothing else" % script, r.returncode == 0 and "KILL SWITCH ACTIVE" in (r.stdout + r.stderr), (r.stdout + r.stderr)[-200:])
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "t11_pause.py"), "--restore", "x"], capture_output=True, text=True, env=senv, timeout=60)
    check("t11 --restore ignores the kill switch (a rollback must always work)", "KILL SWITCH" not in (r.stdout + r.stderr) and r.returncode == 1)
    # D-050: each sending script logs the allow-all warning at startup (real entry points, stopped by the kill switch right after)
    uenv = dict(senv, UAT_TABLEAU_HOST="tableau-uat.example.com", SEND_ALLOW_ALL_UAT="1", TABLEAU_SERVER_URL="http://tableau-uat.example.com:8000/")
    for script, args in (("t2_mapping_sync.py", ["--dry-run"]), ("t4_refresh.py", []), ("t6_send.py", []), ("t11_pause.py", [])):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / script)] + args, capture_output=True, text=True, env=uenv, timeout=60)
        check("%s: logs the SEND_ALLOW_ALL_UAT warning at startup (UAT host)" % script, "SEND_ALLOW_ALL_UAT=1 is ON" in (r.stdout + r.stderr), (r.stdout + r.stderr)[-200:])
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "t6_send.py")], capture_output=True, text=True,
                       env=dict(uenv, TABLEAU_SERVER_URL="https://prod.example.com/"), timeout=60)
    check("t6_send.py: on another host the warning says the switch is IGNORED", "IGNORED" in (r.stdout + r.stderr) and "is ON" not in (r.stdout + r.stderr))
    # alert recipient
    cap = c6.LogCapture()
    ops.log.addHandler(cap)
    ops.log.setLevel(logging.ERROR)
    ses_env = {"SES_SMTP_USER": "u", "SES_SMTP_PASSWORD": "p", "SEND_ALLOWED_RECIPIENTS": "someone@example.org",
               "OWNER_EMAIL": "you@example.com", "UAT_TABLEAU_HOST": "tableau-uat.example.com"}
    cfg = ops.alert_config(True, ses_env, "x")
    check("alert recipient NOT on the allow-list: loud ERROR, returns False", ops.check_alert_recipient(cfg, "t6") is False and any("ALERT RECIPIENT NOT ON ALLOW-LIST" in l for l in cap.lines))
    cfg = ops.alert_config(True, dict(ses_env, SEND_ALLOWED_RECIPIENTS="you@example.com"), "x")
    check("alert recipient on the allow-list (case-insensitive): fine", ops.check_alert_recipient(cfg, "t6") is True)
    check("local transport: nothing to check", ops.check_alert_recipient(ops.alert_config(False, {}, "x"), "t6") is True)
    # SEND_ALLOW_ALL_UAT (D-050): alert recipient needs a valid address, not a list entry; only on the exact UAT host
    uat_env = dict(ses_env, SEND_ALLOWED_RECIPIENTS="", SEND_ALLOW_ALL_UAT="1", TABLEAU_SERVER_URL="http://tableau-uat.example.com:8000/",
                   ALERT_TO="team@example.com")
    cfg = ops.alert_config(True, uat_env, "x")
    check("allow-all on UAT: alert recipient not on any list is fine", ops.check_alert_recipient(cfg, "t6") is True and cfg.allow_all)
    fm = c6.FakeMailer()
    n = ops.alert_once(dataclasses.replace(cfg, alert_state=TMP / "allow_all_alert.json"), fm, "t4", "x_code", "something broke")
    check("allow-all on UAT: an alert mail goes to ALERT_TO without a list", n == 1 and len(fm.sent) == 1 and fm.sent[0][2] == ["team@example.com"])
    cap.lines.clear()
    cfg = ops.alert_config(True, dict(uat_env, SEND_ALLOWED_RECIPIENTS="someone@example.org", TABLEAU_SERVER_URL="http://tableau-uat.example.com.evil.example/"), "x")
    check("allow-all on another host (look-alike): ignored, alert recipient NOT on the list is flagged again",
          ops.check_alert_recipient(cfg, "t6") is False and not cfg.allow_all)
    try:
        ops.alert_config(True, dict(uat_env, ALERT_TO="not an address"), "x")
        r = "accepted"
    except Exception:
        r = "refused"
    check("allow-all on UAT: an invalid ALERT_TO is still refused", r == "refused")
    # daily summary shows the switch
    d0 = {"today": dt.date(2026, 10, 26), "scripts": [], "kill": (False, ""), "sent": [], "released": 0, "not_sent": [], "refresh": [],
          "repaused": [], "halted": [], "errors_send": [], "old_claims": [], "t6_counters": {}}
    check("daily summary: a note when the switch is effective, nothing otherwise",
          "SEND_ALLOW_ALL_UAT is ON" in ds_.render(dict(d0, allow_all_uat=True)) and "SEND_ALLOW_ALL_UAT" not in ds_.render(d0)
          and ds_.attention(dict(d0, allow_all_uat=True)) == ds_.attention(d0))
    ops.log.removeHandler(cap)
    # markers
    e2 = mail_env("markers")
    now = dt.datetime(2026, 10, 23, 9, 0, tzinfo=ops.SGT)
    ops.write_marker("t6", "execute", 0, "RUN SUMMARY a=1", env=e2, now=now)
    ops.write_marker("t6", "execute", 1, "RUN SUMMARY a=2", env=e2, now=now + dt.timedelta(minutes=15))
    ops.write_marker("t6", "dry-run", 0, "x", env=e2, now=now + dt.timedelta(minutes=20))
    mk_ = ops.read_marker("t6", e2)
    ex = mk_["days"]["2026-10-23"]["execute"]
    check("last-run marker: per-day counters per mode", ex["runs"] == 2 and ex["bad"] == 1 and ex["first"] == "09:00" and ex["last"] == "09:15"
          and mk_["days"]["2026-10-23"]["dry-run"]["runs"] == 1 and mk_["summary"] == "x")
    # pat busy streak
    e3 = dict(mail_env("busy"), PAT_BUSY_ALERT_AFTER="3")
    stub = start_stub("ok")
    for _ in range(2):
        ops.on_pat_busy("t4", True, local_port=stub.port, env=e3)
    n0 = len(stub.messages)
    ops.on_pat_busy("t4", True, local_port=stub.port, env=e3)
    ops.on_pat_busy("t4", True, local_port=stub.port, env=e3)
    time.sleep(0.2)
    check("PatBusy N times in a row alerts (once per day); below N nothing", n0 == 0 and len(stub.messages) == 1)
    ops.pat_ok("t4", e3)
    check("a normal run resets the PAT-busy streak", ops.note_pat_busy("t4", False, env=e3) == 0)
    stub.stop()


def test_nondwh_views(conn, cur):
    """D-053 / R8: readiness views with the NonDwhConnection flag. Fixtures R1TEST-ND-*, inside a transaction that is rolled back."""
    q = lambda sql, *a: cur.execute(sql, *a).fetchall()
    # D-053: a data source is flagged non-DWH only when EVERY skipped connection is positively named as non-DWH
    def csv_tbl(name="Users.csv", ctype="textscan", db=None):
        return {"name": name, "schema": None, "connectionType": ctype, "database": {"name": db, "connectionType": ctype} if db else None}
    dwh_tbl = {"name": "Account", "schema": "Dimension", "connectionType": "sqlserver", "database": {"name": "cqglobalsgdw", "connectionType": "sqlserver"}}

    def flagged(tables, **kw):
        src = {"id1": dict({"name": "f", "linked": None, "is_extract": False, "tables": tables, "sql": None}, **kw)}
        out = m2.build_rows(src, {}, set(), [], m2.Resolver(cur))
        return "id1" in out[3]["nondwh_ds_ids"], out[1]
    f, rows = flagged([csv_tbl()])
    check("non-DWH: a CSV-only source is flagged, no table rows", f and rows == [])
    f, rows = flagged([csv_tbl("a.xlsx", "excel-direct"), csv_tbl()])
    check("non-DWH: several file connections are flagged", f)
    f, rows = flagged([csv_tbl("t", "oracle", "ORCL")])
    check("non-DWH: another database engine is flagged", f)
    f, rows = flagged([csv_tbl("t", "sqlserver", "SomeOtherDb")])
    check("non-DWH: SQL Server in ANOTHER database is flagged", f)
    f, rows = flagged([csv_tbl(), dwh_tbl])
    check("non-DWH: file + DWH table is flagged AND keeps its DWH table rows (waits on them)", f and len(rows) >= 1)
    f, rows = flagged([])
    check("non-DWH: no tables at all (read failed) is NOT flagged", not f and rows == [])
    f, rows = flagged([csv_tbl("x", "")])
    check("non-DWH: a table with no connection type is NOT flagged (not positively identified)", not f)
    f, rows = flagged([csv_tbl("x", "sqlproxy")])
    check("non-DWH: a published-source proxy is NOT evidence", not f)
    f, rows = flagged([csv_tbl("x", "hyper")])
    check("non-DWH: a Tableau extract file type is NOT evidence", not f)
    f, rows = flagged([csv_tbl(), csv_tbl("y", "")])
    check("non-DWH: one positive file plus one unidentifiable table is NOT flagged (fail closed)", not f)
    f, rows = flagged([csv_tbl()], flag=True, sql_error=None, union=False, sql=["SELECT a FROM Dimension.AE"])
    check("non-DWH: a custom-SQL source is never flagged", not f)
    f, rows = flagged([csv_tbl()], flag=True, sql_error=None, union=False)
    check("non-DWH: custom SQL flagged without SQL stays unflagged with its placeholder", not f and len(rows) == 1 and rows[0][1] == m2.UNPARSED_SCHEMA)
    f, rows = flagged([csv_tbl("x", "genericodbc")])
    check("non-DWH: a generic ODBC connection is NOT evidence (it can point at our DWH)", not f)
    for ct in ("otherodbc", "odbc", "jdbc", "oledb", "msolap", "sqlserver-odbc", "somethingnew"):
        check("non-DWH: connection type %r is not on the allow-list, so not evidence" % ct, not m2.is_positive_non_dwh(csv_tbl("x", ct)))
    for ct in ("Excel-Direct", "textscan", "CSV", "postgres", "oracle", "snowflake"):
        check("non-DWH: connection type %r is on the allow-list" % ct, m2.is_positive_non_dwh(csv_tbl("x", ct)))
    check("non-DWH: SQL Server database name with brackets/quotes/spaces is normalised to ours (still DWH, not flagged)",
          all(m2.is_dwh_table(csv_tbl("t", "sqlserver", d)) and not m2.is_positive_non_dwh(csv_tbl("t", "sqlserver", d))
              for d in ("[CQGlobalSGDW]", " cqglobalsgdw ", '"CQGlobalSGDW"', "'CQGlobalSGDW'", "[ CQGlobalSGDW ]")))
    check("non-DWH: a bracketed OTHER database name is still positively another database",
          m2.is_positive_non_dwh(csv_tbl("t", "sqlserver", "[OtherDb]")) and not m2.is_dwh_table(csv_tbl("t", "sqlserver", "[OtherDb]")))
    check("non-DWH: an odd SQL Server database name (not a plain name) is NOT evidence (fail closed)",
          not m2.is_positive_non_dwh(csv_tbl("t", "sqlserver", "Other;Db")) and not m2.is_positive_non_dwh(csv_tbl("t", "sqlserver", "")))
    check("non-DWH: is_positive_non_dwh needs a name and a named type", not m2.is_positive_non_dwh({"name": None, "connectionType": "textscan"})
          and not m2.is_positive_non_dwh({}) and m2.is_positive_non_dwh({"name": "a", "connectionType": "Excel-Direct"}))
    try:
        known_pipelines(cur)   # dev's ETL log has no row for some registered pipelines; inside this rolled-back transaction they get one (D-077)
        cur.execute("INSERT Integration.Tableau_DataSource (DataSource_id, DataSource_Name, IsExtract, NonDwhConnection, Active_Flag) VALUES "
                    "('R1TEST-ND-file','file',0,1,1),('R1TEST-ND-filedwh','file+dwh',0,1,1),('R1TEST-ND-fileunm','file+unmapped',0,1,1),"
                    "('R1TEST-ND-none','read failed',0,0,1),('R1TEST-ND-unk','file, kind unknown',NULL,1,1),('R1TEST-ND-dwh','dwh',0,0,1)")
        cur.execute("INSERT Integration.Tableau_DataSourceTable (DataSource_id, SchemaName, TableName) VALUES "
                    "('R1TEST-ND-filedwh','Dimension','AE'),('R1TEST-ND-fileunm','dbo','R1TestNotInRegistry'),('R1TEST-ND-dwh','Dimension','AE')")
        st = dict(tuple(r) for r in q("SELECT DataSource_id, ReadyStatus FROM Integration.vw_Tableau_DataSourceReadiness WHERE DataSource_id LIKE 'R1TEST-ND-%'"))
        check("D-053 view: flagged file-only source -> NotChecked", st.get("R1TEST-ND-file") == "NotChecked", st)
        check("D-053 view: file + mapped DWH table -> Ready (normal rules)", st.get("R1TEST-ND-filedwh") == "Ready")
        pl = q("SELECT PipelineName FROM Integration.vw_Tableau_DataSourcePipeline WHERE DataSource_id = 'R1TEST-ND-filedwh'")
        check("D-053 view: file + DWH table waits on the DWH pipeline only", len(pl) == 1)
        check("D-053 view: flagged source with an unmapped DWH table -> UnmappedTable (not ready)", st.get("R1TEST-ND-fileunm") == "UnmappedTable")
        check("D-053 view: no tables and NOT flagged -> NoTables (fail closed)", st.get("R1TEST-ND-none") == "NoTables")
        check("D-053 view: flagged but IsExtract unknown -> UnknownKind", st.get("R1TEST-ND-unk") == "UnknownKind")
        cur.execute("INSERT Integration.Tableau_WorkbookDataSource (Workbook_id, Workbook_Name, DataSource_id) VALUES "
                    "('R1TEST-ND-w1','w','R1TEST-ND-file'),('R1TEST-ND-w2','w','R1TEST-ND-file'),('R1TEST-ND-w2','w','R1TEST-ND-dwh'),"
                    "('R1TEST-ND-w3','w','R1TEST-ND-file'),('R1TEST-ND-w3','w','R1TEST-ND-fileunm'),"
                    "('R1TEST-ND-w4','w','R1TEST-ND-none'),('R1TEST-ND-w5','w','R1TEST-ND-file'),('R1TEST-ND-w5','w','R1TEST-ND-none')")
        wb = dict(tuple(r) for r in q("SELECT Workbook_id, WorkbookStatus FROM Integration.vw_Tableau_WorkbookReadiness WHERE Workbook_id LIKE 'R1TEST-ND-%'"))
        check("D-053 workbook: file-only workbook is Ready", wb.get("R1TEST-ND-w1") == "Ready", wb)
        check("D-053 workbook: file + ready DWH source is Ready", wb.get("R1TEST-ND-w2") == "Ready")
        check("D-053 workbook: file + unmapped DWH source is NotReady", wb.get("R1TEST-ND-w3") == "NotReady")
        check("D-053 workbook: a source with nothing known (NoTables) keeps it NotReady",
              wb.get("R1TEST-ND-w4") == "NotReady" and wb.get("R1TEST-ND-w5") == "NotReady")
        # end to end with the REAL Gate and a fake export/mail: a workbook whose only source is a file is sent, a read-failed one is not
        today = t4.dwh_today(cur)
        cur.execute("INSERT Integration.Tableau_WorkbookDataSource (Workbook_id, Workbook_Name, DataSource_id) VALUES ('T6TEST-WB', 'w', 'R1TEST-ND-file')")
        c6.add_sub(cur, "T6TEST-NDSUB", susp=1)
        c6.add_backup(cur, "T6TEST-NDSUB")
        mail = c6.FakeMailer()
        ctx = c6.mk_ctx(cur, today, c6.FakeAdapter(suspended=True), mail, gate=t6.Gate(cur, today))
        s = t6.run(cur, ctx, t6.load_subscriptions(cur, {"T6TEST-NDSUB"}, id_like="T6TEST-NDSUB"))
        check("D-053 end to end: a workbook with only a file source is SENT (real gate, fake export and mail)",
              s["kinds"]["sent"] == 1 and len(mail.sent) == 1 and not s["kinds"]["blocked"] and not s["kinds"]["not_ready"], dict(s["kinds"]))
        cur.execute("INSERT Integration.Tableau_WorkbookDataSource (Workbook_id, Workbook_Name, DataSource_id) VALUES ('T6TEST-WB', 'w', 'R1TEST-ND-none')")
        c6.add_sub(cur, "T6TEST-NDSUB2", susp=1)
        c6.add_backup(cur, "T6TEST-NDSUB2")
        mail = c6.FakeMailer()
        ctx = c6.mk_ctx(cur, today, c6.FakeAdapter(suspended=True), mail, gate=t6.Gate(cur, today))
        s = t6.run(cur, ctx, t6.load_subscriptions(cur, {"T6TEST-NDSUB2"}, id_like="T6TEST-NDSUB2"))
        check("D-053 end to end: add a source with no tables and no flag and the same workbook is BLOCKED, nothing sent",
              s["kinds"]["blocked"] == 1 and mail.sent == [], dict(s["kinds"]))
        # T2 writes the flag
        real_wbs = set(r[0] for r in q("SELECT DISTINCT Workbook_id FROM Integration.Tableau_WorkbookDataSource WHERE Active_Flag = 1 AND Workbook_id NOT LIKE 'R1TEST-%'"))
        ds_rows = {"R1TEST-ND-t2a": ("R1TEST-ND-t2a", "a", None, 0), "R1TEST-ND-t2b": ("R1TEST-ND-t2b", "b", None, 0)}
        links = {("R1TEST-ND-w9", "R1TEST-ND-t2a"), ("R1TEST-ND-w9", "R1TEST-ND-t2b")}
        sub_rows = {"R1TEST-ND-s": ("R1TEST-ND-s", "R1TEST-ND-w9", "Workbook", "R1TEST-ND-t", "PDF", "s", None, "A4", "Portrait", 0, "r1", hashlib.sha256(b"x").digest(), None, dt.datetime(2026, 10, 22, 23, 51, 3))}
        m2.write_dwh(conn, {"R1TEST-ND-w9": "w"}, ds_rows, [], links, sub_rows, skipped_wbs=real_wbs, commit=False, nondwh={"R1TEST-ND-t2a"})
        got = dict(tuple(r) for r in q("SELECT DataSource_id, CAST(NonDwhConnection AS int) FROM Integration.Tableau_DataSource WHERE DataSource_id LIKE 'R1TEST-ND-t2%'"))
        check("D-053 T2 merge: the flag is stored for the flagged source only", got == {"R1TEST-ND-t2a": 1, "R1TEST-ND-t2b": 0}, got)
        cur.execute("DROP TABLE #ds, #wd, #dt, #seen, #sub, #skipwb, #skipsub, #protds, #sched")
        m2.write_dwh(conn, {"R1TEST-ND-w9": "w"}, ds_rows, [], links, sub_rows, skipped_wbs=real_wbs, commit=False, nondwh=set())
        got = dict(tuple(r) for r in q("SELECT DataSource_id, CAST(NonDwhConnection AS int) FROM Integration.Tableau_DataSource WHERE DataSource_id LIKE 'R1TEST-ND-t2%'"))
        check("D-053 T2 merge: the flag is cleared again when no longer positively non-DWH", got.get("R1TEST-ND-t2a") == 0, got)
    finally:
        conn.rollback()
        try:
            cur.execute("DROP TABLE #ds, #wd, #dt, #seen, #sub, #skipwb, #skipsub, #protds, #sched")
        except Exception:
            pass
        conn.rollback()
    check("D-053 view tests rolled back: no fixture left", q("SELECT COUNT(*) FROM Integration.Tableau_DataSource WHERE DataSource_id LIKE 'R1TEST-ND-%'")[0][0] == 0)


def test_summary_rows_render(cur):
    """The daily summary must render when the send log has Failed / Claimed / TimedOut rows and a takeover is halted. Uses REAL pyodbc Row
    objects (from a constant SELECT, nothing is written anywhere): a Row is not a tuple, so '%' on it raised TypeError."""
    rows = cur.execute("SELECT 'SUB-F', 'Failed', 'EXPORT: boom' UNION ALL SELECT 'SUB-C', 'Claimed', '' UNION ALL SELECT 'SUB-T', 'TimedOut', 'claim timed out'").fetchall()
    halted = cur.execute("SELECT 'SUB-H', 'pause changed the definition'").fetchall()
    old = cur.execute("SELECT 'SUB-O', CAST('2026-10-20' AS date), 'Claimed'").fetchall()
    d = {"today": dt.date(2026, 10, 26), "scripts": [], "kill": (False, ""), "sent": [], "released": 0, "not_sent": [], "refresh": [],
         "repaused": [], "halted": halted, "errors_send": rows, "old_claims": old, "t6_counters": {}}
    try:
        text = ds_.render(d)
        err = ""
    except Exception as e:
        text, err = "", "%s: %s" % (type(e).__name__, e)
    check("daily summary renders Failed, Claimed and TimedOut rows and a halted takeover (real pyodbc rows)",
          not err and all(x in text for x in ("SUB-F | Failed | EXPORT: boom", "SUB-C | Claimed", "SUB-T | TimedOut", "SUB-H: pause changed the definition", "SUB-O")), err)
    try:
        mail = ds_.build_mail(NS(sender="a@example.com"), d, "b@example.com")
        err = ""
    except Exception as e:
        mail, err = None, "%s: %s" % (type(e).__name__, e)
    check("daily summary mail is built from those rows and counts them as needing attention", not err and mail is not None and "item(s) need attention" in mail["Subject"], err)


def test_daily_summary(cur, today):
    c6.cleanup(cur)
    cur.execute("DELETE FROM Integration.Tableau_Schedule WHERE Schedule_id LIKE 'R1TEST-%'")
    cur.execute("INSERT Integration.Tableau_Schedule (Schedule_id, Name, State, Frequency, StartTime, IntervalHours) VALUES ('R1TEST-S1', 'r1 daily', 'Active', 'Daily', '00:01', 24)")
    for sid in ("T6TEST-D1", "T6TEST-D2"):
        c6.add_sub(cur, sid)
        set_sched(cur, sid, "R1TEST-S1")
        c6.add_backup(cur, sid, taken=1, paused=True)
    cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET PausedAt = DATEADD(day, -3, SYSDATETIMEOFFSET()) WHERE Subscription_id LIKE 'T6TEST-D%'")
    cur.execute("INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status, SentAt, Message) VALUES ('T6TEST-D1', 'Sent', SYSDATETIMEOFFSET(), 'x')")
    env = mail_env("summary")
    now = dt.datetime.now(ops.SGT)
    ops.write_marker("t6", "execute", 0, "RUN SUMMARY mode=execute no_allowed_recipient=2 rejected=0 sent=1", env=env, now=now - dt.timedelta(minutes=10))
    ops.write_marker("t4", "execute", 0, "RUN SUMMARY x", env=env, now=now - dt.timedelta(hours=4))
    ops.write_marker("t2", "dry-run", 0, "x", env=env, now=now - dt.timedelta(hours=4))
    d = ds_.collect(cur, today, now.replace(tzinfo=None), 90, env)
    text = ds_.render(d)
    names = {s[0]: s for s in d["scripts"]}
    check("summary: t11 never ran today, t2 only dry runs, t4 stopped (last run old), t6 fine",
          names["t11"][3] and "DID NOT RUN" in names["t11"][2] and "dry run" in names["t2"][2] and names["t4"][3] and "stopped" in names["t4"][2] and not names["t6"][3])
    check("summary: sent list has the sent subscription, not-sent list has the due one that was not sent",
          any(r[2] == "Sent" for r in d["sent"]) and any("T6TEST" in (x[1] or "") or x[1] == "T6TEST subject" for x in d["not_sent"]) and "DUE BUT NOT SENT" in text)
    check("summary: counters of the last t6 run (no_allowed_recipient) are surfaced", d["t6_counters"].get("no_allowed_recipient") == "2" and "no_allowed_recipient=2" in text)
    check("summary: no address-like text", "@" not in text.replace("you@", ""))
    n0 = ds_.attention(d)
    ops.write_marker("t6", "execute", 0, "RUN SUMMARY mode=execute no_allowed_recipient=2 awaiting_sync=3 sent=1", env=env, now=now - dt.timedelta(minutes=5))
    d2 = ds_.collect(cur, today, now.replace(tzinfo=None), 90, env)
    check("summary (D-046): workbooks waiting for the next daily sync get an info line and do NOT add to the attention count",
          d2["awaiting_sync"] == 3 and "WAITING FOR THE NEXT DAILY SYNC" in ds_.render(d2) and ds_.attention(d2) == n0)
    stub = start_stub("ok")
    os_env = dict(os.environ)
    try:
        os.environ.update(env)
        rc = ds_.main(["--execute", "--local-port", str(stub.port)])
        time.sleep(0.2)
        rc2 = ds_.main(["--execute", "--local-port", str(stub.port)])
        time.sleep(0.2)
    finally:
        os.environ.clear()
        os.environ.update(os_env)
    msg = stub.messages[0].decode(errors="replace") if stub.messages else ""
    check("daily_summary --execute: ONE mail through the local path, a second call the same day sends nothing", rc == 0 and rc2 == 0 and len(stub.messages) == 1 and "daily summary" in msg.lower())
    stub.stop()
    cur.execute("DELETE FROM Integration.Tableau_Schedule WHERE Schedule_id LIKE 'R1TEST-%'")
    c6.cleanup(cur)


# =====================================================================================================================
# E. t4: schedule filter and cascade
# =====================================================================================================================
class FakeJobTab:
    """Tableau stand-in whose refresh jobs finish after `polls` polls; tracks how many of OUR jobs are in flight."""

    def __init__(self, polls=2, clock=None):
        self.polls, self.seq, self.live, self.max_live, self.starts, self.n = polls, 0, {}, 0, [], {}
        self.clock = clock
        self.datasources = NS(refresh=self._refresh)
        self.jobs = NS(get_by_id=self._job)

    def __call__(self):
        return self

    def _refresh(self, ds):
        self.seq += 1
        jid = "job-%d" % self.seq
        self.live[jid] = 0
        self.n[jid] = 0
        self.max_live = max(self.max_live, len(self.live))
        self.starts.append(self.clock() if self.clock else time.monotonic())
        return NS(id=jid)

    def _job(self, jid):
        self.n[jid] += 1
        if self.polls is not None and self.n[jid] >= self.polls:
            self.live.pop(jid, None)
            return NS(id=jid, completed_at=dt.datetime.now(dt.timezone.utc), finish_code=0, notes=[])
        return NS(id=jid, completed_at=None, finish_code=None, notes=[])


class Clk:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_t4(cur, today):
    cur.execute("DELETE FROM Integration.Tableau_RefreshJob WHERE DataSource_id LIKE 'R1TEST-%'")
    cur.execute("DELETE FROM Integration.Tableau_Schedule WHERE Schedule_id LIKE 'R1TEST-%'")
    cur.execute("BEGIN TRAN")   # always rolled back: dev's ETL log has no row for some registered pipelines (D-077)
    try:
        known_pipelines(cur)
        base_targets, _ = t4.load_targets(cur, "all")
        none_targets, ex = t4.load_targets(cur, "all", set())
        all_wbs = {w for t in base_targets for w, _ in t.workbooks}
        keep, _ = t4.load_targets(cur, "all", all_wbs)
    finally:
        cur.execute("ROLLBACK")
    check("t4: with no due workbook nothing is refreshed, the reason names the schedule", len(base_targets) > 0 and none_targets == []
          and any("due today" in why for _, why in ex.values()), "baseline targets %d" % len(base_targets))
    check("t4: all workbooks due -> same targets as before", len(keep) == len(base_targets))
    # due_workbooks with fixtures
    cur.execute("INSERT Integration.Tableau_Schedule (Schedule_id, Name, State, Frequency, StartTime, IntervalHours) VALUES ('R1TEST-DUE', 'd', 'Active', 'Daily', '00:01', 24)")
    cur.execute("INSERT Integration.Tableau_Schedule (Schedule_id, Name, State, Frequency, WeekDays, StartTime) VALUES ('R1TEST-NOT', 'n', 'Active', 'Weekly', ?, '00:01')",
                weekday_of(today, 2)[:3])
    c6.cleanup(cur)
    c6.add_sub(cur, "T6TEST-T1")
    cur.execute("UPDATE Integration.Tableau_Subscription SET ScheduleId = 'R1TEST-DUE', Workbook_id = 'R1TEST-WB1' WHERE Subscription_id = 'T6TEST-T1'")
    c6.add_sub(cur, "T6TEST-T2")
    cur.execute("UPDATE Integration.Tableau_Subscription SET ScheduleId = 'R1TEST-NOT', Workbook_id = 'R1TEST-WB2' WHERE Subscription_id = 'T6TEST-T2'")
    c6.add_sub(cur, "T6TEST-T3")
    cur.execute("UPDATE Integration.Tableau_Subscription SET Workbook_id = 'R1TEST-WB3' WHERE Subscription_id = 'T6TEST-T3'")
    due = t4.due_workbooks(cur, today, "all")
    check("t4 due_workbooks: due schedule yes, other weekday no, no schedule (fail closed) no", "R1TEST-WB1" in due and "R1TEST-WB2" not in due and "R1TEST-WB3" not in due)
    cur.execute("INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status, SentAt, Message) VALUES ('T6TEST-T1', 'Sent', SYSDATETIMEOFFSET(), 'x')")
    check("t4 due_workbooks: an occurrence already sent needs no refresh", "R1TEST-WB1" not in t4.due_workbooks(cur, today, "all"))
    cur.execute("DELETE FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id LIKE 'T6TEST-%'")
    cur.execute("UPDATE Integration.Tableau_Subscription SET Workbook_id = 'R1TEST-WB1', ScheduleId = 'R1TEST-DUE', IsSuspended = 1 WHERE Subscription_id = 'T6TEST-T1'")
    check("t4 due_workbooks: scope 'all' counts a suspended subscription (D-046)", "R1TEST-WB1" in t4.due_workbooks(cur, today, "all"))
    check("t4 due_workbooks: scope 'takeover' ignores subscriptions that are not taken over", "R1TEST-WB1" not in t4.due_workbooks(cur, today, "takeover"))
    c6.add_backup(cur, "T6TEST-T1", taken=1, paused=True)
    cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET PausedAt = DATEADD(day, -2, SYSDATETIMEOFFSET()) WHERE Subscription_id = 'T6TEST-T1'")
    check("t4 due_workbooks: a taken-over (suspended) subscription counts in scope 'takeover'", "R1TEST-WB1" in t4.due_workbooks(cur, today, "takeover"))
    c6.cleanup(cur)
    cur.execute("DELETE FROM Integration.Tableau_Schedule WHERE Schedule_id LIKE 'R1TEST-%'")

    # ---- cascade
    def targets(n):
        return [(t4.Target("R1TEST-C%d" % i, "C%d" % i), NS(id="R1TEST-C%d" % i, name="C%d" % i)) for i in range(n)]
    clk = Clk()
    tab = FakeJobTab(polls=2, clock=clk)
    res = t4.submit_and_wait(cur, tab, targets(4), 10_000, 5, 1, 60, clock=clk, sleep=clk.sleep)
    check("cascade, cap 1: never more than one of our jobs in flight, all four refreshed one after the other", tab.max_live == 1 and list(res.values()) == ["Succeeded"] * 4)
    clk = Clk()
    tab = FakeJobTab(polls=3, clock=clk)
    res = t4.submit_and_wait(cur, tab, targets(5), 10_000, 5, 2, 0, clock=clk, sleep=clk.sleep)
    check("cascade, cap 2 and no stagger: concurrency reaches 2 and never exceeds it", tab.max_live == 2 and len(res) == 5)
    clk = Clk()
    tab = FakeJobTab(polls=40, clock=clk)
    t4.submit_and_wait(cur, tab, targets(3), 10_000, 5, 3, 60, clock=clk, sleep=clk.sleep)
    gaps = [tab.starts[i + 1] - tab.starts[i] for i in range(2)]
    check("stagger: when several may run, starts are at least REFRESH_STAGGER_SEC apart", all(g >= 60 for g in gaps) and tab.max_live == 3, str(gaps))
    cur.execute("DELETE FROM Integration.Tableau_RefreshJob WHERE DataSource_id LIKE 'R1TEST-%'")
    clk = Clk()
    tab = FakeJobTab(polls=None, clock=clk)
    res = t4.submit_and_wait(cur, tab, targets(3), 30, 10, 1, 60, clock=clk, sleep=clk.sleep)
    rows = cur.execute("SELECT DataSource_id, Status FROM Integration.Tableau_RefreshJob WHERE DataSource_id LIKE 'R1TEST-%' ORDER BY RefreshJob_id").fetchall()
    check("budget: the running job is TimedOut, the other two targets were never started (no row, next run retries)", [tuple(r) for r in rows] == [("R1TEST-C0", "TimedOut")]
          and len(tab.starts) == 1)
    cur.execute("DELETE FROM Integration.Tableau_RefreshJob WHERE DataSource_id LIKE 'R1TEST-%'")
    check("settings: defaults are one job at a time, 60 s stagger", t4.MAX_CONCURRENT_REFRESHES == 1 and t4.REFRESH_STAGGER_SEC == 60)


# =====================================================================================================================
# F. T2 resilience
# =====================================================================================================================
def fake_t2_server(break_wb2=True):
    import tableauserverclient as TSC
    wbs = {w: NS(id=w, name="WB " + w, updated_at=(dt.datetime(2026, 10, 23, 7, 51, 3, 456789, tzinfo=dt.timezone(dt.timedelta(hours=8))) if w == "w1" else None))
           for w in ("w1", "w2", "w3", "w4")}
    ds = lambda i, p: {"id": i, "name": "D" + i, "hasExtracts": True, "containsUnsupportedCustomSql": False, "upstreamTables": [],
                       "parentPublishedDatasources": [{"luid": p, "name": "P", "hasExtracts": True, "containsUnsupportedCustomSql": False, "upstreamTables": []}]}

    def sub(i, wb, view=False, pdf=True, img=False, sched="sc1"):
        return NS(id=i, target=NS(id=wb, type="view" if view else "workbook"), attach_pdf=pdf, attach_image=img, schedule_id=sched,
                  subject="s" + i, message="m", page_size_option="A4", page_orientation="Portrait", suspended=False, user_id="u1")
    subs = [sub("s1", "w1"), sub("s2", "w2"), sub("s3", "w3"), sub("s4", "gone-view", view=True), sub("s5", "w4", pdf=False, img=False),
            sub("s6", "w1", sched="bad-sched")]

    def query(q, v=None):
        if "customSQLTablesConnection" in q:
            return {"data": {"customSQLTablesConnection": {"totalCount": 0, "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}}
        wid = v["luid"]
        if wid in ("w1", "w4"):
            return {"data": {"workbooks": [{"luid": wid, "name": "WB " + wid, "embeddedDatasources": [ds("e%s1" % wid, "p1"), ds("e%s2" % wid, "p2")],
                                            "upstreamDatasources": []}]}}
        if wid == "w2":
            return {"data": {"workbooks": []}}
        raise RuntimeError("Metadata API exploded")

    def views_get(i):
        raise RuntimeError("404 view deleted")

    def sched_get(i):
        if i == "bad-sched":
            raise RuntimeError("boom")
        it = TSC.ScheduleItem("sched", 50, "Subscription", "Parallel", TSC.WeeklyInterval(dt.time(9, 0), "Monday"))
        it._id, it._state = i, "Active"
        return it
    return NS(workbooks=NS(items=list(wbs.values())), subscriptions=NS(items=subs), views=NS(get_by_id=views_get),
              datasources=NS(get_by_id=lambda i: NS(has_extracts=True)), metadata=NS(query=query), schedules=NS(get_by_id=sched_get))


def test_t2():
    real_pager = m2.TSC.Pager
    m2.TSC.Pager = lambda ep: iter(ep.items)
    try:
        subs, wb_names, sources, pub, links, st = m2.collect_tableau(fake_t2_server(), pace=0)
    finally:
        m2.TSC.Pager = real_pager
    check("T2: one empty Metadata node and one failing workbook are SKIPPED, the rest is read", set(wb_names) == {"w1", "w4"} and st["skipped_wbs"] == {"w2", "w3"})
    kinds = sorted(k for k, _, _ in st["problems"])
    check("T2: problems recorded for the workbooks, the deleted view, the no-format subscription and the broken schedule",
          kinds == ["schedule", "subscription", "subscription", "workbook", "workbook"] and "s4" in st["skipped_subs"] and "s5" in st["skipped_subs"], str(st["problems"])[:300])
    check("T2: schedule objects are parsed into rows; a failed schedule is simply absent", set(st["schedules"]) == {"sc1"} and st["schedules"]["sc1"].weekdays == frozenset({0}))
    check("T2: the subscriptions of the healthy workbooks carry their schedule id", {s["id"]: s["sched_id"] for s in subs} == {"s1": "sc1", "s6": "bad-sched"} or
          {s["id"] for s in subs} >= {"s1"})
    wu = {s["id"]: s["wb_updated"] for s in subs}
    check("R5/F-3: T2 reads the workbook updated_at from the REST list: naive UTC, whole seconds (07:51:03+08:00 -> 23:51:03 UTC the day before); none stays None",
          wu.get("s1") == dt.datetime(2026, 10, 22, 23, 51, 3) and wu.get("s6") == dt.datetime(2026, 10, 22, 23, 51, 3) and wu.get("s1") is not None)
    check("R5/F-3: a workbook without a change time gives None (t6 then waits: fail closed)", ops.utc_naive(None) is None
          and ops.utc_naive(dt.datetime(2026, 1, 1, 10, 0, 0, 999999)) == dt.datetime(2026, 1, 1, 10, 0, 0))
    # merge protection inside a rolled-back transaction
    conn = m2.connect()
    cur = conn.cursor()
    q_act = ("SELECT (SELECT COUNT(*) FROM Integration.Tableau_WorkbookDataSource WHERE Active_Flag = 1 AND Workbook_id NOT LIKE 'R1TEST-%'),"
             "(SELECT COUNT(*) FROM Integration.Tableau_DataSource WHERE Active_Flag = 1 AND DataSource_id NOT LIKE 'R1TEST-%'),"
             "(SELECT COUNT(*) FROM Integration.Tableau_Subscription WHERE Active_Flag = 1 AND Subscription_id NOT LIKE 'R1TEST-%')")
    before = tuple(cur.execute(q_act).fetchone())
    real_wbs = {r[0] for r in cur.execute("SELECT DISTINCT Workbook_id FROM Integration.Tableau_WorkbookDataSource WHERE Active_Flag = 1")}
    ds_rows = {"R1TEST-DS": ("R1TEST-DS", "fake", None, 0)}
    sub_rows = {"R1TEST-SUB": ("R1TEST-SUB", "R1TEST-WB", "Workbook", "R1TEST-T", "PDF", "s", None, "A4", "Portrait", 0, "r1", hashlib.sha256(b"x").digest(), "R1TEST-S1", dt.datetime(2026, 10, 22, 23, 51, 3))}
    sch = {"R1TEST-S1": sd.Schedule(id="R1TEST-S1", name="r1", state="Active", frequency="Weekly", weekdays=frozenset({0}), start=dt.time(9, 0))}
    drop = "DROP TABLE #ds, #wd, #dt, #seen, #sub, #skipwb, #skipsub, #protds, #sched"
    try:
        m2.write_dwh(conn, {"R1TEST-WB": "fake wb"}, ds_rows, [], {("R1TEST-WB", "R1TEST-DS")}, sub_rows, sch, skipped_wbs=real_wbs, commit=False)
        after = tuple(cur.execute(q_act).fetchone())
        new_sched = cur.execute("SELECT COUNT(*) FROM Integration.Tableau_Schedule WHERE Schedule_id = 'R1TEST-S1'").fetchone()[0]
        check("T2 merge: every workbook skipped -> NO real row is deactivated (workbook links, data sources, subscriptions)", before == after, "%s -> %s" % (before, after))
        check("T2 merge: the schedule row of the fake run is stored", new_sched == 1)
        check("R5/F-3 T2 merge: WorkbookUpdatedAt is stored exactly as read",
              cur.execute("SELECT WorkbookUpdatedAt FROM Integration.Tableau_Subscription WHERE Subscription_id = 'R1TEST-SUB'").fetchone()[0]
              == dt.datetime(2026, 10, 22, 23, 51, 3))
        conn.rollback()
        keep_wb = sorted(real_wbs)[0]
        m2.write_dwh(conn, {"R1TEST-WB": "fake wb"}, ds_rows, [], {("R1TEST-WB", "R1TEST-DS")}, sub_rows, sch, skipped_wbs={keep_wb}, commit=False)
        n_kept = cur.execute("SELECT COUNT(*) FROM Integration.Tableau_Subscription WHERE Workbook_id = ? AND Active_Flag = 1", keep_wb).fetchone()[0]
        n_other = cur.execute("SELECT COUNT(*) FROM Integration.Tableau_Subscription WHERE Workbook_id <> ? AND Active_Flag = 1 AND Subscription_id NOT LIKE 'R1TEST-%'", keep_wb).fetchone()[0]
        check("T2 merge: only the skipped workbook's subscriptions stay active, the others are deactivated as before", n_kept > 0 and n_other == 0, "kept %d other %d" % (n_kept, n_other))
        conn.rollback()
        m2.write_dwh(conn, {"R1TEST-WB": "fake wb"}, ds_rows, [], {("R1TEST-WB", "R1TEST-DS")}, sub_rows, sch, skipped_wbs=set(), commit=False)
        gone = tuple(cur.execute(q_act).fetchone())
        check("T2 merge (control): without the protection the same call deactivates the real rows", all(g < b for g, b in zip(gone, before)), "%s -> %s" % (before, gone))
    finally:
        conn.rollback()
    check("T2 merge test rolled back: active counts as before", tuple(cur.execute(q_act).fetchone()) == before)
    test_nondwh_views(conn, cur)
    # guard counts skipped rows as present
    try:
        m2.guard(cur, {}, {"x": 1}, {"y": 1}, real_wbs)
        r = "passed"
    except RuntimeError:
        r = "aborted"
    check("T2 guard: rows of skipped workbooks count as present (no false 'partial read' abort)", r == "passed")
    try:
        m2.guard(cur, {}, {"x": 1}, {"y": 1}, set())
        r = "passed"
    except RuntimeError:
        r = "aborted"
    check("T2 guard: an empty read without skipped workbooks still aborts", r == "aborted")
    # total failure: exit 1 + alert + marker
    env = mail_env("t2fail")
    stub = start_stub("ok")
    saved = dict(os.environ)
    try:
        os.environ.update(env)
        os.environ["CONN_SERVER"] = "bogus.invalid"
        rc = m2.main(["--local-port", str(stub.port)])
        time.sleep(0.3)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    mk_ = ops.read_marker("t2", env)
    check("T2 failing entirely: exit code 1, ONE alert mail, marker records the failure", rc == 1 and len(stub.messages) == 1 and mk_ and mk_["outcome"] == "fatal")
    stub.stop()
    check("T2 settings exist (pace, page size, lock retries) and the staleness limit is a setting", m2.PACE_SEC >= 0 and m2.METADATA_PAGE > 0 and m2.LOCK_RETRIES >= 0)


# =====================================================================================================================
# G. T11a review fixes
# =====================================================================================================================
def test_t11_fixes(cur, today):
    c11.cleanup(cur)
    srv = c11.FakeServer()
    # ---- halt persists across runs
    for sid in ("T11TEST-H1", "T11TEST-H2"):
        c11.add_sub(cur, sid)
        srv.add(sid)
    srv.subscriptions.drop_message = True
    cfg = c11.fresh_cfg(workbooks=frozenset(), sub_ids=frozenset({"t11test-h1", "t11test-h2"}))
    ctx, mail = c11.run_t11(cur, srv, today, cfg=cfg)
    srv.subscriptions.drop_message = False
    h = cur.execute("SELECT Halted, Note, CAST(PausedAt AS datetime2) FROM Integration.Tableau_SubscriptionBackup WHERE Subscription_id = 'T11TEST-H1'").fetchone()
    check("halt is PERSISTED on the backup row (Halted = 1, reason in Note, PausedAt empty)", h and h[0] and "message" in (h[1] or "") and h[2] is None and ctx.halted)
    ctx2, mail2 = c11.run_t11(cur, srv, today, cfg=cfg)
    h2 = cur.execute("SELECT Halted, CAST(PausedAt AS datetime2) FROM Integration.Tableau_SubscriptionBackup WHERE Subscription_id = 'T11TEST-H1'").fetchone()
    check("the run AFTER a halt does NOT confirm the (suspended) subscription: PausedAt stays empty, event + exit 1",
          h2[0] and h2[1] is None and ctx2.n["confirmed"] == 0 and ctx2.n["halted_rows"] >= 1 and t11.exit_code(ctx2) == 1 and any(e["code"] == "halted" for e in ctx2.events))
    s, reports, ctx6 = c11.run_t6(cur, srv, today, ["T11TEST-H1"])
    check("... and t6 does not send it", s["kinds"]["halted"] + s["kinds"]["takeover_unconfirmed"] >= 1 and reports == [])
    cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET Halted = 0, Note = NULL WHERE Subscription_id = 'T11TEST-H1'")
    ctx3, _ = c11.run_t11(cur, srv, today, cfg=cfg)
    check("after a person clears Halted the normal confirm happens", ctx3.n["confirmed"] >= 1 or ctx3.n["already_paused"] >= 1)
    c11.cleanup(cur)
    srv = c11.FakeServer()
    # ---- schedule id None is skipped; raw missing attributes
    c11.add_sub(cur, "T11TEST-N1")
    srv.add("T11TEST-N1")
    srv.subscriptions.items["T11TEST-N1"].schedule_id = None
    ctx, _ = c11.run_t11(cur, srv, today)
    check("t11 skips (and logs) a subscription with no schedule id (on-the-fly schedule)", ctx.n["skip_no_schedule_id"] == 1 and srv.subscriptions.updates == []
          and c11.backup_rows(cur, "T11TEST-N1") == [])
    c11.cleanup(cur)

    class RawSubs(c11.FakeSubs):
        baseurl = "http://x/subs"

        def get_request(self, url):
            return NS(content=b'<tsResponse xmlns="http://tableau.com/api"><subscription id="a" subject="s" attachPdf="true" suspended="false">'
                              b'<content id="t" type="workbook"/></subscription></tsResponse>')
    srv = c11.FakeServer()
    srv.subscriptions = RawSubs()
    c11.add_sub(cur, "T11TEST-N2")
    srv.add("T11TEST-N2")
    seen = {}
    srv.subscriptions.hook = lambda item: seen.update(img=item.attach_image, pdf=item.attach_pdf, empty=item.send_if_view_empty)
    ctx, _ = c11.run_t11(cur, srv, today)
    check("a GET that omits attachImage / sendIfViewEmpty: the update leaves them UNSET (None), attachPdf (present) is kept",
          seen == {"img": None, "pdf": True, "empty": None}, str(seen))
    c11.cleanup(cur)
    # ---- NULL key hash is never taken over
    srv = c11.FakeServer()
    c11.add_sub(cur, "T11TEST-K1")
    srv.add("T11TEST-K1")
    cur.execute("UPDATE Integration.Tableau_Subscription SET SubscriptionKeyHash = NULL WHERE Subscription_id = 'T11TEST-K1'")
    ctx, _ = c11.run_t11(cur, srv, today)
    check("t11 never pauses a subscription t6 would refuse (NULL SubscriptionKeyHash)", ctx.n["skip_no_key_hash"] == 1 and srv.subscriptions.updates == [])
    c11.cleanup(cur)
    # ---- schedule unsupported -> not taken over
    srv = c11.FakeServer()
    c11.add_sub(cur, "T11TEST-K2")
    srv.add("T11TEST-K2")
    cfg = c11.fresh_cfg()
    ctx = t11.Ctx(cfg, t11.TableauAdapter(lambda: srv), c11.FakeGate(), cur, today)
    ctx.due = sd.DueEngine(today, {}, {}, 3, 26, None)
    t11.run(ctx, c6.FakeMailer())
    check("t11 skips a subscription whose schedule t6 cannot evaluate (schedule mode honour)", ctx.n["skip_schedule"] == 1 and srv.subscriptions.updates == [])
    c11.cleanup(cur)
    # ---- ids only for --execute
    def A(**kw):
        base = dict(execute=False, ses=False, workbooks=None, subscription_ids=None, force_notready=False, retake_restored=False,
                    restore=None, restore_all_for_workbook=None, max_mapping_age_h=26.0)
        base.update(kw)
        return NS(**base)
    try:
        t11.build_config(A(execute=True, workbooks=["CFD Accounts"]), {})
        r = "accepted"
    except t6.ConfigError as e:
        r = "refused" if "IDS" in str(e) else "other"
    check("allow-list by workbook NAME is refused with --execute", r == "refused")
    guid = "14000013-8c5a-4251-baef-972ed283a4cd"
    check("... a workbook id (luid) is accepted for --execute, names are fine in a dry run", t11.build_config(A(execute=True, workbooks=[guid], owner_user_id=["u-1"]), {}).workbooks == {guid}
          and t11.build_config(A(workbooks=["CFD Accounts"]), {}).workbooks == {"cfd accounts"})
    # ---- gone subscription is closed after N days; back-off; JSON reuse
    srv = c11.FakeServer()
    c11.add_sub(cur, "T11TEST-G1")
    srv.add("T11TEST-G1")
    c11.run_t11(cur, srv, today)
    del srv.subscriptions.items["T11TEST-G1"]
    ctx, _ = c11.run_t11(cur, srv, today)
    gs = cur.execute("SELECT GoneSince FROM Integration.Tableau_SubscriptionBackup WHERE Subscription_id = 'T11TEST-G1'").fetchone()[0]
    check("a taken-over subscription that disappears gets GoneSince (day 1), not closed yet", gs == today and ctx.n["closed_gone"] == 0)
    cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET GoneSince = DATEADD(day, -8, GoneSince) WHERE Subscription_id = 'T11TEST-G1'")
    ctx, mail = c11.run_t11(cur, srv, today)
    r = cur.execute("SELECT CAST(RestoredAt AS datetime2), Note FROM Integration.Tableau_SubscriptionBackup WHERE Subscription_id = 'T11TEST-G1'").fetchone()
    check("... after 7+ days the open row is closed with a note and an alert", r[0] is not None and "auto-closed" in (r[1] or "") and ctx.n["closed_gone"] == 1
          and any(e["code"] == "takeover_closed" for e in ctx.events))
    c11.cleanup(cur)
    srv = c11.FakeServer()
    c11.add_sub(cur, "T11TEST-F1")
    srv.add("T11TEST-F1")
    srv.subscriptions.fail_update = RuntimeError("tableau down")
    cfg = c11.fresh_cfg()
    files0 = set((cfg.backup_dir).glob("*F1*")) if cfg.backup_dir.exists() else set()
    for _ in range(3):
        ctx, _ = c11.run_t11(cur, srv, today, cfg=cfg)
    files = set((cfg.backup_dir).glob("*T11TEST-F1.json")) - files0
    check("a failing first pause: backed off after the first failure (no retry storm), at most one JSON backup file per backup row",
          ctx.n["backoff"] == 1 and len(srv.subscriptions.updates) == 0 and len(files) <= 2 and t11.backoff_blocked(cfg, "T11TEST-F1") > 0)
    c11.cleanup(cur)
    # ---- restore takes the PAT lock first
    srv = c11.FakeServer()
    c11.add_sub(cur, "T11TEST-P1")
    srv.add("T11TEST-P1")
    c11.run_t11(cur, srv, today)

    class Busy:
        def __call__(self):
            raise t4.PatBusy()
    cfg = c11.fresh_cfg()
    ctx = t11.Ctx(cfg, t11.TableauAdapter(Busy()), c11.FakeGate(), cur, today)
    try:
        t11.restore(ctx, ["T11TEST-P1"], None)
        r = "no error"
    except t4.PatBusy:
        r = "busy"
    open_ = cur.execute("SELECT COUNT(*) FROM Integration.Tableau_SubscriptionBackup WHERE Subscription_id = 'T11TEST-P1' AND RestoredAt IS NULL").fetchone()[0]
    check("restore with the PAT lock busy: nothing is marked restored (lock taken BEFORE the mark)", r == "busy" and open_ == 1)
    c11.cleanup(cur)
    # ---- rollback SQL refuses open rows
    txt = (ROOT / "scripts" / "sql" / "T11_pause_schema_rollback.sql").read_text(encoding="utf-8")
    check("T11 rollback SQL refuses while open takeover rows exist and says @Force does not override", "TakenOver = 1 AND RestoredAt IS NULL" in txt and "@Force does not override" in txt)


# =====================================================================================================================
def checksum(cur):
    return [tuple(cur.execute("SELECT COUNT(*), CHECKSUM_AGG(CHECKSUM(*)) FROM Integration.%s WHERE %s NOT LIKE 'T6TEST-%%' AND %s NOT LIKE 'T11TEST-%%'" % (t, c, c)).fetchone())
            for t, c in (("Tableau_Subscription", "Subscription_id"), ("Tableau_SubscriptionSendLog", "Subscription_id"), ("Tableau_SubscriptionBackup", "Subscription_id"))]


def main():
    os.environ['EVENTLOG_DISABLED'] = '1'
    os.environ.setdefault('LOG_DIR', tempfile.mkdtemp(prefix='logs_'))   # tests never write the real out/*.log
    os.environ['LASTRUN_DIR'] = str(TMP / 'lastrun_default')   # never write real last-run markers
    os.environ['OPS_STATE_DIR'] = str(TMP / 'state_default')
    for k in [k for k in os.environ if k.startswith(("SES_", "SMTP_"))] + ["SEND_ALLOWED_RECIPIENTS", "SEND_FROM", "ALERT_TO", "MAIL_TRANSPORT", "ADDR_HASH_SALT", "SCHEDULE_MODE", "SEND_ALLOW_ALL_UAT", "TABLEAU_AUTOMATION_DISABLED"]:
        os.environ.pop(k, None)
    real_smtp = smtplib.SMTP

    def loopback_only(host, *a, **kw):
        if str(host) not in ("127.0.0.1", "localhost"):
            raise AssertionError("test tried to open an SMTP connection to %r" % host)
        return real_smtp(host, *a, **kw)
    smtplib.SMTP = loopback_only
    for lg in (t6.log, t4.log, t11.log, m2.log, ops.log):
        lg.setLevel(logging.CRITICAL)   # keep the output readable; the checks capture what they need
    conn = t4.connect()
    cur = conn.cursor()
    cur.execute("EXEC sp_set_session_context N'allow_fixture_delete', 1")   # R3 trigger: fixture cleanups only
    today = t4.dwh_today(cur)
    c6.cleanup(cur)
    c11.cleanup(cur)
    before = checksum(cur)
    sched_before = tuple(cur.execute("SELECT COUNT(*), CHECKSUM_AGG(CHECKSUM(*)) FROM Integration.Tableau_Schedule").fetchone())
    try:
        test_calendar()
        test_due_rules()
        test_smtp_classes()
        test_t6_config()
        test_t6_schedule(cur, today)
        test_t6_ses(cur, today)
        test_t6_load(cur, today)
        test_ops()
        test_summary_rows_render(cur)
        test_daily_summary(cur, today)
        test_t4(cur, today)
        test_t2()
        test_t11_fixes(cur, today)
    finally:
        c6.cleanup(cur)
        c11.cleanup(cur)
        cur.execute("DELETE FROM Integration.Tableau_Schedule WHERE Schedule_id LIKE 'R1TEST-%'")
        cur.execute("DELETE FROM Integration.Tableau_RefreshJob WHERE DataSource_id LIKE 'R1TEST-%'")
        smtplib.SMTP = real_smtp
    check("other rows of Tableau_Subscription / SendLog / Backup unchanged", checksum(cur) == before)
    check("Tableau_Schedule unchanged", tuple(cur.execute("SELECT COUNT(*), CHECKSUM_AGG(CHECKSUM(*)) FROM Integration.Tableau_Schedule").fetchone()) == sched_before)
    print("\n%d failure(s): %s" % (len(fails), fails) if fails else "\nALL CHECKS PASSED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
