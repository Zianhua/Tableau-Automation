"""T6/T7 checks. No real mail and no Tableau access: a fake export layer and a fake mailer stand in, plus tiny local
SMTP servers for the SMTP error classification. Dev DWH only: fixture rows under Subscription_id 'T6TEST-%' in
Integration.Tableau_Subscription and Integration.Tableau_SubscriptionSendLog (deleted at the start and at the end), and a
checksum proves no other row of those tables changed. ETL tables are only READ.

Usage:
  python scripts/t6_checks.py            # all checks
  python scripts/t6_checks.py --e2e      # ALSO the local end-to-end run for the CFD Accounts v2 test subscription
                                         # (real export from Tableau UAT, delivery to a local SMTP sink only; uses the PAT)
"""
import datetime as dt
import hashlib
import io
import logging
import os
import smtplib
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent))
import subscription_mail as sm  # noqa: E402
import t6_send as m  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LIKE = "T6TEST-%"
fails = []
TMP = Path(tempfile.mkdtemp(prefix="t6checks_"))
SECRET_ADDR = "secret.person@example.com"


def check(name, ok, detail=""):
    print("%s  %s %s" % ("PASS" if ok else "FAIL", name, detail))
    if not ok:
        fails.append(name)


# ---------- fakes ----------
def png(w, h):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


PDF = b"%PDF-1.4\n/Type /Page\n" + b"x" * 2000


class FakeAdapter:
    """describe/export like TableauAdapter. Per-call behaviour is set through attributes."""

    def __init__(self, emails=(SECRET_ADDR,), suspended=False, pdf=True, image=True, export_error=None, images=None,
                 target_id=None, pdf_bytes=PDF, page_size="A4", orientation="PORTRAIT", wb_updated_at=None):
        self.emails, self.suspended, self.pdf, self.image = list(emails), suspended, pdf, image
        self.export_error, self.target_id, self.pdf_bytes = export_error, target_id, pdf_bytes
        self.page_size, self.orientation = page_size, orientation
        self.wb_updated_at = wb_updated_at or dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        self.images = images if images is not None else [{"name": "Dash 1", "data": png(100, 100), "orig_w": 100, "orig_h": 100,
                                                          "w": 100, "h": 100, "capped": False, "unreadable": False}]
        self.exports = 0
        self.recipient_reads = 0

    def describe(self, sub, recipients=True):
        self.recipient_reads += 1 if recipients else 0
        return m.Live(suspended=self.suspended, target_id=self.target_id or sub.target_id, attach_pdf=self.pdf,
                      attach_image=self.image, page_size=self.page_size, orientation=self.orientation, subject="Subj", message="",
                      emails=self.emails if recipients else [], label="WB label", ref_kind="workbook", ref_name="WB name", n_views=len(self.images),
                      wb_updated_at=self.wb_updated_at)

    def export(self, sub, live):
        self.exports += 1
        if self.export_error:
            raise RuntimeError(self.export_error)
        return (self.pdf_bytes if live.attach_pdf else None), (self.images if live.attach_image else [])


class FakeMailer:
    def __init__(self, mode="ok", delay=0):
        self.mode, self.delay, self.sent = mode, delay, []
        self.lock = threading.Lock()

    def send(self, raw, from_addr, to_addrs):
        if self.delay:
            time.sleep(self.delay)
        if self.mode == "predata":
            raise m.PreDataError("connect refused for %s" % SECRET_ADDR)
        if self.mode == "ambiguous":
            raise m.AmbiguousSendError("timeout after DATA")
        with self.lock:
            self.sent.append((raw, from_addr, list(to_addrs)))


def reports(mail):
    """Report mails only (the alert mail, if any, is excluded)."""
    return [x for x in mail.sent if b"need attention" not in x[0]]


class FakeGate:
    etl = {}

    def __init__(self, result=(True, "ready", "ok")):
        self.result = result

    def check(self, wb):
        return self.result


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


def cfg(**kw):
    # include_untaken: the older flow tests use plain (not taken over) fixtures; the takeover rule has its own tests below
    base = dict(execute=True, local_release=False, id_like=LIKE, alert_state=TMP / "alerts.json", cutoff=m.dt.time(23, 59, 59),
                include_untaken=True, export_pause_s=0, schedule_enforce=False, takeover_day_guard=False,
                sender="tableau-reports@example.com")
    base.update(kw)
    return m.Config(**base)


# ---------- DWH helpers ----------
def others_checksum(cur):
    out = []
    for t, col in (("Tableau_Subscription", "Subscription_id"), ("Tableau_SubscriptionSendLog", "Subscription_id"),
                   ("Tableau_SubscriptionBackup", "Subscription_id")):
        out.append(tuple(cur.execute("SELECT COUNT(*), CHECKSUM_AGG(CHECKSUM(*)) FROM Integration.%s WHERE %s NOT LIKE ?" % (t, col),
                                     LIKE).fetchone()))
    return out


def cleanup(cur):
    cur.execute("DELETE FROM Integration.Tableau_Priority WHERE Item_id LIKE ?", LIKE)
    cur.execute("DELETE FROM Integration.Tableau_SubscriptionBackup WHERE Subscription_id LIKE ?", LIKE)
    cur.execute("DELETE FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id LIKE ?", LIKE)
    cur.execute("DELETE FROM Integration.Tableau_Subscription WHERE Subscription_id LIKE ?", LIKE)


def test_render_as_subscriber():
    """D-063: with render_as the export runs in the subscriber's own session and signs out; a failed sign-in exports nothing as anyone."""
    from types import SimpleNamespace as NS
    calls = []

    class Srv:
        def __init__(self, who):
            self.who = who
            self.workbooks = NS(get_by_id=lambda i: NS(id=i, views=[]), populate_views=lambda t: setattr(t, "views", [NS(name="v")]))
            self.views = NS(get_by_id=lambda i: NS(id=i))
            self.auth = NS(sign_out=lambda: calls.append(("sign_out", who)))

    saved = (m.sm.export_pdf, m.sm.export_images, m.sign_in_as_subscriber)
    m.sm.export_pdf = lambda srv, target, is_wb, ps, o: (calls.append(("pdf", srv.who)) or b"%PDF")
    m.sm.export_images = lambda srv, views: (calls.append(("img", srv.who)) or [])

    def live(uid, is_wb=True):
        return m.Live(False, "T1", True, True, "a4", "portrait", "s", "", [], "L", "workbook", "n", 1, None,
                      {"target": NS(id="T1"), "views": [NS(name="v")], "is_wb": is_wb, "user_id": uid})
    try:
        def fake_sign_in(uid, env=None):
            if uid == "bad":
                raise RuntimeError("sign-in as the subscriber failed (Boom); is the admin account's password still valid?")
            return Srv("as:" + uid)
        m.sign_in_as_subscriber = fake_sign_in
        m.TableauAdapter(lambda: Srv("pat")).export(NS(id="x"), live("u1"))
        check("render as subscriber: off -> exported on the service session, no sign-in", calls == [("pdf", "pat"), ("img", "pat")], str(calls))
        calls.clear()
        m.TableauAdapter(lambda: Srv("pat"), render_as=True).export(NS(id="x"), live("u1"))
        check("render as subscriber: on -> subscriber's own session, signed out after",
              calls == [("pdf", "as:u1"), ("img", "as:u1"), ("sign_out", "as:u1")], str(calls))
        calls.clear()
        m.TableauAdapter(lambda: Srv("pat"), render_as=True).export(NS(id="x"), live("u2", is_wb=False))
        check("render as subscriber: single view too", [c[1] for c in calls] == ["as:u2"] * 3, str(calls))
        calls.clear()
        try:
            m.TableauAdapter(lambda: Srv("pat"), render_as=True).export(NS(id="x"), live("bad"))
            check("render as subscriber: failed sign-in raises", False)
        except RuntimeError as e:
            check("render as subscriber: failed sign-in raises, nothing exported on the service session",
                  calls == [] and "password still valid" in str(e), str(calls))
    finally:
        m.sm.export_pdf, m.sm.export_images, m.sign_in_as_subscriber = saved
    check("render as subscriber: off by default", m.render_as_subscriber({}) is False)
    env = {"RENDER_AS_SUBSCRIBER": "1"}
    try:
        m.render_as_subscriber(env)
        check("render as subscriber: on without the admin account is a startup failure", False)
    except RuntimeError:
        check("render as subscriber: on without the admin account is a startup failure", True)
    env.update(TABLEAU_IMPERSONATOR_NAME="admin", TABLEAU_IMPERSONATOR_PASSWORD="x")
    check("render as subscriber: on with the admin account", m.render_as_subscriber(env) is True)


def test_send_order(cur):
    """D-062: marked subscriptions are loaded first (lowest number first); unmarked ones keep the usual order."""
    for sid in ("T6TEST-OA", "T6TEST-OB", "T6TEST-OC"):
        add_sub(cur, sid)
    ids = lambda: [x.id for x in m.load_subscriptions(cur, id_like="T6TEST-O%")]
    base = ids()
    check("send order: no marks -> the usual order, nothing lost", sorted(base) == ["T6TEST-OA", "T6TEST-OB", "T6TEST-OC"], str(base))
    ins = "INSERT INTO Integration.Tableau_Priority (Kind, Item_id, Priority) VALUES ('Subscription', ?, ?)"
    last = base[-1]
    cur.execute(ins, last, 1)
    check("send order: a marked subscription goes first, the rest keep their order", ids() == [last] + base[:-1], str(ids()))
    cur.execute(ins, base[0], 0)
    check("send order: lower number first", ids()[:2] == [base[0], last], str(ids()))
    cur.execute("DELETE FROM Integration.Tableau_Priority WHERE Item_id LIKE 'T6TEST-O%'")
    cur.execute("INSERT INTO Integration.Tableau_Priority (Kind, Item_id, Priority) VALUES ('Extract', ?, 0)", last)
    check("send order: an Extract mark does not rank a subscription", ids() == base, str(ids()))
    cleanup(cur)


def add_sub(cur, sid, susp=0, active=1, key="auto", fmt="PDF", age_h=0):
    if key == "auto":
        key = hashlib.sha256(sid.encode()).digest()   # unique per fixture; None = NULL hash
    cur.execute("INSERT Integration.Tableau_Subscription (Subscription_id, Workbook_id, TargetType, TargetId, Format, Subject, "
                "PageSize, Orientation, IsSuspended, ScheduleType, SubscriptionKeyHash, Active_Flag, LastSeenAt, WorkbookUpdatedAt) "
                "VALUES (?, 'T6TEST-WB', 'Workbook', 'T6TEST-TARGET', ?, 'T6TEST subject', 'A4', 'PORTRAIT', ?, 'T6 sched', ?, ?, "
                "DATEADD(minute, -?, SYSDATETIME()), '2020-01-01')",
                sid, fmt, susp, key, active, int(age_h * 60))


def add_backup(cur, sid, taken=1, paused=True, restored=False, repaused_today=False):
    """An open (or restored) Tableau_SubscriptionBackup row for a fixture subscription, as t11_pause.py writes it."""
    cur.execute("INSERT Integration.Tableau_SubscriptionBackup (Subscription_id, Workbook_id, TargetType, TargetId, OwnerUserId, AttachPdf, "
                "AttachImage, TakenOver, PausedAt, RestoredAt, LastRepausedAt, RepauseCount) VALUES "
                "(?, 'T6TEST-WB', 'Workbook', 'T6TEST-TARGET', 'owner', 1, 0, ?, "
                "CASE WHEN ? = 1 THEN SYSDATETIMEOFFSET() END, CASE WHEN ? = 1 THEN SYSDATETIMEOFFSET() END, "
                "CASE WHEN ? = 1 THEN SYSDATETIMEOFFSET() END, ?)", sid, taken, int(paused), int(restored), int(repaused_today),
                1 if repaused_today else 0)


