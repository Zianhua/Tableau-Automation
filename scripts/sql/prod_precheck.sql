/* Read-only pre-check before deploying the Tableau subscription automation to a warehouse (UAT or production).
   Changes nothing. Run it in SSMS on the target warehouse database (CQGlobalSGDW) with any login that can see metadata
   (VIEW DEFINITION). Every row should say OK; anything MISSING must be fixed by the DBJR before the scripts are switched on.
   Role checked: <SERVICE_DB_ROLE> (the service account must be a member of it). Run it with a login that has VIEW DEFINITION
   (or db_owner / sysadmin): without it, grants of other principals are invisible and show falsely as MISSING GRANT.
   Then compare [server] in the last result with CONN_SERVER / DEV_DWH_SERVER in the host's .env: the scripts refuse to run unless
   @@SERVERNAME starts with the first part of that name (a listener or alias name fails closed). */
SET NOCOUNT ON;
DECLARE @role sysname = N'<SERVICE_DB_ROLE>';

DECLARE @need TABLE (obj sysname, perms varchar(100), why varchar(200));
INSERT @need VALUES
 ('Integration.Tableau_Subscription',              'SELECT,INSERT,UPDATE', 'mapping sync'),
 ('Integration.Tableau_WorkbookDataSource',        'SELECT,INSERT,UPDATE', 'mapping sync'),
 ('Integration.Tableau_DataSource',                'SELECT,INSERT,UPDATE', 'mapping sync'),
 ('Integration.Tableau_DataSourceTable',           'SELECT,INSERT,UPDATE,DELETE', 'mapping sync: rows of tables no longer read are removed'),
 ('Integration.Tableau_Schedule',                  'SELECT,INSERT,UPDATE', 'mapping sync, due days'),
 ('Integration.Tableau_PipelineTable',             'SELECT,INSERT,DELETE', 'registry (Auto rows)'),
 ('Integration.Tableau_StaticTable',               'SELECT',               'static tables'),
 ('Integration.Tableau_LineageSource',             'SELECT',               'registry lineage sources'),
 ('Integration.Tableau_SubscriptionBackup',        'SELECT,INSERT,UPDATE', 'subscription takeover'),
 ('Integration.Tableau_SubscriptionSendLog',       'SELECT,INSERT,UPDATE', 'send log'),
 ('Integration.Tableau_RefreshJob',                'SELECT,INSERT,UPDATE', 'extract refresh log'),
 ('Integration.Tableau_ExtractTask',               'SELECT,INSERT,UPDATE', 'Tableau extract tasks'),
 ('Integration.Tableau_ExtractTaskBackup',         'SELECT,INSERT,UPDATE', 'extract takeover'),
 ('Integration.vw_Tableau_DataSourceReadiness',    'SELECT',               'readiness'),
 ('Integration.vw_Tableau_WorkbookReadiness',      'SELECT',               'readiness'),
 ('Integration.vw_Tableau_DataSourcePipeline',     'SELECT',               'readiness'),
 ('Integration.vw_Tableau_DataSourceTableStatus',  'SELECT',               'readiness'),
 ('Integration.vw_Tableau_ExtractSchedule',        'SELECT',               't4 schedule rule'),
 ('Integration.ETL_Master_Log',                    'SELECT',               'ETL completion gate'),
 ('Integration.ETL_Tableau_ExtractMapping',        'SELECT,UPDATE',        'old refresh job list: t4 reads it, the takeover switches Active_Flag'),
 ('Integration.Lineage',                           'SELECT',               'registry lineage'),
 ('Integration.DW_NovaLineage',                    'SELECT',               'registry lineage (if listed in Tableau_LineageSource)'),
 ('Integration.OAO_Lineage',                       'SELECT',               'registry lineage (if listed in Tableau_LineageSource)');

