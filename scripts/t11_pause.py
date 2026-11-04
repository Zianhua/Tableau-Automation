"""T11a: take over the old Tableau subscriptions. Pause them (suspend, never delete, D-020), keep them paused, and restore
them on request. t6_send.py then sends the taken-over subscriptions itself (D-035).

Takeover set: active subscriptions (Integration.Tableau_Subscription) whose workbook has more than one data source (T2
tables) AND is enabled by an EXPLICIT allow-list (D-013: one pilot workbook first). Empty allow-list = nothing new is taken
over. Allow-list: --workbooks (names or ids, comma list) and/or --subscription-ids; if neither is given the env variables
TAKEOVER_WORKBOOK_IDS / TAKEOVER_SUBSCRIPTION_IDS are used. It only gates NEW takeovers: a subscription that is already
taken over (open row in Integration.Tableau_SubscriptionBackup) is kept paused whatever the allow-list says, because t6 sends it.
Never taken over: a workbook t6 cannot send (mapping not Ready, unknown pipeline, ...) unless it is named in the allow-list
AND --force-notready is given; a subscription restored by --restore (unless --retake-restored); one that does not match its last
T2 sync. Suspended is NOT a special category (D-046, reverses D-023): a subscription that is already suspended in Tableau is taken
over like any other (backup row, nothing to pause, PausedAt set at once) and then sent. Whatever must not be sent is deleted in
Tableau by the user; --restore simply un-suspends.

For each new takeover, in this order (nothing is paused before the backup row is committed):
  1. backup of the definition (id, target, owner id, schedule id and name, subject, message, PDF/image flags, page size,
     orientation, time) to Integration.Tableau_SubscriptionBackup (TakenOver = 1, PausedAt NULL) and to
     out/t11_backup/<time>_<subscription>.json. No recipient address is stored.
  2. pause: server.subscriptions.update(item) with item.suspended = True (tableauserverclient 0.42, PUT .../subscriptions/<id>
     with subject, attachImage, attachPdf, pageOrientation, pageSizeOption, suspended, schedule id, sendIfViewEmpty; the
     library does NOT send 'message', so Tableau must leave it unchanged). The subscription is read back and compared with
     the definition before the pause; any difference other than suspended stops the whole run (halted) and raises an alert.
  3. PausedAt is set in the backup table. A subscription found already suspended skips the PUT (nothing to change) and gets PausedAt. (If this step fails, the next run sees it suspended and confirms it.)
Re-pause: on every run, a taken-over subscription found NOT suspended in Tableau (someone resumed it) is paused again,
logged, counted as 'repaused', and listed in ONE alert mail to the owner (ALERT_TO / OWNER_EMAIL) (t6's mail path and allow-list
rules, local transport unless --ses). t6 does not send that subscription on the day it was re-paused (no double mail).
A subscription deleted on Tableau is only logged ('gone'): T2 deactivates it and t6 stops sending it.

Safety fences (R5, R3B_REVIEW F-2): --execute with an allow-list REQUIRES --owner-user-id (or TAKEOVER_OWNER_USER_IDS). At most --max-new
(env TAKEOVER_MAX_NEW, default 10) NEW takeovers happen per run; the rest are logged, alerted (takeover_cap) and wait for the next run.
For the pilot use --subscription-ids (one subscription), not a workbook id.

Restore (rollback, one command): --restore <subscription id> (repeatable) or --restore-all-for-workbook <name or id>. The
backup row is marked restored FIRST (t6 stops sending, t11 stops re-pausing), then the subscription is resumed in Tableau.
Re-running --restore for a restored subscription retries only the resume. R5 / F-4: --restore is REFUSED (exit 1, nothing changed) for a
subscription the script already sent, or started to send, today, because the resumed Tableau schedule could mail it a second time;
--allow-restore-after-send overrides.

ORDER: a subscription must be paused BEFORE the first t6 --execute send for it, otherwise Tableau's own schedule and the
script both mail it. t6 enforces this: it sends only rows with PausedAt set and the subscription suspended in Tableau.
Schedule t11 before t6 in each 15 minute cycle. Both use the shared PAT session lock (busy = skip, exit 0).

Usage:
  python scripts/t11_pause.py --workbooks "CFD Accounts"             # dry run (default): prints, writes nothing
  python scripts/t11_pause.py --workbooks "CFD Accounts" --execute   # backup + pause (WRITES to Tableau and the dev DWH)
  python scripts/t11_pause.py --execute                              # keep taken-over subscriptions paused (re-pause only)
  python scripts/t11_pause.py --restore <id> --execute               # rollback one subscription
  python scripts/t11_pause.py --restore-all-for-workbook "CFD Accounts" --execute
Exit code 1 for a failed pause/restore, a lookup failure or a halt. Each run ends with one 'RUN SUMMARY' line.
Reads TABLEAU_PAT_NAME / TABLEAU_PAT_SECRET from .env; dev DWH only (guard in t4_refresh.connect).
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
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import t4_refresh as t4  # noqa: E402
import t6_send as t6  # noqa: E402
import schedule_due as sd  # noqa: E402
import ops  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOCK_NAME = "T11_pause"
ENV_WORKBOOKS = "TAKEOVER_WORKBOOK_IDS"
ENV_SUBSCRIPTIONS = "TAKEOVER_SUBSCRIPTION_IDS"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
BACKOFF_MIN = [15, 30, 60, 120, 240]     # waits after the 1st, 2nd, ... failed first pause of one subscription
ALERT_INTRO = "These taken-over subscriptions need a look (%s):"
ALERT_SUBJECT = "Tableau subscription automation: %d taken-over subscription(s) re-paused or not paused (%s)"

log = logging.getLogger("t11_pause")


# ---------- Tableau layer ----------
@dataclass
class SubDef:
    """A subscription as Tableau holds it. No recipient address (the owner is a user id)."""
    id: str
    suspended: bool
    target_id: str
    target_type: str
    user_id: str
    schedule_id: str
    subject: str
    message: str
    attach_pdf: bool
    attach_image: bool
    page_size: str
    orientation: str
    send_if_view_empty: object
    schedule_name: object = None
    raw_missing: object = None    # attributes the GET response did NOT contain (the library would read them as False)

    def definition(self):
        """Everything a pause must leave unchanged (None and '' are the same text)."""
        d = dataclasses.asdict(self)
        for k in ("suspended", "schedule_name", "raw_missing"):
            d.pop(k)
        return {k: ("" if v is None else v) if k in ("subject", "message", "page_size", "orientation") else v for k, v in d.items()}


def to_def(item):
    return SubDef(id=item.id, suspended=bool(item.suspended), target_id=item.target.id, target_type=item.target.type.capitalize(),
                  user_id=item.user_id, schedule_id=item.schedule_id, subject=item.subject or "", message=item.message or "",
                  attach_pdf=bool(item.attach_pdf), attach_image=bool(item.attach_image), page_size=item.page_size_option,
                  orientation=item.page_orientation, send_if_view_empty=item.send_if_view_empty)


class TableauAdapter:
    def __init__(self, tableau):
        self.t = tableau   # callable returning a signed-in server (LazyTableau)

    def raw_missing(self, sub_id):
        """R1: names of attributes (attachImage, attachPdf, suspended, sendIfViewEmpty) that the raw GET response does not contain.
        tableauserverclient reads a missing attribute as False and the update request would then WRITE that False. None when the
        raw response cannot be fetched (fake servers)."""
        s = self.t()
        gr = getattr(s.subscriptions, "get_request", None)
        if gr is None:
            return None
        import xml.etree.ElementTree as ET
        root = ET.fromstring(gr("%s/%s" % (s.subscriptions.baseurl, sub_id)).content)
        have = set()
        for el in root.iter():
            have |= set(el.attrib)
        return [k for k in ("attachImage", "attachPdf", "suspended", "sendIfViewEmpty") if k not in have]

    def get(self, sub_id, with_schedule=False):
        s = self.t()
        d = to_def(s.subscriptions.get_by_id(sub_id))
        d.raw_missing = self.raw_missing(sub_id)
        if with_schedule and d.schedule_id:
            try:
                d.schedule_name = s.schedules.get_by_id(d.schedule_id).name
            except Exception:
                d.schedule_name = None
        return d

    def set_suspended(self, sub_id, value):
        """THE write to Tableau: PUT with the item as read, suspended changed. Returns the subscription read back.
        An attribute the GET did not contain is set to None on the item, so the library does not write a default over it."""
        s = self.t()
        item = s.subscriptions.get_by_id(sub_id)
        missing = self.raw_missing(sub_id) or []
        if "attachImage" in missing:
            item._attach_image = None   # the public setter rejects None
        if "attachPdf" in missing:
            item._attach_pdf = None
        if "sendIfViewEmpty" in missing:
            item._send_if_view_empty = None
        item.suspended = value
        s.subscriptions.update(item)
        return to_def(s.subscriptions.get_by_id(sub_id))


def diff_fields(a, b):
    da, db = a.definition(), b.definition()
    return sorted(k for k in da if da[k] != db[k])


# ---------- config ----------
@dataclass
class Config:
    execute: bool = False
    workbooks: frozenset = frozenset()       # lower-cased names or ids
    sub_ids: frozenset = frozenset()         # lower-cased
    force_notready: bool = False
    retake_restored: bool = False
    max_mapping_age_h: float = ops.default_max_age_h()
    backup_dir: Path = ROOT / "out" / "t11_backup"
    id_like: str = "%"                       # tests only
    alert: object = None                     # t6.Config for the alert mail
    ses: bool = False
    owner_user_ids: frozenset = frozenset()  # R3: only subscriptions of these Tableau user ids are taken over (empty = no restriction)
    schedule_enforce: bool = False           # SCHEDULE_MODE=honour: skip subscriptions whose schedule t6 cannot evaluate
    catch_up_days: int = sd.DEFAULT_CATCH_UP_DAYS
    gone_close_days: int = 7                 # an open takeover row whose subscription is gone from Tableau this long is closed
    backoff_path: Path = ROOT / "out" / "t11_backoff.json"
    max_new: int = 10                        # R5 / F-2: at most this many NEW takeovers per run (the rest wait for the next run)
    allow_restore_after_send: bool = False   # R5 / F-4: --restore is refused for a subscription the script already mailed today


def parse_list(values):
    out = set()
    for v in values or []:
        out |= {x.strip().lower() for x in re.split(r"[,;]", v) if x.strip()}
    return frozenset(out)


def build_config(a, env):
    """Allow-list: the command line if given, else the environment. Alert transport = t6's rules (local unless --ses)."""
    if a.workbooks or a.subscription_ids:
        wbs, subs = parse_list(a.workbooks), parse_list(a.subscription_ids)
    else:
        wbs, subs = parse_list([env.get(ENV_WORKBOOKS, "")]), parse_list([env.get(ENV_SUBSCRIPTIONS, "")])
    six = SimpleNamespace(execute=a.execute, ses=a.ses, local_smtp=getattr(a, "local_smtp", False), as_of_date=None, export=False, size_limit_mb=t6.SIZE_LIMIT_MB,
                          cutoff=t6.DEFAULT_CUTOFF, max_mapping_age_h=a.max_mapping_age_h, include_not_taken_over=False)
    if a.execute:   # R1: a name is ambiguous across projects, so a real takeover needs workbook ids (luids)
        bad = sorted(w for w in wbs if not UUID_RE.match(w))
        if bad:
            raise t6.ConfigError("--execute needs workbook IDS (luids) in the allow-list, not names: %s. Dry run shows the matching ids."
                                 % ", ".join(bad))
    elif any(not UUID_RE.match(w) for w in wbs):
        log.warning("allow-list contains workbook NAMES: fine for a dry run (matched ids are shown), --execute needs ids")
    owners = parse_list(list(getattr(a, "owner_user_id", None) or []) + [env.get("TAKEOVER_OWNER_USER_IDS", "")])
    restoring = bool(getattr(a, "restore", None) or getattr(a, "restore_all_for_workbook", None))
    try:
        gmail_test = ops.mail_transport(env) == "smtp" and ops.smtp_provider(env) == "gmail"
    except ValueError:
        gmail_test = False
    if a.execute and not owners and not restoring and (wbs or subs):
        # R5 / F-2: the allow-list is workbook-wide, so an owner fence is REQUIRED for any real takeover run, not only in Gmail test mode.
        # (A run with an empty allow-list only keeps taken-over subscriptions paused and takes nobody new, so it needs no fence.)
        raise t6.ConfigError("--execute with an allow-list needs --owner-user-id <Tableau user id> (repeatable, or TAKEOVER_OWNER_USER_IDS): "
                             "t11 cannot see recipient addresses, and a workbook id alone takes over every subscription on that workbook, "
                             "whoever owns it.%s" % (" Gmail test mode: use the Tableau user id of the Gmail account owner." if gmail_test else ""))
    max_new = a.max_new if getattr(a, "max_new", None) is not None else env.get("TAKEOVER_MAX_NEW", 10)
    try:
        max_new = int(max_new)
    except (TypeError, ValueError):
        raise t6.ConfigError("--max-new / TAKEOVER_MAX_NEW must be a whole number, got %r" % (max_new,))
    if max_new < 0:
        raise t6.ConfigError("--max-new / TAKEOVER_MAX_NEW must be 0 or more")
    base = t6.build_config(six, env)
    alert = dataclasses.replace(base, alert_state=ops.state_dir(env) / ("t11_alert_state.json" if base.ses else "t11_alert_state_local.json"))
    return Config(execute=a.execute, workbooks=wbs, sub_ids=subs, force_notready=a.force_notready,
                  retake_restored=a.retake_restored, max_mapping_age_h=a.max_mapping_age_h, alert=alert, ses=alert.ses, owner_user_ids=owners,
                  schedule_enforce=alert.schedule_enforce, catch_up_days=alert.catch_up_days,
                  gone_close_days=int(env.get("TAKEOVER_GONE_CLOSE_DAYS", 7)), max_new=max_new,
                  allow_restore_after_send=bool(getattr(a, "allow_restore_after_send", False)))