def logrows(cur, sid=None):
    return cur.execute("SELECT Subscription_id, Status, Message FROM Integration.Tableau_SubscriptionSendLog "
                       "WHERE Subscription_id LIKE ? ORDER BY SendLog_id", sid or LIKE).fetchall()


def statuses(cur, sid):
    return [r[1] for r in logrows(cur, sid)]


def mk_ctx(cur, today, adapter=None, mailer=None, gate=None, **cfgkw):
    return m.Ctx(cfg(**cfgkw), adapter or FakeAdapter(), mailer or FakeMailer(), gate or FakeGate(), today)


def run_subs(cur, ctx, ids):
    subs = m.load_subscriptions(cur, set(ids), id_like=LIKE)
    return m.run(cur, ctx, subs)


# ---------- gate (pure) ----------
def mk_gate(**kw):
    g = m.Gate.__new__(m.Gate)
    g.today = dt.date(2026, 10, 22)
    g.wb = {"w": ("Ready", 1)}
    g.sources = {"w": [{"id": "d1", "name": "D1", "status": "Ready", "kind": "Live", "target": None}]}
    g.target_ds = {}
    g.pipes = {"d1": {"P1": True}}
    g.etl = {"P1": (dt.datetime(2026, 10, 22, 10, 0), True)}
    g.refreshed = {}
    for k, v in kw.items():
        setattr(g, k, v)
    return g


def test_gate():
    t = dt.datetime(2026, 10, 22, 10, 0)
    ext = {"w": [{"id": "d1", "name": "D1", "status": "Ready", "kind": "Extract", "target": "T"}]}
    kinds = lambda g: g.check("w")[:2]
    check("gate: live source, pipeline succeeded -> ready", kinds(mk_gate()) == (True, "ready"))
    check("gate: pipeline has no run today -> wait", kinds(mk_gate(etl={})) == (False, "wait"))
    check("gate: latest run of pipeline failed -> blocked", kinds(mk_gate(etl={"P1": (t, False)})) == (False, "blocked"))
    check("gate: unknown pipeline -> blocked", kinds(mk_gate(pipes={"d1": {"P1": False}})) == (False, "blocked"))
    check("gate: mapping NotReady -> blocked", kinds(mk_gate(wb={"w": ("NotReady", 1)})) == (False, "blocked"))
    check("gate: workbook missing from readiness view -> blocked", mk_gate().check("zzz")[:2] == (False, "blocked"))
    check("gate: source list shorter than DataSourceCount -> blocked", kinds(mk_gate(wb={"w": ("Ready", 2)})) == (False, "blocked"))
    check("gate: multi-source, second pipeline missing -> wait (the old script's defect)",
          kinds(mk_gate(wb={"w": ("Ready", 2)}, sources={"w": [
              {"id": "d1", "name": "", "status": "Ready", "kind": "Live", "target": None},
              {"id": "d2", "name": "", "status": "Ready", "kind": "Live", "target": None}]},
              pipes={"d1": {"P1": True}, "d2": {"P2": True}})) == (False, "wait"))
    check("gate: extract never refreshed -> wait", kinds(mk_gate(sources=ext, target_ds={"T": {"d1"}})) == (False, "wait"))
    sub_t = dt.datetime(2026, 10, 22, 10, 5)
    check("gate: extract refreshed AFTER last ETL -> ready",
          kinds(mk_gate(sources=ext, target_ds={"T": {"d1"}}, refreshed={"T": sub_t})) == (True, "ready"))
    check("gate: extract refreshed BEFORE last ETL ended -> wait (stale)",
          kinds(mk_gate(sources=ext, target_ds={"T": {"d1"}}, refreshed={"T": t - dt.timedelta(minutes=5)})) == (False, "wait"))
    check("gate: extract target also fed by another source's pipeline that has not run -> wait",
          kinds(mk_gate(sources=ext, target_ds={"T": {"d1", "d9"}}, pipes={"d1": {"P1": True}, "d9": {"P9": True}},
                        refreshed={"T": sub_t})) == (False, "wait"))
    check("gate: extract without target -> blocked",
          kinds(mk_gate(sources={"w": [{"id": "d1", "name": "", "status": "Ready", "kind": "Extract", "target": None}]})) == (False, "blocked"))
    check("gate: unknown source kind -> blocked",
          kinds(mk_gate(sources={"w": [{"id": "d1", "name": "", "status": "Ready", "kind": "Unknown", "target": None}]})) == (False, "blocked"))
    check("gate: nothing to gate on at all -> blocked",
          kinds(mk_gate(pipes={}, sources={"w": [{"id": "d1", "name": "", "status": "NoReadinessCheck", "kind": "Live", "target": None}]}))
          == (False, "blocked"))
    file_src = {"id": "d2", "name": "", "status": "NotChecked", "kind": "Live", "target": None}
    check("gate (D-053): a workbook with only a NotChecked (file) source is ready, nothing to wait for",
          kinds(mk_gate(pipes={}, etl={}, sources={"w": [file_src]})) == (True, "ready")
          and mk_gate(pipes={}, etl={}, sources={"w": [file_src]}).structural("w") == (True, "ok"))
    two = {"w": [{"id": "d1", "name": "", "status": "Ready", "kind": "Live", "target": None}, file_src]}
    check("gate (D-053): file + DWH source waits on the DWH pipeline only (no run yet -> wait)",
          kinds(mk_gate(wb={"w": ("Ready", 2)}, sources=two, etl={})) == (False, "wait"))
    check("gate (D-053): file + DWH source is ready once the DWH pipeline succeeded",
          kinds(mk_gate(wb={"w": ("Ready", 2)}, sources=two)) == (True, "ready"))
    check("gate (D-053): file + DWH source is blocked when the DWH pipeline failed",
          kinds(mk_gate(wb={"w": ("Ready", 2)}, sources=two, etl={"P1": (t, False)})) == (False, "blocked"))
    check("gate (D-053): a source with no tables and no file flag (NoTables) is still blocked",
          kinds(mk_gate(pipes={}, etl={}, sources={"w": [dict(file_src, status="NoTables")]})) == (False, "blocked"))
    check("gate (D-053): file source + unmapped DWH source (workbook NotReady) is blocked",
          kinds(mk_gate(wb={"w": ("NotReady", 2)}, sources=two)) == (False, "blocked"))
    check("gate: readiness NoReadinessCheck on one source, ETL ok on another -> ready",
          kinds(mk_gate(wb={"w": ("Ready", 2)}, sources={"w": [
              {"id": "d1", "name": "", "status": "Ready", "kind": "Live", "target": None},
              {"id": "d2", "name": "", "status": "NoReadinessCheck", "kind": "Live", "target": None}]})) == (True, "ready"))


# ---------- SMTP classification ----------
class RawServer(threading.Thread):
    """Minimal SMTP server whose behaviour is chosen per test: 'drop' closes the connection after the 354 reply."""

    def __init__(self, mode):
        super().__init__(daemon=True)
        self.mode = mode
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]

    def run(self):
        c, _ = self.sock.accept()
        f = c.makefile("rwb")

        def say(x):
            f.write((x + "\r\n").encode())
            f.flush()
        say("220 test")
        while True:
            line = f.readline().decode().strip()
            u = line.upper()
            if not line:
                break
            if u.startswith("EHLO"):
                say("250 test")
            elif u.startswith("MAIL"):
                say("250 ok")
            elif u.startswith("RCPT"):
                say("550 rejected" if self.mode == "rcpt" else "250 ok")
            elif u == "DATA":
                if self.mode == "data_refused":
                    say("554 no")
                    continue
                say("354 go")
                if self.mode == "drop":
                    c.close()
                    return
                while f.readline().strip() != b".":
                    pass
                say("451 try later" if self.mode == "tempfail" else "250 queued")
            elif u == "QUIT":
                say("221 bye")
                break
        c.close()


def smtp_outcome(mode):
    s = RawServer(mode)
    s.start()
    try:
        m.SmtpMailer("127.0.0.1", s.port, timeout=5).send(b"Subject: x\r\n\r\nbody\r\n.dot line\r\n", "a@localhost", ["b@localhost"])
        return "ok"
    except m.PreDataError:
        return "predata"
    except m.AmbiguousSendError:
        return "ambiguous"


def test_smtp():
    check("smtp: accepted -> ok", smtp_outcome("ok") == "ok")
    check("smtp: RCPT rejected -> before DATA (Failed allowed)", smtp_outcome("rcpt") == "predata")
    check("smtp: DATA command refused (554) -> before content (Failed allowed)", smtp_outcome("data_refused") == "predata")
    check("smtp: connection dropped after 354 -> ambiguous (stays Claimed)", smtp_outcome("drop") == "ambiguous")
    check("smtp: 451 reply after the content -> ambiguous (stays Claimed)", smtp_outcome("tempfail") == "ambiguous")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    try:
        m.SmtpMailer("127.0.0.1", port, timeout=3).send(b"x", "a@localhost", ["b@localhost"])
        r = "ok"
    except m.PreDataError:
        r = "predata"
    except Exception:
        r = "other"
    check("smtp: nothing listening -> before DATA", r == "predata")


