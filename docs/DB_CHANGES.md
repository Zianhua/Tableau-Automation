# Database changes (for the DBA and the data engineer)

Written 2026-10-26, corrected 2026-11-04 (R9 run order and the automatic extract refresh added; counts re-read from dev). Everything here was checked against the SQL files in `scripts/sql/` and, read-only, against the dev DWH (database `CQGlobalSGDW`) on 2026-11-04. Nothing in this document has been applied to any server other than dev. The deployment steps around this list are in [DEPLOYMENT_PLAN.md](DEPLOYMENT_PLAN.md).

Plain names first; the file or tag name follows in brackets so the file can be found.

## Summary for the DBA

**Apply the SQL files BEFORE the code is deployed or any scheduled task runs.** The daily mapping sync checks its tables at start and the extract takeover script refuses to start without them. Each file is run on its own, connected to the production database, with your own login. Every file is re-runnable and starts with a server/database guard (section 4) that you must change to the production server name first.

| No. | File (in `scripts/sql/`) | Creates or alters | Needed on production? | Permissions (service account `[DOMAIN\svc_tableau]`, details in section 9) | Rollback file |
|---|---|---|---|---|---|
| 1 | `T1_mapping_schema.sql` | Creates 9 tables `Tableau_PipelineTable`, `Tableau_StaticTable`, `Tableau_DataSource`, `Tableau_WorkbookDataSource`, `Tableau_DataSourceTable`, `Tableau_DataSourceOverride`, `Tableau_Subscription`, `Tableau_SubscriptionSendLog`, `Tableau_RefreshJob`; 4 views `vw_Tableau_DataSourceTableStatus`, `vw_Tableau_DataSourcePipeline`, `vw_Tableau_DataSourceReadiness`, `vw_Tableau_WorkbookReadiness`; 3 triggers `trg_Tableau_StaticTable_NotInRegistry`, `trg_Tableau_PipelineTable_NotStatic`, `trg_Tableau_SubscriptionSendLog_SentIsFinal`. All in schema `Integration`. | Yes | Table rights as in section 9; `SELECT` on the 4 views and on existing `Integration.ETL_Master_Log` | `T1_mapping_schema_rollback.sql` |
| 2 | `T3_load_registry.sql` | Inserts 33 rows into `Tableau_PipelineTable` and 1 row into `Tableau_StaticTable` (no object created). Content must be validated against production first (6.2). | Yes, after validation | none | none of its own (`DELETE` the rows, or step 1 rollback drops the table) |
| 3 | `R1_schedule_schema.sql` | Creates table `Tableau_Schedule`; adds column `Tableau_Subscription.ScheduleId`. | Yes | `SELECT, INSERT, UPDATE` on `Tableau_Schedule` | `R1_schedule_schema_rollback.sql` |
| 4 | `T11_pause_schema.sql` | Creates table `Tableau_SubscriptionBackup`. | Yes | `SELECT, INSERT, UPDATE` | `T11_pause_schema_rollback.sql` |
| 5 | `R3_sendlog_guard.sql` | Creates trigger `trg_Tableau_SubscriptionSendLog_NoDeleteOfSlot` (INSTEAD OF DELETE on the send log). | Yes | none | `R3_sendlog_guard_rollback.sql` |
| 6 | `R4_takeover_all.sql` | Nothing on a new database (removes a column an old version of step 4 created). | No: safe to run for the record, safe to skip | none | `R4_takeover_all_rollback.sql` (normally never used) |
| 7 | `R5_workbook_updated_at.sql` | Adds column `Tableau_Subscription.WorkbookUpdatedAt`. | Yes | none (column of an existing grant) | `R5_workbook_updated_at_rollback.sql` |
| 8 | `R8_nondwh_sources.sql` | Adds column `Tableau_DataSource.NonDwhConnection` if missing and redefines the two readiness views (`CREATE OR ALTER`, the same definitions as in the current step 1 file): no visible change on a database built from the current step 1. | Yes, run it (it is a no-op after step 1) | none | `R8_nondwh_sources_rollback.sql` |
| 9 | `R9_priority.sql` | Creates table `Tableau_Priority` and view `vw_Tableau_PriorityPicker` (run order of extracts and reports). No `created_by` tag. | Optional: with no rows everything runs by name | `SELECT` on both for the service account (a missing grant is invisible: section 9); `INSERT, UPDATE, DELETE` on `Tableau_Priority` for whoever sets priorities | `R9_priority_rollback.sql` |
| 10 | `ExtractAuto_schema.sql` | Adds column `Tableau_PipelineTable.Source`; creates tables `Tableau_LineageSource` (3 seeded rows), `Tableau_ExtractTask`, `Tableau_ExtractTaskBackup` (including columns `NoRetake` and `OldJobMappingIds`, `OldJobOriginalFlag`, `OldJobSwitchedAt`, `OldJobRestoredAt`) and view `vw_Tableau_ExtractSchedule`. | Yes (automatic registry and extract refresh schedules) | `Tableau_PipelineTable` becomes `SELECT, INSERT, DELETE`; `SELECT` on `Tableau_LineageSource`, the 3 existing lineage tables and the view; `SELECT, INSERT, UPDATE` on the other two tables; **`SELECT, UPDATE` on the existing `ETL_Tableau_ExtractMapping`** (only the takeover uses it) | `ExtractAuto_schema_rollback.sql` (refuses while an extract takeover is open) |

