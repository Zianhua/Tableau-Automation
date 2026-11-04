SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') BEGIN RAISERROR('Wrong database or server: only the DEV DWH (<DEV_DWH_SERVER> / CQGlobalSGDW) is allowed; nothing below is run (SET NOEXEC ON).', 16, 1); SET NOEXEC ON; END
GO
/*
T11a - Backup / takeover table for the old Tableau subscriptions the script takes over (scripts/t11_pause.py).
Dev DWH first (D-026). Re-runnable. Additive only: no existing object is touched. No foreign key to
Tableau_Subscription on purpose, so the T1 rollback still works and a backup outlives a mapping reset.

One row per takeover episode of a subscription. At most ONE open row (RestoredAt IS NULL) per subscription.
  - The row is INSERTed (and committed) with the subscription's full definition BEFORE the pause call, with
    TakenOver = 1 (intent) and PausedAt NULL. PausedAt is set once the script has confirmed Tableau shows the
    subscription suspended.
  - t6_send.py sends a subscription from this table iff TakenOver = 1 AND PausedAt IS NOT NULL AND RestoredAt IS NULL
    AND Tableau shows it suspended (D-035). A subscription is sent only after it was taken over.
  - --restore sets RestoredAt FIRST, then un-suspends in Tableau. A restored row is never re-paused or sent.
  - LastRepausedAt / RepauseCount: a human resumed it and the script paused it again. t6 does not send on that
    Singapore day (the old subscription may already have mailed, at-most-once).
  - Halted (R1): set when a pause changed the definition. t6 never sends a halted row and t11 never confirms it;
    clear with: UPDATE ... SET Halted = 0, Note = NULL WHERE Backup_id = <id> (after comparing with the backup).
  - Every in-scope subscription is taken over, also one that was already suspended in Tableau (D-046, reverses D-023);
    there is no 'original state' column: --restore just un-suspends. (R4_takeover_all.sql removes the old WasSuspended column.)
No recipient addresses are stored (the owner is a Tableau user id). Subject and message are copied as they are.
Rollback: scripts/sql/T11_pause_schema_rollback.sql (refuses to drop a non-empty table unless @Force = 1).
*/
IF OBJECT_ID('Integration.Tableau_SubscriptionBackup') IS NULL
CREATE TABLE Integration.Tableau_SubscriptionBackup
(
    Backup_id       int IDENTITY(1,1) CONSTRAINT PK_Tableau_SubscriptionBackup PRIMARY KEY,
    Subscription_id nvarchar(128) NOT NULL,
    Workbook_id     nvarchar(128) NOT NULL,
    TargetType      varchar(10)   NOT NULL,            -- Workbook | View
    TargetId        nvarchar(128) NOT NULL,
    OwnerUserId     nvarchar(128) NOT NULL,            -- Tableau user id, not an email address
    ScheduleId      nvarchar(128) NULL,
    ScheduleName    nvarchar(200) NULL,
    Subject         nvarchar(500) NULL,
    Message         nvarchar(4000) NULL,
    AttachPdf       bit           NOT NULL,
    AttachImage     bit           NOT NULL,
    PageSize        varchar(20)   NULL,
    Orientation     varchar(20)   NULL,
    SendIfViewEmpty bit           NULL,
    BackedUpAt      datetimeoffset(0) NOT NULL CONSTRAINT DF_Tableau_SubscriptionBackup_BackedUpAt DEFAULT SYSDATETIMEOFFSET(),
    TakenOver       bit           NOT NULL CONSTRAINT DF_Tableau_SubscriptionBackup_TakenOver DEFAULT 0,
    PausedAt        datetimeoffset(0) NULL,
    LastRepausedAt  datetimeoffset(0) NULL,
    LastRepausedDate AS (CAST(SWITCHOFFSET(LastRepausedAt, '+08:00') AS date)) PERSISTED,   -- Singapore day (D-016)
    RepauseCount    int           NOT NULL CONSTRAINT DF_Tableau_SubscriptionBackup_Repause DEFAULT 0,
    RestoredAt      datetimeoffset(0) NULL,
    RunId           varchar(64)   NULL,
    CONSTRAINT CK_Tableau_SubscriptionBackup_TargetType CHECK (TargetType IN ('Workbook','View')),
    CONSTRAINT CK_Tableau_SubscriptionBackup_PausedNeedsTakeover CHECK (PausedAt IS NULL OR TakenOver = 1)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_Tableau_SubscriptionBackup_Open'
               AND object_id = OBJECT_ID('Integration.Tableau_SubscriptionBackup'))
CREATE UNIQUE INDEX UX_Tableau_SubscriptionBackup_Open
    ON Integration.Tableau_SubscriptionBackup (Subscription_id) WHERE RestoredAt IS NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Tableau_SubscriptionBackup_Workbook'
               AND object_id = OBJECT_ID('Integration.Tableau_SubscriptionBackup'))
CREATE INDEX IX_Tableau_SubscriptionBackup_Workbook ON Integration.Tableau_SubscriptionBackup (Workbook_id);
GO
IF OBJECT_ID('Integration.Tableau_SubscriptionBackup') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.extended_properties WHERE class = 1 AND minor_id = 0
                   AND major_id = OBJECT_ID('Integration.Tableau_SubscriptionBackup') AND name = 'created_by')
    EXEC sp_addextendedproperty @name = 'created_by', @value = 'T11_pause_schema',
         @level0type = 'SCHEMA', @level0name = 'Integration', @level1type = 'TABLE', @level1name = 'Tableau_SubscriptionBackup';
GO
IF COL_LENGTH('Integration.Tableau_SubscriptionBackup', 'Halted') IS NULL
    ALTER TABLE Integration.Tableau_SubscriptionBackup ADD Halted bit NOT NULL
        CONSTRAINT DF_Tableau_SubscriptionBackup_Halted DEFAULT 0;   -- R1: a pause changed the definition; only a human clears it
GO
IF COL_LENGTH('Integration.Tableau_SubscriptionBackup', 'Note') IS NULL
    ALTER TABLE Integration.Tableau_SubscriptionBackup ADD Note nvarchar(400) NULL;   -- R1: halt reason, auto-close reason
GO
IF COL_LENGTH('Integration.Tableau_SubscriptionBackup', 'GoneSince') IS NULL
    ALTER TABLE Integration.Tableau_SubscriptionBackup ADD GoneSince date NULL;   -- R1: first Singapore day Tableau answered 404
GO
GO
SET NOEXEC OFF;
GO