# ---------- flow tests ----------
def test_flow(cur, today):
    cap = LogCapture()
    m.log.addHandler(cap)
    m.log.setLevel(logging.INFO)
    try:
        # 1. happy path, layout, rerun
        add_sub(cur, "T6TEST-1")
        mail = FakeMailer()
        ad = FakeAdapter()
        ctx = mk_ctx(cur, today, ad, mail)
        s = run_subs(cur, ctx, ["T6TEST-1"])
        check("happy path: sent once, row Sent with SentAt", s["kinds"]["sent"] == 1 and len(mail.sent) == 1
              and statuses(cur, "T6TEST-1") == ["Sent"] and cur.execute(
                  "SELECT COUNT(*) FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id='T6TEST-1' "
                  "AND SentAt IS NOT NULL").fetchone()[0] == 1)
        import email as _email
        em = _email.message_from_bytes(mail.sent[0][0], policy=_email.policy.default)
        import html as _html
        body = _html.unescape(em.get_body(("html",)).get_content())
        parts = list(em.walk())
        cids = [p["Content-ID"].strip("<>") for p in parts if p["Content-ID"]]
        check("layout: H1, one inline cid image resolving, PDF attached, subscription line, sender display name",
              "<h1>WB name</h1>" in body and len(cids) == 1 and "cid:" + cids[0] in body
              and any(p.get_content_type() == "application/pdf" and p.get_filename() == "WB name.pdf" for p in parts)
              and "Here's your subscription to the workbook WB name." in body and "tableau-reports@example.com" in em["From"]
              and "Subscription schedule" not in body, str(cids))
        check("recipient: envelope is the address from Tableau only in SES mode; local mode uses the localhost sink",
              mail.sent[0][2] == [m.LOCAL_TO])
        s = run_subs(cur, mk_ctx(cur, today, ad, mail), ["T6TEST-1"])
        check("rerun same day: already sent, no second mail", s["kinds"]["already_sent"] == 1 and len(mail.sent) == 1)

        # 2. concurrency: two runs, same subscription, one send
        add_sub(cur, "T6TEST-2")
        mail = FakeMailer(delay=1.0)
        res = []

        def worker():
            conn = m.t4.connect()
            c = conn.cursor()
            ctx = mk_ctx(c, today, FakeAdapter(), mail)
            ctx.prior = {}
            sub = m.load_subscriptions(c, {"T6TEST-2"}, id_like=LIKE)[0]
            res.append(m.process(c, sub, ctx)[0])
        ts = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check("concurrency: one send, the other run loses the claim", sorted(res) == ["claim_lost", "sent"] and len(mail.sent) == 1
              and statuses(cur, "T6TEST-2") == ["Sent"], str(res))
        # app lock
        c1 = m.t4.connect()
        c2 = m.t4.connect()
        q = ("DECLARE @r int; EXEC @r = sp_getapplock @Resource = 'T6TEST_lock', @LockMode = 'Exclusive', "
             "@LockOwner = 'Session', @LockTimeout = 0; SELECT @r")
        r1 = c1.cursor().execute(q).fetchone()[0]
        r2 = c2.cursor().execute(q).fetchone()[0]
        check("app lock: second run cannot take the lock", r1 >= 0 and r2 < 0, "%s %s" % (r1, r2))
        c1.close()
        c2.close()

        # 3. ambiguous error leaves Claimed
        add_sub(cur, "T6TEST-3")
        mail = FakeMailer("ambiguous")
        ctx = mk_ctx(cur, today, FakeAdapter(), mail)
        s = run_subs(cur, ctx, ["T6TEST-3"])
        check("ambiguous send error: row stays Claimed, event raised", statuses(cur, "T6TEST-3") == ["Claimed"]
              and s["kinds"]["ambiguous"] == 1 and any(e["code"] == "ambiguous" for e in ctx.events))
        mail2 = FakeMailer()
        s = run_subs(cur, mk_ctx(cur, today, FakeAdapter(), mail2), ["T6TEST-3"])
        check("ambiguous: next run does NOT resend (slot still claimed)", s["kinds"]["in_progress"] == 1 and mail2.sent == []
              and statuses(cur, "T6TEST-3") == ["Claimed"])

        # 4. pre-DATA error frees the slot
        add_sub(cur, "T6TEST-4")
        s = run_subs(cur, mk_ctx(cur, today, FakeAdapter(), FakeMailer("predata")), ["T6TEST-4"])
        check("pre-DATA error: row Failed (slot free)", statuses(cur, "T6TEST-4") == ["Failed"] and s["kinds"]["send_failed"] == 1)
        mail = FakeMailer()
        s = run_subs(cur, mk_ctx(cur, today, FakeAdapter(), mail), ["T6TEST-4"])
        check("pre-DATA error: next run retries and sends", statuses(cur, "T6TEST-4") == ["Failed", "Sent"] and len(mail.sent) == 1)

        # 5. export failure: retry up to the cap, then give up with an event
        add_sub(cur, "T6TEST-5")
        ad = FakeAdapter(export_error="boom")
        mail = FakeMailer()
        for _ in range(3):
            ctx = mk_ctx(cur, today, ad, mail)
            run_subs(cur, ctx, ["T6TEST-5"])
        check("export failure: three Failed rows, nothing sent, alert event on the third",
              statuses(cur, "T6TEST-5") == ["Failed"] * 3 and reports(mail) == [] and any(e["code"] == "export_failed" for e in ctx.events))
        ctx = mk_ctx(cur, today, ad, mail)
        s = run_subs(cur, ctx, ["T6TEST-5"])
        check("export failure: after the cap no more attempts", s["kinds"]["gave_up"] == 1 and ad.exports == 3)

        # 6. stale claim -> TimedOut (execute only) and it still blocks
        add_sub(cur, "T6TEST-6")
        add_sub(cur, "T6TEST-6B")
        cur.execute("INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status, ClaimedAt) VALUES "
                    "('T6TEST-6', 'Claimed', DATEADD(minute, -3, SYSDATETIMEOFFSET())), "
                    "('T6TEST-6B', 'Claimed', SYSDATETIMEOFFSET())")
        # claim timeout is set to 1 minute here (the real one is 90): a 3 min old claim is stale, a fresh one is not;
        # minutes rather than 100 min ago so the claim cannot fall on yesterday's Singapore date near midnight
        mail = FakeMailer()
        ctx = mk_ctx(cur, today, FakeAdapter(), mail, execute=False, claim_timeout_min=1)
        s = run_subs(cur, ctx, ["T6TEST-6", "T6TEST-6B"])
        check("dry run: stale claim reported but not changed", statuses(cur, "T6TEST-6") == ["Claimed"] and len(s["timed_out_now"]) == 1)
        ctx = mk_ctx(cur, today, FakeAdapter(), mail, claim_timeout_min=1)
        s = run_subs(cur, ctx, ["T6TEST-6", "T6TEST-6B"])
        check("TimedOut: a stale claim becomes TimedOut and is reported; a fresh claim is untouched",
              statuses(cur, "T6TEST-6") == ["TimedOut"] and statuses(cur, "T6TEST-6B") == ["Claimed"]
              and [x[1] for x in s["timed_out_now"]] == ["T6TEST-6"] and any(e["code"] == "timed_out" for e in ctx.events))
        check("TimedOut: slot stays blocked, nothing sent", reports(mail) == [] and s["kinds"]["in_progress"] == 1)
        cur.execute("UPDATE Integration.Tableau_SubscriptionSendLog SET Status = 'Released' WHERE Subscription_id = 'T6TEST-6'")
        s = run_subs(cur, mk_ctx(cur, today, FakeAdapter(), mail, claim_timeout_min=1), ["T6TEST-6"])
        check("TimedOut released by a human: the next run sends", statuses(cur, "T6TEST-6") == ["Released", "Sent"] and len(reports(mail)) == 1)

        # 7. D-046: suspended is no special category (loaded and sendable); inactive (deleted in Tableau) is not a candidate
        add_sub(cur, "T6TEST-7S", susp=1)
        add_sub(cur, "T6TEST-7I", active=0)
        check("a suspended subscription is loaded (D-046), an inactive one is not",
              [x.id for x in m.load_subscriptions(cur, {"T6TEST-7S", "T6TEST-7I"}, id_like=LIKE)] == ["T6TEST-7S"])
        add_sub(cur, "T6TEST-7L")
        mail = FakeMailer()
        s = run_subs(cur, mk_ctx(cur, today, FakeAdapter(target_id="other"), mail), ["T6TEST-7L"])
        check("target changed since last sync: skipped, no claim", s["kinds"]["definition_changed"] == 1 and logrows(cur, "T6TEST-7L") == [])
        s = run_subs(cur, mk_ctx(cur, today, FakeAdapter(suspended=True), mail), ["T6TEST-7L"])
        check("suspended on Tableau is not a skip reason (D-046): the adapter reported it suspended and it was sent, exactly one log row 'Sent'",
              s["kinds"]["sent"] == 1 and sum(s["kinds"].values()) == 1 and statuses(cur, "T6TEST-7L") == ["Sent"] and len(reports(mail)) == 1)

        # 8. not ready: skipped without claim; alert only after the cutoff, once per day
        add_sub(cur, "T6TEST-8")
        mail = FakeMailer()
        ctx = mk_ctx(cur, today, FakeAdapter(), mail, cutoff=m.dt.time(23, 59, 59))
        ctx.gate = FakeGate((False, "wait", "extract not refreshed"))
        s = run_subs(cur, ctx, ["T6TEST-8"])
        check("not ready: no claim, no mail, no alert before the cutoff", s["kinds"]["not_ready"] == 1 and logrows(cur, "T6TEST-8") == []
              and mail.sent == [] and s["alerts"] == 0)
        ctx = mk_ctx(cur, today, FakeAdapter(), mail, cutoff=m.dt.time(0, 0))
        ctx.gate = FakeGate((False, "wait", "extract not refreshed"))
        s = run_subs(cur, ctx, ["T6TEST-8"])
        check("not ready after the cutoff: one alert mail to the team, still no report mail",
              s["alerts"] == 1 and len(mail.sent) == 1 and b"still not ready after the cutoff" in mail.sent[0][0].replace(b"\r\n", b" ")
              and logrows(cur, "T6TEST-8") == [])
        ctx = mk_ctx(cur, today, FakeAdapter(), mail, cutoff=m.dt.time(0, 0))
        ctx.gate = FakeGate((False, "wait", "extract not refreshed"))
        s = run_subs(cur, ctx, ["T6TEST-8"])
        check("alert is sent once per subscription and reason per day", s["alerts"] == 0 and len(mail.sent) == 1)

        # 9. allow-list (SES mode semantics, fake mailer so nothing leaves)
        add_sub(cur, "T6TEST-9")
        mail = FakeMailer()
        ctx = mk_ctx(cur, today, FakeAdapter(emails=["ok.one@example.com", "dropme@example.com"]), mail, ses=True,
                     allow=m.parse_allow("OK.One@example.com"))
        s = run_subs(cur, ctx, ["T6TEST-9"])
        raw = mail.sent[0][0] if mail.sent else b""
        check("allow-list: only the allowed recipient is delivered, the other is dropped; SES-mode final status is Sent",
              len(mail.sent) == 1 and mail.sent[0][2] == ["ok.one@example.com"] and b"dropme@example.com" not in raw
              and statuses(cur, "T6TEST-9") == ["Sent"])
        add_sub(cur, "T6TEST-9B")
        mail = FakeMailer()
        s = run_subs(cur, mk_ctx(cur, today, FakeAdapter(emails=["dropme@example.com"]), mail, ses=True, allow=m.parse_allow("x@example.com")),
                     ["T6TEST-9B"])
        check("allow-list: nobody allowed -> no claim, no mail", s["kinds"]["no_allowed_recipient"] == 1 and logrows(cur, "T6TEST-9B") == []
              and mail.sent == [])
        s = run_subs(cur, mk_ctx(cur, today, FakeAdapter(emails=["dropme@example.com"]), mail, ses=True, allow=frozenset()), ["T6TEST-9B"])
        check("allow-list: empty list -> nothing is sent", mail.sent == [] and logrows(cur, "T6TEST-9B") == [])
        # 9b. SEND_ALLOW_ALL_UAT (D-050): allow_all with an empty list delivers to every subscriber address (fake mailer, nothing leaves)
        add_sub(cur, "T6TEST-9C")
        mail = FakeMailer()
        ctx = mk_ctx(cur, today, FakeAdapter(emails=["one@example.com", "Two@example.com"]), mail, ses=True, allow=frozenset(), allow_all=True)
        s = run_subs(cur, ctx, ["T6TEST-9C"])
        check("allow-all (UAT switch): empty list, every subscriber address is delivered",
              len(mail.sent) == 1 and sorted(mail.sent[0][2]) == ["Two@example.com", "one@example.com"] and statuses(cur, "T6TEST-9C") == ["Sent"]
              and s["kinds"]["no_allowed_recipient"] == 0)
        check("allow-all: the RUN SUMMARY line carries the marker; without the switch it does not",
              "allow_all_uat=1" in m.summary_line(ctx, s) and "allow_all_uat" not in m.summary_line(mk_ctx(cur, today, ses=True), s))
        # 10. dry run writes nothing
        add_sub(cur, "T6TEST-10")
        before = others_checksum(cur)
        mail = FakeMailer()
        ad = FakeAdapter()
        s = run_subs(cur, mk_ctx(cur, today, ad, mail, execute=False), ["T6TEST-10"])
        check("dry run: would_send, no log row, no mail, no export", s["kinds"]["would_send"] == 1 and logrows(cur, "T6TEST-10") == []
              and mail.sent == [] and ad.exports == 0)
        s = run_subs(cur, mk_ctx(cur, today, ad, mail, execute=False, export_in_dry_run=True), ["T6TEST-10"])
        check("dry run with --export: exports and sizes, still no log row and no mail", ad.exports == 1 and logrows(cur, "T6TEST-10") == []
              and mail.sent == [])
        check("dry run: other rows unchanged", others_checksum(cur) == before)

        # 11. oversize
        add_sub(cur, "T6TEST-11")
        mail = FakeMailer()
        ad = FakeAdapter()
        ctx = mk_ctx(cur, today, ad, mail, size_limit=1000)
        s = run_subs(cur, ctx, ["T6TEST-11"])
        rows = logrows(cur, "T6TEST-11")
        check("oversize: refused, row Failed OVERSIZE, alert event, no report mail, one alert mail", reports(mail) == [] and len(mail.sent) == 1 and s["kinds"]["oversize"] == 1
              and len(rows) == 1 and rows[0][1] == "Failed" and rows[0][2].startswith("OVERSIZE:")
              and any(e["code"] == "oversize" for e in ctx.events) and s["alerts"] == 1)
        s = run_subs(cur, mk_ctx(cur, today, ad, mail, size_limit=1000), ["T6TEST-11"])
        check("oversize: not retried on the next run (no second export)", ad.exports == 1 and s["kinds"]["oversize"] == 1)

        # 12. near-duplicate subscription
        key = bytes(range(32))
        add_sub(cur, "T6TEST-12OLD", active=0, key=key)
        add_sub(cur, "T6TEST-12NEW", key=key)
        add_sub(cur, "T6TEST-12ACT", key=key)
        mail = FakeMailer()
        run_subs(cur, mk_ctx(cur, today, FakeAdapter(), mail), ["T6TEST-12ACT"])  # no inactive sibling log row yet: sends
        cur.execute("INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status, SentAt) VALUES ('T6TEST-12OLD', 'Sent', SYSDATETIMEOFFSET())")
        ctx = mk_ctx(cur, today, FakeAdapter(), mail)
        s = run_subs(cur, ctx, ["T6TEST-12NEW"])
        check("near-duplicate: a replaced predecessor already sent today -> not sent, alert event",
              s["kinds"]["duplicate_key"] == 1 and statuses(cur, "T6TEST-12NEW") == [] and any(e["code"] == "duplicate_key" for e in ctx.events))
        check("two ACTIVE subscriptions with the same key (legit multi-schedule) both send", len(reports(mail)) == 1 and statuses(cur, "T6TEST-12ACT") == ["Sent"])

        # 13. no recipient
        add_sub(cur, "T6TEST-13")
        ctx = mk_ctx(cur, today, FakeAdapter(emails=[]), FakeMailer())
        s = run_subs(cur, ctx, ["T6TEST-13"])
        check("no recipient on Tableau: no claim, alert event", s["kinds"]["no_recipient"] == 1 and logrows(cur, "T6TEST-13") == []
              and any(e["code"] == "no_recipient" for e in ctx.events))

        # 14. wide crosstab: unreadable images are warned about and summarised
        add_sub(cur, "T6TEST-14")
        thin = {"name": "Crosstab", "data": png(3200, 99), "orig_w": 32668, "orig_h": 1012, "w": 3200, "h": 99, "capped": True, "unreadable": True}
        ctx = mk_ctx(cur, today, FakeAdapter(images=[thin]), FakeMailer())
        s = run_subs(cur, ctx, ["T6TEST-14"])
        check("unreadable image: WARNING logged, listed in the summary line, mail still sent",
              any("UNREADABLE IMAGE" in x and "Crosstab" in x for x in cap.lines) and "unreadable_images=1" in m.summary_line(ctx, s)
              and s["kinds"]["sent"] == 1)

        # 15. local transport ends as Released so a test delivery does not use up the real slot
        add_sub(cur, "T6TEST-15")
        mail = FakeMailer()
        run_subs(cur, mk_ctx(cur, today, FakeAdapter(), mail, local_release=True), ["T6TEST-15"])
        check("local transport: delivered mail ends as Released (not Sent)", statuses(cur, "T6TEST-15") == ["Released"] and len(mail.sent) == 1)

        # 16. recipients never reach the log or the DWH
        text = "\n".join(cap.lines)
        dwh = " ".join(str(r[2]) for r in logrows(cur))
        check("no recipient address in the log output or in send log messages",
              SECRET_ADDR not in text and "dropme@example.com" not in text and "ok.one@example.com" not in text
              and SECRET_ADDR not in dwh and "example.com" not in dwh)
        check("recipients are logged as a count and a hash", "recipients=1 hash=" in text)
    finally:
        m.log.removeHandler(cap)



