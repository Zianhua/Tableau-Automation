"""T11a checks (t11_pause.py plus the t6 interaction). NO write to Tableau and no real mail: a fake Tableau server object
(real tableauserverclient SubscriptionItem objects) and a fake mailer stand in. One test inspects, without any network, the
HTTP request the real tableauserverclient 0.42 builds for a pause. Dev DWH only: fixture rows under Subscription_id
'T11TEST-%' in Tableau_Subscription, Tableau_SubscriptionBackup and Tableau_SubscriptionSendLog (deleted at the start and
at the end); a checksum proves no other row of those tables changed. Backup JSON files go to a temp directory.

Usage:  python scripts/t11_checks.py
"""
import copy
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent))
import t11_pause as m  # noqa: E402
import t4_refresh as t4  # noqa: E402
import t6_checks as c6  # noqa: E402  (fakes: mailer, adapter, gate, mk_gate)
import t6_send as t6  # noqa: E402
from check_fixtures import known_pipelines  # noqa: E402

LIKE = "T11TEST-%"
WB = "T11TEST-WB"
TARGET = "T11TEST-TARGET"
TMP = Path(tempfile.mkdtemp(prefix="t11checks_"))
SECRET = "secret.person@example.com"
fails = []
_n = [0]


def check(name, ok, detail=""):
    print("%s  %s %s" % ("PASS" if ok else "FAIL", name, detail))
    if not ok:
        fails.append(name)


# ---------- fake Tableau ----------
class NotFound(Exception):
    code = "404021"


class FakeSubs:
    def __init__(self):
        self.items, self.updates = {}, []
        self.hook = None            # called with the item just before an update is applied
        self.drop_message = False   # simulate a server that clears the message on update
        self.fail_update = None     # exception to raise on update

    def get_by_id(self, sid):
        if sid not in self.items:
            raise NotFound()
        return copy.deepcopy(self.items[sid])

    def update(self, item):
        if self.hook:
            self.hook(item)
        if self.fail_update:
            raise self.fail_update
        self.updates.append((item.id, item.suspended))
        stored = self.items[item.id]
        stored.suspended = item.suspended
        if self.drop_message:
            stored.message = None
        return copy.deepcopy(stored)


class FakeServer:
    def __init__(self):
        self.subscriptions = FakeSubs()
        self.schedules = NS(get_by_id=lambda i: NS(name="Daily 0900"))

    def add(self, sid, suspended=False, target=TARGET, message="Hello <b>team</b>", subject="Daily report"):
        from tableauserverclient.models import SubscriptionItem
        from tableauserverclient.models.target import Target
        it = SubscriptionItem(subject, "SCHED-1", "USER-1", Target(target, "workbook"))
        it._id = sid
        it.message, it.page_size_option, it.page_orientation = message, "A4", "Landscape"
        it.attach_pdf, it.attach_image, it.send_if_view_empty, it.suspended = True, False, True, suspended
        self.subscriptions.items[sid] = it

    def suspended(self, sid):
        return self.subscriptions.items[sid].suspended

    def resume(self, sid):   # a human un-pauses it in Tableau
        self.subscriptions.items[sid].suspended = False


class NoTableau:
    def __call__(self):
        raise AssertionError("Tableau must not be touched")


class FakeGate:
    def __init__(self, count=2, structural=(True, "ok")):
        self.wb = {WB: ("Ready", count)}
        self._s = structural

    def structural(self, wb_id):
        return self._s


# ---------- DWH fixtures ----------
def others_checksum(cur):
    return [tuple(cur.execute("SELECT COUNT(*), CHECKSUM_AGG(CHECKSUM(*)) FROM Integration.%s WHERE Subscription_id NOT LIKE ?" % t, LIKE).fetchone())
            for t in ("Tableau_Subscription", "Tableau_SubscriptionBackup", "Tableau_SubscriptionSendLog")]


def cleanup(cur):
    for t in ("Tableau_SubscriptionSendLog", "Tableau_SubscriptionBackup", "Tableau_Subscription"):
        cur.execute("DELETE FROM Integration.%s WHERE Subscription_id LIKE ?" % t, LIKE)


def add_sub(cur, sid, wb=WB, age_h=0, target=TARGET, active=1):
    cur.execute("INSERT Integration.Tableau_Subscription (Subscription_id, Workbook_id, TargetType, TargetId, Format, Subject, PageSize, "
                "Orientation, IsSuspended, ScheduleType, SubscriptionKeyHash, Active_Flag, LastSeenAt, WorkbookUpdatedAt) VALUES (?, ?, 'Workbook', ?, 'PDF', "
                "'Daily report', 'A4', 'Landscape', 0, 'Daily 0900', ?, ?, DATEADD(minute, -?, SYSDATETIME()), '2020-01-01')",
                sid, wb, target, hashlib.sha256(sid.encode()).digest(), active, int(age_h * 60))


def backup_rows(cur, sid):
    return cur.execute("SELECT Backup_id, TakenOver, CAST(PausedAt AS datetime2), CAST(RestoredAt AS datetime2), CAST(LastRepausedAt AS datetime2), RepauseCount, Subject, Message, "
                       "ScheduleId, ScheduleName, PageSize, Orientation, AttachPdf, AttachImage, OwnerUserId, TargetId, TargetType "
                       "FROM Integration.Tableau_SubscriptionBackup WHERE Subscription_id = ? ORDER BY Backup_id", sid).fetchall()


def logrows(cur, sid):
    return [r[0] for r in cur.execute("SELECT Status FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id = ?", sid).fetchall()]


def fresh_cfg(execute=True, **kw):
    _n[0] += 1
    alert = t6.Config(execute=execute, alert_state=TMP / ("alerts%d.json" % _n[0]), cutoff=dt.time(23, 59, 59), local_release=False)
    base = dict(execute=execute, workbooks=frozenset({WB.lower()}), id_like=LIKE, backup_dir=TMP / "backup", alert=alert,
                backoff_path=TMP / ("backoff%d.json" % _n[0]))
    base.update(kw)
    return m.Config(**base)


def run_t11(cur, server, today, cfg=None, gate=None, mailer=None, **kw):
    cfg = cfg or fresh_cfg(**kw)
    ctx = m.Ctx(cfg, m.TableauAdapter(server if callable(server) else (lambda: server)), gate or FakeGate(), cur, today)
    mail = mailer or c6.FakeMailer()
    m.run(ctx, mail)
    return ctx, mail


def alerts(mail):
    return [x for x in mail.sent if b"taken-over" in x[0]]


# ---------- t6 side ----------
class Bridge(c6.FakeAdapter):
    """t6 adapter that reads the fake server, so t6 and t11 see the same Tableau."""

    def __init__(self, server):
        super().__init__(emails=(SECRET,))
        self.server = server

    def describe(self, sub):
        it = self.server.subscriptions.get_by_id(sub.id)
        self.suspended = bool(it.suspended)
        return super().describe(sub)


