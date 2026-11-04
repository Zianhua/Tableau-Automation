"""T6 + T7: report readiness check, export, email and send log, per Tableau subscription.

WHICH subscriptions are sent (D-035): only those the script has TAKEN OVER, i.e. a row in
Integration.Tableau_SubscriptionBackup with TakenOver = 1, PausedAt set and RestoredAt NULL (written by t11_pause.py),
and only while Tableau really shows the subscription as suspended (the pause is in force, so Tableau itself sends
nothing). Suspended is not a special category (D-046, reverses D-023): t11 takes over every in-scope subscription, also
one that was already suspended; what must not be sent is deleted in Tableau by the user. A subscription with no takeover
row is left to Tableau's own schedule (counted as not_taken_over). A restored
subscription is not sent. If a human resumed a taken-over subscription it is not sent (not_paused), and not on the day
t11 paused it again (repaused_today), because the old subscription may already have mailed. A subscription deleted on
Tableau is simply dropped (T2 sets Active_Flag = 0; if the live lookup says 404 it is skipped as deleted_on_tableau).
--include-not-taken-over (dev / local transport only, refused with --ses) also processes subscriptions
that are not taken over, for local tests and dry-run inspection; it never sends for real.

For every such candidate whose workbook is Ready in vw_Tableau_WorkbookReadiness AND
whose data is fresh today (Singapore date, D-016), export the report in the subscription's own format and email it,
once per subscription per day.

Freshness gate (fail closed on anything unknown):
  - every required pipeline of every data source behind the workbook has, as its LATEST Integration.ETL_Master_Log row
    of the day, Success = 1 (same rule as t4_refresh.py);
  - every extract (refresh target) behind the workbook has a Succeeded Integration.Tableau_RefreshJob today that was
    submitted AFTER the latest end of the pipelines behind that target (same rule as t4_refresh.py); live sources need
    no refresh, only the ETL gate; a workbook with nothing to gate on at all is not sent.

Send log (T1_mapping_schema.sql section 8, claim-first, at-most-once; the only table this script writes):
  1. app lock for the run; at start Claimed rows older than the claim timeout (90 min, D-029) become TimedOut;
  2. INSERT 'Claimed' (the unique index allows one Claimed/Sent/TimedOut row per subscription per Singapore day);
  3. export, build the mail, send; on SMTP acceptance UPDATE to 'Sent' (a failure of that update is logged, never retried);
  4. 'Failed' ONLY for errors before the SMTP DATA command (export error, connect/TLS/auth, MAIL/RCPT refused, DATA not
     accepted, oversize). Anything after the DATA command is on the wire (timeout, drop, any reply other than 250,
     crash) is ambiguous and leaves 'Claimed' so a human decides. Sent is final.
Recipients are read from Tableau at send time and never stored; logs show only a count and a short hash.

Mail: default is the LOCAL test SMTP (localhost, preview_smtp.py writes .eml files). Real SES needs BOTH --ses and a
non-empty SEND_ALLOWED_RECIPIENTS (comma list; recipients not on it are dropped and logged), unless SEND_ALLOW_ALL_UAT=1 on the UAT Tableau host (D-050). Env (D-043, provider-neutral): MAIL_TRANSPORT=local|smtp,
SMTP_PROVIDER=ses|gmail|generic, SMTP_HOST, SMTP_PORT (587, STARTTLS), SMTP_USER, SMTP_PASSWORD, SEND_FROM (SES_SMTP_* are accepted aliases).
With local transport a delivered mail ends as 'Released' instead of 'Sent' (a test delivery must not use up the real
day's slot); tests of the Sent path switch that off.

Run safety (D-034): the shared Tableau PAT session lock (TABLEAU_PAT_SESSION) is taken before sign-in (busy -> skip, exit 0);
no new claims after 25 min; nothing is sent when the T2 sync last saw the subscription more than 26 h ago or the workbook changed
since; a NULL SubscriptionKeyHash is not sent; startup failures alert and exit 1.

Alerts (D-018): at the end of an --execute run, subscriptions not sent for a reason that needs a human (export
failure after 3 attempts, repeated SMTP failure, ambiguous send, TimedOut claim, near-duplicate subscription, oversize,
no recipient, send logged late) and, after the cutoff time (default 15:30 Singapore), everything still not ready, go
into ONE email to the owner (ALERT_TO / OWNER_EMAIL) through the same mail path and allow-list. Each (day, subscription,
reason) is alerted once (state file), because the job runs every 15 minutes.

Usage:
  python scripts/t6_send.py                              # dry run (default): readiness + candidate list, writes nothing
  python scripts/t6_send.py --dry-run --as-of-date 2026-10-14   # readiness as of another day (dry run only)
  python scripts/t6_send.py --execute                    # claim + export + send to the LOCAL test SMTP only
  python scripts/t6_send.py --execute --ses              # REAL sending (needs SES_* env and SEND_ALLOWED_RECIPIENTS)
Filters: --subscription-id <id> (repeatable), --workbook <name or id>. --export (dry run) also exports and sizes the mail.
Exit code 1 if anything went wrong that needs a human. Each run ends with one 'RUN SUMMARY' line.
Reads TABLEAU_PAT_NAME / TABLEAU_PAT_SECRET from .env; only one script may use the PAT at a time.
RENDER_AS_SUBSCRIBER=1 (D-063): every report is exported in a session that signs in as its subscriber, with the admin account in
TABLEAU_IMPERSONATOR_NAME / TABLEAU_IMPERSONATOR_PASSWORD, so per-user filters show that person's data. Everything else (describe, busy
checks, refreshes) stays on the PAT. A failed sign-in fails that export; the PAT is never used as a fallback.
"""
import argparse
import datetime as dt
import email.policy
import hashlib
import json
import logging
import os
import re
import smtplib
import ssl
import sys
import dataclasses
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

import pyodbc

sys.path.insert(0, str(Path(__file__).resolve().parent))
import subscription_mail as sm  # noqa: E402
import t4_refresh as t4  # noqa: E402
import schedule_due as sd  # noqa: E402
import ops  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOCK_NAME = "T6_send"
CLAIM_TIMEOUT_MIN = 90      # D-029, above the 1 h Task Scheduler limit
MAX_EXPORT_ATTEMPTS = 3     # export failures per subscription per day, then a human has to look
SMTP_ALERT_AFTER = 3        # pre-DATA SMTP failures per subscription per day before it is alerted
SIZE_WARN_MB, SIZE_LIMIT_MB = 10, 25
CLAIM_BUDGET_MIN = 25       # stop claiming NEW subscriptions after this long in one run: 25 + one slow export (HTTP timeout 25 min) stays under the 55 min task limit
MAX_MAPPING_AGE_H = 26      # refuse to send from a mapping older than this (T2 sync not run); env MAPPING_MAX_AGE_H (R1)
DEFAULT_CUTOFF = "15:30"
DEFAULT_ALERT_TO = os.environ.get("OWNER_EMAIL", "")   # .env; used when ALERT_TO is unset. Empty = no valid recipient (alerts dropped, allow-all refused)
DEFAULT_SES_HOST = ops.DEFAULT_SES_HOST
LOCAL_HOSTS = {"localhost", "127.0.0.1"}
LOCAL_TO = "preview@localhost"
LOCAL_FROM = "preview-sender@localhost"

log = logging.getLogger("t6_send")


class PreDataError(Exception):
    """The mail was NOT accepted: failure before the SMTP DATA command. Safe to retry."""


class NotQueuedError(PreDataError):
    """A 5xx reply AFTER the message content that matches KNOWN_REJECTIONS: SES says it did not queue the message. Deterministic,
    so the row may be marked Failed (REJECTED:) and the slot is freed, but it is not retried the same day."""


class AmbiguousSendError(Exception):
    """Failure after the DATA command was accepted: the mail may or may not have been delivered."""


# ---------- small helpers ----------
def sanitize(text, limit=300):
    """Exception text may echo an address (SMTP replies do): never let one reach a log or the DWH."""
    return re.sub(r"[\w.+'-]+@[\w.-]+", "<addr>", str(text))[:limit]


def addr_hash(addrs):
    """Short hash of the recipient list for logs. Salted with env ADDR_HASH_SALT (D-042): without a salt a small internal address set
    is guessable from the hash, so set one in .env."""
    return hashlib.sha256((ops.hash_salt() + "|" + "|".join(sorted(a.lower() for a in addrs))).encode()).hexdigest()[:10]


def is_not_found(exc):
    """Tableau REST answered 404 (the subscription or its target no longer exists)."""
    return str(getattr(exc, "code", "")).startswith("404")


def default_max_age_h(env=os.environ):
    """The mapping staleness limit in hours (env MAPPING_MAX_AGE_H, default 26): one shared function, ops.default_max_age_h."""
    return ops.default_max_age_h(env)


def sgt_now():
    return dt.datetime.now(t4.SGT)


# ---------- config ----------
@dataclass
class Config:
    execute: bool = False
    ses: bool = False
    export_in_dry_run: bool = False
    sender: str = ""
    alert_to: str = DEFAULT_ALERT_TO
    allow: frozenset = frozenset()
    allow_all: bool = False             # D-050: SEND_ALLOW_ALL_UAT=1 on the UAT Tableau host: every subscriber address is allowed
    size_warn: int = SIZE_WARN_MB * 1024 * 1024
    size_limit: int = SIZE_LIMIT_MB * 1024 * 1024
    claim_timeout_min: int = CLAIM_TIMEOUT_MIN
    budget_min: float = CLAIM_BUDGET_MIN
    max_mapping_age_h: float = MAX_MAPPING_AGE_H
    max_export_attempts: int = MAX_EXPORT_ATTEMPTS
    cutoff: dt.time = dt.time(15, 30)
    local_release: bool = True          # local transport: end as 'Released', not 'Sent' (see docstring)
    alert_state: Path = ROOT / "out" / "t6_alert_state.json"
    id_like: str = "%"                  # tests only: restricts which log rows the sweep may touch
    include_untaken: bool = False       # dev/local only: also process non-suspended subscriptions that are not taken over
    schedule_enforce: bool = True       # R1/D-036: SCHEDULE_MODE=honour (default) | ignore (old behaviour: every day when ready)
    catch_up_days: int = sd.DEFAULT_CATCH_UP_DAYS
    takeover_day_guard: bool = True     # R1: without schedule awareness, never send on the day t11 paused the subscription
    export_pause_s: float = 20          # R1: pause between two subscriptions' exports (load on Tableau), env EXPORT_PAUSE_SEC
    max_sends_per_run: int = 10         # R1: claims per run, the rest wait for the next run, env MAX_SENDS_PER_RUN


def parse_allow(text):
    return frozenset(a.strip().lower() for a in (text or "").split(",") if a.strip())


def filter_allowed(cfg, addrs):
    """(addresses to deliver to, number dropped). Local transport never uses a real address."""
    if not cfg.ses:
        return [LOCAL_TO], 0
    if cfg.allow_all:   # D-050: no list, but only well-formed addresses (nothing odd goes into an SMTP command)
        ok = [a.strip() for a in addrs if ops.ADDR_RE.fullmatch(a.strip())]
        return ok, len(addrs) - len(ok)
    ok = [a for a in addrs if a.lower() in cfg.allow]
    return ok, len(addrs) - len(ok)