class RecSMTP:
    """Stands in for smtplib.SMTP: records starttls/close calls; fails at the stage given."""
    instances = []
    fail_at = None

    def __init__(self, host, port, timeout=None):
        self.closed, self.ctx = False, None
        RecSMTP.instances.append(self)

    def ehlo(self):
        return 250, b""

    def starttls(self, context=None):
        self.ctx = context
        if RecSMTP.fail_at == "starttls":
            raise smtplib.SMTPException("tls failed")
        return 220, b""

    def login(self, u, p):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    def close(self):
        self.closed = True


def test_takeover_rule(cur, today):
    """D-035: t6 sends a subscription only if t11 took it over (open backup row, PausedAt set) AND Tableau shows it suspended."""
    def go(sid, adapter, mail=None, **kw):
        mail = mail or FakeMailer()
        ctx = mk_ctx(cur, today, adapter, mail, include_untaken=False, **kw)
        return run_subs(cur, ctx, [sid]), mail, ctx

    # D-046: a subscription suspended in Tableau is a normal candidate; without a takeover row it is simply not ours yet
    add_sub(cur, "T6TEST-TK1", susp=1)
    check("suspended subscription is loaded (D-046)", [x.id for x in m.load_subscriptions(cur, {"T6TEST-TK1"}, id_like=LIKE)] == ["T6TEST-TK1"])
    s, mail, _ = go("T6TEST-TK1", FakeAdapter(suspended=True))
    check("suspended but not taken over: not sent by t6 until t11 has taken it over, no claim",
          s["kinds"]["not_taken_over"] == 1 and mail.sent == [] and logrows(cur, "T6TEST-TK1") == [])
    # not suspended, not taken over: left to Tableau
    add_sub(cur, "T6TEST-TK2")
    s, mail, _ = go("T6TEST-TK2", FakeAdapter(suspended=False))
    check("not suspended and not taken over: not sent by t6 (Tableau sends it), no claim",
          s["kinds"]["not_taken_over"] == 1 and mail.sent == [] and logrows(cur, "T6TEST-TK2") == [])
    # taken over + suspended in Tableau (the T2 snapshot may say either): SENT
    for sid, snap in (("T6TEST-TK3", 1), ("T6TEST-TK3B", 0)):
        add_sub(cur, sid, susp=snap)
        add_backup(cur, sid)
        s, mail, _ = go(sid, FakeAdapter(suspended=True))
        check("taken over and suspended in Tableau (T2 snapshot IsSuspended=%d): sent once" % snap,
              s["kinds"]["sent"] == 1 and len(mail.sent) == 1 and statuses(cur, sid) == ["Sent"])
        s, mail, _ = go(sid, FakeAdapter(suspended=True))
        check("taken over: second run the same day sends nothing", s["kinds"]["already_sent"] == 1 and mail.sent == [])
    # restored: not sent (even though Tableau may show it suspended or not)
    add_sub(cur, "T6TEST-TK4", susp=1)
    add_backup(cur, "T6TEST-TK4", restored=True)
    s, mail, _ = go("T6TEST-TK4", FakeAdapter(suspended=True))
    check("restored subscription (suspended snapshot): not sent", s["kinds"]["not_taken_over"] == 1 and reports(mail) == [] and logrows(cur, "T6TEST-TK4") == [])
    add_sub(cur, "T6TEST-TK4B")
    add_backup(cur, "T6TEST-TK4B", restored=True)
    s, mail, _ = go("T6TEST-TK4B", FakeAdapter(suspended=False))
    check("restored subscription (resumed): treated as not taken over, not sent by t6",
          s["kinds"]["not_taken_over"] == 1 and reports(mail) == [] and logrows(cur, "T6TEST-TK4B") == [])
    # resumed by a human, t11 has not run yet: NOT sent (Tableau may send it itself), alert
    add_sub(cur, "T6TEST-TK5", susp=1)
    add_backup(cur, "T6TEST-TK5")
    s, mail, ctx = go("T6TEST-TK5", FakeAdapter(suspended=False))
    check("taken over but resumed in Tableau: not sent, no claim, alert event", s["kinds"]["not_paused"] == 1 and reports(mail) == []
          and logrows(cur, "T6TEST-TK5") == [] and any(e["code"] == "not_paused" for e in ctx.events))
    # re-paused by t11 today: not sent today (the old subscription may already have mailed)
    add_sub(cur, "T6TEST-TK6", susp=1)
    add_backup(cur, "T6TEST-TK6", repaused_today=True)
    s, mail, ctx = go("T6TEST-TK6", FakeAdapter(suspended=True))
    check("re-paused today: not sent today, no claim, alert event", s["kinds"]["repaused_today"] == 1 and reports(mail) == []
          and logrows(cur, "T6TEST-TK6") == [] and any(e["code"] == "repaused_today" for e in ctx.events))
    # pause not confirmed by t11 yet
    add_sub(cur, "T6TEST-TK7")
    add_backup(cur, "T6TEST-TK7", paused=False)
    s, mail, _ = go("T6TEST-TK7", FakeAdapter(suspended=True))
    check("taken over but PausedAt not confirmed: not sent", s["kinds"]["takeover_unconfirmed"] == 1 and mail.sent == [])
    # deleted on Tableau: 404 -> dropped, not an error
    add_sub(cur, "T6TEST-TK8", susp=1)
    add_backup(cur, "T6TEST-TK8")

    class Gone:
        def describe(self, sub):
            raise type("NotFound", (Exception,), {"code": "404021"})()
    s, mail, _ = go("T6TEST-TK8", Gone())
    check("deleted on Tableau (404): skipped as deleted_on_tableau, nothing sent, not counted as a failure",
          s["kinds"]["deleted_on_tableau"] == 1 and mail.sent == [] and m.exit_code(None, s) == 0)
    add_sub(cur, "T6TEST-TK9", active=0, susp=1)
    add_backup(cur, "T6TEST-TK9")
    check("deleted subscription after the T2 sync (Active_Flag 0): not loaded, not sent",
          m.load_subscriptions(cur, {"T6TEST-TK9"}, id_like=LIKE) == [])
    # cutover guard (F-11): a new native subscription on a taken-over workbook is alerted and not sent
    add_sub(cur, "T6TEST-TKA", susp=1)
    add_backup(cur, "T6TEST-TKA")
    add_sub(cur, "T6TEST-TKB")      # active, not suspended, not taken over, same workbook (T6TEST-WB)
    mail = FakeMailer()
    ctx = mk_ctx(cur, today, FakeAdapter(suspended=True), mail, include_untaken=False)
    s = run_subs(cur, ctx, ["T6TEST-TKA", "T6TEST-TKB"])
    check("new native subscription on a taken-over workbook: not sent, alert event; the taken-over one is sent",
          s["kinds"]["not_taken_over"] == 1 and s["kinds"]["sent"] == 1 and logrows(cur, "T6TEST-TKB") == []
          and any(e["code"] == "new_native" and e["sub"] == "T6TEST-TKB" for e in ctx.events) and len(reports(mail)) == 1)
    # the include flag never reaches real sending
    ses_full = {"SEND_ALLOWED_RECIPIENTS": "a@example.com", "SES_SMTP_USER": "u", "SES_SMTP_PASSWORD": "p"}
    try:
        m.build_config(ns(execute=True, ses=True, include_not_taken_over=True), ses_full)
        r = "accepted"
    except m.ConfigError:
        r = "refused"
    check("--include-not-taken-over is refused together with --ses", r == "refused")
    check("--include-not-taken-over is off by default", m.build_config(ns(), {}).include_untaken is False)