def run_t6(cur, server, today, ids):
    cfg = t6.Config(execute=True, local_release=False, id_like=LIKE, alert_state=TMP / ("t6alerts%d.json" % (_n[0] + 100)),
                    cutoff=dt.time(23, 59, 59), include_untaken=False, takeover_day_guard=False)
    _n[0] += 1
    mail = c6.FakeMailer()
    ctx = t6.Ctx(cfg, Bridge(server), mail, c6.FakeGate(), today)
    s = t6.run(cur, ctx, t6.load_subscriptions(cur, set(ids), id_like=LIKE))
    return s, c6.reports(mail), ctx


# ---------- the real library request (no network) ----------
def test_real_request():
    import tableauserverclient as TSC
    from tableauserverclient.models import SubscriptionItem
    xml = (b'<tsResponse xmlns="http://tableau.com/api"><subscription id="S1" subject="Sub 1" attachImage="false" attachPdf="true" '
           b'message="Hello msg" pageOrientation="Landscape" pageSizeOption="A4" suspended="false"><content id="W1" type="Workbook" '
           b'sendIfViewEmpty="true"/><schedule id="SCH1"/><user id="U1"/></subscription></tsResponse>')
    srv = TSC.Server("http://example.invalid:8000/", use_server_version=False)
    srv.version, srv._site_id, srv._auth_token = "3.25", "SITE", "tok"
    item = SubscriptionItem.from_response(xml, srv.namespace)[0]
    cap = {}

    def fake_put(url, body, *a, **k):
        cap["url"], cap["body"] = url, body.decode()
        return NS(content=xml.replace(b'suspended="false"', b'suspended="true"'))
    srv.subscriptions.put_request = fake_put
    item.suspended = True
    srv.subscriptions.update(item)
    b = cap["body"]
    check("library: pause is a PUT to .../subscriptions/<id> via server.subscriptions.update", cap["url"].endswith("/sites/SITE/subscriptions/S1"))
    check("library: the request carries suspended=\"true\" and re-sends the values read (subject, formats, page size, orientation, schedule id)",
          'suspended="true"' in b and 'subject="Sub 1"' in b and 'attachPdf="true"' in b and 'attachImage="false"' in b
          and 'pageSizeOption="A4"' in b and 'pageOrientation="Landscape"' in b and 'schedule id="SCH1"' in b and 'sendIfViewEmpty="true"' in b)
    check("library: the request does NOT carry the message (so Tableau must leave it unchanged; t11 verifies by reading back)",
          "message" not in b and "Hello msg" not in b)
    check("library: no recipient and no user element in the request", "<user" not in b and "@" not in b)


# ---------- config ----------
def test_config():
    def A(**kw):
        base = dict(execute=False, ses=False, workbooks=None, subscription_ids=None, force_notready=False, retake_restored=False,
                    restore=None, restore_all_for_workbook=None, max_mapping_age_h=26.0, owner_user_id=None, max_new=None)
        base.update(kw)
        return NS(**base)
    check("allow-list: empty by default (nothing is taken over)", m.build_config(A(), {}).workbooks == frozenset()
          and m.build_config(A(), {}).sub_ids == frozenset())
    c = m.build_config(A(), {"TAKEOVER_WORKBOOK_IDS": "CFD Accounts, wb-2", "TAKEOVER_SUBSCRIPTION_IDS": "S1"})
    check("allow-list from the environment (TAKEOVER_WORKBOOK_IDS / TAKEOVER_SUBSCRIPTION_IDS)",
          c.workbooks == {"cfd accounts", "wb-2"} and c.sub_ids == {"s1"})
    c = m.build_config(A(workbooks=["Other WB"]), {"TAKEOVER_WORKBOOK_IDS": "CFD Accounts"})
    check("allow-list: the command line replaces the environment", c.workbooks == {"other wb"})
    check("dry run is the default", m.build_config(A(), {}).execute is False)

    # R5 / F-2: the owner fence is required for any --execute with an allow-list (not only Gmail mode)
    uid = "11111111-2222-3333-4444-555555555555"

    def cfg_error(**kw):
        env = kw.pop("env", {})
        try:
            m.build_config(A(**kw), env)
            return ""
        except t6.ConfigError as e:
            return str(e)
    e1 = cfg_error(execute=True, workbooks=[uid])
    check("F-2: --execute with a workbook allow-list and NO owner fence is refused (local transport, not Gmail)", "--owner-user-id" in e1, e1[:60])
    check("F-2: ... also for --subscription-ids", "--owner-user-id" in cfg_error(execute=True, subscription_ids=["S1"]))
    check("F-2: ... also when the allow-list comes from the environment", "--owner-user-id" in cfg_error(execute=True, env={"TAKEOVER_WORKBOOK_IDS": uid}))
    check("F-2: with --owner-user-id it is accepted", cfg_error(execute=True, workbooks=[uid], owner_user_id=["u1"]) == "")
    check("F-2: ... or with TAKEOVER_OWNER_USER_IDS", cfg_error(execute=True, workbooks=[uid], env={"TAKEOVER_OWNER_USER_IDS": "u1,u2"}) == "")
    check("F-2: --execute with an EMPTY allow-list (keep-paused run of the scheduler) needs no fence", cfg_error(execute=True) == "")
    check("F-2: --restore --execute needs no fence (a rollback must always work)", cfg_error(execute=True, restore=["S1"]) == "")
    check("F-2: a dry run needs no fence", cfg_error(execute=False, workbooks=[uid]) == "")
    check("F-2: Gmail test mode: same rule, message names the Gmail owner",
          "Gmail" in cfg_error(execute=True, workbooks=[uid], env={"MAIL_TRANSPORT": "smtp", "SMTP_PROVIDER": "gmail"}))
    check("F-2: new-takeover cap defaults to 10, --max-new and TAKEOVER_MAX_NEW override (CLI first)",
          m.build_config(A(), {}).max_new == 10 and m.build_config(A(max_new=3), {"TAKEOVER_MAX_NEW": "7"}).max_new == 3
          and m.build_config(A(), {"TAKEOVER_MAX_NEW": "7"}).max_new == 7)
    check("F-2: a bad cap is refused", "whole number" in cfg_error(env={"TAKEOVER_MAX_NEW": "many"}) and "0 or more" in cfg_error(max_new=-1))
    try:
        m.build_config(A(execute=False, ses=True), {})
        r = "accepted"
    except t6.ConfigError:
        r = "refused"
    check("--ses without --execute is refused (t6 rules)", r == "refused")
    check("alert transport is local by default", m.build_config(A(execute=True), {"SES_SMTP_USER": "u"}).alert.ses is False)
    cfg = fresh_cfg()
    e = lambda wb_id, name, sid: m.enabled(cfg, m.Cand(sid, wb_id, name, "Workbook", "t", "PDF", None, False))
    check("allow-list matches workbook id or (case-insensitive) name, and subscription id",
          e("T11TEST-WB", "x", "s") and not e("other", "x", "s")
          and m.enabled(fresh_cfg(workbooks=frozenset({"cfd accounts"})), m.Cand("s", "w", "CFD Accounts", "Workbook", "t", "PDF", None, False))
          and m.enabled(fresh_cfg(workbooks=frozenset(), sub_ids=frozenset({"s9"})), m.Cand("S9", "w", "n", "Workbook", "t", "PDF", None, False)))


