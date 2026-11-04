SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') BEGIN RAISERROR('Wrong database or server: only the DEV DWH (<DEV_DWH_SERVER> / CQGlobalSGDW) is allowed; nothing below is run (SET NOEXEC ON).', 16, 1); SET NOEXEC ON; END
GO
/*
T1 - Mapping schema (SQL Server 2017+, needs STRING_AGG). Revision 3. Dev DWH first (D-026). Re-runnable.
The guard above sets SET NOEXEC ON when the database or server is not the DEV DWH (<DEV_DWH_SERVER> / CQGlobalSGDW), so no later batch runs; the last batch switches it off again. Mapping tables added later: Tableau_Schedule and Tableau_Subscription.ScheduleId (R1_schedule_schema.sql), Tableau_DataSource.NonDwhConnection and the NotChecked status (R8_nondwh_sources.sql, D-053), Tableau_SubscriptionBackup (T11_pause_schema.sql), the send-log delete guard (R3_sendlog_guard.sql).

What changed from the first draft, and why (docs/PLAN_REVIEW.md G6, G9, G11, G20, G23; code review):
- Fail closed (D-006): readiness is driven from Tableau_DataSource with LEFT JOINs. vw_Tableau_DataSourceReadiness
  returns ReadyStatus = NoReadinessCheck (explicit override only) | UnknownKind (IsExtract NULL) | NotChecked (positively non-DWH, D-053) | NoTables |
  UnmappedTable | UnknownPipeline (registry pipeline not found in ETL_Master_Log) | Ready.
  vw_Tableau_WorkbookReadiness rolls that up per workbook (NoDataSources | InactiveDataSource | NotReady | Ready).
  A source with NoReadinessCheck counts as passing at workbook level (that is what the override is for).
- Static tables (D-025 item 7): Tableau_StaticTable. They never cause UnmappedTable. A table cannot be both
  static and in the registry (triggers).
- Embedded sources (D-015, D1b): DataSource_id = workbook-side id. LinkedPublishedDatasourceId = published source
  behind an embedded link. RefreshKind = Extract/Live/Unknown from IsExtract. ExtractRefreshTargetId is set only
  for Extract (dedupe the refresh set on it); Unknown never yields a refresh target.
- Subscriptions (D-014, D-023): definition only, recipients are read from Tableau (no cached copy: it goes stale
  and puts personal data in the DWH). A subscription re-created in Tableau gets a NEW Subscription_id, so it has no
  claim history and could send again on a day its predecessor already sent. SubscriptionKeyHash (computed by the
  script from workbook, target, owner, subject; not unique) lets the script alert on near-duplicates.
- Send log: claim-first, at-most-once, see section 8.
- Refresh job log: Singapore-date column, TimedOut status, target id.
- Pipeline names verified against Integration.ETL_Master_Log on dev ('CQGlobalSGDW: DailyETL', '...LeadGenETL',
  '...NovaDailyETL', D-025); the views also check this at read time (PipelineKnown, binary collation).
- Every object created here carries the extended property created_by = 'T1_mapping_schema'; the rollback drops
  only tagged objects.

Design:
- Additive only. ETL_Master_Log, ETL_Tableau_ExtractMapping and Tableau_ExtractRefreshLog are untouched.
- Chain: workbook -> data source -> upstream table -> pipeline (registry) -> ETL_Master_Log. Data source ->
  pipeline is derived by join, never stored (D-002, D-005).
- Pipeline-level readiness only (known limit, G12). 'Ready' means the mapping is complete; whether the pipelines
  succeeded today is T4's check.
- To change a table definition: drop and recreate (rollback script), these tables are empty before T2 runs.
- Rollback: scripts/sql/T1_mapping_schema_rollback.sql
*/

-- 1. Pipeline -> table registry (hand-confirmed, populated by T3) ------------------
IF OBJECT_ID('Integration.Tableau_PipelineTable') IS NULL
CREATE TABLE Integration.Tableau_PipelineTable
(
    PipelineName  varchar(50)   NOT NULL,   -- = ETL_Master_Log.PipelineName, incl. the 'CQGlobalSGDW: ' prefix
    SchemaName    sysname       NOT NULL,
    TableName     sysname       NOT NULL,
    Evidence      nvarchar(400) NULL,
    ConfirmedBy   nvarchar(128) NOT NULL CONSTRAINT DF_Tableau_PipelineTable_ConfirmedBy DEFAULT SUSER_SNAME(),
    ConfirmedAt   datetime2(0)  NOT NULL CONSTRAINT DF_Tableau_PipelineTable_ConfirmedAt DEFAULT SYSDATETIME(),
    CONSTRAINT PK_Tableau_PipelineTable PRIMARY KEY (SchemaName, TableName, PipelineName)
);
GO