def test_smtp_tls():
    saved = m.smtplib.SMTP
    m.smtplib.SMTP = RecSMTP
    try:
        try:
            m.SmtpMailer("smtp.example.invalid", 587, "u", "p", starttls=True).send(b"x", "a@b.c", ["d@e.f"])
            r = "ok"
        except m.PreDataError:
            r = "predata"
        s = RecSMTP.instances[-1]
        check("starttls uses a verifying context (certificate + host name) before login",
              s.ctx is not None and s.ctx.verify_mode == m.ssl.CERT_REQUIRED and s.ctx.check_hostname)
        check("auth failure is a pre-DATA error and the socket is closed", r == "predata" and s.closed)
        RecSMTP.fail_at = "starttls"
        try:
            m.SmtpMailer("smtp.example.invalid", 587, "u", "p", starttls=True).send(b"x", "a@b.c", ["d@e.f"])
        except m.PreDataError:
            pass
        check("TLS failure: pre-DATA error and socket closed", RecSMTP.instances[-1].closed)
    finally:
        m.smtplib.SMTP = saved
        RecSMTP.fail_at = None


def ns(**kw):
    base = dict(execute=False, ses=False, local_smtp=False, as_of_date=None, export=False, size_limit_mb=25.0, cutoff="15:30",
                max_mapping_age_h=26.0)
    base.update(kw)
    return NS(**base)


def test_config():
    def err(a, env):
        try:
            m.build_config(a, env)
            return None
        except m.ConfigError as e:
            return str(e)
    full = {"SEND_ALLOWED_RECIPIENTS": "a@example.com", "SES_SMTP_USER": "u", "SES_SMTP_PASSWORD": "p"}
    check("config: --ses with an empty allow-list is refused", "Refusing to send for real" in (err(ns(execute=True, ses=True), {}) or ""))
    check("config: --ses without credentials is refused", err(ns(execute=True, ses=True), {"SEND_ALLOWED_RECIPIENTS": "a@example.com"}) is not None)
    check("config: --ses with allow-list and credentials is accepted", err(ns(execute=True, ses=True), full) is None)
    check("config: --ses without --execute is refused", err(ns(ses=True), full) is not None)
    check("config: --ses and --local-smtp together are refused", err(ns(execute=True, ses=True, local_smtp=True), full) is not None)
    check("config: --as-of-date with --execute is refused", err(ns(execute=True, as_of_date="2026-10-14"), {}) is not None)
    c = m.build_config(ns(execute=True), full)
    check("config: without --ses the transport stays local whatever the environment holds", c.ses is False)
    check("config: size limit and mapping age come from the arguments", m.build_config(ns(size_limit_mb=1.0, max_mapping_age_h=5.0), {}).size_limit == 1048576
          and m.build_config(ns(max_mapping_age_h=5.0), {}).max_mapping_age_h == 5.0)
    mailer = m.make_mailer(c, env=full)
    check("config: local mailer targets localhost, no credential, no STARTTLS", mailer.host == "127.0.0.1" and mailer.user is None and not mailer.starttls)
    check("pdf options: maxage=1 is passed", sm.pdf_options("A4", "LANDSCAPE").get_query_params().get("maxAge") == 1)


def test_allow_all_uat():
    """D-050: the switch is effective only on the exact UAT host; every case builds a Config from a plain dict (no network, no mail)."""
    UAT = "http://tableau-uat.example.com:8000/"
    creds = {"SMTP_USER": "u", "SMTP_PASSWORD": "p", "MAIL_TRANSPORT": "smtp", "UAT_TABLEAU_HOST": "tableau-uat.example.com", "OWNER_EMAIL": "you@example.com"}

    def build(env, execute=True):
        try:
            return m.build_config(ns(execute=execute), dict(creds, **env)), None
        except m.ConfigError as e:
            return None, str(e)
    on = {"SEND_ALLOW_ALL_UAT": "1", "TABLEAU_SERVER_URL": UAT, "UAT_TABLEAU_HOST": "tableau-uat.example.com"}
    c, e = build(on)
    check("allow-all: switch on + UAT host + empty list is accepted for real sending", c is not None and c.ses and c.allow_all and not c.allow)
    c, e = build(dict(on, TABLEAU_SERVER_URL="HTTP://tableau-uat.example.com/"))
    check("allow-all: host compare is case-insensitive and ignores scheme and port", c is not None and c.allow_all)
    c, e = build(dict(on, SEND_ALLOWED_RECIPIENTS="a@example.com"))
    check("allow-all: switch on beats a non-empty list (allow everybody)", c is not None and c.allow_all and m.filter_allowed(c, ["b@example.com"]) == (["b@example.com"], 0))
    c, e = build(dict(on, SEND_ALLOW_ALL_UAT="0"))
    check("allow-all: switch off keeps the old rule (empty list refused)", c is None and "Refusing to send for real" in e)
    c, e = build({"TABLEAU_SERVER_URL": UAT})
    check("allow-all: switch missing keeps the old rule (empty list refused)", c is None and "Refusing to send for real" in e)
    c, e = build({"TABLEAU_SERVER_URL": UAT, "SEND_ALLOWED_RECIPIENTS": "a@example.com"})
    check("allow-all: switch off with a list still filters", c is not None and not c.allow_all and m.filter_allowed(c, ["a@example.com", "b@example.com"]) == (["a@example.com"], 1))
    for why, url in (("another host", "https://tableau.prod.example.com/"), ("no URL at all", ""),
                     ("suffix look-alike", "http://tableau-uat.example.com.evil.example/"),
                     ("prefix look-alike", "http://evil-tableau-uat.example.com/"),
                     ("userinfo trick", "http://tableau-uat.example.com@evil.example/"),
                     ("userinfo trick with port", "http://tableau-uat.example.com:8000@evil.example:8000/"),
                     ("host in the path", "http://evil.example/tableau-uat.example.com"),
                     ("host in the query", "http://evil.example/?h=tableau-uat.example.com"),
                     ("host in the fragment", "http://evil.example/#tableau-uat.example.com"),
                     ("backslash trick", "http://evil.example\\@tableau-uat.example.com/"),
                     ("trailing dot", "http://tableau-uat.example.com./"),
                     ("no scheme", "tableau-uat.example.com:8000"),
                     ("whitespace", "http://evil.example/ tableau-uat.example.com")):
        c, e = build(dict(on, TABLEAU_SERVER_URL=url))
        check("allow-all: %s is NOT the UAT host, so the switch is ignored (empty list refused)" % why, c is None and "Refusing to send for real" in e)
        c, e = build(dict(on, TABLEAU_SERVER_URL=url, SEND_ALLOWED_RECIPIENTS="a@example.com"))
        check("allow-all: %s keeps the list in force" % why, c is not None and not c.allow_all and m.filter_allowed(c, ["b@example.com"]) == ([], 1))
    c, e = build(dict(on, ALERT_TO="not an address"))
    check("allow-all: ALERT_TO must still be a valid address", c is None and "ALERT_TO" in e)
    c, e = build(dict(on, ALERT_TO="team@example.com"))
    check("allow-all: ALERT_TO need not be on a list", c is not None and m.filter_allowed(c, [c.alert_to]) == (["team@example.com"], 0))
    check("allow-all: surrounding whitespace or a newline is stripped, never passed on",
          m.filter_allowed(c, ["x@y.com\n", "x@y.com ", " z@y.com"]) == (["x@y.com", "x@y.com", "z@y.com"], 0))
    check("allow-all: malformed subscriber addresses are dropped, not passed into SMTP commands",
          m.filter_allowed(c, ["ok@example.com", "bad@x.com>\r\nRCPT TO:<evil@x.com", "nobody"]) == (["ok@example.com"], 2))
    c, e = build(dict(on, SMTP_USER=""))
    check("allow-all: credentials are still required", c is None)
    c, e = build(on, execute=False)
    check("allow-all: a dry run never sends (local transport, no real mail path)", c is not None and not c.ses)
    c, e = build({k: v for k, v in on.items()} | {"MAIL_TRANSPORT": "local"})
    check("allow-all: MAIL_TRANSPORT=local stays local", c is not None and not c.ses and m.filter_allowed(c, ["x@y.com"]) == ([m.LOCAL_TO], 0))
    # startup warnings (log text only)
    cap = LogCapture()
    m.ops.log.addHandler(cap)
    m.ops.log.setLevel(logging.INFO)
    try:
        m.ops.log_transport("t6", True, True, dict(on))
        w1 = list(cap.lines)
        cap.lines.clear()
        m.ops.log_transport("t6", True, True, dict(on, TABLEAU_SERVER_URL="https://prod.example.com/"))
        w2 = list(cap.lines)
        cap.lines.clear()
        m.ops.log_transport("t6", True, True, {"TABLEAU_SERVER_URL": UAT})
        w3 = list(cap.lines)
    finally:
        m.ops.log.removeHandler(cap)
    check("allow-all: startup WARNING when effective", any("SEND_ALLOW_ALL_UAT=1 is ON" in l for l in w1))
    check("allow-all: startup WARNING when set but ignored on another host", any("IGNORED" in l for l in w2) and not any("is ON" in l for l in w2))
    check("allow-all: no allow-all warning when the switch is not set", not any("SEND_ALLOW_ALL_UAT" in l for l in w3))
    check("allow-all: RUN SUMMARY marker only when effective", m.ops.allow_all_marker(on) == " allow_all_uat=1"
          and m.ops.allow_all_marker(dict(on, TABLEAU_SERVER_URL="http://x.example/")) == "" and m.ops.allow_all_marker({}) == "")
    for v in ("true", "YES", " on "):
        check("allow-all: switch value %r counts as on" % v, m.ops.allow_all_uat(dict(on, SEND_ALLOW_ALL_UAT=v))[1])
    check("allow-all: a junk switch value is off", not any(m.ops.allow_all_uat(dict(on, SEND_ALLOW_ALL_UAT=v))[0] for v in ("2", "enabled", "")))
    # preflight recipient with an empty list
    c, e = build(on)
    check("allow-all: the SMTP preflight recipient is ALERT_TO when the list is empty", m.preflight_rcpt(c) == c.alert_to)


