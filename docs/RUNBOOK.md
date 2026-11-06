# Runbook

Plain-English operating guide for the Tableau subscription automation (scripts t2, t11, t4, t6, daily_summary). Written against the dev
setup; the tasks, paths and settings are the same on any host. Nothing here has been run on production. Everything marked "guess" is to be
checked during the pilot.

**Deploying to a server?** Start with [DEPLOYMENT_PLAN.md](DEPLOYMENT_PLAN.md) (step-by-step install, `.env`, scheduled tasks, staged rollout, what is untested) and [DB_CHANGES.md](DB_CHANGES.md) (every database change in order, permissions, rollback, dev-only test changes). This runbook is for operating the system once it runs.

## 1. What runs, in which order

| Script | What it does | When | Task Scheduler settings |
|---|---|---|---|
| `t2_mapping_sync.py` | Copies workbooks, data sources, subscriptions and their schedules from Tableau into the DWH mapping tables | **ONCE A DAY at about 07:50** (D-046), before the 08:30-16:30 cycle of the others. A workbook republished after the sync simply waits for the next day's sync (t6 logs it quietly as `awaiting_sync`, no alert; see below). It holds the shared Tableau login at most `T2_MAX_LOCK_MIN` (default 10, raise to about 20 if the scope grows) minutes; workbooks not reached in that time are skipped (stale, alert, exit 2) and read the next day, so rerun T2 by hand if that happens | Daily trigger 07:50, no repetition, ExecutionTimeLimit 30 min, IgnoreNew |
| `extract_takeover.py` | Keeps the Tableau extract refresh tasks of taken-over extracts deleted; with an allow-list takes over new ones (section 20) | **Once a day, 08:20** (after t2, before t11). Dry run unless registered with `-ExtractExtraArgs` | ExecutionTimeLimit 10 min |
| `t11_pause.py --execute` | Pauses the old Tableau subscriptions the script has taken over and keeps them paused | Every 15 min, 08:30-16:30, **first of the three** | Repeat every 15 min for 8 h, IgnoreNew, ExecutionTimeLimit 10 min |
| `t4_refresh.py --execute` | Refreshes extracts once their ETLs are done, waits for the jobs | Every 15 min, 08:30-16:30, second | IgnoreNew, ExecutionTimeLimit 40 min. It holds the Tableau login at most `T4_MAX_LOCK_MIN` (default **10**) minutes per run; jobs still running are reconciled and targets not started are handled by the next run |
| `t6_send.py --execute` | Exports and mails the reports that are due and ready | Every 15 min, 08:30-16:30, third | IgnoreNew, ExecutionTimeLimit 55 min (its own budget is 40 min) |
| `daily_summary.py --execute` | One summary mail for the day | Once, 17:30 (the registered task time) | ExecutionTimeLimit 10 min |

Rules that matter:
- **Each script is its own scheduled task** (not chained in one batch file), each with *IgnoreNew* ("do not start a new instance") and an
  ExecutionTimeLimit. Offset the start times by a minute or two (t11 at :00, t4 at :02, t6 at :04) so they do not all reach for the Tableau
  login at the same second.
- **One Tableau login (PAT) at a time.** All four Tableau scripts share one lock. A script that finds the lock taken skips its run, writes
  `skipped=pat_busy` and exits 0; it tries again 15 minutes later. After `PAT_BUSY_ALERT_AFTER` (4) skips in a row you get one alert mail
  per day: something is holding the lock (usually a hung process).
- **T2 first** (the daily 07:50 run, before the 08:30 cycle). If T2 has not run for `MAPPING_MAX_AGE_H` hours (default 26, the safety net) t6 sends nothing and alerts "mapping outdated". The limit is
  a setting; the schedule of T2 must be shorter than it (daily is fine). **A workbook republished after the daily sync** is not sent that day until the next sync has read it (its mapping may be outdated): t6 writes one INFO line per subscription and counts it as `awaiting_sync`; the daily summary shows the count as "waiting for the next daily sync". It is NOT an alert and not a problem exit code. If it must go out today, rerun `python scripts/t2_mapping_sync.py` by hand. If T2 fails (alert "T2 mapping sync failed"), rerun it by hand:
  `python scripts/t2_mapping_sync.py`. If it fails again and you need the reports out, raise `MAPPING_MAX_AGE_H` for the day only after you
  have looked at why.
- Pause before first send. A subscription must be paused by t11 *before* t6 sends it, otherwise Tableau and the script both mail it. t6
  refuses to send anything that is not paused (see section 6).
- Settings come from `.env` (see `.env.example`). Dry run is the default for every script; `--execute` is needed for anything real.
- **Real mail vs the local test sink is ONE setting: `MAIL_TRANSPORT=local|smtp` in `.env`** (default `local`; `ses` is accepted as another
  word for `smtp`). Set `MAIL_TRANSPORT=smtp` once and every script (t2, t4, t6, t11, daily_summary, and the `--restore` commands) sends its mails and
  alerts for real; nobody has to remember a flag on a task line (`--smtp`, alias `--ses`, still forces it for one run). With `local`, reports AND alerts
  go to a local test sink and **nobody is told about anything**. Every script logs `MAIL TRANSPORT local|smtp ...` as its first lines: check it after
  every change of the task lines. Real mail also needs `SEND_ALLOWED_RECIPIENTS` (must contain `ALERT_TO`; an exact address list) and `SMTP_USER`/`SMTP_PASSWORD`. The one exception is the UAT-only switch `SEND_ALLOW_ALL_UAT=1` (section 16), which replaces the list on the UAT Tableau server and is ignored everywhere else.
- **Second signal that does not need mail:** every run that ends with a non-zero exit code, every kill-switch run and every long PAT-busy streak also writes
  an entry to the **Windows Application event log, source `TableauAutomation`** (event id 100 error, 101 warning). Register the source once, as an
  administrator: `eventcreate /L APPLICATION /T INFORMATION /ID 1 /SO TableauAutomation /D "registered"`. Then point your monitoring (SCOM, a Task
  Scheduler task triggered by the event, a scheduled `Get-WinEvent -FilterHashtable @{LogName='Application';ProviderName='TableauAutomation'}`) at it. If the
  write fails the script only logs a warning (`out/*.log`). Switch it off with `EVENTLOG_DISABLED=1`.
- **Dead-man check (a script that did not run):** `daily_summary` reads the last-run markers and lists every script that did not run today or whose last
  scheduled run is more than 90 minutes old ("it seems to have stopped"). A dry run of a script never counts as a run. Because that check lives in
  the same mail path, also keep ONE monitor that does not: alert on "no `TableauAutomation` event AND no change of `out/lastrun/t6.json` since
  09:30 on a business day", or on Task Scheduler's own "task did not start" history. Task Scheduler's *last result* alone is not monitoring.

## 2. Where things are

| What | Where |
|---|---|
| Logs (one file per day, about a week kept, in the project folder) | `out/t2_sync_<date>.log`, `out/t11_pause_<date>.log`, `out/t4_refresh_<date>.log`, `out/t6_send_<date>.log`, `out/daily_summary_<date>.log` (for example `out/t6_send_2026-10-26.log`) |
| Last-run markers (which script ran today, how many times, last result) | `out/lastrun/<script>.json` (host-local) |
| Alert de-duplication state (one alert per problem per day) | `out/*_alert_state*.json` |
| Backups of paused subscriptions | DWH table `Integration.Tableau_SubscriptionBackup` and `out/t11_backup/*.json` |
| Send log (one slot per subscription per day) | `Integration.Tableau_SubscriptionSendLog` |
| Refresh log | `Integration.Tableau_RefreshJob` |
| Schedules | `Integration.Tableau_Schedule` |

