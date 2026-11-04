# Production preflight (P3): read-only checklist

Nothing in here writes anything. It still needs the owner's go-ahead before it is run against production (CLAUDE.md rule 6). Work through it top to bottom and write the answers next to each step. Stop at the first red flag.

Ground rules
- Every SQL below is a SELECT. Run it with your own Windows login (Integrated Security) in SSMS, read-only. Do not run any INSERT, UPDATE, DELETE, DDL or EXEC.
- Only one Tableau session per token at a time (a second sign-in kills the first). Run the Tableau steps when no script is running and not with the old refresh job's token (D-017).
- Never paste the token or the `.env` values anywhere. The Python snippet in section 3 reads them from the environment and prints no secret.
- The first dry run of any script against production is not part of this list. Section 1 explains why the scripts will refuse to start there.

## 1. Production DWH server name (the hard-coded dev guards)

Why it matters: the scripts refuse to run unless the server and database match the warehouse guard. The guard is in these places (details: docs/DB_CHANGES.md section 4); none needs a code change, only values:
- Python: the keys `DEV_DWH_SERVER` and `DEV_DWH_DATABASE` in `.env` (read by `scripts/t2_mapping_sync.py` and `scripts/t4_refresh.py`; the other scripts call `t4_refresh.connect()`). `CONN_SERVER` / `CONN_DATABASE` must equal them (case-insensitive), and after connecting `@@SERVERNAME` must start with the first label of `DEV_DWH_SERVER`. On production the operator puts the production values in these keys (the names say DEV).
- SQL: every file in `scripts/sql/` (19 deployable files) has the literal `IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') ... SET NOEXEC ON` on line 3: one search-and-replace of the server name, not in `T1_tests.sql`. `extract_checks.py` and `t11_checks.py` assert that name and need the same edit.

Run on the production DWH (read-only):
```sql
SELECT @@SERVERNAME AS ServerName,
       CAST(SERVERPROPERTY('MachineName') AS sysname) AS MachineName,
       CAST(SERVERPROPERTY('ServerName') AS sysname) AS ServerPropertyName,
       DB_NAME() AS DbName,
       CAST(SERVERPROPERTY('ProductVersion') AS varchar(30)) AS SqlVersion;
```
Expect: database name `CQGlobalSGDW` (same as dev). Write down the exact `@@SERVERNAME` and the fully qualified DNS name used in the Tableau connection. Watch for: `@@SERVERNAME` differing from the DNS name (the code compares the first part of the DNS name with `@@SERVERNAME`, so a rename, an alias, an availability-group listener or a cluster name would fail the second check); a different database name; a named instance (backslash).

Also record: which login the scheduled task will run as, and that it has SELECT on `Integration` and `Tableau` schemas and VIEW DEFINITION (the view resolver uses `sys.sql_expression_dependencies`, which returns nothing for objects the login cannot see).

## 2. ETL_Master_Log semantics on production

Dev facts the code relies on (t4_refresh.py header): columns `JobID, PipelineName, EndTime (datetime2, DWH local time = Singapore), Success (bit)`; three pipelines named `CQGlobalSGDW: DailyETL`, `CQGlobalSGDW: NovaDailyETL`, `CQGlobalSGDW: LeadGenETL`; the gate takes the LATEST row of the Singapore day per pipeline; no ETL after 16:30 (D-029).

2a. Columns and types.
```sql
SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'Integration' AND TABLE_NAME = 'ETL_Master_Log' ORDER BY ORDINAL_POSITION;
```
Expect JobID, PipelineName, EndTime datetime2 NOT NULL, Success bit. Watch for: EndTime as datetimeoffset or a UTC column, a new StartTime/Status column, Success nullable.

