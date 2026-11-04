"""R1 operations helpers shared by t2/t4/t6/t11/daily_summary (F-07, F-17, D-039, D-042).

- Mail transport (D-042, D-043): ONE setting MAIL_TRANSPORT=local|smtp (default local; 'ses' is an alias of smtp) read by every script,
  so nobody has to remember --smtp on a task line. --smtp (alias --ses) on the command line still forces it. Every script logs
  `MAIL TRANSPORT ...` at startup. Real smtp also needs --execute (dry runs never send), SMTP_USER/SMTP_PASSWORD (SES_SMTP_* are
  accepted as aliases) and SEND_ALLOWED_RECIPIENTS. Provider-neutral: SMTP_HOST, SMTP_PORT, SMTP_PROVIDER=ses|gmail|generic.
- Kill switch: env TABLEAU_AUTOMATION_DISABLED=1 (or true/yes) or the file out/KILL (path: env KILL_FILE). Checked first
  thing at startup by t2/t4/t6/t11 (t11 --restore is exempt: a rollback must always work) AND again before every claim, refresh
  start and pause inside a run. The script logs a clear line, sends ONE alert per Singapore day (--execute only), writes a
  last-run marker and exits 0.
- Last-run markers: out/lastrun/<script>.json (written atomically), every run, also skips and kill-switch exits. Per-day counters
  per mode, with the timestamp of the last run per mode, so daily_summary.py can say which scripts did not run today.
- Windows Event Log (D-042): a second, SES-independent signal. Every run that ends with a non-zero exit in execute mode, and
  every PAT-busy / kill-switch alert, writes an entry with source TableauAutomation (eventcreate; guarded, never raises; set
  EVENTLOG_DISABLED=1 to turn off). The first write of a new source needs an elevated account: register it once (RUNBOOK).
- Alert recipient check: warns loudly at startup when ALERT_TO is not on SEND_ALLOWED_RECIPIENTS with real SES.
- SEND_ALLOW_ALL_UAT=1 (D-050): no allow-list, effective only when TABLEAU_SERVER_URL's host is exactly the UAT host. Warned at startup
  (log_transport) and marked in the RUN SUMMARY lines (allow_all_uat=1).
"""
import datetime as dt
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
SGT = dt.timezone(dt.timedelta(hours=8))
log = logging.getLogger("ops")
EVENT_SOURCE = "TableauAutomation"
ADDR_RE = re.compile(r"[\w.+'-]+@[\w.-]+")


def sg_now():
    return dt.datetime.now(SGT)


def log_dir(env=os.environ):
    """Folder for the rotating log files (env LOG_DIR, tests only), default out/."""
    d = Path(env.get("LOG_DIR") or ROOT / "out")
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_retention_days(env=os.environ):
    """Days of script log files to keep (env LOG_RETENTION_DAYS, default 7, at least 1)."""
    try:
        return max(1, int(str(env.get("LOG_RETENTION_DAYS", "")).strip() or 7))
    except ValueError:
        return 7


def prune_logs(prefix, env=os.environ, today=None):
    """Delete this script's own log files older than the retention: <prefix>_YYYY-MM-DD.log by the date in the name, and the old
    size-rotated <prefix>.log / <prefix>.log.N by modification time. Only those names inside log_dir() are touched. Never raises:
    a file that is in use (another run holds it) or already gone is skipped. Returns the number deleted."""
    n = 0
    try:
        today = today or sg_now().date()
        cutoff = today - dt.timedelta(days=log_retention_days(env))
        dated = re.compile(r"^%s_(\d{4}-\d{2}-\d{2})\.log$" % re.escape(prefix))
        legacy = re.compile(r"^%s\.log(\.\d+)?$" % re.escape(prefix))
        for f in log_dir(env).iterdir():
            try:
                m = dated.match(f.name)
                if m:
                    old = dt.date.fromisoformat(m.group(1)) < cutoff
                elif legacy.match(f.name):
                    old = dt.date.fromtimestamp(f.stat().st_mtime) < cutoff
                else:
                    continue
                if old and f.is_file():
                    f.unlink()
                    n += 1
            except (OSError, ValueError):
                continue
    except Exception:
        pass
    return n


def log_file_handler(prefix, env=os.environ):
    """One log file per Singapore day: <log_dir>/<prefix>_YYYY-MM-DD.log (append; no rename, so several runs on Windows can share it).
    Prunes this script's old files first (D-051). Replaces RotatingFileHandler."""
    prune_logs(prefix, env)
    return logging.FileHandler(log_dir(env) / ("%s_%s.log" % (prefix, sg_now().date().isoformat())), encoding="utf-8")


