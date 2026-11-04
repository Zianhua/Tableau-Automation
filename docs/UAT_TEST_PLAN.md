# UAT test plan (for the next session)

Written 2026-10-26. Plain names are used on purpose; internal tags are not.

## Ground rules
- All test mail goes to **you@example.com** only. The setting `SEND_ALLOWED_RECIPIENTS` in `.env` holds only that address. To test "more than one subscriber" without a second real mailbox, use plus-addresses (you+a@example.com, you+b@example.com) on the test Tableau users and add them to the allow-list. Check first that the company mail system delivers plus-addresses; if not, use two real aliases.
- Mail goes through the Gmail test account (`--smtp` for the run, or `MAIL_TRANSPORT=smtp`). Put `MAIL_TRANSPORT=local` back after each test run.
- Every real pause or restore is fenced with `--subscription-ids` and `--owner-user-id`. Never take over a subscription that is not a test one.
- UAT cannot reach the production database, so it cannot refresh extracts or show live data. See "Preparation" below for how we get around that for the full-day tests.
- The other UAT subscriptions were first suspended (68) and then DELETED in Tableau on 2026-10-26 (125 in all, list in `out/uat_deleted_20261026.json`), so Tableau itself does not mail real people. Deletion is intended; they are not restorable from our side.
- Extra subscribers are other Tableau accounts; mail goes to their registered email address. Only add an address to the allow-list if its owner agreed to receive test mail. List the accounts and addresses before the test.

## Where we left off (2026-10-26)
- Subscriptions are now enabled on UAT (server setting plus site setting).
- Test subscription "Company OI in CFD Shares" (images only, weekday 06:00 schedule, owner = the user, on workbook "CFD Accounts", Middle Office): taken over and suspended by the script, backup row written. One real export plus Gmail email was sent to you@example.com and looked fine. It is still suspended and still taken over. Restore it (`t11_pause.py --restore`) or reuse it as test case 1.
- The export of this view is a long table cut off at 3200 pixels with white space to the right. Compare against Tableau's own email and decide if that matters.

## Preparation (do first, in this order)
1. **Other UAT subscriptions: done 2026-10-26.** All 68 other active subscriptions were first suspended, and later that day all other UAT subscriptions (125) were deleted in Tableau. The earlier suspended list with their original settings is in `out/uat_suspended_20261026.json`). One that carries a message was checked after the update and kept its message and settings. Do not un-suspend any of them while subscriptions are on.
2. **Make "data is ready" testable on UAT (decided by the user 2026-10-26):** make a few test reports work against the UAT data warehouse and add the ETL-log rows by hand.
   - **Why UAT cannot refresh today:** the restored workbooks' data sources point at the production database server, which UAT cannot reach. Nothing is wrong with Tableau's refresh itself.
   - **Repoint 2 to 4 test workbooks** (one single-source, two with two sources from different data loads, one image, one PDF) to the UAT data warehouse (`<UAT_DWH_SERVER>`). Edit the data source connection in Tableau (or republish). Needs a database login for Tableau to use on UAT: the user enters it in Tableau directly, never in chat. Check the same tables and views exist on the UAT warehouse with the same names.
   - **Then a real refresh works on UAT** and the real refresh path (t4) can be tested end to end.
   - **ETL log rows:** our scripts read the ETL log on the dev warehouse. Insert test rows into it to say the daily load finished today, for the pipelines the test workbooks depend on, and delete them afterwards. This knowingly lifts the old "never modify the ETL log" rule for test rows only (D-052). The table has no tag column (only JobID, PipelineName, EndTime, Success), so the inserted JobIDs are noted locally and only those rows are deleted; never touch real rows.
3. **Create the test subscriptions** in Tableau (see cases). Run the daily mapping sync once afterwards so the script knows about them.
4. Pick test workbooks: need (i) a report with one data source, (ii) a report with two data sources whose tables come from different data loads (for example Daily and Nova), (iii) a live-connection report if one exists on UAT, (iv) a PDF-format report and an image-format report. The review list (`out/review_list.csv`) and the readiness view show which workbooks fit.

## Test cases
Each case: set up in Tableau, let the script take over, then check the next due day. Record pass or fail and what actually happened.

### A. Full life of one subscription (the main case)
Day 0 (set-up): create the subscription as a test user, in the format being tested. Run the daily mapping sync. Run the takeover as a dry run, then for real.
- Expect: the subscription is suspended in Tableau; a backup row exists; subject, format, schedule and message are unchanged; the send dry run says "taken over today", not due today.
Day 1 (a due day):
- Before the data loads finish: nothing is sent ("not ready"), no alert before the 15:30 cutoff.
- Data loads finish, refresh done (see preparation): exactly one email arrives with the right subject, heading, and attachment(s).
- Run the send script again straight away and again 15 minutes later: no second email ("already sent").
- Check the send log has one Sent row for that subscription for that day.
Repeat A for: **image only, PDF only, image and PDF**. Check the attachment format, page size and orientation match the subscription, and the file size is under the limit.

