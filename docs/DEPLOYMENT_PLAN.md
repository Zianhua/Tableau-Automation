# Deployment plan (to the server that hosts the scripts)

Written 2026-10-26, corrected 2026-11-04 (database order, guards read from `.env`, automatic extract refresh and extract takeover task added; run-order table, override table and static/registry triggers removed). For the data engineer and for the DBA / server administrator. Plain names first; file names or tags in brackets.

**Status: nothing in this plan has been done on the target host.** The scripts have only run on the developer machine against the **dev** warehouse and the **UAT** Tableau server. Where a step is unverified it says so. Open decisions are marked **OPEN**. Database side: see [DB_CHANGES.md](DB_CHANGES.md). Operating the system afterwards: [RUNBOOK.md](RUNBOOK.md). Read-only production checks: [PROD_PREFLIGHT.md](PROD_PREFLIGHT.md).

## 0. What gets installed, in one paragraph

**Order that matters: apply the SQL (step 6) BEFORE the code is put into use.** The code can be copied to the host (step 3) at any time, but no script may run and no task may be registered (steps 8 and 9) until the database changes are applied: the mapping sync checks its tables at start (a missing one is a fatal error) and the extract takeover refuses to start without its tables and the column `NoRetake`. Rolling back is the reverse: code first, SQL second.

Small Python scripts run from Windows Task Scheduler on one host: a daily mapping sync (copies workbooks, data sources, subscriptions, schedules and extract refresh tasks from Tableau into the warehouse, and keeps the table-to-pipeline registry in step with the lineage tables), a daily extract takeover check at 08:20 (a dry run until it is enabled; it also mails an alert when a taken-over extract is not being refreshed), three scripts every 15 minutes between 08:30 and 16:30 (take over and pause the old Tableau subscriptions; refresh extracts once their ETL loads have succeeded, in name order; export and email the reports that are due and ready, once per subscription per day), a watchdog every 15 minutes, plus an end-of-day summary mail. State lives in new tables in the warehouse schema `Integration` and in the folder `out\` next to the scripts.

## 1. Decisions that must be made before or during the deployment (OPEN)

| # | Open decision | Who | Needed by step |
|---|---|---|---|
| 1 | **Service account** that runs the tasks (a normal domain account, or a group managed service account `DOMAIN\name$` that needs no stored password). It is also the warehouse login (Windows authentication). | server admin | 3, 9 |
| 2 | **Install folder** on the host (examples use `D:\Apps\TableauAutomation`). Not a user's desktop. | server admin | 3 |
| 3 | **Production mail path.** Recommendation in the project: Amazon SES (the team owns the SES credentials). Not chosen, not tested. Needs a verified sender, a credential, outbound access from the host, and the attachment size limit (unknown). | team, SES owner | 4, 10 |
| 4 | **Who owns the Tableau token** (personal access token) and where it is rotated. It must be a separate token from the old refresh job's (D-017), ideally owned by a service user; only one session per token at a time. | Tableau admin | 4 |
| 5 | **Production DWH server name** as `@@SERVERNAME` and database name (read from the server; not verified). Used to edit the SQL guard line and to fill `DEV_DWH_SERVER` / `DEV_DWH_DATABASE` / `CONN_SERVER` / `CONN_DATABASE` in the production `.env`. No code change is needed. | data engineer, DBA | 4, 5, 6 |
| 6 | **How production recipients are allowed.** The "mail every subscriber" switch is for UAT only (D-050); production needs `SEND_ALLOWED_RECIPIENTS`, an exact comma list of addresses (no wildcards). D-052 item 5 says production should also mail whoever is subscribed in Tableau and that the switch "is to be extended to the production host at the production preflight". These two statements disagree. Decide before the second pilot step. | data engineer | 10 |
| 7 | Whether the repository will be committed/tagged before deployment. The working copy has uncommitted changes (checked 2026-10-26). Deploy a commit, not a working copy. | data engineer | 3 |

## 2. Prerequisites (host)

Checked on the dev machine; the target host is not checked.

| Item | Requirement | How to check on the host |
|---|---|---|
| Operating system | Windows Server with Task Scheduler (dev machine: Windows Server 2022). Host time zone **Singapore Standard Time** (the tasks use host local time; the scripts use Singapore time; the registration script warns if different). | `Get-TimeZone` |
| Python | 3.12, machine-wide (not inside a user profile, or the service account cannot run it). Dev uses 3.12.10. Other versions are untested. | `python --version` |
| Python packages | See step 3. The repository has **no `requirements.txt`**; the list comes from the imports of `scripts\*.py`. | after step 3 |
| ODBC driver | **ODBC Driver 17 for SQL Server** (the name in `.env.example`). Driver 18 is untested. | `Get-OdbcDriver -Name '*SQL Server*' \| Select Name` |
| Git | Needed only to clone (or copy the folder from a release). | `git --version` |
| Network to the warehouse | TCP 1433 from the host to the production DWH (name from decision 5). | `Test-NetConnection <dwh host> -Port 1433` |
| Network to Tableau | HTTPS (or HTTP on UAT port 8000) to the Tableau server, both the REST API and the Metadata API (`/api/metadata/graphql`). | `Test-NetConnection <tableau host> -Port 443` |
| Network to the mail server | Outbound SMTP with STARTTLS, port 587 (SES: `email-smtp.ap-northeast-1.amazonaws.com`; the Tableau UAT server itself uses port 25 without TLS, but the script uses 587 with STARTTLS). | `Test-NetConnection <smtp host> -Port 587` |
| Service account rights | "Log on as a batch job" on the host; Read and Execute on the install folder and python; Modify on `<install folder>\out`; Read on `.env`; DWH rights per [DB_CHANGES.md](DB_CHANGES.md) section 9. | server admin |
| Administrator for one step | Registering the Windows Event Log source (step 7). | |
| Disk | Logs are small (one file per script per day, kept 7 days); `out\` also holds backup JSON files of taken-over subscriptions and any `.eml` files from test runs. | |

## 3. Clone the repository, create the virtual environment, install packages

Run in an elevated or ordinary PowerShell on the host as the installing user. The repository URL below is a **placeholder**: replace it with the real one.

```powershell
# 3.1 clone (REPOSITORY_URL_PLACEHOLDER is not a real address)
git clone REPOSITORY_URL_PLACEHOLDER D:\Apps\TableauAutomation
cd D:\Apps\TableauAutomation
# deploy a fixed commit or tag, not a moving branch
git checkout <commit or tag>

