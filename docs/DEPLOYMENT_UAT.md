# UAT deployment (on the Tableau host)

Target: warehouse `<UAT_DWH_SERVER>` / `CQGlobalSGDW`, Tableau `http://tableau-uat.example.com:8000/`. Database objects, grants and the manual data load are done by the DBA and not covered here. Dev is switched off: do not run anything from the dev machine with the same Tableau token.

Everything below is run in **PowerShell as administrator** on the Tableau host. Install folder: `<INSTALL_DIR>\TableauSubscriptionAutomation`.

## 1. Get the code

Repository (public, values such as server names and emails are blanked in it, they come from `.env`):
`https://github.com/Zianhua/Tableau-Automation.git`

```powershell
git --version
cd <INSTALL_DIR>
git clone https://github.com/Zianhua/Tableau-Automation.git TableauSubscriptionAutomation
```

If `git` is not installed on the host, download the repository as a zip from the GitHub page and extract it to `<INSTALL_DIR>\TableauSubscriptionAutomation` instead.

Then copy two files from the prepared package (`UAT_package\TableauSubscriptionAutomation` on the dev machine) into the same folder on the host:
- `.env` (the UAT settings, holds secrets: copy the file over a secured path, never by mail or chat)
- `scripts\schedule\deploy_uat.ps1` (only if the clone does not already contain it)

## 2. Install (once)

```powershell
cd <INSTALL_DIR>\TableauSubscriptionAutomation
.\scripts\schedule\deploy_uat.ps1
```

The scheduled tasks run as the account you are logged in as, unless you add `-ServiceAccount 'DOMAIN\name'`. Use the account that is in the UAT database role `<SERVICE_DB_ROLE>` (today `example\PNTableauSVRC` and `<UAT_DOMAIN>\PNTableauSVRC`): a personal login has SELECT only and every write would fail. Run the manual commands in section 3 as that account too (for example in a PowerShell opened with runas). It checks the host, builds the Python environment, sets folder rights, registers the Event Log source, runs one read-only check, and registers the scheduled tasks in DRY-RUN mode (it asks for the account password once). Nothing is written, paused, refreshed or mailed yet. Re-running is safe.

Success looks like: `imports ok`, the mapping sync dry run ending with `RUN SUMMARY mode=dry-run exit=0`, and a list of seven tasks with a NextRunTime.

## 3. Go live (each step only when you are ready)

Run in the same folder, after `$py = '.\.venv\Scripts\python.exe'`.

**Step A. First real mapping sync** (after a day or two of clean dry-run logs in `out\`). It fills the mapping tables in the UAT warehouse and does not touch Tableau:

```powershell
& $py scripts\t2_mapping_sync.py --registry-first-run
& $py scripts\review_list.py
```

`--registry-first-run` is needed once: the registry starts empty, and without it only 5 registry rows are written per run (the rest are held and the run exits 2). Until this step has run, the other dry-run tasks see an empty mapping and have nothing to evaluate, so their logs say little.

Open `out\review_list.csv`. Delete in Tableau any subscription that must not go out, then run the two commands again.

**Step B. Mail test.** Set `MAIL_TRANSPORT=smtp` in `.env`, run the command, then set it back to `local`. One mail goes to `ALERT_TO`. The host must be able to reach `smtp.gmail.com` on port 587.

```powershell
& $py scripts\daily_summary.py --execute --smtp
```

**Step C. Pilot, then live.** Take over one subscription you own, then switch the tasks to real runs:

```powershell
& $py scripts\t11_pause.py --subscription-ids <id> --owner-user-id <user id> --execute
.\scripts\schedule\register_tasks.ps1 -RepoRoot $PWD.Path -ServiceAccount '<same account as in section 2>' -Live -T11ExtraArgs '--subscription-ids <id> --owner-user-id <user id>'
```

`SEND_TO_ALL_SUBSCRIBERS=1` is in effect on this host, so real mail goes to every subscriber of a taken-over subscription. Only take over subscriptions you own. The extract takeover stays a dry run (see [RUNBOOK.md](RUNBOOK.md) sections 16 and 21).

## 4. Day to day

- Logs are in `out\*_<date>.log`. Each ends with a `RUN SUMMARY` line; `needs_human=0` means nothing to do. The daily summary mail arrives at 17:30.
- Stop everything: `New-Item <INSTALL_DIR>\TableauSubscriptionAutomation\out\KILL -ItemType File` (delete the file to resume).
- Give a subscription back to Tableau: `& $py scripts\t11_pause.py --restore <id> --execute`
- Remove the tasks: `.\scripts\schedule\register_tasks.ps1 -Unregister`

## 5. UAT warehouse prerequisites (first checked 2026-11-09; item 1 met on 2026-11-12)

The install in section 2 is safe without these (it only runs dry). Steps A to C are not meaningful until items 2 and 3 are settled. The workbooks used for testing must not depend on DailyETL (use ones fed by LeadGenETL or NovaDailyETL, which do log on UAT) and their data sources must read <UAT_DWH_SERVER>.

1. **Registry seed rows: present.** Checked read-only on 2026-11-12 with `scripts/sql/prod_precheck.sql`: registry 20 Auto + 7 Manual rows, 2 static tables, 3 lineage sources, and every object, column and grant the scripts need. Re-run that script on any warehouse before deploying to it.
2. **DailyETL never appears in the UAT ETL log.** `Integration.ETL_Master_Log` only holds LeadGenETL (daily 09:30) and NovaDailyETL (Tue to Sat 10:00). 27 of the 29 mapped extracts belong to DailyETL, and the 33 `Integration.Lineage` tables of DailyETL were last loaded on or before 2026-06-29. Result: those tables are reported UNCONFIRMED by the registry sync (no log evidence, not registered) and their workbooks stay not ready. Either the UAT DailyETL is run and logged, or the DBA adds Manual registry rows for them, plus a test log row (D-052 pattern) if a send is to be tested. Say which before Step A.
3. **Where the Tableau data sources really read.** The 2026-10-22 inventory showed every SQL Server connection on this Tableau pointing at `<PROD_DWH_SERVER>` (production), and the test workbooks were later moved to the dev warehouse. The scripts gate on the UAT warehouse log only; they do not look at the connection. Confirm in Tableau (Edit connection on each in-scope data source) that they read `<UAT_DWH_SERVER>`, otherwise a refresh or send is decided on one warehouse and fed by another.

## 6. Not tested yet

Everything on the host itself (running as the service account, a real task registration, the Event Log write), the scripts against the UAT warehouse (so far only dev), and Gmail from the host network. UAT also needs recent rows in `Integration.ETL_Master_Log` for the three pipelines, or nothing refreshes or sends.
