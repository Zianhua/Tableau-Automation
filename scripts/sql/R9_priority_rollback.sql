SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') BEGIN RAISERROR('Wrong database or server: only the DEV DWH (<DEV_DWH_SERVER> / CQGlobalSGDW) is allowed; nothing below is run (SET NOEXEC ON).', 16, 1); SET NOEXEC ON; END
GO
/* Rollback for R9_priority.sql: drops the helper view and the priority table. The code tolerates a missing table (order falls back to by name). Idempotent. */
DROP VIEW IF EXISTS Integration.vw_Tableau_PriorityPicker;
GO
DROP TABLE IF EXISTS Integration.Tableau_Priority;
GO
SET NOEXEC OFF;
GO