# ---------- mail transport ----------
# R1 / F-06: replies to the end-of-message dot that PROVE nothing was queued, PER PROVIDER (SMTP_PROVIDER). Anything not matched stays
# ambiguous (row left Claimed). THE SES LIST IS FROM RECALL OF AMAZON SES BEHAVIOUR (an unverified sender identity or, in the sandbox, an
# unverified recipient is answered '554 Message rejected: Email address is not verified. The following identities failed the check in
# region ...'). It MUST BE VERIFIED AT THE FIRST LIVE TEST; add patterns only from observed replies. Gmail and generic servers: NO
# pattern, so nothing but a pre-DATA error frees a slot and every post-DATA non-250 reply stays Claimed (D-043).
# A reply is matched as '<code> <text>' (whitespace collapsed, case-insensitive) and the code must be 5xx.
KNOWN_REJECTIONS = {
    "ses": [
        re.compile(r"^554 Message rejected: Email address is not verified", re.I),
        re.compile(r"^554 Message rejected: Missing final '@domain'", re.I),
    ],
    "gmail": [],
    "generic": [],
}


def is_known_rejection(code, text, provider="ses"):
    return 500 <= code <= 599 and any(p.search("%d %s" % (code, " ".join(text.split()))) for p in KNOWN_REJECTIONS.get(provider, []))


def reply_id(text):
    """Message id from a 250 reply. SES answers '250 Ok <id>'; a local stub may answer anything. Contains no address."""
    t = sanitize(" ".join(str(text).split()), 120)
    tok = t.split(" ")[-1] if t else ""
    return tok if len(tok) >= 16 and re.fullmatch(r"[A-Za-z0-9._-]+", tok) else t


class SmtpMailer:
    """SMTP delivery that separates 'definitely not accepted' (PreDataError) from 'may have been sent'."""

    def __init__(self, host, port, user=None, password=None, starttls=False, timeout=60, provider="ses"):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.starttls, self.timeout, self.provider = starttls, timeout, provider

    def send(self, raw, from_addr, to_addrs):
        s = None
        try:
            s = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            s.ehlo()
            if self.starttls:
                s.starttls(context=ssl.create_default_context())  # verifies certificate and host name before any login
                s.ehlo()
            if self.user:
                s.login(self.user, self.password)
            code, _ = s.mail(from_addr)
            if code != 250:
                raise PreDataError("MAIL FROM refused (%d)" % code)
            for r in to_addrs:
                code, _ = s.rcpt(r)
                if code not in (250, 251):
                    raise PreDataError("RCPT refused (%d)" % code)
            code, _ = s.docmd("DATA")
            if code != 354:
                raise PreDataError("DATA not accepted (%d)" % code)
        except PreDataError:
            self._close(s)
            raise
        except Exception as e:  # connect, TLS, auth, protocol: all before the message content was sent
            self._close(s)
            raise PreDataError("%s: %s" % (type(e).__name__, sanitize(e))) from None
        # From here on the content is on the wire: any problem is ambiguous, except a KNOWN_REJECTIONS reply.
        try:
            body = re.sub(rb"(?m)^\.", b"..", raw)
            if not body.endswith(b"\r\n"):
                body += b"\r\n"
            s.send(body + b".\r\n")
            code, text = s.getreply()
        except Exception as e:
            self._close(s)
            raise AmbiguousSendError("%s after DATA: %s" % (type(e).__name__, sanitize(e))) from None
        text = (text.decode("utf-8", "replace") if isinstance(text, bytes) else str(text)).strip()
        if code != 250:
            self._close(s)
            if is_known_rejection(code, text, self.provider):
                raise NotQueuedError("rejected after the message content (%d): %s" % (code, sanitize(text, 160)))
            raise AmbiguousSendError("server replied %d after the message content" % code)
        try:
            s.quit()
        except Exception:
            self._close(s)
        return reply_id(text)

    def preflight(self, from_addr, rcpt):
        """SMTP self-test WITHOUT sending a message (R1): EHLO, STARTTLS, LOGIN, MAIL FROM, RCPT TO, then RSET and QUIT.
        Raises on any refusal. Run once before the first real claim of a run, so a broken SES setup stops the run
        before any subscription is claimed."""
        s = None
        try:
            s = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            s.ehlo()
            if self.starttls:
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
            if self.user:
                s.login(self.user, self.password)
            code, text = s.mail(from_addr)
            if code != 250:
                raise PreDataError("MAIL FROM refused (%d)" % code)
            code, text = s.rcpt(rcpt)
            if code not in (250, 251):
                raise PreDataError("RCPT TO refused (%d) %s" % (code, sanitize(text.decode("utf-8", "replace") if isinstance(text, bytes) else text, 120)))
            s.rset()
            s.quit()
        except PreDataError:
            self._close(s)
            raise
        except Exception as e:
            self._close(s)
            raise PreDataError("%s: %s" % (type(e).__name__, sanitize(e))) from None

    @staticmethod
    def _close(s):
        try:
            if s is not None:
                s.close()
        except Exception:
            pass


def make_mailer(cfg, local_host="127.0.0.1", local_port=1025, env=os.environ):
    if cfg.ses:
        st = ops.smtp_settings(env)
        return SmtpMailer(st["host"], st["port"], st["user"], st["password"], starttls=True, provider=st["provider"])
    if local_host.lower() not in LOCAL_HOSTS:
        sys.exit("Refusing local SMTP host %r: only localhost/127.0.0.1" % local_host)
    return SmtpMailer(local_host, local_port)


# ---------- Tableau layer (read and export only) ----------
@dataclass
class Live:
    suspended: bool
    target_id: str
    attach_pdf: bool
    attach_image: bool
    page_size: str
    orientation: str
    subject: str
    message: str
    emails: list
    label: str
    ref_kind: str
    ref_name: str
    n_views: int = 0
    wb_updated_at: object = None   # aware datetime of the workbook's last change on Tableau
    private: dict = field(default_factory=dict)   # target/view objects kept between describe and export


def render_as_subscriber(env=os.environ):
    """True when RENDER_AS_SUBSCRIBER=1. Raises (a clean startup failure) when it is on but the admin account is not configured, so a
    misconfiguration can never quietly render everything as the service account."""
    if env.get("RENDER_AS_SUBSCRIBER", "0").strip() != "1":
        return False
    if not env.get("TABLEAU_IMPERSONATOR_NAME", "").strip() or not env.get("TABLEAU_IMPERSONATOR_PASSWORD", ""):
        raise RuntimeError("RENDER_AS_SUBSCRIBER=1 but TABLEAU_IMPERSONATOR_NAME / TABLEAU_IMPERSONATOR_PASSWORD are not set")
    return True


def sign_in_as_subscriber(user_id, env=os.environ):
    """A separate Tableau session that acts as `user_id` (REST sign-in impersonation by the admin account). Independent of the PAT
    session. The error never carries the password."""
    import tableauserverclient as TSC
    try:
        server = TSC.Server(env["TABLEAU_SERVER_URL"].strip(), use_server_version=True)
        server.add_http_options({"timeout": t4.HTTP_TIMEOUT_S})
        server.auth.sign_in(TSC.TableauAuth(env["TABLEAU_IMPERSONATOR_NAME"].strip(), env["TABLEAU_IMPERSONATOR_PASSWORD"],
                                            env.get("TABLEAU_SITE_ID", ""), user_id_to_impersonate=user_id))
    except Exception as e:
        raise RuntimeError("sign-in as the subscriber failed (%s); is the admin account's password still valid?" % type(e).__name__) from None
    return server


class TableauAdapter:
    def __init__(self, tableau, id_map=None, render_as=False):
        self.t = tableau  # callable returning a signed-in server
        self.render_as = render_as  # D-063: export in a session that acts as the subscriber
        self._pub_names = {}
        self.id_map = id_map or {}  # tests only: fixture id -> real Tableau subscription id

    def describe(self, sub, recipients=True):
        """Live definition of the subscription from Tableau, plus its recipients. Raises on any lookup failure.
        recipients=False (sample send, D-054): the subscriber is not looked up at all, emails stays empty."""
        s = self.t()
        ls = s.subscriptions.get_by_id(self.id_map.get(sub.id, sub.id))
        user = s.users.get_by_id(ls.user_id) if recipients else None
        is_wb = ls.target.type.lower() == "workbook"
        if is_wb:
            target = s.workbooks.get_by_id(ls.target.id)
            s.workbooks.populate_views(target)
            views, label, wb = list(target.views), target.name, target
        else:
            target = s.views.get_by_id(ls.target.id)
            views = [target]
            wb = s.workbooks.get_by_id(target.workbook_id)
            label = "%s / %s" % (wb.name, target.name)
        return Live(suspended=bool(ls.suspended), target_id=ls.target.id, attach_pdf=bool(ls.attach_pdf),
                    attach_image=bool(ls.attach_image), page_size=ls.page_size_option, orientation=ls.page_orientation,
                    subject=ls.subject or "", message=ls.message or "", emails=[user.email] if user is not None and user.email else [],
                    label=label, ref_kind="workbook" if is_wb else "view", ref_name=target.name, n_views=len(views),
                    wb_updated_at=wb.updated_at,
                    private={"target": target, "views": views, "is_wb": is_wb, "user_id": ls.user_id})

    def refresh_running(self, targets):
        """Name of the first of these refresh targets (published data source ids) that has a queued/running refresh job on Tableau, else
        None (read-only). Jobs are matched on the PUBLISHED data source name, exactly as t4 does (the workbook-side name differs, for
        example '(Extract) vw_x'). A lookup failure raises, and the caller treats it as busy."""
        for tid in targets:
            if tid not in self._pub_names:
                self._pub_names[tid] = self.t().datasources.get_by_id(tid).name
            name = self._pub_names[tid]
            if t4.active_refresh_job(self.t, name) is not None:
                return name
        return None

    def tableau_busy(self, own_ids):
        """D-057: t4.tableau_busy on this adapter's Tableau session: None (check off) or (queued, running, ignored)."""
        return t4.tableau_busy(self.t, own_ids)

    def export(self, sub, live):
        """(pdf bytes or None, [image dicts]) in the subscription's own format, page size and orientation. With render_as, in a
        session that acts as the subscriber (D-063), so the report shows what Tableau would have shown that person."""
        p = live.private
        if not self.render_as:
            return self._export_in(self.t(), p["target"], p["views"], p["is_wb"], live)
        us = sign_in_as_subscriber(p["user_id"])
        try:
            if p["is_wb"]:
                target = us.workbooks.get_by_id(live.target_id)
                us.workbooks.populate_views(target)
                views = list(target.views)
            else:
                target = us.views.get_by_id(live.target_id)
                views = [target]
            return self._export_in(us, target, views, p["is_wb"], live)
        finally:
            try:
                us.auth.sign_out()
            except Exception:
                pass

    @staticmethod
    def _export_in(s, target, views, is_wb, live):
        pdf = sm.export_pdf(s, target, is_wb, live.page_size, live.orientation) if live.attach_pdf else None
        imgs = sm.export_images(s, views) if live.attach_image else []
        return pdf, imgs