-- 2. Static tables: no freshness check needed (e.g. dbo.SalesChannel_Excel, deprecated) -------
IF OBJECT_ID('Integration.Tableau_StaticTable') IS NULL
CREATE TABLE Integration.Tableau_StaticTable
(
    SchemaName sysname       NOT NULL,
    TableName  sysname       NOT NULL,
    Reason     nvarchar(400) NOT NULL,
    SetBy      nvarchar(128) NOT NULL CONSTRAINT DF_Tableau_StaticTable_SetBy DEFAULT SUSER_SNAME(),
    SetAt      datetime2(0)  NOT NULL CONSTRAINT DF_Tableau_StaticTable_SetAt DEFAULT SYSDATETIME(),
    CONSTRAINT PK_Tableau_StaticTable PRIMARY KEY (SchemaName, TableName)
);
GO

-- A table cannot be both static and registered (it would be ambiguous whether it needs a check).
CREATE OR ALTER TRIGGER Integration.trg_Tableau_StaticTable_NotInRegistry
ON Integration.Tableau_StaticTable AFTER INSERT, UPDATE
AS
BEGIN
    SET NOCOUNT ON;
    IF EXISTS (SELECT 1 FROM inserted i JOIN Integration.Tableau_PipelineTable p
                 ON p.SchemaName = i.SchemaName AND p.TableName = i.TableName)
        THROW 50002, 'Table is already in Tableau_PipelineTable; a table cannot be static and registered', 1;
END
GO
CREATE OR ALTER TRIGGER Integration.trg_Tableau_PipelineTable_NotStatic
ON Integration.Tableau_PipelineTable AFTER INSERT, UPDATE
AS
BEGIN
    SET NOCOUNT ON;
    IF EXISTS (SELECT 1 FROM inserted i JOIN Integration.Tableau_StaticTable s
                 ON s.SchemaName = i.SchemaName AND s.TableName = i.TableName)
        THROW 50002, 'Table is already in Tableau_StaticTable; a table cannot be static and registered', 1;
END
GO

-- 3. Data sources as the workbooks see them (written by the T2 sync) ----------------------------
-- DataSource_id = id of the workbook-side data source: an embedded id, or a published luid when the
-- workbook uses the published source directly. Embedded sources link (sqlproxy) to a published source
-- (D1b); that luid is LinkedPublishedDatasourceId. IsExtract is read from the published source, not the shell.
IF OBJECT_ID('Integration.Tableau_DataSource') IS NULL
CREATE TABLE Integration.Tableau_DataSource
(
    DataSource_id                nvarchar(128) NOT NULL CONSTRAINT PK_Tableau_DataSource PRIMARY KEY,
    DataSource_Name              nvarchar(256) NULL,
    LinkedPublishedDatasourceId  nvarchar(128) NULL,
    IsExtract                    bit           NULL,   -- NULL = unknown -> fails closed (RefreshKind 'Unknown')
    RefreshKind                  AS (CASE WHEN IsExtract = 1 THEN 'Extract' WHEN IsExtract = 0 THEN 'Live' ELSE 'Unknown' END) PERSISTED,
    -- Where upstream table rows may be stored: the published source if linked, else the row itself.
    UpstreamDataSourceId         AS (ISNULL(LinkedPublishedDatasourceId, DataSource_id)) PERSISTED,
    -- What gets refreshed. Only for extracts; group by this to refresh each extract once. NULL for Live/Unknown.
    ExtractRefreshTargetId       AS (CASE WHEN IsExtract = 1 THEN ISNULL(LinkedPublishedDatasourceId, DataSource_id) END) PERSISTED,
    -- R8 / D-053: T2 sets 1 only when it POSITIVELY identified every skipped connection as non-DWH (Excel/CSV/other database).
    -- With no DWH table such a source is 'NotChecked' (counts as ready). 0 = unknown = fails closed.
    NonDwhConnection             bit           NOT NULL CONSTRAINT DF_Tableau_DataSource_NonDwh DEFAULT 0,
    LastSeenAt                   datetime2(0)  NOT NULL CONSTRAINT DF_Tableau_DataSource_LastSeenAt DEFAULT SYSDATETIME(),
    Active_Flag                  bit           NOT NULL CONSTRAINT DF_Tableau_DataSource_Active DEFAULT 1
);
GO
IF COL_LENGTH('Integration.Tableau_DataSource', 'NonDwhConnection') IS NULL   -- R8: an older dev table gets the column here too
    ALTER TABLE Integration.Tableau_DataSource ADD NonDwhConnection bit NOT NULL CONSTRAINT DF_Tableau_DataSource_NonDwh DEFAULT 0;
