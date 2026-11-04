SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') BEGIN RAISERROR('Wrong database or server: only the DEV DWH (<DEV_DWH_SERVER> / CQGlobalSGDW) is allowed; nothing below is run (SET NOEXEC ON).', 16, 1); SET NOEXEC ON; END
GO
-- R3: refuse while subscriptions are taken over (paused in Tableau, sent by t6): dropping the mapping would stop those reports silently.
IF OBJECT_ID('Integration.Tableau_SubscriptionBackup') IS NOT NULL
   AND EXISTS (SELECT 1 FROM Integration.Tableau_SubscriptionBackup WHERE TakenOver = 1 AND RestoredAt IS NULL)
    THROW 50006, 'Open takeover rows exist (subscriptions still paused in Tableau). Run t11_pause.py --restore first; nothing dropped.', 1;
/*
Rollback for T1_mapping_schema.sql. Drops ONLY objects carrying the extended property
created_by = 'T1_mapping_schema' (set by that script at creation). Anything else is skipped.
WARNING: dropping also deletes the registry / static rows loaded by T3_load_registry.sql and any synced data.
The send log and refresh job log are only dropped if they are empty, or if @Force = 1 below.
Existing objects (ETL_Master_Log, ETL_Tableau_ExtractMapping, Tableau_ExtractRefreshLog) are never touched.
Single batch on purpose: do not add GO.
*/
DECLARE @Force bit = 0;   -- set to 1 to drop non-empty Tableau_SubscriptionSendLog / Tableau_RefreshJob
DECLARE @sl int = 0, @rj int = 0;
IF OBJECT_ID('Integration.Tableau_SubscriptionSendLog') IS NOT NULL
    SELECT @sl = COUNT(*) FROM Integration.Tableau_SubscriptionSendLog;
IF OBJECT_ID('Integration.Tableau_RefreshJob') IS NOT NULL
    SELECT @rj = COUNT(*) FROM Integration.Tableau_RefreshJob;
PRINT CONCAT('Tableau_SubscriptionSendLog rows: ', @sl, '; Tableau_RefreshJob rows: ', @rj, '; @Force = ', @Force);
IF (@sl > 0 OR @rj > 0) AND @Force = 0
    THROW 50003, 'Send log or refresh job log has rows; nothing dropped. Set @Force = 1 to drop anyway.', 1;

DECLARE @o TABLE (seq int IDENTITY(1,1), kind varchar(5), name sysname);
INSERT @o (kind, name) VALUES
 ('VIEW','vw_Tableau_WorkbookReadiness'),('VIEW','vw_Tableau_DataSourceReadiness'),
 ('VIEW','vw_Tableau_DataSourcePipeline'),('VIEW','vw_Tableau_DataSourceTableStatus'),
 ('TABLE','Tableau_SubscriptionSendLog'),('TABLE','Tableau_Subscription'),('TABLE','Tableau_RefreshJob'),
 ('TABLE','Tableau_DataSourceOverride'),('TABLE','Tableau_DataSourceTable'),('TABLE','Tableau_WorkbookDataSource'),
 ('TABLE','Tableau_DataSource'),('TABLE','Tableau_StaticTable'),('TABLE','Tableau_PipelineTable');
DECLARE @k varchar(5), @n sysname, @sql nvarchar(400);
DECLARE c CURSOR LOCAL FAST_FORWARD FOR SELECT kind, name FROM @o ORDER BY seq;
OPEN c; FETCH NEXT FROM c INTO @k, @n;
WHILE @@FETCH_STATUS = 0
BEGIN
    IF OBJECT_ID('Integration.' + @n) IS NULL
        PRINT CONCAT('absent:  ', @n);
    ELSE IF EXISTS (SELECT 1 FROM sys.extended_properties WHERE class = 1 AND minor_id = 0
                    AND major_id = OBJECT_ID('Integration.' + @n) AND name = 'created_by'
                    AND CAST(value AS nvarchar(128)) = 'T1_mapping_schema')
    BEGIN
        SET @sql = 'DROP ' + @k + ' Integration.' + QUOTENAME(@n);
        EXEC (@sql);
        PRINT CONCAT('dropped: ', @n);
    END
    ELSE PRINT CONCAT('SKIPPED (not tagged T1_mapping_schema): ', @n);
    FETCH NEXT FROM c INTO @k, @n;
END
CLOSE c; DEALLOCATE c;
GO
SET NOEXEC OFF;
GO
