SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') BEGIN RAISERROR('Wrong database or server: only the DEV DWH (<DEV_DWH_SERVER> / CQGlobalSGDW) is allowed; nothing below is run (SET NOEXEC ON).', 16, 1); SET NOEXEC ON; END
GO
/*
R4 - Takeover of ALL in-scope subscriptions (D-046, reverses D-023). Dev DWH first (D-026). Idempotent: safe to re-run.
Drops, from Integration.Tableau_SubscriptionBackup:
  - the CHECK CK_Tableau_SubscriptionBackup_NotPreSuspended (TakenOver = 0 OR WasSuspended = 0),
  - any DEFAULT constraint on WasSuspended (the original DDL may or may not have had one; a column with a default cannot be dropped),
  - the column WasSuspended.
A subscription that is already suspended in Tableau is now taken over like any other; --restore just un-suspends, so there is no
original state to remember. T11_pause_schema.sql is kept consistent (a fresh install never creates the column).
Refuses to run if a row has WasSuspended = 1, because dropping the column would lose that fact: review those rows first. The refusal
is RAISERROR + SET NOEXEC ON (like the dev-server guard above), so the batches after the next GO are skipped too, in SSMS and sqlcmd.
Apply this BEFORE the new t11_pause.py (D-046 code): the new takeover INSERT does not write WasSuspended, so on a database that still
has the NOT NULL column without a default every takeover fails (pause_failed).
Rollback: scripts/sql/R4_takeover_all_rollback.sql (re-adds the column, 0 for every row).
*/
IF OBJECT_ID('Integration.Tableau_SubscriptionBackup') IS NOT NULL AND COL_LENGTH('Integration.Tableau_SubscriptionBackup', 'WasSuspended') IS NOT NULL
BEGIN
    DECLARE @n int;
    EXEC sp_executesql N'SELECT @n = COUNT(*) FROM Integration.Tableau_SubscriptionBackup WHERE WasSuspended = 1', N'@n int OUTPUT', @n OUTPUT;
    IF @n > 0 BEGIN RAISERROR('Backup rows with WasSuspended = 1 exist; review them before dropping the column. Nothing below is run (SET NOEXEC ON).', 16, 1); SET NOEXEC ON; END
END
GO
IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_Tableau_SubscriptionBackup_NotPreSuspended'
           AND parent_object_id = OBJECT_ID('Integration.Tableau_SubscriptionBackup'))
    ALTER TABLE Integration.Tableau_SubscriptionBackup DROP CONSTRAINT CK_Tableau_SubscriptionBackup_NotPreSuspended;
GO
IF OBJECT_ID('Integration.Tableau_SubscriptionBackup') IS NOT NULL AND COL_LENGTH('Integration.Tableau_SubscriptionBackup', 'WasSuspended') IS NOT NULL
BEGIN
    DECLARE @sql nvarchar(max) = N'';
    SELECT @sql = @sql + N'ALTER TABLE Integration.Tableau_SubscriptionBackup DROP CONSTRAINT ' + QUOTENAME(dc.name) + N';'
    FROM sys.default_constraints dc
    WHERE dc.parent_object_id = OBJECT_ID('Integration.Tableau_SubscriptionBackup')
      AND dc.parent_column_id = COLUMNPROPERTY(OBJECT_ID('Integration.Tableau_SubscriptionBackup'), 'WasSuspended', 'ColumnId');
    IF @sql <> N'' EXEC sp_executesql @sql;
END
GO
IF OBJECT_ID('Integration.Tableau_SubscriptionBackup') IS NOT NULL AND COL_LENGTH('Integration.Tableau_SubscriptionBackup', 'WasSuspended') IS NOT NULL
    ALTER TABLE Integration.Tableau_SubscriptionBackup DROP COLUMN WasSuspended;
GO
SET NOEXEC OFF;
GO