## 3. How to read a RUN SUMMARY line

Every run ends with one line starting `RUN SUMMARY`. `needs_human=0` means nothing needs a person. Anything else needs a look. Exit code 1 means
the same (Task Scheduler "last result" 0x1). Do not rely on Task Scheduler alone: read the daily summary mail.

- **t6**: `sent` mails sent this run; `not_due` not on its schedule today (normal); `taken_over_today` paused today after the schedule time,
  first send is the next occurrence (normal); `not_ready` waiting for ETL or refresh; `blocked` mapping gap or failed ETL; `already_sent`;
  `not_taken_over` left to Tableau; `rejected` the mail server refused the message (nothing queued); `ambiguous` outcome unknown (see
  section 5); `deferred` more due than `MAX_SENDS_PER_RUN`, the rest wait for the next run; `deferred_breaker` + `breaker=1` sending was
  stopped after a server problem; `no_allowed_recipient` the person's address is not on `SEND_ALLOWED_RECIPIENTS` (the old subscription is
  paused, so that person gets nothing: fix the allow-list or restore); `schedule_unknown` the schedule could not be evaluated;
  `halted` a takeover is halted (section 6); `awaiting_sync` workbook changed after the daily T2 sync, waits quietly for the next sync (normal, D-046); `schedule_mode=honour|ignore`.
- **t4**: `refreshed`, `waiting` (ETL not done), `skipped`, `failed`, `lookup_failed`; targets that did not start inside the `T4_MAX_LOCK_MIN` (10 minute)
  budget are simply tried again next run.
  A `Failed` refresh row saying "Tableau reports a refresh of this data source is already queued; nothing was submitted by us" means Tableau Server
  refused a new job because an older refresh of that data source is still Pending (stale jobs). Fix: a Tableau admin cancels the stale Pending
  extract-refresh jobs for that data source (Server > Jobs, filter Pending); the next run submits normally. It counts in `failed` and exit code 1 until then.
- **t11**: `paused` (newly taken over; includes `taken_suspended`, those that were already suspended in Tableau and needed no change), `repaused` (somebody resumed one), `confirmed`, `halted`/`halted_rows`, `failed`, `skip_*` (why something was not taken over).
- **t2**: `exit=0` all read; `exit=2` written but some items were skipped (named in the log and the alert); exit 1 failed, nothing written.
- `skipped=pat_busy` or `skipped=kill_switch`: nothing was done on purpose.

## 4. Every morning (5 minutes)

1. Read yesterday's daily summary mail. Anything marked `!!` needs action.
2. Check today's `out/t2_sync_<date>.log` ends with `RUN SUMMARY ... exit=0` from this morning (or the summary says T2 ran).
3. After 09:00 check today's `out/t6_send_<date>.log` for `needs_human=1` lines and that today's due reports show `sent` (or an understood reason).
4. Look at Task Scheduler: all five tasks enabled, last run recent.
5. On the 1st working day after a weekend/month start: monthly and Saturday reports have a catch-up window of `SCHEDULE_CATCHUP_DAYS` (3): a report
   that could not go on its day (no ETL) goes on the first day the data is ready, otherwise a "due occurrence never sent" alert comes on the 4th day.

## 5. A report is stuck: Claimed / TimedOut slot

What the states mean: **Claimed** a run started this subscription's send and has not finished (or died); **TimedOut** a Claimed row older than
90 minutes. Both keep the day's slot, so nothing else is sent for that subscription today. Deliberate: it is better to miss a mail than to send it twice.

1. Find the rows (this only reads). It also shows rows from the last 7 days: a Claimed/TimedOut row of an EARLIER day still counts as "sent" for a
   schedule occurrence that is still inside its catch-up window.
```sql
SELECT l.SendLog_id, l.SendDate, l.Subscription_id, s.Subject, l.Status, l.ClaimedAt, l.Message
FROM Integration.Tableau_SubscriptionSendLog l
JOIN Integration.Tableau_Subscription s ON s.Subscription_id = l.Subscription_id
WHERE l.SendDate >= DATEADD(day, -7, CAST(SYSDATETIMEOFFSET() AT TIME ZONE 'Singapore Standard Time' AS date))
  AND l.Status IN ('Claimed', 'TimedOut', 'Failed')
ORDER BY l.SendDate DESC, l.SendLog_id;
```
2. Decide whether the mail arrived. Ask one recipient, or look at the log (`out/t6_send_<date>.log`, search the subscription id) and the SES sending
   record: a successful send stores `smtp_id=...` in `Message`. A mail with an `smtp_id` was accepted by the mail server.
3. Release it, depending on the answer (change `<id>`; the `AND Status` part protects a row that changed meanwhile):
```sql
-- The mail was NOT sent (or you want it sent again): free the slot, the next run sends it.
-- SAFE FORM: a Claimed row is released only when it is OLDER than the 90 minute claim timeout (a younger one may still be sending: releasing it
-- can make the next run send the same report a SECOND time). TimedOut and Failed rows can be released at any time.
UPDATE Integration.Tableau_SubscriptionSendLog
SET Status = 'Released', Message = LEFT(ISNULL(Message,'') + ' | released by operator ' + CONVERT(varchar(16), SYSDATETIME(), 120), 1000)
WHERE SendLog_id = <id>
  AND (Status IN ('TimedOut', 'Failed') OR (Status = 'Claimed' AND ClaimedAt < DATEADD(minute, -90, SYSDATETIMEOFFSET())));
-- 0 rows updated = the row is a young Claimed one: wait, or look at whether a t6 process is still running (Task Scheduler / Task Manager) first.

-- The mail WAS sent: record it so nothing else is sent today.
UPDATE Integration.Tableau_SubscriptionSendLog
SET Status = 'Sent', SentAt = SYSDATETIMEOFFSET(), Message = LEFT(ISNULL(Message,'') + ' | marked sent by operator', 1000)
WHERE SendLog_id = <id> AND Status IN ('Claimed', 'TimedOut');
```
   **Warning:** never release a young `Claimed` row (under 90 minutes) by hand: the process that claimed it may be about to send, and a second run would then
   send the report twice. The next t6 run turns it into `TimedOut` by itself after 90 minutes. Never change a `Sent` row's status or DELETE a Sent, Claimed or TimedOut row: no trigger stops it (dropped 2026-11-06), and deleting it frees the slot and allows a second send. To remove a real Claimed/TimedOut row, release it first as above.
   The `Message` text uses a fixed label (`operator`) because the login name has no place in a shared log; note who did what in your own ticket.
   **Releasing a TimedOut / Failed row from an EARLIER day reopens that schedule occurrence** (the occurrence is no longer "sent"): if it is still inside its catch-up window
   (3 days) the next run sends it, so a recipient may get a resend. Only release it when you are sure the report did not arrive, or when a resend is wanted. For a schedule with a catch-up window, a row marked `Sent` or left `Claimed/TimedOut` counts as "sent" for
   that occurrence; a `Released` row does not.
4. `Failed` with a message starting `REJECTED:` means the mail server refused the message (for example "Email address is not verified", SES sandbox).
   Nothing was queued, but t6 does not retry the same day. After fixing the cause use the first UPDATE (Failed -> Released) to retry today.
5. `Failed` with `EXPORT:` retries up to 3 times a day; `SMTP:` retries and alerts after 3; `OVERSIZE:` is final for the day.

## 6. Paused subscriptions: look up, restore, halted