def state_dir(env=os.environ):
    """Folder for alert-state files (env OPS_STATE_DIR, tests only), default out/."""
    return Path(env.get("OPS_STATE_DIR") or ROOT / "out")


def atomic_write(path, text):
    """Write temp file then replace, so a crash never leaves a half-written state/marker file."""
    path = Path(path)
    tmp = path.with_name(path.name + ".%d.tmp" % os.getpid())
    tmp.write_text(text, encoding="utf-8")
    for i in range(5):   # Windows: a virus scanner or another process may hold the target for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 4:
                raise
            time.sleep(0.05 * (i + 1))


# ---------- settings shared by all scripts ----------
def default_max_age_h(env=os.environ):
    """The mapping staleness limit in hours: env MAPPING_MAX_AGE_H, else 26 (one place for t4, t6, t11, daily_summary)."""
    try:
        return float(env.get("MAPPING_MAX_AGE_H", 26))
    except ValueError:
        return 26.0


def schedule_mode(env=os.environ, override=None):
    """'honour' (default) or 'ignore' (SCHEDULE_MODE or the command line override). Raises ValueError for anything else."""
    m = (override or env.get("SCHEDULE_MODE") or "honour").strip().lower()
    if m not in ("honour", "ignore"):
        raise ValueError("SCHEDULE_MODE must be honour or ignore, got %r" % m)
    return m


def mail_transport(env=os.environ):
    """'local' or 'smtp' (MAIL_TRANSPORT; 'ses' is accepted as an alias of smtp)."""
    m = (env.get("MAIL_TRANSPORT") or "local").strip().lower()
    if m == "ses":
        m = "smtp"
    if m not in ("local", "smtp"):
        raise ValueError("MAIL_TRANSPORT must be local or smtp (ses = smtp), got %r" % m)
    return m


def effective_ses(flag, execute=True, env=os.environ, local=False):
    """True when mail goes through the real SMTP server: --smtp/--ses on the command line, or MAIL_TRANSPORT=smtp (real only with --execute).
    --local-smtp (local=True) wins over both."""
    if local:
        return False
    return bool(flag) or (bool(execute) and mail_transport(env) == "smtp")


DEFAULT_SES_HOST = "email-smtp.ap-northeast-1.amazonaws.com"
PROVIDERS = ("ses", "gmail", "generic")


def smtp_provider(env=os.environ):
    """ses | gmail | generic: SMTP_PROVIDER, else inferred from the host name (no host = ses, the original default)."""
    p = (env.get("SMTP_PROVIDER") or "").strip().lower()
    if p:
        if p not in PROVIDERS:
            raise ValueError("SMTP_PROVIDER must be one of %s, got %r" % ("/".join(PROVIDERS), p))
        return p
    host = (env.get("SMTP_HOST") or env.get("SES_SMTP_HOST") or "").lower()
    return "gmail" if "gmail" in host or "googlemail" in host else "ses" if (not host or "amazonaws" in host) else "generic"


def smtp_settings(env=os.environ):
    """Real SMTP settings (SMTP_* first, SES_SMTP_* as fallback aliases). The password is returned, never logged."""
    prov = smtp_provider(env)
    host = env.get("SMTP_HOST") or env.get("SES_SMTP_HOST") or ("smtp.gmail.com" if prov == "gmail" else DEFAULT_SES_HOST)
    return {"provider": prov, "host": host, "port": int(env.get("SMTP_PORT") or env.get("SES_SMTP_PORT") or 587),
            "user": env.get("SMTP_USER") or env.get("SES_SMTP_USER") or "",
            "password": env.get("SMTP_PASSWORD") or env.get("SES_SMTP_PASSWORD") or ""}


def sender_warning(provider, sender, user):
    """Text of a warning when Gmail would rewrite the From address (SEND_FROM is not the authenticated account), else None."""
    if provider == "gmail" and user and (sender or "").strip().lower() != user.strip().lower():
        return ("SMTP_PROVIDER=gmail: Gmail rewrites the From address to the authenticated account (SMTP_USER) unless SEND_FROM is a "
                "verified 'send mail as' alias; set SEND_FROM to the account for testing")
    return None