def test_structural():
    base = c6.mk_gate()
    pairs = [("ready live", c6.mk_gate()), ("unknown pipeline", c6.mk_gate(pipes={"d1": {"P1": False}})),
             ("mapping NotReady", c6.mk_gate(wb={"w": ("NotReady", 1)})),
             ("source list short", c6.mk_gate(wb={"w": ("Ready", 2)})),
             ("extract without target", c6.mk_gate(sources={"w": [{"id": "d1", "name": "", "status": "Ready", "kind": "Extract", "target": None}]})),
             ("unknown kind", c6.mk_gate(sources={"w": [{"id": "d1", "name": "", "status": "Ready", "kind": "Unknown", "target": None}]})),
             ("nothing to gate", c6.mk_gate(pipes={}, sources={"w": [{"id": "d1", "name": "", "status": "Ready", "kind": "Live", "target": None}]}))]
    ok = True
    for name, g in pairs:
        s_ok = g.structural("w")[0]
        c_ok, c_kind, _ = g.check("w")
        ok &= (s_ok == (c_kind != "blocked")) if name != "ready live" else (s_ok and c_ok)
    check("Gate.structural agrees with Gate.check on every mapping-level block (blocked <=> not structural)", ok)
    check("Gate.structural ignores today's ETL state (failed latest run is still structurally fine)",
          base.structural("w")[0] and c6.mk_gate(etl={"P1": (dt.datetime(2026, 10, 22, 10, 0), False)}).structural("w")[0])


