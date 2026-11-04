"""R1 review-round checks (D-042, D-043): mail transport setting and provider-neutral SMTP (SES / Gmail / generic), Windows Event Log
signal, retry of a lost alert, PAT-lock holder and t4 lock cap, background-job cache, refresh-probe fail-closed, accounting of skipped
subscriptions, kill switch inside a run, lagging-Metadata drop guard, daily summary details, SQL guards, salted hash, atomic writes.

Same rules as r1_checks.py: no real mail (loopback stub only; ANY other SMTP host fails the test, smtp.gmail.com included), no Tableau,
dev DWH fixtures only (T6TEST-/T11TEST-/R1TEST- rows, removed at the end).
Usage: python scripts/r2_checks.py
"""
import datetime as dt
import inspect
import logging
import os
import smtplib
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_summary as ds_  # noqa: E402
import ops  # noqa: E402
import r1_checks as r1  # noqa: E402
import schedule_due as sd  # noqa: E402
import t2_mapping_sync as m2  # noqa: E402
import t4_refresh as t4  # noqa: E402
import t6_checks as c6  # noqa: E402
import t6_send as t6  # noqa: E402
import t11_checks as c11  # noqa: E402
import t11_pause as t11  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="r2checks_"))
fails = []
D = dt.date


def check(name, ok, detail=""):
    print("%s  %s %s" % ("PASS" if ok else "FAIL", name, detail))
    if not ok:
        fails.append(name)


def A(**kw):
    base = dict(ses=False, local_smtp=False, execute=False, as_of_date=None, export=False, size_limit_mb=None, cutoff="15:30",
                max_mapping_age_h=26.0, include_not_taken_over=False)
    base.update(kw)
    return NS(**base)


# =====================================================================================================================
def test_transport():
    check("MAIL_TRANSPORT: default local, smtp, 'ses' is an alias, junk refused",
          ops.mail_transport({}) == "local" and ops.mail_transport({"MAIL_TRANSPORT": "smtp"}) == "smtp" and ops.mail_transport({"MAIL_TRANSPORT": "SES"}) == "smtp")
    try:
        ops.mail_transport({"MAIL_TRANSPORT": "carrier pigeon"})
        r = "accepted"
    except ValueError:
        r = "refused"
    check("MAIL_TRANSPORT junk value is refused", r == "refused")
    check("effective: smtp only with --execute (dry runs never send), the flag still forces it",
          ops.effective_ses(False, True, {"MAIL_TRANSPORT": "smtp"}) and not ops.effective_ses(False, False, {"MAIL_TRANSPORT": "smtp"})
          and ops.effective_ses(True, True, {}) and not ops.effective_ses(False, True, {}))
    env = {"MAIL_TRANSPORT": "smtp", "SMTP_USER": "me@example.com", "SMTP_PASSWORD": "pw", "SEND_ALLOWED_RECIPIENTS": "me@example.com", "SMTP_HOST": "smtp.gmail.com"}
    c = t6.build_config(A(execute=True), env)
    check("t6: MAIL_TRANSPORT=smtp makes a --execute run real without any flag; the Gmail provider is inferred, size limit 20 MB",
          c.ses and c.size_limit == 20 * 1048576 and ops.smtp_provider(env) == "gmail")
    check("t6: the same env in a dry run stays local", not t6.build_config(A(execute=False), env).ses)
    check("t6: --local-smtp wins over the environment", not t6.build_config(A(execute=True, local_smtp=True), env).ses)
    check("t6: explicit --size-limit-mb wins over the provider default", t6.build_config(A(execute=True, size_limit_mb=7), env).size_limit == 7 * 1048576)
    ses_env = {"MAIL_TRANSPORT": "ses", "SES_SMTP_USER": "u", "SES_SMTP_PASSWORD": "p", "SEND_ALLOWED_RECIPIENTS": "a@b.com"}
    c = t6.build_config(A(execute=True), ses_env)
    st = ops.smtp_settings(ses_env)
    check("old SES_SMTP_* names still work as aliases (provider ses, default SES host, 25 MB)", c.ses and st["provider"] == "ses" and "amazonaws" in st["host"]
          and st["user"] == "u" and c.size_limit == 25 * 1048576)
    check("SMTP_* names win over the SES_SMTP_* aliases", ops.smtp_settings(dict(ses_env, SMTP_USER="new"))["user"] == "new")
    for bad in (dict(env, SEND_ALLOWED_RECIPIENTS=""), dict(env, SMTP_PASSWORD=""), dict(env, SMTP_USER="")):
        try:
            t6.build_config(A(execute=True), bad)
            r = "accepted"
        except t6.ConfigError:
            r = "refused"
        check("real SMTP is refused without the allow-list or credentials", r == "refused")
    try:
        t6.build_config(A(execute=True), dict(env, SMTP_PROVIDER="pigeon"))
        r = "accepted"
    except t6.ConfigError:
        r = "refused"
    check("unknown SMTP_PROVIDER is refused", r == "refused")
    check("--smtp is an alias of --ses on every script (same dest)", all("--smtp" in inspect.getsource(m) for m in (t6, t4, t11, m2, ds_)))
    mailer = t6.make_mailer(c6.cfg(ses=True), env=env)
    check("make_mailer(Gmail): smtp.gmail.com:587, STARTTLS, provider gmail - constructed WITHOUT connecting",
          (mailer.host, mailer.port, mailer.starttls, mailer.provider) == ("smtp.gmail.com", 587, True, "gmail"))
    try:
        smtplib.SMTP("smtp.gmail.com", 587, timeout=1)
        r = "connected"
    except AssertionError:
        r = "blocked"
    check("the test run cannot connect to smtp.gmail.com (non-loopback SMTP fails the test)", r == "blocked")
    cap = c6.LogCapture()
    ops.log.addHandler(cap)
    ops.log.setLevel(logging.INFO)
    ops.log_transport("t6", True, True)
    ops.log.removeHandler(cap)
    check("the transport line is logged and carries no secret", any("MAIL TRANSPORT smtp" in l for l in cap.lines) and not any("pw" == w for l in cap.lines for w in l.split()))
    w = ops.sender_warning("gmail", "tableau-reports@example.com", "me@example.com")
    check("Gmail From rewrite warning when SEND_FROM is not the authenticated account; none when equal or other provider",
          w and "rewrites" in w and ops.sender_warning("gmail", "me@example.com", "me@example.com") is None and ops.sender_warning("ses", "x@y.com", "u") is None)
    check("rejection list is per provider: SES pattern frees a slot, Gmail / generic never",
          t6.is_known_rejection(554, "Message rejected: Email address is not verified.", "ses")
          and not t6.is_known_rejection(554, "Message rejected: Email address is not verified.", "gmail")
          and not t6.is_known_rejection(554, "Message rejected: Email address is not verified.", "generic"))
    # stub: the same SES text from a gmail-provider mailer stays ambiguous
    stub = r1.start_stub("unverified")
    try:
        t6.SmtpMailer("127.0.0.1", stub.port, timeout=5, provider="gmail").send(b"Subject: x\r\n\r\nb\r\n", "a@localhost", ["b@localhost"])
        k = "ok"
    except t6.NotQueuedError:
        k = "notqueued"
    except t6.AmbiguousSendError:
        k = "ambiguous"
    stub.stop()
    check("provider gmail: even a 554 after the content stays AMBIGUOUS (Claimed)", k == "ambiguous")
    # 535 authentication
    stub = r1.start_stub("ok", auth="fail")
    m = t6.SmtpMailer("127.0.0.1", stub.port, "me@example.com", "bad", starttls=False, timeout=5, provider="gmail")
    try:
        m.preflight("me@example.com", "me@example.com")
        k = "ok"
    except t6.PreDataError as e:
        k = "predata" if "535" in str(e) or "Authentication" in str(e) else "predata?"
    try:
        m.send(b"Subject: x\r\n\r\nb\r\n", "me@example.com", ["me@example.com"])
        k2 = "ok"
    except t6.PreDataError:
        k2 = "predata"
    check("Gmail 535 authentication error: preflight and send both raise PreDataError, nothing is sent", k == "predata" and k2 == "predata" and stub.messages == [])
    stub.stop()
    c6.cleanup(cur_)
    c6.add_sub(cur_, "T6TEST-G1")
    stub = r1.start_stub("ok", auth="fail")
    mm = t6.SmtpMailer("127.0.0.1", stub.port, "me@example.com", "bad", starttls=False, timeout=5, provider="gmail")
    ad = c6.FakeAdapter(emails=("me@example.com",))
    ctx = c6.mk_ctx(cur_, today_, ad, mm, None, ses=True, allow=frozenset({"me@example.com"}), alert_to="me@example.com")
    s = c6.run_subs(cur_, ctx, ["T6TEST-G1"])
    check("Gmail 535 trips the PREFLIGHT: nothing claimed, nothing exported, breaker, exit 1", s["kinds"]["preflight_failed"] == 1 and c6.logrows(cur_) == [] and ad.exports == 0
          and t6.exit_code(ctx, s) == 1)
    stub.stop()
    stub = r1.start_stub("ok", auth="ok")
    mm = t6.SmtpMailer("127.0.0.1", stub.port, "me@example.com", "good", starttls=False, timeout=5, provider="gmail")
    ctx = c6.mk_ctx(cur_, today_, c6.FakeAdapter(emails=("me@example.com",)), mm, None, ses=True, allow=frozenset({"me@example.com"}), alert_to="me@example.com")
    s = c6.run_subs(cur_, ctx, ["T6TEST-G1"])
    check("Gmail-style login accepted (stub): preflight passes and the report is sent to the allow-listed address", s["kinds"]["sent"] == 1 and len(stub.messages) == 1)
    stub.stop()
    c6.cleanup(cur_)