# 3.2 virtual environment inside the install folder (the scheduler script looks for .venv\Scripts\python.exe)
python -m venv .venv

# 3.3 packages (versions = those installed on the dev machine on 2026-10-26, on which all checks passed)
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install tableauserverclient==0.42 pyodbc==5.3.0 python-dotenv==1.2.4 sqlglot==30.13.0 Pillow==13.0.0
```

What each package is for (from the imports of `scripts\*.py`):
- `tableauserverclient`: Tableau REST API (all Tableau scripts).
- `pyodbc`: warehouse access (all scripts that use the warehouse).
- `python-dotenv`: reads `.env` (`t2`, `t4`, and the preview/inventory helpers).
- `sqlglot`: parses custom SQL of data sources (`t2` only).
- `Pillow`: shrinks very wide report images (`subscription_mail.py`, used by `t6`).
- Optional: `pypdf` (counts PDF pages; without it a simple fallback is used; not installed on the dev machine). `aiosmtpd` is needed only for the local test mail sink (`preview_smtp.py`) and `t6_checks.py`, not for production.

Check the imports:
```powershell
.\.venv\Scripts\python.exe -c "import tableauserverclient, pyodbc, dotenv, sqlglot, PIL; print('imports ok')"
```
Expected: `imports ok`. (Not run on the host yet.)

Give the service account read/execute on the folder and modify on `out\`:
```powershell
New-Item -ItemType Directory -Force D:\Apps\TableauAutomation\out | Out-Null
icacls D:\Apps\TableauAutomation /grant "DOMAIN\svc_tableau:(OI)(CI)RX"
icacls D:\Apps\TableauAutomation\out /grant "DOMAIN\svc_tableau:(OI)(CI)M"
```
(Replace the account. `icacls` syntax not run on the host.)

## 4. Create `.env` from the template

```powershell
cd D:\Apps\TableauAutomation
Copy-Item .env.example .env
notepad .env        # fill in the values; never paste them into chat, tickets or this repository
# only the service account and administrators may read it
icacls .env /inheritance:r /grant:r "DOMAIN\svc_tableau:R" "BUILTIN\Administrators:F"
```

`.env` is gitignored. It holds the Tableau token and the SMTP password in clear text, so restrict it. **The template `.env.example` does not yet list `SEND_ALLOW_ALL_UAT`, `LOG_RETENTION_DAYS`, the takeover settings (`EXTRACT_TAKEOVER_*` below included) or `T4_REFRESH_SCOPE`; add them to your `.env` by hand from the table below.**

Key names and meaning (values come from the owners named; never written down here):

| Key | Meaning | Where the value comes from |
|---|---|---|
| `TABLEAU_SERVER_URL` | Base URL of the Tableau server the scripts use. Production: the `https://` form. UAT today: `http://tableau-uat.example.com:8000/`. | Tableau admin |
| `TABLEAU_SITE_ID` | Site content URL; empty for the Default site. | Tableau admin |
| `TABLEAU_PAT_NAME`, `TABLEAU_PAT_SECRET` | The personal access token the scripts sign in with. Separate from the old refresh job's token (D-017). Needs: read workbooks, views and subscriptions; export views; suspend/resume subscriptions; run extract refreshes. One session per token at a time: all scripts share one lock. | Tableau admin (decision 4) |
| `CONN_DRIVER` | ODBC driver, `{ODBC Driver 17 for SQL Server}`. | this document |
| `CONN_SERVER`, `CONN_DATABASE` | The warehouse server (fully qualified name) and database `CQGlobalSGDW`. **Must equal `DEV_DWH_SERVER` / `DEV_DWH_DATABASE` below** (case-insensitive; step 5). Windows authentication; no user or password. | DBA (decision 5) |
| `DEV_DWH_SERVER`, `DEV_DWH_DATABASE` | The warehouse guard of the Python scripts. The names say DEV but on production they hold the **production** server and database (the only ones the scripts may touch). Unset = every script refuses to run. | DBA (decision 5) |
| `UAT_TABLEAU_HOST` | Host of the UAT Tableau server; only used by the UAT-only `SEND_ALLOW_ALL_UAT` switch. **Leave out of the production `.env`** together with `SEND_ALLOW_ALL_UAT`. | UAT only |
| `MAIL_TRANSPORT` | `local` = every mail goes to a local test sink and **nobody is told anything**; `smtp` = real mail from every script. Default `local`. Keep `local` until the mail test in step 10 (stage 5); set `smtp` only then. | you |
| `SMTP_PROVIDER` | `ses`, `gmail` or `generic`. Decides which server replies are treated as "message certainly not queued". Only `ses` has a list (written from memory of SES behaviour, to be verified at the first live test). | you |
| `SMTP_HOST`, `SMTP_PORT` | SMTP server and port (587, STARTTLS). SES Tokyo: `email-smtp.ap-northeast-1.amazonaws.com`. | SES owner (decision 3) |
| `SMTP_USER`, `SMTP_PASSWORD` | SMTP credential (for SES: SMTP credentials created in AWS, not an IAM password). The older names `SES_SMTP_USER` / `SES_SMTP_PASSWORD` are still accepted. | SES owner |
| `SEND_FROM` | Sender address; must be verified at the mail server. | SES owner |
| `SEND_ALLOWED_RECIPIENTS` | **Production:** the exact comma-separated list of addresses that may receive mail. Required for any real send; every other recipient is dropped and counted. It must contain `ALERT_TO`. | you / team (decision 6) |
| `SEND_ALLOW_ALL_UAT` | `1` = no allow-list **on the UAT Tableau server only** (D-050). It is ignored (with a warning in every log) on any other host, and **must not be set in production**. Leave it out of the production `.env`. | UAT only |
| `ALERT_TO` | Where alerts and the daily summary go. Default `you@example.com` (D-018, changed by D-058). Must be a valid address and (production) on `SEND_ALLOWED_RECIPIENTS`. | team |
| `ADDR_HASH_SALT` | Any random text. Salts the short hash of the recipient list that appears in the logs, so addresses cannot be recovered from them. Unset while sending for real gives a startup warning. | generate: `[guid]::NewGuid()` |
| `LOG_RETENTION_DAYS` | Days of script log files kept (default 7, minimum 1). One file per script per day; each script deletes its own older files when it starts. Does not touch markers, alert state, takeover backups or any database table. | you |
| `TAKEOVER_WORKBOOK_IDS`, `TAKEOVER_SUBSCRIPTION_IDS` | Allow-list of what the takeover script may take over when it is run without the same options on the command line (`--workbooks`, `--subscription-ids`). Empty = nothing new is taken over. The scheduled live task carries its own allow-list on its command line (step 8), so normally leave these empty. | you |
| `TAKEOVER_OWNER_USER_IDS` | Tableau user ids whose subscriptions may be taken over (the "owner fence", required for every real takeover that uses an allow-list). Same as the `--owner-user-id` option. | Tableau admin / dry run output (`owner=`) |
| `TAKEOVER_MAX_NEW` | Most new takeovers per run (default 10). | you |
| `TAKEOVER_GONE_CLOSE_DAYS` | Days after which an open takeover whose subscription no longer exists in Tableau is closed (default 7). | you |
| `EXTRACT_TAKEOVER_DATASOURCE_IDS`, `EXTRACT_TAKEOVER_OWNER_USER_IDS`, `EXTRACT_TAKEOVER_MAX_NEW` | Allow-list of extract data source ids the extract takeover may take over, the Tableau user ids of their owners (required for a real run), and the most new takeovers per run (default 5). Empty allow-list = nothing new is taken over. The scheduled task carries its own list on its command line (step 9), so normally leave these empty. | you / Tableau admin |
| `T4_REFRESH_SCOPE` | `takeover` (default: refresh only extracts behind taken-over workbooks) or `all` (after the pilot). | you |
| Optional, defaults are fine: `SCHEDULE_MODE=honour`, `SCHEDULE_CATCHUP_DAYS=3`, `MAPPING_MAX_AGE_H=26`, `EXPORT_PAUSE_SEC=20`, `MAX_SENDS_PER_RUN=10`, `MAX_CONCURRENT_REFRESHES=1`, `REFRESH_STAGGER_SEC=60`, `TABLEAU_BUSY_CHECK=1`, `TABLEAU_BUSY_STALE_HOURS=6`, `T2_PACE_SEC=0.5`, `T2_METADATA_PAGE=200`, `T2_LOCK_RETRIES=10`, `T2_MAX_LOCK_MIN=10`, `T4_MAX_LOCK_MIN=10`, `PAT_BUSY_ALERT_AFTER=4`, `T6_NOT_READY_CUTOFF=15:30`, `EVENTLOG_DISABLED`, `KILL_FILE`, `TABLEAU_AUTOMATION_DISABLED` (kill switch). | | |

