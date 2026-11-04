SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') BEGIN RAISERROR('Wrong database or server: only the DEV DWH (<DEV_DWH_SERVER> / CQGlobalSGDW) is allowed; nothing below is run (SET NOEXEC ON).', 16, 1); SET NOEXEC ON; END
GO
/*
Automatic registry and Tableau extract refresh schedules (D-066 to D-071). Dev DWH first (D-026). Re-runnable. Additive.
  1. Integration.Tableau_PipelineTable.Source (new column, 'Manual' | 'Auto', default 'Manual'): who owns the row. Every row that exists
     today stays 'Manual'. The daily mapping sync only ever inserts, moves or removes 'Auto' rows; a 'Manual' row is the override
     and is never changed by the sync (it is only reported when the lineage disagrees).
  2. Integration.Tableau_LineageSource (new): which pipeline a lineage table belongs to by default (the three user-confirmed rows,
     D-009 / D-025). The sync derives the pipeline of a base table from the lineage tables; this is the fallback when the ETL log
     gives no timing evidence.
  3. Integration.Tableau_ExtractTask (new): the Tableau extract refresh tasks, read by the daily mapping sync (schedule id per extract).
  4. Integration.Tableau_ExtractTaskBackup (new): backup and takeover record of extract refresh tasks (extract_takeover.py). A task
     is neutralised by DELETING it in Tableau (there is no per-task suspend); this row holds everything needed to re-create it.
     NoRetake (D-076): set by --restore, keeps the daily run from taking the extract over again until a person clears it.
     OldJob* (D-080): the old refresh job's rows (Integration.ETL_Tableau_ExtractMapping) switched off by the takeover and their original value.
  5. Integration.vw_Tableau_ExtractSchedule (new view): per extract (target) the task, schedule and whether the script has taken it over.
Schedules themselves are stored in the existing Integration.Tableau_Schedule (extract schedules are rows there too).
Rollback: scripts/sql/ExtractAuto_schema_rollback.sql
*/
IF COL_LENGTH('Integration.Tableau_PipelineTable', 'Source') IS NULL
    ALTER TABLE Integration.Tableau_PipelineTable ADD Source varchar(10) NOT NULL
        CONSTRAINT DF_Tableau_PipelineTable_Source DEFAULT 'Manual'
        CONSTRAINT CK_Tableau_PipelineTable_Source CHECK (Source IN ('Manual', 'Auto'));
GO

IF OBJECT_ID('Integration.Tableau_LineageSource') IS NULL
CREATE TABLE Integration.Tableau_LineageSource
(
    LineageTable sysname       NOT NULL CONSTRAINT PK_Tableau_LineageSource PRIMARY KEY,   -- Integration.<LineageTable>
    PipelineName varchar(50)   NOT NULL,                                                  -- = ETL_Master_Log.PipelineName
    Note         nvarchar(200) NULL,
    SetBy        nvarchar(128) NOT NULL CONSTRAINT DF_Tableau_LineageSource_SetBy DEFAULT SUSER_SNAME(),
    SetAt        datetime2(0)  NOT NULL CONSTRAINT DF_Tableau_LineageSource_SetAt DEFAULT SYSDATETIME()
);
GO
INSERT Integration.Tableau_LineageSource (LineageTable, PipelineName, Note)
SELECT v.t, v.p, v.n FROM (VALUES
    ('Lineage',        'CQGlobalSGDW: DailyETL',     'D-009/D-025: NBS. Also lists LeadsGen/K2 tables (LeadGenETL): ETL log timing decides those'),
    ('DW_NovaLineage', 'CQGlobalSGDW: NovaDailyETL', 'D-009/D-025'),
    ('OAO_Lineage',    'CQGlobalSGDW: LeadGenETL',   'D-009/D-025: OAO and LeadGen are one pipeline')) AS v(t, p, n)
WHERE NOT EXISTS (SELECT 1 FROM Integration.Tableau_LineageSource s WHERE s.LineageTable = v.t);
GO