# ---------- readiness gate ----------
@dataclass
class Sub:
    id: str
    wb_id: str
    wb_name: str
    target_type: str
    target_id: str
    fmt: str
    subject: str
    sched: str
    key_hash: bytes
    synced_at: object = None   # Tableau_Subscription.LastSeenAt (DWH local = Singapore, naive): last T2 sync that saw it
    taken_over: bool = False   # open Tableau_SubscriptionBackup row with TakenOver = 1 (D-035)
    paused: bool = False       # that row's PausedAt is set: t11 confirmed the pause
    repaused_date: object = None  # that row's LastRepausedDate (Singapore day t11 last paused it again)
    sched_id: object = None    # Tableau_Subscription.ScheduleId (R1)
    paused_at: object = None   # naive Singapore datetime of the takeover pause (PausedAt)
    halted: bool = False       # R1: a pause changed the definition; only a human clears it
    wb_updated: object = None  # R5: the workbook updated_at (naive UTC) T2 read at the last sync; NULL = not stored yet


SUBS_SQL = """
SELECT s.Subscription_id, s.Workbook_id,
       (SELECT TOP 1 w.Workbook_Name FROM Integration.Tableau_WorkbookDataSource w WHERE w.Workbook_id = s.Workbook_id ORDER BY w.Active_Flag DESC, w.LastSeenAt DESC, w.Workbook_Name),
       s.TargetType, s.TargetId, s.Format, s.Subject, s.ScheduleType, s.SubscriptionKeyHash, s.LastSeenAt,
       b.Backup_id, CASE WHEN b.PausedAt IS NULL THEN 0 ELSE 1 END, b.LastRepausedDate,
       s.ScheduleId, CAST(SWITCHOFFSET(b.PausedAt, '+08:00') AS datetime2(0)), ISNULL(b.Halted, 0), s.WorkbookUpdatedAt
FROM Integration.Tableau_Subscription s
LEFT JOIN Integration.Tableau_SubscriptionBackup b
       ON b.Subscription_id = s.Subscription_id AND b.TakenOver = 1 AND b.RestoredAt IS NULL
WHERE s.Active_Flag = 1 AND s.Subscription_id LIKE ?
ORDER BY 3, s.Subject, s.Subscription_id"""


def load_subscriptions(cur, ids=None, workbook=None, id_like="%"):
    subs = [Sub(r[0], r[1], r[2] or "(unnamed workbook)", r[3], r[4], r[5], r[6] or "", r[7] or "", r[8], r[9],
                taken_over=r[10] is not None, paused=bool(r[11]), repaused_date=r[12],
                sched_id=r[13], paused_at=r[14], halted=bool(r[15]), wb_updated=r[16])
            for r in cur.execute(SUBS_SQL, id_like).fetchall()]
    prio = ops.priorities(cur, "Subscription")   # D-062: marked subscriptions first; the SQL order (workbook, subject) breaks ties
    subs.sort(key=lambda s: prio.get(s.id, 2 ** 31))
    if ids:
        subs = [s for s in subs if s.id in ids]
    if workbook:
        w = workbook.lower()
        subs = [s for s in subs if s.wb_name.lower() == w or s.wb_id.lower() == w]
    return subs


class Gate:
    """Freshness decision per workbook, from the mapping views, ETL_Master_Log and Tableau_RefreshJob (read only)."""

    def __init__(self, cur, today):
        self.today = today
        self._cur, self._inflight = cur, None
        self.wb = {r[0]: (r[1], r[2]) for r in cur.execute(
            "SELECT Workbook_id, WorkbookStatus, DataSourceCount FROM Integration.vw_Tableau_WorkbookReadiness").fetchall()}
        self.sources, self.target_ds = {}, {}
        for wid, dsid, name, status, kind, tgt in cur.execute(
                "SELECT w.Workbook_id, r.DataSource_id, r.DataSource_Name, r.ReadyStatus, r.RefreshKind, "
                "r.ExtractRefreshTargetId FROM Integration.Tableau_WorkbookDataSource w "
                "JOIN Integration.vw_Tableau_DataSourceReadiness r ON r.DataSource_id = w.DataSource_id "
                "WHERE w.Active_Flag = 1").fetchall():
            self.sources.setdefault(wid, []).append({"id": dsid, "name": name, "status": status, "kind": kind, "target": tgt})
            if tgt:
                self.target_ds.setdefault(tgt, set()).add(dsid)
        self.pipes = {}
        for ds, p, known in cur.execute(t4.PIPE_SQL).fetchall():
            self.pipes.setdefault(ds, {})[p] = bool(known)
        self.etl = t4.etl_status_today(cur, today)
        self.refreshed = {r[0]: r[1] for r in cur.execute(
            "SELECT DataSource_id, CAST(MAX(SWITCHOFFSET(SubmittedAt, '+08:00')) AS datetime2(0)) "
            "FROM Integration.Tableau_RefreshJob WHERE Status = 'Succeeded' AND FinishedAt IS NOT NULL "
            "AND SubmittedDate = ? GROUP BY DataSource_id", today.isoformat()).fetchall()}

    def inflight_targets(self):
        """Refresh targets with a 'Submitted' (in-flight) row in Tableau_RefreshJob, loaded once per run."""
        if getattr(self, "_inflight", None) is None:
            self._inflight = {r[0] for r in self._cur.execute(
                "SELECT DISTINCT DataSource_id FROM Integration.Tableau_RefreshJob WHERE Status = 'Submitted'").fetchall()}
        return self._inflight

    def _etl_state(self, pipes):
        """(unknown, failed, missing) pipeline name lists."""
        return ([p for p, k in pipes.items() if not k],
                [p for p in pipes if pipes[p] and p in self.etl and not self.etl[p][1]],
                [p for p in pipes if pipes[p] and p not in self.etl])

    def structural(self, wb_id):
        """(ok, reason): can t6 EVER send this workbook? The mapping-level conditions of check() without today's ETL or
        refresh state. t11_pause.py uses it so it never pauses what t6 would refuse to send (D-035)."""
        status, count = self.wb.get(wb_id, (None, 0))
        if status != "Ready":
            return False, "workbook mapping status %s" % (status or "unknown")
        srcs = self.sources.get(wb_id, [])
        if not srcs or len(srcs) != count:
            return False, "data source list incomplete (%d of %d)" % (len(srcs), count)
        gated, unchecked = 0, 0
        for s in srcs:
            if s["status"] == "NoReadinessCheck":
                continue
            if s["status"] == "NotChecked":   # D-053: positively non-DWH (file / other database): deemed ready, nothing to wait for
                unchecked += 1
                continue
            if s["status"] != "Ready":
                return False, "source %s" % s["status"]
            pipes = self.pipes.get(s["id"], {})
            if any(not k for k in pipes.values()):
                return False, "unknown pipeline"
            gated += len(pipes)
            if s["kind"] == "Extract":
                if not s["target"]:
                    return False, "extract without a refresh target"
                tp = {}
                for ds in self.target_ds.get(s["target"], {s["id"]}):
                    tp.update(self.pipes.get(ds, {}))
                if not tp:
                    return False, "extract has no required pipeline (cannot gate)"
                if any(not k for k in tp.values()):
                    return False, "extract's pipelines unknown"
            elif s["kind"] != "Live":
                return False, "source kind %s unknown" % s["kind"]
        if gated == 0 and not unchecked:
            return False, "nothing to gate freshness on (no required pipeline)"
        return True, "ok"

    def check(self, wb_id):
        """(ok, kind, reason). kind: 'ready' | 'wait' (not yet) | 'blocked' (mapping gap, failed or unknown)."""
        status, count = self.wb.get(wb_id, (None, 0))
        if status != "Ready":
            return False, "blocked", "workbook mapping status %s" % (status or "unknown")
        srcs = self.sources.get(wb_id, [])
        if not srcs or len(srcs) != count:
            return False, "blocked", "data source list incomplete (%d of %d)" % (len(srcs), count)
        waits, gated, unchecked = [], 0, 0
        for s in srcs:
            if s["status"] == "NoReadinessCheck":
                continue
            if s["status"] == "NotChecked":   # D-053: positively non-DWH (file / other database): deemed ready, nothing to wait for
                unchecked += 1
                continue
            if s["status"] != "Ready":
                return False, "blocked", "source %s" % s["status"]
            pipes = self.pipes.get(s["id"], {})
            unk, failed, missing = self._etl_state(pipes)
            if unk:
                return False, "blocked", "%d unknown pipeline(s)" % len(unk)
            if failed:
                return False, "blocked", "latest run of %d required pipeline(s) failed" % len(failed)
            if missing:
                waits.append("%d required pipeline(s) have no run yet" % len(missing))
            gated += len(pipes)
            if s["kind"] == "Extract":
                tgt = s["target"]
                if not tgt:
                    return False, "blocked", "extract without a refresh target"
                tp = {}
                for ds in self.target_ds.get(tgt, {s["id"]}):
                    tp.update(self.pipes.get(ds, {}))
                if not tp:
                    return False, "blocked", "extract has no required pipeline (cannot gate)"
                tunk, tfailed, tmissing = self._etl_state(tp)
                if tunk or tfailed:
                    return False, "blocked", "extract's pipelines unknown or failed"
                if tmissing:
                    waits.append("extract waits for %d pipeline(s)" % len(tmissing))
                else:
                    last_etl = max(self.etl[p][0] for p in tp)
                    done = self.refreshed.get(tgt)
                    if done is None or done <= last_etl:
                        waits.append("extract not refreshed since the last ETL (%s)" % last_etl.strftime("%H:%M"))
            elif s["kind"] != "Live":
                return False, "blocked", "source kind %s unknown" % s["kind"]
        if waits:
            return False, "wait", "; ".join(dict.fromkeys(waits))
        if gated == 0 and not unchecked:
            return False, "blocked", "nothing to gate freshness on (no required pipeline)"
        return True, "ready", "all required pipelines and extracts are fresh"


# ---------- send log ----------
def sweep_stale(cur, cfg, execute):
    """Claimed rows older than the claim timeout -> TimedOut (execute only). Returns [(log id, subscription id)]."""
    rows = cur.execute(
        "SELECT SendLog_id, Subscription_id FROM Integration.Tableau_SubscriptionSendLog WHERE Status = 'Claimed' "
        "AND ClaimedAt < DATEADD(minute, -?, SYSDATETIMEOFFSET()) AND Subscription_id LIKE ?",
        cfg.claim_timeout_min, cfg.id_like).fetchall()
    if execute:
        for lid, _ in rows:
            cur.execute("UPDATE Integration.Tableau_SubscriptionSendLog SET Status = 'TimedOut', "
                        "Message = LEFT(ISNULL(Message, '') + ' | claim timed out', 1000) "
                        "WHERE SendLog_id = ? AND Status = 'Claimed'", lid)
    return [(r[0], r[1]) for r in rows]


def load_prior(cur, today, id_like="%"):
    """Today's log rows per subscription: {sub id: [(status, message)]}. DryRun rows never block."""
    out = {}
    for sid, st, msg in cur.execute(
            "SELECT Subscription_id, Status, Message FROM Integration.Tableau_SubscriptionSendLog "
            "WHERE SendDate = ? AND Subscription_id LIKE ? AND Status <> 'DryRun'", today.isoformat(), id_like).fetchall():
        out.setdefault(sid, []).append((st, msg or ""))
    return out


