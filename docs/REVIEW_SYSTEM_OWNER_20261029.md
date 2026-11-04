# System-owner review, 2026-10-29

Reviewer's stance: owner of the Tableau Server that the scripts will be installed on. Read-only review. Nothing was run against Tableau, the databases or the mail server, and no `.env` was read. The offline check scripts were deliberately not run, because they write fixture rows into the dev warehouse (`t4_checks`, `t6_checks`, `r1_checks` use dev tables) and the brief says no database writes. Everything below comes from reading the code, the docs, the UAT results and the UAT log files in `out\`. Where a statement rests on my recall of Tableau behaviour rather than on this repository, it says "recall, verify".

File references are `file:line` inside `scripts\` unless a docs path is given.

---

## Verdict

**Not ready for a production pilot that reaches real recipients. Ready for the read-only preflight and for an own-mailbox pilot (one workbook, one subscription whose owner is you) once the first four must-fix items are settled.** The core flow is sound and well fenced: the freshness chain (every required ETL succeeded today, then an extract refresh submitted after the last ETL, then a no-cache export) is correct as far as it goes, the send log is genuinely at-most-once, takeover is backed up before it is applied, and every failure path fails closed. The weaknesses that matter to a Tableau owner are different: (1) the "wait until Tableau has no other job" rule has no deadline and can keep reports from going out at all on a busy production backgrounder; (2) a taken-over report that cannot go out (no ETL that day, busy server, republished workbook, blocked mapping) goes out to nobody and the recipient is never told, whereas Tableau always sent something; (3) the report is rendered as the token's user, not as the subscriber, and nothing checks that the subscriber is still licensed or still allowed to see it, while more than half of the in-scope subscriptions on UAT are ones Tableau is not sending today and the script would start sending; (4) all monitoring runs on the same host, task scheduler, service account and mail path as the thing being monitored, so the failures most likely in production (expired service-account password, disabled task, host down) are silent; and (5) a partly loaded warehouse (ETL rerun in progress) is invisible to the gate. None of these is a rewrite; each has a small fix, listed below.

---

## Must fix before any production pilot

### 1. The "wait until Tableau is idle" rule has no deadline and can starve our own reports on a busy server
- **What:** Before it submits any refresh, and before it exports any report, the script looks at the background-job list. If anyone else has a job Pending or InProgress (any type: their refreshes, Tableau's own subscription mails, flows), nothing happens this run. The only escape is a Pending job older than 6 hours. Consequences:
  - There is no escalation. t4 never alerts on waiting. t6 only lists the report in the "still not ready" alert after 15:30. A server whose two backgrounder slots are rarely both empty between 08:30 and 15:30 (hourly extract schedules, the 06:00 to 09:00 subscription mail wave, Tableau's own "when data refreshes" mails) would send nothing, and nobody learns until the afternoon.
  - InProgress jobs are never treated as stale. One hung InProgress job blocks us until Tableau cancels it, up to the 24-hour limit of the job list.
  - Our own refresh triggers other people's work: every extract we refresh fires the native "when data refreshes" subscriptions of single-source workbooks that share it. Those jobs are queued by Tableau, are not in our own-job list, and make the next run wait.
  - The list is `Query Jobs` for the **one site** the token signs in to (`jobs.get` in tableauserverclient is site-level). Jobs of other sites on the same backgrounder are invisible, so on a multi-site server the rule is both too strict and too blind.
  - It is evaluated once per run in t6 (`ctx.busy`, cached) and the job list is cached 60 s, so a 25-minute export phase is not re-checked.
  - It protects the backgrounder only. Export rendering uses VizQL, which users share, and that load is not measured by this rule.
- **Who is hurt:** every recipient of a taken-over report (reports late or never), and indirectly you, because the check was added to protect other users yet starves our own side first.
- **Evidence:** t4_refresh.py:335-359 (rule, stale handling only for Pending), t4_refresh.py:534-547 (applied before refresh), t6_send.py:721-740 (once per run, `ctx.busy`), t6_send.py:1112-1114 (only alert is after the cutoff), t4_refresh.py:325 and 344 (24 h list), t4_refresh.py:305-307 (`jobs.get`, site-level), docs\RUNBOOK.md:224, DECISIONS.md D-057 ("Known gap": check and submit are not atomic). UAT never exercised it live: docs\UAT_TEST_RESULTS_20261027.md:37 ("Proven by offline checks only; Tableau was idle in every live run").
- **Fix:** before the pilot, measure the real idle fraction (Tableau repository `background_jobs`, read-only, by the Tableau admins): for each 15-minute slot between 08:30 and 15:30 over four weeks, how often is anything queued or running. Then make the rule time-bounded: after a configurable time (for example 11:00) block only on jobs that touch the same data sources or only when both slots are busy, treat an InProgress job older than N hours as stale, and send an alert after 60 minutes of continuous waiting (t4 included). Ask the admins whether there is more than one site and whether the token's role sees all jobs (see "Cannot be judged").

### 2. A report that cannot go out goes to nobody, and recipients are never told
- **What:** Today Tableau sends the scheduled mail at the scheduled time whatever the data state (stale included). After takeover the report goes out only when the gate is satisfied, and otherwise silently not at all. Cases: no ETL that day (weekend, holiday, Sunday/Monday pipelines), ETL failed or the log row is missing, extract refresh failed three times, Tableau busy (item 1), workbook republished after 07:50 (waits for next day's sync), mapping gap after the workbook was edited, Claimed row left behind (item 9). The only person told is the alert recipient, once, after 15:30; the people who expect the report get nothing, no "delayed" notice and no "not sent today" notice. For a daily weekday schedule the catch-up window is one day, so a missed day is lost for good.
- Arrival time also changes: nothing is ever sent before the first t6 run at 08:34, so a 06:00 subscription (`0600AM_Weekday mornings` is on UAT, seen in out\t6_send_2026-10-27.log) now arrives after 08:34 at the earliest; and time of day is otherwise ignored, so a 15:00 subscription is sent as soon as the data is ready in the morning.
- **Who is hurt:** every subscriber of every taken-over report. This is the largest behaviour change in the project and it needs to be a conscious, communicated one.
- **Evidence:** schedule_due.py:148-153 (window ends the day before the next occurrence), schedule_due.py:11-15 (time of day ignored), t6_send.py:832-839 (not ready returns `not_ready`, no mail), t6_send.py:1112-1114 (alert only to the alert address, only after the cutoff), t6_send.py:827-831 (mapping stale), t6_send.py:860-868 (awaiting sync), docs\LIMITATIONS.md:28 and 39.
- **Fix:** (a) decide per report class whether "late or nothing" is acceptable, and tell the subscribers before go-live (one message, includes the new send-time rule); (b) at the cutoff, mail the *subscriber* a one-line "today's report is delayed, data not ready" (or at least a team-visible status page / distribution list), with a per-subscription switch; (c) add the Sunday/Monday carry-forward from the proposal (reviewed in appendix C) so predictable no-ETL days do not become missed days.

### 3. Reports are rendered as the token's user, and nothing checks the subscriber's licence or access
- **What:** The export runs under the personal access token, not as the subscriber. Tableau renders a native subscription as the subscriber, with the subscriber's permissions and any row-level filters (`USERNAME()`, user filters, `ISMEMBEROF`). The script does none of that: it uses the subscriber's address only as the mail recipient. Three consequences:
  - A report with per-user content is sent with the token user's view. The only per-viewer content found so far is the `ISMEMBEROF` filter in the two NOVA Multi-Asset reports (docs\LIMITATIONS.md:5); the production inventory has not been checked for others.
  - A subscriber who has since lost the right to see the workbook, or whose account is set to Unlicensed (the usual way a leaver is handled, instead of deletion), keeps receiving the report. Tableau stops mailing those (recall, verify). The script reads only the address (`user.email`) and never the site role or any permission.
  - The token's own role has to be site/server administrator to do what the scripts do (list all subscriptions, read other users' addresses, update subscriptions, list jobs: recall, verify), which makes the stored token a high-privilege credential on a host that already holds everything else (item 7).
- **Who is hurt:** anyone whose data entitlement is narrower than the token user's; the data owner (information leak); leavers' mailboxes.
- **Evidence:** t6_send.py:343-364 (`describe` reads only email), t6_send.py:874-893 (recipient test is "has an address"), no `site_role` anywhere in `scripts\` (grep), docs\LIMITATIONS.md:5, docs\DEPLOYMENT_PLAN.md:100 (token needs read, export, update, refresh, i.e. admin).
- **Fix:** before sending, require `site_role != Unlicensed` and, if the REST permission query allows it for the subscriber, a View capability on the target; skip with an alert otherwise. Have the Tableau admins list, read-only from the repository, every in-scope workbook that uses user filters, `USERNAME()`, `FULLNAME()` or `ISMEMBEROF` (workbook or data source); exclude those workbooks from takeover. Give the token a dedicated service user with the lowest role that works (Site Administrator Explorer, not Creator) and no personal content.

### 4. More than half of the in-scope subscriptions are ones Tableau is not sending today, and takeover turns them on
- **What:** On UAT, 30 of 54 subscriptions on the 38 in-scope workbooks are suspended (STATUS.md:46; UAT is a copy of production). Suspended means Tableau does not mail them: a person paused them, a person left, or Tableau auto-suspended them after repeated failures. By user decision "suspended is not a special category": the script takes over every in-scope subscription that still exists and sends all of them. On go-live, roughly half the recipients start getting a report they have not had for weeks or months, possibly the wrong people (leavers, see item 3). The only safeguard is a manual clean-up of `out\review_list.csv` before go-live.
- **Who is hurt:** recipients who paused on purpose; the data owner if the person should no longer see the data.
- **Evidence:** DECISIONS.md D-046 (line 54), docs\LIMITATIONS.md:11 and 36, t11_pause.py:425 (already suspended: backup row only, PausedAt set, then sent), docs\RUNBOOK.md:274-283.
- **Fix:** keep the user's rule but make the clean-up a hard gate, not advice: the live t11 task should not take over a subscription that was suspended before takeover unless its id appears in an explicit "confirmed" list produced from the review file (default: leave previously suspended ones suspended and unsent). If that is rejected, at least send the first mail of each previously suspended subscription to the alert address only.

### 5. Monitoring dies together with the thing it monitors
- **What:** The watchdog, the daily summary, the dead-man check and the alert mails all run as scheduled tasks under the same service account on the same host, read markers on the same disk and send through the same mail path. The failures most likely in production are exactly those that stop all of them at once: service-account password expiry (a normal account is the fallback in the plan), a disabled or deleted task, Windows Update reboot, host down, outbound SMTP blocked. Result: the taken-over reports simply stop (the old subscriptions are suspended) and nobody is told. The plan itself says the dead-man check "lives in the same mail path" and asks for a second monitor, but none is built. Also, the Event Log write failed with `eventcreate exit 1` on the dev host on 2026-10-27 (out\t4_refresh_2026-10-27.log, out\t6_send_2026-10-27.log); the source is not registered yet, and it is untested whether a non-administrator service account can write under it (recall: `eventcreate` generally needs an elevated account).
- t4 never sends an alert of its own. A refresh that fails three times is visible only in the exit code, the Event Log, the 17:30 summary, or the t6 "not ready" alert at 15:30 or later, which is too late to repair an extract and still meet the day.
- **Who is hurt:** every recipient, for days, on the day the service account password expires.
- **Evidence:** watchdog.py:42-73 (file-based, same host), docs\RUNBOOK.md:43-46, docs\DEPLOYMENT_PLAN.md:251, scripts\schedule\register_tasks.ps1:73-80 and 239-247 (one account, normal-account password stored in Task Scheduler), t4_refresh.py (no `deliver_alerts` anywhere; grep), ops.py:242-258.
- **Fix:** add one monitor that does not share fate: a SQL Agent job on the DWH (different server) that mails when `Tableau_SubscriptionSendLog` has no row and no heartbeat row from t6 by 10:00 on a business day, plus a heartbeat row written by each task run. Use the gMSA, not a password account. Have t4 raise an alert at the third failed refresh of an extract (not at 15:30). Register the event source as administrator and test a write from the service account.

### 6. Rollback is real for one subscription and unproven at scale
- **What:** There is no single "give everything back" command: `--restore-all-for-workbook` takes one workbook, so a full rollback of 40 workbooks is 40 invocations, each with a Tableau sign-in, and `--restore` refuses any subscription that was sent today unless `--allow-restore-after-send` is given (which risks a second mail). The pause and the resume are both done with the library's update call, which does not send the subscription's `message` field; whether Tableau keeps a custom message is unverified (no tested subscription had one). If the message is lost the script only notices *after* the change (the pause has already been applied, the run halts) and both the resume and the later mails would then lack the message; the backup row keeps the text. A suspended subscription also looks abandoned to a Tableau administrator doing hygiene, and a deleted subscription cannot be restored by the script.
- **Who is hurt:** everyone, in the one scenario in which you need the rollback to work first time under pressure.
- **Evidence:** t11_pause.py:148-162 (`set_suspended`), t11_pause.py:433-441 (diff after the write), t11_pause.py:657-729 (restore, per subscription or per workbook), t11_pause.py:684-691 (refusal after a send), docs\LIMITATIONS.md:34, docs\DEPLOYMENT_PLAN.md:276, docs\UAT_TEST_RESULTS_20261027.md:63.
- **Fix:** (a) in the own-mailbox pilot, pause and restore a subscription that *has* a custom message and compare it in the UI (and also one with "don't send if view is empty"); (b) add `--restore-all` (every open takeover row) and test it with 10+ rows; (c) agree with the Tableau admins a manual procedure and a list of what the script owns (`Tableau_SubscriptionBackup`), and tell them not to delete suspended subscriptions on that list; (d) store the message and compare before the write, not after.

### 7. Security baseline on a shared production host is not in place
- **What:** the token and the SMTP password sit in clear text in `.env` on the Tableau host (docs\DEPLOYMENT_PLAN.md:82-92), protected only by a file ACL; the local Administrators group on a Tableau host is usually large. The token is an administrator credential (item 3). Nothing in the repository separates the token used by the production tasks from the one used by tests and tools: `d1_inventory.py`, `review_list.py`, `preview_export.py` and the test suites all sign in with the token from `.env`, and a second sign-in with the same token ends the first session. Anyone who copies the production `.env` to a laptop for a one-off script will randomly kill a running export. The service account and its rights on the production host are not yet requested; `eventcreate` and "Log on as a batch job" depend on the host's local policy.
- **Evidence:** docs\DEPLOYMENT_PLAN.md:82-92, 100, 152; STATUS.md:81 ("Used the existing PAT from .env"); docs\RUNBOOK.md:186-189; ops.py:499-524 (the lock is a database lock, so it protects only our own scripts).
- **Fix:** create a new token for production that is never copied off the host and a different token for every human or test use; restrict `.env` to the service account (and not "Administrators: Full" if the host policy allows); consider DPAPI or Windows Credential Manager for the two secrets; calendar the token expiry and note that a token unused for a long time (kill switch for weeks) can expire (recall: default 15 days of inactivity, verify the server setting) and the first run after a long pause then fails to sign in.

### 8. A warehouse load in progress is invisible to the gate, and a corrected load never reaches the recipients
- **What:** The gate reads only completed rows of `Integration.ETL_Master_Log` (latest row of the day per pipeline). An ETL rerun that is still loading, or one that crashed without writing a row, is invisible: the earlier success of the day still counts, so the extract refresh (or a live query) can read half-loaded tables and the report goes out with partial numbers. t6 also reads the log, the refresh rows and the mapping once at the start of the run (`Gate(cur, today)`), not before each claim, so a rerun finishing during the 25-minute send phase is not noticed. And once a report is `Sent`, nothing can resend it the same day: the row cannot be changed or deleted (trigger), so after a corrected rerun the recipients keep the first (wrong) version and the operator has no supported resend.
- **Who is hurt:** recipients who act on the numbers; the ETL team when a rerun is wasted.
- **Evidence:** t4_refresh.py:21-23 (documented limit), t6_send.py:441-462 and 1503 (snapshot at start), t6_send.py:556-559 (refresh after last ETL), sql\R3_sendlog_guard.sql:23-26 (Sent cannot be deleted), docs\LIMITATIONS.md:18.
- **Fix:** (a) add a "pipeline's Agent job is running now" check (SQL Agent job activity, read-only) as a wait condition in both t4 and t6; (b) re-read the ETL rows and refresh rows right before each claim; (c) add an operator command to supersede a `Sent` row (new slot with a reason) for the rare corrected resend, and put "data as of HH:MM" in the mail body so recipients can tell versions apart. Ask the ETL team how often a same-day rerun happens (preflight query on `ETL_Master_Log`: more than one row per pipeline per day).

---

## Should fix

### 9. A killed or rebooted run blocks that report for the whole day
The slot is claimed *before* the export (t6_send.py:931-940). If the host reboots (patching), the task hits its time limit (55 min; a heavy export holds a run for up to 25 min per attempt, `Daily T+0 Report` timed out twice at 300 s on UAT), or the process is killed, the row stays Claimed, then TimedOut after 90 minutes, and blocks the day until a person releases it. The heaviest reports are the most exposed. Fix: add a second state set immediately before the SMTP DATA command (for example Exporting/Sending); a row still in the earlier state after the timeout is provably unsent and can be released automatically. The same applies to a 55-minute task kill.

### 10. Extract refresh: always full, alphabetical order, strictly serial
- Full refresh only: `datasources.refresh(ds)` (t4_refresh.py:465) calls the library with `incremental=False` (default, confirmed in the installed library, `datasources_endpoint.py:450`). An extract the owners have scheduled as incremental becomes a full rebuild every day. The large fact extracts take far longer, load the warehouse more, and are exposed to the server's job time limit (recall: 2 hours by default) which turns into a Failed attempt, three of which stop the day.
- Order is alphabetical (`sorted(targets, key=name)`, t4_refresh.py:531), not by earliest schedule time or expected duration. With one concurrent refresh (default) a heavy extract named early blocks every other extract behind it.
- Throughput: each run waits at most 10 minutes (`T4_MAX_LOCK_MIN`), later runs only start the next extract at the next 15-minute slot. N extracts of 12 minutes each cost about N x 15 minutes. With the 15:30 cutoff, a late ETL plus a handful of extracts can miss the day. D-059 correctly counts our own still-running jobs, which is what makes this serial.
- Fix: use the stored refresh history to estimate durations; order by earliest due report then shortest first; add an early alert if the projected finish passes 14:30; allow `incremental` where the owners use it today (read from the extract refresh task).

### 11. Republished workbooks lose the day
A workbook whose `updated_at` is newer than the value the 07:50 sync stored is not sent until the next sync (t6_send.py:860-868). For a daily schedule that is a lost day, quietly (no alert, only a count in the daily summary; the "never sent" alert comes the next morning). Authors republish in the morning. Also an unresolved question (docs\LIMITATIONS.md:59): if an embedded-extract refresh moves `updated_at`, such a workbook may wait every day. Fix: when a single workbook is found changed, re-run the mapping for that one workbook on demand (T2 can take a workbook filter) instead of waiting a day, and alert after a second skipped day.

### 12. "Don't send if the view is empty" is not honoured; hourly and repeated subscriptions are sent once
The subscription's `sendIfViewEmpty` is read and backed up but never used when sending (t11_pause.py:99, 115 and 308; no use in t6). Tableau withholds an empty report when the subscriber chose that; the script sends it. Separately, an hourly schedule (every 1 to 12 hours) is accepted (schedule_due.py:126-129) but the send log allows one send per subscription per day, so a subscriber who gets four mails a day from Tableau gets one. Two subscriptions of the same person on the same workbook at different times (the plan counts 9 such pairs on dev, D-033 item 4) both fire as soon as the data is ready. Fix: record the "empty" decision (an image dimension or PDF text check is not reliable; ask whether any in-scope subscription uses it), refuse to take over hourly schedules and same-recipient pairs until a rule exists, or document the change to the subscribers.

### 13. One token, one lock: a long send starves refresh and re-pause
All Tableau scripts share one token and one lock, so while t6 exports (up to 55 minutes per run) t4 cannot reconcile or start refreshes and t11 cannot re-pause. Reports then wait for each other. Using one token for t6 and another for t2/t4/t11 removes this and costs nothing (the single-session rule is per token). t6_send.py:1500 and docs\LIMITATIONS.md:27.

### 14. Mail looks different from Tableau's in ways recipients will notice
See the checklist below: the heading and images are not links, high-resolution images fall back to standard resolution above 1 MB (subscription_mail.py:77-82), the fixed 1000 x 1000 render request can change the layout of auto-sized dashboards (subscription_mail.py:22), the file name is the raw workbook name, there is no styling and no footer. Fix after the NOVA before/after comparison shows which of these matter.

### 15. Registry drift is not detected
Which pipeline loads which table is a hand-loaded registry (33 rows). If the warehouse team moves a table to another pipeline or adds a table behind a view, the gate keeps using the old mapping without any signal and reports go out early or wait forever. The design note itself says a runtime check against the lineage tables "can validate guesses". Fix: a nightly report comparing each registered table's last load time (lineage tables) with its pipeline's end time; alert on a table that loaded outside its pipeline's window.

### 16. Time zone and maintenance windows
- Task Scheduler runs in host local time, the scripts reason in Singapore time. If the host is UTC the whole day shifts eight hours; the registration script only warns (register_tasks.ps1:157-159). Make it refuse. (Singapore has no daylight saving, so DST affects only a host in another zone.)
- Schedule start times read from Tableau are interpreted as Singapore time (schedule_due.py:21-22, 166-171, "recall, verify"); if the Tableau server runs in another zone, the takeover-day guard and the "time passed" tests are off by the difference.
- Tableau upgrades, `tsm` restarts and backups: the scripts do not know about them. A restart ends in-flight refresh jobs as Failed or Cancelled and each such failure uses one of the three attempts for the day; sign-in failures during the outage alert once. Fix: create `out\KILL` as part of the maintenance runbook before `tsm stop`/upgrade/backup and delete it afterwards, and do not count a job that ended during a server restart as an attempt (compare the job end time with the server start time).

### 17. First day and bulk takeover
Takeover is capped at 10 per run and runs every 15 minutes (t11_pause.py:596-600), and a subscription with a schedule time earlier than its takeover is skipped for that day (takeover-day guard). A 40-subscription go-live therefore takes an hour, during which later-scheduled subscriptions fire natively with the old, partly stale data. The live task also requires one `--owner-user-id` per owner on its command line (register_tasks.ps1:131-133), so any subscriber not named stays on Tableau, and the new-subscription alert (`new_native`) is the only hint. Fix: do the bulk takeover manually the evening before, and add an "all owners in these workbooks" mode once the pilot is signed off; document how a new subscriber on an allow-listed workbook is handled (today: alert, then manual).

### 18. Alert fatigue on predictable days
Every Sunday and Monday (no ETL) every due subscription is added to the 15:30 "not ready" alert, and a daily Monday schedule produces a "due occurrence never sent" alert on Tuesday. The alert mail is then ignored on the day it matters. Handled by the carry-forward calendar (appendix C) plus suppressing alerts for pipelines the calendar says do not run.

### 19. Security of the mail path
Port 587 with STARTTLS to SES is not what Tableau itself uses on UAT (port 25, no TLS; STATUS.md:86). The Tableau host's egress rules need to allow it (Test-NetConnection is in the deployment plan). SES bounces, complaints and the sandbox are not handled: a bounced address is invisible. Nothing is known yet about the corporate mail gateway's size limit; the script's own limit is 25 MB (t6_send.py:93), SES SMTP accepts up to 40 MB message size (recall, verify) but the recipients' mailboxes and gateway may refuse earlier. The oversize outcome is final for the day with no fallback (t6_send.py:950-953).

---

## Nice to have

- Show the data date in the mail ("Data as of 09:42 SGT, loads: DailyETL, NovaDailyETL"), and a "view in Tableau" link on the heading and images.
- Log and state housekeeping: `out\t11_backup\*.json`, `out\*_state*.json`, and the DWH tables `Tableau_RefreshJob` and `Tableau_SubscriptionSendLog` grow without limit (small; add yearly pruning for refresh rows only, never for the send log).
- A dry-run report of "what we would send today and when" for the Tableau admins and the business, generated from `review_list.py`, so the change in send time is visible before go-live.
- Pillow resizing of a 32,000-pixel-wide crosstab holds a few hundred MB in memory for a moment; fine on the host, but run reports with such sheets last or exclude them.
- Per-report "stale ok on non-ETL days" flag (see appendix C).
- Record who released a Claimed row, in a table, not only in the free-text message.
- Python on the Tableau host: pin the virtual environment in the install folder and exclude it and `out\` from real-time antivirus scanning if the AV policy allows (the atomic write already retries on a locked file, ops.py:99-111).

---

## Cannot be judged from the code (needs a live test or the Tableau admins)

Ask the Tableau admins (read-only, ideally from the repository database `workgroup`):
1. **Subscriptions that the REST list does not show:** how many "when data refreshes" (data-driven) subscriptions exist on the in-scope workbooks, and who owns them. The script ignores them by decision, and they cannot be suspended through the API, so on a taken-over workbook they keep firing natively with the old partial data *in addition* to our mail. They are the original problem this project exists for; confirm whether they are in the 38/266 scope numbers.
2. **Subscriptions on custom views or with saved filters:** the repository `subscriptions` table has a custom-view reference (recall, verify the column). If the REST list shows only the underlying view, our export renders the default view and the subscriber gets a different report. Count them.
3. **The token's role and what it sees:** does `Query Jobs` return all users' jobs for it; can it read other users' emails; does it see every in-scope workbook; is there more than one site; do other sites share the backgrounder.
4. **Idle fraction of the backgrounder** between 08:30 and 15:30 (item 1) and the typical extract durations (item 10).
5. **Server time zone**, and whether schedule start times in the REST schedule are in that zone.
6. **Job time limit and extract refresh timeout** (the server's `backgrounder.querylimit`), and whether any in-scope extract is scheduled incremental today.
7. **Tableau's own maintenance schedule** (backups, cleanup, log archiving, upgrade windows, planned restarts) and whether anything restarts the server in business hours.
8. **Whether a user can resume or edit a suspended subscription in the UI** and what an admin sees in Site Tasks for a suspended subscription (to write the manual rollback procedure).
9. **Whether the PDF/PNG export for a subscriber differs from one done as the token's user** for the in-scope workbooks (row-level security, `USERNAME()`), by exporting one workbook as two different users.
10. **Mail:** the SES account and verified sender, whether the host may reach port 587, the gateway's maximum message size, whether SPF/DKIM/DMARC pass for the sender we use, SES bounce handling.
11. **ETL team:** which days each pipeline runs including public holidays, whether a same-day rerun ever happens, and the exact meaning of the `Success` column (a failure may write no row at all; `ETL_Master_Log` has been seen with only success rows on dev).
12. **Windows:** the service account type (gMSA preferred), "Log on as a batch job" under the host's local policy, whether it can write to the Application event log under a registered source.

Needs a live test:
13. The wait-while-busy rule on a live server (never run live).
14. Pause and restore with a custom message and with "don't send if empty" (message preserved?).
15. A real task registration under the service account, including the last 16:30/16:32/16:34 slots and "stop at end of repetition" being off.
16. A real SES send: login, sandbox, sender, size, the reply text that proves "not queued" (the list in `KNOWN_REJECTIONS` is from recall, t6_send.py:195-202), throttling.
17. Whether a workbook's `updated_at` moves when its embedded extract is refreshed (docs\LIMITATIONS.md:59).
18. Behaviour across a Tableau restart or upgrade with a job in flight, and across the host reboot with a Claimed row.

---

## NOVA Global Story: before and after comparison

### Differences found by reading `scripts\subscription_mail.py` against `docs\preview_notes.md`

| Area | Tableau's own mail (sample) | Our mail | Noticed by a recipient? |
|---|---|---|---|
| Subject | Subscription subject (default: workbook/view name) | Live subject; if empty falls back to stored subject, then workbook label (`workbook / view` for a view target) | Only if the subject was empty or the target is a view |
| Heading | Workbook or view name, very likely a link to Tableau (recall, verify) | Plain `<h1>` text, no link (subscription_mail.py:119) | Yes if the heading is clickable in the original |
| Images | Inline, each probably a link to the view; about 2x resolution (sample images 2100 x 2200) | Inline, not linked; high resolution requested, but if the PNG exceeds 1 MB the script falls back to standard resolution (subscription_mail.py:77-82); render area forced to 1000 x 1000 (line 22); anything wider than 3200 px is shrunk (wide crosstabs become slivers) | Yes for busy dashboards (blurrier), possibly different layout |
| Image order, count, hidden tabs | All visible tabs in tab order | `populate_views` order; hidden tabs not listed (unverified) | Maybe |
| Message text | Under the heading (position to confirm) | `<p>` under the heading, line breaks kept | Unlikely |
| Body lines | "Your Tableau PDF is attached." / "Here's your subscription to the workbook X." plus a line about when the mail is sent (sample is data-driven: "when data is refreshed") | The first two lines; the schedule line was removed on 2026-10-27 | Only if the original has a schedule or "manage / unsubscribe" line. **Check the reference is a time-based subscription, not data-driven** (see below) |
| Footer, "manage subscription", "unsubscribe", branding | Present in recent versions (recall, verify) | None | Possibly |
| PDF file name | `<Workbook name>.pdf` | `<ref_name>.pdf`, raw name, no sanitising (line 132) | Only for names with unusual characters |
| PDF content | Rendered as the subscriber | Rendered as the token's user, no-cache (`maxAge=1`); page size and orientation from the subscription | Only with per-user content |
| Sender | Server's configured sender (`tableau-reports@example.com` in the sample) | `SEND_FROM`; must be a verified SES identity | No, if the same address and domain authentication passes |
| To header | Probably display name plus address | Bare address(es) | No |
| Headers | May carry SES feedback id, tracking pixel (gateway), `X-` headers | Message-ID, Date (host local zone), MIME-Version only; no tracking pixel | No (but check SPF/DKIM/DMARC results and the spam folder) |
| MIME structure | HTML with related images + PDF | plain + HTML alternative, related images, PDF as mixed | Depends on the client (plain-text view) |
| Styling | Styled HTML | Browser default fonts | Yes, slightly |
| Timing | At the schedule time, every day | After the data is ready, never before 08:34, once per day | Yes (and the biggest) |
| "Don't send if empty" | Honoured | Ignored | Only when a view is empty |

### Exact before/after checklist for the NOVA test

Preparation (before takeover):
1. Use a subscription of the **same kind that production will take over**: schedule-based, same schedule type, same formats (PDF and image) and a custom message, owned by you, addressed to your own mailbox. A data-driven subscription is not a valid reference: its wording and schedule line differ.
2. Note in the Tableau UI: subject, message, formats (PDF/image), page size, orientation, "send if view is empty", schedule name and time, state. Take a screenshot.
3. Let Tableau send it once (or use "send now" if the admin has it). Save the mail from the mailbox as `.eml` (Outlook: File > Save As; Gmail: Show original). This is the **before** file. Open it in desktop Outlook, web and phone and screenshot.

After takeover and the first script send (same subscription, same day, a few hours apart is fine):
4. Tableau UI: subscription is suspended; subject, message, formats, page size, orientation, send-if-empty and schedule are exactly as in step 2. Only one mail arrived for the day (no native copy).
5. Save the **after** `.eml`.
6. Structure diff: list the MIME tree of both (`python -I -c "import email,sys;m=email.message_from_binary_file(open(sys.argv[1],'rb'));[print(p.get_content_type(),p.get_filename(),len(p.get_payload(decode=True) or b'')) for p in m.walk()]" file.eml`): parts, content types, inline vs attachment, file names, sizes.
7. Headers: From (name and address), To, Reply-To, Date offset, Subject character for character, Message-ID domain, Authentication-Results for SPF, DKIM and DMARC (all pass?), spam folder placement.
8. Body: heading text and whether it is a link; message text and position; every text line; any footer or link ("manage subscription", "unsubscribe", "powered by"); font and spacing; plain-text part.
9. Images: count, order and names against the workbook's visible tabs; pixel width and height of each (save and inspect); sharpness side by side at 100%; layout of each dashboard (cut-off, scrollbars, different arrangement of floating objects); any image shrunk or fallen back to standard resolution; whether clicking opens Tableau.
10. PDF: file name; page count; page size and orientation; every page against the original page by page (order, cut-off, headers and footers); file size; values in a few filtered worksheets.
11. Data: pick three numbers; compare with the warehouse at the time of the extract refresh; the as-of date in the mail.
12. Per-user content: add a worksheet with `USERNAME()` or a group filter to a test copy, subscribe a second, restricted test user, and compare what the restricted user receives (before) with what arrives (after).
13. Timing and count: time of arrival against the schedule time; the same recipients; no extra recipients; one mail only; a second `t6_send --execute` run sends nothing.
14. Restore: restore the subscription (before the next schedule time and not on the day it was sent, or with `--allow-restore-after-send`), confirm the UI shows the original state, and that the next native mail equals the first "before" mail.

Pass rule: every difference in steps 6 to 10 is either removed or listed in the message to subscribers before go-live.

---

## Appendix A: scenarios where a report that Tableau sends today does not arrive, is late, or is stale after takeover

| Scenario | What the recipient gets | Evidence / note |
|---|---|---|
| No ETL that day (Sunday/Monday pipelines, public holiday, ETL not started) | Nothing (daily) or arrival on a later day (weekly/monthly, up to 3 days) | t6_send.py:556-559, schedule_due.py:148-153 |
| ETL log row missing or late | Nothing until the row appears; alert only after 15:30 | t6_send.py:1112-1114 |
| ETL failed (latest row Success = 0) | Blocked for the day | t6_send.py:535-537 |
| Subscription taken over but the sync/mapping later fails (republished, new table, new data source) | Nothing; the old subscription stays suspended | t6_send.py:827-835, 860-868 |
| Extract never refreshed by us: embedded extract, Excel/CSV/Google Sheets, other database, unmapped override | Sent with whatever the extract holds (stale unless Tableau's own schedule refreshes it); an embedded DWH extract blocks the report and takeover | docs\RUNBOOK.md:363, t4_refresh.py:24-27, 436-439 |
| Published data source shared by several workbooks | Refreshed once for all; the rest wait for the same job; our refresh also fires other workbooks' native data-driven mails | t4_refresh.py:130-141 |
| Extract on Tableau's own schedule | Our refresh is additional; Tableau's schedule may rebuild it again later (harmless) or read a half-loaded warehouse (not seen by the gate) | item 8 |
| Subscription on a custom view or saved filter | Default view of the workbook | Appendix, "Cannot be judged" 2 |
| Per-recipient variants (user filters, group filters) | Token user's view | item 3 |
| Subscriber is a Tableau user with email | Gets the mail | t6_send.py:874 |
| Subscriber's address is not on the allow-list (production uses an exact list, or the open decision) | Nothing, and the old subscription is suspended | t6_send.py:886-893, docs\DEPLOYMENT_PLAN.md:20 |
| External addresses | Tableau only mails Tableau users, so only the address in the user profile; not on the allow-list unless added | D-008 |
| Subscriber removed from Tableau | Subscription disappears, the script drops it silently; closed after 7 days | t11_pause.py:483-497 |
| Subscriber Unlicensed or lost access | Still receives mail | item 3 |
| Schedule unsupported (customised monthly, interval over 24 h, on-the-fly schedule without id) | Never taken over (stays native) or not sent with an alert | schedule_due.py:96-136, t11_pause.py:576-579 |
| Hourly or repeated same-day subscriptions | One mail per day instead of several | item 12 |
| Workbook edited, republished, owner changed, renamed | Waits for the next sync; a daily report loses the day | item 11 |
| Midnight and day boundary | "Today" is the Singapore date from the warehouse clock; an ETL ending 23:58 counts for the previous day; claims use the warehouse clock | t4_refresh.py:106-112, T1 send-log index |
| Daylight saving | Not applicable in Singapore; applies only to the host's zone | item 16 |
| First day of use | Subscriptions whose schedule time already passed at takeover are skipped for that day (the Tableau mail already went); cap of 10 new takeovers per run | t11_pause.py:596-600, schedule_due.py:166-171 |
| Scripts switched off or host down for a day or a week | Nobody receives the taken-over reports. Nothing tells the recipients. Restore needs Python, the token and the warehouse; no manual UI route is documented | docs\RUNBOOK.md:167-182, item 5, item 6 |
| Previously suspended subscriptions | Start receiving | item 4 |

## Appendix B: proof or disproof that a sent report carries post-ETL data

Proven when the following all hold (t6_send.py:515-566): every required pipeline's latest row of the Singapore day is a success; every extract (refresh target, pipelines of all workbooks sharing it) has a Succeeded refresh submitted after the last of those ETL end times; live sources need only the ETL; the export asks for a render no older than one minute (subscription_mail.py:57 and 79; the installed library sends `maxAge` for PDFs and images); and the extract refresh reads the warehouse after its submission, so a refresh submitted after the ETL end sees the loaded data.

Not proven, in order of risk:
1. An ETL rerun in progress or one that crashed without a row (item 8). The extract refresh and the live query both read half-loaded tables.
2. The gate snapshot is taken at the start of the t6 run (t6_send.py:441-462, 1503) and the send phase can last 25 minutes; a rerun that completes in that window is not seen. The time from refresh finished to export started is therefore up to one 15-minute cycle plus the run's own queue.
3. A later ETL after the send: no resend (trigger) and no notice; the recipients keep the earlier version.
4. Pipeline-level, not table-level: a failed table in a pipeline that logged success counts as ready (docs\LIMITATIONS.md:7).
5. Registry drift (item 15).
6. Embedded extracts and non-DWH sources are never refreshed by us (appendix A).
7. Clock: the refresh `SubmittedAt` comes from the warehouse clock and the ETL `EndTime` is written by the load; both are assumed to be the same clock. A skew in the harmless direction only causes one extra refresh.
8. Caches: an extract refresh invalidates the view cache, and `maxAge=1` bypasses it for live connections; the server-side "refresh less often" setting is overridden by `maxAge` (recall, verify on the pilot).

## Appendix C: review of the Sunday/Monday proposal

The proposal (a table of the days each pipeline runs; the gate then checks the last scheduled run) is right in direction. Flaws and fixes:
1. **It moves the gate from fail-closed to trust-the-table.** Today "no run today" means wait. With the table, "no run expected today" means use the last run. A wrong or stale table silently sends old data. Mitigation: alert when a pipeline runs on a day the table says it does not (or does not run on a day it should), and keep "expected but missing" as wait.
2. **A hand-kept table drifts** (holidays, schedule changes, special month-end runs). Prefer reading the SQL Agent job schedules (`msdb`, read-only) or deriving expected days from the last eight weeks of `ETL_Master_Log` (a weekday that ran in at least 3 of the last 4 weeks is expected); keep a small override table for holidays.
3. **"Last scheduled run" needs a maximum age.** Last success must be no older than the longest normal gap (here Saturday to Tuesday), or a pipeline stopped for a week passes the gate on its old success.
4. **The latest row still decides.** A failed Sunday row (the job ran and failed) must block, even though Sunday is not an expected day; read rows from the whole lookback, not only "today".
5. **Refresh freshness also needs a look-back.** Both t4 and t6 count only refresh rows from "today" (t4_refresh.py:272, t6_send.py:459-462). On Monday the last ETL is Saturday's; the Saturday refresh is not "today", so t4 refreshes again on Monday (harmless but wasteful) and t6 will not accept the Saturday refresh. Compare the refresh submission time with the last ETL end time without the date filter.
6. **Mixed pipelines:** a workbook with one pipeline running Monday to Friday and one Tuesday to Saturday needs the rule per pipeline and per day, not per workbook.
7. **Content meaning:** a "T+0" or "daily" report sent on Sunday with Saturday's data may mislead; make carry-forward a per-report opt-in and put "data as of" in the mail.
8. **Catch-up window interaction:** with carry-forward, a monthly report due on Sunday goes out on Sunday with Saturday's data; keep the 3-day catch-up as the fallback and decide whether a report sent on its day with carry-forward may be sent again when the next real run arrives (default no).
9. **Alert noise:** suppress the "not ready after 15:30" line for pipelines the calendar says do not run.
10. **Test:** add cases for Sunday, Monday, a holiday on a run day, a pipeline that runs on a no-run day, a failed Sunday run.

## Appendix D: untested items ranked by harm to Tableau users

1. Wait-while-busy on a live production server (item 1).
2. Pause and restore of a subscription with a custom message and "don't send if empty"; restore of 10+ rows (item 6).
3. Rendering as another user: a restricted subscriber, Unlicensed subscriber (item 3).
4. A real task registration under the service account, run over a full day, including a reboot mid-run (item 9) and password-expiry behaviour (item 5).
5. A real SES send including the reply that proves "not queued" (`KNOWN_REJECTIONS` from recall) and the size limit.
6. ETL rerun in progress while t4/t6 run (item 8).
7. Whole-workbook delete and edits after takeover (workbook republished with a new data source) (listed as not tested).
8. The takeover cap of 10 and the owner fence under a bulk takeover (item 17).
9. Mail server down (breaker and preflight tested against a stub only).
10. Tableau restart or upgrade with jobs in flight (item 16).
11. Restricted database permissions (every run used a full-rights login) and the production warehouse guard change.
12. Token expiry after a long pause (item 7).
13. A Tableau site other than the default, and a non-admin token (item 1, "Cannot be judged" 3).
14. Embedded-extract effect on `updated_at` (item 11).
