SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') BEGIN RAISERROR('Wrong database or server: only the DEV DWH (<DEV_DWH_SERVER> / CQGlobalSGDW) is allowed; nothing below is run (SET NOEXEC ON).', 16, 1); SET NOEXEC ON; END
GO
/*
R5 (R3B_REVIEW F-3) - Integration.Tableau_Subscription.WorkbookUpdatedAt. Dev DWH first (D-026). Re-runnable. Additive: ONE new
nullable column, nothing else changes.
T2 stores here the workbook's REST updated_at exactly as it read it (naive UTC, whole seconds, read BEFORE the Metadata query of
that workbook). t6 compares the workbook's live updated_at with THIS value (changed since the sync => awaiting_sync), not with
LastSeenAt, which is a clock stamped after the whole read. NULL (not yet synced since R5) = awaiting_sync, so run
t2_mapping_sync.py once right after applying this script.
Apply BEFORE the new t2/t6 code runs. Rollback: scripts/sql/R5_workbook_updated_at_rollback.sql
*/
IF COL_LENGTH('Integration.Tableau_Subscription', 'WorkbookUpdatedAt') IS NULL
    ALTER TABLE Integration.Tableau_Subscription ADD WorkbookUpdatedAt datetime2(0) NULL;
GO
SET NOEXEC OFF;
GO