# ---------- t11 flow ----------
def test_flow(cur, today):
    cap = c6.LogCapture()
    for lg in (m.log, t6.log):
        lg.addHandler(cap)
        lg.setLevel(logging.INFO)
    try:
        srv = FakeServer()

        # empty allow-list: nothing, and Tableau is never touched
        add_sub(cur, "T11TEST-A")
        srv.add("T11TEST-A")
        ctx, mail = run_t11(cur, NoTableau(), today, workbooks=frozenset())
        check("empty allow-list: nothing is taken over and Tableau is not touched (no sign-in needed)",
              ctx.n["skip_not_enabled"] >= 1 and ctx.n["paused"] == 0 and backup_rows(cur, "T11TEST-A") == [])

        # dry run default
        files_before = set((TMP / "backup").glob("*")) if (TMP / "backup").exists() else set()
        before = others_checksum(cur)
        ctx, mail = run_t11(cur, srv, today, execute=False)
        check("dry run: would back up and pause, writes nothing to Tableau, the DWH or disk",
              ctx.n["paused"] == 1 and srv.subscriptions.updates == [] and backup_rows(cur, "T11TEST-A") == []
              and not srv.suspended("T11TEST-A") and mail.sent == []
              and (set((TMP / "backup").glob("*")) if (TMP / "backup").exists() else set()) == files_before)
        check("dry run: other rows unchanged", others_checksum(cur) == before)

        # execute: the backup row is COMMITTED (seen from a second connection) before the pause call
        seen = {}
        c2 = t4.connect().cursor()

        def hook(item):
            seen["rows"] = c2.execute("SELECT TakenOver, CAST(PausedAt AS datetime2) FROM Integration.Tableau_SubscriptionBackup "
                                      "WHERE Subscription_id = ?", item.id).fetchall()
        srv.subscriptions.hook = hook
        ctx, mail = run_t11(cur, srv, today)
        srv.subscriptions.hook = None
        r = seen.get("rows", [])
        check("execute: the backup row is committed (visible to another connection) before the pause call, PausedAt still empty",
              len(r) == 1 and r[0][0] and r[0][1] is None, str(r))
        rows = backup_rows(cur, "T11TEST-A")
        b = rows[0] if rows else None
        check("execute: subscription paused, PausedAt set, TakenOver=1, one row",
              ctx.n["paused"] == 1 and srv.suspended("T11TEST-A") and len(rows) == 1 and b[1] and b[2] is not None and b[3] is None)
        check("schema: no 'was suspended' column or pre-suspended CHECK on the backup table (D-046, R4)",
              cur.execute("SELECT COUNT(*) FROM sys.columns WHERE object_id = OBJECT_ID('Integration.Tableau_SubscriptionBackup') AND name LIKE 'Was%'").fetchone()[0] == 0
              and cur.execute("SELECT COUNT(*) FROM sys.check_constraints WHERE parent_object_id = OBJECT_ID('Integration.Tableau_SubscriptionBackup') "
                              "AND name LIKE '%PreSusp%'").fetchone()[0] == 0)
        check("backup holds the definition: subject, message, schedule id and name, page size, orientation, flags, owner id, target",
              b[6] == "Daily report" and b[7] == "Hello <b>team</b>" and b[8] == "SCHED-1" and b[9] == "Daily 0900" and b[10] == "A4"
              and b[11] == "Landscape" and b[12] and not b[13] and b[14] == "USER-1" and b[15] == TARGET and b[16] == "Workbook")
        files = sorted((TMP / "backup").glob("*T11TEST-A.json"))
        js = json.loads(files[0].read_text(encoding="utf-8")) if files else {}
        check("backup JSON written to the backup directory with the same definition and no address",
              len(files) == 1 and js.get("subject") == "Daily report" and js.get("message") == "Hello <b>team</b>" and js.get("user_id") == "USER-1"
              and "backed_up_at" in js and "@" not in files[0].read_text(encoding="utf-8")
              and not any(k.startswith("was") for k in js))
        it = srv.subscriptions.items["T11TEST-A"]
        check("pause changed nothing but suspended (subject, message, formats, page size, orientation, schedule, target)",
              it.subject == "Daily report" and it.message == "Hello <b>team</b>" and it.attach_pdf and not it.attach_image
              and it.page_size_option == "A4" and it.page_orientation == "Landscape" and it.schedule_id == "SCHED-1" and it.target.id == TARGET)
        ctx, mail = run_t11(cur, srv, today)
        check("rerun: already paused, no second backup, no second update", ctx.n["already_paused"] == 1 and ctx.n["paused"] == 0
              and len(backup_rows(cur, "T11TEST-A")) == 1 and len(srv.subscriptions.updates) == 1 and alerts(mail) == [])

        # re-pause: someone resumes it
        srv.resume("T11TEST-A")
        ctx, mail = run_t11(cur, srv, today)
        rows = backup_rows(cur, "T11TEST-A")
        check("resumed by a human: paused again, counted 'repaused', LastRepausedAt and RepauseCount recorded, still one backup row",
              ctx.n["repaused"] == 1 and ctx.n["paused"] == 0 and srv.suspended("T11TEST-A") and len(rows) == 1 and rows[0][4] is not None
              and rows[0][5] == 1)
        check("re-pause is listed in ONE alert mail to the team path (local transport), with the subscription id",
              len(alerts(mail)) == 1 and b"T11TEST-A" in alerts(mail)[0][0] and ctx.n["alerts"] == 1
              and "repaused=1" in m.summary_line(ctx))
        check("alert mail goes through t6's allow-list/transport rules: local sink, no real address",
              alerts(mail)[0][2] == [t6.LOCAL_TO] and b"example.com" not in alerts(mail)[0][0])

        # D-046: already suspended in Tableau (by anyone) is taken over like any other: backup row, no change in Tableau
        add_sub(cur, "T11TEST-B")
        srv.add("T11TEST-B", suspended=True)
        n_upd = len(srv.subscriptions.updates)
        ctx, mail = run_t11(cur, srv, today, execute=False, workbooks=frozenset(), sub_ids=frozenset({"t11test-b"}))
        check("dry run: an already suspended subscription would be taken over (no row, no change)",
              ctx.n["paused"] == 1 and backup_rows(cur, "T11TEST-B") == [] and len(srv.subscriptions.updates) == n_upd
              and srv.suspended("T11TEST-B") and any("already suspended" in x for x in cap.lines))
        ctx, mail = run_t11(cur, srv, today, workbooks=frozenset(), sub_ids=frozenset({"t11test-b"}))
        rows = backup_rows(cur, "T11TEST-B")
        check("already suspended in Tableau: TAKEN OVER (backup row, PausedAt set at once), no update call to Tableau, still suspended",
              ctx.n["paused"] == 1 and ctx.n["taken_suspended"] == 1 and len(rows) == 1 and rows[0][1] and rows[0][2] is not None
              and len(srv.subscriptions.updates) == n_upd and srv.suspended("T11TEST-B") and "taken_suspended=1" in m.summary_line(ctx))
        ctx, mail = run_t11(cur, srv, today)
        check("rerun: the taken-over (formerly suspended) subscription stays as it is", ctx.n["already_paused"] >= 1 and len(backup_rows(cur, "T11TEST-B")) == 1
              and len(srv.subscriptions.updates) == n_upd)
        check("DB guard: at most one open backup row per subscription (unique index)", _raises(
            cur, "INSERT Integration.Tableau_SubscriptionBackup (Subscription_id, Workbook_id, TargetType, TargetId, OwnerUserId, AttachPdf, "
                 "AttachImage, TakenOver) VALUES ('T11TEST-A', 'w', 'Workbook', 't', 'u', 1, 0, 1)"))

        # scope rules
        add_sub(cur, "T11TEST-C")
        srv.add("T11TEST-C")
        ctx, _ = run_t11(cur, srv, today, gate=FakeGate(structural=(False, "source NoTables")))
        check("workbook t6 would not send: skipped with the reason, nothing paused", ctx.n["skip_not_ready"] >= 1
              and not srv.suspended("T11TEST-C") and backup_rows(cur, "T11TEST-C") == [])
        check("the skip reason is logged", any("source NoTables" in x for x in cap.lines))
        ctx, _ = run_t11(cur, srv, today, gate=FakeGate(structural=(False, "x")), force_notready=True, workbooks=frozenset(), sub_ids=frozenset())
        check("--force-notready without an allow-list entry still takes nothing", not srv.suspended("T11TEST-C"))
        ctx, _ = run_t11(cur, srv, today, gate=FakeGate(count=1))
        check("single data source workbook: out of scope, skipped", ctx.n["skip_single_source"] >= 1 and not srv.suspended("T11TEST-C"))
        add_sub(cur, "T11TEST-D", age_h=30)
        srv.add("T11TEST-D")
        ctx, _ = run_t11(cur, srv, today)
        check("T2 mapping older than 26 h: new takeover skipped", ctx.n["skip_stale"] == 1 and not srv.suspended("T11TEST-D"))
        add_sub(cur, "T11TEST-E", target="other-target")
        srv.add("T11TEST-E")
        ctx, _ = run_t11(cur, srv, today)
        check("target differs from the T2 sync: skipped", ctx.n["skip_definition_changed"] == 1 and not srv.suspended("T11TEST-E"))
        ctx, _ = run_t11(cur, srv, today, gate=FakeGate(structural=(False, "NotReady")), force_notready=True)
        check("named in the allow-list AND --force-notready: taken over although not sendable (logged as FORCED)",
              srv.suspended("T11TEST-C") and any("FORCED" in x for x in cap.lines))

        # pause failure and recovery, crash windows
        add_sub(cur, "T11TEST-F")
        srv.add("T11TEST-F")
        srv.subscriptions.fail_update = RuntimeError("503 from %s" % SECRET)
        ctx, mail = run_t11(cur, srv, today, workbooks=frozenset(), sub_ids=frozenset({"t11test-f"}))
        rows = backup_rows(cur, "T11TEST-F")
        check("pause fails: failed=1, exit code 1, backup row exists with PausedAt empty, alert event, not recorded as paused",
              ctx.n["failed"] == 1 and m.exit_code(ctx) == 1 and len(rows) == 1 and rows[0][2] is None and not srv.suspended("T11TEST-F")
              and any(e["code"] == "pause_failed" for e in ctx.events))
        srv.subscriptions.fail_update = None
        ctx, _ = run_t11(cur, srv, today, workbooks=frozenset(), sub_ids=frozenset())
        rows = backup_rows(cur, "T11TEST-F")
        check("next run (allow-list empty): the pending takeover is completed as a first pause, not a re-pause, one backup row",
              ctx.n["paused"] == 1 and ctx.n["repaused"] == 0 and srv.suspended("T11TEST-F") and len(rows) == 1 and rows[0][2] is not None)
        add_sub(cur, "T11TEST-G")
        srv.add("T11TEST-G")
        run_t11(cur, srv, today, workbooks=frozenset(), sub_ids=frozenset({"t11test-g"}))
        cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET PausedAt = NULL WHERE Subscription_id = 'T11TEST-G'")   # crash after the pause
        ctx, _ = run_t11(cur, srv, today)
        check("crash after the pause call: next run finds it suspended and confirms PausedAt (no second pause call)",
              ctx.n["confirmed"] == 1 and backup_rows(cur, "T11TEST-G")[0][2] is not None
              and sum(1 for u in srv.subscriptions.updates if u[0] == "T11TEST-G") == 1)

        # deleted on Tableau
        add_sub(cur, "T11TEST-H")
        srv.add("T11TEST-H")
        run_t11(cur, srv, today, workbooks=frozenset(), sub_ids=frozenset({"t11test-h"}))
        del srv.subscriptions.items["T11TEST-H"]
        ctx, _ = run_t11(cur, srv, today)
        check("subscription deleted in Tableau: logged as gone, no error, no pause attempt, exit code 0",
              ctx.n["gone"] == 1 and ctx.n["failed"] == 0 and m.exit_code(ctx) == 0)

        # restore
        order = {}

        def hook2(item):
            order["restored_before_resume"] = c2.execute("SELECT CAST(RestoredAt AS datetime2) FROM Integration.Tableau_SubscriptionBackup "
                                                         "WHERE Subscription_id = 'T11TEST-A'").fetchone()[0] is not None
        srv.subscriptions.hook = hook2
        ctx = m.Ctx(fresh_cfg(), m.TableauAdapter(lambda: srv), None, cur, today)
        m.restore(ctx, ["T11TEST-A"], None)
        srv.subscriptions.hook = None
        rows = backup_rows(cur, "T11TEST-A")
        check("--restore: marked restored FIRST, then resumed in Tableau; definition unchanged",
              order.get("restored_before_resume") is True and not srv.suspended("T11TEST-A") and rows[0][3] is not None and ctx.n["restored"] == 1
              and srv.subscriptions.items["T11TEST-A"].message == "Hello <b>team</b>")
        ctx, _ = run_t11(cur, srv, today)
        check("restored subscription is not re-paused and not taken over again", not srv.suspended("T11TEST-A")
              and ctx.n["skip_restored"] == 1 and len(backup_rows(cur, "T11TEST-A")) == 1)
        ctx = m.Ctx(fresh_cfg(execute=False), m.TableauAdapter(NoTableau()), None, cur, today)
        ctx, _ = run_t11(cur, srv, today, retake_restored=True)
        rows = backup_rows(cur, "T11TEST-A")
        check("--retake-restored: taken over again with a NEW backup row (history kept)",
              srv.suspended("T11TEST-A") and len(rows) == 2 and rows[0][3] is not None and rows[1][3] is None)
        ctx = m.Ctx(fresh_cfg(), m.TableauAdapter(lambda: srv), None, cur, today)
        m.restore(ctx, [], WB)
        open_left = cur.execute("SELECT COUNT(*) FROM Integration.Tableau_SubscriptionBackup WHERE Subscription_id LIKE ? AND RestoredAt IS NULL "
                                "AND Workbook_id = ?", LIKE, WB).fetchone()[0]
        check("--restore-all-for-workbook: every taken-over subscription of the workbook is restored and resumed",
              open_left == 0 and not srv.suspended("T11TEST-A") and not srv.suspended("T11TEST-C") and not srv.suspended("T11TEST-F"))
        check("--restore simply un-suspends, also a subscription that was suspended before the takeover (no original state kept, D-046)",
              not srv.suspended("T11TEST-B") and backup_rows(cur, "T11TEST-B")[0][3] is not None)
        add_sub(cur, "T11TEST-N")
        srv.add("T11TEST-N", suspended=True)
        ctx = m.Ctx(fresh_cfg(), m.TableauAdapter(lambda: srv), None, cur, today)
        m.restore(ctx, ["T11TEST-N"], None)
        check("--restore on a subscription the script never took over (no backup row): refused, nothing changed in Tableau",
              ctx.n["failed"] == 1 and srv.suspended("T11TEST-N"))
        ctx = m.Ctx(fresh_cfg(execute=False), m.TableauAdapter(NoTableau()), None, cur, today)
        run_cnt = cur.execute("SELECT COUNT(*) FROM Integration.Tableau_SubscriptionBackup WHERE Subscription_id LIKE ?", LIKE).fetchone()[0]
        srv2 = FakeServer()
        m.restore(ctx, ["T11TEST-A"], None)
        check("--restore without --execute (dry run): no Tableau call, no DWH change",
              cur.execute("SELECT COUNT(*) FROM Integration.Tableau_SubscriptionBackup WHERE Subscription_id LIKE ? AND RestoredAt IS NULL",
                          LIKE).fetchone()[0] == 0 and run_cnt >= 1 and srv2.subscriptions.updates == [])

        # halt on a definition change (server clears the message on update)
        for sid in ("T11TEST-M1", "T11TEST-M2"):
            add_sub(cur, sid, wb="T11TEST-WB")
            srv.add(sid)
        srv.subscriptions.drop_message = True
        ctx, mail = run_t11(cur, srv, today, workbooks=frozenset(), sub_ids=frozenset({"t11test-m1", "t11test-m2"}))
        srv.subscriptions.drop_message = False
        touched = [u for u in srv.subscriptions.updates if u[0] in ("T11TEST-M1", "T11TEST-M2")]
        check("pause changes another field (message cleared): run HALTS after the first, second untouched, exit 1, alert",
              ctx.halted and len(touched) == 1 and ctx.n["failed"] == 1 and m.exit_code(ctx) == 1
              and any(e["code"] == "definition_changed" and "message" in e["text"] for e in ctx.events) and len(alerts(mail)) == 1)
        check("halted pause is not recorded as paused (PausedAt empty) so t6 will not send it",
              all(r[2] is None for sid in ("T11TEST-M1", "T11TEST-M2") for r in backup_rows(cur, sid)))

        # no secrets
        text = "\n".join(cap.lines)
        files_text = " ".join(f.read_text(encoding="utf-8") for f in (TMP / "backup").glob("*.json"))
        dwh = " ".join(str(c) for r in cur.execute("SELECT Subject, Message, OwnerUserId FROM Integration.Tableau_SubscriptionBackup "
                                                   "WHERE Subscription_id LIKE ?", LIKE).fetchall() for c in r)
        check("no recipient address in the log, the backup JSON or the backup table", SECRET not in text and SECRET not in files_text
              and "@" not in files_text and SECRET not in dwh)
    finally:
        for lg in (m.log, t6.log):
            lg.removeHandler(cap)


