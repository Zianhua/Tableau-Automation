SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') BEGIN RAISERROR('Wrong database or server: only the DEV DWH (<DEV_DWH_SERVER> / CQGlobalSGDW) is allowed; nothing below is run (SET NOEXEC ON).', 16, 1); SET NOEXEC ON; END
GO
-- Rollback for R3_sendlog_guard.sql: drops the trigger only (after this a DELETE of a Sent row frees the slot again).
IF OBJECT_ID('Integration.trg_Tableau_SubscriptionSendLog_NoDeleteOfSlot') IS NOT NULL
BEGIN
    DROP TRIGGER Integration.trg_Tableau_SubscriptionSendLog_NoDeleteOfSlot;
    PRINT 'dropped: trg_Tableau_SubscriptionSendLog_NoDeleteOfSlot';
END
ELSE PRINT 'absent:  trg_Tableau_SubscriptionSendLog_NoDeleteOfSlot';
GO
SET NOEXEC OFF;
GO