# =====================================================================================================================
def test_eventlog_alert_retry():
    calls = []
    real_run = subprocess.run

    def fake_run(args, **kw):
        calls.append(args)
        return NS(returncode=0)
    subprocess.run = fake_run
    try:
        env = {"EVENTLOG_DISABLED": ""}
        saved_off = os.environ.get("EVENTLOG_DISABLED")
        os.environ["EVENTLOG_DISABLED"] = ""    # the fake runner below stands in for eventcreate: allow it for this block only
        ok = ops.event_log("t6", "run ended; person@example.com was affected", "ERROR", env)
        text = calls[0][calls[0].index("/D") + 1] if calls else ""
        check("event log: eventcreate with source TableauAutomation, address removed from the text",
              ok and "/SO" in calls[0] and "TableauAutomation" in calls[0] and "@" not in text and "<addr>" in text, str(text)[:100])
        n = len(calls)
        check("event log: EVENTLOG_DISABLED=1 writes nothing", not ops.event_log("t6", "x", env={"EVENTLOG_DISABLED": "1"}) and len(calls) == n)
        ops.write_marker("t9", "execute", 1, "RUN SUMMARY x", env=dict(os.environ, EVENTLOG_DISABLED="", LASTRUN_DIR=str(TMP / "mk1")))
        check("a failed execute run writes an event log entry", len(calls) == n + 1)
        ops.write_marker("t9", "execute", 1, "x", outcome="pat_busy", env=dict(os.environ, EVENTLOG_DISABLED="", LASTRUN_DIR=str(TMP / "mk1")))
        ops.write_marker("t9", "execute", 0, "x", env=dict(os.environ, EVENTLOG_DISABLED="", LASTRUN_DIR=str(TMP / "mk1")))
        ops.write_marker("t9", "dry-run", 1, "x", env=dict(os.environ, EVENTLOG_DISABLED="", LASTRUN_DIR=str(TMP / "mk1")))
        check("... but not for a PAT-busy skip, a clean run or a dry run", len(calls) == n + 1)

        def boom(*a, **k):
            raise OSError("no eventcreate")
        subprocess.run = boom
        check("event log never raises (eventcreate missing)", ops.event_log("t6", "x", env={"EVENTLOG_DISABLED": ""}) is False)
        os.environ["EVENTLOG_DISABLED"] = "1"
        n0 = len(calls)
        ops.event_log("t6", "x", env={"EVENTLOG_DISABLED": ""})
        check("event log: the PROCESS environment EVENTLOG_DISABLED also switches it off (so tests never write the real log)", len(calls) == n0)
    finally:
        subprocess.run = real_run
        os.environ["EVENTLOG_DISABLED"] = saved_off if saved_off is not None else "1"
    check("markers are written atomically (no leftover .tmp file)", not list((TMP / "mk1").glob("*.tmp")) and (TMP / "mk1" / "t9.json").exists())
    # lost alert: ambiguous is retried once, then not again
    ev = [{"sub": "S1", "wb": "WB", "code": "x", "text": "t"}]
    c = c6.cfg(alert_state=TMP / "alert_retry.json")
    mm = c6.FakeMailer("ambiguous")
    n1, n2, n3 = (t6.deliver_alerts(c, mm, D(2026, 10, 23), ev) for _ in range(3))
    check("alert with an unknown outcome is retried ONCE, then left alone", (n1, n2, n3) == (1, 1, 0))
    # real socket: timeout stub then a good one
    stub = r1.start_stub("timeout")
    c = c6.cfg(alert_state=TMP / "alert_retry2.json")
    a1 = t6.deliver_alerts(c, t6.SmtpMailer("127.0.0.1", stub.port, timeout=1), D(2026, 10, 23), ev)
    stub2 = r1.start_stub("ok")
    a2 = t6.deliver_alerts(c, t6.SmtpMailer("127.0.0.1", stub2.port, timeout=3), D(2026, 10, 23), ev)
    a3 = t6.deliver_alerts(c, t6.SmtpMailer("127.0.0.1", stub2.port, timeout=3), D(2026, 10, 23), ev)
    time.sleep(0.2)
    check("lost alert (SMTP timeout after DATA) reaches the team on the next run, exactly once more", (a1, a2, a3) == (1, 1, 0) and len(stub2.messages) == 1)
    stub.stop()
    stub2.stop()