def _raises(cur, sql):
    import pyodbc
    try:
        cur.execute(sql)
        return False
    except pyodbc.Error:
        return True


# ---------- t6 + t11 together ----------
def test_interaction(cur, today):
    srv = FakeServer()
    allow = dict(workbooks=frozenset(), sub_ids=frozenset({"t11test-i1", "t11test-i2", "t11test-i3", "t11test-i4"}))
    for sid in ("T11TEST-I1", "T11TEST-I2", "T11TEST-I3", "T11TEST-I4"):
        add_sub(cur, sid)
    srv.add("T11TEST-I1")
    srv.add("T11TEST-I2")
    srv.add("T11TEST-I3")
    srv.add("T11TEST-I4", suspended=True)       # already suspended in Tableau before the takeover (D-046: no special category)
    cur.execute("UPDATE Integration.Tableau_Subscription SET IsSuspended = 1 WHERE Subscription_id = 'T11TEST-I4'")

    ids = ["T11TEST-I1", "T11TEST-I2", "T11TEST-I3", "T11TEST-I4"]
    s, rep, _ = run_t6(cur, srv, today, ids)
    check("before takeover: t6 sends nothing (no takeover row yet, suspended or not)",
          rep == [] and s["kinds"]["not_taken_over"] == 4 and logrows(cur, "T11TEST-I4") == [])
    n_upd = len(srv.subscriptions.updates)
    ctx, _ = run_t11(cur, srv, today, **allow)
    check("t11 takes over I1-I3 (paused) AND the already suspended I4 (no change in Tableau)", ctx.n["paused"] == 4 and ctx.n["taken_suspended"] == 1
          and len(backup_rows(cur, "T11TEST-I4")) == 1 and len(srv.subscriptions.updates) == n_upd + 3 and srv.suspended("T11TEST-I4"))
    # a human resumes I2 before t6's first send, and I3 after it was sent
    s, rep, _ = run_t6(cur, srv, today, ["T11TEST-I1", "T11TEST-I3", "T11TEST-I4"])
    check("taken over (suspended in Tableau): t6 sends it, once; the formerly suspended I4 is sent too", s["kinds"]["sent"] == 3 and len(rep) == 3
          and logrows(cur, "T11TEST-I1") == ["Sent"] and logrows(cur, "T11TEST-I4") == ["Sent"])
    s, rep, _ = run_t6(cur, srv, today, ["T11TEST-I4"])
    check("the formerly suspended I4 is not sent a second time the same day", rep == [] and s["kinds"]["already_sent"] == 1 and logrows(cur, "T11TEST-I4") == ["Sent"])
    srv.resume("T11TEST-I2")
    srv.resume("T11TEST-I3")
    s, rep, ctx6 = run_t6(cur, srv, today, ids)
    check("resumed by a human (before the send): t6 does NOT send it (Tableau may), nothing claimed",
          rep == [] and s["kinds"]["not_paused"] == 1 and logrows(cur, "T11TEST-I2") == []
          and any(e["code"] == "not_paused" for e in ctx6.events))
    ctx, mail = run_t11(cur, srv, today, **allow)
    check("t11 pauses both resumed subscriptions again (repaused=2) and alerts", ctx.n["repaused"] == 2 and srv.suspended("T11TEST-I2")
          and srv.suspended("T11TEST-I3") and len(alerts(mail)) == 1)
    s, rep, _ = run_t6(cur, srv, today, ids)
    check("after the re-pause: I2 is NOT sent that day (repaused_today) and I3 / I4 (already sent) are not sent twice",
          rep == [] and s["kinds"]["repaused_today"] == 2 and logrows(cur, "T11TEST-I2") == [] and logrows(cur, "T11TEST-I3") == ["Sent"]
          and logrows(cur, "T11TEST-I4") == ["Sent"])
    # next day: the repaused flag no longer applies, I2 is sent
    cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET LastRepausedAt = DATEADD(day, -1, LastRepausedAt) "
                "WHERE Subscription_id = 'T11TEST-I2'")
    s, rep, _ = run_t6(cur, srv, today, ["T11TEST-I2"])
    check("the day after a re-pause the subscription is sent again", s["kinds"]["sent"] == 1 and len(rep) == 1)
    # restore: t6 stops, Tableau sends again
    ctx = m.Ctx(fresh_cfg(), m.TableauAdapter(lambda: srv), None, cur, today)
    m.restore(ctx, ["T11TEST-I1"], None)
    check("F-4 end to end: I1 was sent by t6 today, so a plain --restore is refused (still suspended, still taken over)",
          ctx.n["restore_refused"] == 1 and srv.suspended("T11TEST-I1") and ctx.n["restored"] == 0)
    ctx = m.Ctx(fresh_cfg(allow_restore_after_send=True), m.TableauAdapter(lambda: srv), None, cur, today)
    m.restore(ctx, ["T11TEST-I1"], None)
    cur.execute("DELETE FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id = 'T11TEST-I1'")
    s, rep, _ = run_t6(cur, srv, today, ["T11TEST-I1"])
    check("restored: resumed in Tableau, t6 no longer sends it", not srv.suspended("T11TEST-I1") and rep == [] and s["kinds"]["not_taken_over"] == 1)
    ctx = m.Ctx(fresh_cfg(allow_restore_after_send=True), m.TableauAdapter(lambda: srv), None, cur, today)
    m.restore(ctx, ["T11TEST-I4"], None)
    check("restore of the formerly suspended I4 just un-suspends it (it is no longer suspended in Tableau)", not srv.suspended("T11TEST-I4") and ctx.n["restored"] == 1)
    # deleted on Tableau: dropped by t6 without an error
    del srv.subscriptions.items["T11TEST-I2"]
    cur.execute("DELETE FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id = 'T11TEST-I2'")
    s, rep, _ = run_t6(cur, srv, today, ["T11TEST-I2"])
    check("deleted on Tableau: t6 stops sending (404 -> deleted_on_tableau), exit code 0", rep == [] and s["kinds"]["deleted_on_tableau"] == 1
          and t6.exit_code(None, s) == 0)
    cur.execute("UPDATE Integration.Tableau_Subscription SET Active_Flag = 0 WHERE Subscription_id = 'T11TEST-I2'")   # what the next T2 sync does
    s, rep, _ = run_t6(cur, srv, today, ["T11TEST-I2"])
    check("deleted subscription after the next T2 sync (Active_Flag 0): no longer a candidate at all", rep == [] and sum(s["kinds"].values()) == 0)