Dry-run tasks and smoke tests need **no** SMTP values (they use the local sink). Start with `MAIL_TRANSPORT=local`.

## 5. The warehouse guards: values to set (no code change)

The scripts and every SQL file refuse to run unless the server is the one named in the guard. Details and the list of files are in [DB_CHANGES.md](DB_CHANGES.md) section 4. In this order, before steps 6 onwards touch production:

1. Record `SELECT @@SERVERNAME, DB_NAME()` on the production DWH (read-only preflight, [PROD_PREFLIGHT.md](PROD_PREFLIGHT.md) section 1).
2. **Python scripts:** in the production `.env` set `DEV_DWH_SERVER` and `DEV_DWH_DATABASE` to the production server (fully qualified) and database, and `CONN_SERVER` / `CONN_DATABASE` to the same text (case-insensitive). After connecting, `@@SERVERNAME` must start with the first label of `DEV_DWH_SERVER`. The scripts need no edit.
3. **SQL files:** one search-and-replace in `scripts/sql` (not `T1_tests.sql`): `NOT IN ('<DEV_DWH_SERVER>')` becomes `NOT IN ('<production @@SERVERNAME>')`, so 17 files change; then update the two checks that assert the name (`extract_checks.py`, `t11_checks.py`) and re-run all eight checks files. That edit is the DBA's or the data engineer's, with a second person looking at the diff.
4. Until then **every script stops at start** on production ("Refusing to run: only the dev DWH ... is allowed") and every SQL file prints its error and does nothing. That is the designed fail-safe, not a fault.