GO

-- 4. Workbook <-> data source (D-015: the same connection used twice is two rows with two ids) ----
-- Active_Flag = 0 means the link no longer exists in the workbook (ignored by workbook readiness).
IF OBJECT_ID('Integration.Tableau_WorkbookDataSource') IS NULL
CREATE TABLE Integration.Tableau_WorkbookDataSource
(
    Workbook_id   nvarchar(128) NOT NULL,
    Workbook_Name nvarchar(256) NOT NULL,
    DataSource_id nvarchar(128) NOT NULL CONSTRAINT FK_Tableau_WorkbookDataSource_DataSource
                      REFERENCES Integration.Tableau_DataSource (DataSource_id),
    LastSeenAt    datetime2(0)  NOT NULL CONSTRAINT DF_Tableau_WorkbookDataSource_LastSeenAt DEFAULT SYSDATETIME(),
    Active_Flag   bit           NOT NULL CONSTRAINT DF_Tableau_WorkbookDataSource_Active DEFAULT 1,
    CONSTRAINT PK_Tableau_WorkbookDataSource PRIMARY KEY (Workbook_id, DataSource_id)
);
GO

-- 5. Data source -> upstream base tables (T2 sync; views already resolved to base tables) ---------
IF OBJECT_ID('Integration.Tableau_DataSourceTable') IS NULL
CREATE TABLE Integration.Tableau_DataSourceTable
(
    DataSource_id nvarchar(128) NOT NULL,
    SchemaName    sysname       NOT NULL,
    TableName     sysname       NOT NULL,
    LastSeenAt    datetime2(0)  NOT NULL CONSTRAINT DF_Tableau_DataSourceTable_LastSeenAt DEFAULT SYSDATETIME(),
    CONSTRAINT PK_Tableau_DataSourceTable PRIMARY KEY (DataSource_id, SchemaName, TableName)
);
GO

-- 6. Explicit "no readiness check" override (the only way to get 'NoReadinessCheck'; rows with 0 are ignored) --
IF OBJECT_ID('Integration.Tableau_DataSourceOverride') IS NULL
CREATE TABLE Integration.Tableau_DataSourceOverride
(
    DataSource_id    nvarchar(128) NOT NULL CONSTRAINT PK_Tableau_DataSourceOverride PRIMARY KEY,
    NoReadinessCheck bit           NOT NULL CONSTRAINT DF_Tableau_DataSourceOverride_NRC DEFAULT 1,
    Reason           nvarchar(400) NOT NULL,
    SetBy            nvarchar(128) NOT NULL CONSTRAINT DF_Tableau_DataSourceOverride_SetBy DEFAULT SUSER_SNAME(),
    SetAt            datetime2(0)  NOT NULL CONSTRAINT DF_Tableau_DataSourceOverride_SetAt DEFAULT SYSDATETIME()
);
GO