# ---------- R5: cap on new takeovers, restore after a same-day send, SQL files ----------
WBCAP = "T11TEST-WB-CAP"


def capgate():
    g = FakeGate()
    g.wb = {WBCAP: ("Ready", 2)}
    return g


def split_go(text):
    return [b for b in re.split(r"^\s*GO\s*$", text, flags=re.M) if b.strip()]


def run_sql_file(cur, name):
    """Run a scripts/sql file batch by batch like SSMS (GO splits; an error in one batch does not stop the next, SET NOEXEC does).
    Returns the error messages."""
    import pyodbc
    errs = []
    for b in split_go((Path(__file__).resolve().parent / "sql" / name).read_text(encoding="utf-8")):
        try:
            cur.execute(b)
            while cur.nextset():
                pass
        except pyodbc.Error as e:
            errs.append(str(e))
    return errs


def test_r5(cur, today):
    # ---- F-2: per-run cap on NEW takeovers
    srv = FakeServer()
    ids = ["T11TEST-CAP%02d" % i for i in range(12)]
    for sid in ids:
        add_sub(cur, sid, wb=WBCAP)
        srv.add(sid)
    allow = dict(workbooks=frozenset({WBCAP.lower()}))
    ctx, mail = run_t11(cur, srv, today, gate=capgate(), execute=False, **allow)
    check("F-2 cap (dry run): 12 matches, default cap 10 -> 10 would be taken over, 2 skipped", ctx.n["paused"] == 10 and ctx.n["skip_cap"] == 2)
    ctx, mail = run_t11(cur, srv, today, gate=capgate(), **allow)
    opened = cur.execute("SELECT COUNT(*) FROM Integration.Tableau_SubscriptionBackup WHERE Subscription_id LIKE 'T11TEST-CAP%'").fetchone()[0]
    check("F-2 cap: only 10 of the 12 are taken over in one run (rows and Tableau pauses), 2 wait", ctx.n["paused"] == 10 and opened == 10
          and sum(1 for sid in ids if srv.suspended(sid)) == 10 and ctx.n["skip_cap"] == 2)
    check("F-2 cap: the skipped ones are in the RUN SUMMARY and raise ONE 'takeover_cap' alert event",
          "skip_cap=2" in m.summary_line(ctx) and [e["code"] for e in ctx.events].count("takeover_cap") == 1 and len(alerts(mail)) == 1)
    ctx, mail = run_t11(cur, srv, today, gate=capgate(), **allow)
    check("F-2 cap: the next run takes the remaining 2 (the 10 are only confirmed, not counted again)", ctx.n["paused"] == 2 and ctx.n["skip_cap"] == 0
          and all(srv.suspended(sid) for sid in ids))
    for sid in ids[:3]:
        cur.execute("DELETE FROM Integration.Tableau_SubscriptionBackup WHERE Subscription_id = ?", sid)
        srv.resume(sid)
    ctx, mail = run_t11(cur, srv, today, gate=capgate(), cfg=fresh_cfg(max_new=2, **allow))
    check("F-2 cap is configurable (max_new=2): 3 candidates -> 2 taken over, 1 skipped", ctx.n["paused"] == 2 and ctx.n["skip_cap"] == 1)
    ctx, mail = run_t11(cur, srv, today, gate=capgate(), cfg=fresh_cfg(max_new=0, **allow))
    check("F-2 cap 0 takes nobody new (but keeps the taken-over ones paused)", ctx.n["paused"] == 0 and ctx.n["skip_cap"] == 1)

    # ---- F-4: restore after a send that already happened today
    srv = FakeServer()
    for sid in ("T11TEST-RS1", "T11TEST-RS2", "T11TEST-RS3"):
        add_sub(cur, sid)
        srv.add(sid)
    run_t11(cur, srv, today, workbooks=frozenset(), sub_ids=frozenset({"t11test-rs1", "t11test-rs2", "t11test-rs3"}))
    cur.execute("INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status, SentAt, Message) VALUES ('T11TEST-RS1', 'Sent', SYSDATETIMEOFFSET(), 'x')")
    cur.execute("INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status, Message) VALUES ('T11TEST-RS2', 'Claimed', 'x')")
    cur.execute("INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status, ClaimedAt, SentAt, Message) VALUES "
                "('T11TEST-RS3', 'Sent', DATEADD(day, -1, SYSDATETIMEOFFSET()), DATEADD(day, -1, SYSDATETIMEOFFSET()), 'yesterday')")
    cap = c6.LogCapture()
    m.log.addHandler(cap)
    m.log.setLevel(logging.INFO)
    try:
        n_upd = len(srv.subscriptions.updates)
        ctx = m.Ctx(fresh_cfg(), m.TableauAdapter(lambda: srv), None, cur, today)
        m.restore(ctx, ["T11TEST-RS1", "T11TEST-RS2"], None)
        open_rows = cur.execute("SELECT COUNT(*) FROM Integration.Tableau_SubscriptionBackup WHERE Subscription_id IN ('T11TEST-RS1', 'T11TEST-RS2') "
                                "AND RestoredAt IS NULL").fetchone()[0]
        check("F-4: --restore after a Sent / Claimed row TODAY is REFUSED: failure count, still suspended, backup row still open, no Tableau call",
              ctx.n["restore_refused"] == 2 and ctx.n["failed"] == 2 and ctx.n["restored"] == 0 and open_rows == 2
              and srv.suspended("T11TEST-RS1") and srv.suspended("T11TEST-RS2") and len(srv.subscriptions.updates) == n_upd
              and any("RESTORE REFUSED" in x and "second mail" in x for x in cap.lines) and "refused_sent_today=2" in m.restore_summary(ctx))
        ctx = m.Ctx(fresh_cfg(execute=False), m.TableauAdapter(NoTableau()), None, cur, today)
        m.restore(ctx, ["T11TEST-RS1"], None)
        check("F-4: the dry run reports the refusal too", ctx.n["restore_refused"] == 1 and ctx.n["failed"] == 1)
        ctx = m.Ctx(fresh_cfg(), m.TableauAdapter(lambda: srv), None, cur, today)
        m.restore(ctx, ["T11TEST-RS3"], None)
        check("F-4: a send YESTERDAY does not block the restore", ctx.n["restored"] == 1 and ctx.n["restore_refused"] == 0 and not srv.suspended("T11TEST-RS3"))
        ctx = m.Ctx(fresh_cfg(allow_restore_after_send=True), m.TableauAdapter(lambda: srv), None, cur, today)
        m.restore(ctx, ["T11TEST-RS1"], None)
        check("F-4: --allow-restore-after-send overrides: warned, then restored and resumed",
              ctx.n["restored"] == 1 and ctx.n["restore_refused"] == 0 and not srv.suspended("T11TEST-RS1")
              and any("already sent it today" in x for x in cap.lines))
    finally:
        m.log.removeHandler(cap)
    flag = NS(restore=["x"], restore_all_for_workbook=None, execute=False, ses=False, workbooks=None, subscription_ids=None, force_notready=False,
              retake_restored=False, max_mapping_age_h=26.0, owner_user_id=None, max_new=None, allow_restore_after_send=True)
    check("F-4: the override is a command line flag (--allow-restore-after-send)", m.build_config(flag, {}).allow_restore_after_send is True
          and "--allow-restore-after-send" in open(m.__file__, encoding="utf-8").read())