def default_size_limit_mb(env=os.environ):
    """Message size limit in MB: 20 for Gmail (limit 25 MB incl. encoding), else 25."""
    return 20 if smtp_provider(env) == "gmail" else 25


UAT_HOST_VAR = "UAT_TABLEAU_HOST"
ALLOW_ALL_VAR = "SEND_ALLOW_ALL_UAT"


def server_host(env=os.environ):
    """Lower-case host of TABLEAU_SERVER_URL (userinfo and port stripped), or None when it is missing or looks tampered with
    (backslash, whitespace or control characters, which parsers disagree on). Compared as a whole later, never as a substring."""
    url = (env.get("TABLEAU_SERVER_URL") or "").strip()
    if not url or any(c == "\\" or c.isspace() or ord(c) < 32 for c in url):
        return None
    try:
        return (urlsplit(url).hostname or "").lower() or None
    except ValueError:
        return None


def uat_host(env=os.environ):
    """Lower-case UAT Tableau host from UAT_TABLEAU_HOST (.env), or None when unset: the allow-all switch is then never effective."""
    return (env.get(UAT_HOST_VAR) or "").strip().lower() or None


def allow_all_uat(env=os.environ):
    """(requested, effective) for the SEND_ALLOW_ALL_UAT switch (D-050). Effective only when the Tableau host is exactly the UAT host."""
    requested = str(env.get(ALLOW_ALL_VAR, "")).strip().lower() in ("1", "true", "yes", "on")
    return requested, requested and uat_host(env) is not None and server_host(env) == uat_host(env)


def allow_all_marker(env=os.environ):
    """' allow_all_uat=1' for the RUN SUMMARY lines when the switch is effective, else ''."""
    return " allow_all_uat=1" if allow_all_uat(env)[1] else ""


def log_transport(script, ses, execute, env=os.environ):
    log.info("MAIL TRANSPORT %s for %s (%s): %s", "smtp" if ses else "local", script, "execute" if execute else "dry-run",
             "alerts and reports go to the REAL mail server" if ses and execute else
             "nothing is sent for real (dry run)" if not execute else
             "LOCAL TEST SINK ONLY - alerts do NOT reach anybody (set MAIL_TRANSPORT=smtp in .env for real mail)")
    requested, effective = allow_all_uat(env)
    if effective:
        log.warning("%s=1 is ON for %s: the recipient allow-list is NOT applied on the UAT Tableau server (%s). Every subscriber "
                    "of a taken-over subscription gets the mail once real sending is on (--execute, MAIL_TRANSPORT=smtp).",
                    ALLOW_ALL_VAR, script, uat_host(env))
    elif requested:
        log.warning("%s=1 is set but IGNORED for %s: TABLEAU_SERVER_URL host is %r, not %s. The normal SEND_ALLOWED_RECIPIENTS rules apply.",
                    ALLOW_ALL_VAR, script, server_host(env), uat_host(env) or "(UAT_TABLEAU_HOST not set)")


def salt_warning(env=os.environ):
    """Text of a startup warning when real SMTP is used without ADDR_HASH_SALT, else None."""
    if not hash_salt(env):
        return ("ADDR_HASH_SALT is not set: the recipient hash in the logs is unsalted and guessable for a small address set "
                "(set any random text in .env)")
    return None


def hash_salt(env=os.environ):
    """Salt for the short recipient-list hash in logs (env ADDR_HASH_SALT). Unset = unsalted hash (guessable for a small address set)."""
    return env.get("ADDR_HASH_SALT", "")


# ---------- second signal: Windows Event Log ----------
def event_log(script, text, level="ERROR", env=os.environ):
    """Write an entry to the Windows Application log with source TableauAutomation (eventcreate). Never raises. Returns True if written."""
    try:
        off = ("1", "true", "yes")
        if sys.platform != "win32" or str(env.get("EVENTLOG_DISABLED", "")).strip().lower() in off \
                or str(os.environ.get("EVENTLOG_DISABLED", "")).strip().lower() in off:   # the process environment also counts (tests)
            return False
        msg = ADDR_RE.sub("<addr>", "%s: %s" % (script, text))[:480]
        r = subprocess.run(["eventcreate", "/L", "APPLICATION", "/T", level, "/ID", "100" if level == "ERROR" else "101",
                            "/SO", EVENT_SOURCE, "/D", msg], capture_output=True, timeout=20)
        if r.returncode != 0:
            log.warning("Windows Event Log entry not written (eventcreate exit %d; register the source once as administrator, see RUNBOOK)",
                        r.returncode)
        return r.returncode == 0
    except Exception as e:
        log.warning("Windows Event Log entry not written (%s)", type(e).__name__)
        return False