### B. More than one subscriber on one report
- Two (or three) different test users each subscribe to the same view or workbook, same or different schedules and formats.
- Expect after takeover: each subscription is paused separately; on the due day each subscriber gets exactly one email (so 2 mails for 2 subscribers), none gets two.
- The report is exported once per subscription at serial pace (about 20 seconds apart), not all at once.
- Remove one subscriber's address from the allow-list: that subscription is not sent and an alert says no allowed recipient; the other still sends.
- A subscription with more than one recipient (if Tableau shows more than the owner): check all allowed recipients are on the one mail and any not allowed are dropped and counted.

### C. Report with one data source (and a live connection)
- **One data source, extract:** decide the expected behaviour first (by design these stay on normal Tableau subscriptions and are out of scope). Expect: not in the mapping as in scope, never taken over, never paused, nothing sent by us. Tableau sends it on its own schedule. Verify with the one-data-source report "CFD Accounts v2".
- **One data connection, live (no extract):** if one is on the in-scope list, expect no refresh to be requested, and the send waits only for the data loads.

### D. Report with two data sources from different data loads
- Pick a workbook whose two sources depend on different loads (for example Daily and Nova).
- Only one load finished: nothing refreshes, nothing is sent.
- Both finished: each extract refreshes in turn (one at a time, 60 seconds apart), then the report is sent once.
- One load fails: the report is not sent and an alert fires after the 15:30 cutoff.
- A load finishes again after the refresh already ran: the extract is refreshed again and the send waits for it.
- Two of the same sources shared by two workbooks: the shared extract is refreshed once, not twice.

### E. Deleting
- Delete a subscription in Tableau **before** takeover: it simply disappears from the list at the next sync; nothing sent.
- Delete a subscription **after** takeover (still paused): the script notices, closes the backup row, never sends, no error storm.
- Delete a subscription **on a due day after the data is ready but before the send**: skipped as deleted, no email, no crash.
- Delete the whole workbook: its subscriptions are dropped, an alert says a taken-over report left scope.
- Delete one of two subscribers' subscriptions on the same report: the other still sends.
- Confirm deleted subscriptions leave no active takeover row and do not count against the takeover cap.

### F. Takeover and pause behaviour
- A subscription that was already suspended is taken over too (rule: everything in scope is taken over); confirm the first-day skip warning and the Event Log warning appear.
- A human resumes a taken-over subscription: the next run suspends it again, and nothing is sent that day.
- Take over more subscriptions than the cap (10 per run): only 10 are taken, the rest wait, one alert fires.
- Run the takeover with the wrong user id: refused (owner fence).
- Restore a subscription: it is un-suspended, backup closed, and it is no longer sent by us. Restore after it has been sent today: refused unless the override is used.

### G. Schedule days
- Daily, weekdays, weekly and monthly schedules: sent only on due days; not sent on other days.
- A due day missed (data late): sent within the 3 day catch-up window, not after.
- Monthly subscription taken over a day or more before its due date: sent on the due day.
- Monthly subscription taken over on its due day: the first-day skip warning appears (known limit; confirm the warning is loud).

### H. Changes after takeover
- Someone edits a taken-over subscription (changes format, schedule or recipient): check what the daily sync picks up, and that the old values are not sent.
- Someone adds a new subscription to an already taken-over workbook: it appears at the next sync, is taken over at the next takeover run, and is not mailed by Tableau before that (note any gap).
- A workbook is republished after the daily sync and before the send: the send waits until the next sync (quiet note, not an alert).

### I. Safety and failure
- Kill switch on (file `out/KILL` or environment flag): all scripts stop and say so.
- Two scripts at the same time: the second skips (Tableau login busy).
- Send to a bad address: failure recorded, circuit breaker after repeated failures, no repeated spam.
- Mail server unreachable: send marked failed or left for a human as designed; no duplicate on retry.
- An export fails: three attempts, then an alert, no email.
- Mapping older than 26 hours: nothing sent, alert.
- Daily summary email arrives with the right counts.
- Windows scheduled tasks (dry-run mode): run for two full cycles, no overlap, last runs start at 16:30, 16:32 and 16:34 and are not killed.

## What "done" looks like
Cases A, B, D (both data sources) and E pass cleanly on UAT with mail to the test address. Anything that fails is fixed by a builder and re-tested with a fresh reviewer, then the production preflight starts.