## 6. Apply the database changes (with the DBA) - BEFORE any script runs

Follow [DB_CHANGES.md](DB_CHANGES.md): the summary table at its top, then sections 3 and 9. The objects are created from the DBJR document (ITSC_384043_TableauEnhancement_ML); the creation scripts are no longer shipped in this repo (only rollbacks and `T3_load_registry.sql` are), then the single grant script for the service account. Nothing in steps 8 to 10 may run before this is done. Then verify, as the DBA:

```sql
SELECT CAST(value AS nvarchar(60)) AS Tag, COUNT(*) AS Objects
FROM sys.extended_properties WHERE class = 1 AND minor_id = 0 AND name = 'created_by'
GROUP BY CAST(value AS nvarchar(60));          -- expect T1_mapping_schema 12, R1_schedule_schema 1, T11_pause_schema 1, ExtractAuto_schema 4
SELECT COUNT(*) AS RegistryRows FROM Integration.Tableau_PipelineTable;   -- expect 33 (after validating the registry against production)
SELECT COL_LENGTH('Integration.Tableau_ExtractTaskBackup', 'NoRetake') AS NoRetakeLen,
       COL_LENGTH('Integration.Tableau_ExtractTaskBackup', 'OldJobMappingIds') AS OldJobLen;   -- both not NULL
```

Nothing else in this plan writes to the warehouse until step 10, stage 3 (the first real mapping sync).

## 7. Register the Windows Event Log source (as administrator, once)

The scripts write a second alert signal that does not depend on mail to the Windows Application log, source `TableauAutomation` (event id 100 = error, 101 = warning).