def test_t4_counts_takeover(cur, today):
    """Runs the checks below on a dev DWH that has NO open takeover row, whatever is really taken over: inside a transaction that is
    always rolled back, the real open backup rows are marked restored first (the unique index allows one open row per subscription,
    and "nothing taken over" is the premise of the first checks). Everything this function does, including the fixture row, is rolled back."""
    cur.execute("BEGIN TRAN")
    try:
        cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET RestoredAt = SYSDATETIMEOFFSET() WHERE RestoredAt IS NULL")
        known_pipelines(cur)   # dev's ETL log has no row for some registered pipelines; inside this rolled-back transaction they get one (D-077)
        _t4_counts_takeover(cur, today)
    finally:
        cur.execute("ROLLBACK")


def _t4_counts_takeover(cur, today):
    """t4's OWN code path (CAND_TEMPLATE via load_targets, due_workbooks) on the real dev mapping: scope 'takeover' counts only taken-over
    subscriptions; scope 'all' counts every active subscription, suspended or not (D-046). No fixture re-implements the SCOPES SQL."""
    sql_tk = t4.CAND_TEMPLATE % t4.SCOPES["takeover"]
    check("t4 takeover-scope SQL counts taken-over subscriptions as active (else their extracts would stop refreshing)",
          "Tableau_SubscriptionBackup" in sql_tk and "TakenOver = 1" in sql_tk and "RestoredAt IS NULL" in sql_tk)
    check("t4 default SQL (scope all) has no IsSuspended condition (D-046)", "IsSuspended" not in t4.CAND_SQL)
    check("t4 CAND_SQL still runs", isinstance(cur.execute(t4.CAND_SQL.replace("WHERE r.RefreshKind = 'Extract'", "WHERE 1 = 0")).fetchall(), list))
    all_t, _ = t4.load_targets(cur, "all")
    pil_t, pil_x = t4.load_targets(cur, "takeover")
    check("t4 pilot scope with nothing taken over refreshes nothing (empty takeover set = do nothing, not everything)", pil_t == [])
    check("t4 pilot scope: the real extract targets are excluded with the reason 'no taken-over subscription'",
          len(pil_x) >= 1 and any("no taken-over subscription" in why for _, why in pil_x.values()))
    # D-046 on the real data: a target whose workbooks only have SUSPENDED subscriptions is refreshed in scope 'all'
    susp_only = []
    for t in all_t:
        tot = cur.execute("SELECT COUNT(*), SUM(CAST(IsSuspended AS int)) FROM Integration.Tableau_Subscription WHERE Active_Flag = 1 AND Workbook_id IN (%s)"
                          % ",".join("?" * len(t.workbooks)), [w for w, _ in t.workbooks]).fetchone()
        if tot[0] and tot[0] == tot[1]:
            susp_only.append(t.id)
    if susp_only:
        check("t4 scope 'all' (load_targets): extracts behind workbooks with only suspended subscriptions ARE refreshed (D-046)", True, "%d target(s)" % len(susp_only))
    else:
        print("SKIP  t4 D-046 suspended-only target (the real dev mapping has none right now)")
    if not all_t:
        check("t4 pilot scope fence (needs at least one refresh target in the real mapping)", False, "no targets")
        return
    wbid, _n = all_t[0].workbooks[0]
    other_ids = {w for t in all_t for w, _ in t.workbooks}
    real = cur.execute("SELECT TOP 1 Subscription_id FROM Integration.Tableau_Subscription WHERE Workbook_id = ? AND Active_Flag = 1", wbid).fetchone()
    try:
        cur.execute("INSERT Integration.Tableau_SubscriptionBackup (Subscription_id, Workbook_id, TargetType, TargetId, OwnerUserId, AttachPdf, "
                    "AttachImage, TakenOver, PausedAt, RunId) VALUES (?, ?, 'Workbook', 't', 'u', 1, 0, 1, SYSDATETIMEOFFSET(), "
                    "'T11TEST-T4FENCE')", real[0], wbid)
        one_t, _ = t4.load_targets(cur, "takeover")
        wbs_in = {w for t in one_t for w, _ in t.workbooks}
        check("t4 pilot scope: only extracts behind the taken-over workbook are refreshed (others in the all-set stay untouched)",
              wbid in wbs_in and wbs_in <= {wbid} and len(other_ids) >= 1 and (len(other_ids) == 1 or len(wbs_in) < len(other_ids)),
              "%d workbook(s) of %d" % (len(wbs_in), len(other_ids)))
        check("t4 scope 'all' is unchanged by the takeover row", {t.id for t in t4.load_targets(cur, "all")[0]} == {t.id for t in all_t})
        cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET RestoredAt = SYSDATETIMEOFFSET() WHERE RunId = 'T11TEST-T4FENCE'")
        check("t4 pilot scope: once restored, the workbook no longer counts (nothing refreshed)", t4.load_targets(cur, "takeover")[0] == [])
        cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET RestoredAt = NULL WHERE RunId = 'T11TEST-T4FENCE'")
        due_tk = t4.due_workbooks(cur, today, "takeover")
        check("t4 due_workbooks(scope takeover) only ever returns taken-over workbooks", due_tk <= {wbid})
        due_t, _ = t4.load_targets(cur, "takeover", due_wbs=set())
        check("t4 load_targets with an empty 'due' set refreshes nothing, whatever is taken over", due_t == [])
    finally:
        cur.execute("DELETE FROM Integration.Tableau_SubscriptionBackup WHERE RunId = 'T11TEST-T4FENCE'")