-- 7. Subscriptions: the definition to replicate (D-014, D-023). Recipients are read from Tableau. ---
IF OBJECT_ID('Integration.Tableau_Subscription') IS NULL
CREATE TABLE Integration.Tableau_Subscription
(
    Subscription_id nvarchar(128) NOT NULL CONSTRAINT PK_Tableau_Subscription PRIMARY KEY,
    Workbook_id     nvarchar(128) NOT NULL,
    TargetType      varchar(10)   NOT NULL,
    TargetId        nvarchar(128) NOT NULL,
    Format          varchar(12)   NOT NULL,   -- D-023: keep the original format
    Subject         nvarchar(500) NULL,
    Message         nvarchar(2000) NULL,
    PageSize        varchar(20)   NULL,
    Orientation     varchar(20)   NULL,
    IsSuspended     bit           NOT NULL CONSTRAINT DF_Tableau_Subscription_Susp DEFAULT 0,  -- information only (D-046: suspended subscriptions are taken over and sent like the others)
    ScheduleType    varchar(50)   NULL,
    SubscriptionKeyHash varbinary(32) NULL,   -- hash(workbook, target, owner, subject), set by the script; see header
    LastSeenAt      datetime2(0)  NOT NULL CONSTRAINT DF_Tableau_Subscription_LastSeenAt DEFAULT SYSDATETIME(),
    Active_Flag     bit           NOT NULL CONSTRAINT DF_Tableau_Subscription_Active DEFAULT 1,
    CONSTRAINT CK_Tableau_Subscription_TargetType CHECK (TargetType IN ('Workbook','View')),
    CONSTRAINT CK_Tableau_Subscription_Format     CHECK (Format IN ('PDF','Image','PDFAndImage'))
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Tableau_Subscription_KeyHash'
               AND object_id = OBJECT_ID('Integration.Tableau_Subscription'))
CREATE INDEX IX_Tableau_Subscription_KeyHash ON Integration.Tableau_Subscription (SubscriptionKeyHash);
GO

-- 8. Send log: one slot per SUBSCRIPTION per Singapore day (D-014, D-016, G9) -----------------------
-- SCRIPT CONTRACT (claim-first, at-most-once; the bias is "never double email"):
--   0. Take an application lock for the run (sp_getapplock, @LockOwner='Session', exclusive, no wait) so two
--      runs of the script cannot overlap; the unique index below is the safety net, not the first line.
--   1. Before exporting or mailing, INSERT a 'Claimed' row (ClaimedAt defaults to now; SendDate is computed from
--      it, in Singapore time, and cannot be set). The filtered unique index covers Claimed, Sent AND TimedOut,
--      so while any of those exists for the subscription and day, another claim fails with error 2601.
--   2. After SMTP accepts the mail: UPDATE ... SET Status='Sent', SentAt=SYSDATETIMEOFFSET()
--      WHERE SendLog_id=@id AND Status IN ('Claimed','TimedOut'). This update must tolerate failure (0 rows or
--      an error): the mail is already out, so log it, do not retry the mail, and do not crash the batch.
--   3. 'Failed' may be used ONLY for failures BEFORE the SMTP DATA command (export error, connect/auth/TLS
--      error, recipient rejected at RCPT). It frees the slot and the next run retries. Any ambiguous error
--      (timeout, connection lost during or after DATA, crash) must leave the row 'Claimed'.
--   4. Stale claim: at startup, rows with Status='Claimed' and ClaimedAt older than the claim timeout become
--      'TimedOut' and the team is alerted (D-018). TimedOut STILL blocks the slot. Only a human releases it, by
--      setting that row to 'Released' (mail confirmed not sent, resend allowed) or 'Failed'. If the human confirms
--      it was sent, set it to 'Sent' (with SentAt). The claim timeout (90 min, D-029) MUST be longer than the Task
--      Scheduler ExecutionTimeLimit, so a straggler process cannot outlive its claim. NOTE the deployed task has a
--      1 h limit (PLAN_REVIEW G8): the 90 min timeout is above that limit.
--   5. A 'Sent' row is final: a trigger rejects any update that changes its Status, and a CHECK requires SentAt.
--   Trade-off: a crash after the mail left means a human must look; the residual risk is one lost or late mail,
--   never a duplicate. 'DryRun' rows record a dry run and never block anything. 'Released' never blocks.
IF OBJECT_ID('Integration.Tableau_SubscriptionSendLog') IS NULL
CREATE TABLE Integration.Tableau_SubscriptionSendLog
(
    SendLog_id      int IDENTITY(1,1) CONSTRAINT PK_Tableau_SubscriptionSendLog PRIMARY KEY,
    Subscription_id nvarchar(128) NOT NULL CONSTRAINT FK_Tableau_SubscriptionSendLog_Subscription
                        REFERENCES Integration.Tableau_Subscription (Subscription_id),
    Status          varchar(10)   NOT NULL,
    ClaimedAt       datetimeoffset(0) NOT NULL CONSTRAINT DF_Tableau_SubscriptionSendLog_ClaimedAt DEFAULT SYSDATETIMEOFFSET(),
    -- Singapore calendar date of the claim (UTC+8, no DST). Computed, so the script cannot override it.
    SendDate        AS (CAST(SWITCHOFFSET(ClaimedAt, '+08:00') AS date)) PERSISTED,
    SentAt          datetimeoffset(0) NULL,
    RunId           varchar(64)   NULL,
    Message         nvarchar(1000) NULL,
    CONSTRAINT CK_Tableau_SubscriptionSendLog_Status CHECK (Status IN ('Claimed','Sent','Failed','DryRun','TimedOut','Released')),
    CONSTRAINT CK_Tableau_SubscriptionSendLog_SentAt CHECK (Status <> 'Sent' OR SentAt IS NOT NULL)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_Tableau_SubscriptionSendLog_OncePerDay'
               AND object_id = OBJECT_ID('Integration.Tableau_SubscriptionSendLog'))