# ---------- DWH ----------
@dataclass
class Row:
    backup_id: int
    sub_id: str
    wb_id: str
    wb_name: str
    taken_over: bool
    paused: bool
    halted: bool = False
    gone_since: object = None


WBNAME = ("(SELECT TOP 1 w.Workbook_Name FROM Integration.Tableau_WorkbookDataSource w WHERE w.Workbook_id = %s.Workbook_id "
          "ORDER BY w.Active_Flag DESC, w.LastSeenAt DESC, w.Workbook_Name)")
OPEN_SQL = """
SELECT b.Backup_id, b.Subscription_id, b.Workbook_id,
       """ + (WBNAME % "b") + """,
       b.TakenOver, CASE WHEN b.PausedAt IS NULL THEN 0 ELSE 1 END, ISNULL(b.Halted, 0), b.GoneSince
FROM Integration.Tableau_SubscriptionBackup b WHERE b.RestoredAt IS NULL AND b.Subscription_id LIKE ?
ORDER BY b.Backup_id"""

CAND_SQL = """
SELECT s.Subscription_id, s.Workbook_id,
       """ + (WBNAME % "s") + """,
       s.TargetType, s.TargetId, s.Format, s.LastSeenAt,
       CASE WHEN EXISTS (SELECT 1 FROM Integration.Tableau_SubscriptionBackup b
                         WHERE b.Subscription_id = s.Subscription_id AND b.RestoredAt IS NOT NULL) THEN 1 ELSE 0 END,
       CASE WHEN s.SubscriptionKeyHash IS NULL THEN 1 ELSE 0 END, s.ScheduleId
FROM Integration.Tableau_Subscription s WHERE s.Active_Flag = 1 AND s.Subscription_id LIKE ?
ORDER BY 3, s.Subject, s.Subscription_id"""


