SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') BEGIN RAISERROR('Wrong database or server: only the DEV DWH (<DEV_DWH_SERVER> / CQGlobalSGDW) is allowed; nothing below is run (SET NOEXEC ON).', 16, 1); SET NOEXEC ON; END
GO
/*
R8 (D-053) - non-DWH sources count as ready. Dev DWH first (D-026). Re-runnable. Additive: ONE new column
Integration.Tableau_DataSource.NonDwhConnection (bit, default 0) and the two readiness views redefined.
T2 sets the column to 1 only when it POSITIVELY identified every skipped connection of the source as non-DWH (Excel/CSV/other database,
named connection type). A source with such a flag and NO DWH table gets ReadyStatus 'NotChecked', which counts as ready at workbook level.
Everything else is unchanged: no flag and no tables = NoTables (not ready); flag plus an unmapped DWH table = UnmappedTable (not ready);
flag plus mapped DWH tables = Ready/waits on those tables only.
Apply BEFORE the new t2 code runs, then run t2_mapping_sync.py once. Rollback: scripts/sql/R8_nondwh_sources_rollback.sql
*/
IF COL_LENGTH('Integration.Tableau_DataSource', 'NonDwhConnection') IS NULL
    ALTER TABLE Integration.Tableau_DataSource ADD NonDwhConnection bit NOT NULL CONSTRAINT DF_Tableau_DataSource_NonDwh DEFAULT 0;
GO
CREATE OR ALTER VIEW Integration.vw_Tableau_DataSourceReadiness
AS
SELECT ds.DataSource_id,
       ds.DataSource_Name,
       ds.Active_Flag,
       ds.RefreshKind,
       ds.ExtractRefreshTargetId,
       CASE WHEN o.DataSource_id IS NOT NULL       THEN 'NoReadinessCheck'
            WHEN ds.RefreshKind = 'Unknown'        THEN 'UnknownKind'
            WHEN ISNULL(c.TableCount, 0) = 0 AND ds.NonDwhConnection = 1 THEN 'NotChecked'
            WHEN ISNULL(c.TableCount, 0) = 0       THEN 'NoTables'
            WHEN c.UnmappedCount > 0               THEN 'UnmappedTable'
            WHEN ISNULL(pl.UnknownCount, 0) > 0    THEN 'UnknownPipeline'
            ELSE 'Ready' END                       AS ReadyStatus,
       ISNULL(c.TableCount, 0)                     AS TableCount,
       ISNULL(c.UnmappedCount, 0)                  AS UnmappedCount,
       ISNULL(pl.PipelineCount, 0)                 AS RequiredPipelineCount,
       pl.RequiredPipelines
FROM Integration.Tableau_DataSource AS ds
LEFT JOIN Integration.Tableau_DataSourceOverride AS o
       ON o.DataSource_id = ds.DataSource_id AND o.NoReadinessCheck = 1
LEFT JOIN (SELECT DataSource_id,
                  COUNT(*) AS TableCount,
                  SUM(CASE WHEN IsStatic = 0 AND IsRegistered = 0 THEN 1 ELSE 0 END) AS UnmappedCount
           FROM Integration.vw_Tableau_DataSourceTableStatus
           GROUP BY DataSource_id) AS c
       ON c.DataSource_id = ds.DataSource_id
LEFT JOIN (SELECT DataSource_id,
                  COUNT(*) AS PipelineCount,
                  SUM(CASE WHEN PipelineKnown = 0 THEN 1 ELSE 0 END) AS UnknownCount,
                  STRING_AGG(PipelineName, '|') WITHIN GROUP (ORDER BY PipelineName) AS RequiredPipelines
           FROM Integration.vw_Tableau_DataSourcePipeline
           GROUP BY DataSource_id) AS pl
       ON pl.DataSource_id = ds.DataSource_id
WHERE ds.Active_Flag = 1;
GO
CREATE OR ALTER VIEW Integration.vw_Tableau_WorkbookReadiness
AS
WITH wb AS (
    SELECT Workbook_id FROM Integration.Tableau_Subscription
    UNION
    SELECT Workbook_id FROM Integration.Tableau_WorkbookDataSource WHERE Active_Flag = 1
), links AS (
    SELECT wb.Workbook_id, w.DataSource_id, r.ReadyStatus
    FROM wb
    LEFT JOIN Integration.Tableau_WorkbookDataSource AS w
           ON w.Workbook_id = wb.Workbook_id AND w.Active_Flag = 1
    LEFT JOIN Integration.vw_Tableau_DataSourceReadiness AS r
           ON r.DataSource_id = w.DataSource_id
)
SELECT Workbook_id,
       CASE WHEN COUNT(DataSource_id) = 0 THEN 'NoDataSources'
            WHEN SUM(CASE WHEN DataSource_id IS NOT NULL AND ReadyStatus IS NULL THEN 1 ELSE 0 END) > 0 THEN 'InactiveDataSource'
            WHEN SUM(CASE WHEN ReadyStatus NOT IN ('Ready','NoReadinessCheck','NotChecked') THEN 1 ELSE 0 END) > 0 THEN 'NotReady'
            ELSE 'Ready' END AS WorkbookStatus,
       COUNT(DataSource_id) AS DataSourceCount,
       SUM(CASE WHEN DataSource_id IS NOT NULL AND ReadyStatus IS NULL THEN 1 ELSE 0 END) AS InactiveCount,
       SUM(CASE WHEN ReadyStatus NOT IN ('Ready','NoReadinessCheck','NotChecked') THEN 1 ELSE 0 END) AS NotReadyCount
FROM links
GROUP BY Workbook_id;
GO
SET NOEXEC OFF;
GO