List what the script owns:
```sql
SELECT b.Backup_id, b.Subscription_id, b.Subject, b.PausedAt, b.LastRepausedAt, b.RepauseCount, b.Halted, b.Note, b.GoneSince
FROM Integration.Tableau_SubscriptionBackup b WHERE b.TakenOver = 1 AND b.RestoredAt IS NULL;
```
**Stop the script handling a subscription:** either DELETE it in Tableau (the next T2 sync deactivates it; t6 stops at once on the 404; nothing to do in the DWH), or restore it (below). There is no "suspended on purpose" category any more (D-046): every in-scope subscription that still exists in Tableau is taken over and sent, suspended or not. If you do not want a report sent, delete the subscription.

**Restore a paused subscription (give it back to Tableau)**, one command, works even with the kill switch on. `--restore` simply UN-SUSPENDS the subscription (the script keeps no "original state"):
```
python scripts/t11_pause.py --restore <subscription id> --execute
python scripts/t11_pause.py --restore-all-for-workbook <workbook id> --execute
```
R5 / F-4: if the script already sent (or started to send) that subscription TODAY, `--restore` is REFUSED (error `RESTORE REFUSED`, exit 1, nothing changed), because the resumed Tableau schedule could mail it a second time. Restore tomorrow, or add `--allow-restore-after-send` if a second mail is acceptable.
It marks the row restored first (the script stops sending and re-pausing at once), then resumes the subscription in Tableau. If it says the
PAT is busy, run it again. If the resume fails it alerts; run the same command again. Check in the Tableau UI that the subscription is active and
its subject, message, format and schedule are unchanged.

**Halted takeover**: a pause changed something besides "suspended" (t11 compares before and after). The row is marked `Halted = 1`, t11 stops
touching it and t6 does not send it. Compare the Tableau subscription with `Tableau_SubscriptionBackup` / `out/t11_backup/<id>_<subscription>.json`,
fix it by hand in Tableau if needed, then either restore it (above) or clear the halt:
```sql
UPDATE Integration.Tableau_SubscriptionBackup SET Halted = 0, Note = NULL WHERE Backup_id = <id> AND RestoredAt IS NULL;
```
**Resumed by a human**: t11 pauses it again on its next run and t6 skips that subscription for the rest of that day (the old subscription may
already have mailed). Nothing to do except telling the person not to resume it.
**Deleted or re-created in Tableau**: the open row gets `GoneSince`; after `TAKEOVER_GONE_CLOSE_DAYS` (7) days it is closed with an alert. A
re-created subscription is a new subscription: it is taken over only if its workbook is in the allow-list (workbook ids, `TAKEOVER_WORKBOOK_IDS`).
**First pilot pause**: compare the subscription in the Tableau UI before and after (subject, message, format, schedule, "send if view is empty").
The Tableau library reads an attribute the server omits as false; the script leaves such attributes unset on the update and warns in the log
(`the Tableau response does not contain ...`).

## 7. Switch everything off

*Stop the scripts* (reports taken over stay paused, so nobody gets them!):
- create the file `out/KILL` (empty is fine), or set `TABLEAU_AUTOMATION_DISABLED=1` in `.env`/the environment. t2, t4, t6 and t11 check this
  first, log `KILL SWITCH ACTIVE`, send ONE alert mail a day, write `skipped=kill_switch` and exit 0. They do not touch the DWH or Tableau. `daily_summary`
  keeps working and shows the kill switch at the top.
- To resume: delete `out/KILL` / unset the variable.

*Hand the reports back to Tableau* (do this if the kill switch is going to stay on for more than a day). **Warning:** if t6 already sent a report today and you
then restore its subscription, Tableau's own schedule may send the same report again later that day (a second mail). Since R5 `--restore` refuses in that case;
restore after the day's schedule time has passed, or accept the duplicate with `--allow-restore-after-send`:
```
python scripts/t11_pause.py --restore-all-for-workbook <workbook id> --execute     # per workbook, or --restore <id> per subscription
# (with MAIL_TRANSPORT=smtp in .env its alerts go out for real; otherwise add --smtp)
```
Disabling the Task Scheduler tasks also works but is slower and per task. Dry-run mode of any script never writes anything.

## 8. Rotate credentials

- **Tableau PAT** (`TABLEAU_PAT_NAME`, `TABLEAU_PAT_SECRET` in `.env`): create the new PAT in Tableau (account settings > personal access
  tokens, ideally for a service user), put both values into `.env`, then run `python scripts/t4_refresh.py` (dry run) to see a clean sign-in. A
  Tableau PAT allows ONE session at a time, so never use the same PAT in two places (this script's PAT must differ from the old refresh job's).
  Then delete the old PAT. Calendar the expiry date (guess: PATs expire after a period of inactivity or absolute age; check the server's setting).
- **SMTP credentials** (`SMTP_USER`, `SMTP_PASSWORD`; the old names `SES_SMTP_USER`/`SES_SMTP_PASSWORD` still work). For AWS SES (owned by the team): create
  new SMTP credentials in AWS, update `.env`. For the Gmail test setup see section 12. After any change run a normal `--execute` t6 run only with the team's
  go-ahead; the preflight at the start of each real run (login + `MAIL FROM` + `RCPT TO`, no message) fails loudly before anything is claimed if the
  credential is wrong.
- `.env` holds both secrets in clear text: restrict its file permissions to the service account and administrators.

## 9. Mail checks you should know about

- **Preflight**: before the first real claim of a real-mail run the script connects, logs in and asks the server whether it would accept the sender and
  one allowed recipient. If not: nothing is claimed, one alert, exit 1, next run tries again.
- **Circuit breaker**: ONE rejection of a single report ("not queued", or one refused recipient) is recorded as `Failed` + alert and the run goes on. TWO such
  failures in a row, or any other server problem (connection, login, an unknown or unexpected final reply, a timeout after the message) stop the run: no
  further report is claimed, one alert carries the server's reply (addresses removed), the rest is retried next run.
- **Only a known rejection frees the slot, and only for providers with a known list**: with `SMTP_PROVIDER=ses` a 554 "Email address is not verified" after the message is
  `Failed` (`REJECTED:`). With `gmail` or `generic` NOTHING frees a slot after the message: every non-250 reply, timeout or lost connection leaves the row
  `Claimed` (section 5); only errors before the message (connect, login 535, MAIL/RCPT refused) are `Failed`. The SES list is from memory of SES behaviour and must be
  verified at the first live test (`KNOWN_REJECTIONS` in `t6_send.py`).
- **Lost alerts**: an alert mail whose outcome is unknown (timeout after the message) is sent once more on a later run, so it can arrive twice but is not lost.
- **Kill switch inside a run**: the kill switch is also checked before every claim (t6), every refresh start (t4) and every pause (t11), not only at startup.
- **Alert recipient**: if `ALERT_TO` is not on `SEND_ALLOWED_RECIPIENTS` in real mode, every script logs `ALERT RECIPIENT NOT ON ALLOW-LIST` at start
  (alerts would be dropped). Add it.
- Dry runs read Tableau (to see the live subscription) and therefore take the same PAT lock while they work and release it when they end; if the lock is
  busy they skip. They never wait for it. Run them outside the 08:30-16:30 window if you can.

## 10. Server load settings (defaults are conservative, tune in the pilot)

UAT has no live data, so UAT timings cannot predict production load. Settings (all in `.env`): `MAX_CONCURRENT_REFRESHES=1` and
`REFRESH_STAGGER_SEC=60` (t4 refreshes one extract at a time; a target that does not get its turn inside the 10 minute `T4_MAX_LOCK_MIN` budget is retried next run),
`EXPORT_PAUSE_SEC=20` (t6 exports strictly one report at a time with this pause between reports), `MAX_SENDS_PER_RUN=10`, `T2_PACE_SEC=0.5` and
`T2_METADATA_PAGE=200` (t2). t6 also does not export a workbook while a refresh job of one of its extracts is running or queued (if Tableau's job list
cannot be read it counts as busy, and 3 failed runs in a row raise an alert). **t4 holds the shared Tableau login at most `T4_MAX_LOCK_MIN` (default 10) minutes per run**;
jobs still running and targets not started are picked up by the next run, so t6 and t11 are never starved for long. A skipped run that finds the login held by
t4 inside this allowance is a normal backlog and is not counted towards the PAT-busy alert. The list of background jobs is fetched once per run (60 s cache).

**Wait while Tableau is busy (D-057).** Before t4 submits a refresh and before t6 exports, the scripts look at Tableau's background jobs. If anyone else has a job Pending or InProgress (their own refreshes, Tableau's subscriptions, anything) the run does nothing and logs `Tableau busy: N queued, M running; waiting` (dry run: `would wait`). t4 counts the targets as `waiting`; t6 counts the reports as `not_ready` (reason "Tableau busy"), writes no send-log row and sends no alert of its own; if it is still busy after the 15:30 cutoff the normal "still not ready after the cutoff" alert covers it. The next 15-minute run tries again. Our own recorded job ids never count. A Pending job older than `TABLEAU_BUSY_STALE_HOURS` (default 6) is ignored with a WARNING naming how many (cancel such jobs on Tableau Server). `TABLEAU_BUSY_CHECK=0` switches the check off (tests only; default is on). If the job list cannot be read, the run also waits (and counts as a lookup failure).