def claim(cur, sub_id, run_id):
    """INSERT the 'Claimed' row. Returns its id, or None when the slot is taken (unique index, errors 2601/2627)."""
    try:
        return cur.execute("SET NOCOUNT ON; INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status, RunId, Message) "
                           "OUTPUT inserted.SendLog_id VALUES (?, 'Claimed', ?, 'claimed')", sub_id, run_id).fetchone()[0]
    except pyodbc.IntegrityError as e:
        if "2601" in str(e) or "2627" in str(e):
            return None
        raise


def set_status(cur, log_id, status, msg):
    """Tolerant update of a Claimed row: on error the row stays Claimed (the safe state)."""
    try:
        if status == "Sent":
            cur.execute("UPDATE Integration.Tableau_SubscriptionSendLog SET Status = 'Sent', SentAt = SYSDATETIMEOFFSET(), "
                        "Message = ? WHERE SendLog_id = ? AND Status IN ('Claimed', 'TimedOut')", msg[:1000], log_id)
        else:
            cur.execute("UPDATE Integration.Tableau_SubscriptionSendLog SET Status = ?, Message = ? "
                        "WHERE SendLog_id = ? AND Status = 'Claimed'", status, msg[:1000], log_id)
        return cur.rowcount == 1
    except pyodbc.Error as e:
        log.error("Could not set send log row %s to %s (%s); it stays as it was", log_id, status, type(e).__name__)
        return False


DUP_SQL = """
SELECT TOP 1 1 FROM Integration.Tableau_Subscription s2
JOIN Integration.Tableau_SubscriptionSendLog l ON l.Subscription_id = s2.Subscription_id
WHERE s2.SubscriptionKeyHash = ? AND s2.Subscription_id <> ? AND s2.Active_Flag = 0
  AND l.SendDate = ? AND l.Status IN ('Claimed', 'Sent', 'TimedOut')"""


# An ACTIVE sibling with the same key that has history while this subscription has none before today.
SIBLING_SQL = """
SELECT TOP 1 1 FROM Integration.Tableau_Subscription s2
WHERE s2.SubscriptionKeyHash = ? AND s2.Subscription_id <> ? AND s2.Active_Flag = 1
  AND NOT EXISTS (SELECT 1 FROM Integration.Tableau_SubscriptionSendLog a WHERE a.Subscription_id = ? AND a.SendDate < ?)
  AND EXISTS (SELECT 1 FROM Integration.Tableau_SubscriptionSendLog b WHERE b.Subscription_id = s2.Subscription_id AND b.SendDate < ?)"""


# ---------- run context ----------
@dataclass
class Ctx:
    cfg: Config
    adapter: object
    mailer: object
    gate: object
    today: dt.date
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    prior: dict = field(default_factory=dict)
    events: list = field(default_factory=list)      # dict(sub, wb, code, text): needs a human
    unreadable: list = field(default_factory=list)  # (sub id, view, w, h)
    not_ready: list = field(default_factory=list)   # (Sub, reason)
    sizes: list = field(default_factory=list)       # (sub id, bytes)
    format_warnings: list = field(default_factory=list)  # (sub id, text)
    taken_wbs: set = field(default_factory=set)     # workbooks with at least one taken-over subscription (cutover guard)
    due: object = None                              # schedule_due.DueEngine (None = schedule ignored)
    breaker: str = ""                               # R1 circuit breaker: reason text once tripped, no further claims this run
    smtp_streak: int = 0                            # consecutive SMTP failures
    preflight_done: bool = False
    claims: int = 0
    last_export_end: float = 0.0
    killed: bool = False                            # kill switch appeared during the run: nothing new is started
    probe_fail: int = 0                             # refresh-job probe failures in this run
    probe_ok: int = 0
    busy: object = False                            # D-057: "Tableau busy" reason once looked up this run ("" = idle or check off)

    def event(self, sub, code, text):
        self.events.append({"sub": sub.id, "wb": sub.wb_name, "code": code, "text": text})


def render(sub, live, to, ctx):
    """Export in the subscription's format and build the mail. Returns (raw bytes, pdf bytes or None, images)."""
    pdf, imgs = ctx.adapter.export(sub, live)
    for im in imgs:
        if im["unreadable"]:
            log.warning("UNREADABLE IMAGE sub=%s view='%s': %dx%d px after the %d px cap (was %d x %d); design fix needed",
                        sub.id, im["name"], im["w"], im["h"], sm.MAX_IMAGE_WIDTH, im["orig_w"], im["orig_h"])
            ctx.unreadable.append((sub.id, im["name"], im["w"], im["h"]))
    cfg = ctx.cfg
    msg = sm.build_message(ref_kind=live.ref_kind, ref_name=live.ref_name, subject=live.subject or sub.subject or live.label,
                           sender=cfg.sender, to_header=", ".join(to) if cfg.ses else LOCAL_TO, note=live.message,
                           sched=sub.sched, pdf_bytes=pdf, images=[(i["name"], i["data"]) for i in imgs],
                           msgid_domain=cfg.sender.split("@")[-1])
    return msg.as_bytes(policy=email.policy.SMTP), pdf, imgs


def trip(ctx, sub, why):
    """Circuit breaker (R1): stop claiming further subscriptions in this run and alert once with the reply text (no addresses).
    The remaining subscriptions stay unclaimed, so the next run retries them."""
    if not ctx.breaker:
        ctx.breaker = why
        log.error("CIRCUIT BREAKER: %s. No further subscription is claimed in this run.", why)
        ctx.event(sub, "smtp_breaker", "sending stopped for the rest of this run: %s" % why)


def preflight_rcpt(cfg):
    """The allow-listed recipient used for the RCPT TO self-test (the alert recipient if allowed, else the first allowed)."""
    return cfg.alert_to if cfg.allow_all or cfg.alert_to.lower() in cfg.allow else sorted(cfg.allow)[0]


def refresh_busy(ctx, sub):
    """Reason text when a refresh job of one of the workbook's extracts is running (DWH in-flight marker or Tableau), else None.
    Never export while data is being refreshed (R1: load on Tableau and half-loaded extracts)."""
    g = ctx.gate
    fn = getattr(g, "inflight_targets", None)
    srcs = getattr(g, "sources", {}).get(sub.wb_id, [])
    targets = {s["target"] for s in srcs if s.get("target")}
    if fn and targets & fn():
        return "a refresh job of one of the workbook's extracts is in flight (Tableau_RefreshJob)"
    probe = getattr(ctx.adapter, "refresh_running", None)
    if probe:
        names = sorted({s["target"] for s in srcs if s.get("target")})   # refresh TARGET ids: the adapter looks up the PUBLISHED name
        try:
            hit = probe(names)
        except Exception as e:
            ctx.probe_fail += 1
            log.warning("Refresh-job lookup on Tableau failed for %s (%s): treated as BUSY, skipped this run (alert after 3 runs in a row)",
                        sub.id, type(e).__name__)
            return "the refresh-job lookup on Tableau failed (%s); not exporting blind" % type(e).__name__
        ctx.probe_ok += 1
        if hit:
            return "a refresh job of data source '%s' is running on Tableau" % hit
    return None


def tableau_wait(cur, ctx):
    """D-057: reason text when Tableau is busy with someone else's background jobs (queued or running), else None. Looked up once per
    run. Our own recorded refresh job ids do not count. A lookup failure is busy (fail closed) and counts as a probe failure."""
    if ctx.busy is False:
        probe = getattr(ctx.adapter, "tableau_busy", None)
        ctx.busy = ""
        if probe:
            try:
                own = [r[0] for r in cur.execute("SELECT TableauJobId FROM Integration.Tableau_RefreshJob WHERE TableauJobId IS NOT NULL "
                                                 "AND SubmittedAt >= DATEADD(day, -3, SYSUTCDATETIME())").fetchall()]
                r = probe(own)
            except Exception as e:
                ctx.probe_fail += 1
                log.warning("Tableau busy check failed (%s): treated as BUSY, nothing is exported this run", type(e).__name__)
                ctx.busy = "Tableau busy (the job lookup failed: %s)" % type(e).__name__
            else:
                if r and r[0] + r[1]:
                    ctx.busy = "Tableau busy: %d queued, %d running" % r[:2]
                    log.info("%s; %s", ctx.busy, "waiting" if ctx.cfg.execute else "would wait (dry run)")
    return ctx.busy or None


def recheck_takeover(cur, sub, ctx):
    """Re-read the backup row right before the claim (R1): the snapshot loaded at the start of the run may be minutes old.
    False when the subscription must not be sent now (restored, halted, re-paused today)."""
    r = cur.execute("SELECT CAST(SWITCHOFFSET(b.PausedAt, '+08:00') AS datetime2(0)), b.LastRepausedDate, ISNULL(b.Halted, 0) FROM Integration.Tableau_SubscriptionBackup b "
                    "WHERE b.Subscription_id = ? AND b.TakenOver = 1 AND b.RestoredAt IS NULL", sub.id).fetchone()
    if r is None or r[0] is None or r[2] or r[1] == ctx.today:
        log.warning("Takeover row of %s changed during the run (restored, halted or re-paused): not sent", sub.id)
        return False
    return True


def takeover_day_warning(ctx, sub, why):
    """R5 / F-1: the takeover-day guard skipped today's occurrence of a subscription taken over today. Right for a live subscription
    (Tableau may have mailed it already), but if it was SUSPENDED before the takeover Tableau sent nothing and nobody sends this
    occurrence. One WARNING per subscription and Singapore day in the log and (execute only) the Windows Event Log, so it is seen."""
    text = ("Workbook '%s', subscription %s (%s): taken over today and today's occurrence is NOT sent by the script (%s). If it was "
            "suspended in Tableau before the takeover, Tableau did not send it either: send it by hand if it is needed. The next "
            "occurrence is sent normally." % (sub.wb_name, sub.id, sub.sched or "no schedule name", why))
    log.warning("TAKEOVER-DAY GUARD: %s", text)
    if ctx.cfg.execute:
        ops.event_log_once("t6", "takeover_day:" + sub.id, text, "WARNING", day=ctx.today)