# =====================================================================================================================
def test_holder_and_cache(cur):
    env = {"LASTRUN_DIR": str(TMP / "holder"), "PAT_BUSY_ALERT_AFTER": "2", "T4_MAX_LOCK_MIN": "10"}
    now = ops.sg_now()
    ops.write_holder("t4", env)
    ops.on_pat_busy("t6", False, env=env, now=now + dt.timedelta(minutes=6))
    n = ops.on_pat_busy("t6", False, env=env, now=now + dt.timedelta(minutes=6))
    check("PAT busy because t4 holds the lock inside its cap (10 min + grace): ordinary backlog, NOT counted", n == 0 and ops.note_streak("t6", False, env) == 0)
    n1 = ops.on_pat_busy("t6", False, env=env, now=now + dt.timedelta(minutes=30))
    n2 = ops.on_pat_busy("t6", False, env=env, now=now + dt.timedelta(minutes=45))
    check("t4 holding the lock far beyond its cap IS counted", (n1, n2) == (1, 2))
    check("T4_MAX_LOCK_MIN sets the allowance (default 10 + 5 grace)", ops.holder_allowance_min("t4", {}) == 15 and ops.holder_allowance_min("t4", {"T4_MAX_LOCK_MIN": "3"}) == 8)
    check("t4 waits at most T4_MAX_LOCK_MIN (10) minutes per run by default", t4.T4_MAX_LOCK_MIN == 10 and "T4_MAX_LOCK_MIN" in inspect.getsource(t4.main))
    # LazyTableau writes/clears the holder file
    saved = os.environ.get("LASTRUN_DIR")
    os.environ["LASTRUN_DIR"] = str(TMP / "holder2")
    try:
        lt = t4.LazyTableau(t4.connect().cursor(), owner="t4", lock_name="R2TEST_PAT_LOCK")   # never the real TABLEAU_PAT_SESSION
        lt._lock()
        h = ops.read_holder()
        lt._unlock()
        check("the PAT lock holder is recorded while the lock is held and removed on release", h and h["script"] == "t4" and ops.read_holder() is None)
    finally:
        if saved is None:
            os.environ.pop("LASTRUN_DIR", None)
        else:
            os.environ["LASTRUN_DIR"] = saved
    # jobs cache
    calls = []

    class Tab:
        def __init__(self):
            self.jobs = NS(get=self.get)

        def __call__(self):
            return self

        def get(self, req_options=None):
            calls.append(1)
            now_ = dt.datetime.now(dt.timezone.utc)
            items = [NS(title="DS%d" % i, type="RefreshExtract", created_at=now_, ended_at=None if i == 3 else now_, id="j%d" % i) for i in range(5)]
            return items, NS(page_number=1, page_size=1000, total_available=5)
    tab = Tab()
    t4.clear_jobs_cache()
    hits = [t4.active_refresh_job(tab, "DS%d" % i) for i in range(5)]
    check("background-job list fetched ONCE for five data sources (cache), results still correct", len(calls) == 1 and [h is not None for h in hits] == [False, False, False, True, False])
    t4.clear_jobs_cache()
    t4.active_refresh_job(tab, "DS1")
    check("clear_jobs_cache forces a new listing", len(calls) == 2)
    t4.active_refresh_job(Tab(), "DS1")
    check("the cache is not shared between Tableau sessions", len(calls) == 3)
    clk = [0.0]
    t4.clear_jobs_cache()
    t4.recent_refresh_jobs(tab, dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24), clock=lambda: clk[0])
    clk[0] = 61
    t4.recent_refresh_jobs(tab, dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24), clock=lambda: clk[0])
    check("the cache expires after the TTL (60 s)", len(calls) == 5)
    t4.clear_jobs_cache()
    check("t4 and t6 share one implementation (t6 calls t4.active_refresh_job)", "t4.active_refresh_job" in inspect.getsource(t6.TableauAdapter.refresh_running))


def test_probe_skipped_kill(cur, today):
    c6.cleanup(cur)
    # refresh-probe failure = busy, alert after 3 runs in a row
    c6.add_sub(cur, "T6TEST-Q1")

    class G(c6.FakeGate):
        sources = {"T6TEST-WB": [{"target": "TGT", "name": "DS1"}]}

    class BadProbe(c6.FakeAdapter):
        def refresh_running(self, names):
            raise RuntimeError("jobs endpoint down")
    saved = os.environ.get("LASTRUN_DIR")
    os.environ["LASTRUN_DIR"] = str(TMP / "probe")
    try:
        codes = []
        for i in range(3):
            ad = BadProbe()
            ctx = c6.mk_ctx(cur, today, ad, c6.FakeMailer(), G())
            s = c6.run_subs(cur, ctx, ["T6TEST-Q1"])
            codes.append(any(e["code"] == "refresh_probe_failed" for e in ctx.events))
        check("job probe failing: treated as BUSY (no export, no claim, kind not_ready) - fail closed", s["kinds"]["not_ready"] == 1 and ad.exports == 0 and c6.logrows(cur) == [])
        check("... and an alert after 3 runs in a row, not before", codes == [False, False, True])
        ad = c6.FakeAdapter()
        ad.refresh_running = lambda names: None
        ctx = c6.mk_ctx(cur, today, ad, c6.FakeMailer(), G())
        c6.run_subs(cur, ctx, ["T6TEST-Q1"])
        check("a run with a working probe resets the streak", ops.note_streak("t6_refresh_probe", False) == 0)
    finally:
        if saved is None:
            os.environ.pop("LASTRUN_DIR", None)
        else:
            os.environ["LASTRUN_DIR"] = saved
    c6.cleanup(cur)
    # missed/not_ready for subscriptions the loop never processed
    ids = ["T6TEST-X1", "T6TEST-X2", "T6TEST-X3"]
    for i in ids:
        c6.add_sub(cur, i)
        r1.set_sched(cur, i, "S")
    r1.set_sched(cur, ids[0], "MISS")
    sc = {"S": r1.daily_all(), "MISS": r1.S("Weekly", (r1.weekday_of(today, -4),), name="MISS")}
    ctx = r1.mk(cur, today, ids, sc, gate=c6.FakeGate((False, "wait", "extract not refreshed")), budget_min=0)
    s = c6.run_subs(cur, ctx, ids)
    check("budget exhausted: every subscription is deferred ...", s["kinds"]["deferred"] == 3)
    check("... yet the missed occurrence is alerted and the due-but-not-ready ones are listed for the cutoff alert",
          any(e["code"] == "missed_due" and e["sub"] == ids[0] for e in ctx.events) and {x[0].id for x in ctx.not_ready} == set(ids[1:]))
    c6.cleanup(cur)
    # kill switch appearing in the middle of a run
    kf = TMP / "KILL_MID"
    saved = os.environ.get("KILL_FILE")
    os.environ["KILL_FILE"] = str(kf)
    kf.unlink(missing_ok=True)

    class Killer(c6.FakeMailer):
        def send(self, raw, f, to):
            r = super().send(raw, f, to)
            kf.write_text("stop")
            return r
    try:
        for i in ids:
            c6.add_sub(cur, i)
        mail = Killer()
        ctx = c6.mk_ctx(cur, today, c6.FakeAdapter(), mail, None)
        s = c6.run_subs(cur, ctx, ids)
        check("kill switch created during a t6 run: the first report went, nothing else is claimed, exit 0", s["kinds"]["sent"] == 1 and s["kinds"]["killed"] == 2
              and len(c6.logrows(cur)) == 1 and t6.exit_code(ctx, s) == 0)
        # t4 cascade
        kf.unlink(missing_ok=True)
        cur.execute("DELETE FROM Integration.Tableau_RefreshJob WHERE DataSource_id LIKE 'R1TEST-%'")
        tab = r1.FakeJobTab(polls=1)
        orig = tab._refresh

        def hook(ds):
            kf.write_text("stop")
            return orig(ds)
        tab.datasources = NS(refresh=hook)
        tg = [(t4.Target("R1TEST-K%d" % i, "K%d" % i), NS(id="R1TEST-K%d" % i, name="K%d" % i)) for i in range(3)]
        res = t4.submit_and_wait(cur, tab, tg, 600, 0, 1, 0)
        check("kill switch during t4's cascade: the running job is finished, the other targets are not started", len(res) == 1 and tab.seq == 1)
        cur.execute("DELETE FROM Integration.Tableau_RefreshJob WHERE DataSource_id LIKE 'R1TEST-%'")
        # t11 pause
        kf.write_text("stop")
        c11.cleanup(cur)
        srv = c11.FakeServer()
        c11.add_sub(cur, "T11TEST-KS")
        srv.add("T11TEST-KS")
        ctx, _ = c11.run_t11(cur, srv, today)
        check("kill switch during t11: no backup row, no pause", srv.subscriptions.updates == [] and c11.backup_rows(cur, "T11TEST-KS") == [] and ctx.n["killed"] == 1)
        c11.cleanup(cur)
    finally:
        kf.unlink(missing_ok=True)
        if saved is None:
            os.environ.pop("KILL_FILE", None)
        else:
            os.environ["KILL_FILE"] = saved
        c6.cleanup(cur)