2b. Pipeline names and success pattern.
```sql
SELECT PipelineName, COUNT(*) AS Rows_, MIN(EndTime) AS FirstRow, MAX(EndTime) AS LastRow,
       SUM(CASE WHEN Success = 1 THEN 1 ELSE 0 END) AS Ok, SUM(CASE WHEN Success = 0 THEN 1 ELSE 0 END) AS Failed,
       SUM(CASE WHEN Success IS NULL THEN 1 ELSE 0 END) AS NullSuccess
FROM Integration.ETL_Master_Log GROUP BY PipelineName ORDER BY PipelineName;
```
Expect the same three names, exact text including the `CQGlobalSGDW: ` prefix. Watch for: other or differently spelled pipelines (the registry then points at nothing, the readiness is "no run today" and reports never send); `Failed = 0` over a long history (on dev every row had Success = 1, so a failed run may not be logged at all: a failure then looks like "no row today", which the gate treats as pending, safe); NULL Success.

2c. Rows per pipeline per day, last 30 days (reruns, late runs).
```sql
SELECT CAST(EndTime AS date) AS Day_, PipelineName, COUNT(*) AS Runs,
       MIN(CAST(EndTime AS time(0))) AS FirstEnd, MAX(CAST(EndTime AS time(0))) AS LastEnd,
       SUM(CASE WHEN Success = 0 THEN 1 ELSE 0 END) AS Failed
FROM Integration.ETL_Master_Log WHERE EndTime >= DATEADD(day, -30, SYSDATETIME())
GROUP BY CAST(EndTime AS date), PipelineName ORDER BY Day_ DESC, PipelineName;
```
Watch for: more than one row a day (a later failure row blocks the day, a later success unblocks it: confirm that matches how the team reruns); runs ending after 16:30 or before midnight-rolled (the 2026-10-13 23:03 NovaDailyETL run on dev belongs to that day's date, check prod's Nova timing); days with no row on weekends/holidays (monthly and weekend reports then wait for the catch-up window of 3 days).

2d. Time zone of EndTime: compare with the server clock and with the Agent job's own record.
```sql
SELECT SYSDATETIME() AS ServerLocalNow, SYSUTCDATETIME() AS UtcNow, SYSDATETIMEOFFSET() AS WithOffset,
       DATEPART(TZoffset, SYSDATETIMEOFFSET()) AS OffsetMinutes;      -- t4 aborts unless this is 480
SELECT TOP (10) j.name, h.run_date, h.run_time, h.run_duration, h.run_status
FROM msdb.dbo.sysjobhistory h JOIN msdb.dbo.sysjobs j ON j.job_id = h.job_id
WHERE h.step_id = 0 AND j.name IN ('DailyETL', 'NovaDailyETL', 'LeadGenETL')
ORDER BY h.instance_id DESC;
```
Expect OffsetMinutes = 480 and the last `EndTime` of a pipeline within a minute or two of the Agent job's finish time (`run_date`/`run_time` are HHMMSS integers in server local time; `run_status` 1 = succeeded, 0 = failed). Watch for: offset not 480 (the script then refuses to start by design), or EndTime 8 hours off the job history (EndTime is UTC; the whole gate would be wrong and must be changed before anything runs). Also check any `run_status = 0` job day: does `ETL_Master_Log` have a Success = 0 row for it, or nothing?

2e. Sanity: are the prod tables the registry expects present and in the three pipelines (the dev registry has 33 rows)?
```sql
SELECT s.name AS SchemaName, o.name AS ObjectName, o.type_desc
FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id
WHERE (s.name = 'Dimension' AND o.name IN ('AE','Client','MultipleAccounts','MultipleAccounts_ManualImportData','Account','Instrument','Platform','PM_ExchangeRate','Application_Remark_K2','ApplicationInfo_K2'))
   OR (s.name = 'Fact' AND o.name IN ('DastFinancialSummary','DastTradeConfirmationPNL','DastOpenPositionFinal','DailyNovaTradeVolume'))
   OR (s.name = 'Integration' AND o.name IN ('MultipleAccounts_Staging','DefaultFxTradeSize_Staging','SharesLotSize_Staging','PreciousMetal_Staging','Nova_ClientFund_Staging'))
ORDER BY 1, 2;
```
(A spot check; the full list is `scripts/sql/T3_load_registry.sql`.) Expect all rows to exist as user tables.

## 3. Tableau: REST version, HTTPS, token rights

Fill in the production values: `TABLEAU_SERVER_URL` (the key name in `.env`), site id key `TABLEAU_SITE_ID`.