def process(cur, sub, ctx):
    """One subscription. Returns (kind, detail). Never raises for per-subscription problems."""
    cfg = ctx.cfg
    prior = ctx.prior.get(sub.id, [])
    states = {p[0] for p in prior}
    if not sub.taken_over and not cfg.include_untaken:
        if sub.wb_id in ctx.taken_wbs:
            ctx.event(sub, "new_native", "an active Tableau subscription that is NOT taken over exists on a workbook the script has taken "
                      "over (created after the takeover?). Tableau sends it itself and the script does not; t11_pause.py takes it over "
                      "on its next run if the workbook is in the allow-list, otherwise pause or delete it by hand")
        return "not_taken_over", "left to Tableau's own schedule (not taken over by t11_pause.py)"
    if sub.taken_over and sub.halted:
        ctx.event(sub, "halted", "t11 halted on this subscription (a pause changed its definition). It is not sent until a person has "
                  "compared it with the backup and cleared Halted in Tableau_SubscriptionBackup (see docs/RUNBOOK.md)")
        return "halted", "takeover halted, needs a human"
    if sub.taken_over and not sub.paused:
        return "takeover_unconfirmed", "taken over but t11 has not confirmed the pause yet"
    if sub.taken_over and sub.repaused_date == ctx.today:
        if "Sent" not in states:
            ctx.event(sub, "repaused_today", "resumed in Tableau and paused again by t11 today: the old subscription may already "
                      "have mailed, so the script does not send it today (no double mail)")
        return "repaused_today", "re-paused today, not sent today"
    if "Sent" in states:
        return "already_sent", ""
    if "TimedOut" in states:
        ctx.event(sub, "timed_out", "an earlier claim timed out; the slot stays blocked until a human sets it to Released/Failed/Sent")
        return "timed_out", ""
    if "Claimed" in states:
        return "in_progress", "claimed by another run"
    failed = [m for s, m in prior if s == "Failed"]
    if any(m.startswith("REJECTED:") for m in failed):
        ctx.event(sub, "rejected", "the mail server rejected the message today (nothing was queued); not retried today. Fix the cause, then "
                  "set the Failed row to Released to retry today (docs/RUNBOOK.md)")
        return "rejected", ""
    if any(m.startswith("OVERSIZE:") for m in failed):
        ctx.event(sub, "oversize", "message over the size limit; not sent")
        return "oversize", ""
    if sum(m.startswith("EXPORT:") for m in failed) >= cfg.max_export_attempts:
        ctx.event(sub, "export_failed", "export failed %d times today; not retrying" % cfg.max_export_attempts)
        return "gave_up", ""
    if (ctx.due is None and cfg.takeover_day_guard and sub.taken_over and sub.paused_at is not None
            and sub.paused_at.date() == ctx.today):
        takeover_day_warning(ctx, sub, "schedule not honoured, so today is skipped whatever its time")
        return "taken_over_today", "paused today; Tableau may already have mailed it today (schedule not honoured, so assumed)"
    if ctx.due is not None:   # R1 / D-036: only a subscription that is DUE today is looked at any further
        r = ctx.due.status(sub.id, sub.sched_id, sub.paused_at if sub.taken_over else None)
        if r.missed:
            ctx.event(sub, "missed_due", "the occurrence of %s was never sent and its catch-up window (%d day(s)) has ended" % (
                r.missed, cfg.catch_up_days))
        if r.status == "unsupported":
            ctx.event(sub, "schedule_unknown", "cannot tell whether the subscription is due: %s. Not sent (fail closed)" % r.note)
            return "schedule_unknown", r.note
        if r.status == "satisfied":
            return "already_sent", "occurrence %s already sent" % r.occurrence
        if r.status == "not_due":
            if r.taken_today:
                takeover_day_warning(ctx, sub, "its schedule time today had already passed when it was taken over")
            return ("taken_over_today" if r.taken_today else "not_due"), (
                "taken over after today's schedule time; first send is the next occurrence" if r.taken_today else "")
    age_h = (sgt_now().replace(tzinfo=None) - sub.synced_at).total_seconds() / 3600 if sub.synced_at else None
    if age_h is None or age_h > cfg.max_mapping_age_h:
        ctx.event(sub, "mapping_stale", "the T2 mapping sync last saw this subscription %s ago (limit %.0f h): not sent"
                  % ("?" if age_h is None else "%.1f h" % age_h, cfg.max_mapping_age_h))
        return "mapping_stale", "sync older than %.0f h" % cfg.max_mapping_age_h
    ok, kind, why = ctx.gate.check(sub.wb_id)
    if not ok:
        ctx.not_ready.append((sub, why))
        return ("not_ready" if kind == "wait" else "blocked"), why
    wait = tableau_wait(cur, ctx)   # D-057: before any export work; no claim row, and the after-cutoff not-ready alert covers a long wait
    if wait:
        ctx.not_ready.append((sub, wait))
        return "not_ready", ("" if cfg.execute else "would wait: ") + wait
    if not sub.key_hash:
        ctx.event(sub, "duplicate_key", "no SubscriptionKeyHash stored: the duplicate check is impossible, not sent")
        return "duplicate_key", "no key hash"
    if cur.execute(DUP_SQL, sub.key_hash, sub.id, ctx.today.isoformat()).fetchone():
        ctx.event(sub, "duplicate_key", "a deleted/replaced subscription with the same workbook, target, owner and subject "
                  "already has a send log row today: possible duplicate")
        return "duplicate_key", ""
    try:
        live = ctx.adapter.describe(sub)
    except Exception as e:
        if is_not_found(e):
            log.warning("Subscription %s no longer exists on Tableau (404): not sent; the next T2 sync deactivates it", sub.id)
            return "deleted_on_tableau", "404 from Tableau"
        log.warning("Tableau lookup failed for %s (%s): skipped", sub.id, type(e).__name__)
        return "lookup_failed", type(e).__name__
    if sub.taken_over:
        if not live.suspended:
            ctx.event(sub, "not_paused", "taken over but NOT suspended in Tableau (resumed by someone?): Tableau may send it "
                      "itself, so the script does not; t11_pause.py pauses it again")
            return "not_paused", "taken over but not suspended on Tableau"
    live_upd = ops.utc_naive(live.wb_updated_at)
    if live_upd is None or sub.wb_updated is None or live_upd > sub.wb_updated:
        # D-046: T2 runs once a day, so a workbook republished after the sync simply waits for the next one. Quiet: INFO log line,
        # a counter in the RUN SUMMARY and a line in the daily summary, no alert mail and not a problem exit code.
        # R5 / F-3: compared with the updated_at T2 stored when it read the workbook, never with a clock. NULL stored (T2 has not run
        # since R5) or unknown live change time = wait (fail closed).
        log.info("%s '%s': workbook changed on Tableau after the last T2 sync (or its change time is unknown or not stored yet); waits for the next daily sync",
                 sub.id, sub.wb_name)
        return "awaiting_sync", "workbook changed since the last sync; waits for the next daily T2 sync"
    if live.target_id != sub.target_id:
        return "definition_changed", "target differs from the last sync"
    if not (live.attach_pdf or live.attach_image):
        ctx.event(sub, "no_format", "neither PDF nor image is set on the subscription")
        return "no_format", ""
    if not live.emails:
        ctx.event(sub, "no_recipient", "the subscriber has no email address in Tableau")
        return "no_recipient", ""
    if live.attach_pdf:
        bad = sm.page_problems(live.page_size, live.orientation)
        if bad:
            log.warning("FORMAT WARNING sub=%s: unknown or missing %s; Tableau's default (letter/portrait) will be used",
                        sub.id, " and ".join(bad))
            ctx.format_warnings.append((sub.id, " and ".join(bad)))
    if cur.execute(SIBLING_SQL, sub.key_hash, sub.id, sub.id, ctx.today.isoformat(), ctx.today.isoformat()).fetchone():
        ctx.event(sub, "duplicate_active", "another ACTIVE subscription has the same workbook, target, owner and subject and this "
                  "one has no earlier send history: possible re-created duplicate (still sent)")
    to, dropped = filter_allowed(cfg, live.emails)
    log.info("sub=%s recipients=%d hash=%s dropped_by_allow_list=%d", sub.id, len(live.emails), addr_hash(live.emails), dropped)
    if not to:
        why = ("the subscriber's address is not a well-formed e-mail address" if cfg.allow_all else
               "the subscriber's address is not on SEND_ALLOWED_RECIPIENTS")
        ctx.event(sub, "no_allowed_recipient", why + ", so nothing is sent while the "
                  "old Tableau subscription is paused (fix the address in Tableau, or restore the subscription)")
        return "no_allowed_recipient", why
    fmt = "+".join(x for x, f in (("PDF", live.attach_pdf), ("images(%d)" % live.n_views, live.attach_image)) if f)

    if not cfg.execute:
        if not cfg.export_in_dry_run:
            return "would_send", fmt
        try:
            raw, pdf, imgs = render(sub, live, to, ctx)
        except Exception as e:
            log.warning("Dry-run export failed for %s: %s", sub.id, sanitize(e))
            return "export_failed", sanitize(e)
        ctx.sizes.append((sub.id, len(raw)))
        return "would_send", "%s: pdf=%s B, %d image(s), message=%d B" % (fmt, len(pdf) if pdf else 0, len(imgs), len(raw))

    if cfg.execute and ops.killed_midrun("t6"):
        ctx.killed = True
        return "killed", "kill switch activated during the run"
    if ctx.breaker:
        return "deferred_breaker", "circuit breaker: " + ctx.breaker
    if ctx.claims >= cfg.max_sends_per_run:
        return "deferred", "MAX_SENDS_PER_RUN reached"
    busy = refresh_busy(ctx, sub)
    if busy:
        ctx.not_ready.append((sub, busy))
        return "not_ready", busy
    if sub.taken_over and not recheck_takeover(cur, sub, ctx):
        return "takeover_changed", "backup row changed since the run started"
    if cfg.ses and cfg.execute and not ctx.preflight_done:
        try:
            pf = getattr(ctx.mailer, "preflight", None)
            if pf:
                pf(cfg.sender, preflight_rcpt(cfg))
            ctx.preflight_done = True
        except Exception as e:
            ctx.breaker = "SMTP preflight failed: %s" % sanitize(e, 200)
            log.error("SES PREFLIGHT FAILED: %s. No subscription is claimed in this run.", sanitize(e, 200))
            ctx.event(sub, "smtp_breaker", ctx.breaker)
            return "preflight_failed", sanitize(e)
    log_id = claim(cur, sub.id, ctx.run_id)
    if log_id is None:
        return "claim_lost", "another run holds today's slot"
    ctx.claims += 1
    try:
        wait = cfg.export_pause_s - (time.monotonic() - ctx.last_export_end)
        if ctx.last_export_end and wait > 0:
            time.sleep(wait)   # strictly serial exports with a pause between subscriptions (load on Tableau)
        raw, pdf, imgs = render(sub, live, to, ctx)
        ctx.last_export_end = time.monotonic()
    except Exception as e:
        ctx.last_export_end = time.monotonic()
        n_failed = sum(m.startswith("EXPORT:") for m in failed) + 1
        set_status(cur, log_id, "Failed", "EXPORT: %s" % sanitize(e))
        log.error("Export failed for %s (attempt %d of %d): %s", sub.id, n_failed, cfg.max_export_attempts, sanitize(e))
        if n_failed >= cfg.max_export_attempts:
            ctx.event(sub, "export_failed", "export failed %d times today: %s" % (n_failed, sanitize(e, 120)))
        return "export_failed", sanitize(e)
    ctx.sizes.append((sub.id, len(raw)))
    if len(raw) > cfg.size_limit:
        set_status(cur, log_id, "Failed", "OVERSIZE: %d bytes > limit %d" % (len(raw), cfg.size_limit))
        ctx.event(sub, "oversize", "message is %.1f MB, limit %.0f MB; not sent" % (len(raw) / 1048576, cfg.size_limit / 1048576))
        return "oversize", "%d bytes" % len(raw)
    if len(raw) > cfg.size_warn:
        log.warning("Large message for %s: %.1f MB (warn above %.0f MB)", sub.id, len(raw) / 1048576, cfg.size_warn / 1048576)
    env_from = cfg.sender if cfg.ses else LOCAL_FROM
    try:
        smtp_id = ctx.mailer.send(raw, env_from, to)
    except NotQueuedError as e:
        set_status(cur, log_id, "Failed", "REJECTED: %s" % sanitize(e))
        log.error("REJECTED by the mail server for %s, nothing queued: %s", sub.id, sanitize(e))
        ctx.event(sub, "rejected", "the mail server rejected the message after the content (nothing was queued): %s" % sanitize(e, 160))
        ctx.smtp_streak += 1   # one per-recipient rejection does NOT stop the run; two in a row do
        if ctx.smtp_streak >= 2:
            trip(ctx, sub, "two rejections in a row, last: %s" % sanitize(e, 160))
        return "rejected", sanitize(e)
    except PreDataError as e:
        set_status(cur, log_id, "Failed", "SMTP: %s" % sanitize(e))
        n = sum(m.startswith("SMTP:") for m in failed) + 1
        log.error("SMTP failure before DATA for %s (attempt %d today): %s", sub.id, n, sanitize(e))
        if n >= SMTP_ALERT_AFTER:
            ctx.event(sub, "send_failed", "SMTP failed %d times before DATA: %s" % (n, sanitize(e, 120)))
        per_recipient = str(e).startswith(("RCPT refused", "MAIL FROM refused"))
        ctx.smtp_streak += 1
        if ctx.smtp_streak >= 2 or not per_recipient:   # connection / TLS / login / DATA problems are not about one recipient
            trip(ctx, sub, "SMTP failure before DATA: %s" % sanitize(e, 160))
        return "send_failed", sanitize(e)
    except Exception as e:  # AmbiguousSendError or anything unexpected after we started to send: leave Claimed
        log.error("AMBIGUOUS send for %s: %s. The log row stays Claimed; a human must check.", sub.id, sanitize(e))
        ctx.event(sub, "ambiguous", "send outcome unknown (%s); row left Claimed, check whether the mail arrived" % sanitize(e, 120))
        ctx.smtp_streak += 1
        trip(ctx, sub, sanitize(e, 160))   # an unknown outcome or an unexpected final reply: stop and look
        return "ambiguous", sanitize(e)
    ctx.smtp_streak = 0
    final = "Released" if (not cfg.ses and cfg.local_release) else "Sent"
    msg = "%s; %d recipient(s) hash %s; %d B%s" % ("test delivery to local SMTP" if final == "Released" else "accepted by SMTP",
                                                   len(to), addr_hash(live.emails), len(raw),
                                                   "; smtp_id=%s" % smtp_id if smtp_id else "")
    if not set_status(cur, log_id, "Sent" if final == "Sent" else "Released", msg):
        log.error("MAIL SENT for %s but the log row could not be updated; do NOT resend, set the row to Sent by hand", sub.id)
        ctx.event(sub, "sent_unlogged", "mail was sent but the log row is still Claimed: set it to Sent by hand")
        return "sent_unlogged", ""
    return "sent", "%s; %s" % (fmt, msg)