@dataclass
class Cand:
    id: str
    wb_id: str
    wb_name: str
    target_type: str
    target_id: str
    fmt: str
    synced_at: object
    was_restored: bool
    no_hash: bool = False        # t6 refuses a NULL SubscriptionKeyHash, so it is never taken over
    sched_id: object = None


def load_open(cur, id_like="%"):
    return {r[1]: Row(r[0], r[1], r[2], r[3] or "(unnamed workbook)", bool(r[4]), bool(r[5]), bool(r[6]), r[7])
            for r in cur.execute(OPEN_SQL, id_like).fetchall()}


def load_cands(cur, id_like="%"):
    return [Cand(r[0], r[1], r[2] or "(unnamed workbook)", r[3], r[4], r[5], r[6], bool(r[7]), bool(r[8]), r[9])
            for r in cur.execute(CAND_SQL, id_like).fetchall()]


def write_backup(cur, cfg, run_id, live, wb_id, row=None):
    """JSON file first, then the DWH row (autocommit: committed when execute returns). Returns the Backup_id. Raises on failure."""
    now = dt.datetime.now(t4.SGT)
    cfg.backup_dir.mkdir(parents=True, exist_ok=True)
    # one file per subscription and backup row, overwritten by a retry (a failing first pause must not litter the folder)
    f = cfg.backup_dir / ("%s_%s.json" % (row.backup_id if row is not None else "new", re.sub(r"[^\w.-]", "_", live.id)))
    f.write_text(json.dumps(dict(dataclasses.asdict(live), workbook_id=wb_id, backed_up_at=now.isoformat(),
                                 note="definition before takeover; no recipient addresses"), indent=2, default=str), encoding="utf-8")
    args = (live.target_type, live.target_id, live.user_id, live.schedule_id, live.schedule_name, live.subject[:500],
            live.message[:4000], live.attach_pdf, live.attach_image, live.page_size, live.orientation, live.send_if_view_empty)
    if row is None:
        bid = cur.execute(
            "SET NOCOUNT ON; INSERT Integration.Tableau_SubscriptionBackup (Subscription_id, Workbook_id, TargetType, TargetId, OwnerUserId, "
            "ScheduleId, ScheduleName, Subject, Message, AttachPdf, AttachImage, PageSize, Orientation, SendIfViewEmpty, "
            "TakenOver, RunId) OUTPUT inserted.Backup_id VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            live.id, wb_id, *args, run_id).fetchone()[0]
    else:   # an unfinished earlier attempt (never paused): refresh the definition
        cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET TargetType = ?, TargetId = ?, OwnerUserId = ?, ScheduleId = ?, "
                    "ScheduleName = ?, Subject = ?, Message = ?, AttachPdf = ?, AttachImage = ?, PageSize = ?, Orientation = ?, "
                    "SendIfViewEmpty = ?, BackedUpAt = SYSDATETIMEOFFSET(), RunId = ? WHERE Backup_id = ? AND PausedAt IS NULL",
                    *args, run_id, row.backup_id)
        bid = row.backup_id
    if cur.execute("SELECT COUNT(*) FROM Integration.Tableau_SubscriptionBackup WHERE Backup_id = ? AND TakenOver = 1 AND RestoredAt IS NULL",
                   bid).fetchone()[0] != 1:
        raise RuntimeError("backup row %s not found after writing" % bid)
    return bid


