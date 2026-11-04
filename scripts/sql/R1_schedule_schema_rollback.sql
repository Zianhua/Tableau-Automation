SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') BEGIN RAISERROR('Wrong database or server: only the DEV DWH (<DEV_DWH_SERVER> / CQGlobalSGDW) is allowed; nothing below is run (SET NOEXEC ON).', 16, 1); SET NOEXEC ON; END
GO
/* Rollback for R1_schedule_schema.sql: drops the column Tableau_Subscription.ScheduleId and the tagged table Tableau_Schedule.
   After this, t6/t4 (schedule awareness on) refuse to send: run with SCHEDULE_MODE=ignore (--schedule-mode ignore) on dev only, or re-apply the schema. */
-- R1 review: refuse while subscriptions are taken over (paused in Tableau, sent by t6): without schedules t6 would send them every day.
IF OBJECT_ID('Integration.Tableau_SubscriptionBackup') IS NOT NULL
   AND EXISTS (SELECT 1 FROM Integration.Tableau_SubscriptionBackup WHERE TakenOver = 1 AND RestoredAt IS NULL)
    THROW 50005, 'Open takeover rows exist (subscriptions still paused in Tableau). Run t11_pause.py --restore first; nothing dropped.', 1;
IF COL_LENGTH('Integration.Tableau_Subscription', 'ScheduleId') IS NOT NULL
BEGIN
    ALTER TABLE Integration.Tableau_Subscription DROP COLUMN ScheduleId;
    PRINT 'dropped: Tableau_Subscription.ScheduleId';
END
IF OBJECT_ID('Integration.Tableau_Schedule') IS NULL PRINT 'absent:  Tableau_Schedule';
ELSE IF EXISTS (SELECT 1 FROM sys.extended_properties WHERE class = 1 AND minor_id = 0
                AND major_id = OBJECT_ID('Integration.Tableau_Schedule') AND name = 'created_by'
                AND CAST(value AS nvarchar(128)) = 'R1_schedule_schema')
BEGIN DROP TABLE Integration.Tableau_Schedule; PRINT 'dropped: Tableau_Schedule'; END
ELSE PRINT 'SKIPPED (not tagged R1_schedule_schema): Tableau_Schedule';
GO
GO
SET NOEXEC OFF;
GO