# ---------- alerts ----------
def load_state(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(path, state):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    ops.atomic_write(path, json.dumps(state))


ALERT_TEXT = {
    "export_failed": "export failed", "send_failed": "SMTP failed repeatedly", "ambiguous": "send outcome unknown",
    "timed_out": "claim timed out", "duplicate_key": "possible duplicate subscription", "oversize": "message too large",
    "no_recipient": "no recipient address", "no_format": "no format set", "sent_unlogged": "sent but not logged",
    "not_ready": "still not ready after the cutoff", "mapping_stale": "mapping outdated",
    "duplicate_active": "possible duplicate (still sent)", "startup": "run could not start", "refresh_probe_failed": "Tableau job lookup keeps failing",
    "rejected": "mail rejected by the server (nothing queued)", "smtp_breaker": "sending stopped (circuit breaker)",
    "halted": "takeover halted", "schedule_unknown": "schedule unknown or unsupported", "missed_due": "due occurrence never sent",
    "no_allowed_recipient": "recipient not on the allow-list", "takeover_closed": "taken-over subscription closed (gone from Tableau)",
    "restore_failed": "restore did not complete", "pat_busy": "Tableau PAT lock held for a long time", "pause_failed": "pause did not complete",
    "definition_changed": "pause changed the definition", "repaused": "re-paused", "killed": "kill switch active", "kill_switch": "kill switch active",
    "refresh_failed": "extract refresh failed", "refresh_gave_up": "extract refresh gave up (needs a human)",
    "run_stuck": "run did not finish (killed or hung)", "script_stopped": "script is not running", "refresh_stuck": "extract refresh stuck",
    "t2_failed": "T2 mapping sync failed", "t2_partial": "T2 mapping sync partly failed",
    "not_paused": "taken over but not suspended in Tableau", "new_native": "new Tableau subscription on a taken-over workbook", "repaused_today": "re-paused today, not sent today",
}


def build_alert(cfg, today, events, unreadable, to_header, intro=None, footer=True):
    msg = EmailMessage()
    msg["Subject"] = "Tableau subscription automation: %d subscription(s) need attention (%s)" % (len(events), today)
    msg["From"] = formataddr((cfg.sender, cfg.sender))
    msg["To"] = to_header
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.sender.split("@")[-1])
    lines = [intro or "These subscriptions were NOT sent on %s and need a person:" % today, ""]
    for e in events:
        lines.append("- [%s] %s (subscription %s): %s" % (ALERT_TEXT.get(e["code"], e["code"]), e["wb"], e["sub"], e["text"]))
    if unreadable:
        lines += ["", "Images too thin to read after the %d px width cap (design fix needed, mail still sent):" % sm.MAX_IMAGE_WIDTH]
        lines += ["- subscription %s, view '%s': %dx%d px" % u for u in unreadable]
    if footer:
        lines += ["", "Rows left Claimed or TimedOut in Integration.Tableau_SubscriptionSendLog block that subscription for the day "
                  "until a person sets them to Released (not sent, resend allowed), Failed or Sent."]
    msg.set_content("\n".join(lines) + "\n")
    return msg


def _key(e):
    return "%s|%s" % (e["sub"], e["code"])


def fresh_events(cfg, day, events):
    """(events to alert now, set of keys already alerted today). An event whose alert mail had an UNKNOWN outcome (timeout or error
    after DATA) is eligible once more on a later run (state '<day>#retry'), so a lost alert is not silently lost for the day."""
    st = load_state(cfg.alert_state)
    seen, retry = set(st.get(str(day), [])), set(st.get(str(day) + "#retry", []))
    return [e for e in events if _key(e) not in seen or _key(e) in retry], seen


def deliver_alerts(cfg, mailer, day, events, unreadable=(), intro=None, footer=True, subject=None):
    """Mail the events not yet alerted today as ONE alert. The state is written BEFORE the send, so an alert that went
    out is never repeated every run; it is taken back when the mail certainly did not leave (error before DATA). When the
    outcome is unknown (ambiguous) the events are retried ONCE on the next run (they may be mailed twice, never lost).
    Returns the number of events mailed."""
    fresh, seen = fresh_events(cfg, day, events)
    if not fresh:
        return 0
    to, _ = filter_allowed(cfg, [cfg.alert_to])
    if not to:
        log.error("Alert recipient is not on SEND_ALLOWED_RECIPIENTS (or is not a valid address): alert NOT sent (%d item(s)); see the log", len(fresh))
        ops.event_log("alerts", "alert recipient is not on SEND_ALLOWED_RECIPIENTS (or not a valid address): %d alert item(s) were NOT sent" % len(fresh))
        return 0
    keys = {_key(e) for e in fresh}
    st0 = load_state(cfg.alert_state)
    retry0, done0 = set(st0.get(str(day) + "#retry", [])), set(st0.get(str(day) + "#done", []))
    retrying = keys & retry0
    retry, done = retry0 - retrying, done0 | retrying

    def write(seen_keys, retry_keys, done_keys):
        save_state(cfg.alert_state, {str(day): sorted(seen_keys), str(day) + "#retry": sorted(retry_keys), str(day) + "#done": sorted(done_keys)})
    try:
        write(seen | keys, retry, done)
    except Exception as e:
        log.error("Alert state file not writable (%s); sending anyway", type(e).__name__)
    msg = build_alert(cfg, day, fresh, unreadable, ", ".join(to) if cfg.ses else LOCAL_TO, intro, footer)
    if subject:
        del msg["Subject"]
        msg["Subject"] = subject % (len(fresh), day)
    try:
        mailer.send(msg.as_bytes(policy=email.policy.SMTP), cfg.sender if cfg.ses else LOCAL_FROM, to)
    except PreDataError as e:
        log.error("Alert mail failed before DATA (%s); will retry next run", sanitize(e))
        try:
            write(seen, retry0, done0)
        except Exception:
            pass
        return 0
    except Exception as e:
        again = keys - done
        log.error("Alert mail outcome unknown (%s); %s", sanitize(e), "retried once on the next run" if again else "already retried once, not again")
        try:
            write(seen | keys, retry | again, done)
        except Exception:
            pass
        return len(fresh)
    log.info("Alert mail sent with %d item(s)", len(fresh))
    return len(fresh)


def send_alerts(cur, ctx, summary):
    """One alert mail for events not yet alerted today. Returns the number of events mailed."""
    cfg = ctx.cfg
    events = list(ctx.events)
    if sgt_now().time() >= cfg.cutoff:
        events += [{"sub": s.id, "wb": s.wb_name, "code": "not_ready", "text": why} for s, why in ctx.not_ready]
    fresh, _ = fresh_events(cfg, ctx.today, events)
    summary["alerts"] = len(fresh)
    if not fresh:
        return 0
    if not cfg.execute:
        log.info("DRY RUN: would send one alert mail with %d item(s) (not sent)", len(fresh))
        return 0
    return deliver_alerts(cfg, ctx.mailer, ctx.today, events, ctx.unreadable)


def fatal(cfg, mailer, what):
    """The run could not start or finish (DWH connect, Tableau sign-in): log a RUN SUMMARY line, try to alert, exit code 1."""
    what = sanitize(what, 200)
    log.error("RUN SUMMARY mode=%s transport=%s FATAL=%s needs_human=1%s", "execute" if cfg.execute else "dry-run",
              "ses" if cfg.ses else "local", what, " allow_all_uat=1" if cfg.allow_all else "")
    if cfg.execute:
        try:
            deliver_alerts(cfg, mailer, sgt_now().date(), [{"sub": "-", "wb": "(whole run)", "code": "startup", "text": what}])
        except Exception as e:
            log.error("Startup alert could not be sent (%s)", type(e).__name__)
    ops.write_marker("t6", "execute" if cfg.execute else "dry-run", 1, "FATAL " + what, outcome="fatal")
    return 1


# ---------- run ----------
BAD_KINDS = ("export_failed", "send_failed", "ambiguous", "oversize", "sent_unlogged", "timed_out", "gave_up",
             "duplicate_key", "no_recipient", "no_format", "lookup_failed", "mapping_stale", "rejected", "halted",
             "no_allowed_recipient", "schedule_unknown", "preflight_failed")


def account_skipped(ctx, sub):
    """A subscription the loop did not process (budget used up, circuit breaker, kill switch) must still be accounted for: a missed
    occurrence is alerted and a due-but-not-ready one is listed for the after-cutoff alert (R1 review #7)."""
    try:
        if ctx.due is None or not (sub.taken_over or ctx.cfg.include_untaken):
            return
        r = ctx.due.status(sub.id, sub.sched_id, sub.paused_at if sub.taken_over else None)
        if r.missed:
            ctx.event(sub, "missed_due", "the occurrence of %s was never sent and its catch-up window (%d day(s)) has ended" % (
                r.missed, ctx.cfg.catch_up_days))
        if r.status == "due":
            ok, kind, why = ctx.gate.check(sub.wb_id)
            if not ok:
                ctx.not_ready.append((sub, why))
    except Exception as e:
        log.warning("accounting for the skipped subscription %s failed (%s)", sub.id, type(e).__name__)