3a. REST version and product version (no sign-in needed).
```powershell
Invoke-RestMethod -Uri "https://<prod host>/api/3.25/serverinfo" -Headers @{Accept='application/json'} | ConvertTo-Json -Depth 5
```
Expect `restApiVersion` of 3.25 or higher (UAT is 3.25, product 2025.1.7; the library is told to use the server's version, `use_server_version=True`). If 3.25 gives 404, drop the version number to a lower one (the call also tells you the real version). Watch for: an older server where schedules, subscription update or the Metadata API behave differently; each of those would need a test, not a guess.

3b. HTTPS. Same call with `http://` and with `https://`. Expect `https` to work with a certificate trusted by the host that will run the scripts (no `-SkipCertificateCheck` needed), and `TABLEAU_SERVER_URL` set to the `https://` form. Watch for: http only (UAT is http, so the token crosses the network in clear text; on production use https, or accept it knowingly), or an internal CA the scheduler host does not trust.

3c. Sign-in, role and rights. Use one small read-only Python snippet; it prints no secret:
```python
import os
from dotenv import load_dotenv
import tableauserverclient as TSC
load_dotenv(".env")
auth = TSC.PersonalAccessTokenAuth(os.environ["TABLEAU_PAT_NAME"], os.environ["TABLEAU_PAT_SECRET"], os.environ.get("TABLEAU_SITE_ID", ""))
srv = TSC.Server(os.environ["TABLEAU_SERVER_URL"].strip(), use_server_version=True)
with srv.auth.sign_in(auth):
    me = srv.users.get_by_id(srv.user_id)
    print("rest version:", srv.version, "| site role:", me.site_role)
    subs = list(TSC.Pager(srv.subscriptions))
    print("subscriptions visible:", len(subs), "| distinct owners:", len({s.user_id for s in subs}))
    print("workbooks visible:", len(list(TSC.Pager(srv.workbooks))))
    print("site disable_subscriptions:", getattr(srv.sites.get_by_id(srv.site_id), "disable_subscriptions", "unknown"))
```
Run it with no other script running, then sign-in ends with the `with` block.
Expect: site role `ServerAdministrator` or `SiteAdministratorCreator` (Explorer-level admin may not be enough for "see all and refresh all"); subscriptions visible equal to the count shown by an administrator in the Tableau UI, with more than one distinct owner; `disable_subscriptions` False (on UAT it is True and blocks creating subscriptions, errorCode 150008). Watch for: a non-admin role (you then see only your own subscriptions: the script would silently take over almost nothing); counts lower than the UI; the token belonging to the old refresh job (D-017 asks for a separate token for this project) or to a personal account.

3d. Metadata API (T2 depends on it).
```powershell
# POST body: {"query":"{ workbooks(first: 1) { luid name } }"}, to https://<prod host>/api/metadata/graphql
```
Run with the `X-Tableau-Auth` header from a signed-in session (easiest: in the same Python session use `srv.metadata.query('{ workbooks(first: 1) { luid name } }')`). Expect a workbook back. Watch for: 403 (Metadata services not enabled; UAT answered 403 at first), an empty list on a server whose indexing is not finished, or an error mentioning permissions.

3e. Rights that cannot be proved without a write or a render, so do not guess:
- Refresh all extracts: site role evidence from 3c only; the real proof is the first `t4 --execute` on the pilot extract.
- Export all views: needs View + Export Image/PDF on every in-scope workbook. Proof is the pilot dry run plus one export of the pilot workbook; a render is read-only for data but loads the server, run only with go-ahead.
- Suspend/resume other owners' subscriptions: proof is the pilot pause of your own subscription first (owner fence), then another person's only after that.
Token expiry date and who owns the token: write them down (RUNBOOK section 8).

## 4. Schedule time zone

The code assumes Tableau schedules fire in Singapore time (LIMITATIONS: "assumed, not verified on prod"). Time of day is ignored by the due rule, but the DAY a monthly/weekly report is due and the takeover-day guard both depend on it.

4a. On the Tableau host (read-only): `Get-TimeZone` (or `tzutil /g`). Expect `Singapore Standard Time`. Watch for UTC or another zone: a 11:00 schedule shown in the UI would then be a different local day for the recipients.

4b. List the schedules and compare with the Tableau web UI for the same schedule (Settings, Schedules):
```python
# inside the same signed-in session as 3c
for s in TSC.Pager(srv.schedules):
    print(s.name, s.schedule_type, s.frequency, s.interval_item, s.next_run_at)
```
(Run it in the 3c session; it reads only.) Expect the `next_run_at` and start times to match the UI to the minute. Watch for a fixed offset between API and UI (for example 8 hours: the API is UTC, and the due-day logic in `schedule_due.py` must be checked against that).

4c. Cross-check with real behaviour: pick a subscription whose last mail is visible in the UI or a recipient mailbox, and compare the mail's sent time with its schedule. Expect it to land within the hour of the schedule in Singapore time.

4d. The 22 monthly 11:00 subscriptions: confirm the day-of-month and whether any use day 29-31 or "last day". Takeover must happen at least one day before the first due date, never on it (RUNBOOK section 14).

## 5. View dependencies re-resolved against production

T2 resolves each Tableau view to its base tables with the recursive query below (`RESOLVE_SQL` in `t2_mapping_sync.py`), and the registry maps those tables to pipelines. If production views differ from dev, the mapping is wrong without any error.

Run for each of the seven root views (change the two names): `Tableau.vw_DataSource_PNL`, `Tableau.vw_DataSource_PNL_ParentAccountFix`, `Tableau.vw_DataSource_DastOpenPosition`, `Tableau.vw_DataSource_ClientFinancialSummary`, `Tableau.vw_DailyNovaVolume`, `Tableau.vw_DailyOAOApplication_Filtered`, `dbo.vw_Application_Remark_K2`.
```sql
DECLARE @schema sysname = N'Tableau', @name sysname = N'vw_DataSource_PNL';
WITH r AS (
  SELECT o.object_id, o.type FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id
  WHERE s.name = @schema AND o.name = @name
  UNION ALL
  SELECT o2.object_id, o2.type FROM r
  JOIN sys.sql_expression_dependencies d ON d.referencing_id = r.object_id AND r.type = 'V'
  JOIN sys.objects o2 ON o2.object_id = d.referenced_id
)
SELECT DISTINCT s.name AS SchemaName, o.name AS TableName
FROM r JOIN sys.objects o ON o.object_id = r.object_id JOIN sys.schemas s ON s.schema_id = o.schema_id
WHERE r.type = 'U' ORDER BY 1, 2;
```
Expect the same base tables as dev (docs/T3_mapping_proposal.md section "views": for example `vw_DataSource_PNL` gives dbo.SalesChannel_Excel, Dimension.AE, Client, MultipleAccounts, MultipleAccounts_ManualImportData, Fact.DastFinancialSummary, DastTradeConfirmationPNL; 7 views resolve to 33 tables in total). Watch for: an empty result (login lacks VIEW DEFINITION, or the view does not exist); a table that is on dev but not on prod or the reverse (new table = no pipeline in the registry = the workbook goes NotReady, which is safe; a table that disappeared = a stale registry row); references through synonyms, other databases, linked servers or dynamic SQL (invisible to this query; T2 marks those unresolved).

Also check that no view references a base table that is loaded by something other than the three pipelines:
```sql
SELECT s.name, o.name, o.create_date, o.modify_date FROM sys.views o JOIN sys.schemas s ON s.schema_id = o.schema_id
WHERE s.name = 'Tableau' ORDER BY o.modify_date DESC;
```
Expect no recently modified view you did not know about (the modification date catches a view changed after the dev mapping was made).

## 6. Sign-off table

| Step | Result | Date | OK? |
|---|---|---|---|
| 1 prod server name written down | | | |
| 2a-2e ETL log: names, columns, time zone, failure behaviour | | | |
| 3a REST version, 3b HTTPS | | | |
| 3c role, visible subscriptions, subscriptions enabled | | | |
| 3d Metadata API answers | | | |
| 4a-4d schedule time zone and monthly days | | | |
| 5 all seven views resolve as on dev | | | |

Anything marked "watch for" that you see goes back to the PM session as a decision, not a quick fix. The scripts stay on dev until the guard values (section 1) are set and reviewed.