## Decisions made (2026-10-26)
- Data-ready tests: hand-inserted ETL log test rows on the dev warehouse (D-052: the test workbooks read the dev warehouse, so repointing to the UAT warehouse was dropped). The rows cannot be tagged; the inserted JobIDs are noted locally. See Preparation 2.
- Extra subscribers: other Tableau accounts, using their registered email.
- The other UAT subscriptions: first suspended, then deleted for real (user decision 2026-10-26, out/uat_deleted_20261026.json).
- Cut-off table: send as is. The picture Tableau returns is only what its viewport shows (the table has a scrollbar), which is also what Tableau's own email shows, so sending the entire table is not possible with images. A PDF subscription carries the full table. Not worth more effort; revisit only if the report owners complain.

## Still open
1. Which exact table gets the test ETL log rows (confirm before the first insert).
2. A database login for Tableau on the UAT warehouse (user enters it in Tableau).
3. Which Tableau accounts act as extra subscribers, and whether their owners agree to test mail.


## What the user sets up in Tableau before the test (checklist)
Do this on the day BEFORE the send test, so the takeover happens on day 0 and the send on day 1.
1. **Pick 4 test workbooks** (tell the PM their names): (a) two data sources from different data loads, to be sent as images; (b) two data sources, to be sent as PDF; (c) one data source (stays on Tableau, out of scope); (d) one workbook for the "more than one subscriber" case (can be (a) or (b)).
2. **Repoint (a), (b) and (d) to the UAT warehouse.** Edit the data source connection in Tableau and enter a UAT database login there (never in chat). Click "Refresh now" once in Tableau to prove it works. Check the tables and views exist on the UAT warehouse under the same names.
3. **Have at least 3 Tableau accounts** with a registered email whose owners agreed to receive test mail (you plus two others, or test accounts). Each needs permission to view the test workbooks. Send the PM the list of account names and addresses so the allow-list can be set.
4. **Create the subscriptions.** Subscribing others is switched off on the site, so either sign in as each account and subscribe, or turn on "let site administrators/users subscribe others" in Site Settings, General (the Subscriptions section should now show since subscriptions are enabled). Create, using an existing weekday schedule so tomorrow is a due day:
   - workbook (a): image only, one subscriber;
   - workbook (b): PDF only, one subscriber;
   - workbook (d): image and PDF, three subscribers (three accounts);
   - plus three throwaway subscriptions on a test workbook, for the delete cases (delete before takeover, after takeover, on the send day);
   - one weekly or monthly schedule subscription, for the schedule-day case;
   - one subscription that you suspend by hand before the takeover (to test taking over an already-suspended one).
5. **Tell the PM which dev data loads (pipelines) each test workbook depends on**, and confirm which table gets the test ETL log rows (decided: `Integration.ETL_Master_Log` on dev, D-052).
6. **Decide the stale mapping clean-up:** SQL on dev (recommended) or a new logged flag.
7. **Optional, once, as administrator on the Tableau server:** register the Windows Event Log source (RUNBOOK).

## Update 2026-10-26 evening
- Test workbooks are chosen and subscribed (see D-052): Weekly T+0 Failure Report, Daily T+0 Report, CFD Accounts v2. They use the dev warehouse, so Preparation step 2 (repointing to the UAT warehouse) is no longer needed.
- The allow-list is no longer maintained on UAT: set `SEND_ALLOW_ALL_UAT=1` in `.env` (only effective on the UAT Tableau host, D-050). Alerts still need a valid `ALERT_TO`.
- Tableau emails the T+0 subscriptions itself at 06:00 on 2026-10-27 unless they are taken over first (user accepted this).
- Still open (as of the evening): the test rows in the dev load log (D-052), the stale mapping clean-up on dev, takeover of the test subscriptions, the table trim slice, and only two subscribers on Daily T+0 (not three), all on the same weekday schedule (the weekly and monthly cases need one more subscription).

## Update 2026-10-26 night
- Done since the evening note: the stale mapping rows on dev were deactivated and the daily mapping sync re-run; the table trim was done (D-052; archives `Fact.*_archive_20261026`); the four test subscriptions were taken over at 18:11 (CFD Accounts, two on Daily T+0 Report, one on Weekly T+0 Failure Report); the older pilot subscription "Company OI in CFD Shares" was deleted in Tableau, and CFD Accounts has a new subscription. The Weekly NOVA Multi-Asset Report is in scope with a Saturday weekly subscription and is not taken over (the weekly-schedule case).
- Because the four are suspended, Tableau does not mail them itself at 06:00 on 2026-10-27; they go only when the script sends.
- Still open: the test rows in the dev load log (none inserted yet), the first real send test on 2026-10-27 (needs the test rows dated that day, an extract refresh after them, and `MAIL_TRANSPORT=smtp` for that one run), the monthly-schedule case, and a third subscriber.
- Every database change made for these tests, with how to undo it, is listed in docs/DB_CHANGES.md section 11.
