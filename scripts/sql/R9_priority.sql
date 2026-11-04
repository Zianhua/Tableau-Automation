SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') BEGIN RAISERROR('Wrong database or server: only the DEV DWH (<DEV_DWH_SERVER> / CQGlobalSGDW) is allowed; nothing below is run (SET NOEXEC ON).', 16, 1); SET NOEXEC ON; END
GO
/*
R9 (D-062) - run order. Dev DWH first (D-026). Re-runnable. Additive: ONE new table Integration.Tableau_Priority and ONE helper view.
The T2 sync never touches this table, so a mark survives re-syncs.
  Kind 'Extract'      Item_id = Tableau_DataSource.ExtractRefreshTargetId : t4 refreshes marked extracts first
  Kind 'Subscription' Item_id = Tableau_Subscription.Subscription_id      : t6 sends marked subscriptions first
Lower Priority number goes first (1 before 5). Anything not listed goes after every listed item, in the usual name order.
Extracts and subscriptions are ranked separately; refreshes always run before sends because t4 starts before t6 and t6 waits for them.
To mark: INSERT INTO Integration.Tableau_Priority (Kind, Item_id, Priority) VALUES ('Extract', '<id>', 1);  (find ids in vw_Tableau_PriorityPicker)
To clear: DELETE FROM Integration.Tableau_Priority WHERE Kind = '...' AND Item_id = '...';
Rollback: scripts/sql/R9_priority_rollback.sql
*/
IF OBJECT_ID('Integration.Tableau_Priority') IS NULL
CREATE TABLE Integration.Tableau_Priority
(
    Kind     varchar(12)   NOT NULL,
    Item_id  nvarchar(128) NOT NULL,
    Priority int           NOT NULL,
    Note     nvarchar(200) NULL,
    SetBy    nvarchar(128) NOT NULL CONSTRAINT DF_Tableau_Priority_SetBy DEFAULT SUSER_SNAME(),
    SetAt    datetime2(0)  NOT NULL CONSTRAINT DF_Tableau_Priority_SetAt DEFAULT SYSDATETIME(),
    CONSTRAINT PK_Tableau_Priority PRIMARY KEY (Kind, Item_id),
    CONSTRAINT CK_Tableau_Priority_Kind CHECK (Kind IN ('Extract', 'Subscription'))
);
GO
CREATE OR ALTER VIEW Integration.vw_Tableau_PriorityPicker
AS
SELECT 'Extract' AS Kind, ds.ExtractRefreshTargetId AS Item_id, MAX(ds.DataSource_Name) AS Name, p.Priority
FROM Integration.Tableau_DataSource ds
LEFT JOIN Integration.Tableau_Priority p ON p.Kind = 'Extract' AND p.Item_id = ds.ExtractRefreshTargetId
WHERE ds.Active_Flag = 1 AND ds.ExtractRefreshTargetId IS NOT NULL
GROUP BY ds.ExtractRefreshTargetId, p.Priority
UNION ALL
SELECT 'Subscription', s.Subscription_id,
       ISNULL((SELECT TOP 1 w.Workbook_Name FROM Integration.Tableau_WorkbookDataSource w WHERE w.Workbook_id = s.Workbook_id
               ORDER BY w.Active_Flag DESC, w.LastSeenAt DESC), s.Workbook_id) + ' / ' + ISNULL(s.Subject, ''),
       p.Priority
FROM Integration.Tableau_Subscription s
LEFT JOIN Integration.Tableau_Priority p ON p.Kind = 'Subscription' AND p.Item_id = s.Subscription_id
WHERE s.Active_Flag = 1;
GO
SET NOEXEC OFF;
GO