def event_log_once(script, key, text, level="WARNING", env=os.environ, day=None):
    """event_log, but at most once per Singapore day for (script, key), so a 15-minute task does not repeat it 30 times.
    The marker is written before the entry. Never raises. Returns True if an entry was written now."""
    try:
        day = (day or sg_now().date()).isoformat()
        f = state_dir(env) / "event_once.json"
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            st = {}
        st = {d: v for d, v in st.items() if d >= (dt.date.fromisoformat(day) - dt.timedelta(days=3)).isoformat()}
        k = "%s|%s" % (script, key)
        if k in st.get(day, []):
            return False
        st.setdefault(day, []).append(k)
        f.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(f, json.dumps(st))
    except Exception as e:
        log.warning("event-once marker not written (%s); writing the entry anyway", type(e).__name__)
    return event_log(script, text, level, env)


def utc_naive(d):
    """A datetime as naive UTC truncated to seconds (what T2 stores in Tableau_Subscription.WorkbookUpdatedAt and t6 compares
    with). None stays None. A naive input is taken as already UTC."""
    if d is None:
        return None
    if d.tzinfo is not None:
        d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d.replace(microsecond=0)


# ---------- kill switch ----------
def kill_file(env=os.environ):
    return Path(env.get("KILL_FILE") or ROOT / "out" / "KILL")


def kill_switch_active(env=os.environ):
    """(active, reason)."""
    if str(env.get("TABLEAU_AUTOMATION_DISABLED", "")).strip().lower() in ("1", "true", "yes", "on"):
        return True, "environment variable TABLEAU_AUTOMATION_DISABLED is set"
    kf = kill_file(env)
    if kf.exists():
        return True, "kill file %s exists" % kf
    return False, ""


def alert_config(ses, env=os.environ, name="ops", execute=True, local_smtp=False):
    """A t6.Config for operational alerts with t6's transport and allow-list rules (local unless --smtp / MAIL_TRANSPORT=smtp)."""
    import dataclasses
    import t6_send as t6
    ses = effective_ses(ses, execute, env, local_smtp)
    six = SimpleNamespace(execute=execute, ses=ses and execute, local_smtp=local_smtp, as_of_date=None, export=False, size_limit_mb=t6.SIZE_LIMIT_MB,
                          cutoff=t6.DEFAULT_CUTOFF, max_mapping_age_h=default_max_age_h(env), include_not_taken_over=False,
                          ignore_schedule=False, catch_up_days=None)
    c = t6.build_config(six, env)
    return dataclasses.replace(c, alert_state=state_dir(env) / ("%s_alert_state%s.json" % (name, "" if ses else "_local")))


def alert_once(cfg, mailer, script, code, text, day=None):
    """One alert mail per Singapore day for (script, code). Returns the number of events mailed."""
    import t6_send as t6
    day = day or sg_now().date()
    return t6.deliver_alerts(cfg, mailer, day, [{"sub": script, "wb": "(whole job)", "code": code, "text": text}])


def check_kill_switch(script, execute, ses=False, local_host="127.0.0.1", local_port=1025, env=os.environ):
    """Returns True when the script must exit 0 now. Logs, alerts once a day (execute only) and writes the marker."""
    mark_started(script, "execute" if execute else "dry-run", env)
    active, why = kill_switch_active(env)
    if not active:
        return False
    log.error("KILL SWITCH ACTIVE (%s): %s does nothing and exits 0. Remove out/KILL or unset TABLEAU_AUTOMATION_DISABLED to resume. "
              "Subscriptions the script took over stay PAUSED in Tableau until t11_pause.py --restore is run.", why, script)
    if execute:
        event_log(script, "KILL SWITCH ACTIVE (%s): the scripts do nothing; taken-over subscriptions stay paused" % why, "WARNING", env)
        try:
            import t6_send as t6
            cfg = alert_config(ses, env, "kill", True, not ses)   # ses is already the effective choice
            mailer = t6.make_mailer(cfg, local_host, local_port, env)
            t6.deliver_alerts(cfg, mailer, sg_now().date(), [{"sub": "-", "wb": "(whole job)", "code": "kill_switch", "text":
                              "the kill switch is active (%s); %s and the other scripts do nothing. Taken-over subscriptions stay paused in "
                              "Tableau, so those reports are NOT being sent by anyone." % (why, script)}])
        except Exception as e:  # an alert problem must not turn the kill switch into a crash
            log.error("Kill-switch alert could not be sent (%s)", type(e).__name__)
    write_marker(script, "execute" if execute else "dry-run", 0, "RUN SUMMARY skipped=kill_switch", outcome="kill_switch", env=env)
    return True