## 11. Schedules (what "due" means)

`SCHEDULE_MODE=honour` (default): a subscription is sent only on the days its Tableau schedule says it would fire (daily/hourly: every day or the listed
weekdays; weekly: the listed weekdays; monthly: the day number, the last day, or "first Monday"-style), after its data is ready, at most once per due
day. A due day whose data is not ready is retried on the following days up to `SCHEDULE_CATCHUP_DAYS` (3), never beyond the next due day; then an alert.
`SCHEDULE_MODE=ignore`: the old rule, every day when the data is ready. Time of day is ignored. Details in D-036.

Sunday and Monday (D-065): no ETL pipeline runs, so a weekly or monthly report due on those days waits and goes out on Tuesday through the catch-up window. The "not ready" alert on those days is expected noise for such reports; no action needed.

## 12. Gmail as the test mail server (instead of SES)

> **WARNING for Gmail testing: run `t11_pause.py --execute` only for subscriptions owned by the Gmail account's owner.** t11 cannot see recipient addresses. With the allow-list holding
> only the owner's address, a taken-over subscription owned by anybody else is PAUSED in Tableau and then never delivered (t6 drops that recipient), so that person would stop getting their
> report. Therefore, when `MAIL_TRANSPORT=smtp` and `SMTP_PROVIDER=gmail`, `t11 --execute` **refuses to start without `--owner-user-id <Tableau user id>`** (repeatable, or env
> `TAKEOVER_OWNER_USER_IDS`): only subscriptions whose subscriber has that Tableau user id are taken over. Find the id with a dry run: `t11_pause.py --workbooks <workbook name>` logs
> `would back up and pause ... owner=<user id>` for each candidate. A restore (`--restore`) does not need it.
> Since R5 (F-2) the owner fence is required for EVERY `--execute` with an allow-list (not only in Gmail mode); a keep-paused run with an empty allow-list needs none.

For testing the real mail path uses the project owner's Gmail account. The mail code is provider-neutral: `.env` (names only here, never put the values in a document):
```
MAIL_TRANSPORT=smtp
SMTP_PROVIDER=gmail
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=<the gmail address>
SMTP_PASSWORD=<the Google app password named "Tableau-Python">
SEND_FROM=<the same gmail address>
SEND_ALLOWED_RECIPIENTS=<the same gmail address>
ALERT_TO=<the same gmail address>
```
- **Create the app password:** sign in to the Google account > Security > turn on 2-Step Verification (required) > Security > "App passwords" (or search for it) > create one, name it
  `Tableau-Python` > Google shows a 16-character password ONCE: copy it into `SMTP_PASSWORD` in `.env` (spaces do not matter) and close the page. Revoke it on the same page when testing is
  over. Never paste it into chat, logs or tickets. An ordinary account password does not work with SMTP.
- **From address:** Gmail rewrites the From address to the authenticated account unless the other address is a verified "Send mail as" alias, so for testing `SEND_FROM` must be the Gmail
  address (the script logs a warning otherwise). The Tableau-style sender name `tableau-reports@example.com` only appears with a real company relay.
- **Limits (personal Gmail, guess from memory, verify):** about 500 recipients a day, 25 MB per message including encoding; the size limit defaults to **20 MB** for `SMTP_PROVIDER=gmail`
  (`--size-limit-mb` overrides). Exceeding the daily limit shows up as a 5xx after the message or a lockout; because Gmail has no known-rejection list, such a message stays `Claimed`
  (section 5) until a person checks.
- **Errors:** a wrong or revoked app password is a 535 at login: the preflight fails before anything is claimed, one alert, exit 1.
- **The allow-list stays mandatory** (except on the UAT Tableau server with `SEND_ALLOW_ALL_UAT=1`, section 16): real mail needs `SEND_ALLOWED_RECIPIENTS`; during testing it holds only the owner's address, so every other recipient is dropped (and raises the
  `no_allowed_recipient` alert for taken-over subscriptions).
- Tests never connect to `smtp.gmail.com`: the check files fail on any non-loopback SMTP connection and use a local stub server.

## 13. Scope note: "when data refreshes" subscriptions

Only schedule-based (time-based) Tableau subscriptions are in scope. Data-driven ("when data refreshes") subscriptions, if any exist on production, are ignored by design
(user decision, D-044): the scripts neither take them over nor pause them. This is not an open item.

## 14. Clean up before go-live (checklist, D-046)

*Disabled schedules (D-080):* the state of a schedule (enabled or disabled) is ignored. A subscription on a disabled schedule, which Tableau itself never sent, WILL be sent by the script once it is taken over, on the schedule's days. The `schedule_state` column of `review_list.py` shows which subscriptions are on a disabled schedule: check that list before the takeover and delete in Tableau any subscription that must not go out.

The script sends every in-scope subscription that still exists in Tableau, suspended or not. Anything stale or unwanted must be DELETED in Tableau before the first takeover.