CREATE UNIQUE INDEX UX_Tableau_SubscriptionSendLog_OncePerDay
    ON Integration.Tableau_SubscriptionSendLog (Subscription_id, SendDate)
    WHERE Status IN ('Claimed','Sent','TimedOut');
GO
CREATE OR ALTER TRIGGER Integration.trg_Tableau_SubscriptionSendLog_SentIsFinal
ON Integration.Tableau_SubscriptionSendLog AFTER UPDATE
AS
BEGIN
    SET NOCOUNT ON;
    IF EXISTS (SELECT 1 FROM deleted d JOIN inserted i ON i.SendLog_id = d.SendLog_id
               WHERE d.Status = 'Sent' AND i.Status <> 'Sent')
        THROW 50001, 'A Sent row is final: its Status cannot change', 1;
END
GO

-- 9. Refresh job log: one row per submitted job, updated when it finishes (T4/T5) ----------------
-- DataSource_id here is the refresh TARGET (Tableau_DataSource.ExtractRefreshTargetId).
-- Status: Submitted -> Succeeded | Failed | Cancelled | TimedOut (script gave up waiting, G20).
IF OBJECT_ID('Integration.Tableau_RefreshJob') IS NULL
CREATE TABLE Integration.Tableau_RefreshJob
(
    RefreshJob_id  int IDENTITY(1,1) CONSTRAINT PK_Tableau_RefreshJob PRIMARY KEY,
    DataSource_id  nvarchar(128) NOT NULL,
    TableauJobId   nvarchar(64)  NULL,
    SubmittedAt    datetimeoffset(0) NOT NULL CONSTRAINT DF_Tableau_RefreshJob_SubmittedAt DEFAULT SYSDATETIMEOFFSET(),
    SubmittedDate  date          NOT NULL CONSTRAINT DF_Tableau_RefreshJob_SubmittedDate
                       DEFAULT CAST(SYSDATETIMEOFFSET() AT TIME ZONE 'Singapore Standard Time' AS date),
    FinishedAt     datetimeoffset(0) NULL,
    Status         varchar(10)   NOT NULL CONSTRAINT DF_Tableau_RefreshJob_Status DEFAULT 'Submitted',
    Message        nvarchar(1000) NULL,
    CONSTRAINT CK_Tableau_RefreshJob_Status CHECK (Status IN ('Submitted','Succeeded','Failed','Cancelled','TimedOut'))
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Tableau_RefreshJob_DataSource'
               AND object_id = OBJECT_ID('Integration.Tableau_RefreshJob'))
CREATE INDEX IX_Tableau_RefreshJob_DataSource ON Integration.Tableau_RefreshJob (DataSource_id, SubmittedDate DESC);
GO

-- 10. Views ---------------------------------------------------------------------------------------
-- Tables a data source reads (rows stored under its own id or under its upstream id), classified.
CREATE OR ALTER VIEW Integration.vw_Tableau_DataSourceTableStatus
AS
SELECT DISTINCT ds.DataSource_id, dst.SchemaName, dst.TableName,
       CAST(CASE WHEN EXISTS (SELECT 1 FROM Integration.Tableau_StaticTable s
                              WHERE s.SchemaName = dst.SchemaName AND s.TableName = dst.TableName) THEN 1 ELSE 0 END AS bit) AS IsStatic,
       CAST(CASE WHEN EXISTS (SELECT 1 FROM Integration.Tableau_PipelineTable p
                              WHERE p.SchemaName = dst.SchemaName AND p.TableName = dst.TableName) THEN 1 ELSE 0 END AS bit) AS IsRegistered