Also for the DBA: (a) after the files, run the grant script of section 9 once; (b) the person who applies the files needs `CREATE TABLE`, `CREATE VIEW` and `ALTER` on schema `Integration` (section 9); (c) no existing object is altered or dropped, and no existing table gets a new column; the ONE change to existing data is that `extract_takeover.py` updates `Active_Flag` in the existing `Integration.ETL_Tableau_ExtractMapping` (the old refresh job's list) for the extracts it takes over, and back on a restore (D-080, section 6.8), which is why the service account needs `UPDATE` on that table; (d) rollback order is ExtractAuto, R9, R8, R5, R4, R3, T11, R1, T1 (section 8); (e) the dev-only test data and objects of section 11 must **not** be shipped.

## 1. What this is, in five lines

- All changes are **additive**: 15 new tables, new columns on our own new tables (and `Source` on our own `Tableau_PipelineTable`), 6 views, 4 triggers. No existing object is altered or dropped. The one write to existing data is the `Active_Flag` switch in `ETL_Tableau_ExtractMapping` by the extract takeover (6.8).
- Everything lives in the existing `Integration` schema and is named `Tableau_*`, `vw_Tableau_*` or `trg_Tableau_*`.
- The scripts connect with **Windows authentication** (the service account is the DWH login). No SQL login, no password.
- Every SQL file is **re-runnable** (it checks before it creates) and has a **rollback file** (except the one-off registry load, which is rolled back by the base schema rollback).
- Every SQL file starts with a **guard** that stops the file when the server or database is wrong. The SQL guards name the dev server in the file text and must be edited for production (section 4); the Python guards read their values from `.env`.

## 2. Existing objects: read, and one table updated (`ETL_Tableau_ExtractMapping`)

| Existing object | What we do | Who |
|---|---|---|
| `Integration.ETL_Master_Log` | **SELECT only.** Columns used: `JobID int NOT NULL`, `PipelineName varchar NOT NULL`, `EndTime datetime2 NOT NULL`, `Success bit NOT NULL` (verified on dev). Used to decide "has this pipeline finished successfully today" (latest row of the Singapore day per pipeline) and to check that a registered pipeline name really exists. | `t4_refresh.py` and `t6_send.py` directly; the view `vw_Tableau_DataSourcePipeline` reads it for the "pipeline known" test. |
| `Integration.ETL_Tableau_ExtractMapping` | **Read and UPDATED** by `extract_takeover.py` only, and only column `Active_Flag` (D-080). It is the old refresh job's list (columns `Mapping_id`, `Etl_Name`, `DataSource_id`, `DataSource_Name`, `Active_Flag`, `Refresh_Frequency`; the old job refreshes each `DataSource_id` with `Active_Flag = 1`). When the takeover takes over an extract it sets `Active_Flag = 0` on exactly the rows of that data source that were 1, in the same transaction as its backup row, and a restore sets exactly those rows back (the `Mapping_id` list is kept in `Tableau_ExtractTaskBackup.OldJobMappingIds`). No row is inserted or deleted, no other column is changed, no row of another data source and no row that was already 0 is touched; a data source without a row there changes nothing; if the table is absent nothing happens. T1's possible duplicate clean-up of this table is still **not** done. | `extract_takeover.py` |
| `Integration.Tableau_ExtractRefreshLog` | **Not read, not written** by any of our scripts or SQL files. Old refresh job only. Our own refresh log is the new table `Tableau_RefreshJob`. | nobody |
| `Integration.Lineage`, `Integration.DW_NovaLineage`, `Integration.OAO_Lineage` | **SELECT only** (three columns each: table name, load completed time, was successful; names in 12.6 (a)). Used by the daily mapping sync to find which pipeline loads a table (automatic registry, section 12). | `t2_mapping_sync.py` (through `registry_sync.py`) |
| `sys.objects`, `sys.schemas`, `sys.sql_expression_dependencies` | **SELECT** (metadata). The daily mapping sync resolves each Tableau view down to its base tables with a recursive query. It never reads the base tables themselves (`Dimension.*`, `Fact.*`). | `t2_mapping_sync.py` (also `sys.tables` across all schemas, to see whether a bare lineage name is unique) |
| `sys.columns`, `HAS_PERMS_BY_NAME`, `COL_LENGTH`, `OBJECT_ID` | Metadata and permission functions used by `extract_takeover.py` (checks that `ETL_Tableau_ExtractMapping` exists with the columns it needs and that the account may `UPDATE` it; checks its own schema at start) and by `t2` (schema check). They need the visibility of section 9 (`VIEW DEFINITION`) and no other right. | `extract_takeover.py`, `t2_mapping_sync.py` |
| `SYSDATETIMEOFFSET()`, `sp_getapplock` | Used. `t4` refuses to run unless the DWH clock offset is +480 minutes (Singapore). `sp_getapplock` is the shared Tableau-login lock (resource `TABLEAU_PAT_SESSION`) and a per-run lock; it needs no special right. | all Tableau scripts |

Requirements on the SQL Server instance: SQL Server 2017 or later (`STRING_AGG`), `AT TIME ZONE 'Singapore Standard Time'` (2016+; the Windows time-zone data on the SQL host must know that zone), `CREATE OR ALTER` (2016 SP1+). The production version is not yet checked (see [PROD_PREFLIGHT.md](PROD_PREFLIGHT.md) section 1).

## 3. Order of application and dependencies

**Apply the SQL BEFORE the code is deployed.** The new code needs the new objects: `t2_mapping_sync.py` checks its tables and columns at start (`schema_ready`: a missing one is a fatal error, mail `t2_failed`, nothing written) and `extract_takeover.py` refuses to start (fatal, exit 1) unless `Tableau_ExtractTask`, `Tableau_ExtractTaskBackup` and its columns `NoRetake` and `OldJobMappingIds` exist. The reverse order (rollback) is code first, SQL second (12.4).

Apply in this order on a **new** database (production). Each file is a separate run in SSMS (or sqlcmd), connected to the target database, with the DBA's own login. This is the same list as the summary at the top.

| Step | File (plain name) | Needs first | Notes |
|---|---|---|---|
| 1 | `T1_mapping_schema.sql` (base mapping schema) | nothing | Creates 9 tables, 4 views, 3 triggers. Since 2026-10-26 it already contains the non-warehouse-source column and views (step 8), so on a new database step 8 changes nothing. |
| 2 | `T3_load_registry.sql` (pipeline registry load) | step 1 | Loads 33 registry rows and 1 static table row. **Content must be validated on production first** (see 6.2). |
| 3 | `R1_schedule_schema.sql` (schedule table and column) | step 1 | New table `Tableau_Schedule`; new column `Tableau_Subscription.ScheduleId`. |
| 4 | `T11_pause_schema.sql` (takeover backup table) | none technically (no foreign key to the mapping tables on purpose) | New table `Tableau_SubscriptionBackup`. Do it before the scripts run so `t11` can write. |
| 5 | `R3_sendlog_guard.sql` (send-log delete guard) | step 1 | One `INSTEAD OF DELETE` trigger on `Tableau_SubscriptionSendLog`. |
| 6 | `R4_takeover_all.sql` (takeover-all change) | step 4 | **A no-op on a new database**: it removes a column (`WasSuspended`) that the current `T11_pause_schema.sql` no longer creates. Safe to run for the record, safe to skip. It exists only for databases built before 2026-10-23 (dev). |
| 7 | `R5_workbook_updated_at.sql` (workbook change-time column) | step 1 | New nullable column `Tableau_Subscription.WorkbookUpdatedAt`. After it, run the mapping sync once (until then nothing is sent: see 6.7). |
| 8 | `R8_nondwh_sources.sql` (non-warehouse sources count as ready) | step 1 | Adds `Tableau_DataSource.NonDwhConnection` if missing and redefines the two readiness views. A no-op after a fresh step 1 of the current file. |
| 9 | `R9_priority.sql` (run order) | step 1 | New table `Integration.Tableau_Priority` and view `vw_Tableau_PriorityPicker` (5.8). Optional: with no rows everything runs by name. Rollback: `R9_priority_rollback.sql` (the code tolerates the table missing, but see the permission caution in section 9). |
| 10 | `ExtractAuto_schema.sql` (automatic registry and extract refresh schedules) | steps 1 and 3 | New column `Tableau_PipelineTable.Source`, new tables `Tableau_LineageSource`, `Tableau_ExtractTask`, `Tableau_ExtractTaskBackup`, new view `vw_Tableau_ExtractSchedule`. Full list, permissions and rollback: section 12. Rollback: `ExtractAuto_schema_rollback.sql` (refuses while an extract takeover is open). |
| - | `T1_tests.sql`, `T1_concurrency_test.ps1` | - | **Dev only. Never run on production** (see 11.4). Not part of the deployment. |

Dependency summary: everything hangs off step 1; steps 3, 5, 7, 8, 9 each alter, trigger on or read a step 1 table; step 4 stands alone; step 6 only touches a step 4 table; step 10 needs steps 1 and 3.

Rollback order is the **reverse**: ExtractAuto (10), R9 (9), R8 (8), R5 (7), R4 (6), R3 (5), T11 (4), R1 (3), T1 (1). The registry load (2) has no rollback of its own: dropping the registry table in step 1's rollback removes its rows. Rollback files refuse to run while any taken-over subscription (or extract task) is still open (restore them first: `t11_pause.py --restore`, `extract_takeover.py --restore-all`), and they do not drop a non-empty send log or refresh log unless `@Force = 1` is set by hand. See section 8.

## 4. The guards: what must change for production

**SQL guard.** Line 3 of every SQL file is:

```
IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') BEGIN RAISERROR(...); SET NOEXEC ON; END
```

Meaning: on any other server or database the whole file silently does nothing except print the error, and `SET NOEXEC ON` skips every later batch. This is intended protection against running on the wrong server.

Files that carry it: the **19 deployable files** `T1_mapping_schema`, `T1_mapping_schema_rollback`, `T3_load_registry`, `R1_schedule_schema`, `R1_schedule_schema_rollback`, `R3_sendlog_guard`, `R3_sendlog_guard_rollback`, `R4_takeover_all`, `R4_takeover_all_rollback`, `R5_workbook_updated_at`, `R5_workbook_updated_at_rollback`, `R8_nondwh_sources`, `R8_nondwh_sources_rollback`, `T11_pause_schema`, `T11_pause_schema_rollback`, `ExtractAuto_schema`, `ExtractAuto_schema_rollback`, `R9_priority`, `R9_priority_rollback`; and the dev-only `T1_tests.sql`, which **keeps the dev name** and is never shipped.

**For production:**
1. Find the exact production value: `SELECT @@SERVERNAME, DB_NAME();` on the production DWH. The production host is named in D-010, but `@@SERVERNAME` must be read from the server itself (a rename, alias, listener or named instance would differ). The database name is expected to be the same, `CQGlobalSGDW`.
2. One search-and-replace across `scripts/sql`, excluding `T1_tests.sql`: replace the literal `NOT IN ('<DEV_DWH_SERVER>')` with `NOT IN ('<production @@SERVERNAME>')` (and the same name in the message text of that line, which names the dev server). Nothing else in the files changes. A list `NOT IN ('<dev>', '<production>')` is also valid if the same files must run on both.
3. The checks that assert the guard text need the same edit: `scripts/extract_checks.py` (`test_sql_files`) and `scripts/t11_checks.py` (`test_sql_files`) assert the literal dev server name in the files. `scripts/r2_checks.py` asserts only the structure (`DB_NAME()`, `@@SERVERNAME`, `SET NOEXEC ON`, closing `SET NOEXEC OFF;`) and needs no edit.
4. **No code change is needed for the Python guards.** `scripts/t2_mapping_sync.py` (about lines 103-104) and `scripts/t4_refresh.py` (about lines 83-84) read the environment variables `DEV_DWH_SERVER` and `DEV_DWH_DATABASE` (environment, else `.env`); if either is unset the script refuses to run. The other scripts (`t6_send.py`, `t11_pause.py`, `extract_takeover.py`, `daily_summary.py`, `review_list.py`) call `t4_refresh.connect()` and so use the `t4` copy. Three checks per connection: `CONN_SERVER` and `CONN_DATABASE` in `.env` must equal `DEV_DWH_SERVER` / `DEV_DWH_DATABASE` (case-insensitive, exact text); after connecting, `@@SERVERNAME` must start with the first label of `DEV_DWH_SERVER` (the text before the first dot); and `DB_NAME()` must equal `DEV_DWH_DATABASE`. **On production the operator sets these two keys in the production `.env` to the production values** (the names say DEV but they hold whatever server the scripts may use). `T1_concurrency_test.ps1` (dev only) is env-driven by the same two keys.
5. The Tableau host switch is a separate key, `UAT_TABLEAU_HOST` (read by `scripts/ops.py`): the UAT-only "mail every subscriber" switch (`SEND_ALLOW_ALL_UAT`) only works when the host of `TABLEAU_SERVER_URL` equals it. It is not a DWH guard. Leave both keys out of the production `.env` ([DEPLOYMENT_PLAN.md](DEPLOYMENT_PLAN.md) step 4).
6. One constant is not a guard but must hold: `t2_mapping_sync.py` classifies a Tableau connection as "our warehouse" by the database name `cqglobalsgdw` (`DWH_DB`). Production must use the same database name: with another name, connections to the production warehouse would be read as "another database" and counted as not needing a readiness check (`NotChecked`, R8), so reports could go out before the data is loaded. Confirm the name in [PROD_PREFLIGHT.md](PROD_PREFLIGHT.md) section 1.

## 5. Object list, file by file

Common to all new tables: schema `Integration`; every object created by `T1_mapping_schema.sql` carries the extended property `created_by = 'T1_mapping_schema'` (R1: `'R1_schedule_schema'`, takeover backup: `'T11_pause_schema'`), and a rollback drops **only** objects with its own tag. Dev check on 2026-11-04: 13 objects tagged T1, 1 tagged R1, 1 tagged T11 and 4 tagged `ExtractAuto_schema` (section 12). The R3 trigger, the R4/R5/R8 columns and views, and the R9 objects carry no tag (the R9 rollback drops its two objects by name).

### 5.1 Base mapping schema (`T1_mapping_schema.sql`, rollback `T1_mapping_schema_rollback.sql`)

Purpose: store the chain workbook -> data source -> upstream table -> pipeline, the subscription definitions, the send log (one slot per subscription per Singapore day) and the refresh-job log; and compute readiness from them (fail closed).

**Tables (9)**

| Table | Columns (type, null, default) | Keys, indexes, constraints |
|---|---|---|
| `Tableau_PipelineTable` (registry: which table is loaded by which pipeline) | `PipelineName varchar(50) NOT NULL` (= `ETL_Master_Log.PipelineName`, including the `CQGlobalSGDW: ` prefix); `SchemaName sysname NOT NULL`; `TableName sysname NOT NULL`; `Evidence nvarchar(400) NULL`; `ConfirmedBy nvarchar(128) NOT NULL DEFAULT SUSER_SNAME()`; `ConfirmedAt datetime2(0) NOT NULL DEFAULT SYSDATETIME()` | PK `PK_Tableau_PipelineTable` (SchemaName, TableName, PipelineName); trigger `trg_Tableau_PipelineTable_NotStatic` |
| `Tableau_StaticTable` (tables that need no freshness check) | `SchemaName sysname NOT NULL`; `TableName sysname NOT NULL`; `Reason nvarchar(400) NOT NULL`; `SetBy nvarchar(128) NOT NULL DEFAULT SUSER_SNAME()`; `SetAt datetime2(0) NOT NULL DEFAULT SYSDATETIME()` | PK (SchemaName, TableName); trigger `trg_Tableau_StaticTable_NotInRegistry` |
| `Tableau_DataSource` (data sources as the workbooks see them) | `DataSource_id nvarchar(128) NOT NULL`; `DataSource_Name nvarchar(256) NULL`; `LinkedPublishedDatasourceId nvarchar(128) NULL`; `IsExtract bit NULL` (NULL = unknown, fails closed); `RefreshKind` computed persisted (`Extract` / `Live` / `Unknown`); `UpstreamDataSourceId` computed persisted; `ExtractRefreshTargetId` computed persisted (set only for extracts); `NonDwhConnection bit NOT NULL DEFAULT 0` (constraint `DF_Tableau_DataSource_NonDwh`); `LastSeenAt datetime2(0) NOT NULL DEFAULT SYSDATETIME()`; `Active_Flag bit NOT NULL DEFAULT 1` | PK `PK_Tableau_DataSource` (DataSource_id) |
| `Tableau_WorkbookDataSource` (workbook to data source links) | `Workbook_id nvarchar(128) NOT NULL`; `Workbook_Name nvarchar(256) NOT NULL`; `DataSource_id nvarchar(128) NOT NULL`; `LastSeenAt datetime2(0) NOT NULL DEFAULT SYSDATETIME()`; `Active_Flag bit NOT NULL DEFAULT 1` | PK (Workbook_id, DataSource_id); FK `FK_Tableau_WorkbookDataSource_DataSource` to `Tableau_DataSource` |
| `Tableau_DataSourceTable` (data source to base tables, views already resolved) | `DataSource_id nvarchar(128) NOT NULL`; `SchemaName sysname NOT NULL`; `TableName sysname NOT NULL`; `LastSeenAt datetime2(0) NOT NULL DEFAULT SYSDATETIME()` | PK (DataSource_id, SchemaName, TableName) |
| `Tableau_DataSourceOverride` (explicit "no readiness check" per source; empty by design) | `DataSource_id nvarchar(128) NOT NULL`; `NoReadinessCheck bit NOT NULL DEFAULT 1`; `Reason nvarchar(400) NOT NULL`; `SetBy nvarchar(128) NOT NULL DEFAULT SUSER_SNAME()`; `SetAt datetime2(0) NOT NULL DEFAULT SYSDATETIME()` | PK (DataSource_id) |
| `Tableau_Subscription` (subscription definitions; **no recipient addresses are stored**) | `Subscription_id nvarchar(128) NOT NULL`; `Workbook_id nvarchar(128) NOT NULL`; `TargetType varchar(10) NOT NULL`; `TargetId nvarchar(128) NOT NULL`; `Format varchar(12) NOT NULL`; `Subject nvarchar(500) NULL`; `Message nvarchar(2000) NULL`; `PageSize varchar(20) NULL`; `Orientation varchar(20) NULL`; `IsSuspended bit NOT NULL DEFAULT 0` (information only); `ScheduleType varchar(50) NULL`; `SubscriptionKeyHash varbinary(32) NULL`; `LastSeenAt datetime2(0) NOT NULL DEFAULT SYSDATETIME()`; `Active_Flag bit NOT NULL DEFAULT 1`. Added later by other files: `ScheduleId nvarchar(128) NULL` (5.3), `WorkbookUpdatedAt datetime2(0) NULL` (5.6) | PK `PK_Tableau_Subscription`; index `IX_Tableau_Subscription_KeyHash` (SubscriptionKeyHash); CHECK `CK_Tableau_Subscription_TargetType` (`Workbook`, `View`); CHECK `CK_Tableau_Subscription_Format` (`PDF`, `Image`, `PDFAndImage`) |
| `Tableau_SubscriptionSendLog` (send log, claim first, at most once) | `SendLog_id int IDENTITY(1,1) NOT NULL`; `Subscription_id nvarchar(128) NOT NULL`; `Status varchar(10) NOT NULL`; `ClaimedAt datetimeoffset(0) NOT NULL DEFAULT SYSDATETIMEOFFSET()`; `SendDate` computed persisted (Singapore calendar date of `ClaimedAt`, cannot be set by the script); `SentAt datetimeoffset(0) NULL`; `RunId varchar(64) NULL`; `Message nvarchar(1000) NULL` | PK `PK_Tableau_SubscriptionSendLog`; FK `FK_Tableau_SubscriptionSendLog_Subscription` to `Tableau_Subscription`; CHECK `CK_Tableau_SubscriptionSendLog_Status` (`Claimed`, `Sent`, `Failed`, `DryRun`, `TimedOut`, `Released`); CHECK `CK_Tableau_SubscriptionSendLog_SentAt` (a `Sent` row needs `SentAt`); **filtered unique index `UX_Tableau_SubscriptionSendLog_OncePerDay`** on (Subscription_id, SendDate) WHERE Status IN (`Claimed`,`Sent`,`TimedOut`) = the double-send protection; trigger `trg_Tableau_SubscriptionSendLog_SentIsFinal` |
| `Tableau_RefreshJob` (one row per submitted extract refresh) | `RefreshJob_id int IDENTITY(1,1) NOT NULL`; `DataSource_id nvarchar(128) NOT NULL` (the refresh target); `TableauJobId nvarchar(64) NULL`; `SubmittedAt datetimeoffset(0) NOT NULL DEFAULT SYSDATETIMEOFFSET()`; `SubmittedDate date NOT NULL DEFAULT` Singapore date of now; `FinishedAt datetimeoffset(0) NULL`; `Status varchar(10) NOT NULL DEFAULT 'Submitted'`; `Message nvarchar(1000) NULL` | PK `PK_Tableau_RefreshJob`; index `IX_Tableau_RefreshJob_DataSource` (DataSource_id, SubmittedDate DESC); CHECK `CK_Tableau_RefreshJob_Status` (`Submitted`, `Succeeded`, `Failed`, `Cancelled`, `TimedOut`) |

**Triggers (3 in this file)**
- `trg_Tableau_StaticTable_NotInRegistry` (AFTER INSERT, UPDATE on `Tableau_StaticTable`): error 50002 if the table is also in the registry.
- `trg_Tableau_PipelineTable_NotStatic` (AFTER INSERT, UPDATE on `Tableau_PipelineTable`): error 50002 if the table is also static.
- `trg_Tableau_SubscriptionSendLog_SentIsFinal` (AFTER UPDATE): error 50001 if a `Sent` row's status is changed.
- (the fourth trigger, the send-log delete guard, is in 5.5)

**Views (4)**
- `vw_Tableau_DataSourceTableStatus`: tables a source reads, flagged static / registered.
- `vw_Tableau_DataSourcePipeline`: source to required pipelines, with `PipelineKnown` (does the name exist in `ETL_Master_Log`, binary comparison). **This view reads `ETL_Master_Log`.**
- `vw_Tableau_DataSourceReadiness`: `ReadyStatus` per active source: `NoReadinessCheck`, `UnknownKind`, `NotChecked`, `NoTables`, `UnmappedTable`, `UnknownPipeline` or `Ready`.
- `vw_Tableau_WorkbookReadiness`: `WorkbookStatus`: `NoDataSources`, `InactiveDataSource`, `NotReady` or `Ready`.

**Existing objects touched:** none altered. Only read: `ETL_Master_Log` (through `vw_Tableau_DataSourcePipeline`).

**Who writes and reads:** see the matrix in section 7.

**Guarded to:** dev server and database (section 4). **Rollback:** `T1_mapping_schema_rollback.sql` (drops the 13 tagged T1 objects in dependency order; refuses if any open takeover row exists; refuses if the send log or refresh log has rows unless `@Force = 1`; does **not** drop `Tableau_Schedule` or `Tableau_SubscriptionBackup`: run their rollbacks first).

**Applied on dev:** 2026-10-22 (first version, then the reviewed rev 3 re-created the same day). The file was edited again on 2026-10-26 (to carry the non-warehouse-source change), but dev received that change through its own file (5.7), not by re-running this one.

**Verification (expect 13):**
```sql
SELECT COUNT(*) FROM sys.extended_properties
WHERE class = 1 AND minor_id = 0 AND name = 'created_by' AND CAST(value AS nvarchar(100)) = 'T1_mapping_schema';
```

### 5.2 Pipeline registry load (`T3_load_registry.sql`, no rollback file)

Purpose: fill `Tableau_PipelineTable` with the confirmed "this table is loaded by this pipeline" rows and mark `dbo.SalesChannel_Excel` as static (deprecated, no check).

- **Rows inserted:** 33 registry rows (3 pipelines: `CQGlobalSGDW: DailyETL`, `CQGlobalSGDW: NovaDailyETL`, `CQGlobalSGDW: LeadGenETL`) and 1 static row (`dbo.SalesChannel_Excel`). It inserts only rows that are missing; it never updates or deletes. It ends by listing registry rows that are not in its own list (expected: none) and the row count.
- **Existing objects touched:** none.
- **Written by / read by:** loaded once by the DBA with this file; read by the views (and so by every script). The scripts never write the registry.
- **Guarded to:** dev (section 4). **Rollback:** none of its own; to remove the rows drop the table through the base schema rollback or `DELETE FROM Integration.Tableau_PipelineTable` / `Tableau_StaticTable`.
- **Production note:** the evidence text and the table list were built from the **dev** DWH metadata. Before loading on production, compare with production (steps 2e and 5 of PROD_PREFLIGHT): the pipeline names in `ETL_Master_Log` must match exactly, and the tables must exist. A wrong or missing row fails safe (the report is not sent), but a table registered under the wrong pipeline would send early.
- **Applied on dev:** 2026-10-22 (32 rows plus the static row); the 33rd row (`Integration.MultipleAccounts_Staging`) was added 2026-10-23.
- **Verification (expect 33 and 1):** `SELECT (SELECT COUNT(*) FROM Integration.Tableau_PipelineTable) AS RegistryRows, (SELECT COUNT(*) FROM Integration.Tableau_StaticTable) AS StaticRows;`

### 5.3 Schedule table and column (`R1_schedule_schema.sql`, rollback `R1_schedule_schema_rollback.sql`)

Purpose: store each subscription's Tableau schedule so the scripts send only on the days the schedule fires.

- **New table `Tableau_Schedule`:** `Schedule_id nvarchar(128) NOT NULL` (PK `PK_Tableau_Schedule`); `Name nvarchar(200) NULL`; `State varchar(12) NULL` (Active / Suspended); `Frequency varchar(10) NULL` (Hourly / Daily / Weekly / Monthly); `WeekDays varchar(40) NULL`; `MonthDays varchar(80) NULL`; `StartTime time(0) NULL`; `IntervalHours decimal(6,2) NULL`; `LastSeenAt datetime2(0) NOT NULL DEFAULT SYSDATETIME()`; `Active_Flag bit NOT NULL DEFAULT 1`. Tag `created_by = 'R1_schedule_schema'`.
- **New column:** `Tableau_Subscription.ScheduleId nvarchar(128) NULL`.
- **Existing objects touched:** none (it alters our own `Tableau_Subscription`).
- **Written by:** `t2_mapping_sync.py` (MERGE). **Read by:** `t6`, `t4`, `t11` (through `schedule_due.py`), `review_list.py`.
- **Guarded to:** dev (section 4). **Rollback:** `R1_schedule_schema_rollback.sql` (drops the column and the tagged table; refuses while any subscription is still taken over; after it, a script with schedule awareness on refuses to send).
- **Applied on dev:** 2026-10-23.
- **Verification:** `SELECT OBJECT_ID('Integration.Tableau_Schedule') AS ScheduleTable, COL_LENGTH('Integration.Tableau_Subscription','ScheduleId') AS ScheduleIdLen;` (both not NULL; dev: 256 bytes).

### 5.4 Takeover backup table (`T11_pause_schema.sql`, rollback `T11_pause_schema_rollback.sql`)

Purpose: the only record of which old Tableau subscriptions the script has taken over (paused), with a full copy of each definition before it was paused, so it can be restored. t6 sends a subscription only if it has an open row here.

- **New table `Tableau_SubscriptionBackup`** (tag `created_by = 'T11_pause_schema'`): `Backup_id int IDENTITY(1,1) NOT NULL`; `Subscription_id nvarchar(128) NOT NULL`; `Workbook_id nvarchar(128) NOT NULL`; `TargetType varchar(10) NOT NULL`; `TargetId nvarchar(128) NOT NULL`; `OwnerUserId nvarchar(128) NOT NULL` (a Tableau user id, not an address); `ScheduleId nvarchar(128) NULL`; `ScheduleName nvarchar(200) NULL`; `Subject nvarchar(500) NULL`; `Message nvarchar(4000) NULL`; `AttachPdf bit NOT NULL`; `AttachImage bit NOT NULL`; `PageSize varchar(20) NULL`; `Orientation varchar(20) NULL`; `SendIfViewEmpty bit NULL`; `BackedUpAt datetimeoffset(0) NOT NULL DEFAULT SYSDATETIMEOFFSET()`; `TakenOver bit NOT NULL DEFAULT 0`; `PausedAt datetimeoffset(0) NULL`; `LastRepausedAt datetimeoffset(0) NULL`; `LastRepausedDate` computed persisted (Singapore date of `LastRepausedAt`); `RepauseCount int NOT NULL DEFAULT 0`; `RestoredAt datetimeoffset(0) NULL`; `RunId varchar(64) NULL`; `Halted bit NOT NULL DEFAULT 0`; `Note nvarchar(400) NULL`; `GoneSince date NULL`.
- **Constraints and indexes:** PK `PK_Tableau_SubscriptionBackup`; CHECK `CK_Tableau_SubscriptionBackup_TargetType` (`Workbook`, `View`); CHECK `CK_Tableau_SubscriptionBackup_PausedNeedsTakeover` (`PausedAt IS NULL OR TakenOver = 1`); **filtered unique index `UX_Tableau_SubscriptionBackup_Open`** on (Subscription_id) WHERE `RestoredAt IS NULL` (at most one open takeover per subscription); index `IX_Tableau_SubscriptionBackup_Workbook` (Workbook_id). No foreign key, on purpose (a backup must outlive a mapping reset).
- **Existing objects touched:** none.
- **Written by:** `t11_pause.py` (INSERT / UPDATE). **Read by:** `t11`, `t6`, `t4`, `daily_summary.py`, `review_list.py`.
- **Guarded to:** dev (section 4). **Rollback:** `T11_pause_schema_rollback.sql` (refuses while an open takeover row exists, and `@Force` does not override that; refuses a non-empty table unless `@Force = 1`; the rows are the only copy of the old definitions apart from `out\t11_backup\*.json` on the host that ran `t11`).
- **Applied on dev:** 2026-10-23 (the three later columns `Halted`, `Note`, `GoneSince` were added to the same file and dev the same day).
- **Verification:** `SELECT OBJECT_ID('Integration.Tableau_SubscriptionBackup') AS BackupTable, COL_LENGTH('Integration.Tableau_SubscriptionBackup','GoneSince') AS GoneSinceLen;` (both not NULL).

### 5.5 Send-log delete guard (`R3_sendlog_guard.sql`, rollback `R3_sendlog_guard_rollback.sql`)

Purpose: a `Sent`, `Claimed` or `TimedOut` row holds the day's slot; deleting it would free the slot and allow a second send. This trigger refuses such deletes.

- **New trigger:** `trg_Tableau_SubscriptionSendLog_NoDeleteOfSlot` (INSTEAD OF DELETE on `Tableau_SubscriptionSendLog`). Rows with status `Released`, `Failed` or `DryRun` may be deleted. `Sent` / `Claimed` / `TimedOut` rows are refused with error 50007, except in a test session that has run `EXEC sp_set_session_context 'allow_fixture_delete', 1` **and** whose rows all belong to a subscription named `T6TEST-%`, `T11TEST-%` or `R1TEST-%`. On production there are no such subscriptions, so a slot-holding row can never be deleted; a person releases a `Claimed`/`TimedOut` row (status change) instead (RUNBOOK section 5).
- **Existing objects touched:** none. **Written / read by:** it fires on any delete; no script deletes from the send log.
- **Guarded to:** dev (section 4). **Rollback:** `R3_sendlog_guard_rollback.sql` (drops the trigger only).
- **Applied on dev:** 2026-10-23.
- **Verification:** `SELECT name, is_instead_of_trigger, is_disabled FROM sys.triggers WHERE name = 'trg_Tableau_SubscriptionSendLog_NoDeleteOfSlot';` (one row, 1, 0).

### 5.6 Takeover-all change (`R4_takeover_all.sql`, rollback `R4_takeover_all_rollback.sql`) and workbook change-time column (`R5_workbook_updated_at.sql`, rollback `R5_workbook_updated_at_rollback.sql`)

**Takeover-all change.** Removes from `Tableau_SubscriptionBackup` the check `CK_Tableau_SubscriptionBackup_NotPreSuspended`, any default on `WasSuspended`, and the column `WasSuspended` (decision: a subscription that is already suspended is taken over like any other). It refuses to run if a row has `WasSuspended = 1`. **On a database built with the current `T11_pause_schema.sql` there is nothing to remove**, so this file does nothing; it matters only for dev, which had the old column. Its rollback re-adds the column as `bit NOT NULL DEFAULT 0` (named `DF_Tableau_SubscriptionBackup_WasSuspended`) and the check; use it only together with the old code, and read its warning about open rows first. Applied on dev 2026-10-23 (0 backup rows existed). Verification: `SELECT COL_LENGTH('Integration.Tableau_SubscriptionBackup','WasSuspended');` returns NULL.

**Workbook change-time column.** Adds `Tableau_Subscription.WorkbookUpdatedAt datetime2(0) NULL`: the workbook's `updated_at` as the mapping sync read it (naive UTC). `t6` compares the live value with it and waits ("awaiting sync") if the workbook changed after the sync. NULL means "not yet synced since this column existed": nothing is sent for that subscription until the mapping sync has run once. Written by `t2_mapping_sync.py`, read by `t6_send.py`. Apply before the new `t2`/`t6` code runs. Applied on dev 2026-10-23. Verification: `SELECT COL_LENGTH('Integration.Tableau_Subscription','WorkbookUpdatedAt');` (6 bytes on dev, not NULL). Rollback: drops the column; use only with the old code.

Both: existing objects touched none; guarded to dev (section 4).

### 5.7 Non-warehouse sources count as ready (`R8_nondwh_sources.sql`, rollback `R8_nondwh_sources_rollback.sql`)

Purpose: a report is ready when its warehouse parts are ready; an Excel/CSV or other-database source cannot be checked and counts as ready.

- **New column:** `Tableau_DataSource.NonDwhConnection bit NOT NULL`, default 0 (constraint `DF_Tableau_DataSource_NonDwh`). The mapping sync sets it to 1 only when it positively identified every skipped connection of the source as not our warehouse.
- **Redefined views (CREATE OR ALTER):** `vw_Tableau_DataSourceReadiness` (new status `NotChecked`: flag set and no warehouse table) and `vw_Tableau_WorkbookReadiness` (`NotChecked` counts as ready). Nothing else changes.
- **Existing objects touched:** none. **Written by** `t2_mapping_sync.py` (the column); **read by** the views, hence every script.
- **Guarded to:** dev (section 4). **Rollback:** restores the two views as they were before, then drops the column; use with the old code. **Applied on dev:** 2026-10-26.
- **Verification:** `SELECT COL_LENGTH('Integration.Tableau_DataSource','NonDwhConnection') AS Col, CASE WHEN OBJECT_DEFINITION(OBJECT_ID('Integration.vw_Tableau_DataSourceReadiness')) LIKE '%NotChecked%' THEN 1 ELSE 0 END AS ViewHasNotChecked;` (1 byte, 1).

### 5.8 Run order (`R9_priority.sql`, rollback `R9_priority_rollback.sql`)

Purpose: let a person say which extracts are refreshed first and which reports are sent first. Without any row everything runs in name order, as before.

- **New table `Integration.Tableau_Priority`** (no `created_by` tag): `Kind varchar(12) NOT NULL` (`Extract` or `Subscription`); `Item_id nvarchar(128) NOT NULL` (for `Extract` the `ExtractRefreshTargetId` of the data source, for `Subscription` the `Subscription_id`); `Priority int NOT NULL` (lower goes first); `Note nvarchar(200) NULL`; `SetBy nvarchar(128) NOT NULL` (default `SUSER_SNAME()`, constraint `DF_Tableau_Priority_SetBy`); `SetAt datetime2(0) NOT NULL` (default `SYSDATETIME()`, constraint `DF_Tableau_Priority_SetAt`). Primary key `PK_Tableau_Priority` (Kind, Item_id); check `CK_Tableau_Priority_Kind`.
- **New view `Integration.vw_Tableau_PriorityPicker`** (`Kind`, `Item_id`, `Name`, `Priority`): the active extract refresh targets (from `Tableau_DataSource`) and the active subscriptions (from `Tableau_Subscription` and `Tableau_WorkbookDataSource`) with their priority if any. It exists so a person can find the `Item_id` to mark (RUNBOOK section 19).
- **Existing objects touched:** none. **Written by:** a person with `INSERT, UPDATE, DELETE` on `Tableau_Priority` (no script writes it). **Read by:** `t4_refresh.py` (kind `Extract`) and `t6_send.py` (kind `Subscription`) through `ops.priorities`; the view is for people.
- **Caution (permissions):** `ops.priorities` first runs `SELECT OBJECT_ID('Integration.Tableau_Priority')`. `OBJECT_ID` returns NULL for an object the login has no right to see, and the code then silently falls back to by-name order. A missing `SELECT` grant on `Tableau_Priority` is therefore invisible: the marks simply have no effect. Grant `SELECT` (section 9) and test with one mark.
- **Guarded to:** dev (section 4). **Rollback:** `R9_priority_rollback.sql` (drops the view, then the table; the code tolerates the table missing). **Applied on dev:** 2026-11-03.
- **Verification:** `SELECT OBJECT_ID('Integration.Tableau_Priority') AS PriorityTable, OBJECT_ID('Integration.vw_Tableau_PriorityPicker') AS PickerView;` (both not NULL; as the service account too, after the grants).

### 5.9 Automatic registry and extract refresh schedules (`ExtractAuto_schema.sql`)

Described in section 12 (objects 12.1, permissions section 9, rollback 12.4).

## 6. Things the DBA should know

6.1 **Nothing is deleted from existing tables, ever.** The only deletes our code does are inside our own tables: the mapping sync replaces the `Tableau_DataSourceTable` rows of a data source it has just read (a `MERGE ... WHEN NOT MATCHED BY SOURCE THEN DELETE`, limited to the sources seen in that run), and the registry sync (`registry_sync.py`) `DELETE`s `Auto` rows of `Tableau_PipelineTable` that it removes or moves (never a `Manual` row). Rows of other mapping tables that disappear from Tableau are set `Active_Flag = 0`, not deleted. The send log, the refresh log and the backup table are never pruned (the send log is the double-send protection).

6.2 **The registry is production-sensitive.** The 33 registry rows say which pipeline loads which table. Wrong or missing rows fail closed (not sent) except a table registered under a pipeline that finishes before the table is really loaded. Validate against production before loading.

6.3 **Volume is small.** Dev holds (read 2026-11-04): 67 subscription rows (5 active), 91 data sources (9 active), 92 links (9 active), 123 upstream-table rows, 16 schedules (10 subscription schedules and 6 extract schedules), 33 registry rows (all `Manual`), 8 send-log rows, 6 refresh-job rows, 11 subscription backup rows (10 open), 23 extract tasks, 0 extract task backup rows, 0 priority rows, 3 lineage-source rows. The send log grows by at most one row per subscription per day.

6.4 **The old refresh job keeps running, but not for the extracts we take over (D-080).** It reads `ETL_Tableau_ExtractMapping` (`WHERE active_flag = 1`) and writes `Tableau_ExtractRefreshLog`; it is not changed. Our scripts refresh extracts through a different token (D-017) and record it in `Tableau_RefreshJob`. For every extract the takeover takes over, the takeover sets `Active_Flag = 0` on that data source's rows in `ETL_Tableau_ExtractMapping` (so the old job leaves it alone) and a restore sets them back (6.8). For all other extracts the old job and ours may both refresh the same extract on the same day; ours skips a target that already has a queued or running job. `Tableau_ExtractRefreshLog` is still neither read nor written by us.

6.5 **Clock.** The scripts assume the DWH clock is Singapore time (+08:00) and `ETL_Master_Log.EndTime` is Singapore local time. `t4` stops with an error if the offset is not 480 minutes. This is verified on dev only.

6.6 **No credentials in the database.** No recipient addresses, no tokens, no passwords are stored. Recipients are read from Tableau at send time; logs hold only a count and a salted hash.

6.8 **What is written to the old job's table, exactly (D-080).** One statement shape only: `UPDATE ... SET Active_Flag = 0 WHERE DataSource_id = <taken-over data source> AND Active_Flag = 1` (takeover) and `UPDATE ... SET Active_Flag = <stored original value> WHERE Mapping_id IN (<stored list>) AND DataSource_id = <that data source> AND Active_Flag <> <stored original value>` (restore). Both run in one transaction with the write to our own backup row, so the backup row and the switch always agree. The takeover also repeats the switch-off on every run for an open takeover, so a row added later for the same data source is switched off too (and added to the stored list). Undo by hand if ever needed: `UPDATE Integration.ETL_Tableau_ExtractMapping SET Active_Flag = 1 WHERE Mapping_id IN (<OldJobMappingIds of the backup row>);`.

6.7 **First run after R5/R8:** run `python scripts\t2_mapping_sync.py` once; until then rows have `WorkbookUpdatedAt = NULL` and `t6` sends nothing.

## 7. Who reads and writes each new table

(From the SQL text of each script, 2026-11-04. "Views" means the four readiness views, so every script that uses readiness reads those tables.)

| Table | Written by | Read by |
|---|---|---|
| `Tableau_PipelineTable` | DBA (registry load file) for `Manual` rows; `t2_mapping_sync.py` through `registry_sync.py` (INSERT and DELETE of `Auto` rows only, section 12). | views (`t4`, `t6`, `t11`, `review_list`, `daily_summary` through them) |
| `Tableau_StaticTable` | DBA (registry load file). No script. | views |
| `Tableau_DataSourceOverride` | nobody (manual, empty) | views |
| `Tableau_DataSource` | `t2_mapping_sync.py` (MERGE) | `t2`, `daily_summary`, views |
| `Tableau_WorkbookDataSource` | `t2_mapping_sync.py` (MERGE) | `t2`, `t4`, `t6`, `t11`, `daily_summary`, `review_list`, views |
| `Tableau_DataSourceTable` | `t2_mapping_sync.py` (MERGE, including delete of replaced rows) | `t2`, views |
| `Tableau_Subscription` | `t2_mapping_sync.py` (MERGE) | `t2`, `t4`, `t6`, `t11`, `daily_summary`, `review_list` |
| `Tableau_Schedule` | `t2_mapping_sync.py` (MERGE) | `t6`, `t4`, `t11` (through `schedule_due.py`), `review_list` |
| `Tableau_SubscriptionSendLog` | `t6_send.py` (INSERT, UPDATE) | `t6`, `t11` (restore refusal after a same-day send), `daily_summary`, `review_list`, `schedule_due.py` |
| `Tableau_RefreshJob` | `t4_refresh.py` (INSERT, UPDATE) | `t4`, `t6`, `daily_summary`, `watchdog.py` |
| `Tableau_SubscriptionBackup` | `t11_pause.py` (INSERT, UPDATE) | `t11`, `t6`, `t4`, `daily_summary`, `review_list` |
| `Tableau_Priority` (R9) | a person (`INSERT`, `UPDATE`, `DELETE`; RUNBOOK section 19). No script. | `t4` (kind `Extract`), `t6` (kind `Subscription`) via `ops.priorities`; people through `vw_Tableau_PriorityPicker` |
| `Tableau_LineageSource`, `Tableau_ExtractTask`, `Tableau_ExtractTaskBackup` (ExtractAuto) | see 12.3 | see 12.3 |
| `ETL_Master_Log` (existing) | nobody of ours | `t4`, `t6` (via `t4`), `vw_Tableau_DataSourcePipeline` |

## 8. Rollback, in order

Do this only when the scheduled tasks are unregistered or disabled (**code first, SQL second**), every taken-over subscription is restored (`python scripts\t11_pause.py --restore-all-for-workbook <workbook id> --execute`, per workbook) and every taken-over extract task is restored (`python scripts\extract_takeover.py --restore-all --execute`). Then, reversing section 3:

1. `ExtractAuto_schema_rollback.sql` (refuses while any extract takeover row is open; deletes the `Auto` registry rows; see 12.4)
2. `R9_priority_rollback.sql` (the code tolerates the missing table)
3. `R8_nondwh_sources_rollback.sql` (only if the old code is back)
4. `R5_workbook_updated_at_rollback.sql` (only if the old code is back)
5. `R4_takeover_all_rollback.sql` (only with the old code; normally skip)
6. `R3_sendlog_guard_rollback.sql`
7. `T11_pause_schema_rollback.sql` (refuses while any row is open; `@Force = 1` for a non-empty table after restoring)
8. `R1_schedule_schema_rollback.sql`
9. `T1_mapping_schema_rollback.sql` (`@Force = 1` if the send log or refresh log has rows)

Leaving everything in place and just removing the scheduled tasks is harmless: the tables are inert without the scripts.

## 9. Permissions the service account needs

The scripts connect as the service account with Integrated Security. **This list is derived from the code; it has not been tested against a restricted account.** Please confirm on dev with a test login before production. This section is the one complete list of everything the project needs from the database (it includes the automatic registry and extract objects of section 12):

| Who | What |
|---|---|
| **Service account** (Windows login, Integrated Security) | Table and view rights in the tables below and the grant script; **`SELECT, UPDATE` on `Integration.ETL_Tableau_ExtractMapping`** (D-080); `SELECT` on `ETL_Master_Log` and the three lineage tables; database-level `VIEW DEFINITION`; `CONNECT`; `sp_getapplock` (built in, needs no grant) and `SYSDATETIMEOFFSET()`; no DDL, no `EXECUTE` on our own procedures (there are none). |
| **Person who applies the SQL files** | `CREATE TABLE`, `CREATE VIEW`, `ALTER` on schema `Integration` (plus adding extended properties), `SELECT` on `ETL_Master_Log`; in practice `db_ddladmin` or the DBA's own rights (last paragraph of this section). |
| **Whoever sets priorities** | `INSERT, UPDATE, DELETE` on `Tableau_Priority`. |
| **Host (not the database)** | Windows Application event log source `TableauAutomation`. |

**Existing objects (read):**
- `SELECT` on `Integration.ETL_Master_Log`.
- `SELECT` on `Integration.Lineage`, `Integration.DW_NovaLineage` and `Integration.OAO_Lineage` (daily mapping sync, section 12).
- **Metadata visibility: the database-level `VIEW DEFINITION` grant** (`GRANT VIEW DEFINITION TO [DOMAIN\svc_tableau];`). A schema-level grant is not enough: the view-resolution query follows each Tableau view down to its base tables through `sys.sql_expression_dependencies`, and that chain crosses the schemas `Tableau`, `dbo` and `Integration` before it reaches `Dimension` and `Fact`; one object the login cannot see ends the chain. Without it the query returns nothing, every workbook looks like it has no tables (fails closed: nothing sends), and the ambiguity check of the automatic registry does not fail closed (12.6 (b)). No `SELECT` on the base tables is needed.
- `SELECT, UPDATE` on `Integration.ETL_Tableau_ExtractMapping` (the old refresh job's list; `extract_takeover.py` only; D-080). Without `UPDATE` a new takeover is refused (fail closed) because the old job would keep refreshing the extract; a restore then reports a problem and the row stays open.
- No access is needed to `Tableau_ExtractRefreshLog`.

**New objects:**

| Object | Rights | Used by |
|---|---|---|
| `Tableau_Subscription`, `Tableau_DataSource`, `Tableau_WorkbookDataSource`, `Tableau_Schedule` | `SELECT`, `INSERT`, `UPDATE` | `t2` writes; the others read |
| `Tableau_DataSourceTable` | `SELECT`, `INSERT`, `UPDATE`, `DELETE` | `t2` |
| `Tableau_SubscriptionSendLog` | `SELECT`, `INSERT`, `UPDATE` (no `DELETE`: the trigger would refuse it anyway) | `t6` |
| `Tableau_RefreshJob` | `SELECT`, `INSERT`, `UPDATE` | `t4` writes; `t6`, `daily_summary` read |
| `Tableau_SubscriptionBackup` | `SELECT`, `INSERT`, `UPDATE` | `t11_pause.py`; read by `t4`, `t6`, `daily_summary`, `review_list` |
| `Tableau_StaticTable`, `Tableau_DataSourceOverride`, `Tableau_LineageSource` | `SELECT` only (the DBA maintains them) | views, `t2` |
| `Tableau_PipelineTable` | **`SELECT`, `INSERT`, `DELETE`** in one grant (the daily sync inserts, moves and removes `Auto` rows only and never touches `Manual` rows; `UPDATE` is not needed) | `t2` (through `registry_sync.py`); the views read |
| `Tableau_ExtractTask` | `SELECT`, `INSERT`, `UPDATE` | `t2` (MERGE), `extract_takeover.py` (restore); `t4` through the view |
| `ETL_Tableau_ExtractMapping` (existing, the old refresh job's table) | `SELECT`, `UPDATE` (column `Active_Flag` only is changed) | `extract_takeover.py` only (D-080) |
| `Tableau_ExtractTaskBackup` | `SELECT`, `INSERT`, `UPDATE` | read **directly** by `t4_refresh.py` (taken-over extracts) and by `t2` (its MERGE leaves a taken-over task alone, and it reads the schedule ids); read **and written** by `extract_takeover.py`; also read through the view |
| `Tableau_Priority` | service account: **`SELECT`** (a missing grant is invisible: `OBJECT_ID` returns NULL and the code silently falls back to by-name order, 5.8). Whoever sets priorities (a person, not the service account): `INSERT`, `UPDATE`, `DELETE` | `t4`, `t6` read; people write |
| the four `vw_Tableau_*` readiness views, `vw_Tableau_ExtractSchedule`, `vw_Tableau_PriorityPicker` | `SELECT` | all scripts; `t4` (`vw_Tableau_ExtractSchedule`); people (`vw_Tableau_PriorityPicker`) |
| database | `CONNECT`; no DDL; no `EXECUTE` on procedures other than the built-in `sp_getapplock` | |

A suggested grant script (for the DBA to review, not run by us), **one script for everything above**:
```sql
-- run as the DBA after the SQL files; replace [DOMAIN\svc_tableau] with the real service account
GRANT SELECT ON Integration.ETL_Master_Log TO [DOMAIN\svc_tableau];
GRANT SELECT, UPDATE ON Integration.ETL_Tableau_ExtractMapping TO [DOMAIN\svc_tableau];   -- old refresh job's list: the takeover switches Active_Flag (D-080)
GRANT SELECT ON Integration.Lineage TO [DOMAIN\svc_tableau];
GRANT SELECT ON Integration.DW_NovaLineage TO [DOMAIN\svc_tableau];
GRANT SELECT ON Integration.OAO_Lineage TO [DOMAIN\svc_tableau];
GRANT SELECT, INSERT, UPDATE ON Integration.Tableau_Subscription TO [DOMAIN\svc_tableau];
GRANT SELECT, INSERT, UPDATE ON Integration.Tableau_DataSource TO [DOMAIN\svc_tableau];
GRANT SELECT, INSERT, UPDATE ON Integration.Tableau_WorkbookDataSource TO [DOMAIN\svc_tableau];
GRANT SELECT, INSERT, UPDATE ON Integration.Tableau_Schedule TO [DOMAIN\svc_tableau];
GRANT SELECT, INSERT, UPDATE, DELETE ON Integration.Tableau_DataSourceTable TO [DOMAIN\svc_tableau];
GRANT SELECT, INSERT, UPDATE ON Integration.Tableau_SubscriptionSendLog TO [DOMAIN\svc_tableau];
GRANT SELECT, INSERT, UPDATE ON Integration.Tableau_RefreshJob TO [DOMAIN\svc_tableau];
GRANT SELECT, INSERT, UPDATE ON Integration.Tableau_SubscriptionBackup TO [DOMAIN\svc_tableau];
GRANT SELECT, INSERT, DELETE ON Integration.Tableau_PipelineTable TO [DOMAIN\svc_tableau];
GRANT SELECT ON Integration.Tableau_StaticTable TO [DOMAIN\svc_tableau];
GRANT SELECT ON Integration.Tableau_DataSourceOverride TO [DOMAIN\svc_tableau];
GRANT SELECT ON Integration.Tableau_LineageSource TO [DOMAIN\svc_tableau];
GRANT SELECT, INSERT, UPDATE ON Integration.Tableau_ExtractTask TO [DOMAIN\svc_tableau];
GRANT SELECT, INSERT, UPDATE ON Integration.Tableau_ExtractTaskBackup TO [DOMAIN\svc_tableau];
GRANT SELECT ON Integration.Tableau_Priority TO [DOMAIN\svc_tableau];          -- missing = marks silently ignored
GRANT SELECT ON Integration.vw_Tableau_DataSourceTableStatus TO [DOMAIN\svc_tableau];
GRANT SELECT ON Integration.vw_Tableau_DataSourcePipeline TO [DOMAIN\svc_tableau];
GRANT SELECT ON Integration.vw_Tableau_DataSourceReadiness TO [DOMAIN\svc_tableau];
GRANT SELECT ON Integration.vw_Tableau_WorkbookReadiness TO [DOMAIN\svc_tableau];
GRANT SELECT ON Integration.vw_Tableau_ExtractSchedule TO [DOMAIN\svc_tableau];
GRANT SELECT ON Integration.vw_Tableau_PriorityPicker TO [DOMAIN\svc_tableau];
-- metadata for the view-resolution query: DATABASE level (schema level is not enough, see above)
GRANT VIEW DEFINITION TO [DOMAIN\svc_tableau];

-- for whoever sets priorities (a person or group, not the service account):
-- GRANT INSERT, UPDATE, DELETE ON Integration.Tableau_Priority TO [DOMAIN\priority_setters];
-- and SELECT on Tableau_Priority and vw_Tableau_PriorityPicker so that person can look up the ids.
```
The Tableau side needs no new right for reading tasks and schedules. Deleting and re-creating a task (extract takeover and restore) needs the rights of the token's user on the data source and the schedule (a site or server administrator). **Not tested with a restricted account.**

The person who **applies the SQL files** needs `CREATE TABLE`, `CREATE VIEW` and `ALTER` on schema `Integration` (create triggers, add columns, and **add the extended properties** `created_by`: adding an extended property to an object needs `ALTER` on its schema) and `SELECT` on `ETL_Master_Log`; in practice `db_ddladmin` or the DBA's own rights.

**Not a database matter but needed on the host:** the service account must be allowed to write the Windows Application event log under the source `TableauAutomation` (the source is registered once as administrator). The scripts write it with `eventcreate.exe`; whether `eventcreate` works for a non-administrator account is **not tested**. If it fails, the scripts log a warning and carry on.

## 10. Applied on dev, with dates (from STATUS and DECISIONS)

| What | Applied on dev |
|---|---|
| Base mapping schema | 2026-10-22 (first version; reviewed version re-created the same day) |
| Pipeline registry load | 2026-10-22 (32 + 1 static); 33rd row 2026-10-23 |
| Schedule table and column | 2026-10-23 |
| Takeover backup table (and its three later columns) | 2026-10-23 |
| Send-log delete guard | 2026-10-23 |
| Takeover-all change | 2026-10-23 (backup table was empty) |
| Workbook change-time column | 2026-10-23 |
| Non-warehouse sources | 2026-10-26 |
| Run order table and view (`R9_priority.sql`) | 2026-11-03 |
| Automatic registry and extract refresh schedules (section 12) | 2026-11-04 (the `NoRetake` column the same day) |

Dev state checked read-only on 2026-11-04: 13 + 1 + 1 + 4 (ExtractAuto) tagged objects as expected, 15 `Tableau_*` tables, 6 views, 4 triggers; all four triggers present and enabled; `WasSuspended` column absent; `ScheduleId`, `WorkbookUpdatedAt`, `GoneSince`, `NonDwhConnection` present; the readiness view contains `NotChecked`. One caution: the base schema file on disk was edited after dev was built (it now includes the non-warehouse change). A fresh rebuild of dev from the files would give the same objects; this has not been done.

## 11. Dev-only changes made for testing (NOT part of the deployment)

These exist only on dev (or are test data in dev). **None of them is to be repeated or recreated on production.** For each: what it is, and how to undo it.

### 11.1 Archive copies and trimmed fat tables (decision: UAT test set-up)

- **What:** on 2026-10-26 (about 17:31) two full copies were made on dev: `Fact.DastTradeConfirmationPNL_archive_20261026` (42,948 rows) and `Fact.DailyNovaTradeVolume_archive_20261026` (4,050,007 rows). The original tables were then trimmed in place (no rename, so views, procedures and indexes keep working): `Fact.DastTradeConfirmationPNL` keeps **6 rows** (its last month of data, trade date 2026-09-09); `Fact.DailyNovaTradeVolume` keeps the **newest 100,000 rows** by key (every row has the same order day, so a date cut was impossible). Counts verified on dev 2026-10-26: 6 / 42,948 / 100,000 / 4,050,007. On 2026-11-04 the Nova table holds **100,012** rows: the extra 12 are the test rows of 11.7. Reason: these tables made the test workbooks' extracts very slow to refresh.
- **Properties:** the archives are heap copies with the same 111 and 36 columns, **no primary key and no indexes**.
- **Caution:** the next real dev load (DailyETL for PNL, NovaDailyETL for Nova) may refill or change the originals, so the trimmed state is not guaranteed to stay.
- **Undo (dev only, DBA):**
  - Check first that the original still has only the trimmed rows: `SELECT COUNT(*) FROM Fact.DastTradeConfirmationPNL;` (6) and `SELECT COUNT(*) FROM Fact.DailyNovaTradeVolume;` (100,000, or 100,012 while the rows of 11.7 are in). If a load has run since, decide by hand which rows win.
  - `Fact.DastTradeConfirmationPNL` has no identity and no key: restore by `DELETE FROM Fact.DastTradeConfirmationPNL;` then `INSERT INTO Fact.DastTradeConfirmationPNL SELECT * FROM Fact.DastTradeConfirmationPNL_archive_20261026;` (the archive is the full table, so this gives the original 42,948 rows back).
  - `Fact.DailyNovaTradeVolume` has an identity key (`DailyNovaTradeVolumeKey`, primary key `PK__DailyNov__849A805CC9987F9E`). Restore the missing rows with an explicit column list: `SET IDENTITY_INSERT Fact.DailyNovaTradeVolume ON; INSERT INTO Fact.DailyNovaTradeVolume (<all 36 columns>) SELECT <all 36 columns> FROM Fact.DailyNovaTradeVolume_archive_20261026 a WHERE NOT EXISTS (SELECT 1 FROM Fact.DailyNovaTradeVolume t WHERE t.DailyNovaTradeVolumeKey = a.DailyNovaTradeVolumeKey); SET IDENTITY_INSERT Fact.DailyNovaTradeVolume OFF;` (list the columns from `sys.columns`).
  - When no longer needed, drop the two archive tables: `DROP TABLE Fact.DastTradeConfirmationPNL_archive_20261026; DROP TABLE Fact.DailyNovaTradeVolume_archive_20261026;` (about 1.4 GB).

### 11.2 Deactivated mapping rows (stale-mapping clean-up)

- **What:** on 2026-10-26, mapping rows for subscriptions and links that no longer matched the test set-up were set `Active_Flag = 0` on dev so the old UAT content would not be sent or counted. The list of what was deactivated is in `out\mapping_cleanup_20261026.json` (local to the project folder, not in git): 52 subscription ids (`subs`) and 86 workbook-to-data-source pairs (`links`); the file has no data source list. Dev state on 2026-11-04: 62 of 67 subscription rows, 83 of 92 link rows and 82 of 91 data source rows are inactive (the mapping sync added further deactivations of its own after the clean-up).
- **Undo:** `UPDATE Integration.Tableau_Subscription SET Active_Flag = 1 WHERE Subscription_id IN (<ids from the file>)` and the same for links by (`Workbook_id`, `DataSource_id`). Note that the daily mapping sync sets `Active_Flag` itself from what Tableau returns, so the next run will deactivate again any row whose subscription is gone from Tableau. Nothing was deleted.

### 11.3 Test rows to be inserted into `Integration.ETL_Master_Log` on the test days (decision: UAT test set-up)

- **What:** the user approved inserting rows that say a load finished on the test day, so the send test can pass the "data is ready" gate on dev. The table has no tag column: only `JobID` (identity), `PipelineName`, `EndTime`, `Success`. The user said no marking is needed. On 2026-11-04 the table holds 27 rows (JobID 367 to 417; the newest is JobID 417, `CQGlobalSGDW: NovaDailyETL`, 2026-11-04 10:00:52, a real run). The table cannot tell test rows from real ones; none of the planned test rows is recorded here. The check scripts never insert permanent rows (11.9).
- **How it will be done:** insert one row per needed pipeline (`CQGlobalSGDW: DailyETL`, `CQGlobalSGDW: NovaDailyETL`, `CQGlobalSGDW: LeadGenETL`) with `Success = 1` and an `EndTime` on the test day, using `OUTPUT INSERTED.JobID` and writing the returned JobIDs into a local note.
- **Undo:** delete **only** those JobIDs: `DELETE FROM Integration.ETL_Master_Log WHERE JobID IN (<the noted ids>);` Never delete by date or pipeline name (real rows of the same day would go too). Real rows are never touched.
- **Risk:** anything else that reads `ETL_Master_Log` on dev (for example a copy of the old refresh job, or reports) sees the test rows until they are deleted.

### 11.4 Leftover fixture row in the send log from the base-schema test file

- **What:** running `T1_tests.sql` on 2026-10-26 (after the send-log delete guard existed) left `Tableau_SubscriptionSendLog` row `SendLog_id = 2152`, `Subscription_id = 'TEST-sub'`, status `Sent`, send date 2026-10-26, plus the inactive subscription row `Tableau_Subscription.Subscription_id = 'TEST-sub'` (workbook `TEST-wb`). The guard refuses to delete it (the test name does not start with `T6TEST-`, `T11TEST-` or `R1TEST-`), and a `Sent` row can never change status. It is harmless (inactive subscription, never sent by a real run) but it makes the base-schema rollback ask for `@Force = 1` on dev.
- **Undo (dev only, DBA):**
```sql
DISABLE TRIGGER Integration.trg_Tableau_SubscriptionSendLog_NoDeleteOfSlot ON Integration.Tableau_SubscriptionSendLog;
DELETE FROM Integration.Tableau_SubscriptionSendLog WHERE SendLog_id = 2152 AND Subscription_id = 'TEST-sub';
DELETE FROM Integration.Tableau_Subscription WHERE Subscription_id = 'TEST-sub' AND Workbook_id = 'TEST-wb';
ENABLE TRIGGER Integration.trg_Tableau_SubscriptionSendLog_NoDeleteOfSlot ON Integration.Tableau_SubscriptionSendLog;
```
- **Do not run `T1_tests.sql` again** on any database that matters; its own header says so. On production its guard stops it, and it must never get the production server name.

### 11.5 Test takeovers on dev (rows and Tableau state)

- **What:** the subscription backup table on dev holds 11 rows on 2026-11-04, 10 of them open: `Backup_id 1000` (the older CFD Accounts pilot subscription, taken over 2026-10-26, deleted in Tableau at that time; check before relying on this), `1369` to `1372` (four test subscriptions taken over on 2026-10-26 on UAT Tableau) and `1878` to `1883` (later UAT test takeovers; `1883` was restored on 2026-10-27). The open ones are really **suspended in Tableau UAT** until restored (their current Tableau state was not re-checked for this document). The extract task backup table is empty: no extract task was ever taken over.
- **Undo:** `python scripts\t11_pause.py --restore <subscription id> --execute` per subscription (or `--restore-all-for-workbook`). While open rows exist, the rollbacks of the takeover table, the schedule file and the base schema refuse to run on dev.

### 11.6 Test files and objects that are not tables

`out\` (logs, backups, markers, sample `.eml` files, `uat_suspended_20261026.json`, `uat_deleted_20261026.json`) is local, gitignored and not part of the deployment. The test sets in `T1_tests.sql` and the eight `*_checks.py` files create `TEST-*`, `T6TEST-*`, `T11TEST-*`, `R1TEST-*` and `T4TEST` rows and remove them again (some inside a rolled-back transaction). They are never run on production.

### 11.7 Test clients and trades for the inactive-client workbook (ZZT rows)

- **What:** the script `testdata/dummy_inactive_clients_nova.sql` (moved out of `scripts/sql`; **never ship it**: its own guard names the dev server) was run on dev. It inserted **6 rows into `Dimension.Client`** (`Client_No` ZZT0001 to ZZT0006), **6 rows into `Dimension.Account`** (`AccountNo` ZZT0001 to ZZT0006) and **12 rows into `Fact.DailyNovaTradeVolume`** (two per account), so that a test workbook has clients in its 90-to-180-day inactive window. Verified on dev 2026-11-04: 6 / 6 / 12; the Nova table therefore holds 100,012 rows (11.1).
- **Why it matters to a DBA:** these are rows in tables our code only reads, in a warehouse that the real ETL loads. They exist on dev only.
- **Undo (dev only):** the three `DELETE` statements at the top of that script:
```sql
DELETE FROM Fact.DailyNovaTradeVolume WHERE AccountNo LIKE 'ZZT%';
DELETE FROM Dimension.Account         WHERE AccountNo LIKE 'ZZT%';
DELETE FROM Dimension.Client          WHERE Client_No LIKE 'ZZT%';
```

### 11.8 Objects that are the owner's own, not created by this project, not part of the deliverable

The owner confirmed (2026-11-04) that these three dev objects are his own work: the table `Dimension.Client_Scoring_Archive_20261026` (8,981 rows, created 2026-10-27 12:32), the view `Tableau.vw_Client_Score` and the procedure `Integration.MigrateStagedClientData`. **Nothing in this project creates or changes them** (no script or SQL file of ours writes to them). They are not part of the deliverable: do not ship them with the project, and do not touch them as part of its deployment or rollback. (`MigrateStagedClientData` is only named in the pipeline proposal as one of the procedures that load `Dimension.Client`.)

### 11.9 Gap in the `ETL_Master_Log` identity (harmless)

`Integration.ETL_Master_Log.JobID` is an identity column. The check scripts insert fixture rows inside transactions that are always rolled back (`scripts/check_fixtures.py`), and a rollback does not give identity values back. On 2026-11-04 the next identity value is above 463 while the highest `JobID` is 417, so the numbering has a gap. Nothing depends on gap-free numbers; no row is affected.

## 12. Automatic registry and extract refresh schedules (added 2026-11-04, decisions D-066 to D-079)

One new SQL file, **`ExtractAuto_schema.sql`** (rollback `ExtractAuto_schema_rollback.sql`, tag `created_by = 'ExtractAuto_schema'`). Step 10 in the order of section 3: it needs step 1 (it changes `Tableau_PipelineTable` and the readiness views read it) and step 3 (extract schedules are stored in `Tableau_Schedule`). Re-runnable, guarded to dev like the others (section 4: it and its rollback are two of the 19 files whose guard must change for production). **Apply it before the code is deployed** (section 3). Applied on dev 2026-11-04. Nothing in an existing object is dropped or rewritten; `ETL_Master_Log`, `Tableau_ExtractRefreshLog` is not touched, and `ETL_Tableau_ExtractMapping` only has its `Active_Flag` switched by the extract takeover (6.8); the three lineage tables and `ETL_Master_Log` are only read.

### 12.1 Every new or changed object

| # | Object | New / changed | Purpose |
|---|---|---|---|
| 1 | `Integration.Tableau_PipelineTable.Source` (`varchar(10) NOT NULL`, default `'Manual'`, check `Manual` or `Auto`; constraints `DF_Tableau_PipelineTable_Source`, `CK_Tableau_PipelineTable_Source`) | **changed** (one column added to our own table) | Who owns a registry row. All 33 existing rows became `Manual` (the override: the sync never changes or removes them). Rows the daily sync derives from the lineage tables are `Auto`. |
| 2 | `Integration.Tableau_LineageSource` | new table | One row per lineage table (`Lineage`, `DW_NovaLineage`, `OAO_Lineage`) with its default pipeline (the three rows the user confirmed in D-009 / D-025). Used when the ETL log gives no timing evidence. Seeded by the file. |
| 3 | `Integration.Tableau_ExtractTask` | new table | The Tableau extract refresh tasks (task id, data source or workbook id, schedule id, priority, failed count), written by the daily mapping sync. Tasks that vanish are set `Active_Flag = 0`, except a task the script took over. |
| 4 | `Integration.Tableau_ExtractTaskBackup` | new table | One row per takeover of a task: everything needed to re-create it (schedule id and name, priority, failed count, last run, data source owner id; no recipient addresses), when the delete was confirmed, when and as which new task it was restored. Written before the task is deleted in Tableau. |
| 5 | `Integration.vw_Tableau_ExtractSchedule` | new view | Per extract data source: its tasks and schedules, and whether the takeover is confirmed. Read by `t4_refresh.py`. |
| 6 | `Integration.Tableau_Schedule` | **existing table, new use** | Extract schedules are stored here too (same columns as subscription schedules). No change to the table. |
| 7 | `Integration.Tableau_ExtractTaskBackup.NoRetake` (`bit NOT NULL`, default 0, constraint `DF_Tableau_ExtractTaskBackup_NoRetake`) | column added later in the same file (additive, re-runnable; D-076) | Set to 1 by `--restore`: the daily takeover run does not take that extract over again until a person sets it back to 0. Applied on dev 2026-11-04. Undo of this column alone: drop the constraint, then the column; but **`extract_takeover.py` then refuses to start, `--restore` included** (12.4). The full rollback drops the table with it. |
| 8 | `Integration.Tableau_ExtractTaskBackup.OldJobMappingIds` (`nvarchar(1000) NULL`), `OldJobOriginalFlag` (`bit NULL`), `OldJobSwitchedAt` (`datetimeoffset(0) NULL`), `OldJobRestoredAt` (`datetimeoffset(0) NULL`) | columns added later in the same file (additive, re-runnable; D-080) | The old refresh job's rows (`ETL_Tableau_ExtractMapping.Mapping_id`) that the takeover switched off, their original `Active_Flag` (always 1) and when they were switched off and back. All NULL when nothing was switched off. Applied on dev 2026-11-04. Undo of these columns alone: drop the four columns; **`extract_takeover.py` then refuses to start** (12.4). The full rollback drops the table with them. |

Constraints and indexes created with them: primary keys `PK_Tableau_LineageSource`, `PK_Tableau_ExtractTask`, `PK_Tableau_ExtractTaskBackup`; `CK_Tableau_ExtractTask_TargetType`, `CK_Tableau_ExtractTaskBackup_NeedsTakeover`; indexes `IX_Tableau_ExtractTask_Target`, `UX_Tableau_ExtractTaskBackup_Open` (one open row per task), `IX_Tableau_ExtractTaskBackup_Target`.

### 12.2 Who needs which permission (service account)

The rights for these objects are part of the single table and the single grant script in **section 9** (do not run a second, separate script). In short: `Tableau_PipelineTable` changes from `SELECT` to `SELECT, INSERT, DELETE` (the sync inserts, moves and removes `Auto` rows and never touches `Manual` rows; `UPDATE` is not needed); `Tableau_LineageSource` `SELECT`; the three existing lineage tables `SELECT`; `Tableau_ExtractTask` and `Tableau_ExtractTaskBackup` `SELECT, INSERT, UPDATE`; `vw_Tableau_ExtractSchedule` `SELECT`; existing `ETL_Tableau_ExtractMapping` `SELECT, UPDATE` (D-080). `Tableau_Schedule` and `Tableau_RefreshJob` are unchanged (`t4` now also reads `Tableau_RefreshJob` for the schedule rule; it already could).

### 12.3 Who reads and writes (addition to section 7)

`Tableau_PipelineTable`: now also written by `t2_mapping_sync.py` (`Auto` rows only). `Tableau_ExtractTask`: written by `t2` and `extract_takeover.py`, read through the view. `Tableau_ExtractTaskBackup`: read and written by `extract_takeover.py`; read **directly** by `t4_refresh.py` (the taken-over extracts) and by `t2` (its MERGE leaves a taken-over task alone, and it reads the schedule ids of open rows); also read through `vw_Tableau_ExtractSchedule`. `Tableau_LineageSource`: read by `t2`. `Tableau_Schedule`: also holds extract schedules (written by `t2`, read by `t4`).

### 12.4 Rollback (the first step of section 8, newest first)

First restore every taken-over extract task (`python scripts\extract_takeover.py --restore-all --execute`): `ExtractAuto_schema_rollback.sql` **refuses while any takeover row is open** (not bypassed by `@Force`) and keeps a non-empty backup table unless `@Force = 1`. It drops the view and the three new tables, **deletes the `Auto` registry rows** (their count is printed; the extracts behind them then show `UnmappedTable`, fail closed, until the rows are loaded by hand) and drops the `Source` column with its two constraints. **Order: roll the CODE back first** (scripts, scheduled tasks, or at least stop `t2_mapping_sync.py`, `extract_takeover.py` and `t4_refresh.py`), then the SQL. `t2_mapping_sync.py` checks at start that the `Source` column and the three new tables exist (`schema_ready`) and `extract_takeover.py` checks for the two tables and the columns `NoRetake` and `OldJobMappingIds`; `t4_refresh.py` reads the view. If the SQL is rolled back first, the daily sync fails entirely (fatal, mail `t2_failed`) and writes nothing until the code is rolled back or the SQL is applied again. The rollback script also prints a WARNING when a backup row switched off old-job rows that were never switched back (check `Active_Flag` in `ETL_Tableau_ExtractMapping` before the table is dropped; an open row is refused anyway). **Dropping the `NoRetake` or `OldJobMappingIds` column alone makes `extract_takeover.py` refuse to start, including `--restore` and `--restore-all`**: re-run `ExtractAuto_schema.sql` (it is re-runnable and adds the column back) before restoring anything. Tested on dev inside a rolled-back transaction, with the backup table emptied inside the test transaction and with an open row (`extract_checks.py`; it no longer assumes the real backup table is empty).

To close one backup row by hand (without Tableau), see RUNBOOK section 20, item 10.

### 12.5 Dev-only test objects and data (not part of the deployment)

- No permanent test object. `extract_checks.py` writes fixture rows (`EXTRTEST-%`, schema `EXTRTEST`) into the new tables and into `Tableau_Schedule`, `Tableau_RefreshJob`, `Tableau_DataSource`, `Tableau_DataSourceTable`, `Tableau_PipelineTable` **only inside transactions that are rolled back**; a checksum of the real rows is compared at the end. It also deletes three real registry rows inside such a transaction to prove the derivation (rolled back), and inserts fixture rows (`Etl_Name LIKE 'EXTRTEST-%'`) into `ETL_Tableau_ExtractMapping` to prove the old-job switch-off and switch-back, also only inside a rolled-back transaction (the checksum covers that table). Real dev state of that table (read 2026-11-04): 27 rows, 24 data sources, all `Active_Flag = 1`; no takeover has switched anything off.
- Real dev state (read 2026-11-04): `Tableau_ExtractTask` holds the 23 tasks read from UAT (20 on data sources, 3 on workbooks); `Tableau_ExtractTaskBackup` is empty (**no takeover was run**); `Tableau_PipelineTable` is unchanged (33 `Manual` rows); `Tableau_Schedule` holds 16 rows, 6 of them extract schedules.
- **Undo of that data only:** `DELETE FROM Integration.Tableau_ExtractTask;` (the next sync reads it again) and, if wanted, `DELETE FROM Integration.Tableau_Schedule WHERE Schedule_id NOT IN (SELECT ScheduleId FROM Integration.Tableau_Subscription WHERE ScheduleId IS NOT NULL);`. Full undo of everything in this section: 12.4.
- If a takeover is ever run on dev against UAT, undo the Tableau side with `extract_takeover.py --restore`, never with SQL.
- The checks that need a real refresh target (`t4_checks`, `t11_checks`, `r1_checks`) insert one year-2001 `ETL_Master_Log` row per registered pipeline that has none **inside a transaction that is always rolled back** (`scripts/check_fixtures.py`, D-077). No permanent `DailyETL` row exists or is needed. The only change to dev made by the fix pass: `ALTER TABLE ... ADD NoRetake` (row 7 of 12.1), applied by re-running `ExtractAuto_schema.sql`.

### 12.6 For the DBA to confirm before production (D-077)

(a) **Lineage column names and pipeline names.** `registry_sync.LINEAGE_COLS` holds the column names read on dev for `Integration.Lineage` (`[Table Name]`, `[Data Load Completed]`, `[Was Successful]`), `Integration.DW_NovaLineage` (the same) and `Integration.OAO_Lineage` (`[table_name]`, `[end_time]`, `[was_successful]`). Confirm that production has exactly these columns with the same meaning. Also confirm the three seeded rows of `Integration.Tableau_LineageSource` (`Lineage` is `CQGlobalSGDW: DailyETL`, `DW_NovaLineage` is `CQGlobalSGDW: NovaDailyETL`, `OAO_Lineage` is `CQGlobalSGDW: LeadGenETL`) against the pipeline names in production `ETL_Master_Log`. A wrong column name makes the sync report a problem (exit 2); a wrong pipeline name makes tables show up as unconfirmed or `UnknownPipeline`, never as guessed.
(b) **Metadata visibility across schemas.** The ambiguity check for a bare lineage table name reads `sys.tables` across ALL schemas, and the view-resolution query crosses the schemas `Tableau`, `dbo` and `Integration`. SQL Server shows a login only the objects it may see, so the service account needs the **database-level** grant `GRANT VIEW DEFINITION TO [DOMAIN\svc_tableau];` (in the script of section 9). A schema-level grant is not enough. **Without it the check does not fail closed**: a table name that exists in two schemas would look unique and could be registered on a guess (the evidence rule of D-075 still applies).
(c) **One pipeline per table.** The primary key of `Tableau_PipelineTable` allows several pipelines for one table, but the registry sync derives only one. A table that several pipelines load needs Manual rows (one per pipeline); the sync never changes or removes a Manual row and only reports when the lineage disagrees.
(d) **Rollback order.** Code first, SQL second (12.4). Rolling the SQL back first stops the daily sync.
