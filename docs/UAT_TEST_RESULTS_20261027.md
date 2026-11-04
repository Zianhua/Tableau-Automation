# UAT test results, 2026-10-27

Setup: UAT Tableau, dev warehouse, mail through the Gmail test account. Test reports: Weekly T+0 Failure Report, Daily T+0 Report (2 subscribers), CFD Accounts. LeadGen and Nova had really finished today; DailyETL was fed by hand with test rows (all deleted afterwards).

## Bottom line
The flow works end to end: ETL ready, then extract refresh, then send, with single-source reports untouched. Two of three reports were emailed. The third (Daily T+0) cannot be exported in time.

## What worked
| Area | Result |
|---|---|
| Daily mapping sync | 4 workbooks in scope; single-source "CFD Accounts v2" skipped |
| Loads not finished | Nothing refreshed, nothing sent |
| DailyETL failed | Reports blocked, no refresh, no alert before 15:30 |
| Failure after the cutoff | One alert mail; a repeat run sent none |
| DailyETL recovered | Refresh plan correct; the extract shared by two workbooks refreshed once |
| Refresh | One at a time, about 60 s apart, logged |
| ETL runs again after a refresh | Sends go back to waiting; extract refreshed again |
| Retry cap | After 3 failed attempts a target waits for a human |
| Real send: Weekly T+0 | Sent once (PDF + image) |
| Real send: CFD Accounts | Sent once (7 images, 3.2 MB) |
| Run send again, twice | "Already sent", no second mail |
| Single-source report | Not in our mapping, no refresh, no send |
| Weekly NOVA (Saturday) | Left to Tableau, not due |
| Kill switch | Scripts stop |
| Takeover with wrong owner id | Refused |
| Restore after a send | Refused |
| Daily summary | Failures listed |

## What went wrong
| # | Problem | Status |
|---|---|---|
| 1 | Refresh crashed when Tableau said "already queued" (two stale Pending jobs from Feb and Aug). | Fixed and reviewed. The stale jobs were cancelled. |
| 2 | CFD Accounts extracts pointed at a server UAT cannot reach. | You repointed them; refresh and send now work. |
| 3 | Daily T+0 Report export timed out at 5 minutes (heavy live dashboard). Two attempts, nothing sent. | Timeout raised to 25 min; not yet retried (attempt 3 of 3 left today). |

## Built because of today
- Refresh and send now wait while anyone else has a queued or running job on Tableau (stale queued jobs older than 6 hours are ignored). Proven by offline checks only; Tableau was idle in every live run.
- The "Subscription schedule" line is removed from the mail body (mails sent earlier today still have it).

## Third round: deletes, resume, schedules, scheduled tasks (14:15 to 14:55)
Test subscriptions (subject "ZZTEST ...") were created on the Weekly T+0 workbook, image only, and all removed afterwards.

| Test | What happened | Result |
|---|---|---|
| Deleted before takeover | Gone from the list at the next sync; nothing sent | Pass |
| Deleted after takeover | Marked gone, warning, never sent, no errors | Pass |
| Deleted on a due day, after data ready, before the send | Found deleted at send time; skipped; no mail, no crash | Pass |
| Two subscriptions on one report, one deleted | The other sent one mail | Pass |
| A person resumes a taken-over subscription | Re-paused at the next takeover run (counted, warning); not sent that day | Pass |
| Weekly (Saturday) schedule | Fri not due; Sat, Sun, Mon due; Wed (4 days late) not due | Pass |
| Monthly (day 1) schedule | Oct 31 not due; Nov 1 to 4 due; Nov 5 not due | Pass |
| Scheduled tasks (dry-run) | 2 full cycles, 15 min apart, 2 min stagger, all exit 0, no overlap, none killed | Pass, with a limit (below) |
| Export timeout | Raised to 25 min (`TABLEAU_HTTP_TIMEOUT_S`, default 1500 s). You say Tableau subscriptions take up to 15 min, so this leaves headroom | Done |
| Export asks Tableau for a fresh render | Every PDF and image asks for a render no older than a minute, after the extract refresh | Confirmed in code |

Limits and new findings:
4. **Task registration not tested for real.** `register_tasks.ps1` asks for the service-account password at a prompt, which an automated session cannot answer. I ran equivalent dry-run tasks under your own login instead (same timings and settings). The real script was only previewed with `-WhatIf`. Run it once on the host.
5. **Refresh cap counts successes.** Three refreshes of the same extract in one day (even all successful, from repeated ETL re-runs) triggers "gave up, needs a human". Suggest counting failures only. Not changed.
6. **The daily sync stops on a big drop in subscriptions.** If subscriptions fall by more than 25% in a day, it aborts (partial-read protection) and there is no override for subscriptions. Deleting 4 of 9 tripped it. I fixed the test data by hand. Suggest an override flag, or a higher threshold for small counts. Not changed.
7. **Repeated warning:** the old deleted pilot subscription still logs a "no longer exists" warning on every takeover run.

## Not tested yet
Whole-workbook delete (destructive), changes after takeover (someone edits a subscription), the takeover cap of 10, mail server down, the real task registration with a service account.

## State left behind
No test ETL rows remain. Send log today: Weekly T+0 and CFD Sent, Daily T+0 two Failed (2 of 3 attempts). `.env` mail setting is still local. Nothing is committed.