;WITH want AS (
    SELECT n.obj, n.why, LTRIM(RTRIM(s.value)) AS perm
    FROM @need n CROSS APPLY STRING_SPLIT(n.perms, ',') s),
have AS (
    SELECT OBJECT_SCHEMA_NAME(p.major_id) + '.' + OBJECT_NAME(p.major_id) AS obj, p.permission_name AS perm
    FROM sys.database_permissions p JOIN sys.database_principals r ON r.principal_id = p.grantee_principal_id
    WHERE r.name = @role AND p.state IN ('G', 'W') AND p.class = 1
    UNION
    SELECT o.obj, p.permission_name                     -- schema-level grants cover every object in the schema
    FROM sys.database_permissions p JOIN sys.database_principals r ON r.principal_id = p.grantee_principal_id
    CROSS JOIN (SELECT obj FROM @need) o
    WHERE r.name = @role AND p.state IN ('G', 'W') AND p.class = 3 AND SCHEMA_NAME(p.major_id) = PARSENAME(o.obj, 2))
SELECT w.obj AS [object], w.perm AS [permission],
       CASE WHEN OBJECT_ID(w.obj) IS NULL THEN 'MISSING OBJECT'
            WHEN h.obj IS NULL THEN 'MISSING GRANT' ELSE 'OK' END AS [status], w.why
FROM want w LEFT JOIN have h ON h.obj = w.obj AND h.perm = w.perm
ORDER BY CASE WHEN OBJECT_ID(w.obj) IS NULL OR h.obj IS NULL THEN 0 ELSE 1 END, w.obj, w.perm;

-- Columns added by later steps of the DBJR (the scripts refuse to start without them)
SELECT c.t AS [table], c.col AS [column], CASE WHEN COL_LENGTH(c.t, c.col) IS NULL THEN 'MISSING' ELSE 'OK' END AS [status]
FROM (VALUES ('Integration.Tableau_Subscription', 'ScheduleId'), ('Integration.Tableau_Subscription', 'WorkbookUpdatedAt'),
             ('Integration.Tableau_DataSource', 'NonDwhConnection'), ('Integration.Tableau_PipelineTable', 'Source'),
             ('Integration.Tableau_ExtractTaskBackup', 'NoRetake'), ('Integration.Tableau_ExtractTaskBackup', 'OldJobMappingIds'),
             ('Integration.ETL_Tableau_ExtractMapping', 'Mapping_id'), ('Integration.ETL_Tableau_ExtractMapping', 'DataSource_id'),
             ('Integration.ETL_Tableau_ExtractMapping', 'Active_Flag')) c(t, col);

-- Database-level rights and membership
SELECT 'VIEW DEFINITION for the role, a member, or a member login (server-level VIEW ANY DEFINITION); has it: ' + ISNULL((
          SELECT STRING_AGG(x.who, ', ') FROM (
            SELECT g.name + ' (database)' AS who FROM sys.database_permissions p JOIN sys.database_principals g ON g.principal_id = p.grantee_principal_id
            WHERE p.class = 0 AND p.permission_name = 'VIEW DEFINITION' AND p.state IN ('G', 'W')
              AND (g.name = @role OR g.principal_id IN (SELECT rm.member_principal_id FROM sys.database_role_members rm
                   JOIN sys.database_principals r ON r.principal_id = rm.role_principal_id WHERE r.name = @role))
            UNION
            SELECT sp.name + ' (server)' FROM sys.server_permissions p JOIN sys.server_principals sp ON sp.principal_id = p.grantee_principal_id
            WHERE p.permission_name IN ('VIEW ANY DEFINITION', 'CONTROL SERVER') AND p.state IN ('G', 'W')
              AND sp.sid IN (SELECT m.sid FROM sys.database_role_members rm JOIN sys.database_principals r ON r.principal_id = rm.role_principal_id
                             JOIN sys.database_principals m ON m.principal_id = rm.member_principal_id WHERE r.name = @role)) x), '(nobody)') AS [check],
       CASE WHEN EXISTS (SELECT 1 FROM sys.database_permissions p JOIN sys.database_principals g ON g.principal_id = p.grantee_principal_id
                         WHERE p.class = 0 AND p.permission_name = 'VIEW DEFINITION' AND p.state IN ('G', 'W')
                           AND (g.name = @role OR g.principal_id IN (SELECT rm.member_principal_id FROM sys.database_role_members rm
                                JOIN sys.database_principals r ON r.principal_id = rm.role_principal_id WHERE r.name = @role)))
              OR EXISTS (SELECT 1 FROM sys.server_permissions p JOIN sys.server_principals sp ON sp.principal_id = p.grantee_principal_id
                         WHERE p.permission_name IN ('VIEW ANY DEFINITION', 'CONTROL SERVER') AND p.state IN ('G', 'W')
                           AND sp.sid IN (SELECT m.sid FROM sys.database_role_members rm JOIN sys.database_principals r ON r.principal_id = rm.role_principal_id
                                          JOIN sys.database_principals m ON m.principal_id = rm.member_principal_id WHERE r.name = @role))
            THEN 'OK (check the host runs as a login listed here)' ELSE 'MISSING' END AS [status]