def test_log_retention():
    """D-051: one log file per day, old files of this script's own prefix are pruned, nothing else is touched, never raises."""
    d = Path(tempfile.mkdtemp(prefix="logret_"))
    env = {"LOG_DIR": str(d)}
    today = dt.date(2026, 10, 26)
    keep, gone = [], []
    for name in ("t6_send_2026-10-26.log", "t6_send_2026-10-19.log", "t6_send_2026-10-20.log"):   # 7 days old is still kept
        (d / name).write_text("x")
        keep.append(name)
    for name in ("t6_send_2026-10-18.log", "t6_send_2025-01-01.log"):
        (d / name).write_text("x")
        gone.append(name)
    others = ["t4_refresh_2020-01-01.log", "t6_send_2020-01-01.txt", "t6_send_2020-01-01.log.bak", "t6_sendX_2020-01-01.log", "xt6_send_2020-01-01.log",
              "t6_alert_state.json", "t11_backup.json", "KILL", "t6_send_notadate.log", "t6_send_2020-13-45.log"]
    for name in others:
        (d / name).write_text("x")
    (d / "lastrun").mkdir()
    (d / "lastrun" / "t6.json").write_text("{}")
    old_legacy, new_legacy = d / "t6_send.log.2", d / "t6_send.log"
    old_legacy.write_text("x")
    new_legacy.write_text("x")
    os.utime(old_legacy, (1.0, 1.0))
    n = m.ops.prune_logs("t6_send", env, today)
    left = sorted(p.name for p in d.iterdir() if p.is_file())
    check("log retention: files older than 7 days of this script are deleted, younger ones kept",
          all(not (d / g).exists() for g in gone) and all((d / k).exists() for k in keep) and not old_legacy.exists() and new_legacy.exists())
    check("log retention: other scripts' logs, other extensions, look-alike names, state files, markers and folders are untouched",
          all((d / o).exists() for o in others) and (d / "lastrun" / "t6.json").exists() and n == 3, "%d deleted, left %s" % (n, left))
    check("log retention: LOG_RETENTION_DAYS changes the window, junk falls back to 7",
          m.ops.log_retention_days({"LOG_RETENTION_DAYS": "3"}) == 3 and m.ops.log_retention_days({"LOG_RETENTION_DAYS": "x"}) == 7
          and m.ops.log_retention_days({}) == 7 and m.ops.log_retention_days({"LOG_RETENTION_DAYS": "0"}) == 1)
    (d / "t6_send_2026-10-24.log").write_text("x")
    m.ops.prune_logs("t6_send", dict(env, LOG_RETENTION_DAYS="1"), today)
    check("log retention: a shorter window prunes more", not (d / "t6_send_2026-10-24.log").exists() and (d / "t6_send_2026-10-26.log").exists())
    # a file that cannot be deleted (held open / permission) and a missing directory must not raise
    (d / "t6_send_2024-01-01.log").write_text("x")
    saved = Path.unlink

    def deny(self, *a, **kw):
        raise PermissionError("in use")
    Path.unlink = deny
    try:
        n = m.ops.prune_logs("t6_send", env, today)
        r = "ok"
    except Exception as e:
        r, n = "raised %s" % type(e).__name__, -1
    finally:
        Path.unlink = saved
    check("log retention: a file that cannot be deleted is skipped without raising", r == "ok" and n == 0 and (d / "t6_send_2024-01-01.log").exists())
    with open(d / "t6_send_2024-01-01.log", "a") as held:   # really held open (Windows refuses the delete)
        held.write("y")
        try:
            m.ops.prune_logs("t6_send", env, today)
            r = "ok"
        except Exception as e:
            r = "raised %s" % type(e).__name__
    check("log retention: a file held open by another run does not raise", r == "ok")
    try:
        r = m.ops.prune_logs("t6_send", {"LOG_DIR": str(d / "nowhere" / "x")}, today)
    except Exception as e:
        r = "raised %s" % type(e).__name__
    check("log retention: a missing folder does not raise", r == 0)
    # the handler: today's file, pruning at creation
    (d / "t6_send_2020-02-02.log").write_text("x")
    h = m.ops.log_file_handler("t6_send", env)
    h.close()
    check("log handler: file named by today's date, old file pruned when it is created",
          Path(h.baseFilename).name == "t6_send_%s.log" % m.ops.sg_now().date().isoformat() and not (d / "t6_send_2020-02-02.log").exists())
    check("log handler: no size rotation (no rename can fail while another run holds the file)", type(h) is logging.FileHandler)


def test_sample_send(cur, today):
    """D-054: one-off sample mail of ONE subscription to ONE address; fake Tableau/mailer only."""
    import email as _email
    A = lambda **kw: NS(**dict(dict(sample_send=True, sample_to="sample@example.com", subscription_id=["T6TEST-SMP"], workbook=None,
                                    as_of_date=None, include_not_taken_over=False), **kw))
    check("sample args: a complete request is accepted", m.check_sample_args(A()) is None)
    check("sample args: no flags at all is no sample request", m.check_sample_args(NS(sample_send=False, sample_to=None, subscription_id=None)) is None)
    bad = [("no --sample-to", A(sample_to=None)), ("--sample-to without --sample-send", A(sample_send=False)),
           ("no subscription id", A(subscription_id=[])), ("two subscription ids", A(subscription_id=["a", "b"])),
           ("a --workbook", A(workbook="wb")), ("--as-of-date", A(as_of_date="2026-10-14")), ("--include-not-taken-over", A(include_not_taken_over=True)),
           ("comma list", A(sample_to="a@example.com,b@example.com")), ("semicolon list", A(sample_to="a@example.com;b@example.com")),
           ("space list", A(sample_to="a@example.com b@example.com")), ("leading space", A(sample_to=" a@example.com")),
           ("trailing newline", A(sample_to="a@example.com\n")), ("two @", A(sample_to="a@@example.com")), ("no @", A(sample_to="nobody")),
           ("angle brackets", A(sample_to="<a@example.com>")), ("empty", A(sample_to=""))]
    for why, a in bad:
        check("sample args: refused with %s" % why, m.check_sample_args(a) is not None)
    check("sample: main() refuses --sample-send without an address (exit 1, nothing else touched)", m.main(["--sample-send", "--subscription-id", "x"]) == 1)
    check("sample: main() refuses --sample-to alone", m.main(["--sample-to", "a@example.com"]) == 1)
    check("sample: a subscription that is not in the mapping is not loaded", m.load_subscriptions(cur, {"T6TEST-NOPE"}, id_like=LIKE) == [])

    add_sub(cur, "T6TEST-SMP", susp=1)
    add_backup(cur, "T6TEST-SMP")     # taken over and paused TODAY: the takeover-day guard applies to a real send
    load = lambda: m.load_subscriptions(cur, {"T6TEST-SMP"}, id_like=LIKE)[0]
    SAMPLE = "sample@example.com"
    real = [SECRET_ADDR, "real.two@example.com"]
    mail, ad = FakeMailer(), FakeAdapter(suspended=True, emails=real)
    ctx = mk_ctx(cur, today, ad, mail, ses=True, allow=m.parse_allow(SAMPLE + "," + SECRET_ADDR + ",real.two@example.com"), takeover_day_guard=True)
    s = run_subs(cur, ctx, ["T6TEST-SMP"])
    check("sample: a REAL send of a subscription taken over today is stopped by the takeover-day guard",
          s["kinds"]["taken_over_today"] == 1 and mail.sent == [] and logrows(cur, "T6TEST-SMP") == [])
    ok, text = m.sample_send(cur, load(), ctx, SAMPLE)
    raw = mail.sent[0][0] if mail.sent else b""
    msg = _email.message_from_bytes(raw, policy=_email.policy.default) if raw else None
    check("sample: sent although the guard stops a real send", ok and len(mail.sent) == 1, text)
    check("sample: goes ONLY to the sample address", mail.sent and mail.sent[0][2] == [SAMPLE] and msg["To"] == SAMPLE
          and SECRET_ADDR.encode() not in raw and b"real.two@example.com" not in raw)
    check("sample: the subscriber's address is never read from Tableau", ad.recipient_reads == 0)
    check("sample: subject is the real subject with the [SAMPLE] prefix", msg is not None and msg["Subject"] == "[SAMPLE] Subj", msg and msg["Subject"])
    check("sample: nothing is written to the send log", logrows(cur, "T6TEST-SMP") == [])
    check("sample: same export as a real send (PDF attached, one inline image)",
          msg is not None and any(p.get_content_type() == "application/pdf" for p in msg.walk())
          and any(p.get_content_type() == "image/png" for p in msg.walk()) and ad.exports == 1)
    s = run_subs(cur, ctx, ["T6TEST-SMP"])
    check("sample: afterwards a real send is still stopped by the guard (the sample is no back door)", s["kinds"]["taken_over_today"] == 1 and len(mail.sent) == 1)
    mail2 = FakeMailer()
    ctx2 = mk_ctx(cur, today, FakeAdapter(suspended=True, emails=real), mail2, ses=True, allow=m.parse_allow(",".join(real + [SAMPLE])),
                  takeover_day_guard=False)
    s = run_subs(cur, ctx2, ["T6TEST-SMP"])
    check("sample: the same day's real send still works afterwards, to the real recipients (no slot was consumed)",
          s["kinds"]["sent"] == 1 and statuses(cur, "T6TEST-SMP") == ["Sent"] and sorted(mail2.sent[0][2]) == sorted(real))
    mail3 = FakeMailer()
    ok, text = m.sample_send(cur, load(), mk_ctx(cur, today, FakeAdapter(suspended=True), mail3, ses=True, allow=m.parse_allow(SAMPLE)), SAMPLE)
    check("sample: not blocked by an existing Sent row of the day, and still writes no row", ok and len(mail3.sent) == 1 and statuses(cur, "T6TEST-SMP") == ["Sent"])
    # allow-list rules (non-UAT host: allow_all False)
    mail4 = FakeMailer()
    ok, text = m.sample_send(cur, load(), mk_ctx(cur, today, FakeAdapter(), mail4, ses=True, allow=m.parse_allow("someone.else@example.com")), SAMPLE)
    check("sample: off the UAT host the sample address must be on the allow-list", not ok and mail4.sent == [], text)
    mail4 = FakeMailer()
    ok, text = m.sample_send(cur, load(), mk_ctx(cur, today, FakeAdapter(), mail4, ses=True, allow=frozenset(), allow_all=True), SAMPLE)
    check("sample: on the UAT host with the switch no list entry is needed", ok and mail4.sent and mail4.sent[0][2] == [SAMPLE], text)
    mail4 = FakeMailer()
    ok, text = m.sample_send(cur, load(), mk_ctx(cur, today, FakeAdapter(), mail4, ses=True, allow=frozenset(), allow_all=True), "bad address")
    check("sample: even with the switch a malformed address is refused", not ok and mail4.sent == [])
    mail4 = FakeMailer()
    ok, text = m.sample_send(cur, load(), mk_ctx(cur, today, FakeAdapter(), mail4), "anyone@example.org")
    check("sample: local transport only writes to the local test sink, never to the given address",
          ok and mail4.sent and mail4.sent[0][2] == [m.LOCAL_TO])
    # gates that stay
    kf = TMP / "sample_kill_file"
    kf.write_text("x")
    saved = os.environ.get("KILL_FILE")
    os.environ["KILL_FILE"] = str(kf)
    try:
        mail5 = FakeMailer()
        ok, text = m.sample_send(cur, load(), mk_ctx(cur, today, FakeAdapter(), mail5), SAMPLE)
        check("sample: the kill switch stops it", not ok and "kill switch" in text and mail5.sent == [])
    finally:
        if saved is None:
            os.environ.pop("KILL_FILE", None)
        else:
            os.environ["KILL_FILE"] = saved
        kf.unlink()
    add_sub(cur, "T6TEST-SMP2", age_h=100)
    mail5 = FakeMailer()
    ok, text = m.sample_send(cur, m.load_subscriptions(cur, {"T6TEST-SMP2"}, id_like=LIKE)[0], mk_ctx(cur, today, FakeAdapter(), mail5), SAMPLE)
    check("sample: a mapping older than the limit is refused", not ok and "mapping sync" in text and mail5.sent == [])
    mail5 = FakeMailer()
    ok, text = m.sample_send(cur, load(), mk_ctx(cur, today, FakeAdapter(export_error="boom"), mail5, max_export_attempts=2), SAMPLE)
    check("sample: a failing export is retried, then reported (no mail, no alert, no log row)", not ok and "export failed" in text and mail5.sent == []
          and statuses(cur, "T6TEST-SMP2") == [])
    mail5 = FakeMailer()
    ok, text = m.sample_send(cur, load(), mk_ctx(cur, today, FakeAdapter(), mail5, size_limit=100), SAMPLE)
    check("sample: the size limit applies", not ok and "limit" in text and mail5.sent == [])
    ok, text = m.sample_send(cur, load(), mk_ctx(cur, today, FakeAdapter(), FakeMailer("predata")), SAMPLE)
    check("sample: a send failure is reported on the console result (no retry)", not ok and "send failed" in text)
    mail5 = FakeMailer()
    ok, text = m.sample_send(cur, load(), mk_ctx(cur, today, FakeAdapter(), mail5, execute=False), SAMPLE)
    check("sample: without --execute nothing is mailed", ok and "dry run" in text and mail5.sent == [])
    check("sample: nothing in the send log for the sample subscriptions at the end", statuses(cur, "T6TEST-SMP2") == [])


