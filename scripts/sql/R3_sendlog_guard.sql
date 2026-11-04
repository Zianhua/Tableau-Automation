SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') BEGIN RAISERROR('Wrong database or server: only the DEV DWH (<DEV_DWH_SERVER> / CQGlobalSGDW) is allowed; nothing below is run (SET NOEXEC ON).', 16, 1); SET NOEXEC ON; END
GO
/*
R3 - Send-log delete guard (schema revision after R1/T11). Dev DWH first (D-026). Re-runnable. Additive: one trigger on
Integration.Tableau_SubscriptionSendLog, no table changed.

Why: a Sent, Claimed or TimedOut row HOLDS the day's slot (and, with schedules, the occurrence). The Sent trigger stops a status change but a
DELETE removed the row and so freed the slot, which allows a second send. This INSTEAD OF DELETE trigger refuses to delete such rows.
  - Rows with Status Released / Failed / DryRun may be deleted (they hold nothing).
  - Sent / Claimed / TimedOut rows may be deleted ONLY by test cleanups: the session must have set
      EXEC sp_set_session_context 'allow_fixture_delete', 1;
    AND every such row must belong to a fixture subscription (Subscription_id LIKE 'T6TEST-%', 'T11TEST-%' or 'R1TEST-%').
  - To remove a real Claimed/TimedOut row a person sets it to 'Released' first (RUNBOOK section 5). A Sent row can never be removed.
Rollback: scripts/sql/R3_sendlog_guard_rollback.sql
*/
CREATE OR ALTER TRIGGER Integration.trg_Tableau_SubscriptionSendLog_NoDeleteOfSlot
ON Integration.Tableau_SubscriptionSendLog INSTEAD OF DELETE
AS
BEGIN
    SET NOCOUNT ON;
    IF EXISTS (SELECT 1 FROM deleted d WHERE d.Status IN ('Sent', 'Claimed', 'TimedOut')
               AND NOT (ISNULL(CAST(SESSION_CONTEXT(N'allow_fixture_delete') AS int), 0) = 1
                        AND (d.Subscription_id LIKE 'T6TEST-%' OR d.Subscription_id LIKE 'T11TEST-%' OR d.Subscription_id LIKE 'R1TEST-%')))
        THROW 50007, 'Rows with Status Sent, Claimed or TimedOut hold the send slot and cannot be deleted. Set a Claimed/TimedOut row to Released first (RUNBOOK section 5); a Sent row can never be removed.', 1;
    DELETE t FROM Integration.Tableau_SubscriptionSendLog t JOIN deleted d ON d.SendLog_id = t.SendLog_id;
END
GO
SET NOEXEC OFF;
GO
