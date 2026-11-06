# UAT deployment (on the Tableau host)

Target: warehouse `<UAT_DWH_SERVER>` / `CQGlobalSGDW`, Tableau `http://tableau-uat.example.com:8000/`. Database objects, grants and the manual data load are done by the DBA and not covered here. Dev is switched off: do not run anything from the dev machine with the same Tableau token.

Everything below is run in **PowerShell as administrator** on the Tableau host. Install folder: `F:\datateam_implementation\TableauSubscriptionAutomation`.

## 1. Get the code

Repository (public, values such as server names and emails are blanked in it, they come from `.env`):
`https://github.com/Zianhua/Tableau-Automation.git`

```powershell
git --version
cd F:\datateam_implementation
git clone https://github.com/Zianhua/Tableau-Automation.git TableauSubscriptionAutomation
```

If `git` is not installed on the host, download the repository as a zip from the GitHub page and extract it to `F:\datateam_implementation\TableauSubscriptionAutomation` instead.

Then copy two files from the prepared package (`UAT_package\TableauSubscriptionAutomation` on the dev machine) into the same folder on the host:
- `.env` (the UAT settings, holds secrets: copy the file over a secured path, never by mail or chat)
- `scripts\schedule\deploy_uat.ps1` (only if the clone does not already contain it)

## 2. Install (once)

```powershell
cd F:\datateam_implementation\TableauSubscriptionAutomation
.\scripts\schedule\deploy_uat.ps1
```

The scheduled tasks run as the account you are logged in as (add `-ServiceAccount 'DOMAIN\name'` to use another). That account must have the DB access. It checks the host, builds the Python environment, sets folder rights, registers the Event Log source, runs one read-only check, and registers the scheduled tasks in DRY-RUN mode (it asks for the account password once). Nothing is written, paused, refreshed or mailed yet. Re-running is safe.

Success looks like: `imports ok`, the mapping sync dry run ending with `RUN SUMMARY mode=dry-run exit=0`, and a list of seven tasks with a NextRunTime.

## 3. Go live (each step only when you are ready)

Run in the same folder, after `$py = '.\.venv\Scripts\python.exe'`.

**Step A. First real mapping sync** (after a day or two of clean dry-run logs in `out\`). It fills the mapping tables in the UAT warehouse and does not touch Tableau:

```powershell
& $py scripts\t2_mapping_sync.py
& $py scripts\review_list.py
```

Open `out\review_list.csv`. Delete in Tableau any subscription that must not go out, then run the two commands again.

**Step B. Mail test.** Set `MAIL_TRANSPORT=smtp` in `.env`, run the command, then set it back to `local`. One mail goes to `ALERT_TO`. The host must be able to reach `smtp.gmail.com` on port 587.

```powershell
& $py scripts\daily_summary.py --execute --smtp
```

**Step C. Pilot, then live.** Take over one subscription you own, then switch the tasks to real runs:

```powershell
& $py scripts\t11_pause.py --subscription-ids <id> --owner-user-id <user id> --execute
.\scripts\schedule\register_tasks.ps1 -RepoRoot $PWD.Path -ServiceAccount "$env:USERDOMAIN\$env:USERNAME" -Live -T11ExtraArgs '--subscription-ids <id> --owner-user-id <user id>'
```

`SEND_ALLOW_ALL_UAT=1` is in effect on this host, so real mail goes to every subscriber of a taken-over subscription. Only take over subscriptions you own. The extract takeover stays a dry run (see [RUNBOOK.md](RUNBOOK.md) sections 16 and 21).

## 4. Day to day

- Logs are in `out\*_<date>.log`. Each ends with a `RUN SUMMARY` line; `needs_human=0` means nothing to do. The daily summary mail arrives at 17:30.
- Stop everything: `New-Item F:\datateam_implementation\TableauSubscriptionAutomation\out\KILL -ItemType File` (delete the file to resume).
- Give a subscription back to Tableau: `& $py scripts\t11_pause.py --restore <id> --execute`
- Remove the tasks: `.\scripts\schedule\register_tasks.ps1 -Unregister`

## 5. Not tested yet

Everything on the host itself (running as the service account, a real task registration, the Event Log write), the scripts against the UAT warehouse (so far only dev), and Gmail from the host network. UAT also needs recent rows in `Integration.ETL_Master_Log` for the three pipelines, or nothing refreshes or sends.
