SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') BEGIN RAISERROR('Wrong database or server: only the DEV DWH (<DEV_DWH_SERVER> / CQGlobalSGDW) is allowed; nothing below is run (SET NOEXEC ON).', 16, 1); SET NOEXEC ON; END
GO
/*
Rollback for ExtractAuto_schema.sql. Idempotent. Drops only objects tagged created_by = 'ExtractAuto_schema', and the Source column.
Safety (same as the subscription takeover):
  - Open extract takeover rows mean tasks are still deleted in Tableau (and the old refresh job's rows switched off): restore them first (extract_takeover.py --restore). NEVER bypassed.
  - A non-empty Tableau_ExtractTaskBackup is the only copy of the old task definitions: kept unless @Force = 1. Rows whose old-job switch-off was never undone additionally need @OldJobRowsChecked = 1.
  - Auto rows of Tableau_PipelineTable are DELETED with the column (they exist only because the sync derived them); the count is printed.
    The extracts behind them then show UnmappedTable (fail closed) until the rows are loaded by hand.
Single batch on purpose: do not add GO inside.
*/
DECLARE @Force bit = 0;
DECLARE @OldJobRowsChecked bit = 0;   -- set to 1 only after the old refresh job's rows listed by this script were checked / switched back (D-080)
DECLARE @n int = 0;
IF OBJECT_ID('Integration.Tableau_ExtractTaskBackup') IS NOT NULL
BEGIN
    SELECT @n = COUNT(*) FROM Integration.Tableau_ExtractTaskBackup;
    IF EXISTS (SELECT 1 FROM Integration.Tableau_ExtractTaskBackup WHERE TakenOver = 1 AND RestoredAt IS NULL)
        THROW 50014, 'Open extract takeover rows exist (tasks still deleted in Tableau). Run extract_takeover.py --restore first. @Force does not override this.', 1;
    -- D-080: old refresh job rows switched off by a takeover and not switched back (an open row is already refused above; this is a row closed by hand)
    IF COL_LENGTH('Integration.Tableau_ExtractTaskBackup', 'OldJobMappingIds') IS NOT NULL
    BEGIN
        DECLARE @oj int;
        EXEC sp_executesql N'SELECT @c = COUNT(*) FROM Integration.Tableau_ExtractTaskBackup WHERE OldJobMappingIds IS NOT NULL AND OldJobRestoredAt IS NULL', N'@c int OUTPUT', @c = @oj OUTPUT;
        IF @oj > 0
        BEGIN
            -- the old refresh job's rows are still switched off: show them, and refuse @Force unless the second flag says they were checked
            EXEC sp_executesql N'SELECT Backup_id, Task_id, Target_id, TargetName, OldJobMappingIds, OldJobSwitchedAt FROM Integration.Tableau_ExtractTaskBackup WHERE OldJobMappingIds IS NOT NULL AND OldJobRestoredAt IS NULL';
            PRINT 'Rows of Integration.ETL_Tableau_ExtractMapping listed above (Mapping_id) may still have Active_Flag = 0 and the old refresh job then skips them. Switch them back first: UPDATE Integration.ETL_Tableau_ExtractMapping SET Active_Flag = 1 WHERE Mapping_id IN (<OldJobMappingIds>);';
            IF @OldJobRowsChecked = 0
                THROW 50015, 'Backup rows hold old-job rows that were never switched back (listed above). Switch them back, then set @OldJobRowsChecked = 1 (and @Force = 1). Nothing dropped.', 1;
        END
    END
    IF @n > 0 AND @Force = 0
        THROW 50013, 'Tableau_ExtractTaskBackup has rows; nothing dropped. Restore the tasks, then set @Force = 1.', 1;
END
DROP VIEW IF EXISTS Integration.vw_Tableau_ExtractSchedule;
IF OBJECT_ID('Integration.Tableau_ExtractTaskBackup') IS NOT NULL AND EXISTS (SELECT 1 FROM sys.extended_properties WHERE class = 1 AND minor_id = 0
   AND major_id = OBJECT_ID('Integration.Tableau_ExtractTaskBackup') AND name = 'created_by' AND CAST(value AS nvarchar(128)) = 'ExtractAuto_schema')
    DROP TABLE Integration.Tableau_ExtractTaskBackup;
IF OBJECT_ID('Integration.Tableau_ExtractTask') IS NOT NULL AND EXISTS (SELECT 1 FROM sys.extended_properties WHERE class = 1 AND minor_id = 0
   AND major_id = OBJECT_ID('Integration.Tableau_ExtractTask') AND name = 'created_by' AND CAST(value AS nvarchar(128)) = 'ExtractAuto_schema')
    DROP TABLE Integration.Tableau_ExtractTask;
IF OBJECT_ID('Integration.Tableau_LineageSource') IS NOT NULL AND EXISTS (SELECT 1 FROM sys.extended_properties WHERE class = 1 AND minor_id = 0
   AND major_id = OBJECT_ID('Integration.Tableau_LineageSource') AND name = 'created_by' AND CAST(value AS nvarchar(128)) = 'ExtractAuto_schema')
    DROP TABLE Integration.Tableau_LineageSource;
IF COL_LENGTH('Integration.Tableau_PipelineTable', 'Source') IS NOT NULL
BEGIN
    DECLARE @auto int;
    EXEC sp_executesql N'SELECT @a = COUNT(*) FROM Integration.Tableau_PipelineTable WHERE Source = ''Auto''', N'@a int OUTPUT', @a = @auto OUTPUT;
    PRINT CONCAT('deleting Auto registry rows: ', @auto);
    EXEC sp_executesql N'DELETE FROM Integration.Tableau_PipelineTable WHERE Source = ''Auto''';
    ALTER TABLE Integration.Tableau_PipelineTable DROP CONSTRAINT CK_Tableau_PipelineTable_Source;
    ALTER TABLE Integration.Tableau_PipelineTable DROP CONSTRAINT DF_Tableau_PipelineTable_Source;
    ALTER TABLE Integration.Tableau_PipelineTable DROP COLUMN Source;
    PRINT 'dropped: Tableau_PipelineTable.Source';
END
GO
SET NOEXEC OFF;
GO