def main():
    os.environ['EVENTLOG_DISABLED'] = '1'
    os.environ.setdefault('LOG_DIR', tempfile.mkdtemp(prefix='logs_'))   # tests never write the real out/*.log
    os.environ['LASTRUN_DIR'] = tempfile.mkdtemp(prefix='lastrun_')   # tests must not write real last-run markers
    os.environ['OPS_STATE_DIR'] = os.environ['LASTRUN_DIR']
    for k in [k for k in os.environ if k.startswith(("SES_", "SMTP_"))] + ["SEND_ALLOWED_RECIPIENTS", "SEND_ALLOW_ALL_UAT", "SEND_FROM", "ALERT_TO", "MAIL_TRANSPORT", "ADDR_HASH_SALT"]:
        os.environ.pop(k, None)
    import smtplib
    real_smtp = smtplib.SMTP

    def loopback_only(host, *a, **kw):
        raise AssertionError("test tried to open an SMTP connection to %r" % host)
    smtplib.SMTP = loopback_only
    conn = t4.connect()
    cur = conn.cursor()
    cur.execute("EXEC sp_set_session_context N'allow_fixture_delete', 1")   # R3 trigger: fixture cleanups only
    today = t4.dwh_today(cur)
    cleanup(cur)
    before = others_checksum(cur)
    try:
        test_real_request()
        test_config()
        test_structural()
        test_flow(cur, today)
        test_interaction(cur, today)
        test_r5(cur, today)
        test_t4_counts_takeover(cur, today)
    finally:
        cleanup(cur)
        smtplib.SMTP = real_smtp
    check("other rows of Tableau_Subscription / Tableau_SubscriptionBackup / Tableau_SubscriptionSendLog unchanged", others_checksum(cur) == before)
    check("fixture rows cleaned up", sum(cur.execute("SELECT COUNT(*) FROM Integration.%s WHERE Subscription_id LIKE ?" % t, LIKE).fetchone()[0]
                                         for t in ("Tableau_Subscription", "Tableau_SubscriptionBackup", "Tableau_SubscriptionSendLog")) == 0)
    print("\n%d failure(s)" % len(fails) if fails else "\nALL CHECKS PASSED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