# ---------- run ----------
@dataclass
class Ctx:
    cfg: Config
    adapter: object
    gate: object
    cur: object
    today: dt.date
    run_id: str = field(default_factory=lambda: __import__("uuid").uuid4().hex[:12])
    n: Counter = field(default_factory=Counter)
    events: list = field(default_factory=list)
    halted: bool = False
    due: object = None

    def event(self, sub_id, wb, code, text):
        self.events.append({"sub": sub_id, "wb": wb, "code": code, "text": text})


def _backoff_load(cfg):
    try:
        return json.loads(Path(cfg.backoff_path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _backoff_save(cfg, d):
    try:
        Path(cfg.backoff_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cfg.backoff_path).write_text(json.dumps(d), encoding="utf-8")
    except Exception as e:
        log.warning("backoff state not written (%s)", type(e).__name__)


def backoff_blocked(cfg, sub_id, now=None):
    """Minutes left to wait before a FIRST pause of this subscription may be tried again (0 = go)."""
    e = _backoff_load(cfg).get(sub_id)
    if not e:
        return 0
    left = (dt.datetime.fromisoformat(e["next"]) - (now or dt.datetime.now())).total_seconds() / 60
    return max(0, left)


def backoff_fail(cfg, sub_id, now=None):
    d = _backoff_load(cfg)
    n = d.get(sub_id, {}).get("fails", 0) + 1
    wait = BACKOFF_MIN[min(n - 1, len(BACKOFF_MIN) - 1)]
    d[sub_id] = {"fails": n, "next": ((now or dt.datetime.now()) + dt.timedelta(minutes=wait)).isoformat(timespec="seconds")}
    _backoff_save(cfg, d)


def backoff_clear(cfg, sub_id):
    d = _backoff_load(cfg)
    if d.pop(sub_id, None) is not None:
        _backoff_save(cfg, d)


def halt_row(ctx, row, why):
    """Persist the halt (R1): the row stays Halted until a person clears it, so the next run cannot 'confirm' it and t6 cannot send it."""
    try:
        ctx.cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET Halted = 1, Note = ? WHERE Backup_id = ? AND RestoredAt IS NULL",
                        why[:400], row.backup_id)
    except Exception as e:
        log.error("HALT flag could not be written for %s (%s). Set Halted = 1 by hand.", row.sub_id, type(e).__name__)


def pause(ctx, sub_id, wb_id, wb_name, live, row):
    """Pause (or re-pause) a subscription that has an open takeover row. `live` was read just before. Returns True if now paused."""
    cur, execute = ctx.cur, ctx.cfg.execute
    first = row is None or not row.paused
    verb = "pause" if first else "re-pause"
    key = "paused" if first else "repaused"
    note = ("found RESUMED in Tableau; %s paused again. The script does not send it today (the old subscription may "
            "already have mailed)." % ("it was" if execute else "it would be"))
    if ctx.halted:
        ctx.n["halted"] += 1
        log.error("%s: not %sd, the run is halted after a definition change", sub_id, verb)
        return False
    if execute and ops.killed_midrun("t11"):
        ctx.n["killed"] += 1
        return False
    if first and execute:
        wait = backoff_blocked(ctx.cfg, sub_id)
        if wait > 0:
            ctx.n["backoff"] += 1
            log.warning("%s: first pause failed before, next try in %.0f min (back-off)", sub_id, wait)
            return False
    if not execute:
        ctx.n[key] += 1
        if not first:
            ctx.event(sub_id, wb_name, "repaused", note)
        log.info("DRY RUN: would %s%s %s '%s' (%s) owner=%s%s", "back up and " if row is None else "", verb, sub_id, wb_name, live.subject[:40], live.user_id,
                 " [already suspended: only the takeover row, no change in Tableau]" if first and live.suspended else "")
        return False
    try:
        if first:
            bid = write_backup(cur, ctx.cfg, ctx.run_id, live, wb_id, row)
            row = row or Row(bid, sub_id, wb_id, wb_name, True, False)
        # D-046: already suspended (by anyone) = nothing to change in Tableau; the takeover row alone makes it ours
        after = live if (first and live.suspended) else ctx.adapter.set_suspended(sub_id, True)
    except Exception as e:
        ctx.n["failed"] += 1
        if first:
            backoff_fail(ctx.cfg, sub_id)
        log.error("%s of %s FAILED (%s: %s); nothing recorded as paused", verb, sub_id, type(e).__name__, t6.sanitize(e, 200))
        ctx.event(sub_id, wb_name, "pause_failed", "the %s did not complete (%s)" % (verb, type(e).__name__))
        return False
    changed = diff_fields(live, after)
    if changed:
        ctx.halted = True
        ctx.n["failed"] += 1
        halt_row(ctx, row, "pausing changed: %s" % ", ".join(changed))
        log.error("HALT: pausing %s changed %s besides suspended. Compare with the backup (Tableau_SubscriptionBackup / out/t11_backup) "
                  "and restore by hand. No further subscription is touched in this run.", sub_id, ", ".join(changed))
        ctx.event(sub_id, wb_name, "definition_changed", "pausing changed: %s. The run was stopped; check against the backup." % ", ".join(changed))
        return False
    if not after.suspended:
        ctx.n["failed"] += 1
        log.error("%s of %s: Tableau still shows it as not suspended", verb, sub_id)
        ctx.event(sub_id, wb_name, "pause_failed", "Tableau still shows it as not suspended after the %s" % verb)
        return False
    if first:
        backoff_clear(ctx.cfg, sub_id)
    try:
        if first:
            cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET PausedAt = SYSDATETIMEOFFSET() WHERE Backup_id = ? AND RestoredAt IS NULL",
                        row.backup_id)
        else:
            cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET LastRepausedAt = SYSDATETIMEOFFSET(), RepauseCount = RepauseCount + 1 "
                        "WHERE Backup_id = ? AND RestoredAt IS NULL", row.backup_id)
    except Exception as e:
        ctx.n["failed"] += 1
        log.error("%s is paused in Tableau but the backup row could not be updated (%s); the next run confirms it", sub_id, type(e).__name__)
        return True
    ctx.n[key] += 1
    if first:
        if live.suspended:
            ctx.n["taken_suspended"] += 1
        log.info("%s %s '%s' (%s)", "TAKEN OVER (was already suspended)" if live.suspended else "PAUSED", sub_id, wb_name, live.subject[:40])
    else:
        ctx.event(sub_id, wb_name, "repaused", note)
        log.warning("RE-PAUSED %s '%s' (%s): it had been resumed in Tableau", sub_id, wb_name, live.subject[:40])
    return True