0. **SQL order (R5, once per database, dev DWH only until the production guard is changed):** apply `scripts/sql/R4_takeover_all.sql` BEFORE the new `t11_pause.py` runs. The new t11 does not write `WasSuspended`; on a database where R4 was not applied and the column has no default, every takeover fails (`pause_failed`). R4 refuses (RAISERROR + SET NOEXEC ON, nothing after it runs) if a backup row still has `WasSuspended = 1`; it drops the old CHECK, any DEFAULT on the column (whatever its name) and the column. Then apply `scripts/sql/R5_workbook_updated_at.sql` (adds `Tableau_Subscription.WorkbookUpdatedAt`) and run `python scripts/t2_mapping_sync.py` once: until T2 has stored the workbook change time, t6 waits (`awaiting_sync`) and sends nothing. Rollbacks: `R4_takeover_all_rollback.sql` (read its warning about open rows first) and `R5_workbook_updated_at_rollback.sql`.
1. Make sure no other Tableau script is running, then: `python scripts/review_list.py` (read-only: one Tableau sign-in, no export, no mail). It writes `out/review_list.csv` (gitignored; no email addresses, owners are Tableau user ids) and prints the totals.
2. Open the CSV (one row per subscription, sorted by workbook then schedule; a text cell that would start a formula, for example a subject beginning with `=`, `+`, `-` or `@`, is written with a leading quote so Excel shows it as text). `next_due` is the first date the SCRIPT would send if it took the subscription over right now: if today is a fire day and the schedule time has already passed, today does NOT count (takeover-day guard) and `next_due_note` says so; `next_fire_date` is the plain next date the schedule fires; `retry_until` is the last day a failed send of that occurrence is still retried (catch-up window). Look at, in this order: `suspended_now = yes` (these WILL be sent after takeover, were they meant to be?), `readiness` other than Ready (will not be sent; `not_ready_why` says why, for example an unmapped table or an unknown pipeline; file and other-database sources no longer block, section 17), `last_sent` and `subject` of old or test subscriptions, duplicates (same workbook, owner and subject).
3. Delete in Tableau what must not be sent. Do not just leave it suspended.
4. Rerun `python scripts/t2_mapping_sync.py`, then `python scripts/review_list.py` again until the list is what you want to go out. `found_in_tableau = no` means it was deleted after the last T2 sync.
5. Only then set the allow-list and run `t11_pause.py` (dry run first). Day-one order is unchanged: t11 `--execute` before the first t6 `--execute`. Fences (R5, R3B_REVIEW F-2): `--execute` with an allow-list REQUIRES `--owner-user-id <Tableau user id>` (repeat per owner, or `TAKEOVER_OWNER_USER_IDS`; a dry run shows `owner=` for every candidate), and at most `--max-new` (env `TAKEOVER_MAX_NEW`, default 10) NEW takeovers happen per run: further matches are skipped with a warning, one `takeover_cap` alert, `skip_cap=N` in the RUN SUMMARY, and the next run continues. For the pilot use `--subscription-ids <the one id>`, not a workbook id (a workbook id takes over every subscription on that workbook). The scheduled `-Live` registration also demands `--owner-user-id`.
6. A subscription that was already suspended is "taken over" without any change in Tableau (backup row only). On its takeover day it is not sent if its schedule time has already passed (takeover-day guard, same as for the others).
   IMPORTANT (R3B_REVIEW F-1, user decision: WasSuspended is NOT restored): the script cannot tell that Tableau never mailed an already-suspended subscription, so if the first takeover happens on or after a subscription's due time on its due day, that occurrence is skipped (for example the 22 monthly 11:00 subscriptions due 2026-11-01). Rule: take over EVERYTHING at least one day before the first due date (before 2026-10-31 for the monthly ones), never on the due day itself. Since R5 the skip is no longer silent: t6 writes one WARNING per subscription and day to the log and to the Windows Event Log (source TableauAutomation, execute runs only) saying the occurrence was not sent and, if the subscription had been suspended, that Tableau did not send it either; send that report by hand if it is needed.

## 15. Windows Task Scheduler: where everything sits, register, verify, disable, unregister (T12/T13)

Script: `scripts/schedule/register_tasks.ps1`. Not run anywhere yet (written against the plan in section 1; verified only with `-WhatIf`).

**Where everything must sit**

| Thing | Location | Notes |
|---|---|---|
| Install folder (task working directory) | `-RepoRoot`, default = the repo where the script lives (today `<PROJECT_DIR>`) | Copy or clone the repo to the server folder (e.g. `D:\Apps\TableauAutomation`) and run the script from there. Not a user's Desktop in production. |
| Python used by the tasks | `-PythonExe`, default `<RepoRoot>\.venv\Scripts\python.exe` if it exists, else the first `python.exe` on PATH | Use a venv in the install folder (`python -m venv .venv`, then `pip install -r` the packages: tableauserverclient, pyodbc, python-dotenv, sqlglot, Pillow). The service account needs read and execute on it. The script prints the path it will use. |
| `.env` | `<RepoRoot>\.env` (fixed, the scripts load it from there) | Holds the Tableau token, DWH connection, mail settings. Readable only by the service account and admins. Never committed. |
| Logs, last-run markers, alert state, KILL file | `<RepoRoot>\out\` (logs `out\*.log`, markers `out\lastrun\`, kill switch `out\KILL`) | Fixed location (no setting moves it in production). The service account needs Modify on this folder. |
| Task folder in Task Scheduler | `\TableauAutomation` | Fixed. |

**Account that runs the tasks** (`-ServiceAccount DOMAIN\name`, or a group managed service account `DOMAIN\gmsa$`, which needs no password). Needs:
- "Log on as a batch job" on the host; Read on the install folder and python, Modify on `<RepoRoot>\out`.
- The DWH: Windows authentication (Integrated Security), so this account is the DWH login. Read on the ETL log/lineage tables; read and write on the mapping, schedule, backup, refresh-job and send-log tables the scripts own (`Integration.Tableau_*`); no DDL. The grant list derived from the code is in [DB_CHANGES.md](DB_CHANGES.md) section 9; it is not tested with a restricted account, so confirm with the DBA when the production DWH is set up.
- Tableau: no account rights needed; the token in `.env` is the identity there (separate token for the script, D-017; it must be allowed to read workbooks and views, pause/resume subscriptions, run extract refreshes).
- Mail: `MAIL_TRANSPORT`, `SMTP_*`, `SEND_ALLOWED_RECIPIENTS` in `.env`; outbound access to the SMTP host.
- Write to the Application event log is by source (below); no special right once the source exists.

**One-time, as administrator, on the host:** register the event source so the scripts can write to the Application log:
`eventcreate /L APPLICATION /T INFORMATION /ID 1 /SO TableauAutomation /D "registered"`

**Register (dry-run only by default)**
```
cd <RepoRoot>
.\scripts\schedule\register_tasks.ps1 -RepoRoot <RepoRoot> -ServiceAccount 'DOMAIN\svc_tableau' -WhatIf     # preview, changes nothing
.\scripts\schedule\register_tasks.ps1 -RepoRoot <RepoRoot> -ServiceAccount 'DOMAIN\svc_tableau'             # creates the tasks (asks for the password; none for a gMSA)
```
Running it again replaces the tasks; a task that was Disabled stays Disabled, and the script prints `mode OLD -> NEW` per task (re-running without `-Live` on a live host downgrades it to dry-run). Registration FAILS (not warns) if `.env` or any of the five scripts is missing or python cannot be run. Python is taken in this order: explicit `-PythonExe`, `<RepoRoot>\.venv\Scripts\python.exe`, a machine-wide python on PATH; a python inside a user profile (`\Users\`, `\AppData\`) gives a warning because the service account probably cannot run it. The script also warns if the host time zone is not Singapore (tasks use host local time, the scripts use Singapore time). Without `-Live` every task is dry-run only: t2 `--dry-run`, t11/t4/t6 `--dry-run`, daily_summary without `--execute`; nothing is written, paused, refreshed or mailed. Task times: t2 07:50; t11 08:30, t4 08:32, t6 08:34, each every 15 min. **Real last starts: t11 16:30, t4 16:32, t6 16:34** (the window is 8 h after each task's own first start); a run already going when the window ends is NOT killed (StopAtDurationEnd = false), only its time limit stops it. daily_summary 17:30. t2 has StartWhenAvailable (a missed 07:50 runs when the host is back; the others do not catch up, and the 26 h mapping-age limit covers one missed t2). Time limits 30 / 10 / 30 / 55 / 10 min (t6's own budget is 40 min), IgnoreNew.

**Go live (only after the pilot is signed off):** re-run with `-Live` (adds `--execute`; t2 drops `--dry-run`). `-Live` throws unless `-T11ExtraArgs` carries an allow-list (`'--workbooks <workbook guid[,guid]>'` or `'--subscription-ids <ids>'`, plus `--owner-user-id <id>`, now always required); only those three flags are accepted (`--restore` and `--force-notready` are rejected). It then asks for confirmation; `-Force` skips only that prompt, never the allow-list. Real mail additionally needs `MAIL_TRANSPORT=smtp` in `.env`.

**Verify**
- `Get-ScheduledTask -TaskPath '\TableauAutomation\' | Get-ScheduledTaskInfo` shows last run time and result (0 = ok); the task Description says `mode=DRY-RUN` or `mode=LIVE`.
- Open one task's action: the arguments show exactly which flag it runs with (no `--execute` in dry-run mode).
- `out\*.log`: first lines show `MAIL TRANSPORT ...`; RUN SUMMARY lines (section 3); `out\lastrun\` markers; the daily summary mail.
- Not verified here: that the last slots (16:30/16:32/16:34) fire and that "Stop all running tasks at end of repetition duration" is unticked in the task (check one task in the GUI after creating them).
- **A long t6 starves t11 and t4.** All Tableau scripts share one login lock. While t6 runs (budget 25 min, limit 55), t11 and t4 slots find the lock taken, write `skipped=pat_busy`, exit 0 and try 15 minutes later (re-pause and refresh are delayed, never sent twice). After `PAT_BUSY_ALERT_AFTER` (4) skips in a row one alert mail per day says the Tableau login is busy; on a normal long send day that alert is expected, not a hung process. Check that t6 is still running before reacting; raise `PAT_BUSY_ALERT_AFTER` if it is noisy.

**Disable (kill switch)**: fastest is creating `out\KILL` (section 7); the tasks keep running and exit 0 doing nothing. To stop the tasks themselves: `Get-ScheduledTask -TaskPath '\TableauAutomation\' | Disable-ScheduledTask` (and `Enable-ScheduledTask` to resume). Taken-over subscriptions stay paused in Tableau; see section 7 to hand them back.