def run(cur, ctx, subs):
    """One scheduler run over `subs`. Returns the summary dict."""
    cfg = ctx.cfg
    summary = {"kinds": Counter(), "timed_out_now": [], "alerts": 0}
    stale = sweep_stale(cur, cfg, cfg.execute)
    summary["timed_out_now"] = stale
    for lid, sid in stale:
        log.error("TIMED OUT claim: send log row %s of subscription %s was Claimed for over %d min%s",
                  lid, sid, cfg.claim_timeout_min, "" if cfg.execute else " (dry run: not changed)")
        ctx.events.append({"sub": sid, "wb": "(see log)", "code": "timed_out",
                           "text": "claim older than %d min became TimedOut; check whether the mail arrived" % cfg.claim_timeout_min})
    ctx.prior = load_prior(cur, ctx.today, cfg.id_like)
    ctx.taken_wbs = {r[0] for r in cur.execute("SELECT DISTINCT Workbook_id FROM Integration.Tableau_SubscriptionBackup "
                                               "WHERE TakenOver = 1 AND RestoredAt IS NULL AND Subscription_id LIKE ?", cfg.id_like)}
    log.info("%s run for Singapore date %s, transport=%s: %d candidate subscription(s); ETL latest rows today: %s",
             "EXECUTE" if cfg.execute else "DRY", ctx.today, "SES" if cfg.ses else "local", len(subs),
             {p: ("OK" if ok else "FAILED") + " " + e.strftime("%H:%M") for p, (e, ok) in sorted(ctx.gate.etl.items())} or "none")
    t0 = time.monotonic()
    warned = False
    for sub in subs:
        if time.monotonic() - t0 >= cfg.budget_min * 60:
            if not warned:
                log.warning("Run budget of %s min used up: no new subscriptions are claimed, the rest wait for the next run", cfg.budget_min)
                warned = True
            summary["kinds"]["deferred"] += 1
            account_skipped(ctx, sub)
            continue
        if ctx.killed:
            summary["kinds"]["killed"] += 1
            account_skipped(ctx, sub)
            continue
        if ctx.breaker:
            summary["kinds"]["deferred_breaker"] += 1   # circuit breaker tripped: nothing further is claimed, next run retries
            account_skipped(ctx, sub)
            continue
        try:
            kind, detail = process(cur, sub, ctx)
        except Exception as e:  # a bug must not stop the batch; anything after a claim stays Claimed
            log.exception("Unexpected error for subscription %s", sub.id)
            kind, detail = "error", type(e).__name__
        summary["kinds"][kind] += 1
        lvl = logging.WARNING if kind in BAD_KINDS or kind == "error" else logging.INFO
        log.log(lvl, "%s -> %s '%s' [%s] %s", sub.id, kind.upper(), sub.wb_name, sub.fmt, detail)
    if cfg.execute and (ctx.probe_fail or ctx.probe_ok):
        n = ops.note_streak("t6_refresh_probe", bool(ctx.probe_fail))
        if n >= 3:
            ctx.events.append({"sub": "-", "wb": "(whole run)", "code": "refresh_probe_failed", "text":
                               "the Tableau refresh-job lookup failed in %d runs in a row: reports whose extracts cannot be checked are not exported" % n})
    send_alerts(cur, ctx, summary)
    return summary


def summary_line(ctx, s):
    k = s["kinds"]
    bad = sum(k[x] for x in BAD_KINDS) + k["error"] + sum(e["code"] == "missed_due" for e in ctx.events)
    return ("RUN SUMMARY mode=%s transport=%s candidates=%d sent=%d would_send=%d not_ready=%d blocked=%d already_sent=%d "
            "in_progress=%d claim_lost=%d export_failed=%d send_failed=%d ambiguous=%d oversize=%d timed_out=%d "
            "mapping_stale=%d deferred=%d no_allowed_recipient=%d unreadable_images=%d format_warnings=%d alerts=%d "
            "not_taken_over=%d not_paused=%d repaused_today=%d deleted_on_tableau=%d not_due=%d taken_over_today=%d rejected=%d halted=%d "
            "schedule_unknown=%d deferred_breaker=%d breaker=%d awaiting_sync=%d schedule_mode=%s needs_human=%d" % (
                "execute" if ctx.cfg.execute else "dry-run", "ses" if ctx.cfg.ses else "local", sum(k.values()),
                k["sent"], k["would_send"], k["not_ready"], k["blocked"], k["already_sent"], k["in_progress"], k["claim_lost"],
                k["export_failed"], k["send_failed"], k["ambiguous"], k["oversize"], k["timed_out"] + len(s["timed_out_now"]),
                k["mapping_stale"], k["deferred"], k["no_allowed_recipient"], len(ctx.unreadable), len(ctx.format_warnings),
                s["alerts"], k["not_taken_over"], k["not_paused"], k["repaused_today"], k["deleted_on_tableau"], k["not_due"], k["taken_over_today"],
                k["rejected"], k["halted"], k["schedule_unknown"], k["deferred_breaker"], int(bool(ctx.breaker)), k["awaiting_sync"],
                "honour" if ctx.cfg.schedule_enforce else "ignore", bad)) + (" allow_all_uat=1" if ctx.cfg.allow_all else "")


def exit_code(ctx, s):
    k = s["kinds"]
    return 1 if (s["timed_out_now"] or k["error"] or any(k[x] for x in BAD_KINDS)
                 or (ctx is not None and (ctx.breaker or any(e["code"] == "missed_due" for e in ctx.events)))) else 0


def setup_logging():
    """One console handler and ONE daily file handler (ops.log_file_handler), shared by this script's logger and t4's (LazyTableau logs there)."""
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handlers = None
    for lg in (log, t4.log, ops.log):
        lg.setLevel(logging.INFO)
        if lg.handlers:
            continue
        if handlers is None:
            sh = logging.StreamHandler(sys.stdout)
            fh = ops.log_file_handler("t6_send")
            for h in (sh, fh):
                h.setFormatter(fmt)
            handlers = (sh, fh)
        for h in handlers:
            lg.addHandler(h)

    def hook(et, ev, tb):
        log.critical("Unhandled error", exc_info=(et, ev, tb))
    sys.excepthook = hook


class ConfigError(Exception):
    pass


def build_config(a, env):
    """Validate the arguments against `env` (a dict) and build the Config. Raises ConfigError. Real sending is possible
    only with --execute and --smtp (or MAIL_TRANSPORT=smtp) AND a non-empty SEND_ALLOWED_RECIPIENTS AND SMTP credentials in `env`."""
    if a.ses and a.local_smtp:
        raise ConfigError("--ses and --local-smtp are mutually exclusive")
    try:
        ses_env = (not a.local_smtp) and bool(a.execute) and ops.mail_transport(env) == "smtp"   # MAIL_TRANSPORT=smtp (D-042, D-043)
    except ValueError as e:
        raise ConfigError(str(e))
    if a.as_of_date and a.execute:
        raise ConfigError("--as-of-date is for dry runs only")
    if a.ses and not a.execute:
        raise ConfigError("--ses only makes sense with --execute")
    ses = bool(a.ses) or ses_env
    if ses and getattr(a, "include_not_taken_over", False):
        raise ConfigError("--include-not-taken-over is for dev/local runs only: real sending is limited to taken-over subscriptions")
    allow = parse_allow(env.get("SEND_ALLOWED_RECIPIENTS"))
    allow_all = ops.allow_all_uat(env)[1]   # D-050: ignored (False) unless TABLEAU_SERVER_URL is exactly the UAT host
    try:
        mode = ops.schedule_mode(env, getattr(a, "schedule_mode", None))
    except ValueError as e:
        raise ConfigError(str(e))
    if ses:
        try:
            st = ops.smtp_settings(env)
        except ValueError as e:
            raise ConfigError(str(e))
        if not (allow or allow_all) or not st["user"] or not st["password"]:
            raise ConfigError("Refusing to send for real: SEND_ALLOWED_RECIPIENTS is empty or SMTP_USER/SMTP_PASSWORD "
                              "(or the SES_SMTP_* aliases) are not set")
        if allow_all and not ops.ADDR_RE.fullmatch((env.get("ALERT_TO") or env.get("OWNER_EMAIL") or DEFAULT_ALERT_TO).strip()):
            raise ConfigError("Refusing to send for real: ALERT_TO is not a valid e-mail address")
    try:
        size_mb = a.size_limit_mb if a.size_limit_mb is not None else ops.default_size_limit_mb(env)
    except ValueError as e:
        raise ConfigError(str(e))
    return Config(execute=a.execute, ses=ses, export_in_dry_run=a.export, allow=allow, allow_all=allow_all,
                  sender=env.get("SEND_FROM") or sm.default_sender(env), size_limit=int(size_mb * 1048576),
                  alert_to=env.get("ALERT_TO") or env.get("OWNER_EMAIL") or DEFAULT_ALERT_TO, schedule_enforce=(mode == "honour"),
                  catch_up_days=getattr(a, "catch_up_days", None) if getattr(a, "catch_up_days", None) is not None else sd.catch_up_default(env),
                  export_pause_s=float(env.get("EXPORT_PAUSE_SEC", 20)), max_sends_per_run=int(env.get("MAX_SENDS_PER_RUN", 10)),
                  cutoff=dt.time.fromisoformat(a.cutoff), max_mapping_age_h=a.max_mapping_age_h,
                  include_untaken=getattr(a, "include_not_taken_over", False),
                  alert_state=ops.state_dir(env) / ("t6_alert_state.json" if ses else "t6_alert_state_local.json"))


# ---------- one-off sample send (D-054) ----------
SAMPLE_PREFIX = "[SAMPLE] "


def check_sample_args(a):
    """None when the arguments are a valid sample request, else the reason it is refused. The sample address must be ONE plain address."""
    ids = getattr(a, "subscription_id", None) or []
    to = getattr(a, "sample_to", None)
    if not getattr(a, "sample_send", False):
        return "--sample-to only makes sense with --sample-send" if to else None
    if not to:
        return "--sample-send needs --sample-to <one e-mail address>"
    if len(to) > 254 or to != to.strip() or not ops.ADDR_RE.fullmatch(to) or to.count("@") != 1:
        return "--sample-to must be exactly ONE plain e-mail address (no list, spaces, commas or semicolons)"
    if len(ids) != 1:
        return "--sample-send needs exactly one --subscription-id"
    if getattr(a, "workbook", None) or getattr(a, "as_of_date", None) or getattr(a, "include_not_taken_over", False):
        return "--sample-send cannot be combined with --workbook, --as-of-date or --include-not-taken-over"
    return None