def handle_owned(ctx, row):
    """A taken-over subscription: keep it paused."""
    ctx.n["owned"] += 1
    if row.halted:
        ctx.n["halted_rows"] += 1
        log.error("%s '%s' is HALTED (a pause changed its definition): not confirmed, not paused again, not sent. A person must compare "
                  "it with the backup and clear Halted (docs/RUNBOOK.md)", row.sub_id, row.wb_name)
        ctx.event(row.sub_id, row.wb_name, "halted", "takeover halted after a pause changed the definition; clear it by hand after checking")
        return
    try:
        live = ctx.adapter.get(row.sub_id)
    except Exception as e:
        if t6.is_not_found(e):
            ctx.n["gone"] += 1
            log.warning("%s '%s' no longer exists on Tableau (deleted): nothing to pause; T2 deactivates it and t6 stops sending it",
                        row.sub_id, row.wb_name)
            if ctx.cfg.execute:
                ctx.cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET GoneSince = ISNULL(GoneSince, CAST(SWITCHOFFSET("
                                "SYSDATETIMEOFFSET(), '+08:00') AS date)) WHERE Backup_id = ? AND RestoredAt IS NULL", row.backup_id)
                if row.gone_since and (ctx.today - row.gone_since).days >= ctx.cfg.gone_close_days:
                    ctx.cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET RestoredAt = SYSDATETIMEOFFSET(), Note = ? "
                                    "WHERE Backup_id = ? AND RestoredAt IS NULL",
                                    "auto-closed: the subscription has been gone from Tableau since %s" % row.gone_since, row.backup_id)
                    ctx.n["closed_gone"] += 1
                    ctx.event(row.sub_id, row.wb_name, "takeover_closed", "deleted or re-created in Tableau %d+ days ago: the open takeover "
                              "row was closed. If it was re-created, the new subscription is taken over only if its workbook is allow-listed"
                              % ctx.cfg.gone_close_days)
        else:
            ctx.n["lookup_failed"] += 1
            log.warning("Tableau lookup failed for %s (%s)", row.sub_id, type(e).__name__)
        return
    if row.gone_since and ctx.cfg.execute:
        ctx.cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET GoneSince = NULL WHERE Backup_id = ? AND RestoredAt IS NULL",
                        row.backup_id)
    if live.suspended:
        if not row.paused:
            ctx.n["confirmed"] += 1
            log.info("%s is suspended in Tableau, confirming the pause%s", row.sub_id, "" if ctx.cfg.execute else " (dry run: not written)")
            if ctx.cfg.execute:
                ctx.cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET PausedAt = SYSDATETIMEOFFSET() "
                                "WHERE Backup_id = ? AND RestoredAt IS NULL AND PausedAt IS NULL", row.backup_id)
        else:
            ctx.n["already_paused"] += 1
        return
    pause(ctx, row.sub_id, row.wb_id, row.wb_name, live, row)


def enabled(cfg, c):
    return c.wb_id.lower() in cfg.workbooks or c.wb_name.lower() in cfg.workbooks or c.id.lower() in cfg.sub_ids