def killed_midrun(script, env=os.environ):
    """Check inside a run (before a claim / refresh start / pause). True = stop starting new work."""
    active, why = kill_switch_active(env)
    if active:
        log.error("KILL SWITCH ACTIVE (%s): %s stops starting new work in this run", why, script)
    return active


def check_alert_recipient(cfg, script):
    """Loud log when real SES is in use and ALERT_TO is not on the allow-list (the alert would be silently dropped).
    Returns True when alerts can be delivered."""
    if not cfg.ses:
        return True
    if cfg.allow_all or cfg.alert_to.strip().lower() in cfg.allow:   # allow_all: validated address, no list (D-050)
        return True
    log.error("ALERT RECIPIENT NOT ON ALLOW-LIST: %s would DROP every alert mail (ALERT_TO is not in SEND_ALLOWED_RECIPIENTS). "
              "Add it to SEND_ALLOWED_RECIPIENTS or nobody is told when something fails.", script)
    return False


# ---------- last-run markers ----------
def lastrun_dir(env=os.environ):
    return Path(env.get("LASTRUN_DIR") or ROOT / "out" / "lastrun")


def mark_started(script, mode, env=os.environ):
    """out/lastrun/<script>.running: written at the start of a run, removed by write_marker. One that stays behind means the run was
    killed (time limit, host restart) or crashed before it could report; scripts/watchdog.py mails about it. Never raises."""
    try:
        d = lastrun_dir(env)
        d.mkdir(parents=True, exist_ok=True)
        atomic_write(d / ("%s.running" % script), json.dumps({"script": script, "mode": mode, "pid": os.getpid(),
                                                              "since": sg_now().isoformat(timespec="seconds")}))
    except Exception:
        pass


def read_running(script, env=os.environ):
    try:
        return json.loads((lastrun_dir(env) / ("%s.running" % script)).read_text(encoding="utf-8"))
    except Exception:
        return None


def write_marker(script, mode, exit_code, summary, outcome="ran", env=os.environ, now=None):
    """Record this run (atomic write). Counters are per Singapore day and per mode. A non-zero execute run also goes to the
    Windows Event Log. Never raises."""
    try:
        now = now or sg_now()
        d = lastrun_dir(env)
        d.mkdir(parents=True, exist_ok=True)
        try:
            (d / ("%s.running" % script)).unlink()
        except FileNotFoundError:
            pass
        f = d / ("%s.json" % script)
        try:
            old = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            old = {}
        day = now.date().isoformat()
        days = old.get("days", {})
        days = {k: v for k, v in days.items() if k >= (now.date() - dt.timedelta(days=7)).isoformat()}
        c = days.setdefault(day, {})
        e = c.setdefault(mode, {"runs": 0, "bad": 0, "skipped": 0, "first": now.strftime("%H:%M"), "last": None})
        e["runs"] += 1
        e["bad"] += 1 if exit_code else 0
        e["skipped"] += 1 if outcome != "ran" else 0
        e["last"] = now.strftime("%H:%M")
        e["last_ts"] = now.isoformat(timespec="seconds")
        atomic_write(f, json.dumps({"script": script, "last_run": now.isoformat(timespec="seconds"), "mode": mode,
                                    "exit_code": exit_code, "outcome": outcome, "summary": (summary or "")[:600], "days": days}))
    except Exception as e:
        log.warning("last-run marker not written (%s)", type(e).__name__)
    if exit_code and mode == "execute" and outcome not in ("pat_busy", "kill_switch"):
        event_log(script, "run ended with exit code %s (%s): %s" % (exit_code, outcome, (summary or "")[:300]), "ERROR", env)


