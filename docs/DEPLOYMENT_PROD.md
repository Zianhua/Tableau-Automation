# Production deployment (on the production Tableau host)

Same install as UAT ([DEPLOYMENT_UAT.md](DEPLOYMENT_UAT.md)); only `.env` differs. Nothing in the code names a server, a database id or a Tableau id: everything environment-specific comes from `.env`, and the Tableau ids are read from the production Tableau by the daily mapping sync. **Nothing is triggered on production to test it**: the install runs dry, the checks below are read-only, and going live is done one subscription at a time.

## 0. Before the install (DBA and owner)

1. **Database:** the DBA has applied the DBJR on the production warehouse (objects, grants for the role `<SERVICE_DB_ROLE>`, the service account in that role, the manual data load and the 3 `Tableau_LineageSource` rows). Then run the read-only `scripts/sql/prod_precheck.sql` in SSMS on that warehouse, with a login that has VIEW DEFINITION. **Every row must say OK**, the lineage sources must say OK, and `utc_offset_min_must_be_480` must be 480. Note the `[server]` value (`@@SERVERNAME`): see `.env` below.
2. **Tableau:** a personal access token (PAT) for the production server, owned by a **site administrator** (the scripts list background jobs, extract tasks and schedules, which need admin rights). Not the UAT token: one token per server, and never shared with another machine (a second sign-in with the same token kills the first session).
3. **Mail:** the production SMTP settings (host, port, user, password). The scripts always use STARTTLS: an SMTP relay without TLS will not work. Decide `ALERT_TO` (the person or mailbox that gets the alerts).
4. **Owner decisions already taken** (RUNBOOK sections 14, 16, 22): production mails every subscriber of a taken-over subscription (`SEND_TO_ALL_SUBSCRIBERS=1` with the production host); every subscription that still exists in Tableau is sent once taken over, suspended or not (delete in Tableau what must not go out, using `review_list.py`); the old refresh job keeps refreshing its extracts and its refresh counts as it is (no extra refresh). Re-confirm them before step 3.
5. **Host:** Python 3.12 on the machine path, ODBC Driver 17 for SQL Server, network access to the warehouse (1433), Tableau, the SMTP host and PyPI (for the pinned packages), host time zone Singapore.

## 1. Get the code

Download the folder `tableau_subscription_automation` from the GitLab branch `tableau-subscription-automation` of `server-automation/tableau-extract-and-maintenance` (not the public GitHub copy: its values are blanked) to any temporary place on the host, for example `C:\temp\tableau_subscription_automation`. The legacy extract job (`executables_tableau_extract`, `batch_files`, `log` and its two scheduled tasks) stays where it is and is never changed. In an elevated PowerShell, from that downloaded folder:

```powershell
.\scripts\schedule\install_on_host.ps1 -CheckOnly                  # read-only: finds the legacy job and its folder, checks Python, ODBC, time zone, PyPI
.\scripts\schedule\install_on_host.ps1                             # copies the solution into <legacy root>\TableauSubscriptionAutomation
.\scripts\schedule\install_on_host.ps1 -LegacyRoot <INSTALL_DIR>   # if the check found no folder or more than one
```

