SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') BEGIN RAISERROR('Wrong database or server: only the DEV DWH (<DEV_DWH_SERVER> / CQGlobalSGDW) is allowed; nothing below is run (SET NOEXEC ON).', 16, 1); SET NOEXEC ON; END
GO
/*
R1 (D-036) - Tableau schedules, so t6/t4 can honour each subscription's own schedule. Dev DWH first (D-026). Re-runnable.
Additive: a new table Integration.Tableau_Schedule (tagged created_by = 'R1_schedule_schema') and ONE new nullable column
Integration.Tableau_Subscription.ScheduleId. No other T1 object is changed. Written by t2_mapping_sync.py from the REST
schedule object (frequency, weekdays, month day(s), start time, interval hours, state).
Rollback: scripts/sql/R1_schedule_schema_rollback.sql
*/
IF OBJECT_ID('Integration.Tableau_Schedule') IS NULL
CREATE TABLE Integration.Tableau_Schedule
(
    Schedule_id   nvarchar(128) NOT NULL CONSTRAINT PK_Tableau_Schedule PRIMARY KEY,
    Name          nvarchar(200) NULL,
    State         varchar(12)   NULL,        -- Active | Suspended (Tableau's schedule state)
    Frequency     varchar(10)   NULL,        -- Hourly | Daily | Weekly | Monthly
    WeekDays      varchar(40)   NULL,        -- 'Mon,Tue,Wed'
    MonthDays     varchar(80)   NULL,        -- '1' | '1,15' | 'LastDay' | 'First,Monday' (Tableau tokens)
    StartTime     time(0)       NULL,
    IntervalHours decimal(6,2)  NULL,
    LastSeenAt    datetime2(0)  NOT NULL CONSTRAINT DF_Tableau_Schedule_LastSeenAt DEFAULT SYSDATETIME(),
    Active_Flag   bit           NOT NULL CONSTRAINT DF_Tableau_Schedule_Active DEFAULT 1
);
GO
IF COL_LENGTH('Integration.Tableau_Subscription', 'ScheduleId') IS NULL
    ALTER TABLE Integration.Tableau_Subscription ADD ScheduleId nvarchar(128) NULL;
GO
IF OBJECT_ID('Integration.Tableau_Schedule') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.extended_properties WHERE class = 1 AND minor_id = 0
                   AND major_id = OBJECT_ID('Integration.Tableau_Schedule') AND name = 'created_by')
    EXEC sp_addextendedproperty @name = 'created_by', @value = 'R1_schedule_schema',
         @level0type = 'SCHEMA', @level0name = 'Integration', @level1type = 'TABLE', @level1name = 'Tableau_Schedule';
GO
GO
SET NOEXEC OFF;
GO