FROM Integration.Tableau_DataSource AS ds
JOIN Integration.Tableau_DataSourceTable AS dst
  ON dst.DataSource_id IN (ds.DataSource_id, ds.UpstreamDataSourceId);
GO

-- Data source -> required pipelines, one row each (use this for consumers). Static tables contribute none.
-- PipelineKnown = the name exists in ETL_Master_Log (exact, binary comparison). Scans ETL_Master_Log per
-- distinct pipeline; fine at this size, index PipelineName there if it ever grows large.
CREATE OR ALTER VIEW Integration.vw_Tableau_DataSourcePipeline
AS
SELECT x.DataSource_id, x.PipelineName,
       CAST(CASE WHEN EXISTS (SELECT 1 FROM Integration.ETL_Master_Log l
                              WHERE l.PipelineName COLLATE Latin1_General_BIN2 = x.PipelineName COLLATE Latin1_General_BIN2)
                 THEN 1 ELSE 0 END AS bit) AS PipelineKnown
FROM (SELECT DISTINCT t.DataSource_id, p.PipelineName
      FROM Integration.vw_Tableau_DataSourceTableStatus AS t
      JOIN Integration.Tableau_PipelineTable AS p
        ON p.SchemaName = t.SchemaName AND p.TableName = t.TableName
      WHERE t.IsStatic = 0) AS x;
GO

-- Fail-closed readiness (D-006), one row per ACTIVE data source (inactive ones are filtered out, so a workbook
-- linked to one is caught by vw_Tableau_WorkbookReadiness). First matching rule wins:
--   NoReadinessCheck  explicit override with NoReadinessCheck = 1
--   UnknownKind       IsExtract is NULL (do not guess whether a refresh is needed)
--   NotChecked        (R8, D-053) no DWH table AND T2 positively identified the connection as non-DWH (NonDwhConnection = 1):
--                     counts as ready, nothing to wait for. With DWH tables the normal rules below apply (waits on those only).
--   NoTables          no upstream table rows (LEFT JOIN keeps these visible)
--   UnmappedTable     a table is neither in the registry nor static
--   UnknownPipeline   a required pipeline name is not in ETL_Master_Log
--   Ready             mapping complete. RequiredPipelines is delimited with '|' (NULL when none).
CREATE OR ALTER VIEW Integration.vw_Tableau_DataSourceReadiness
AS
SELECT ds.DataSource_id,
       ds.DataSource_Name,
       ds.Active_Flag,
       ds.RefreshKind,
       ds.ExtractRefreshTargetId,
       CASE WHEN o.DataSource_id IS NOT NULL       THEN 'NoReadinessCheck'
            WHEN ds.RefreshKind = 'Unknown'        THEN 'UnknownKind'
            WHEN ISNULL(c.TableCount, 0) = 0 AND ds.NonDwhConnection = 1 THEN 'NotChecked'
            WHEN ISNULL(c.TableCount, 0) = 0       THEN 'NoTables'
            WHEN c.UnmappedCount > 0               THEN 'UnmappedTable'
            WHEN ISNULL(pl.UnknownCount, 0) > 0    THEN 'UnknownPipeline'
            ELSE 'Ready' END                       AS ReadyStatus,
       ISNULL(c.TableCount, 0)                     AS TableCount,
       ISNULL(c.UnmappedCount, 0)                  AS UnmappedCount,
       ISNULL(pl.PipelineCount, 0)                 AS RequiredPipelineCount,
       pl.RequiredPipelines
FROM Integration.Tableau_DataSource AS ds
LEFT JOIN Integration.Tableau_DataSourceOverride AS o
       ON o.DataSource_id = ds.DataSource_id AND o.NoReadinessCheck = 1
LEFT JOIN (SELECT DataSource_id,
                  COUNT(*) AS TableCount,
                  SUM(CASE WHEN IsStatic = 0 AND IsRegistered = 0 THEN 1 ELSE 0 END) AS UnmappedCount
           FROM Integration.vw_Tableau_DataSourceTableStatus
           GROUP BY DataSource_id) AS c
       ON c.DataSource_id = ds.DataSource_id
LEFT JOIN (SELECT DataSource_id,
                  COUNT(*) AS PipelineCount,
                  SUM(CASE WHEN PipelineKnown = 0 THEN 1 ELSE 0 END) AS UnknownCount,
                  STRING_AGG(PipelineName, '|') WITHIN GROUP (ORDER BY PipelineName) AS RequiredPipelines
           FROM Integration.vw_Tableau_DataSourcePipeline
           GROUP BY DataSource_id) AS pl
       ON pl.DataSource_id = ds.DataSource_id