def test_pat_lock(cur):
    c2 = m.t4.connect()
    # own lock name: the real TABLEAU_PAT_SESSION may be held by a real run or another session, which made this test crash with PatBusy
    a, b = m.t4.LazyTableau(cur, lock_name="T6TEST_PAT_SESSION"), m.t4.LazyTableau(c2.cursor(), lock_name="T6TEST_PAT_SESSION")
    a._lock()
    try:
        b._lock()
        r = "got it"
    except m.t4.PatBusy:
        r = "busy"
    check("PAT session lock: a second script cannot take it (PatBusy, to be skipped with exit 0)", r == "busy" and a.locked and not b.locked)
    a.close()
    try:
        b._lock()
        r = "got it"
    except m.t4.PatBusy:
        r = "busy"
    check("PAT session lock: free again after the first script closes", r == "got it")
    b.close()
    c2.close()

    class Busy:
        def describe(self, sub):
            raise m.t4.PatBusy()
    add_sub(cur, "T6TEST-PAT")
    ctx = mk_ctx(cur, m.t4.dwh_today(cur), Busy(), FakeMailer())
    try:
        run_subs(cur, ctx, ["T6TEST-PAT"])
        r = "swallowed"
    except m.t4.PatBusy:
        r = "propagated"
    check("PAT busy is not swallowed by the per-subscription error handler, and nothing was claimed",
          r == "propagated" and logrows(cur, "T6TEST-PAT") == [])


def test_logging_handlers():
    m.setup_logging()
    f1 = [h for h in m.log.handlers if isinstance(h, logging.FileHandler)]
    f2 = [h for h in m.t4.log.handlers if isinstance(h, logging.FileHandler)]
    check("logging: one file handler on today's t6_send_<date>.log, shared with t4's logger",
          len(f1) == 1 and len(f2) == 1 and f1[0] is f2[0]
          and Path(f1[0].baseFilename).name == "t6_send_%s.log" % m.ops.sg_now().date().isoformat())
    for lg in (m.log, m.t4.log):   # leave the test run's output out of the real log file
        for h in list(lg.handlers):
            lg.removeHandler(h)
            h.close()


def test_alert_state(cur, today):
    ev = [{"sub": "S1", "wb": "WB", "code": "ambiguous", "text": "x"}]
    c = cfg(alert_state=TMP / "alert_state_a.json")
    amb = FakeMailer("ambiguous")
    n = m.deliver_alerts(c, amb, today, ev)
    mm = FakeMailer()
    n2 = m.deliver_alerts(c, mm, today, ev)
    n3 = m.deliver_alerts(c, mm, today, ev)
    check("alert after an ambiguous send is retried ONCE on the next run (never lost), then not again", n == 1 and n2 == 1 and len(mm.sent) == 1 and n3 == 0)
    c = cfg(alert_state=TMP / "alert_state_b.json")
    m.deliver_alerts(c, FakeMailer("predata"), today, ev)
    ok = FakeMailer()
    n = m.deliver_alerts(c, ok, today, ev)
    check("alert that failed before DATA is retried on the next run", n == 1 and len(ok.sent) == 1)
    (TMP / "afile").write_text("x")
    c = cfg(alert_state=TMP / "afile" / "state.json")   # a path below a regular file cannot be created
    ok = FakeMailer()
    check("unwritable alert state does not stop the alert", m.deliver_alerts(c, ok, today, ev) == 1 and len(ok.sent) == 1)
    # startup failure: RUN SUMMARY-style line, alert once, exit code 1
    cap = LogCapture()
    m.log.addHandler(cap)
    m.log.setLevel(logging.INFO)
    c = cfg(alert_state=TMP / "alert_state_c.json")
    mail = FakeMailer()
    rc = m.fatal(c, mail, "Tableau sign-in failed")
    rc2 = m.fatal(c, mail, "Tableau sign-in failed")
    m.log.removeHandler(cap)
    check("startup failure: exit code 1, RUN SUMMARY FATAL line, one alert mail per day",
          rc == 1 and rc2 == 1 and len(mail.sent) == 1 and any("RUN SUMMARY" in x and "FATAL=" in x for x in cap.lines))
    mail = FakeMailer()
    m.fatal(cfg(execute=False, alert_state=TMP / "alert_state_d.json"), mail, "x")
    check("startup failure in a dry run sends no alert", mail.sent == [])


def test_new_rules(cur, today):
    cap = LogCapture()
    m.log.addHandler(cap)
    m.log.setLevel(logging.INFO)
    try:
        # NULL hash fails closed
        add_sub(cur, "T6TEST-N1", key=None)
        mail = FakeMailer()
        ctx = mk_ctx(cur, today, FakeAdapter(), mail)
        s = run_subs(cur, ctx, ["T6TEST-N1"])
        check("NULL SubscriptionKeyHash: not sent, no claim, alert event", s["kinds"]["duplicate_key"] == 1 and logrows(cur, "T6TEST-N1") == []
              and reports(mail) == [] and any(e["code"] == "duplicate_key" for e in ctx.events))
        # active sibling, this one has no history while the sibling has: alert but still sent
        key = bytes(range(32, 64))
        add_sub(cur, "T6TEST-N2OLD", key=key)
        add_sub(cur, "T6TEST-N2NEW", key=key)
        cur.execute("INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status, SentAt, ClaimedAt) VALUES "
                    "('T6TEST-N2OLD', 'Sent', DATEADD(day, -3, SYSDATETIMEOFFSET()), DATEADD(day, -3, SYSDATETIMEOFFSET()))")
        mail = FakeMailer()
        ctx = mk_ctx(cur, today, FakeAdapter(), mail)
        s = run_subs(cur, ctx, ["T6TEST-N2NEW"])
        check("new active duplicate of a subscription with history: alert, but still sent", s["kinds"]["sent"] == 1
              and any(e["code"] == "duplicate_active" for e in ctx.events))
        # stale mapping
        add_sub(cur, "T6TEST-S1", age_h=30)
        mail = FakeMailer()
        ctx = mk_ctx(cur, today, FakeAdapter(), mail)
        s = run_subs(cur, ctx, ["T6TEST-S1"])
        check("T2 sync older than 26 h: not sent, no claim, alert event", s["kinds"]["mapping_stale"] == 1 and logrows(cur, "T6TEST-S1") == []
              and reports(mail) == [] and any(e["code"] == "mapping_stale" for e in ctx.events))
        add_sub(cur, "T6TEST-S2", age_h=1)
        ctx = mk_ctx(cur, today, FakeAdapter(wb_updated_at=dt.datetime.now(dt.timezone.utc)), FakeMailer())
        s = run_subs(cur, ctx, ["T6TEST-S2"])
        check("workbook changed on Tableau after the last sync (D-046): waits quietly for the next daily sync (no claim, no alert, exit 0)",
              s["kinds"]["awaiting_sync"] == 1 and s["kinds"]["mapping_stale"] == 0 and logrows(cur, "T6TEST-S2") == []
              and not any(e["code"] == "mapping_stale" for e in ctx.events) and m.exit_code(ctx, s) == 0
              and "awaiting_sync=1" in m.summary_line(ctx, s) and "needs_human=0" in m.summary_line(ctx, s))
        # R5 / F-3: the workbook change time is compared with the updated_at T2 STORED when it read the workbook, never with a clock
        U = lambda h, mi=0: dt.datetime(2026, 1, 1, h, mi, tzinfo=dt.timezone.utc)

        def upd_case(sid, stored, live):
            add_sub(cur, sid)   # LastSeenAt = now, i.e. LATER than both times below: the old clock-based check would have passed
            cur.execute("UPDATE Integration.Tableau_Subscription SET WorkbookUpdatedAt = ? WHERE Subscription_id = ?", stored, sid)
            cx = mk_ctx(cur, today, FakeAdapter(wb_updated_at=live), FakeMailer())
            return run_subs(cur, cx, [sid])["kinds"], cx
        k, _ = upd_case("T6TEST-U1", dt.datetime(2026, 1, 1, 10, 0), U(10, 5))
        check("F-3 hole (a): workbook republished at 10:05, T2 read it at 10:00 but stamped LastSeenAt later: still awaiting_sync, not sent",
              k["awaiting_sync"] == 1 and logrows(cur, "T6TEST-U1") == [])
        k, _ = upd_case("T6TEST-U2", dt.datetime(2026, 1, 1, 10, 0), U(10, 0))
        check("F-3: live updated_at equal to the stored value: not awaiting, sent", k["awaiting_sync"] == 0 and k["sent"] == 1)
        k, _ = upd_case("T6TEST-U3", dt.datetime(2026, 1, 1, 10, 0), U(9, 0))
        check("F-3: live updated_at older than the stored value: sent", k["awaiting_sync"] == 0 and k["sent"] == 1)
        k, _ = upd_case("T6TEST-U4", dt.datetime(2026, 1, 1, 10, 0), dt.datetime(2026, 1, 1, 18, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))))
        check("F-3: time zones are compared as instants (18:00+08:00 == 10:00 UTC): sent", k["awaiting_sync"] == 0 and k["sent"] == 1)
        k, _ = upd_case("T6TEST-U5", None, U(9, 0))
        check("F-3: nothing stored yet (T2 has not run since R5): awaiting_sync (fail closed), no claim",
              k["awaiting_sync"] == 1 and logrows(cur, "T6TEST-U5") == [])
        # budget
        add_sub(cur, "T6TEST-B1")
        mail = FakeMailer()
        s = run_subs(cur, mk_ctx(cur, today, FakeAdapter(), mail, budget_min=0), ["T6TEST-B1"])
        check("run budget used up: nothing new is claimed (deferred to the next run)", s["kinds"]["deferred"] == 1 and logrows(cur, "T6TEST-B1") == []
              and mail.sent == [])
        # format warnings
        add_sub(cur, "T6TEST-F1")
        ctx = mk_ctx(cur, today, FakeAdapter(page_size="TABLOID-X", orientation=None), FakeMailer(), execute=False)
        s = run_subs(cur, ctx, ["T6TEST-F1"])
        check("unknown page size / missing orientation: WARNING logged and counted in the summary line",
              any("FORMAT WARNING" in x and "TABLOID-X" in x for x in cap.lines) and "format_warnings=1" in m.summary_line(ctx, s))
        check("known page size and orientation raise no warning", sm.page_problems("A4", "LANDSCAPE") == [] and sm.page_problems("legal", "portrait") == [])
    finally:
        m.log.removeHandler(cap)


class BusyAdapter(FakeAdapter):
    """FakeAdapter whose Tableau background-job list is set by the test; uses the real t4.tableau_busy (D-057)."""

    def __init__(self, jobs=(), fail=False, **kw):
        super().__init__(**kw)
        self.jobs, self.fail, self.own_seen = list(jobs), fail, None

    def tableau_busy(self, own_ids):
        self.own_seen = list(own_ids)
        if self.fail:
            raise RuntimeError("jobs endpoint down")

        m.t4.clear_jobs_cache()
        srv = NS(jobs=NS(get=lambda req_options=None: (
            list(self.jobs), NS(page_number=1, page_size=1000, total_available=len(self.jobs)))))
        return m.t4.tableau_busy(lambda: srv, own_ids)


def bjob(jid, status, hours_old=0.1):
    return NS(id=jid, title="x", type="SubscriptionTask", status=status, ended_at=None,
                           created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_old))