def consider(ctx, c, open_rows):
    """A subscription without an open row: take it over if everything allows it."""
    cfg = ctx.cfg
    if c.id in open_rows:
        return
    if not enabled(cfg, c):
        ctx.n["skip_not_enabled"] += 1
        return
    log.info("allow-list match: workbook id %s ('%s'), subscription %s", c.wb_id, c.wb_name, c.id)
    if c.no_hash:
        ctx.n["skip_no_key_hash"] += 1
        log.warning("%s '%s': skipped, SubscriptionKeyHash is NULL and t6 would refuse to send it", c.id, c.wb_name)
        return
    if ctx.due is not None:
        st = ctx.due.status(c.id, c.sched_id, None)
        if st.status == "unsupported":
            ctx.n["skip_schedule"] += 1
            log.warning("%s '%s': skipped, t6 would not send it (%s)", c.id, c.wb_name, st.note)
            return
    if c.was_restored and not cfg.retake_restored:
        ctx.n["skip_restored"] += 1
        log.info("%s '%s': skipped, restored earlier (use --retake-restored to take it over again)", c.id, c.wb_name)
        return
    count = ctx.gate.wb.get(c.wb_id, (None, 0))[1]
    if count <= 1:
        ctx.n["skip_single_source"] += 1
        log.info("%s '%s': skipped, not in scope (%s data source(s) in the T2 tables, needs more than one)", c.id, c.wb_name, count)
        return
    age_h = (t6.sgt_now().replace(tzinfo=None) - c.synced_at).total_seconds() / 3600 if c.synced_at else None
    if age_h is None or age_h > cfg.max_mapping_age_h:
        ctx.n["skip_stale"] += 1
        log.warning("%s '%s': skipped, the T2 sync last saw it %s ago (limit %.0f h)", c.id, c.wb_name,
                    "?" if age_h is None else "%.1f h" % age_h, cfg.max_mapping_age_h)
        return
    ok, why = ctx.gate.structural(c.wb_id)
    if not ok:
        if cfg.force_notready:
            log.warning("%s '%s': FORCED although t6 cannot send this workbook (%s): --force-notready and it is named in the allow-list",
                        c.id, c.wb_name, why)
        else:
            ctx.n["skip_not_ready"] += 1
            log.warning("%s '%s': skipped, t6 would not send this workbook (%s); --force-notready overrides when it is named in the allow-list",
                        c.id, c.wb_name, why)
            return
    try:
        live = ctx.adapter.get(c.id, with_schedule=True)
    except Exception as e:
        if t6.is_not_found(e):
            ctx.n["gone"] += 1
            log.warning("%s '%s': not on Tableau any more (404), skipped", c.id, c.wb_name)
        else:
            ctx.n["lookup_failed"] += 1
            log.warning("Tableau lookup failed for %s (%s)", c.id, type(e).__name__)
        return
    if not live.schedule_id:
        ctx.n["skip_no_schedule_id"] += 1
        log.warning("%s '%s': skipped, no schedule id (on-the-fly schedule): the update request may be rejected", c.id, c.wb_name)
        return
    if live.raw_missing:
        log.warning("%s '%s': the Tableau response does not contain %s; the library would read them as False. The pause leaves them "
                    "unset instead. Compare this subscription in the Tableau UI before and after the first pilot pause.",
                    c.id, c.wb_name, ", ".join(live.raw_missing))
    if cfg.owner_user_ids and (live.user_id or "").lower() not in cfg.owner_user_ids:
        ctx.n["skip_not_owner"] += 1
        log.info("%s '%s': skipped, its subscriber is not one of the pilot owner user ids (--owner-user-id)", c.id, c.wb_name)
        return
    if live.target_id != c.target_id:
        ctx.n["skip_definition_changed"] += 1
        log.warning("%s '%s': skipped, its target differs from the last T2 sync", c.id, c.wb_name)
        return
    if not (live.attach_pdf or live.attach_image):
        ctx.n["skip_no_format"] += 1
        log.warning("%s '%s': skipped, neither PDF nor image is set", c.id, c.wb_name)
        return
    if ctx.n["new_attempts"] >= cfg.max_new:
        ctx.n["skip_cap"] += 1
        log.warning("%s '%s': NOT taken over in this run, the cap of %d new takeovers per run is reached (--max-new / TAKEOVER_MAX_NEW); "
                    "the next run continues", c.id, c.wb_name, cfg.max_new)
        return
    ctx.n["new_attempts"] += 1
    pause(ctx, c.id, c.wb_id, c.wb_name, live, None)


def send_alerts(ctx, mailer):
    events = ctx.events
    fresh, _ = t6.fresh_events(ctx.cfg.alert, ctx.today, events)
    if not fresh:
        return 0
    if not ctx.cfg.execute:
        log.info("DRY RUN: would send one alert mail with %d item(s) (not sent)", len(fresh))
        return len(fresh)
    return t6.deliver_alerts(ctx.cfg.alert, mailer, ctx.today, events, intro=ALERT_INTRO % ctx.today, footer=False, subject=ALERT_SUBJECT)


def run(ctx, mailer):
    cur, cfg = ctx.cur, ctx.cfg
    if cfg.schedule_enforce and ctx.due is None:
        ctx.due = sd.DueEngine.load(cur, ctx.today, cfg.catch_up_days, cfg.max_mapping_age_h)
    open_rows = load_open(cur, cfg.id_like)
    for row in open_rows.values():
        if row.taken_over:
            if ctx.halted:
                ctx.n["halted"] += 1
                continue
            handle_owned(ctx, row)
        else:
            log.warning("%s has an open backup row that is not marked TakenOver: ignored, check Tableau_SubscriptionBackup", row.sub_id)
    for c in load_cands(cur, cfg.id_like):
        if ctx.halted:
            break
        consider(ctx, c, open_rows)
    if ctx.n["skip_cap"]:
        ctx.event("-", "(whole run)", "takeover_cap", "the cap of %d new takeovers per run was reached; %d more subscription(s) matched the "
                  "allow-list and were NOT taken over. If the allow-list is what you intended, run t11 again; if not, fix the allow-list "
                  "or the owner fence" % (cfg.max_new, ctx.n["skip_cap"]))
    ctx.n["alerts"] = send_alerts(ctx, mailer)
    return ctx.n