# =====================================================================================================================
def test_drops():
    real_pager = m2.TSC.Pager
    m2.TSC.Pager = lambda ep: iter(ep.items)
    try:
        srv = r1.fake_t2_server()
        subs, wbn, src, pub, links, st = m2.collect_tableau(srv, pace=0, stored={"w1": 3, "w4": 2})
        check("lagging Metadata: a workbook whose data sources dropped (3 stored -> 2 now) is SKIPPED, rows kept, problem recorded",
              "w1" in st["skipped_wbs"] and "w1" not in wbn and "w4" in wbn and any("dropped from 3" in why for _, i, why in st["problems"] if i == "w1"))
        subs, wbn, src, pub, links, st = m2.collect_tableau(srv, pace=0, stored={"w1": 3, "w4": 2}, accept_drops={"w1"})
        check("--accept-drops <id> lets THAT workbook through (and logs it); another shrunken workbook is still skipped", "w1" in wbn and "w1" not in st["skipped_wbs"])
        subs, wbn, src, pub, links, st = m2.collect_tableau(srv, pace=0, stored={"w1": 3, "w4": 3}, accept_drops={"w1"})
        check("accepting one id does not accept another", "w4" in st["skipped_wbs"] and "w1" in wbn)
        shrunk = r1.fake_t2_server()
        orig_query = shrunk.metadata.query

        def one_source(q, v=None):
            r = orig_query(q, v)
            if v and v.get("luid") == "w1":
                r["data"]["workbooks"][0]["embeddedDatasources"] = r["data"]["workbooks"][0]["embeddedDatasources"][:1]
            return r
        shrunk.metadata.query = one_source
        subs, wbn, src, pub, links, st = m2.collect_tableau(shrunk, pace=0, stored={"w1": 2, "w4": 2})
        check("injected shrunken response (2 stored -> 1 now): skipped as a drop, not silently deactivated as single-source",
              "w1" in st["skipped_wbs"] and st["skipped_single_source"] == 0)
        subs, wbn, src, pub, links, st = m2.collect_tableau(shrunk, pace=0, stored={"w1": 2, "w4": 2}, accept_drops={"w1"})
        check("a legitimate drop to ONE source: explicit --accept-drops <id> -> counted single-source (leaves the scope), logged, not skipped",
              "w1" not in st["skipped_wbs"] and st["skipped_single_source"] == 1)
        subs, wbn, src, pub, links, st = m2.collect_tableau(srv, pace=0, deadline=time.monotonic() - 1)
        check("T2 time cap (T2_MAX_LOCK_MIN): workbooks not reached are recorded as skipped, not read", st["skipped_wbs"] >= {"w1", "w4"} and wbn == {})
    finally:
        m2.TSC.Pager = real_pager
    sources = {"d1": {"name": "A", "linked": "p1", "sql": None, "flag": False, "sql_error": None},
               "d2": {"name": "B", "linked": None, "sql": ["select 1"], "flag": False, "sql_error": None}}
    links = {("w1", "d1"), ("w2", "d2")}
    drops = m2.detect_table_drops({"p1": 5, "d2": 5}, [("p1", "s", "t1"), ("p1", "s", "t2"), ("d2", "s", "x")], sources, links)
    check("table drop guard: fewer tables for a source without custom SQL -> its workbook is skipped; custom-SQL sources are exempt", set(drops) == {"w1"})
    check("table drop guard: an accepted workbook id is let through",
          m2.detect_table_drops({"p1": 5}, [("p1", "s", "t1")], sources, links, accept={"w1"}) == {})
    check("table drop guard: same or more tables is fine", m2.detect_table_drops({"p1": 2}, [("p1", "s", "t1"), ("p1", "s", "t2"), ("p1", "s", "t3")], sources, links) == {})
    check("--accept-drops takes a workbook id (repeatable), it is not a global switch",
          'action="append"' in inspect.getsource(m2.main).split("--accept-drops")[1][:120] and "only shrink through an accepted drop" in m2.__doc__)