def sample_send(cur, sub, ctx, to_addr):
    """Mail ONE subscription's real output (same export and layout as a real send, subject prefixed '[SAMPLE] ') to to_addr ONLY.
    Bypasses the takeover-day, schedule, readiness/freshness gates and the takeover requirement; never reads the subscriber's address, never
    writes the send log (so a real send of the same subscription the same day is unaffected). Returns (ok, text)."""
    cfg = ctx.cfg
    active, why = ops.kill_switch_active()
    if active:
        return False, "kill switch active (%s): nothing done" % why
    age_h = (sgt_now().replace(tzinfo=None) - sub.synced_at).total_seconds() / 3600 if sub.synced_at else None
    if age_h is None or age_h > cfg.max_mapping_age_h:
        return False, "the T2 mapping sync last saw this subscription %s ago (limit %.0f h)" % (
            "?" if age_h is None else "%.1f h" % age_h, cfg.max_mapping_age_h)
    to, _ = filter_allowed(cfg, [to_addr])
    if not to:
        return False, "the sample address is not allowed (not on SEND_ALLOWED_RECIPIENTS, or not well-formed)"
    try:
        live = ctx.adapter.describe(sub, recipients=False)
    except Exception as e:
        return False, ("the subscription no longer exists on Tableau" if is_not_found(e) else "Tableau lookup failed (%s)" % type(e).__name__)
    if not (live.attach_pdf or live.attach_image):
        return False, "neither PDF nor image is set on the subscription"
    if live.attach_pdf and sm.page_problems(live.page_size, live.orientation):
        log.warning("FORMAT WARNING sub=%s: unknown or missing page size/orientation; Tableau's default will be used", sub.id)
    live = dataclasses.replace(live, subject=SAMPLE_PREFIX + (live.subject or sub.subject or live.label), emails=[])
    if cfg.ses and cfg.execute:
        pf = getattr(ctx.mailer, "preflight", None)
        if pf:
            try:
                pf(cfg.sender, preflight_rcpt(cfg))
            except Exception as e:
                return False, "SMTP preflight failed: %s" % sanitize(e, 200)
    raw, err = None, ""
    for attempt in range(1, cfg.max_export_attempts + 1):
        try:
            raw, _, _ = render(sub, live, to, ctx)
            break
        except Exception as e:
            err = sanitize(e)
            log.error("Sample export failed for %s (attempt %d of %d): %s", sub.id, attempt, cfg.max_export_attempts, err)
            if attempt < cfg.max_export_attempts and cfg.export_pause_s:
                time.sleep(cfg.export_pause_s)
    if raw is None:
        return False, "export failed: %s" % err
    if len(raw) > cfg.size_limit:
        return False, "message is %.1f MB, limit %.0f MB; not sent" % (len(raw) / 1048576, cfg.size_limit / 1048576)
    if not cfg.execute:
        return True, "dry run: would send %d B to the sample address (add --execute)" % len(raw)
    try:
        smtp_id = ctx.mailer.send(raw, cfg.sender if cfg.ses else LOCAL_FROM, to)
    except Exception as e:
        return False, "send failed or outcome unknown (%s: %s); check the mailbox before trying again" % (type(e).__name__, sanitize(e, 160))
    return True, "sample sent to %s; %d B%s" % ("the sample address" if cfg.ses else "the local test SMTP", len(raw),
                                                "; smtp_id=%s" % smtp_id if smtp_id else "")


def run_sample(a):
    """Entry point of --sample-send. Exit 0 = sent (or dry run), 1 = refused or failed. Alerts are NOT mailed: the console says what went wrong."""
    bad = check_sample_args(a)
    if bad:
        print(bad, file=sys.stderr)
        return 1
    try:
        cfg = build_config(a, os.environ)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 1
    setup_logging()
    ops.log_transport("t6", cfg.ses, cfg.execute, os.environ)
    active, why = ops.kill_switch_active()
    if active:
        log.error("KILL SWITCH ACTIVE (%s): the sample send does nothing", why)
        return 1
    mailer = make_mailer(cfg, a.local_host, a.local_port)
    try:
        conn = t4.connect()
        cur = conn.cursor()
        today = t4.dwh_today(cur)
    except (Exception, SystemExit) as e:
        print("DWH connect or guard failed: %s" % (e if isinstance(e, SystemExit) else type(e).__name__), file=sys.stderr)
        return 1
    subs = load_subscriptions(cur, set(a.subscription_id))
    if len(subs) != 1:
        print("subscription %s is not an active subscription in the mapping" % a.subscription_id[0], file=sys.stderr)
        return 1
    tableau = t4.LazyTableau(cur, owner="t6")
    try:
        ctx = Ctx(cfg, TableauAdapter(tableau, render_as=render_as_subscriber()), mailer, None, today)
        ok, text = sample_send(cur, subs[0], ctx, a.sample_to)
    except t4.PatBusy:
        ok, text = False, "another script holds the Tableau PAT session; try again in a few minutes"
    except SystemExit:
        ok, text = False, "Tableau sign-in failed"
    except Exception as e:
        ok, text = False, "failed: %s" % type(e).__name__
    finally:
        tableau.close()
    line = "RUN SUMMARY sample=1 mode=%s transport=%s sub=%s %s=%s%s" % (
        "execute" if cfg.execute else "dry-run", "ses" if cfg.ses else "local", subs[0].id, "result" if ok else "FAILED", text,
        " allow_all_uat=1" if cfg.allow_all else "")
    (log.info if ok else log.error)(line)
    if ok and cfg.execute and cfg.ses:
        ops.event_log("t6", "SAMPLE SEND of subscription %s was mailed to a single sample address (sample=1)" % subs[0].id, "INFORMATION")
    print(("SAMPLE OK: " if ok else "SAMPLE FAILED: ") + text)
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="default: evaluate and list, claim and send nothing")
    g.add_argument("--execute", action="store_true", help="claim, export and send (local test SMTP unless --ses)")
    ap.add_argument("--local-smtp", action="store_true", help="deliver to the local test SMTP (the default)")
    ap.add_argument("--smtp", "--ses", dest="ses", action="store_true",
                    help="REAL sending via the SMTP server in SMTP_HOST (SES, Gmail, ...); also needs SEND_ALLOWED_RECIPIENTS. "
                         "Or set MAIL_TRANSPORT=smtp in .env")
    ap.add_argument("--local-host", default="127.0.0.1")
    ap.add_argument("--local-port", type=int, default=1025)
    ap.add_argument("--as-of-date", help="dry run only: evaluate readiness as if this Singapore date were today")
    ap.add_argument("--export", action="store_true", help="dry run: also export and size each mail (reads Tableau, sends nothing)")
    ap.add_argument("--subscription-id", action="append", help="only this subscription (repeatable)")
    ap.add_argument("--workbook", help="only subscriptions of this workbook (name or id)")
    ap.add_argument("--include-not-taken-over", action="store_true",
                    help="dev/local only (refused with --ses): also process non-suspended subscriptions t11 has not taken over")
    ap.add_argument("--size-limit-mb", type=float, default=None, help="default 25, or 20 for SMTP_PROVIDER=gmail")
    ap.add_argument("--max-mapping-age-h", type=float, default=default_max_age_h(),
                    help="refuse to send when the T2 sync last saw the subscription longer ago than this (default 26, env MAPPING_MAX_AGE_H)")
    ap.add_argument("--schedule-mode", choices=("honour", "ignore"), default=None,
                    help="honour (default, env SCHEDULE_MODE): send only on the subscription's schedule days (D-036); "
                         "ignore: the old rule, once per day whenever the data is ready")
    ap.add_argument("--catch-up-days", type=int, default=None, help="days after a due day on which it may still be sent (default 3, env SCHEDULE_CATCHUP_DAYS)")
    ap.add_argument("--cutoff", default=os.environ.get("T6_NOT_READY_CUTOFF", DEFAULT_CUTOFF),
                    help="HH:MM Singapore; 'not ready' becomes an alert from this time (default 15:30)")
    ap.add_argument("--sample-send", action="store_true",
                    help="ONE-OFF sample (D-054): mail the real output of exactly one --subscription-id to --sample-to only; "
                         "bypasses the takeover-day/schedule/readiness gates, writes no send-log row. Needs --execute for a real mail")
    ap.add_argument("--sample-to", help="the single address the sample goes to (never the subscriber)")
    a = ap.parse_args(argv)
    if a.sample_send or a.sample_to:
        return run_sample(a)
    try:
        cfg = build_config(a, os.environ)
    except ConfigError as e:
        sys.exit(str(e))
    setup_logging()
    ops.log_transport("t6", cfg.ses, cfg.execute, os.environ)
    if cfg.ses:
        st = ops.smtp_settings(os.environ)
        log.info("SMTP provider %s via %s:%d", st["provider"], st["host"], st["port"])
        if ops.salt_warning():
            log.warning(ops.salt_warning())
        w = ops.sender_warning(st["provider"], cfg.sender, st["user"])
        if w:
            log.warning(w)
    if ops.check_kill_switch("t6", cfg.execute, cfg.ses, a.local_host, a.local_port):
        return 0
    ops.check_alert_recipient(cfg, "t6")
    mailer = make_mailer(cfg, a.local_host, a.local_port)
    try:
        conn = t4.connect()
        cur = conn.cursor()
        real_today = t4.dwh_today(cur)
    except (Exception, SystemExit) as e:
        return fatal(cfg, mailer, "DWH connect or guard failed: %s" % (e if isinstance(e, SystemExit) else type(e).__name__))
    today = dt.date.fromisoformat(a.as_of_date) if a.as_of_date else real_today
    if a.execute:
        got = cur.execute("DECLARE @r int; EXEC @r = sp_getapplock @Resource = ?, @LockMode = 'Exclusive', "
                          "@LockOwner = 'Session', @LockTimeout = 0; SELECT @r", LOCK_NAME).fetchone()[0]
        if got < 0:
            log.warning("Another run holds the lock; exiting")
            return 0
    tableau = t4.LazyTableau(cur, owner="t6")   # takes the shared PAT session lock before the first sign-in
    try:
        subs = load_subscriptions(cur, set(a.subscription_id or []), a.workbook)
        ctx = Ctx(cfg, TableauAdapter(tableau, render_as=render_as_subscriber()), mailer, Gate(cur, today), today)
        if cfg.schedule_enforce:
            ctx.due = sd.DueEngine.load(cur, today, cfg.catch_up_days, cfg.max_mapping_age_h)
        s = run(cur, ctx, subs)
    except t4.PatBusy:
        log.info("RUN SUMMARY mode=%s skipped=pat_busy (another script holds the Tableau PAT session)",
                 "execute" if cfg.execute else "dry-run")
        ops.on_pat_busy("t6", cfg.execute, cfg=cfg, mailer=mailer)
        ops.write_marker("t6", "execute" if cfg.execute else "dry-run", 0, "skipped=pat_busy", outcome="pat_busy")
        return 0
    except SystemExit:  # Tableau sign-in failed (logged by LazyTableau)
        return fatal(cfg, mailer, "Tableau sign-in failed")
    except Exception as e:
        log.exception("Run failed")
        return fatal(cfg, mailer, "run failed: %s" % type(e).__name__)
    finally:
        tableau.close()
    ops.pat_ok("t6")
    line = summary_line(ctx, s)
    log.info(line)
    code = exit_code(ctx, s)
    ops.write_marker("t6", "execute" if cfg.execute else "dry-run", code, line)
    return code


if __name__ == "__main__":
    sys.exit(main())
