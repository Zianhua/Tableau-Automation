SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') BEGIN RAISERROR('Wrong database or server: only the DEV DWH (<DEV_DWH_SERVER> / CQGlobalSGDW) is allowed; nothing below is run (SET NOEXEC ON).', 16, 1); SET NOEXEC ON; END
GO
/*
Rollback for T11_pause_schema.sql. Drops ONLY an object carrying the extended property created_by = 'T11_pause_schema'.
The backup rows are the only copy of the pre-takeover definitions (besides the JSON files in out/t11_backup), so a non-empty table is NOT
dropped unless @Force = 1. Restore the subscriptions first (t11_pause.py --restore ...), THEN drop.
Single batch on purpose: do not add GO.
*/
DECLARE @Force bit = 0;
DECLARE @n int = 0;
IF OBJECT_ID('Integration.Tableau_SubscriptionBackup') IS NOT NULL
    SELECT @n = COUNT(*) FROM Integration.Tableau_SubscriptionBackup;
PRINT CONCAT('Tableau_SubscriptionBackup rows: ', @n, '; @Force = ', @Force);
-- R1: open takeover rows mean subscriptions are still paused in Tableau. NEVER bypassed by @Force: restore them first.
IF OBJECT_ID('Integration.Tableau_SubscriptionBackup') IS NOT NULL
   AND EXISTS (SELECT 1 FROM Integration.Tableau_SubscriptionBackup WHERE TakenOver = 1 AND RestoredAt IS NULL)
    THROW 50004, 'Open takeover rows exist (subscriptions still paused in Tableau). Run t11_pause.py --restore first. @Force does not override this.', 1;
IF @n > 0 AND @Force = 0
    THROW 50003, 'Tableau_SubscriptionBackup has rows; nothing dropped. Restore the subscriptions, then set @Force = 1.', 1;
IF OBJECT_ID('Integration.Tableau_SubscriptionBackup') IS NULL
    PRINT 'absent:  Tableau_SubscriptionBackup';
ELSE IF EXISTS (SELECT 1 FROM sys.extended_properties WHERE class = 1 AND minor_id = 0
                AND major_id = OBJECT_ID('Integration.Tableau_SubscriptionBackup') AND name = 'created_by'
                AND CAST(value AS nvarchar(128)) = 'T11_pause_schema')
BEGIN
    DROP TABLE Integration.Tableau_SubscriptionBackup;
    PRINT 'dropped: Tableau_SubscriptionBackup';
END
ELSE PRINT 'SKIPPED (not tagged T11_pause_schema): Tableau_SubscriptionBackup';
GO
SET NOEXEC OFF;
GO
