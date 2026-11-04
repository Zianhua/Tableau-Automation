SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') BEGIN RAISERROR('Wrong database or server: only the DEV DWH (<DEV_DWH_SERVER> / CQGlobalSGDW) is allowed; nothing below is run (SET NOEXEC ON).', 16, 1); SET NOEXEC ON; END
GO
/*
Rollback for R4_takeover_all.sql: re-adds Integration.Tableau_SubscriptionBackup.WasSuspended as bit NOT NULL with the named DEFAULT
DF_Tableau_SubscriptionBackup_WasSuspended (0), and the CHECK CK_Tableau_SubscriptionBackup_NotPreSuspended. Idempotent.
Use it only together with a rollback of the code (t11/t6/t4 of D-046, which do not read or write the column).
WARNING with rows present: the original per-row state is unknown after R4, so EVERY existing row gets WasSuspended = 0 (the CHECK then
holds). Old code reads 0 as "paused by us", so after this rollback an old-code --restore would RESUME a row that was taken over
while already suspended in Tableau. Restore (or close) those rows with the new code BEFORE rolling back, or fix the rows by hand.
The default stays in place so an INSERT that omits the column keeps working; R4 drops it again.
*/
IF OBJECT_ID('Integration.Tableau_SubscriptionBackup') IS NOT NULL AND COL_LENGTH('Integration.Tableau_SubscriptionBackup', 'WasSuspended') IS NULL
    ALTER TABLE Integration.Tableau_SubscriptionBackup ADD WasSuspended bit NOT NULL
        CONSTRAINT DF_Tableau_SubscriptionBackup_WasSuspended DEFAULT 0;
GO
IF OBJECT_ID('Integration.Tableau_SubscriptionBackup') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_Tableau_SubscriptionBackup_NotPreSuspended'
                   AND parent_object_id = OBJECT_ID('Integration.Tableau_SubscriptionBackup'))
    ALTER TABLE Integration.Tableau_SubscriptionBackup WITH CHECK ADD CONSTRAINT CK_Tableau_SubscriptionBackup_NotPreSuspended
        CHECK (TakenOver = 0 OR WasSuspended = 0);
GO
SET NOEXEC OFF;
GO