def summary_line(ctx):
    n, cfg = ctx.n, ctx.cfg
    skips = " ".join("%s=%d" % (k, v) for k, v in sorted(n.items()) if k.startswith("skip_"))
    bad = n["failed"] + n["lookup_failed"] + n["halted"] + n["halted_rows"]
    return ("RUN SUMMARY mode=%s transport=%s allow_list_workbooks=%d allow_list_subscriptions=%d owned=%d paused=%d repaused=%d confirmed=%d "
            "already_paused=%d taken_suspended=%d gone=%d failed=%d lookup_failed=%d halted=%d halted_rows=%d backoff=%d closed_gone=%d %s alerts=%d needs_human=%d" % (
                "execute" if cfg.execute else "dry-run", "ses" if cfg.ses else "local", len(cfg.workbooks), len(cfg.sub_ids),
                n["owned"], n["paused"], n["repaused"], n["confirmed"], n["already_paused"], n["taken_suspended"], n["gone"], n["failed"], n["lookup_failed"],
                n["halted"], n["halted_rows"], n["backoff"], n["closed_gone"], skips or "skipped=0", n["alerts"], bad)) + ops.allow_all_marker()


def exit_code(ctx):
    return 1 if (ctx.n["failed"] or ctx.n["lookup_failed"] or ctx.n["halted"] or ctx.n["halted_rows"]) else 0


# ---------- restore ----------
def restore(ctx, sub_ids, workbook):
    """Mark restored first, then resume in Tableau. Returns the number of problems."""
    cur, cfg, n = ctx.cur, ctx.cfg, ctx.n
    targets = []   # (sub id, wb name, backup id or None if already restored)
    for sid in sub_ids:
        r = cur.execute("SELECT TOP 1 b.Backup_id, CASE WHEN b.RestoredAt IS NULL THEN 0 ELSE 1 END, (SELECT TOP 1 w.Workbook_Name FROM Integration.Tableau_WorkbookDataSource w "
                        "WHERE w.Workbook_id = b.Workbook_id ORDER BY w.Active_Flag DESC, w.LastSeenAt DESC, w.Workbook_Name) FROM Integration.Tableau_SubscriptionBackup b WHERE b.Subscription_id = ? "
                        "AND b.Subscription_id LIKE ? AND b.TakenOver = 1 ORDER BY b.Backup_id DESC", sid, cfg.id_like).fetchone()
        if r is None:
            n["failed"] += 1
            log.error("%s: no takeover backup row, so it is not ours; nothing restored", sid)
            continue
        targets.append((sid, r[2] or "(unnamed workbook)", r[0] if not r[1] else None))
    if workbook:
        w = workbook.lower()
        for r in load_open(cur, cfg.id_like).values():
            if r.taken_over and (r.wb_id.lower() == w or r.wb_name.lower() == w):
                targets.append((r.sub_id, r.wb_name, r.backup_id))
        if not targets:
            log.warning("no taken-over subscription found for workbook '%s'", workbook)
    if cfg.execute and targets:
        ctx.adapter.t()   # signs in = takes the PAT lock BEFORE anything is marked restored (PatBusy then leaves the rows untouched)
    for sid, wbn, bid in dict(((t[0], t) for t in targets)).values():
        n["restore_targets"] += 1
        # R5 / F-4: Tableau's own schedule resumes with this restore, so a mail the script already sent today may be sent a second time
        sent_today = cur.execute("SELECT COUNT(*) FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id = ? AND SendDate = ? "
                                 "AND Status IN ('Sent', 'Claimed', 'TimedOut')", sid, ctx.today).fetchone()[0]
        if sent_today:
            if not cfg.allow_restore_after_send:
                n["failed"] += 1
                n["restore_refused"] += 1
                log.error("RESTORE REFUSED %s '%s': the script already sent (or started to send) it today. Restoring now resumes Tableau's own "
                          "schedule and the recipients may get a second mail today. Restore tomorrow, or add --allow-restore-after-send "
                          "if a second mail is acceptable. Nothing was changed.", sid, wbn)
                continue
            log.warning("RESTORE %s '%s': the script already sent it today; Tableau may send it again (--allow-restore-after-send given)", sid, wbn)
        if not cfg.execute:
            log.info("DRY RUN: would mark %s '%s' restored%s and resume it in Tableau", sid, wbn, " (already marked)" if bid is None else "")
            continue
        if bid is not None:
            cur.execute("UPDATE Integration.Tableau_SubscriptionBackup SET RestoredAt = SYSDATETIMEOFFSET() WHERE Backup_id = ? AND RestoredAt IS NULL", bid)
        try:
            live = ctx.adapter.get(sid)
        except Exception as e:
            if t6.is_not_found(e):
                n["gone"] += 1
                log.warning("%s no longer exists on Tableau; marked restored, nothing to resume", sid)
            else:
                n["failed"] += 1
                ctx.event(sid, wbn, "restore_failed", "marked restored but still paused or unreadable in Tableau; run --restore again")
                log.error("RESTORE %s: marked restored, but Tableau could not be read (%s). Run --restore again to resume it.", sid, type(e).__name__)
            continue
        if not live.suspended:
            n["restored"] += 1
            log.info("RESTORED %s '%s' (it was already resumed in Tableau)", sid, wbn)
            continue
        try:
            after = ctx.adapter.set_suspended(sid, False)
        except Exception as e:
            n["failed"] += 1
            ctx.event(sid, wbn, "restore_failed", "marked restored but still paused or unreadable in Tableau; run --restore again")
            log.error("RESTORE %s: marked restored, but the resume FAILED (%s). Run --restore again.", sid, type(e).__name__)
            continue
        changed = diff_fields(live, after)
        if changed or after.suspended:
            n["failed"] += 1
            ctx.event(sid, wbn, "restore_failed", "marked restored but still paused or unreadable in Tableau; run --restore again")
            log.error("RESTORE %s: after the resume %s. Compare with the backup.", sid,
                      ("these changed: " + ", ".join(changed)) if changed else "Tableau still shows it suspended")
            continue
        n["restored"] += 1
        log.info("RESTORED %s '%s': resumed in Tableau; t6 no longer sends it, Tableau's own schedule does", sid, wbn)
    return n["failed"]


