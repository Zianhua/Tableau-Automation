# Full end-to-end review (independent, static)

Reviewer: independent senior review, 2026-10-23. STATIC only: I read code, SQL and docs. I ran nothing, did not use the PAT, wrote to no database, sent no mail. I did not open `.env` values (key names only: TABLEAU_SERVER_URL, TABLEAU_SITE_ID, TABLEAU_PAT_NAME, TABLEAU_PAT_SECRET, CONN_DRIVER, CONN_SERVER, CONN_DATABASE; no SES keys yet). T11a (`scripts/t11_pause.py` and its change to `t6_send.py`) is in progress and was NOT reviewed; interaction risks are listed in F-03 and F-11 only.

Read: CLAUDE.md, STATUS.md, BACKLOG.md, DECISIONS.md (D-001..D-034), docs/LIMITATIONS.md, PLAN.md, PLAN_REVIEW.md, d1b_findings.md (summary, sections 1, 5, 9), customsql_mapping_proposal.md, preview_notes.md, edge_cases.md, T1_mapping_schema.sql, T3_load_registry.sql (head), rollback (head), t2_mapping_sync.py, t4_refresh.py, t6_send.py, subscription_mail.py, the check files (test names only, not every line), the old `main.py`, the deployed scheduler XML and `run_extract_refresh.bat`.

Line numbers refer to the files as read today. Statements about Tableau/SES/SQL Server behaviour that come from my memory, not from this repo, are marked "(recall, verify)".

---

## 1. Plain-English summary

The build is careful in the places the team thought about: claim-first at-most-once sends, fail-closed mapping, a gate that needs an ETL success AND a later extract refresh, alert de-duplication, lots of fake-based tests. The weaknesses are in what the design never asked.

The five things that matter most:

1. **The script ignores the Tableau schedule.** Every active subscription is sent once per day as soon as its data is ready. But the 54 subscriptions on UAT (a prod backup) are time-based with 9 real schedules: 22 are "1st day of every month", 16 are Tue-Fri, 10 are Saturday, others Monday or 06:00 weekday mornings (d1b section 5). As built, a monthly report goes out every day, a Saturday report does not go out if no ETL runs on Saturday, and a 06:00 report moves to ~10:00. No decision records this. This is the single most likely cause of an angry-recipient day one.
2. **There is no "taken over" switch.** Both scripts act on every active, non-suspended subscription they know about. The only pilot fence is a recipient allow-list, which fences people, not subscriptions: allow-listing the pilot owner would also send all of that owner's other subscriptions (one owner has 27). And the planned auto-pause (T11a) will interact badly with the "suspended = ignore" rule that t4 and t6 both apply.
3. **Silent-failure paths remain.** Alerts travel by the same SES path that may be broken, nothing checks that the job ran at all, T2 (the mapping sync) is unscheduled, unlogged and aborts the whole run on one odd item, and one missed T2 run blocks every send within about 26 hours.
4. **The first real SES send is likely to go badly in a specific way.** A 5xx rejection after the message body (typical for an unverified sender or sandbox recipient; recall, verify) is treated as "ambiguous", so each subscription stays Claimed all day and a human must release each row by SQL.
5. **Production is a code change plus a re-discovery, not a config change.** The dev DWH guard is hard-coded in three places, view definitions/pipeline names/ETL semantics have only ever been seen on dev, all Tableau ids differ, and hidden "when data refreshes" subscriptions (proven to exist on prod by the sample email in preview_notes.md) are invisible to the mapping, will not be paused, and will fire on the script's own refreshes.

Nothing here says the design is wrong. It says the next step should be a small amount of design work (schedule, takeover flag, alert channel, prod preflight) before any `--execute` on prod.

---

## 2. Ranked findings

Severity: Critical = wrong or lost business email on day one, or silent total failure; High = likely incident or blocks go-live; Medium = ops pain or edge-case wrong output; Low = hygiene.

### Critical