# =====================================================================================================================
def test_summary_sql_hash(cur, today):
    c6.cleanup(cur)
    cur.execute("DELETE FROM Integration.Tableau_Schedule WHERE Schedule_id LIKE 'R1TEST-%'")
    wd = r1.weekday_of(today, 2)[:3]
    cur.execute("INSERT Integration.Tableau_Schedule (Schedule_id, Name, State, Frequency, WeekDays, StartTime) VALUES ('R1TEST-OTHER', 'o', 'Active', 'Weekly', ?, '00:01')", wd)
    c6.add_sub(cur, "T6TEST-Y1")
    r1.set_sched(cur, "T6TEST-Y1", "R1TEST-OTHER")
    c6.add_backup(cur, "T6TEST-Y1", taken=1, paused=True)
    cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET PausedAt = DATEADD(day, -9, SYSDATETIMEOFFSET()) WHERE Subscription_id = 'T6TEST-Y1'")
    c6.add_sub(cur, "T6TEST-Y2")
    cur.execute("INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status, Message) VALUES ('T6TEST-Y2', 'Released', 'local test')")
    c6.add_sub(cur, "T6TEST-Y3")
    cur.execute("INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status, ClaimedAt, Message) VALUES ('T6TEST-Y3', 'TimedOut', DATEADD(day, -1, SYSDATETIMEOFFSET()), 'old')")
    env = r1.mail_env("r2sum")
    now = dt.datetime.now(ops.SGT)
    ops.write_marker("t4", "execute", 0, "RUN SUMMARY x", env=env, now=now - dt.timedelta(hours=4))
    ops.write_marker("t4", "dry-run", 0, "x", env=env, now=now - dt.timedelta(minutes=2))
    d = ds_.collect(cur, today, now.replace(tzinfo=None), 90, dict(env, SCHEDULE_MODE="honour"))
    names = {s[0]: s for s in d["scripts"]}
    check("daily summary: a later DRY run does not hide that the scheduled (execute) task stopped", names["t4"][3] and "stopped" in names["t4"][2])
    check("daily summary: 'sent' counts Status = Sent only; Released is listed separately", all(r[2] == "Sent" for r in d["sent"]) and d["released"] >= 1
          and "not counted" in ds_.render(d))
    check("daily summary: Claimed/TimedOut rows from EARLIER days are listed", any(x[0] == "T6TEST-Y3" and x[2] == "TimedOut" for x in d["old_claims"]) and "EARLIER days" in ds_.render(d))
    honour = [x for x in d["not_sent"] if x[1] == "T6TEST subject" and "T6TEST" not in x[0]]
    d2 = ds_.collect(cur, today, now.replace(tzinfo=None), 90, dict(env, SCHEDULE_MODE="ignore"))
    check("daily summary reads SCHEDULE_MODE like t6: honour hides a subscription that is not due today, ignore lists it",
          len(d2["not_sent"]) == len(d["not_sent"]) + 1)
    try:
        ds_.collect(cur, today, now.replace(tzinfo=None), 90, dict(env, SCHEDULE_MODE="sometimes"))
        r = "accepted"
    except ValueError:
        r = "refused"
    check("... and a bad SCHEDULE_MODE is refused the same way", r == "refused")
    # SQL guards
    for f in (ROOT / "scripts" / "sql").glob("*.sql"):
        t = f.read_text(encoding="utf-8")
        lines = [x.strip() for x in t.splitlines() if x.strip()]
        check("%s: guard checks DB_NAME() AND @@SERVERNAME, sets NOEXEC ON, and the file ends with SET NOEXEC OFF; GO" % f.name,
              "DB_NAME()" in t and "@@SERVERNAME" in t and "SET NOEXEC ON" in t and lines[-1] == "GO" and lines[-2] == "SET NOEXEC OFF;")
    c6.cleanup(cur)
    c6.add_sub(cur, "T6TEST-Y4")
    c6.add_backup(cur, "T6TEST-Y4", taken=1, paused=True)
    txt = (ROOT / "scripts" / "sql" / "R1_schedule_schema_rollback.sql").read_text(encoding="utf-8")
    import re
    batches = [b for b in re.split(r"(?im)^\s*GO\s*$", txt) if b.strip()]
    err = ""
    cur.execute("BEGIN TRAN")
    try:
        for b in batches:
            try:
                cur.execute(b)
            except Exception as e:
                err = str(e)
                break
    finally:
        cur.execute("IF @@TRANCOUNT > 0 ROLLBACK")
    col = cur.execute("SELECT COL_LENGTH('Integration.Tableau_Subscription', 'ScheduleId')").fetchone()[0]
    check("R1 rollback SQL refuses while a takeover row is open (50005) and drops nothing", "50005" in err and col is not None, err[:120])
    check("R1 rollback comment points at SCHEDULE_MODE=ignore", "--schedule-mode ignore" in txt and "--ignore-schedule" not in txt)
    # salted hash
    saved = os.environ.get("ADDR_HASH_SALT")
    os.environ.pop("ADDR_HASH_SALT", None)
    h0 = t6.addr_hash(["a@b.com"])
    os.environ["ADDR_HASH_SALT"] = "pepper"
    h1 = t6.addr_hash(["a@b.com"])
    os.environ["ADDR_HASH_SALT"] = "other"
    h2 = t6.addr_hash(["A@b.com"])
    if saved is None:
        os.environ.pop("ADDR_HASH_SALT", None)
    else:
        os.environ["ADDR_HASH_SALT"] = saved
    check("recipient hash is salted from ADDR_HASH_SALT (different salt -> different hash, case-insensitive address)", len({h0, h1, h2}) == 3 and len(h0) == 10)
    # t2 failure text sanitised
    stub = r1.start_stub("ok")
    env2 = r1.mail_env("t2san")
    saved_env = dict(os.environ)
    try:
        os.environ.update(env2)
        os.environ["CONN_SERVER"] = "person@secret.example.com"
        rc = m2.main(["--local-port", str(stub.port)])
        time.sleep(0.3)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
    body = stub.messages[0].decode(errors="replace") if stub.messages else ""
    check("T2 failure alert text is sanitised (no address-like text)", rc == 1 and body and "person@secret" not in body.replace("=\r\n", ""))
    stub.stop()
    c6.cleanup(cur)
    cur.execute("DELETE FROM Integration.Tableau_Schedule WHERE Schedule_id LIKE 'R1TEST-%'")


