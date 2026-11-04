SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') BEGIN RAISERROR('Wrong database or server: only the DEV DWH (<DEV_DWH_SERVER> / CQGlobalSGDW) is allowed; nothing below is run (SET NOEXEC ON).', 16, 1); SET NOEXEC ON; END
GO
/* Rollback for R5_workbook_updated_at.sql: drops Tableau_Subscription.WorkbookUpdatedAt. Use it only together with a rollback of the
   t2/t6 code of R5 (the new code reads the column). Idempotent. */
IF COL_LENGTH('Integration.Tableau_Subscription', 'WorkbookUpdatedAt') IS NOT NULL
BEGIN
    ALTER TABLE Integration.Tableau_Subscription DROP COLUMN WorkbookUpdatedAt;
    PRINT 'dropped: Tableau_Subscription.WorkbookUpdatedAt';
END
GO
SET NOEXEC OFF;
GO