```powershell
# elevated PowerShell on the host
eventcreate /L APPLICATION /T INFORMATION /ID 1 /SO TableauAutomation /D "registered"
Get-WinEvent -FilterHashtable @{ LogName = 'Application'; ProviderName = 'TableauAutomation' } -MaxEvents 1
```
Expected: one information event with the text `registered`. **Not verified:** that the service account (not an administrator) can write events under this source with `eventcreate`; if it cannot, the scripts log a warning and continue, and the log-file and mail signals remain.

## 8. Smoke tests in dry-run (no writes, no mail)

Prerequisites: step 5 done (guard values set), step 6 done (SQL applied, grants given), `.env` filled with `MAIL_TRANSPORT=local`. Run as the **service account** (for example with `runas` or a one-off scheduled task) because that tests its rights; run **one script at a time** (they share one Tableau login lock; a second one skips with `skipped=pat_busy`). Dry runs do sign in to Tableau (to read the live subscription) but write nothing.

```powershell
cd D:\Apps\TableauAutomation
$py = '.\.venv\Scripts\python.exe'
& $py scripts\t2_mapping_sync.py --dry-run
& $py scripts\t11_pause.py --dry-run
& $py scripts\extract_takeover.py --dry-run
& $py scripts\t4_refresh.py --dry-run
& $py scripts\t6_send.py --dry-run
& $py scripts\daily_summary.py
```

What to expect (taken from the dev logs of 2026-10-26; production numbers will differ):

| Script | First log line | Last log line |
|---|---|---|
| `t2_mapping_sync.py --dry-run` | `MAIL TRANSPORT local for t2 (dry-run): nothing is sent for real (dry run)`, then `Tableau read (read-only): N workbooks with subscriptions, M in scope ...` | `RUN SUMMARY mode=dry-run exit=0 workbooks=... problems=0 needs_human=0` |
| `t11_pause.py --dry-run` | `MAIL TRANSPORT local for t11 (dry-run) ...` | `RUN SUMMARY mode=dry-run transport=local allow_list_workbooks=0 allow_list_subscriptions=0 owned=0 paused=0 ... failed=0 ...` (with an empty allow-list nothing new is taken over) |
| `extract_takeover.py --dry-run` | `MAIL TRANSPORT local for extract_takeover (dry-run) ...` | `RUN SUMMARY mode=dry-run ... allow_list=0 ... orphaned=0 ... needs_human=0` (with an empty allow-list nothing is taken over; an orphan, if any, is reported and mailed through the alert transport even in a dry run) |
| `t4_refresh.py --dry-run` | `MAIL TRANSPORT local for t4 (dry-run) ...`, `DRY run for Singapore date <today>: ...` | `RUN SUMMARY mode=dry-run refreshed=0(would) waiting=... skipped=0 failed=0 lookup_failed=0 schedule_mode=honour` |
| `t6_send.py --dry-run` | `MAIL TRANSPORT local for t6 (dry-run) ...`, `DRY run for Singapore date <today>, transport=local: N candidate subscription(s); ETL latest rows today: {...}` | `RUN SUMMARY mode=dry-run transport=local candidates=... sent=0 would_send=...` |
| `daily_summary.py` | `MAIL TRANSPORT local for daily_summary (dry-run) ...` | prints the summary text, then `RUN SUMMARY daily_summary mode=dry-run attention=N (not sent)` |

Before the first real mapping sync (step 10, stage 3) the mapping tables are empty, so the `t6`/`t4` dry runs show 0 candidates: that is correct.

Also check:
- The first line of every log says `MAIL TRANSPORT local ...`. If it says `smtp`, stop.
- No line says `SEND_ALLOW_ALL_UAT=1 is ON` on production. If it appears, remove the key from `.env`.
- `review_list.py` (read-only, writes `out\review_list.csv`): `& $py scripts\review_list.py`. Needs the mapping to have been synced.
- **Known defect found while writing this plan (2026-10-26):** `scripts\daily_summary.py` crashes with `TypeError: not enough arguments for format string` when the send log has a `Failed`, `Claimed` or `TimedOut` row for today, or when a takeover is halted (lines 160 and 162 format a database row with `%`; a tuple is needed). On exactly the days when something went wrong, the summary mail would not be built. Fix before relying on the summary. Verified by reproducing the formatting error with a database row, not by a full run with such rows.

## 9. Register the scheduled tasks

Script: `scripts\schedule\register_tasks.ps1`. **A real registration has never been run** (only the preview with `-WhatIf` and a parse check). Expect to find small problems on the first real run; check the task in the Task Scheduler window afterwards (a checklist is below).

Default = **dry-run tasks**: the command lines carry no `--execute` (`t2` and `extract_takeover` carry `--dry-run`, because `t2` writes by default; the summary has no `--execute`). Nothing is written, paused, refreshed or mailed.

Times and cadence (host local time, which must be Singapore time):