**Unregister:** `.\scripts\schedule\register_tasks.ps1 -Unregister` (add `-WhatIf` to preview). Removes the five tasks only.

## 16. UAT: mail every subscriber without an allow-list (SEND_ALLOW_ALL_UAT, D-050)

**What it does.** On the UAT Tableau server only, anyone who is a subscriber of a subscription the script has taken over gets the mail. You do not maintain `SEND_ALLOWED_RECIPIENTS` there; it may be empty or missing. If it is filled in, it is not applied while the switch is on.

**Turn it on.** Add this line to `.env` on the host:

    SEND_ALLOW_ALL_UAT=1

**Turn it off.** Delete the line or set it to `0`. The normal rule applies again: real sending needs a non-empty `SEND_ALLOWED_RECIPIENTS`, and recipients not on it are dropped.

**Safety rules built in**
- It only works when `TABLEAU_SERVER_URL` has the host `tableau-uat.example.com` exactly. On any other host (production, a look-alike name, an address with extra text before an `@`) it is ignored, and every script logs a WARNING saying the switch was set but ignored.
- It does not make anything send by itself. Real mail still needs `--execute`, `MAIL_TRANSPORT=smtp` (or `--smtp`) and the SMTP user and password. Takeover is still limited by the workbook or subscription ids, `--owner-user-id` and the new-takeover cap.
- `ALERT_TO` must still be a valid e-mail address (it does not have to be on a list). A malformed subscriber address is dropped and counted as "dropped_by_allow_list" in the log.

**How to see it is on.** Every script that can send mail logs `SEND_ALLOW_ALL_UAT=1 is ON ...` as a WARNING at startup. Each RUN SUMMARY line ends with `allow_all_uat=1`, and the daily summary mail starts with a NOTE line. If the summary line has no marker, the switch is off or was ignored.

**Do not use it in production.** Production keeps the allow-list (an exact list of addresses). Open question for the production preflight: D-052 item 5 says production should also mail whoever is subscribed in Tableau and that this switch is to be extended to the production host; until that is decided and built, the switch only works on the UAT host.

**Log files and retention (D-051).** Each script writes one log file per day, named with the date, and deletes its own log files older than 7 days when it starts (change with `LOG_RETENTION_DAYS` in `.env`, minimum 1). Only files named `<script>_<yyyy-mm-dd>.log` of that script in `out/` are removed, plus the old `<script>.log` and `<script>.log.1..3` files left from earlier versions once they are older than the window. A file that is in use is skipped. Last-run markers, alert state, the kill file and the database send log are never touched: the send log in the database is what stops a double send and is not pruned.

**Startup warning (checked):** every script that can send mail (t2, t4, t6, t11, daily_summary) logs the `SEND_ALLOW_ALL_UAT=1 is ON` WARNING right after it starts logging, before the kill-switch check, so it also appears in a run that stops at the kill switch. A check starts the real scripts and looks for that line.

## 17. Reports with a file or other-database source (D-053)

**Rule (user):** a report is ready when its warehouse (DWH) parts are ready. Anything that does not come from our DWH (Excel or CSV files, other databases) cannot be checked and is deemed ready.

**How it works.** The daily mapping sync (T2) looks at the connection type of every table Tableau reports for a data source. Only when it can POSITIVELY name every skipped connection as not our DWH (a file type such as Excel or text/CSV, another database engine, or SQL Server in another database) does it set `NonDwhConnection = 1` on that data source (the sync log lists them: "POSITIVELY identified as non-DWH"). A flagged source with no DWH table gets the status `NotChecked`, which counts as ready everywhere a source is tested. A source with both file and DWH tables waits on its DWH tables only, as before.

**What stays blocked (fail closed):** a source with no tables that is not flagged (`NoTables`: the read failed, custom SQL could not be resolved, or the connection type is missing or vague); a flagged source that also has a DWH table missing from the pipeline registry (`UnmappedTable`); an unknown pipeline; an unknown extract/live kind. Custom-SQL sources are never flagged.

**A workbook with only file sources** is sent on its schedule without waiting for anything. Nothing notices a stale file (docs/LIMITATIONS.md).

**Embedded extracts of files are not refreshed by us.** A NotChecked source is never refreshed by the script (t4 has no pipeline to gate on). If the file is inside an embedded Tableau extract, the report goes out with whatever that extract holds, which is stale unless Tableau's own extract schedule refreshes it. Which connection types count as "not our DWH" is an allow-list in `t2_mapping_sync.py` (`POSITIVE_NON_DWH_CTYPES`: Excel, text/CSV, Google Sheets and named database engines such as Oracle or PostgreSQL, and SQL Server in a differently named database). Generic ODBC/JDBC connections, extract files, proxies and anything unknown are NOT evidence and keep the source NotReady. Database names are compared after trimming spaces, brackets and quotes.

**Tested end to end:** `r1_checks.py` runs the real readiness gate with a fake export and fake mail: a workbook with only a file source is sent, and the same workbook with an unknown (no tables, no flag) source is blocked.

**Apply (once per database, dev first):** run `scripts/sql/R8_nondwh_sources.sql` (guards the server name, adds the column, redefines the two readiness views), then `python scripts/t2_mapping_sync.py` once. Rollback: `scripts/sql/R8_nondwh_sources_rollback.sql` (together with the old code). Check: `SELECT DataSource_Name, ReadyStatus FROM Integration.vw_Tableau_DataSourceReadiness WHERE ReadyStatus = 'NotChecked'`.

## 18. One-off sample mail of a subscription (D-054)

Use it to see the real email format today, for example for a subscription the script took over today (the takeover-day guard would stop a normal send).

    python scripts/t6_send.py --sample-send --execute --smtp --subscription-id <subscription id> --sample-to <your address>