def test_tableau_busy(cur, today):
    """D-057: no export and no claim while Tableau has other people's queued/running jobs."""
    saved = os.environ.pop("TABLEAU_BUSY_CHECK", None)
    try:
        for sid in ("T6TEST-BUSY1", "T6TEST-BUSY2", "T6TEST-BUSY3", "T6TEST-BUSY4", "T6TEST-BUSY5"):
            add_sub(cur, sid)
        mail = FakeMailer()
        ad = BusyAdapter([bjob("a", "Pending"), bjob("b", "InProgress")])
        ctx = mk_ctx(cur, today, ad, mail, cutoff=m.dt.time(23, 59, 59))
        s = run_subs(cur, ctx, ["T6TEST-BUSY1"])
        check("busy: deferred as not_ready, no export, no claim row, no mail, no alert before the cutoff",
              s["kinds"]["not_ready"] == 1 and ad.exports == 0 and logrows(cur, "T6TEST-BUSY1") == [] and not mail.sent and s["alerts"] == 0,
              str(dict(s["kinds"])))
        check("busy: reason names Tableau busy with counts", ctx.not_ready and "Tableau busy: 1 queued, 1 running" in ctx.not_ready[0][1])
        ctx = mk_ctx(cur, today, ad, mail, cutoff=m.dt.time(0, 0))
        s = run_subs(cur, ctx, ["T6TEST-BUSY1"])
        check("busy past the cutoff: the existing not-ready alert covers it (one mail, still no report)",
              s["alerts"] == 1 and len(mail.sent) == 1 and b"still not ready after the cutoff" in mail.sent[0][0].replace(b"\r\n", b" ")
              and logrows(cur, "T6TEST-BUSY1") == [])
        # dry run says would wait
        ctx = mk_ctx(cur, today, BusyAdapter([bjob("a", "Pending")]), FakeMailer(), execute=False)
        subs = m.load_subscriptions(cur, {"T6TEST-BUSY1"}, id_like=LIKE)
        ctx.prior = {}
        kind, detail = m.process(cur, subs[0], ctx)
        check("dry run: would wait: Tableau busy", kind == "not_ready" and detail.startswith("would wait: Tableau busy"), detail)
        # our own recorded job is not someone else's
        cur.execute("INSERT Integration.Tableau_RefreshJob (DataSource_id, TableauJobId) VALUES ('T6TEST-OWNDS', 'own-job-1')")
        ad = BusyAdapter([bjob("own-job-1", "InProgress")])
        mail = FakeMailer()
        s = run_subs(cur, mk_ctx(cur, today, ad, mail), ["T6TEST-BUSY2"])
        cur.execute("DELETE FROM Integration.Tableau_RefreshJob WHERE DataSource_id = 'T6TEST-OWNDS'")
        check("own job ignored: not busy, sent", s["kinds"]["sent"] == 1 and "own-job-1" in (ad.own_seen or []))
        # stale Pending ignored
        s = run_subs(cur, mk_ctx(cur, today, BusyAdapter([bjob("old", "Pending", hours_old=7)]), FakeMailer()), ["T6TEST-BUSY3"])
        check("Pending job older than 6 h ignored: sent", s["kinds"]["sent"] == 1)
        # idle
        s = run_subs(cur, mk_ctx(cur, today, BusyAdapter([bjob("d", "Success")]), FakeMailer()), ["T6TEST-BUSY4"])
        check("idle: normal send", s["kinds"]["sent"] == 1)
        # switched off
        os.environ["TABLEAU_BUSY_CHECK"] = "0"
        s = run_subs(cur, mk_ctx(cur, today, BusyAdapter([bjob("a", "InProgress")]), FakeMailer()), ["T6TEST-BUSY5"])
        check("TABLEAU_BUSY_CHECK=0: check off, sent", s["kinds"]["sent"] == 1)
        os.environ.pop("TABLEAU_BUSY_CHECK")
        # lookup failure = busy
        add_sub(cur, "T6TEST-BUSY6")
        ctx = mk_ctx(cur, today, BusyAdapter(fail=True), FakeMailer())
        s = run_subs(cur, ctx, ["T6TEST-BUSY6"])
        check("job list unreadable: treated as busy, no claim", s["kinds"]["not_ready"] == 1 and logrows(cur, "T6TEST-BUSY6") == []
              and ctx.probe_fail == 1)
    finally:
        os.environ.pop("TABLEAU_BUSY_CHECK", None)
        if saved is not None:
            os.environ["TABLEAU_BUSY_CHECK"] = saved


def test_export_cap():
    """export_images against a fake server: the 3200 px cap, and which images are flagged unreadable."""
    sizes = {"wide": (3300, 100), "normal": (2000, 1500), "wide_ok": (3300, 3000)}   # just over the 3200 px cap: small images (a 6400 px one ran out of memory once on the 8 GB host)

    class Server:
        def __init__(self):
            self.views = NS(populate_image=self.pop)

        def pop(self, v, opts):
            v.image = png(*sizes[v.name])
    views = [NS(name=n, image=None) for n in sizes]
    out = {i["name"]: i for i in sm.export_images(Server(), views)}
    check("export cap: 3300x100 -> 3200x97 and flagged only if under 150 px high", (out["wide"]["w"], out["wide"]["h"]) == (3200, 97)
          and out["wide"]["unreadable"] and out["wide"]["capped"])
    check("export cap: wide but tall enough is capped, not flagged", (out["wide_ok"]["w"], out["wide_ok"]["h"]) == (3200, 2909)
          and not out["wide_ok"]["unreadable"])
    check("export cap: normal image untouched", (out["normal"]["w"], out["normal"]["h"]) == (2000, 1500) and not out["normal"]["capped"])


# ---------- local end-to-end ----------
def e2e(cur, today):
    """Real export of the CFD Accounts v2 test subscription from Tableau UAT, delivered to a local SMTP sink (.eml in out/)."""
    import asyncio
    import os
    from aiosmtpd.controller import Controller
    import preview_smtp
    out = ROOT / "out"
    before = set(out.glob("smtp_*.eml"))
    ctrl = Controller(preview_smtp.EmlHandler(out), hostname="127.0.0.1", port=1031)
    ctrl.start()
    tableau = m.t4.LazyTableau(cur)
    sid = None
    try:
        server = tableau()
        subs = list(__import__("tableauserverclient").Pager(server.subscriptions))
        wbs = {w.id: w for w in __import__("tableauserverclient").Pager(server.workbooks)}
        pick = [s for s in subs if s.target.type.lower() == "workbook" and wbs.get(s.target.id) and wbs[s.target.id].name == "CFD Accounts v2"]
        print("e2e: %d subscription(s) on the workbook 'CFD Accounts v2'" % len(pick))
        if not pick:
            check("e2e: CFD Accounts v2 test subscription found", False)
            return
        s0 = pick[0]
        sid = "T6TEST-E2E"   # a copy: the real Subscription_id and its history are never touched
        key = hashlib.sha256(sid.encode()).digest()
        fmt = "PDFAndImage" if s0.attach_pdf and s0.attach_image else "PDF" if s0.attach_pdf else "Image"
        sched = ""
        try:
            sched = server.schedules.get_by_id(s0.schedule_id).name[:50]
        except Exception:
            pass
        cur.execute("INSERT Integration.Tableau_Subscription (Subscription_id, Workbook_id, TargetType, TargetId, Format, Subject, "
                    "PageSize, Orientation, IsSuspended, ScheduleType, Active_Flag, SubscriptionKeyHash, WorkbookUpdatedAt) VALUES (?, ?, 'Workbook', ?, ?, ?, ?, ?, 0, ?, 1, ?, SYSUTCDATETIME())",
                    sid, s0.target.id, s0.target.id, fmt, (s0.subject or "")[:500], s0.page_size_option, s0.page_orientation, sched, key)
        c = cfg(id_like=sid, local_release=True, alert_state=TMP / "e2e_alerts.json")
        ctx = m.Ctx(c, m.TableauAdapter(tableau, {sid: s0.id}), m.make_mailer(c, "127.0.0.1", 1031), FakeGate(), today)
        subs = m.load_subscriptions(cur, {sid}, id_like=sid)
        s = m.run(cur, ctx, subs)
        print(m.summary_line(ctx, s))
        rows = cur.execute("SELECT Status, Message FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id = ?", sid).fetchall()
        print("e2e send log rows:", [tuple(r) for r in rows])
        time.sleep(1.5)
        new = sorted(set(out.glob("smtp_*.eml")) - before)
        check("e2e: one delivery to the local sink, log row Released", len(new) >= 1 and [r[0] for r in rows] == ["Released"] and s["kinds"]["sent"] == 1)
        import email as _email
        for f in new:
            em = _email.message_from_bytes(f.read_bytes(), policy=_email.policy.default)
            print("e2e eml: %s  %d bytes  subject=%r" % (f.name, f.stat().st_size, em["Subject"]))
            for p in em.walk():
                if p.get_content_maintype() in ("image", "application"):
                    d = p.get_content()
                    dims = "%dx%d px" % sm.png_size(d) if p.get_content_type() == "image/png" else "%d pages" % sm.page_count(d)
                    print("   part %-10s %-45s %8d B  %s  %s" % (p.get_content_type(), p.get_filename(), len(d), dims,
                                                             "inline" if p["Content-ID"] else "attachment"))
        for u in ctx.unreadable:
            print("   unreadable image:", u[1:])
    finally:
        tableau.close()
        ctrl.stop()


def main():
    os.environ['EVENTLOG_DISABLED'] = '1'
    os.environ.setdefault('LOG_DIR', tempfile.mkdtemp(prefix='logs_'))   # tests never write the real out/*.log
    os.environ['LASTRUN_DIR'] = tempfile.mkdtemp(prefix='lastrun_')   # tests must not write real last-run markers
    os.environ['OPS_STATE_DIR'] = os.environ['LASTRUN_DIR']
    # NO test may reach SES, whatever the environment holds: clear every sending setting and refuse any non-loopback SMTP host.
    for k in [k for k in os.environ if k.startswith(("SES_", "SMTP_"))] + ["SEND_ALLOWED_RECIPIENTS", "SEND_FROM", "ALERT_TO", "MAIL_TRANSPORT", "ADDR_HASH_SALT", "SEND_ALLOW_ALL_UAT"]:
        os.environ.pop(k, None)
    real_smtp = smtplib.SMTP

    def loopback_only(host, *a, **kw):
        if str(host) not in ("127.0.0.1", "localhost") and not str(host).endswith(".invalid"):
            raise AssertionError("test tried to open an SMTP connection to %r" % host)
        return real_smtp(host, *a, **kw)
    smtplib.SMTP = loopback_only
    conn = m.t4.connect()
    cur = conn.cursor()
    cur.execute("EXEC sp_set_session_context N'allow_fixture_delete', 1")   # R3 trigger: fixture cleanups only
    today = m.t4.dwh_today(cur)
    cleanup(cur)
    before = others_checksum(cur)
    try:
        test_gate()
        test_export_cap()
        test_smtp()
        test_smtp_tls()
        test_config()
        test_allow_all_uat()
        test_log_retention()
        test_logging_handlers()
        test_alert_state(cur, today)
        test_flow(cur, today)
        test_new_rules(cur, today)
        test_tableau_busy(cur, today)
        test_takeover_rule(cur, today)
        test_sample_send(cur, today)
        test_pat_lock(cur)
        test_send_order(cur)
        test_render_as_subscriber()
        if "--e2e" in sys.argv:
            e2e(cur, today)
    finally:
        cleanup(cur)
    check("other rows of Tableau_Subscription / Tableau_SubscriptionSendLog unchanged", others_checksum(cur) == before)
    check("fixture rows cleaned up", cur.execute("SELECT (SELECT COUNT(*) FROM Integration.Tableau_Subscription WHERE Subscription_id LIKE ?) "
                                                 "+ (SELECT COUNT(*) FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id LIKE ?) "
                                                 "+ (SELECT COUNT(*) FROM Integration.Tableau_SubscriptionBackup WHERE Subscription_id LIKE ?)",
                                                 LIKE, LIKE, LIKE).fetchone()[0] == 0)
    print("\n%d failure(s)" % len(fails) if fails else "\nALL CHECKS PASSED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