# =====================================================================================================================
def test_review3(cur, today):
    # ---- alert_config: execute / local_smtp
    smtp_env = {"MAIL_TRANSPORT": "smtp", "SMTP_PROVIDER": "gmail"}   # NO credentials, NO allow-list
    c = ops.alert_config(False, smtp_env, "x", execute=False)
    check("alert_config for a DRY run with MAIL_TRANSPORT=smtp and no SMTP credentials does not die and stays local", c.ses is False)
    try:
        ops.alert_config(False, smtp_env, "x", execute=True)
        r = "built"
    except t6.ConfigError:
        r = "refused"
    check("... an execute run without credentials is refused at startup", r == "refused")
    full = dict(smtp_env, SMTP_USER="a@b.com", SMTP_PASSWORD="pw", SEND_ALLOWED_RECIPIENTS="a@b.com")
    check("alert_config: smtp with credentials -> real; --local-smtp forces local; transport follows execute",
          ops.alert_config(False, full, "x", execute=True).ses and not ops.alert_config(False, full, "x", execute=True, local_smtp=True).ses
          and not ops.alert_config(False, full, "x", execute=False).ses)
    check("effective_ses honours local", not ops.effective_ses(True, True, full, True))
    check("every script source has the --local-smtp flag and uses ops.log_dir()", all("--local-smtp" in inspect.getsource(m) for m in (t6, t4, t11, m2, ds_))
          and all("ops.log_file_handler(" in inspect.getsource(m) for m in (t6, t4, t11, m2, ds_)))
    ld = ops.log_dir({"LOG_DIR": str(TMP / "ld")})
    check("LOG_DIR redirects the log folder", ld == TMP / "ld" and ld.is_dir())
    # ---- child env of t2_checks
    import t2_checks
    saved = {k: os.environ.get(k) for k in ("SMTP_PASSWORD", "MAIL_TRANSPORT", "SEND_ALLOWED_RECIPIENTS")}
    os.environ.update(SMTP_PASSWORD="x", MAIL_TRANSPORT="smtp", SEND_ALLOWED_RECIPIENTS="a@b.com")
    try:
        ce = t2_checks.child_env()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    check("t2_checks child environment: local transport, no SMTP/allow-list, markers/logs/state in a temp folder, event log off",
          ce["MAIL_TRANSPORT"] == "local" and "SMTP_PASSWORD" not in ce and "SEND_ALLOWED_RECIPIENTS" not in ce and ce["EVENTLOG_DISABLED"] == "1"
          and ce["LASTRUN_DIR"] == ce["OPS_STATE_DIR"] == ce["LOG_DIR"])
    check("ADDR_HASH_SALT warning exists when unset, none when set", ops.salt_warning({}) and ops.salt_warning({"ADDR_HASH_SALT": "x"}) is None)
    check("T2 holds the PAT lock at most T2_MAX_LOCK_MIN (default 10) and the holder allowance matches (+5)",
          m2.T2_MAX_LOCK_MIN == 10 and ops.holder_allowance_min("t2", {}) == 15 and ops.holder_allowance_min("t2", {"T2_MAX_LOCK_MIN": "3"}) == 8)
    # ---- R3: send-log delete guard
    c6.cleanup(cur)
    cur.execute("EXEC sp_set_session_context N'allow_fixture_delete', 1")
    c6.add_sub(cur, "T6TEST-D1")
    cur.execute("INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status, SentAt, Message) VALUES ('T6TEST-D1', 'Sent', SYSDATETIMEOFFSET(), 'x')")

    def delete_err(sql, *a):
        try:
            cur.execute(sql, *a)
            return ""
        except Exception as e:
            return str(e)
    cur.execute("EXEC sp_set_session_context N'allow_fixture_delete', 0")
    e1 = delete_err("DELETE FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id = 'T6TEST-D1'")
    n1 = cur.execute("SELECT COUNT(*) FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id = 'T6TEST-D1'").fetchone()[0]
    check("R3: deleting a Sent row (which would free the slot) is refused without the fixture flag", "50007" in e1 and n1 == 1, e1[:80])
    cur.execute("EXEC sp_set_session_context N'allow_fixture_delete', 1")
    e2 = delete_err("DELETE FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id = 'T6TEST-D1'")
    check("R3: ... allowed for a fixture subscription id when the test session sets the flag", e2 == "" and cur.execute(
        "SELECT COUNT(*) FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id = 'T6TEST-D1'").fetchone()[0] == 0)
    cur.execute("INSERT Integration.Tableau_Subscription (Subscription_id, Workbook_id, TargetType, TargetId, Format, Subject, Active_Flag) "
                "VALUES ('ZZNOFIX-1', 'ZZWB', 'Workbook', 'ZZT', 'PDF', 's', 0)")
    cur.execute("INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status, Message) VALUES ('ZZNOFIX-1', 'TimedOut', 'x')")
    e3 = delete_err("DELETE FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id = 'ZZNOFIX-1'")
    check("R3: a NON-fixture TimedOut row cannot be deleted even with the flag", "50007" in e3)
    cur.execute("UPDATE Integration.Tableau_SubscriptionSendLog SET Status = 'Released' WHERE Subscription_id = 'ZZNOFIX-1'")
    cur.execute("EXEC sp_set_session_context N'allow_fixture_delete', 0")
    e4 = delete_err("DELETE FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id = 'ZZNOFIX-1'")
    check("R3: after releasing it (RUNBOOK procedure) the row can be deleted without any flag", e4 == "")
    cur.execute("DELETE FROM Integration.Tableau_Subscription WHERE Subscription_id = 'ZZNOFIX-1'")
    cur.execute("EXEC sp_set_session_context N'allow_fixture_delete', 1")
    check("R3 DDL and rollback files exist", all((ROOT / "scripts" / "sql" / f).exists() for f in ("R3_sendlog_guard.sql", "R3_sendlog_guard_rollback.sql")))
    check("T1 rollback refuses while takeover rows are open", "THROW 50006" in (ROOT / "scripts" / "sql" / "T1_mapping_schema_rollback.sql").read_text(encoding="utf-8"))
    c6.cleanup(cur)
    # ---- probe matches the PUBLISHED name
    now_ = dt.datetime.now(dt.timezone.utc)

    class Tab:
        def __init__(self, fail=False):
            self.fail = fail
            self.datasources = NS(get_by_id=self.get_ds)
            self.jobs = NS(get=self.get_jobs)

        def __call__(self):
            return self

        def get_ds(self, tid):
            if self.fail:
                raise RuntimeError("down")
            return NS(name="PUB1")

        def get_jobs(self, req_options=None):
            return [NS(title="PUB1", type="RefreshExtract", created_at=now_, ended_at=None, id="j1")], NS(page_number=1, page_size=1000, total_available=1)

    class G:
        sources = {"WB": [{"target": "TGT", "name": "(Extract) PUB1"}]}
    t4.clear_jobs_cache()
    ctx = NS(gate=G(), adapter=t6.TableauAdapter(Tab()), probe_fail=0, probe_ok=0)
    why = t6.refresh_busy(ctx, NS(id="s", wb_id="WB"))
    check("t6 matches the refresh job on the PUBLISHED data source name (workbook-side '(Extract) PUB1' would have missed it)", why and "PUB1" in why)
    t4.clear_jobs_cache()
    ctx = NS(gate=G(), adapter=t6.TableauAdapter(Tab(fail=True)), probe_fail=0, probe_ok=0)
    why = t6.refresh_busy(ctx, NS(id="s", wb_id="WB"))
    check("... and a failed published-name lookup counts as busy (fail closed)", why and "not exporting blind" in why and ctx.probe_fail == 1)
    t4.clear_jobs_cache()

    # ---- t11 owner fence
    def A(**kw):
        base = dict(execute=False, ses=False, workbooks=None, subscription_ids=None, force_notready=False, retake_restored=False,
                    restore=None, restore_all_for_workbook=None, max_mapping_age_h=26.0)
        base.update(kw)
        return NS(**base)
    guid = "14000013-8c5a-4251-baef-972ed283a4cd"
    gm = {"MAIL_TRANSPORT": "smtp", "SMTP_PROVIDER": "gmail", "SMTP_USER": "me@example.com", "SMTP_PASSWORD": "pw", "SEND_ALLOWED_RECIPIENTS": "me@example.com"}
    try:
        t11.build_config(A(execute=True, workbooks=[guid]), gm)
        r = "accepted"
    except t6.ConfigError as e:
        r = "refused" if "owner-user-id" in str(e) else "other"
    check("t11 --execute in Gmail test mode without --owner-user-id is refused", r == "refused")
    ok1 = t11.build_config(A(execute=True, workbooks=[guid], owner_user_id=["U-1"]), gm).owner_user_ids == {"u-1"}
    ok2 = t11.build_config(A(execute=True, workbooks=[guid]), dict(gm, TAKEOVER_OWNER_USER_IDS="U-2")).owner_user_ids == {"u-2"}
    ok3 = t11.build_config(A(execute=False, workbooks=[guid]), gm).owner_user_ids == frozenset()
    ok4 = t11.build_config(A(execute=True, restore=["x"]), gm).execute
    try:
        t11.build_config(A(execute=True, workbooks=[guid]), {"MAIL_TRANSPORT": "local"})
        ok5 = False
    except t6.ConfigError:
        ok5 = True     # R5 / F-2: the fence is required in every transport, not only Gmail
    check("... accepted with --owner-user-id or the env, not needed for a dry run or a restore; since R5 (F-2) also required with local mail", ok1 and ok2 and ok3 and ok4 and ok5)
    c11.cleanup(cur)
    srv = c11.FakeServer()
    for sid in ("T11TEST-O1", "T11TEST-O2"):
        c11.add_sub(cur, sid)
        srv.add(sid)
    srv.subscriptions.items["T11TEST-O2"].user_id = "SOMEONE-ELSE"
    ctx, _ = c11.run_t11(cur, srv, today, owner_user_ids=frozenset({"user-1"}))
    check("t11 pilot fence: only the subscription whose subscriber is the given Tableau user id is paused",
          [u[0] for u in srv.subscriptions.updates] == ["T11TEST-O1"] and ctx.n["skip_not_owner"] == 1)
    c11.cleanup(cur)