UNION ALL
SELECT 'members of ' + @role + ': ' + ISNULL((SELECT STRING_AGG(m.name, ', ') FROM sys.database_role_members rm
          JOIN sys.database_principals r ON r.principal_id = rm.role_principal_id
          JOIN sys.database_principals m ON m.principal_id = rm.member_principal_id WHERE r.name = @role), '(none)'),
       CASE WHEN EXISTS (SELECT 1 FROM sys.database_role_members rm JOIN sys.database_principals r ON r.principal_id = rm.role_principal_id
                         WHERE r.name = @role) THEN 'OK' ELSE 'MISSING' END;

-- Seed rows the solution needs (zero = nothing becomes Ready, so nothing is refreshed or sent; it fails safe but does nothing)
IF OBJECT_ID('Integration.Tableau_PipelineTable') IS NOT NULL AND COL_LENGTH('Integration.Tableau_PipelineTable', 'Source') IS NOT NULL
    EXEC ('SELECT ''registry rows'' AS [seed], Source, COUNT(*) AS [rows] FROM Integration.Tableau_PipelineTable GROUP BY Source');
IF OBJECT_ID('Integration.Tableau_LineageSource') IS NOT NULL
    EXEC ('SELECT ''lineage sources'' AS [seed], ls.LineageTable, ls.PipelineName,
                  CASE WHEN OBJECT_ID(''Integration.'' + ls.LineageTable) IS NULL THEN ''MISSING TABLE'' ELSE ''OK'' END AS [status]
           FROM Integration.Tableau_LineageSource ls');
IF OBJECT_ID('Integration.Tableau_StaticTable') IS NOT NULL
    EXEC ('SELECT ''static tables'' AS [seed], COUNT(*) AS [rows] FROM Integration.Tableau_StaticTable');
IF OBJECT_ID('Integration.ETL_Tableau_ExtractMapping') IS NOT NULL
    EXEC ('SELECT ''old refresh job list'' AS [seed], COUNT(*) AS [rows], SUM(CASE WHEN Active_Flag = 1 THEN 1 ELSE 0 END) AS [active],
                  COUNT(DISTINCT DataSource_id) AS [data sources] FROM Integration.ETL_Tableau_ExtractMapping');

-- Server identity and clock (the scripts require UTC+8 and match @@SERVERNAME against CONN_SERVER / DEV_DWH_SERVER in .env)
SELECT @@SERVERNAME AS [server], DB_NAME() AS [database], DATEPART(TZOFFSET, SYSDATETIMEOFFSET()) AS [utc_offset_min_must_be_480],
       CONVERT(varchar(40), SYSDATETIMEOFFSET(), 121) AS [server_time];