- **Exactly one** subscription id and **exactly one** plain address. A list, a comma, a space, `--workbook` or a second id is refused (exit 1, nothing done).
- The mail goes **only** to `--sample-to`. The subscriber's address is not even read from Tableau. The subject is the real subject with the prefix `[SAMPLE] `; the export, PDF, inline images, heading and message are built by the same code as a real send.
- It skips the takeover-day guard, the schedule check, the readiness and freshness gates and the taken-over requirement. It still needs: the subscription active in the mapping and present in Tableau, a mapping sync newer than the age limit, the kill switch off, the Tableau token free (otherwise try again in a few minutes), the size limit, and for a real mail `--execute` plus `--smtp` (or `MAIL_TRANSPORT=smtp`) and SMTP credentials. Without `--execute` it only exports and reports the size; with local transport it writes the local test mail.
- Allow-list: on UAT with `SEND_ALLOW_ALL_UAT=1` the address needs no list entry; anywhere else it must be on `SEND_ALLOWED_RECIPIENTS`.
- **No send-log row is written**, so the real send of the same subscription the same day is not blocked and not affected. The log gets one `RUN SUMMARY sample=1 ...` line, a real mail also writes one information entry to the Windows Event Log. A failure is printed on the console (`SAMPLE FAILED: ...`, exit 1); no alert mail is sent and the normal last-run marker and daily summary are not touched.
- Running it twice sends two mails: that is up to you.

## 20. Extract refresh: automatic registry, Tableau extract schedules, takeover (D-066 to D-079)

**The old refresh job (D-017, decided in D-080).** The old job (the existing script with its own PAT, not a Tableau task) keeps running side by side with this work and refreshes every data source that has a row with `Active_Flag = 1` in `Integration.ETL_Tableau_ExtractMapping`, as soon as ONE ETL has succeeded (the known defect in CLAUDE.md). The takeover now stops it for the extracts it takes over: in the same transaction as the backup row it sets `Active_Flag = 0` on that data source's rows (only rows that were 1), and `--restore` sets exactly those rows back. A data source with no row there changes nothing; if the table is absent nothing happens. A dry run prints which rows would be switched off. The old job itself is not changed. The `Mapping_id` list is in `Tableau_ExtractTaskBackup.OldJobMappingIds`; by hand: `UPDATE Integration.ETL_Tableau_ExtractMapping SET Active_Flag = 1 WHERE Mapping_id IN (<that list>);`. A schedule's enabled or disabled state does not matter: a task on a disabled schedule is taken over like any other, and t4 uses the schedule's days.

**What happens every day without anyone.** `t2_mapping_sync.py` (07:50) now does two more things in the same run. (1) Registry: for each base table the mapped data sources read it finds the pipeline from the lineage tables and the ETL log, and keeps the `Auto` rows of `Integration.Tableau_PipelineTable` in step. (2) Extract tasks: it reads the Tableau extract refresh tasks and their schedules into `Tableau_ExtractTask` and `Tableau_Schedule`. Its `RUN SUMMARY` line ends with `registry_insert= registry_move= registry_remove= registry_manual_differs= registry_ambiguous= registry_no_pipeline= registry_unconfirmed=` and `static_clash=<n>` and shows `extract_tasks=<n>`. It also reads the schedule of every taken-over task, which no live task names any more (D-073); a schedule that was deleted is a problem of the sync (exit 2, mail) because the extract would stop being due.
- **A table in both the static list and the registry (D-081).** There is no database trigger for it any more. Every sync checks `Tableau_StaticTable` against `Tableau_PipelineTable`; a table in both is logged as `STATIC/REGISTRY CLASH` (error), counted in `static_clash=<n>` of the RUN SUMMARY and mailed once a day (`static_registry_clash`). The sync changes nothing and does not stop. Static wins in the readiness views (no freshness check); decide which list the table belongs to and `DELETE` its row from the other one.
- **Preview first:** `python scripts\t2_mapping_sync.py --dry-run` prints `Registry from lineage (dry run, nothing written): ...` and one line per `REGISTRY INSERT / MOVE / REMOVE / MANUAL ROW DIFFERS / AMBIGUOUS / NO PIPELINE`. Nothing is written in a dry run.
- **A table with no pipeline or two candidate pipelines** is reported as `REGISTRY NO PIPELINE` or `REGISTRY AMBIGUOUS` and is not registered, so every extract that reads it shows `UnmappedTable` and is not refreshed (the not-ready alert of t6 reports the affected reports). Fix: add a Manual row (the override): `INSERT INTO Integration.Tableau_PipelineTable (PipelineName, SchemaName, TableName, Evidence) VALUES ('<pipeline as in ETL_Master_Log>', '<schema>', '<table>', '<who decided and why>');` (Source defaults to Manual). A Manual row is never changed or removed by the sync. To hand an Auto row over to a person: `UPDATE Integration.Tableau_PipelineTable SET Source = 'Manual' WHERE SchemaName = ... AND TableName = ...;`
- **An inserted, moved or removed Auto row** is logged at WARNING and mailed once a day (`registry_changed`, one line per change). Look at the lineage table (`Integration.Lineage`, `DW_NovaLineage`, `OAO_Lineage`) and `ETL_Master_Log` for that table. If the change is wrong, make it a Manual row with the right pipeline. More than 5 inserts + moves + removals in one run are not applied (exit 2): something is wrong with the lineage tables. On the very first run only, `t2_mapping_sync.py --registry-first-run` raises the limit to 50 (`REGISTRY_MAX_CHANGES_FIRST`).
- **UNCONFIRMED (no log evidence, D-075).** A table whose pipeline would only be the lineage default, because no `ETL_Master_Log` row of THAT pipeline ended within 10 minutes after any of its recent loads (D-078: log rows of another pipeline do not confirm it), is NOT registered: log `REGISTRY UNCONFIRMED`, `registry_unconfirmed=<n>`, and a mail `registry_unconfirmed` once a day. Its extract stays `UnmappedTable`, is not refreshed and not taken over. When the pipeline has run and the log confirms it, the next daily sync registers it; if you know the pipeline now, add a Manual row (same INSERT as above). Dev note: a pipeline with no log rows at all (no DailyETL row on dev) leaves all its tables unconfirmed.
- **When does t4 refresh?** (a) An extract whose Tableau task the script took over: on the days its Tableau schedule says it is due (time of day ignored, up to 3 days of catch-up, once per occurrence), AND on days a dependent subscription is due. (b) Every other extract: only for a due subscription, as before. Always only after all its required pipelines succeeded today. t4 logs `via=extract schedule` or `via=due subscription` per extract and `due_by_extract_schedule=<n>` in its `RUN SUMMARY`. `--schedule-mode ignore` turns the schedule rule off.