| Task (folder `\TableauAutomation`) | When | Time limit |
|---|---|---|
| `t2_mapping_sync` | daily 07:50 (a missed run starts when the host is back) | 30 min |
| `extract_takeover` | daily 08:20; **a dry run unless enabled** (see below) | 10 min |
| `t11_pause` | 08:30, then every 15 min, last start 16:30 | 10 min |
| `t4_refresh` | 08:32, then every 15 min, last start 16:32 | 40 min |
| `t6_send` | 08:34, then every 15 min, last start 16:34 | 55 min (its own budget is 25 min) |
| `watchdog` | 08:45, then every 15 min, last start 17:15 | 5 min |
| `daily_summary` | daily 17:30 | 10 min |

All tasks: do not start a second copy while one is running (`IgnoreNew`); a run that is still going when the window ends is not killed (only its time limit stops it).

```powershell
cd D:\Apps\TableauAutomation
# 1. preview, changes nothing
.\scripts\schedule\register_tasks.ps1 -RepoRoot D:\Apps\TableauAutomation -ServiceAccount 'DOMAIN\svc_tableau' -WhatIf
# 2. create the DRY-RUN tasks (asks for the account password; none for a group managed service account DOMAIN\name$)
.\scripts\schedule\register_tasks.ps1 -RepoRoot D:\Apps\TableauAutomation -ServiceAccount 'DOMAIN\svc_tableau'
# 3. look at them
Get-ScheduledTask -TaskPath '\TableauAutomation\' | Get-ScheduledTaskInfo
```
Expected from the preview: one line per task with the python path and arguments such as `-u "D:\Apps\TableauAutomation\scripts\t6_send.py" --dry-run`, and `Mode: DRY-RUN`. Registration **fails** (it does not warn) if `.env`, one of the scripts or a runnable python is missing.

Checklist after the first real registration (all unverified): the task description says `mode=DRY-RUN`; the action arguments contain no `--execute`; in each repeating task "Stop all running tasks at end of repetition duration" is **unticked**; the 16:30/16:32/16:34 slots fire; after two full working days of dry runs there is no overlap and every log ends with a `RUN SUMMARY` line.

**Going live** is a deliberate second step, only after the pilot is signed off (step 10, stage 6): re-run the script with `-Live` and a takeover allow-list:
```powershell
.\scripts\schedule\register_tasks.ps1 -RepoRoot D:\Apps\TableauAutomation -ServiceAccount 'DOMAIN\svc_tableau' `
    -Live -T11ExtraArgs '--subscription-ids <subscription id> --owner-user-id <Tableau user id>'