It installs into a NEW subfolder next to the legacy folders (never into them), never copies `.env` or test suites, saves the legacy task definitions in `out\legacy_snapshot`, and proves that every legacy file and task is unchanged afterwards. For a later code update use `-Update` (keeps `.env`, `out\` and `.venv`).

## 2. `.env` (copy `.env.example`, keep it off mail and chat)

| Key | Production value |
|---|---|
| `TABLEAU_SERVER_URL`, `TABLEAU_SITE_ID` | the production Tableau server and site |
| `TABLEAU_PAT_NAME`, `TABLEAU_PAT_SECRET` | the production token (step 0.2) |
| `CONN_SERVER`, `CONN_DATABASE` | the production warehouse |
| `DEV_DWH_SERVER`, `DEV_DWH_DATABASE` | **the same values as `CONN_SERVER` / `CONN_DATABASE`** (the name is historical: it is the warehouse guard of this host). The scripts also require `@@SERVERNAME` to start with the first part of this name: compare with `[server]` from the precheck (a listener or alias name will make every script refuse to run) |
| `MAIL_TRANSPORT` | `local` for the install and the dry-run days; `smtp` only at step 4 |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SEND_FROM` | step 0.3 |
| `ALERT_TO`, `SEND_ALLOWED_RECIPIENTS` | the alert address; the allow-list must contain it |
| `SEND_TO_ALL_HOST` | the production Tableau host name, exactly as in `TABLEAU_SERVER_URL` |
| `SEND_TO_ALL_SUBSCRIBERS` | leave commented out until step 4 |
| `OLD_JOB_DATASOURCE_IDS` | leave empty (the role can read the old job's table; the precheck confirms it) |
| `OWNER_EMAIL`, `DEFAULT_SENDER_EMAIL` | the fallback alert recipient and the display From address (used when `ALERT_TO` / `SEND_FROM` are unset); replace the `example.com` values |
| everything else | the defaults in `.env.example` |

## 3. Install, dry run only

```powershell
cd <legacy root>\TableauSubscriptionAutomation
.\scripts\schedule\deploy_uat.ps1 -ServiceAccount '<DOMAIN\service account in <SERVICE_DB_ROLE>>'
```

The script name says UAT, but it only reads `.env`: it creates the venv, installs the pinned packages, sets the folder rights, registers the Event Log source, runs the mapping sync as a dry run and registers the seven tasks in **dry-run** mode. Nothing is written to the warehouse or Tableau and no mail is sent.

Then, run as the service account:

```powershell
$py = '.\.venv\Scripts\python.exe'
& $py scripts\t2_mapping_sync.py --registry-first-run     # first real mapping sync: writes the mapping tables only, never Tableau
& $py scripts\review_list.py                              # read-only: out\review_list.csv
```

Let the dry-run tasks run for at least two business days. Check every day (read-only):
- every `out\*.log` ends with a `RUN SUMMARY` line and no `FATAL`;
- run `& $py scripts\t4_refresh.py --dry-run --scope all` by hand in the afternoon, after the old refresh job has run (the scheduled dry run uses the pilot scope and shows nothing until subscriptions are taken over). For every extract behind a subscription due today that the old job or a Tableau schedule refreshes, expect `ADOPT` (would adopt) and **never** `REFRESH`; `OTHER REFRESHERS UNKNOWN` must not appear;
- how often `Tableau busy` appears, and how long t11 runs (`t11_pause` log): if t11 regularly takes more than 2 minutes, tell the developer before going live;
- `review_list.csv`: delete in Tableau what must not be sent.

## 4. Go live, one subscription first

```powershell
# mail test to ALERT_TO only (set MAIL_TRANSPORT=smtp in .env first)
& $py scripts\daily_summary.py --execute
# pilot: one subscription you own
& $py scripts\t11_pause.py --subscription-ids <id> --owner-user-id <user id> --execute
.\scripts\schedule\register_tasks.ps1 -RepoRoot $PWD.Path -ServiceAccount '<same account>' -Live -T11ExtraArgs '--subscription-ids <id> --owner-user-id <user id>'
```

Turn on `SEND_TO_ALL_SUBSCRIBERS=1` only when the pilot subscription went out correctly. Before taking over subscriptions whose extracts are refreshed by a Tableau schedule, check that schedule's time in Tableau: a schedule that fires after the last t6 run (16:34) means the report waits for the next day every day (RUNBOOK section 22). Widen the allow-list in small steps (RUNBOOK section 14). The extract takeover stays a dry run until you decide to take extracts over (RUNBOOK section 21, a few at a time, and only before the morning ETLs).

## 5. Roll back (in this order)

1. Stop everything: `New-Item <install folder>\out\KILL -ItemType File` (restores still work with the kill switch on).
2. Give the subscriptions back to Tableau: list them with `SELECT Subscription_id FROM Integration.Tableau_SubscriptionBackup WHERE TakenOver = 1 AND RestoredAt IS NULL;`, then `& $py scripts\t11_pause.py --restore <id> --execute` (repeat `--restore <id>` for several), or `--restore-all-for-workbook <workbook> --execute` per workbook. A subscription already sent today is only restored with `--allow-restore-after-send` or the next day (RUNBOOK section 6).
3. Give the extracts back: `& $py scripts\extract_takeover.py --restore-all --execute`. On a day the script already refreshed an extract it refuses (no second refresh): run it the next morning before the ETLs, or add `--force`.
4. Remove the tasks: `.\scripts\schedule\register_tasks.ps1 -Unregister`.
5. Only then, if wanted, the DBA rolls back the DBJR objects.