def read_marker(script, env=os.environ):
    try:
        return json.loads((lastrun_dir(env) / ("%s.json" % script)).read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------- streak counters (PAT busy, refresh probe) ----------
def note_streak(key, bad, env=os.environ):
    """Count consecutive bad runs of `key` (file out/lastrun/<key>.busy). Returns the streak; a good run resets it."""
    f = lastrun_dir(env) / ("%s.busy" % key)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        n = int(f.read_text()) if f.exists() else 0
        n = n + 1 if bad else 0
        atomic_write(f, str(n))
        return n
    except Exception:
        return 0


def note_pat_busy(script, busy, threshold=4, env=os.environ):
    return note_streak(script, busy, env)


def pat_busy_threshold(env=os.environ):
    try:
        return max(1, int(env.get("PAT_BUSY_ALERT_AFTER", 4)))
    except ValueError:
        return 4


# ---------- who holds the PAT lock, and for how long may that be normal ----------
def holder_allowance_min(script, env=os.environ):
    """Minutes a script may normally hold the PAT lock in one run (plus 5 min grace). t4 is capped by T4_MAX_LOCK_MIN (default 10)."""
    try:
        t4cap = float(env.get("T4_MAX_LOCK_MIN", 10))
    except ValueError:
        t4cap = 10.0
    try:
        t2cap = float(env.get("T2_MAX_LOCK_MIN", 10))
    except ValueError:
        t2cap = 10.0
    return {"t4": t4cap, "t6": 45.0, "t11": 10.0, "t2": t2cap}.get(script, 30.0) + 5


def write_holder(script, env=os.environ):
    try:
        d = lastrun_dir(env)
        d.mkdir(parents=True, exist_ok=True)
        atomic_write(d / "pat_holder.json", json.dumps({"script": script, "since": sg_now().isoformat(timespec="seconds")}))
    except Exception:
        pass


def clear_holder(script, env=os.environ):
    try:
        f = lastrun_dir(env) / "pat_holder.json"
        if f.exists() and json.loads(f.read_text(encoding="utf-8")).get("script") == script:
            f.unlink()
    except Exception:
        pass


def read_holder(env=os.environ):
    try:
        return json.loads((lastrun_dir(env) / "pat_holder.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def on_pat_busy(script, execute, ses=False, cfg=None, mailer=None, local_host="127.0.0.1", local_port=1025, env=os.environ, now=None):
    """Call when a run is skipped because another script holds the Tableau PAT lock. A holder that has held it for no longer than its normal
    allowance (t4 cap + grace ...) is an ordinary backlog and is NOT counted. After PAT_BUSY_ALERT_AFTER (default 4) counted skips in a
    row one alert goes out per Singapore day: something holds the lock far too long and nothing is running."""
    h = read_holder(env)
    if h:
        try:
            held = ((now or sg_now()) - dt.datetime.fromisoformat(h["since"])).total_seconds() / 60
            if held <= holder_allowance_min(h.get("script", ""), env):
                log.info("PAT lock held by %s for %.0f min, within its normal allowance: %s skips this run, not counted", h.get("script"), held, script)
                return 0
        except Exception:
            pass
    n = note_streak(script, True, env)
    if execute and n >= pat_busy_threshold(env):
        log.error("PAT BUSY %d runs in a row for %s: something has held the Tableau PAT session lock for a long time", n, script)
        event_log(script, "PAT lock busy %d runs in a row" % n, "ERROR", env)
        try:
            import t6_send as t6
            cfg = cfg or alert_config(ses, env, script, True, not ses)
            mailer = mailer or t6.make_mailer(cfg, local_host, local_port, env)
            alert_once(cfg, mailer, script, "pat_busy", "%s was skipped %d runs in a row because the Tableau PAT session lock is held "
                       "by another script (or a stuck process) beyond its normal time. Check Task Scheduler for a hung t4/t6/t11/t2." % (script, n))
        except Exception as e:
            log.error("PAT-busy alert could not be sent (%s)", type(e).__name__)
    return n


def pat_ok(script, env=os.environ):
    note_streak(script, False, env)


def priorities(cur, kind):
    """{item id: priority} from Integration.Tableau_Priority (R9, D-062); lower goes first. {} when nothing is marked or the
    table is not deployed yet, so the order falls back to by name."""
    if cur.execute("SELECT OBJECT_ID('Integration.Tableau_Priority')").fetchone()[0] is None:
        return {}
    return {i: p for i, p in cur.execute("SELECT Item_id, Priority FROM Integration.Tableau_Priority WHERE Kind = ?", kind).fetchall()}


def priority_key(prio, item_id, name):
    """Sort key: marked items first (lowest number first), then everything else; name order within each group."""
    return (prio.get(item_id, 2 ** 31), (name or "").lower())