```
`-Live` adds `--execute` to `t11`, `t4`, `t6`, `watchdog` and `daily_summary` and drops `--dry-run` from `t2`. **`extract_takeover` stays a dry run even with `-Live`** unless `-ExtractExtraArgs '--datasource-ids <data source id> --owner-user-id <Tableau user id>'` is also given (deleting Tableau extract refresh tasks is its own go-ahead; RUNBOOK section 20). Even as a dry run it mails the alert for a taken-over extract that nothing refreshes (through `MAIL_TRANSPORT`). It **refuses** without an allow-list (`--workbooks <guid[,guid]>` or `--subscription-ids <ids>`) and without `--owner-user-id`, and asks for confirmation (`-Force` skips only the question). Only those three options are accepted. Real mail additionally needs `MAIL_TRANSPORT=smtp` in `.env`. Re-running replaces tasks but keeps a task that was Disabled, and prints `mode DRY-RUN -> LIVE` per task.

## 10. Staged rollout

Each stage needs the user's explicit go-ahead before it touches anything real (project rule). Stop at the first red flag.

**Stage 1. Production preflight (read-only).** Work through [PROD_PREFLIGHT.md](PROD_PREFLIGHT.md): production server name, load-log columns and time zone, Tableau REST version, HTTPS, token role and rights, schedule time zone, the seven views resolve to the same base tables as on dev. Write the answers in its sign-off table. Anything that differs from dev goes back as a decision, not a quick fix.

**Stage 2. Guards, SQL, installation** (steps 3 to 9 above), with dry-run tasks running for at least two working days.

**Stage 3. First real mapping sync.** `& $py scripts\t2_mapping_sync.py` (no `--dry-run`): writes only the new mapping tables. Then `review_list.py`. Expect `RUN SUMMARY mode=execute exit=0 ... needs_human=0`. Run it once by hand; the first real `t2` has never been run against production.

**Stage 4. Clean up before the first takeover** (RUNBOOK section 14). The scripts send **every** in-scope subscription that still exists in Tableau, suspended or not. Read `out\review_list.csv` and delete in Tableau what must not be sent; re-run `t2` and `review_list` until the list is what should go out. Take over at least one day before a subscription's first due date, never on the due day (a subscription taken over after its time on its due day is skipped for that occurrence).

**Stage 5. Production mail path.** Decision 3 first. Then test the mail path alone: set `MAIL_TRANSPORT=smtp`, `SMTP_*`, `SEND_FROM`, `SEND_ALLOWED_RECIPIENTS=<your own address>`, `ALERT_TO=<your own address>`, `ADDR_HASH_SALT`, and run `& $py scripts\daily_summary.py --execute --smtp`: one real mail to your own address, which also tests login, sender and the allow-list. Do not start any other real step while testing. SES sandbox, verified sender and the "not queued" reply text are unverified; watch the first replies. Put `MAIL_TRANSPORT=local` back until the pilot starts.

**Stage 6. One pilot workbook to the owner's own address.** One workbook, one subscription whose owner is you, and the allow-list holding only your address.
1. Dry run: `& $py scripts\t11_pause.py --subscription-ids <id> --owner-user-id <Tableau user id>` (shows `would back up and pause ... owner=`).
2. Take over: add `--execute`. This **writes to Tableau** (backup row first, then pause). Compare the subscription in the Tableau web UI before and after: subject, message, format, schedule, "send if view is empty". The Tableau client library does not send the message field when pausing; whether Tableau keeps it is unverified, and the script halts and alerts if anything else changed.
3. Send: `& $py scripts\t6_send.py --execute --smtp --subscription-id <id>` once its data is ready on a due day (a taken-over subscription is not sent on its takeover day if the schedule time has passed). Compare the mail with what Tableau itself would have sent. Run it again straight away: expect `already_sent`, no second mail.
4. Keep it or give it back: `& $py scripts\t11_pause.py --restore <id> --execute` (refused if it was already sent today unless `--allow-restore-after-send`).
5. Then, and only then, register the live tasks for that one subscription (step 9, `-Live`).

**Stage 6b. Extract takeover (optional, own go-ahead).** The old refresh job (D-017) refreshes extracts as soon as one ETL has succeeded; the takeover switches it off for exactly the extracts it takes over (`Active_Flag = 0` in `ETL_Tableau_ExtractMapping`, D-080) and a restore switches it back, so the service account needs `SELECT, UPDATE` on that table (DB_CHANGES section 9). RUNBOOK section 21 has the short version. Then: `& $py scripts\extract_takeover.py` (dry run, lists candidates), one extract with `--datasource-ids <id> --owner-user-id <id>` (dry run), then `--execute` for that one extract, and register the live task with `-ExtractExtraArgs`. Give back with `--restore`.

**Stage 7. More.** Add subscriptions in small groups: `--subscription-ids` lists or a workbook id, one `--owner-user-id` per owner, at most 10 new takeovers per run (`TAKEOVER_MAX_NEW`); re-register with `-Live` and the larger allow-list. Set `T4_REFRESH_SCOPE=all` only after the pilot. The broader recipient list needs decision 6.

## 11. Monitoring

- **Daily summary mail** at 17:30 to `ALERT_TO`: what was sent, what was due but not sent and why, refreshes, re-pauses, halted takeovers, errors, scripts that did not run. Fix the defect noted in step 8 first.
- **Alert mails** during the day (one per problem per day): a taken-over extract that nothing refreshes (also from the dry-run `extract_takeover` task), export failure after 3 tries, repeated mail failure, an ambiguous send, a claim that timed out, no allowed recipient, mapping outdated, "not ready after 15:30", and so on.
- **Windows Event Log** (second signal, independent of mail): source `TableauAutomation`, event id 100 (error) / 101 (warning) for every non-zero run, kill-switch run and long Tableau-login-busy streak. Point your monitoring at it. The daily summary lives on the same mail path, so also keep one monitor that does not (RUNBOOK section 1, "dead-man check").
- **Log files** in `out\`: `t2_sync_<date>.log`, `extract_takeover_<date>.log`, `t11_pause_<date>.log`, `t4_refresh_<date>.log`, `t6_send_<date>.log`, `daily_summary_<date>.log`; each script deletes its own files older than `LOG_RETENTION_DAYS` (default 7). Each run ends with one `RUN SUMMARY` line; `needs_human=0` means nothing to do.
- **Last-run markers** `out\lastrun\<script>.json` (host-local), and Task Scheduler's own history.
- Morning routine (5 minutes): RUNBOOK section 4.

## 12. Kill switch

Fastest, no admin rights: create an empty file `out\KILL` (or set `TABLEAU_AUTOMATION_DISABLED=1` in `.env`):
```powershell
New-Item D:\Apps\TableauAutomation\out\KILL -ItemType File
```
All scripts then log `KILL SWITCH ACTIVE`, send one alert a day, do nothing and exit 0; the daily summary keeps working. Delete the file to resume. **The kill switch stops the scripts, not Tableau:** taken-over subscriptions stay paused in Tableau, so those reports stop arriving. If it will be on for more than a day, hand them back (next section). To stop the tasks themselves: `Get-ScheduledTask -TaskPath '\TableauAutomation\' | Disable-ScheduledTask`.