WHERE ds.Active_Flag = 1;
GO

-- Workbook readiness (fail closed). Workbooks come from subscriptions and from active links.
--   NoDataSources       no active link rows
--   InactiveDataSource  a linked source is inactive or missing (not in the readiness view)
--   NotReady            a linked source is none of Ready, NoReadinessCheck, NotChecked
--   Ready               every linked source exists, is active, and is Ready (or NoReadinessCheck / NotChecked)
CREATE OR ALTER VIEW Integration.vw_Tableau_WorkbookReadiness
AS
WITH wb AS (
    SELECT Workbook_id FROM Integration.Tableau_Subscription
    UNION
    SELECT Workbook_id FROM Integration.Tableau_WorkbookDataSource WHERE Active_Flag = 1
), links AS (
    SELECT wb.Workbook_id, w.DataSource_id, r.ReadyStatus
    FROM wb
    LEFT JOIN Integration.Tableau_WorkbookDataSource AS w
           ON w.Workbook_id = wb.Workbook_id AND w.Active_Flag = 1
    LEFT JOIN Integration.vw_Tableau_DataSourceReadiness AS r
           ON r.DataSource_id = w.DataSource_id
)
SELECT Workbook_id,
       CASE WHEN COUNT(DataSource_id) = 0 THEN 'NoDataSources'
            WHEN SUM(CASE WHEN DataSource_id IS NOT NULL AND ReadyStatus IS NULL THEN 1 ELSE 0 END) > 0 THEN 'InactiveDataSource'
            WHEN SUM(CASE WHEN ReadyStatus NOT IN ('Ready','NoReadinessCheck','NotChecked') THEN 1 ELSE 0 END) > 0 THEN 'NotReady'
            ELSE 'Ready' END AS WorkbookStatus,
       COUNT(DataSource_id) AS DataSourceCount,
       SUM(CASE WHEN DataSource_id IS NOT NULL AND ReadyStatus IS NULL THEN 1 ELSE 0 END) AS InactiveCount,
       SUM(CASE WHEN ReadyStatus NOT IN ('Ready','NoReadinessCheck','NotChecked') THEN 1 ELSE 0 END) AS NotReadyCount
FROM links
GROUP BY Workbook_id;
GO

-- 11. Tag everything this script creates so the rollback drops only these objects ----------------
DECLARE @names TABLE (n sysname, kind varchar(5));
INSERT @names VALUES
 ('Tableau_PipelineTable','TABLE'),('Tableau_StaticTable','TABLE'),('Tableau_DataSource','TABLE'),
 ('Tableau_WorkbookDataSource','TABLE'),('Tableau_DataSourceTable','TABLE'),('Tableau_DataSourceOverride','TABLE'),
 ('Tableau_Subscription','TABLE'),('Tableau_SubscriptionSendLog','TABLE'),('Tableau_RefreshJob','TABLE'),
 ('vw_Tableau_DataSourceTableStatus','VIEW'),('vw_Tableau_DataSourcePipeline','VIEW'),
 ('vw_Tableau_DataSourceReadiness','VIEW'),('vw_Tableau_WorkbookReadiness','VIEW');
DECLARE @n sysname, @k varchar(5);
DECLARE c CURSOR LOCAL FAST_FORWARD FOR SELECT n, kind FROM @names;
OPEN c; FETCH NEXT FROM c INTO @n, @k;
WHILE @@FETCH_STATUS = 0
BEGIN
    IF OBJECT_ID('Integration.' + @n) IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.extended_properties WHERE class = 1 AND minor_id = 0
                       AND major_id = OBJECT_ID('Integration.' + @n) AND name = 'created_by')
        EXEC sp_addextendedproperty @name = 'created_by', @value = 'T1_mapping_schema',
             @level0type = 'SCHEMA', @level0name = 'Integration', @level1type = @k, @level1name = @n;
    FETCH NEXT FROM c INTO @n, @k;
END
CLOSE c; DEALLOCATE c;
GO

/*
NOT included on purpose (touches an existing table, needs the user's go-ahead):
  Dedupe + unique constraint on Integration.ETL_Tableau_ExtractMapping(Etl_Name, DataSource_id).
*/
GO
SET NOEXEC OFF;
GO