IF OBJECT_ID('Integration.Tableau_ExtractTask') IS NULL
CREATE TABLE Integration.Tableau_ExtractTask
(
    Task_id                nvarchar(128) NOT NULL CONSTRAINT PK_Tableau_ExtractTask PRIMARY KEY,
    Target_id              nvarchar(128) NOT NULL,   -- data source luid (= Tableau_DataSource.ExtractRefreshTargetId) or workbook luid
    TargetType             varchar(12)   NOT NULL,   -- datasource | workbook (workbook tasks are listed, never refreshed or taken over by us)
    Schedule_id            nvarchar(128) NULL,       -- row in Integration.Tableau_Schedule
    Priority               int           NULL,
    ConsecutiveFailedCount int           NULL,
    LastSeenAt             datetime2(0)  NOT NULL CONSTRAINT DF_Tableau_ExtractTask_LastSeenAt DEFAULT SYSDATETIME(),
    Active_Flag            bit           NOT NULL CONSTRAINT DF_Tableau_ExtractTask_Active DEFAULT 1,
    CONSTRAINT CK_Tableau_ExtractTask_TargetType CHECK (TargetType IN ('datasource', 'workbook'))
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Tableau_ExtractTask_Target' AND object_id = OBJECT_ID('Integration.Tableau_ExtractTask'))
CREATE INDEX IX_Tableau_ExtractTask_Target ON Integration.Tableau_ExtractTask (Target_id);
GO

-- One row per takeover episode of a task. The row is written and committed BEFORE the task is deleted in Tableau.
--   NeutralisedAt NULL = the delete is not confirmed yet (the next run checks Tableau and finishes or retries).
--   RestoredAt set     = the task was re-created (NewTaskId) or the row was closed by a person.
IF OBJECT_ID('Integration.Tableau_ExtractTaskBackup') IS NULL
CREATE TABLE Integration.Tableau_ExtractTaskBackup
(
    Backup_id              int IDENTITY(1,1) CONSTRAINT PK_Tableau_ExtractTaskBackup PRIMARY KEY,
    Task_id                nvarchar(128) NOT NULL,
    Target_id              nvarchar(128) NOT NULL,
    TargetName             nvarchar(256) NULL,
    OwnerUserId            nvarchar(128) NULL,       -- Tableau user id of the data source owner, not an email address
    ScheduleId             nvarchar(128) NOT NULL,
    ScheduleName           nvarchar(200) NULL,
    Priority               int           NULL,       -- lost on restore (the new task gets the schedule's priority)
    ConsecutiveFailedCount int           NULL,       -- lost on restore
    LastRunAt              datetimeoffset(0) NULL,   -- lost on restore
    BackedUpAt             datetimeoffset(0) NOT NULL CONSTRAINT DF_Tableau_ExtractTaskBackup_BackedUpAt DEFAULT SYSDATETIMEOFFSET(),
    TakenOver              bit           NOT NULL CONSTRAINT DF_Tableau_ExtractTaskBackup_TakenOver DEFAULT 0,
    NeutralisedAt          datetimeoffset(0) NULL,
    RestoredAt             datetimeoffset(0) NULL,
    NewTaskId              nvarchar(128) NULL,       -- the task created by --restore
    RunId                  varchar(64)   NULL,
    Note                   nvarchar(400) NULL,
    NoRetake               bit           NOT NULL CONSTRAINT DF_Tableau_ExtractTaskBackup_NoRetake DEFAULT 0,   -- set by --restore: do not take this extract over again until a person clears it
    CONSTRAINT CK_Tableau_ExtractTaskBackup_NeedsTakeover CHECK (NeutralisedAt IS NULL OR TakenOver = 1)
);
GO
-- D-076: added later (additive, re-runnable). --restore sets NoRetake = 1 on the restored row so the daily takeover run does not take the extract
-- over again while its id is still in the allow-list. A person clears it with: UPDATE Integration.Tableau_ExtractTaskBackup SET NoRetake = 0 WHERE Backup_id = <id>.
-- Rollback of this column alone: ALTER TABLE ... DROP CONSTRAINT DF_Tableau_ExtractTaskBackup_NoRetake; ALTER TABLE ... DROP COLUMN NoRetake (the
-- full rollback script drops the whole table, column included).
IF COL_LENGTH('Integration.Tableau_ExtractTaskBackup', 'NoRetake') IS NULL
    ALTER TABLE Integration.Tableau_ExtractTaskBackup ADD NoRetake bit NOT NULL
        CONSTRAINT DF_Tableau_ExtractTaskBackup_NoRetake DEFAULT 0;
GO
-- D-080: the OLD refresh job's rows that the takeover switched off (Integration.ETL_Tableau_ExtractMapping.Active_Flag 1 -> 0), so --restore can
-- set exactly those rows back. Additive, re-runnable, all NULL for a takeover that switched nothing off.
--   OldJobMappingIds   comma list of the Mapping_id values we set to 0 (never a row we did not switch off)
--   OldJobOriginalFlag the value they had before (always 1: only active rows are switched off)
--   OldJobSwitchedAt / OldJobRestoredAt  when they were switched off / back
-- Rollback of these columns alone: ALTER TABLE ... DROP COLUMN OldJobMappingIds, OldJobOriginalFlag, OldJobSwitchedAt, OldJobRestoredAt (the full
-- rollback script drops the whole table). Open takeover rows with OldJobMappingIds set mean the old job is still switched off: restore first.
IF COL_LENGTH('Integration.Tableau_ExtractTaskBackup', 'OldJobMappingIds') IS NULL
    ALTER TABLE Integration.Tableau_ExtractTaskBackup ADD OldJobMappingIds nvarchar(1000) NULL, OldJobOriginalFlag bit NULL,
        OldJobSwitchedAt datetimeoffset(0) NULL, OldJobRestoredAt datetimeoffset(0) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_Tableau_ExtractTaskBackup_Open' AND object_id = OBJECT_ID('Integration.Tableau_ExtractTaskBackup'))
CREATE UNIQUE INDEX UX_Tableau_ExtractTaskBackup_Open ON Integration.Tableau_ExtractTaskBackup (Task_id) WHERE RestoredAt IS NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Tableau_ExtractTaskBackup_Target' AND object_id = OBJECT_ID('Integration.Tableau_ExtractTaskBackup'))
CREATE INDEX IX_Tableau_ExtractTaskBackup_Target ON Integration.Tableau_ExtractTaskBackup (Target_id);
GO

-- Per extract data source: its tasks (live, or taken over and therefore gone from Tableau) and the schedule of each.
-- TakenOver = 1 only once the delete is confirmed (NeutralisedAt set). t4 reads this view.
CREATE OR ALTER VIEW Integration.vw_Tableau_ExtractSchedule
AS
SELECT t.Target_id, t.Task_id, t.Schedule_id,
       CAST(CASE WHEN b.NeutralisedAt IS NOT NULL THEN 1 ELSE 0 END AS bit) AS TakenOver,
       b.NeutralisedAt
FROM Integration.Tableau_ExtractTask AS t
LEFT JOIN Integration.Tableau_ExtractTaskBackup AS b
       ON b.Task_id = t.Task_id AND b.TakenOver = 1 AND b.RestoredAt IS NULL
WHERE t.TargetType = 'datasource' AND (t.Active_Flag = 1 OR b.Backup_id IS NOT NULL);
GO

DECLARE @names TABLE (n sysname, kind varchar(5));
INSERT @names VALUES ('Tableau_LineageSource','TABLE'),('Tableau_ExtractTask','TABLE'),('Tableau_ExtractTaskBackup','TABLE'),('vw_Tableau_ExtractSchedule','VIEW');
DECLARE @n sysname, @k varchar(5);
DECLARE c CURSOR LOCAL FAST_FORWARD FOR SELECT n, kind FROM @names;
OPEN c; FETCH NEXT FROM c INTO @n, @k;
WHILE @@FETCH_STATUS = 0
BEGIN
    IF OBJECT_ID('Integration.' + @n) IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.extended_properties WHERE class = 1 AND minor_id = 0
                       AND major_id = OBJECT_ID('Integration.' + @n) AND name = 'created_by')
        EXEC sp_addextendedproperty @name = 'created_by', @value = 'ExtractAuto_schema',
             @level0type = 'SCHEMA', @level0name = 'Integration', @level1type = @k, @level1name = @n;
    FETCH NEXT FROM c INTO @n, @k;
END
CLOSE c; DEALLOCATE c;
GO
SET NOEXEC OFF;
GO