## 13. Rollback and restore of taken-over subscriptions

1. Stop the scripts (kill switch or disable the tasks).
2. Give the subscriptions back to Tableau (works even with the kill switch on):
```powershell
& $py scripts\t11_pause.py --restore <subscription id> --execute
& $py scripts\t11_pause.py --restore-all-for-workbook <workbook id> --execute
```
   This marks the backup row restored first, then un-suspends the subscription in Tableau. It is refused for a subscription that was sent today (a second mail could follow from Tableau's own schedule) unless `--allow-restore-after-send`. Afterwards check in the Tableau UI that subscription, schedule, subject and format are as before. A subscription that was suspended in Tableau before the takeover comes back **active**: the script does not remember the original state.
3. Remove the tasks: `.\scripts\schedule\register_tasks.ps1 -Unregister` (add `-WhatIf` to preview).
4. Give back every taken-over extract task first if any: `& $py scripts\extract_takeover.py --restore-all --execute`.
5. Only if the database objects must go too: the SQL rollbacks in reverse order ([DB_CHANGES_DETAIL.md](DB_CHANGES_DETAIL.md) section 8: ExtractAuto, R8, R5, R4, R3, T11, R1, T1), **after** the code is stopped. Leaving them in place is harmless.

A subscription deleted in Tableau cannot be restored by the script. The backup table and `out\t11_backup\*.json` hold the old definition, but re-creating it is manual.

## 14. What has NOT been tested yet

- A **real registration** of the scheduled tasks (only `-WhatIf` and a parse check), including the live-mode confirmation prompt, a Disabled task staying Disabled, the last 16:30/16:32/16:34 slots and the gMSA form.
- **Anything on production:** the production warehouse, the production Tableau server (REST version, HTTPS, token rights, schedule time zone), the registry rows against production metadata.
- **The warehouse guards on production:** the values to set (step 5) have not been set or tried on the production host; the SQL guard edit has not been made or reviewed.
- **The extract takeover** has never taken over a task (the extract task backup table on dev is empty); neither delete nor restore has run against a real Tableau task in this project, and the automatic registry has never been applied on dev (33 `Manual` rows only).
- **Real mail through SES** (login, sandbox, verified sender, size cap, throttling, the list of "not queued" replies). Only a local test server and, on 2026-10-23 and 2026-10-26, the project owner's Gmail account have been used.
- A real **Windows Event Log** write by the service account (the checks fake it).
- **Restricted database permissions:** every run so far used a login with full rights on dev.
- **Running as the service account** at all, from the install folder, with `.env` readable only by it.
- A real **pause and restore** of other people's subscriptions (only the project owner's own test subscriptions on UAT have been paused).
- Whether Tableau keeps a subscription's **message** when it is paused (the client library omits that field), and whether a workbook's change time moves when its embedded extract refreshes (LIMITATIONS).
- The **first extract refresh** the script submits (`t4 --execute`) against a real job queue; UAT cannot refresh because its data sources point at a server it cannot reach.
- The `daily_summary` crash noted in step 8.

## 15. Quick reference: the commands

| Purpose | Command |
|---|---|
| Mapping sync, dry run / real | `python scripts\t2_mapping_sync.py --dry-run` / `python scripts\t2_mapping_sync.py` |
| Takeover, dry run / real | `python scripts\t11_pause.py --subscription-ids <id> --owner-user-id <id>` / add `--execute` |
| Keep paused (what the task does) | `python scripts\t11_pause.py --execute` |
| Restore | `python scripts\t11_pause.py --restore <id> --execute` |
| Extract takeover, dry run / one extract / give back | `python scripts\extract_takeover.py` / `... --datasource-ids <id> --owner-user-id <id> --execute` / `... --restore <task id> --execute` |
| Refresh, dry run / real | `python scripts\t4_refresh.py` / `python scripts\t4_refresh.py --execute` |
| Send, dry run / real | `python scripts\t6_send.py` / `python scripts\t6_send.py --execute --smtp` (or `MAIL_TRANSPORT=smtp`) |
| Daily summary | `python scripts\daily_summary.py` (print) / `--execute --smtp` (mail) |
| Review list for the clean-up | `python scripts\review_list.py` |
| Kill switch on / off | `New-Item out\KILL -ItemType File` / `Remove-Item out\KILL` |