**F-01. No schedule awareness: cadence is "daily when ready", not the subscription's own schedule** (Critical)
- Where: `t6_send.py:287-304` (SUBS_SQL has no schedule filter), `process()` 491-611 (no due-date check), `subscription_mail.py:115` (schedule name used only as email text). T2 stores only the schedule NAME (`t2_mapping_sync.py:328-338`, D-028 item 3). Evidence of real schedules: `d1b_findings.md` section 5 (1100AM_1stDayEveryMonth x22, 1015AM_Tue-Fri x16, 1030AM_Weekly Sat x10, 0900/0930AM Monday, 0600AM_Weekday mornings x2, 0930AM_Mon-Sat). No decision or LIMITATIONS entry covers it (D-014 keeps owner, subject, target, not schedule).
- Failure scenarios: (a) the 22 monthly subscriptions go out every day. (b) A Saturday subscription: `Gate` needs "ETL ran today" (ETL_SQL, t4:149-160); if no ETL runs on Saturday it never sends, and the 15:30 cutoff alert fires every Saturday. (c) The scheduler trigger is daily including weekends and Singapore public holidays, so every non-ETL day ends with a "still not ready" alert for all subscriptions (F-18). (d) 06:00 weekday subscriptions intentionally sent yesterday's data early; they now wait for today's ETL.
- Fix: decide the rule first and write it as a D-number: "send on the subscription's schedule days, after data is ready, at most once per due day; if the due day has no ETL, send/skip as decided". Then: T2 stores frequency, weekday set, month-day (REST schedule object: frequency + frequency details) not just the name; add `is_due(sub, today)` before the gate in `process()`; unit-test it with the 9 real schedule names plus a 1st-on-Saturday case and a holiday case.
- Test: table-driven unit test on the 9 schedules (see test plan #1).

**F-02. No per-subscription / per-workbook takeover flag; pilot cannot be fenced** (Critical for the pilot)
- Where: `t6_send.py:287-293` (all `Active_Flag=1 AND IsSuspended=0`), `build_config` 809-824 (fence = recipient allow-list), `t4_refresh.py:109-146` (refreshes every ready target with an active subscription). PLAN_REVIEW G25 asked for a per-workbook Enabled flag; it was never built (BACKLOG has none; T11a is "allow-list only", which I have not seen).
- Scenario: pilot is "CFD Accounts" (D-022b), owner X. Putting X's address in SEND_ALLOWED_RECIPIENTS sends every subscription X owns that is ready (D1b: one user owns 27 of 54). All of them are still live in Tableau, so each recipient gets two mails. Separately, `t4_refresh.py --execute` on prod refreshes every ready shared extract, not just the pilot's.
- Fix: a DWH column or small table (`TakenOver`, set by T11a at pause time, with who/when); t4 and t6 both require it; pilot scheduled command lines also pass `--subscription-id` as belt and braces. Make an empty takeover set mean "do nothing", not "everything".
- Test: dev fixture with two subs of one recipient, one taken over; assert only that one is claimed/sent and only its extract refreshed.

**F-03. Interaction risk with T11a: "suspended" has two meanings** (High to Critical; interaction only)
- Where: `t4_refresh.py:112-114,133-136` (extract refreshed only if the workbook has an ACTIVE NON-SUSPENDED subscription), `t6_send.py:292` (`IsSuspended = 0`), `t6_send.py:531` (live `suspended` -> skip), `t2_mapping_sync.py:337,516` (T2 copies Tableau's suspended flag). D-023: "suspended subscriptions are NOT sent".
- Scenario: T11a pauses the old subscription in Tableau. Next T2 sets IsSuspended=1. t4 now sees "all subscriptions suspended" and stops refreshing that workbook's extracts; t6 drops the subscription; and the live check at 531 would skip it even if t6 were patched. Result: pause succeeds, nothing is refreshed, nothing is sent, nobody is alerted (suspended subs raise no event). The task text says T11a changes t6 only; t4 has the same filter.
- Also: Tableau may auto-suspend subscriptions after repeated failures (recall, verify). 30 of 54 UAT subscriptions are suspended (D1b); some may be failure victims of the very bug this project fixes, which D-023 treats as intent. Ask the owners on prod before ignoring them.
- Fix: store "paused by us" (backup table key) and treat `IsSuspended=1 AND PausedByUs` as active in t2/t4/t6, including the live check; a user-suspended sub stays ignored. Do the pause, the T2 sync and the first send in a defined order (F-11).

### High

**F-04. Hidden data-driven ("when data refreshes") subscriptions: not mapped, not paused, and triggered by our refreshes** (High)
- Evidence: `preview_notes.md:6` (the sample Tableau email is "data-driven schedule", "You receive this subscription email when data is refreshed"), versus `d1b_findings.md` section 9 and `LIMITATIONS.md:13` (REST 3.25 shows none). So they exist on prod and REST does not list them. Open item in STATUS.md has been open since 2026-10-20.
- Scenario: the workbook has a hidden data-driven subscription. T4 refreshes its extracts (and again after an ETL rerun, up to 3 times a day, t4:196-199); each refresh completion can fire Tableau's own email, possibly with partial data (the original bug). The script's pause never touches it, so the recipient gets Tableau's mail plus ours.
- Fix: before any prod `--execute`, list them from the Tableau repository (read-only, needs a Tableau admin to enable repository access; recall, verify) or the UI, per workbook in scope. Decide: leave, delete, or exclude workbook. Record as a decision.
- Test: prod read-only preflight (test plan #3).

**F-05. The ETL gate cannot see a rerun in progress or an empty-but-successful load** (High; partly documented as G12)
- Where: `t4_refresh.py:149-160, 173-200`; `t6_send.py:332-386`; LIMITATIONS.md:18.
- Scenarios: (a) a mid-day rerun (truncate and reload) starts at 14:00; the log still shows the morning success; a deferred or just-ready subscription is exported against half-loaded tables and sent, and Sent is final. (b) ETL writes Success=1 with an empty staging table, extract refresh finishes with code 0, a blank report is mailed. (c) every dev row has Success=1, so it is not even known that failures are logged (t4 docstring 12-16). (d) After a rerun finishes the extract is refreshed again, but already-sent reports are never corrected.
- Fix: (a) add a "job currently running" check for each required pipeline (SQL Agent job activity in msdb; needs rights; or an ETL start marker) and a short quiet period after the latest EndTime; (b) a cheap post-refresh sanity check per key fact table (row count or max business date versus yesterday) stored with the refresh row, blocking on a large drop; (c) decide and document a resend rule for ETL reruns after the send.
- Test: dev, insert fixture rows into ETL_Master_Log for T-prefixed pipelines (allowed by D-026) and assert behaviour; sanity check needs a pilot.

**F-06. SES rejection after the message body is classified "ambiguous", so the first real send can jam every subscription** (High; recall, verify the SES behaviour)
- Where: `t6_send.py:182-193`; LIMITATIONS.md:26; D-033 item 3. SES typically answers an unverified sender identity or an unverified recipient in sandbox with `554 Message rejected` after the DATA terminator (recall, verify). Per RFC 5321 any non-250 reply to end-of-data means the server did NOT take the message; only a lost reply (timeout/drop) is truly ambiguous.
- Scenario: first pilot morning, sender not verified or recipient not verified in sandbox. Every ready subscription is claimed, rejected, left Claimed, alerted as "ambiguous", and blocked all day; an operator must release each row by hand.
- Fix: keep timeout/drop/crash ambiguous, but classify 5xx replies as Failed (permanent, alert after the first, do not retry-loop) and 4xx as Failed with retry. Also log the full 250 reply (it carries the SES message id, currently discarded at `code, _ = s.getreply()`, line 187) in the send log; it is the only handle for tracing a bounce.
- Test: local SMTP stub returning 554 after DATA (test plan #5), then the real SES test (#4).

**F-07. Alerting shares the failure domain of sending; nothing detects "the job did not run"** (High)
- Where: `t6_send.py:661-691, 694-707, 710-720`; `build_config` 809-824; no heartbeat anywhere; T2 has no log file or exit-code story (`t2_mapping_sync.py` prints only, 570-622).
- Scenarios: expired PAT (alert goes out, fine); rotated SES credential (alert goes through the same SES login, fails with PreDataError, state is taken back, retried forever, nobody told); scheduler task dead (service account password changed, host rebooted, task disabled): no run, no alert; T2 silently fails for a day. Also the alert recipient `<team-mailbox>@...` must be in SEND_ALLOWED_RECIPIENTS or the alert is dropped with only a log line (668-671); `build_config` does not check this.
- Fix: (a) heartbeat row per run (run id, time, outcome) in the DWH; (b) an independent check, for example a SQL Agent job on the DWH using Database Mail or the existing internal relay, that alarms when no heartbeat for t4/t6 by 09:30 on a business day or no successful T2 in 26 h; (c) startup check that ALERT_TO is on the allow-list; (d) Task Scheduler "last result" is not monitoring; do not rely on it.
- Test: drill #9.

**F-08. T2 is unscheduled, unlocked, unlogged, and one bad item aborts the entire sync; the stale-mapping margin is only ~2 h** (High)
- Where: `t2_mapping_sync.py:245` (`sys.exit` when the Metadata API returns nothing for a workbook; the API indexes new content asynchronously, recall, verify), `:327` (`sys.exit` on a subscription with neither PDF nor image), `:206` (`get_by_id` of a subscribed view that was deleted raises), `:222,:242` (GraphQL errors exit), `:536` (25% drop guard), `:577-581` (no PAT lock, no HTTP timeout, no log file). `t6_send.py:510-514` blocks a subscription whose LastSeenAt is over 26 h old; T9 (scheduling) is parked, so no schedule for T2 exists.
- Scenario: a user publishes a workbook and subscribes within minutes; the Metadata API has not indexed it; T2 exits; LastSeenAt stops advancing; about 26 h later every send is blocked (alerts go out but there is nothing to fix except rerunning). With T2 at 07:00 daily, one failed run blocks all sends from ~09:00 the next day.
- Fix: per-workbook try/except that marks only that workbook unseen (fail closed for it alone) and exits non-zero at the end; run T2 twice a day or raise the limit to ~36 h with an early warning at 26 h; give T2 a rotating log, the PAT lock, and an HTTP timeout.
- Test: dev, feed `collect_tableau` a fake server with one empty Metadata node and one deleted view; assert the rest is stored (test plan #6).

**F-09. One PAT, three scripts plus humans; T2 ignores the session lock; docs and code disagree on variable names** (High)
- Where: `t4_refresh.py:473-525` (lock), `t2_mapping_sync.py:577` (no lock), `.env` keys (TABLEAU_PAT_*), `customsql_mapping_proposal.md:16` ("no TABLEAU_SCRIPT_PAT_* is set") while no script reads `TABLEAU_SCRIPT_PAT_*`; STATUS handoff 2026-10-22 says the preview used "the existing PAT from .env" (the old job's?).
- Scenario: a Tableau PAT supports one session at a time; a new sign-in invalidates the older one (recall, verify). An operator runs T2 manually at 09:35 while t6 is exporting: t6's next request returns 401, the subscription counts an EXPORT failure, after 3 the subscription gives up for the day (t6:507-509). If the .env PAT is the old job's PAT, running any of these kills the old job's session as well, breaking the "side by side" guarantee (D-017).
- Also: a PAT belongs to a person. If that account is disabled the whole system stops; PATs expire on inactivity/absolute age (recall, verify the server's values).
- Fix: dedicated Tableau service user + one PAT per script (T2, T4, T6) with distinct env names and a startup log line showing the PAT name prefix (the old job does this); T2 takes the same lock; a calendar entry for PAT expiry; document owner.
- Test: dev, two processes contend for the lock; UAT, confirm the one-session behaviour with two PATs read-only.

**F-10. Production switch is a code edit and a re-discovery; many dev-only assumptions** (High)
- Hard-coded: `DEV_SERVER`/`DEV_DB` guard in `t2_mapping_sync.py:52-92` and `t4_refresh.py:63-89` (t6 inherits via `t4.connect`); the same guard protects the test suites (they insert fixture rows), so loosening it for prod also lets the tests write to prod unless guarded separately. D-010 itself says Tableau reads `<PROD_DWH_SERVER>`; the registry (T3), the view/base-table resolution and the ETL pipeline names were all derived on `<DEV_DWH_SERVER>`.
- Unverified on prod (list for the preflight, see test plan #3): ETL_Master_Log columns/semantics/time zone/PipelineName values; whether failed runs are logged; multiple rows per day; SQL Server version for `STRING_AGG` (2017+) and `AT TIME ZONE` (2016+); rights for `sys.sql_expression_dependencies` (VIEW DEFINITION; without it views resolve to nothing and everything fails closed silently); rights to create the Integration tables (T1 DDL on PROD DWH needs change control); view definitions of `vw_DataSource_PNL` etc. on prod; Tableau prod REST version and TSC 0.38 compatibility (`use_server_version=True`); HTTPS and whether the corporate CA is trusted by the Python/`requests` bundle (the old main.py has a commented-out `verify: False`, the new code has none); site id (default site vs a named one; subscriptions on other sites are invisible); PAT user role (listing all subscriptions and users' emails needs admin); Metadata API enabled; data-driven subscriptions (F-04); all Tableau ids differ so T2 must be re-run on prod. The d1b numbers (54 subs, 30 suspended, 11 extracts) come from a backup (D-012) and may be stale.
- Fix: replace hard-coded guards by `EXPECTED_DWH=server|db` env for runtime and keep a separate hard-coded dev guard in the test files; write one read-only preflight script that checks every item above and prints a pass/fail list.

**F-11. Cutover day-one double-send windows and lifecycle gaps** (High)
- Scenarios (T11a not reviewed, these are things it must cover):
  1. Order on day one: if the script sends at 09:30 and the old subscription fires at 10:15 and has not been paused yet, double send. Pause must precede enabling the script and be verified.
  2. A user later creates a NEW native Tableau subscription on a taken-over workbook: Tableau sends it on its own schedule AND T2 picks it up so the script sends too. Needs "new subscription on taken-over workbook" detection (alert or auto-pause).
  3. A workbook that loses a data source becomes single-source: `t2_mapping_sync.py:251-253` skips it, its subscriptions go `Active_Flag=0`, the old one stays paused: the report silently stops. A workbook that gains one starts being sent by the script while its native subscription is still live (double).
  4. Re-created subscription: new id, same hash; handled by `DUP_SQL` only if the old row got a send today (t6:440-444) and by an alert otherwise (SIBLING_SQL). It still sends.
  5. Renamed workbook/project: ids are stable, names refresh at the next T2; harmless. A republish-as-new gives a new id and orphans the mapping.
  6. Custom views: REST hides them (LIMITATIONS.md:10); a subscription on a custom view is exported as the base workbook. "example-Nova ETPs Daily Report" has one (d1b section 1).
  7. Subscription on a view of a workbook: the gate uses the workbook's readiness; fine.
- Fix: one written cutover runbook (pause, verify, T2, enable, first-day watch) and the detections above as alerts.

### Medium

**F-12. Recipient handling has silent drops and no hygiene checks** (Medium)
- `no_allowed_recipient` and `blocked` are not in `BAD_KINDS` (t6:724-725), so an address missing from SEND_ALLOWED_RECIPIENTS means a paused old subscription plus a script that quietly never sends, exit code 0, no alert. The allow-list is exact-address (t6:139-144), so every new recipient needs an .env edit; D-008 is described as a domain rule in PLAN_REVIEW but implemented as a list. The script does not check the subscriber's site role: a user who left (unlicensed or removed) still has a subscription and an address, and Tableau would not have sent to them (recall, verify). Subscriptions to groups appear as per-user subscriptions in REST (recall, verify); a user with no email raises `no_recipient` (good).
- Fix: alert on `no_allowed_recipient` (once per sub per day); use a domain allow-list (`@example.com`) plus the explicit list for the pilot; skip/alert on Unlicensed users.

**F-13. The exported content is never validated and is rendered as a different identity** (Medium)
- Export runs as the PAT user, not the subscriber (LIMITATIONS.md:5). D1b found no user functions in 37 workbooks, but cannot see database-side row-level security or whether data sources use "prompt user"/Kerberos delegation (d1b section 9). If the PAT user cannot connect, Tableau returns a PDF/PNG containing "unable to connect" and the script mails it. `send_if_view_empty` is not read (53 of 54 are true, so low impact). No comparison with the previous send (page count, size).
- Fix: store pdf size/page count/image count in the send log; warn or hold when it deviates sharply from the previous day for the same subscription; pilot side-by-side against a Tableau-sent email of the same subscription.

**F-14. Size and timing were measured on UAT with thin data; limits are untested** (Medium)
- D-012: UAT cannot reach the prod DB, so e2e figures (3.09 MB mail, 7-page PDF) say nothing about prod. Size limit 25 MB (`t6:79`) is above what most corporate mailboxes accept (D-019 unknown); an oversize mail is `Failed OVERSIZE`, final for the day (t6:504-506, 584-587), with no fallback such as a link. SES's own cap (about 40 MB incl. encoding, recall, verify) is irrelevant if recipients' limit is lower, and the bounce arrives later, invisible to the script.
- Scale: sequential exports; `HTTP_TIMEOUT_S = 300` per request; the 09:30-ish burst (about 24 live subscriptions, 7-view workbooks with up to two image calls per view) could take 25-50 min, hitting the 40 min claim budget (t6:746) and the 1 h task limit; deferred subscriptions wait for the next run. Per-run REST cost is fine (already-sent and not-ready subscriptions return before any Tableau call).
- Fix: measure on prod with `--dry-run --export`; set the size limit from the real mailbox limit (ask the Exchange admin; 10-15 MB is a common ceiling, recall, verify); add a "link instead of attachment" fallback or at least an alert.

**F-15. The scheduler design for the new scripts does not exist** (Medium)
- T9 is parked, so there is no XML or batch for t2/t4/t6. The deployed pattern is one task, 08:30 daily, repeat 15 min for 8 h, `ExecutionTimeLimit PT1H`, `IgnoreNew` (Scheduler_Tableau_Extract_Automation.xml:11-13,30,45). If t4 and t6 are chained in one task, a 40 min refresh wait plus a 40 min claim budget exceeds 1 h and Task Scheduler hard-terminates mid-send, leaving rows Claimed -> TimedOut at 90 min (alert, manual release). If they are two tasks, which one gets the PAT lock at each :00/:15 tick is a race, and a long t6 batch delays t4 (a late extract waits for t6 to release the PAT) and vice versa (LIMITATIONS.md:27). Not covered: wrapper batch exit codes, log location, host (is it the Tableau host?), service account rights on `out/`.
- Fix: two tasks, offset by a few minutes, t4 before t6; ExecutionTimeLimit longer than the in-code budgets; per-request deadline check inside the render loop; decide the host.

**F-16. Mapping-staleness gate has gaps in both directions** (Medium)
- t6 blocks a workbook whose `updated_at` is later than the last T2 sync (t6:533-536). Any author republish after the daily T2 run blocks that workbook's report for the rest of the day (alert fires, correct but noisy); whether an embedded-extract refresh bumps the workbook's `updated_at` is unknown (recall, verify). In the other direction nothing watches the PUBLISHED data sources, which carry the custom SQL and connections: a republished shared extract (11 distinct, used by many workbooks) can change tables with no workbook change, and the stale mapping is used for up to 26 h.
- Fix: run T2 more often; also compare each published data source's `updated_at` with its last sync; measure what bumps `updated_at` on UAT (read-only).

**F-17. No operator runbook, kill switch or audit for manual fixes** (Medium)
- Kill switch: none except disabling Task Scheduler tasks one by one (and an empty allow-list happens to make t6 exit 1 before logging is set up, t6:849-851, which looks like a crash). Manual release: described in the schema comment (T1 sql 187-198) and the alert text, but there is no tested SQL, no audit (the UPDATE does not record who or why), and no way to check whether a mail arrived (SES message id not stored, no SES event destination, bounces go to whatever mailbox <sender-mailbox>@ is, if it exists). Restore of a paused subscription is T11a's. Logs: `out/t6_send.log` and `t4_refresh.log`, 5 MB x 4, relative to the project folder (a packaged exe would break `Path(__file__)` based ROOT, cf. the old main.py's frozen-path handling; T9 parked).
- Fix: a one-page runbook with copy-paste SQL for release/mark-sent (with Message note), a single DWH control row (`Enabled`, `ReadOnlyMode`) read by t4/t6/T2, SES configuration set + SNS to the team mailbox, and storing the SES message id.

**F-18. Alert fatigue: weekend, holiday, and never-taken-over subscriptions trigger daily "not ready" alerts** (Medium)
- `send_alerts` (t6:698-700) adds every not-ready/blocked subscription after 15:30 SGT, including subscriptions that are not due (F-01), weekends/holidays (the trigger runs 7 days), and workbooks that are permanently NotReady (the 4 Excel/CSV workbooks, 22+ blocked ones kept on Tableau per PLAN_REVIEW D-017). One mail a day with ~20 lines trains people to ignore it, and real ambiguity alerts ride in the same mail.
- Fix: alert only for taken-over, due subscriptions; separate "needs a human now" from "FYI not ready".

**F-19. Interaction with the old refresh job and Tableau's own schedules** (Medium)
- Old job (`main.py`): refreshes a data source after ANY one mapped ETL succeeded (lines 84-94, 139-151) and logs at submit; it swallows every exception and exits 0 (157-163), blocks on `input()` if `.env` is missing (21, 40), and its batch wrapper appends to an unrotated log forever (`run_extract_refresh.bat:3-37`). It is never modified (D-017), so partial-data refreshes by the old job will continue. This is safe for correctness because the new gate requires its OWN successful refresh after the last ETL, and `active_refresh_job` (t4:264-271) skips when a job is running. Residual: (a) a same-tick race can produce a second, harmless refresh; (b) Tableau-side schedules (22 active extract tasks at 09:05/09:40 per d1b section 1) refresh extracts mid-ETL and can fire data-driven subscriptions (F-04); (c) if our job gives up after 3 attempts (t4:196-197), the extract holds the old job's partial data and the gate correctly waits, but no human alert for "gave up" exists in t4 itself (only exit code 1); (d) un-suspending the suspended schedule "Extract 1030AM_Tue-to-Sat (DataTeam)" (29 extracts hang on it) would start refreshing them irrespective of ETLs, nothing detects that.
- Fix: t4 `gave up`/`lookup_failed` should alert through the same channel as t6; monitor schedules read-only; document that the old job must keep running.

### Low

**F-20. Data safety** (Low to Medium)
- `.env` will hold the PAT secret and SES SMTP password in clear text on the prod host (D-017/D-019). Restrict the file ACL to the service account and admins; no AWS Secrets Manager reach from an on-prem Windows host is documented, so consider DPAPI or the Windows Credential Manager. `.gitignore` covers `.env`, `out/`, `*.log`; note that this folder is not currently a git repository (no history, no rollback of code), though CLAUDE.md assumes commits.
- `out/` on the dev workstation is 32 MB and holds .eml/PDF/PNG built from a prod-backup (real client data and a real address, LIMITATIONS.md:30). Delete or move before wider sharing.
- Logs carry workbook names, subscription ids, error text and a 10-char unsalted SHA-256 of the recipient list (t6:106-107, 555); an internal address set is guessable from it. Salt it or log only counts. Send-log Message and alert mails carry sanitized exception text; DWH stores subject and message text (D-028).

**F-21. Email fidelity and delivery details** (Low to Medium)
- Encoding: D1b notes message text with special characters came back as replacement characters (section 9). Subject, body and filenames use EmailMessage defaults (UTF-8), which is fine; the replacement characters, if real, were lost before storage (T2 stores `nvarchar`, but `s.message[:2000]`). Check one real message.
- From: `formataddr((sender, sender))` renders "addr <addr>" (subscription_mail.py:106, t6:639); Tableau's own display name is unknown. Date uses `localtime=True` (host zone).
- Images: no width attribute on `<img>`; images up to 3200 px wide render at native size in Outlook desktop (recall, verify). Wide-crosstab slivers are still sent (LIMITATIONS.md:24). Schedule line prints internal schedule names such as "1100AM_1stDayEveryMonth" to recipients (guessed wording, preview_notes.md).
- Delivery: SES sandbox/production access, verified identity for `<sender-mailbox>@` (UAT used `<uat-sender-mailbox>@`), port 587 egress from the prod host, SPF/DKIM for the sender domain, per-second send rate (sequential sends, low risk), bounce/complaint visibility (F-17). Only a live test settles these.

**F-22. Doc drift and small inconsistencies** (Low)
- STATUS.md header says "T2 in progress; refresh and send not started" while the handoff log says built. BACKLOG T6/T7 cite 65 checks and T4 37 while STATUS cites 93 and 61. BACKLOG T11a "in-progress" with no owner details. CLAUDE.md "packaged as an exe" vs scope cut. `customsql_mapping_proposal.md` refers to `TABLEAU_SCRIPT_PAT_*` which no code reads. `t4` docstring says the DWH is dev-only (ok today).

---

## 3. What each check file cannot catch

All three check files use fakes for Tableau and SMTP (or a local socket server), run against the dev DWH, and cannot prove:
- t2_checks: counts are compared with d1b JSON (same Tableau backup); tolerances hide drift; rerun idempotency only. Nothing tests partial Metadata responses, deleted views, new workbooks, or the schedule data (F-08, F-01). The "rerun" step uses the shared PAT.
- t4_checks: fake Tableau; the real `datasources.refresh`, finish codes on the real server, queue behaviour and the one-session PAT behaviour were never exercised (UAT cannot refresh, D-012). Real ETL_Master_Log reads exist only for dev; rerun-in-progress and empty-load are not modelled (F-05).
- t6_checks: no real SES (reply codes, STARTTLS login, sandbox, identity, size caps, throttling), no real export of a data-bearing workbook, no schedule logic (does not exist), no PAT contention with T2, no Task Scheduler kill mid-send, no test that an alert recipient is on the allow-list, no 554-after-DATA case, no multi-recipient or non-ASCII subject/filename case, no clock skew between hosts.
- Only a prod pilot can prove: ETL_Master_Log semantics on prod, prod view/pipeline mapping, prod Tableau REST/HTTPS/site/permissions, real export time and size, SES delivery and bounce path, hidden data-driven subscriptions, recipients' mailbox limits, the Tableau-sent email versus ours, and whether the extract refresh really lands before the export.

---

## 4. Things the plan assumes that nothing verifies (checklist)

| Assumption | Where it matters | How to verify | Needs go-ahead |
|---|---|---|---|
| ETL_Master_Log: one or many rows a day; Success=0 rows exist; EndTime is Singapore time; names match dev | t4:92-98, 149-160 | SELECT-only queries on prod DWH | prod read |
| DWH clock is UTC+8 on prod | t4:92-98 aborts if not | same | prod read |
| "Today" = business day; ETL runs on weekends/holidays? | F-01, F-18 | ETL_Master_Log history for 8 weeks | prod read |
| Prod Tableau: REST version, HTTPS cert, site, Metadata API, TSC compatibility | all | one read-only sign-in with the new PAT | prod read + new PAT |
| PAT user can list all subscriptions, read users' emails, export every view, refresh every extract | t2, t6, t4 | read-only calls; refresh test on a pilot extract | prod read; refresh needs go-ahead |
| Subscription types incl. data-driven | F-04 | repository query or admin UI | admin |
| Per-viewer/RLS content | F-13 | DWH RLS check + compare PDFs | prod read |
| SES: out of sandbox, sender identity, port 587, size cap, bounce path | F-06, F-14 | one live test to the user's own address | SES owner + user |
| Recipient mailbox limits | F-14 | ask Exchange admins | none |
| Host: is it the Tableau server; which account runs the tasks; rights on out/ | F-15 | ask | none |

---

## 5. Prioritised test plan (10 tests)

Legend: U = unit/local, D = dev DWH (standing permission D-026), R = read-only against UAT/prod, P = pilot on prod.

1. **Schedule due-logic (U + R).** Build `is_due()` (F-01). Table-driven test over the 9 real schedule names (monthly 1st, Tue-Fri, Sat, Mon, Mon-Sat, weekday 06:00) plus: 1st of month on a Saturday, a public holiday, a day with no ETL. Pass: monthly subscription due only on its day; Saturday subscription sends only on Saturday (and per the decided rule when there is no ETL); nothing sends on a non-due day; no alert for non-due subscriptions. Read the real frequency details from the UAT schedules with a read-only call. No go-ahead needed for U; the UAT read uses the existing PAT.
2. **Takeover scoping and the T11a interaction (D).** Fixtures: two subscriptions of one recipient, one taken over, one paused-by-us, one user-suspended. Pass: t4 refreshes only the taken-over workbook's extracts; t6 claims only taken-over subscriptions; paused-by-us counts as active, user-suspended stays ignored; empty takeover set means nothing happens; T2 rerun after the pause does not change this. Dev only.
3. **Prod read-only preflight (R, needs user go-ahead).** One script that prints pass/fail for: ETL_Master_Log columns, row semantics, timezone, pipeline names and 8-week history (weekends/holidays/failure rows); SQL Server version; VIEW DEFINITION rights (resolve `Tableau.vw_DataSource_PNL` to the same 7 base tables as dev); Tableau REST version, HTTPS trust, site, Metadata API, PAT role (lists all subscriptions; can read emails); and a list of data-driven subscriptions from the repository/admin UI. Pass: every item matches the plan's assumption, or a decision is recorded. No writes, no refresh, no mail.
4. **SES live test (P, needs user and SES owner go-ahead).** From the prod host, STARTTLS login on 587; send three messages to the user's own allow-listed address: 1 MB, 10 MB, 20 MB (and one to the SES bounce simulator if the account allows). Pass: all accepted with a logged 250 message id; sender identity accepted; size ceiling known; bounce reaches the team mailbox; sandbox status known.
5. **SMTP reply classification (U/D).** Local stub that returns 554 and 451 after DATA, 250, and drops; Pass: 5xx -> Failed with alert after 1; 4xx -> Failed retry; drop/timeout -> Claimed + ambiguous alert; the 250 reply text is stored. Local only.
6. **T2 resilience and PAT contention (D).** Fake Tableau feeding an empty Metadata node, a deleted view, a subscription with no format, a new workbook. Pass: the rest of the mapping is stored, only the bad workbook fails closed, exit code non-zero, heartbeat written; two processes contending for the PAT lock never sign in at the same time; a second sync run within the day does not break gating. Dev only.
7. **ETL rerun and empty-load drills (D).** Insert T-prefixed ETL_Master_Log fixtures: success then a rerun-in-progress marker; success followed by failure; success with an empty source. Pass: no export during a rerun; failure blocks; the sanity check holds an empty load; documented resend behaviour after a rerun. Dev only (ETL running marker needs the msdb activity query rights; if absent, the pass criterion becomes "quiet period honoured").
8. **Prod dry-run with export (R/P, needs go-ahead).** `t6_send.py --dry-run --export` on prod with the new PAT: records export time, PDF size, page count, mail size per subscription. Pass: each export under 5 minutes, total under the run budget, every mail under the confirmed mailbox limit, no "unable to connect" content, compare one PDF and image set side-by-side with a Tableau-sent email of the same subscription (user supplies it). No mail is sent.
9. **Failure drills (D/P, some need go-ahead).** (a) kill the process mid-send, confirm Claimed -> TimedOut at 90 min -> alert -> run the documented release SQL -> next run behaves; (b) expire/revoke the PAT: the alert arrives through the independent channel; (c) wrong SES password: alert still reaches the team via the independent channel; (d) stop the scheduler: the heartbeat alarm fires by 09:30; (e) fill `out/` or make it read-only: the run still finishes or fails loudly. Pass: every case reaches a human within the stated time, and the runbook steps work as written.
10. **Prod pilot, two business days plus a non-due day (P, needs explicit go-ahead).** One subscription, one allow-listed address (the user's own first, then the real recipient), old subscription paused and verified BEFORE the first run, hidden data-driven subscriptions for that workbook handled, scheduler tasks installed as two offset tasks. Pass: exactly one mail per due day from the script and none from Tableau; send log `Sent` with message id; nothing sent on a non-due day or weekend; no unexpected alerts; rollback drill (restore the paused subscription) works in under 10 minutes.

---

## 6. What I could not verify

- Anything about prod: Tableau, DWH, SES, mailbox limits, the host, the service accounts.
- Tableau/SES/SQL Server behaviours marked "recall, verify" (SES response to an unverified identity, PAT single-session and expiry rules, Metadata API indexing lag, auto-suspend of failing subscriptions, what bumps a workbook's `updated_at`, group subscriptions in REST, Outlook image scaling).
- T11a files (in progress), `preview_export.py`, `preview_smtp.py`, `d1_inventory.py`, `T1_tests.sql`, `T1_concurrency_test.ps1` and the full bodies of the check files (I read test names and structure, not every assertion).
- I did not run or time anything; all performance figures are estimates from the code's budgets and the d1b/D1 numbers.