**Taking over the Tableau extract refresh tasks (the script then refreshes instead of Tableau).** Not done by any scheduled task by itself. Never run without the user's go-ahead.
1. Dry run: `python scripts\extract_takeover.py` lists the extracts that could be taken over (those t4 can refresh itself) with their data source ids. Then name one: `python scripts\extract_takeover.py --datasource-ids <data source id>` shows what would be backed up and deleted.
2. Look at the task in Tableau (Tasks > Extract Refreshes): note whether it is a full or incremental refresh and its priority. The REST API does not show this.
3. Real run for one extract: `python scripts\extract_takeover.py --datasource-ids <data source id> --owner-user-id <Tableau user id of the data source owner> --execute`. It backs up the task (table `Tableau_ExtractTaskBackup`, file `out\extract_backup\*.json`), deletes exactly that one task in Tableau, reads Tableau again to check, and confirms. At most 5 new takeovers per run (`--max-new`). A failure leaves the backup row unconfirmed and the next run finishes or retries it.
4. Next day: `t4_refresh.py` dry run shows the extract `via=extract schedule` on its schedule days.
5. **Restore** (gives the extract back to Tableau): `python scripts\extract_takeover.py --restore <original task id> --execute` (or `--restore-all`). It re-creates the task on the same schedule (or adopts a task for the same data source AND the same schedule that someone already created; a task for it on another schedule is not adopted, only warned about) and only then marks the backup row restored. The old refresh job's rows are switched back to their original value in the same step. Lost on restore: the task id (new one), the task's own priority, the failure count and the last run time. If it fails (`restore_failed` alert: schedule deleted, data source gone, Tableau refused), the backup row stays open: create the task by hand in Tableau from the JSON file, then run `--restore` again (it adopts the task).
   **After a restore (D-076):** the restored backup row gets `NoRetake = 1`, so the daily 08:20 run does not take the extract over again even while its id is still in the allow-list. **Remove the data source id from the allow-list** (`--datasource-ids` / `EXTRACT_TAKEOVER_DATASOURCE_IDS` in the scheduled task) all the same. To allow a new takeover later: `UPDATE Integration.Tableau_ExtractTaskBackup SET NoRetake = 0 WHERE Backup_id = <id>;` and put the id back on the list.
6. **Held back for a day (D-075):** an extract with a table whose pipeline row is an Auto row younger than 1 day is skipped (`skip_young_auto`), so you can read the `registry_changed` mail first; the next daily run tries again. A Manual row never holds it back.
7. **Daily alert for orphans (D-074, D-078).** The daily run mails once a day, with an empty allow-list and also as a dry run (the 08:20 task is a dry run until it is enabled; the mail uses the same transport as the other monitors: the local test sink unless `MAIL_TRANSPORT=smtp`), and writes one Windows Event Log entry, for every extract whose task the script took over and that nothing refreshes: `takeover_orphaned` (no longer a t4 candidate: left the mapping, registry row removed, `UnknownPipeline`), `takeover_schedule` (its schedule is stale or not understood, so it is never due), `takeover_missed` (the last due occurrence has no successful refresh). Fix the cause or give the task back with `--restore`. An orphan makes the run exit 1 and counts in `needs_human`. t4 lists these extracts in its log (`TAKEN OVER but not a candidate now`) and its `RUN SUMMARY` shows `taken_over=` and `taken_over_excluded=`.
7b. **Removing an id from the allow-list does not stop an unfinished takeover.** If a takeover started (backup row written, old job switched off) but the Tableau delete did not finish, every run keeps retrying the delete whatever the allow-list says. Only `--restore <task id> --execute` stops it (it gives the extract back and switches the old job back on).
8. A retried delete (the backup row is open and the task is still in Tableau) reads Tableau again first: if the task's data source or schedule differs from the backup it is NOT deleted (`takeover_changed` alert, exit 1); look at the task, then restore or close the backup row. A transient empty task list from Tableau raises a partial-read error and confirms nothing.
9. To register the daily step: `register_tasks.ps1` registers `extract_takeover` at 08:20 as a dry run; a real run needs `-Live -ExtractExtraArgs '--datasource-ids <id> --owner-user-id <id>'`.
10. **Close a backup row by hand** (the task was re-created in Tableau by a person, or the extract is not to be managed by the script any more and `--restore` cannot run, for example the data source is gone). Check first that the row is the right one: `SELECT Backup_id, Task_id, TargetName, NeutralisedAt, RestoredAt, NoRetake FROM Integration.Tableau_ExtractTaskBackup WHERE RestoredAt IS NULL;` Then:
```sql
UPDATE Integration.Tableau_ExtractTaskBackup
SET RestoredAt = SYSDATETIMEOFFSET(), NoRetake = 1, Note = 'closed by hand <date>, <your name>: <why>'
WHERE Backup_id = <n> AND RestoredAt IS NULL;
```
`NoRetake = 1` keeps the daily run from taking the extract over again; remove the data source id from the allow-list as well. The row stays as history (never delete it). This does not touch Tableau: make sure a refresh task exists there (or the extract is meant to have none).
   **Also switch the old refresh job back on** if the row has `OldJobMappingIds` (`--restore` does this by itself; closing by hand does not):
```sql
SELECT Backup_id, OldJobMappingIds FROM Integration.Tableau_ExtractTaskBackup WHERE OldJobMappingIds IS NOT NULL AND OldJobRestoredAt IS NULL;
UPDATE Integration.ETL_Tableau_ExtractMapping SET Active_Flag = 1 WHERE Mapping_id IN (<the numbers of OldJobMappingIds>);
UPDATE Integration.Tableau_ExtractTaskBackup SET OldJobRestoredAt = SYSDATETIMEOFFSET() WHERE Backup_id = <n>;
```
11. **Apply the SQL before the code.** `extract_takeover.py` refuses to start (fatal, exit 1) unless the tables `Tableau_ExtractTask` and `Tableau_ExtractTaskBackup` AND the columns `Tableau_ExtractTaskBackup.NoRetake` and `OldJobMappingIds` exist, and `t2_mapping_sync.py` checks its schema at start. So dropping the `NoRetake` or `OldJobMappingIds` column alone (for example by hand) makes `extract_takeover.py` refuse to start, **including `--restore` and `--restore-all`**: put the column back (from the DBJR document, ITSC_384043_TableauEnhancement_ML; the creation script is no longer in this repo) before restoring anything. Order for a deployment: SQL first, then the code; for a rollback: code first (stop the tasks), then the SQL.
- Schedules the script does not understand, workbook tasks and extracts that are not in the mapping are never taken over (Tableau keeps refreshing them).
- Roll the whole feature back: DB_CHANGES.md section 12.4 (restore every takeover first).

## 21. How to switch the extract takeover on

Plain version of section 20. Do this only with the owner's go-ahead.

1. **Look first (nothing is written):** `python scripts\extract_takeover.py --datasource-ids <data source id> --owner-user-id <Tableau user id of the owner>`. It prints what would be backed up and deleted, and which rows of the old refresh job's list (`ETL_Tableau_ExtractMapping`) would be switched off.
2. **Register the daily step for real:** `scripts\schedule\register_tasks.ps1 -Live -ExtractExtraArgs '--datasource-ids <luid> --owner-user-id <id>'`. Several data sources: comma separated luids. Without `-ExtractExtraArgs` the 08:20 task stays a dry run, even with `-Live`. At most 5 new takeovers per run.
3. **What it does for each extract:** saves the Tableau refresh task (database and `out\extract_backup`), switches the old refresh job off for that data source, deletes the Tableau task, and from then on `t4_refresh.py` refreshes the extract on the schedule's days (an enabled or disabled schedule makes no difference), after all its ETLs succeeded.
4. **Give one back:** `python scripts\extract_takeover.py --restore <original task id> --execute`. It puts the task back on its schedule and switches the old refresh job's rows back to what they were. It will not be taken over again until you clear `NoRetake` (section 20, item 5); also remove its id from `--datasource-ids`.
5. **Give everything back:** `python scripts\extract_takeover.py --restore-all --execute`.
6. **Kill switch (stop taking over, keep what was taken):** create the empty file `out\KILL` or set `TABLEAU_AUTOMATION_DISABLED=1` (section 7). The takeover run then does nothing and exits 0. Extracts already taken over stay as they are (the old job stays off, Tableau has no task) and `t4_refresh.py` is stopped by the same switch, so nothing refreshes them until the switch is removed. If it stays on for more than a day, run `--restore-all` first: `--restore` and `--restore-all` work even with the kill switch on.