def restore_summary(ctx):
    n = ctx.n
    return ("RUN SUMMARY mode=%s restore targets=%d restored=%d gone=%d refused_sent_today=%d failed=%d needs_human=%d" % (
        "execute" if ctx.cfg.execute else "dry-run", n["restore_targets"], n["restored"], n["gone"], n["restore_refused"], n["failed"], n["failed"])) + ops.allow_all_marker()


# ---------- main ----------
def setup_logging():
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    fh = ops.log_file_handler("t11_pause")
    for h in (sh, fh):
        h.setFormatter(fmt)
    for lg in (log, t4.log, t6.log, ops.log):
        lg.setLevel(logging.INFO)
        if not lg.handlers:
            lg.addHandler(sh)
            lg.addHandler(fh)

    def hook(et, ev, tb):
        log.critical("Unhandled error", exc_info=(et, ev, tb))
    sys.excepthook = hook


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="default: print what would happen, write nothing")
    g.add_argument("--execute", action="store_true", help="WRITE: backup rows in the dev DWH and pause/resume in Tableau")
    ap.add_argument("--workbooks", action="append", help="allow-list: workbook names or ids (comma list, repeatable)")
    ap.add_argument("--subscription-ids", action="append", help="allow-list: subscription ids (comma list, repeatable)")
    ap.add_argument("--force-notready", action="store_true", help="also take over a named workbook that t6 would not send")
    ap.add_argument("--retake-restored", action="store_true", help="allow a subscription restored earlier to be taken over again")
    ap.add_argument("--restore", action="append", help="rollback: resume this subscription and mark it restored (repeatable)")
    ap.add_argument("--restore-all-for-workbook", help="rollback: restore every taken-over subscription of this workbook (name or id)")
    ap.add_argument("--smtp", "--ses", dest="ses", action="store_true",
                    help="alert mail through the real SMTP server (needs --execute and t6's SMTP settings); or set MAIL_TRANSPORT=smtp")
    ap.add_argument("--local-smtp", action="store_true", help="force the local test sink even if MAIL_TRANSPORT=smtp")
    ap.add_argument("--owner-user-id", action="append",
                    help="pilot fence: take over ONLY subscriptions whose subscriber is this Tableau user id (repeatable; env TAKEOVER_OWNER_USER_IDS). "
                         "REQUIRED for any --execute with an allow-list (t11 cannot see recipient addresses)")
    ap.add_argument("--max-new", type=int, default=None,
                    help="cap on NEW takeovers per run (default 10, env TAKEOVER_MAX_NEW); further matches wait for the next run")
    ap.add_argument("--allow-restore-after-send", action="store_true",
                    help="with --restore: restore even if the script already mailed the subscription today (a second mail is possible)")
    ap.add_argument("--local-host", default="127.0.0.1")
    ap.add_argument("--local-port", type=int, default=1025)
    ap.add_argument("--max-mapping-age-h", type=float, default=ops.default_max_age_h())
    a = ap.parse_args(argv)
    try:
        cfg = build_config(a, os.environ)
    except t6.ConfigError as e:
        sys.exit(str(e))
    setup_logging()
    restoring = bool(a.restore or a.restore_all_for_workbook)
    mode = "execute" if cfg.execute else "dry-run"
    ops.log_transport("t11", cfg.alert.ses, cfg.execute)
    if not restoring and ops.check_kill_switch("t11", cfg.execute, cfg.ses, a.local_host, a.local_port):
        return 0
    ops.check_alert_recipient(cfg.alert, "t11")
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
        ops.write_marker("t11", mode, 1, "FATAL " + what, outcome="fatal")
        return 1
    try:
        conn = t4.connect()
        cur = conn.cursor()
        today = t4.dwh_today(cur)
    except (Exception, SystemExit) as e:
        return fatal("DWH connect or guard failed: %s" % (e if isinstance(e, SystemExit) else type(e).__name__))
    if cfg.execute:
        got = cur.execute("DECLARE @r int; EXEC @r = sp_getapplock @Resource = ?, @LockMode = 'Exclusive', @LockOwner = 'Session', "
                          "@LockTimeout = 0; SELECT @r", LOCK_NAME).fetchone()[0]
        if got < 0:
            log.warning("Another t11 run holds the lock; exiting")
            return 1 if restoring else 0
    tableau = t4.LazyTableau(cur, owner="t11")
    ctx = Ctx(cfg, TableauAdapter(tableau), None, cur, today)
    try:
        if restoring:
            restore(ctx, a.restore or [], a.restore_all_for_workbook)
            log.info(restore_summary(ctx))
            if ctx.events:
                send_alerts(ctx, mailer)
            return 1 if ctx.n["failed"] else 0
        ctx.gate = t6.Gate(cur, today)
        run(ctx, mailer)
    except t4.PatBusy:
        log.info("RUN SUMMARY mode=%s skipped=pat_busy (another script holds the Tableau PAT session)", mode)
        ops.on_pat_busy("t11", cfg.execute, cfg=cfg.alert, mailer=mailer)
        ops.write_marker("t11", mode, 1 if restoring else 0, "skipped=pat_busy", outcome="pat_busy")
        return 1 if restoring else 0
    except SystemExit:   # sign-in failed (logged by LazyTableau)
        return fatal("Tableau sign-in failed")
    except Exception:
        log.exception("Run failed")
        return fatal("run failed")
    finally:
        tableau.close()
    ops.pat_ok("t11")
    line = summary_line(ctx)
    log.info(line)
    code = exit_code(ctx)
    ops.write_marker("t11", mode, code, line)
    return code


if __name__ == "__main__":
    sys.exit(main())