def test_review_list(cur, today):
    """review_list.py (R3B_REVIEW F-7/F-8): formula neutralisation, takeover-day/catch-up effects in next_due, CSV content, read-only."""
    import csv
    import review_list as rl
    # ---- formula neutralisation
    check("review_list: a cell starting with = + - @ tab or CR gets a leading quote; other text, numbers and None are untouched",
          [rl.safe_cell(x) for x in ("=1+1", "+cmd", "-5", "@SUM(A1)", "\tx", "\rx")] == ["'=1+1", "'+cmd", "'-5", "'@SUM(A1)", "'\tx", "'\rx"]
          and [rl.safe_cell(x) for x in ("Daily report", "2026-11-01", "", None, 5)] == ["Daily report", "2026-11-01", "", None, 5])
    # ---- next_due: takeover-day guard and catch-up window
    mo = sd.Schedule(id="m", name="1100AM_1stDayEveryMonth", frequency="Monthly", month_tokens=("1",), start=dt.time(11, 0))
    dly = sd.Schedule(id="d", name="d", frequency="Daily", weekdays=frozenset(), start=dt.time(11, 0), interval_hours=24.0)
    nov1 = D(2026, 11, 1)
    first, fire, until, note = rl.next_due(mo, nov1, dt.datetime(2026, 11, 1, 9, 0), 3)
    check("next_due monthly, taken over 09:00 on the 1st (schedule 11:00 still ahead): first send today, retried until the 4th (catch-up)",
          first == nov1 and fire == nov1 and until == D(2026, 11, 4) and "2026-11-04" in note and "guard" not in note, str((first, fire, until, note)))
    first, fire, until, note = rl.next_due(mo, nov1, dt.datetime(2026, 11, 1, 12, 0), 3)
    check("next_due monthly, taken over 12:00 on the 1st (11:00 passed): first send is 1 Dec, NOT today; next_fire_date still shows today; note says why",
          first == D(2026, 12, 1) and fire == nov1 and until == D(2026, 12, 4) and "takeover-day guard" in note and "11:00" in note, str((first, fire, until, note)))
    first, fire, until, note = rl.next_due(mo, D(2026, 10, 23), dt.datetime(2026, 10, 23, 12, 0), 3)
    check("next_due monthly, taken over on 23 Oct: first send 1 Nov (no guard), retried until 4 Nov", first == nov1 and until == D(2026, 11, 4) and "guard" not in note)
    first, fire, until, note = rl.next_due(dly, nov1, dt.datetime(2026, 11, 1, 12, 0), 3)
    check("next_due daily after 11:00: first send tomorrow, no retry beyond the day (next occurrence follows)", first == D(2026, 11, 2) and until == first and "guard" in note)
    check("next_due: no schedule / unsupported give no date and a reason; a disabled schedule gives a date like an enabled one",
          rl.next_due(None, nov1)[0] is None and "no schedule" in rl.next_due(None, nov1)[3]
          and rl.next_due(sd.Schedule(id="s", state="Suspended", frequency="Daily"), nov1)[1] == nov1
          and "unsupported" in rl.next_due(sd.Schedule(id="u", frequency="Weekly", start=dt.time(9, 0)), nov1)[3])
    # ---- build_rows + CSV on fixtures
    cur.execute("INSERT Integration.Tableau_Schedule (Schedule_id, Name, State, Frequency, MonthDays, StartTime) "
                "VALUES ('R1TEST-RLS', '1100AM_1stDayEveryMonth', 'Active', 'Monthly', '1', '11:00:00')")
    c6.add_sub(cur, "T6TEST-RL1")
    cur.execute("UPDATE Integration.Tableau_Subscription SET ScheduleId = 'R1TEST-RLS', Subject = '=HYPERLINK(\"http://x\",\"y\")' WHERE Subscription_id = 'T6TEST-RL1'")
    live = {"T6TEST-RL1": ("USER-1", True)}
    rows = rl.build_rows(cur, nov1, {}, live, 14, now=dt.datetime(2026, 11, 1, 12, 0))
    r = next(x for x in rows if x["subscription_id"] == "T6TEST-RL1")
    check("review_list rows: next_due = first send after the takeover-day guard, plus next_fire_date, retry_until and a note; suspended_now from Tableau",
          r["next_due"] == "2026-12-01" and r["next_fire_date"] == "2026-11-01" and r["retry_until"] == "2026-12-04"
          and "takeover-day guard" in r["next_due_note"] and r["due_in_14_days"] == "no" and r["suspended_now"] == "yes" and r["found_in_tableau"] == "yes", str(r)[:300])
    rows = rl.build_rows(cur, nov1, {}, live, 14, now=dt.datetime(2026, 11, 1, 9, 0))
    r = next(x for x in rows if x["subscription_id"] == "T6TEST-RL1")
    check("review_list rows: taken over before the schedule time: due today, within 14 days", r["next_due"] == "2026-11-01" and r["due_in_14_days"] == "yes"
          and r["retry_until"] == "2026-11-04")
    rows = rl.build_rows(cur, nov1, {}, {}, 14, now=dt.datetime(2026, 11, 1, 9, 0))
    r = next(x for x in rows if x["subscription_id"] == "T6TEST-RL1")
    check("review_list rows: not found in Tableau -> flagged, suspended_now 'unknown'", r["found_in_tableau"] == "no" and r["suspended_now"].startswith("unknown"))
    # ---- main(): CSV written with neutralised cells, Tableau never touched (load_live patched), DWH not changed
    out = TMP / "review_list_test.csv"
    snap = tuple(cur.execute("SELECT COUNT(*), CHECKSUM_AGG(CHECKSUM(*)) FROM Integration.Tableau_Subscription").fetchone())
    real_load = rl.load_live
    rl.load_live = lambda cur_, owner="review_list": ({}, {"T6TEST-RL1": ("USER-1", False)})
    try:
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = rl.main(["--out", str(out)])
    finally:
        rl.load_live = real_load
    with out.open(encoding="utf-8-sig", newline="") as f:
        data = list(csv.DictReader(f))
    mine = [x for x in data if x["subscription_id"] == "T6TEST-RL1"]
    check("review_list main: exit 0, one CSV row per active subscription, columns as documented", code == 0 and list(data[0].keys()) == rl.COLUMNS
          and len(data) == cur.execute("SELECT COUNT(*) FROM Integration.Tableau_Subscription WHERE Active_Flag = 1").fetchone()[0])
    check("review_list main: the formula subject is written with a leading quote, so a spreadsheet shows text instead of running it",
          len(mine) == 1 and mine[0]["subject"].startswith("'=HYPERLINK") and not any(v.startswith(rl.FORMULA_START) for x in data for v in x.values() if v))
    check("review_list main: no e-mail address in the file and the DWH is unchanged (read-only)",
          "@" not in out.read_text(encoding="utf-8-sig").replace("'@", "") and
          tuple(cur.execute("SELECT COUNT(*), CHECKSUM_AGG(CHECKSUM(*)) FROM Integration.Tableau_Subscription").fetchone()) == snap)
    src = inspect.getsource(rl)
    check("review_list is read-only by construction: no SQL write and no Tableau write/refresh/mail call in its source",
          all(w not in src for w in ("subscriptions.update", "subscriptions.create", "subscriptions.delete", "workbooks.refresh",
                                     "datasources.refresh", "smtplib", "INSERT ", "UPDATE ", "DELETE FROM", "MERGE ")))


def test_event_once():
    """ops.event_log_once (F-1): one Event Log entry per (script, key) and Singapore day; the day after it writes again."""
    calls = []
    real = ops.event_log
    ops.event_log = lambda script, text, level="ERROR", env=os.environ: calls.append((script, text, level)) or True
    env = {"OPS_STATE_DIR": str(TMP / "once_state")}
    try:
        a = ops.event_log_once("t6", "k1", "first", "WARNING", env=env, day=D(2026, 11, 1))
        b = ops.event_log_once("t6", "k1", "again", "WARNING", env=env, day=D(2026, 11, 1))
        c = ops.event_log_once("t6", "k2", "other key", "WARNING", env=env, day=D(2026, 11, 1))
        d = ops.event_log_once("t6", "k1", "next day", "WARNING", env=env, day=D(2026, 11, 2))
        e = ops.event_log_once("t11", "k1", "other script", "WARNING", env=env, day=D(2026, 11, 2))
    finally:
        ops.event_log = real
    check("event_log_once: once per script, key and day; a new day or key writes again; level is passed on",
          [a, b, c, d, e] == [True, False, True, True, True] and [x[1] for x in calls] == ["first", "other key", "next day", "other script"]
          and all(x[2] == "WARNING" for x in calls))
    (TMP / "once_file").write_text("x")
    bad = ops.event_log_once("t6", "k9", "x", "WARNING", env={"OPS_STATE_DIR": str(TMP / "once_file" / "sub")}, day=D(2026, 11, 1))
    check("event_log_once never raises, even when its state folder is unusable", bad in (True, False))


cur_ = None
today_ = None


def main():
    global cur_, today_
    os.environ["LOG_DIR"] = str(TMP / "logs")   # never attach the real out/*.log
    os.environ["LASTRUN_DIR"] = str(TMP / "lastrun_default")
    os.environ["OPS_STATE_DIR"] = str(TMP / "state_default")
    os.environ["EVENTLOG_DISABLED"] = "1"
    for k in [k for k in os.environ if k.startswith(("SES_", "SMTP_"))] + ["SEND_ALLOWED_RECIPIENTS", "SEND_ALLOW_ALL_UAT", "SEND_FROM", "ALERT_TO", "SCHEDULE_MODE",
                                                                         "TABLEAU_AUTOMATION_DISABLED", "MAIL_TRANSPORT", "ADDR_HASH_SALT"]:
        os.environ.pop(k, None)
    real_smtp = smtplib.SMTP

    def loopback_only(host, *a, **kw):
        if str(host) not in ("127.0.0.1", "localhost"):
            raise AssertionError("test tried to open an SMTP connection to %r" % host)
        return real_smtp(host, *a, **kw)
    smtplib.SMTP = loopback_only
    for lg in (t6.log, t4.log, t11.log, m2.log, ops.log):
        lg.setLevel(logging.CRITICAL)
    conn = t4.connect()
    cur_ = cur = conn.cursor()
    cur.execute("EXEC sp_set_session_context N'allow_fixture_delete', 1")
    today_ = today = t4.dwh_today(cur)
    c6.cleanup(cur)
    c11.cleanup(cur)
    before = r1.checksum(cur)
    try:
        test_transport()
        test_eventlog_alert_retry()
        test_holder_and_cache(cur)
        test_probe_skipped_kill(cur, today)
        test_drops()
        test_summary_sql_hash(cur, today)
        test_review3(cur, today)
        test_review_list(cur, today)
        test_event_once()
    finally:
        c6.cleanup(cur)
        c11.cleanup(cur)
        cur.execute("DELETE FROM Integration.Tableau_Schedule WHERE Schedule_id LIKE 'R1TEST-%'")
        cur.execute("DELETE FROM Integration.Tableau_RefreshJob WHERE DataSource_id LIKE 'R1TEST-%'")
        smtplib.SMTP = real_smtp
    check("other rows of Tableau_Subscription / SendLog / Backup unchanged", r1.checksum(cur) == before)
    print("\n%d failure(s): %s" % (len(fails), fails) if fails else "\nALL CHECKS PASSED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
