SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
IF DB_NAME() <> 'CQGlobalSGDW' OR UPPER(@@SERVERNAME) NOT IN ('<DEV_DWH_SERVER>') BEGIN RAISERROR('Wrong database or server: only the DEV DWH (<DEV_DWH_SERVER> / CQGlobalSGDW) is allowed; nothing below is run (SET NOEXEC ON).', 16, 1); SET NOEXEC ON; END
GO
/*
T1 tests. Dev DWH only. Test rows are all named TEST-* / TESTSCH and are deleted at the start and at the end
(no outer transaction: the Sent trigger and the static/registry triggers raise errors on purpose).
Results are returned as the last result set. Needs T1_mapping_schema.sql and T3_load_registry.sql.
The two-session claim race is in T1_concurrency_test.ps1.
NOTE (2026-10-26): since the R3 send-log delete guard this script can no longer clean up its own 'Sent' test row in the send-log part, so
do NOT run it again on a database that matters; the readiness cases R8a..R8h at the top of it are also covered by r1_checks.py (rolled back, no send log).
*/
SET NOCOUNT ON;
DECLARE @r TABLE (Test varchar(70), Passed bit, Detail nvarchar(400));
DECLARE @today date = CAST(SYSDATETIMEOFFSET() AT TIME ZONE 'Singapore Standard Time' AS date);

-- cleanup (start) -----------------------------------------------------------------------------
DELETE FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id LIKE 'TEST-%';
DELETE FROM Integration.Tableau_Subscription WHERE Subscription_id LIKE 'TEST-%';
DELETE FROM Integration.Tableau_RefreshJob WHERE DataSource_id LIKE 'TEST-%';
DELETE FROM Integration.Tableau_WorkbookDataSource WHERE Workbook_id LIKE 'TEST-%';
DELETE FROM Integration.Tableau_DataSourceTable WHERE DataSource_id LIKE 'TEST-%';
DELETE FROM Integration.Tableau_DataSourceOverride WHERE DataSource_id LIKE 'TEST-%';
DELETE FROM Integration.Tableau_DataSource WHERE DataSource_id LIKE 'TEST-%';
DELETE FROM Integration.Tableau_PipelineTable WHERE SchemaName = 'TESTSCH';
DELETE FROM Integration.Tableau_StaticTable WHERE SchemaName = 'TESTSCH';

BEGIN TRY
    ----- readiness: data sources ------------------------------------------------------------------
    INSERT Integration.Tableau_PipelineTable (PipelineName, SchemaName, TableName, ConfirmedBy)
        VALUES ('TEST pipeline not in log', 'TESTSCH', 'T1', 'test');
    INSERT Integration.Tableau_DataSource (DataSource_id, DataSource_Name, LinkedPublishedDatasourceId, IsExtract, Active_Flag) VALUES
      ('TEST-notables',  'no tables',            NULL, 1, 1),
      ('TEST-unmapped',  'unmapped table',       NULL, 1, 1),
      ('TEST-ready',     'two pipelines',        NULL, 1, 1),
      ('TEST-static',    'static + registered',  NULL, 1, 1),
      ('TEST-onlystatic','only static',          NULL, 1, 1),
      ('TEST-override',  'override, no tables',  NULL, 1, 1),
      ('TEST-ovr0',      'override=0 ignored',   NULL, 1, 1),
      ('TEST-pub',       'published extract',    NULL, 1, 1),
      ('TEST-emb',       'embedded -> published','TEST-pub', 1, 1),
      ('TEST-unknown',   'IsExtract NULL',       NULL, NULL, 1),
      ('TEST-live',      'IsExtract 0',          NULL, 0, 1),
      ('TEST-badpipe',   'pipeline not in log',  NULL, 1, 1),
      ('TEST-inactive',  'inactive source',      NULL, 1, 0);
    -- R8 / D-053: flagged = T2 positively identified the connection(s) as non-DWH (file / other database)
    INSERT Integration.Tableau_DataSource (DataSource_id, DataSource_Name, LinkedPublishedDatasourceId, IsExtract, NonDwhConnection, Active_Flag) VALUES
      ('TEST-file',      'file only, flagged',          NULL, 0, 1, 1),
      ('TEST-filedwh',   'file + mapped DWH table',     NULL, 0, 1, 1),
      ('TEST-fileunm',   'file + unmapped DWH table',   NULL, 0, 1, 1),
      ('TEST-fileunk',   'file only, IsExtract NULL',   NULL, NULL, 1, 1);
    INSERT Integration.Tableau_DataSourceTable (DataSource_id, SchemaName, TableName) VALUES
      ('TEST-filedwh','Dimension','AE'), ('TEST-fileunm','dbo','TestNotInRegistry');
    INSERT Integration.Tableau_DataSourceTable (DataSource_id, SchemaName, TableName) VALUES
      ('TEST-unmapped','dbo','TestNotInRegistry'), ('TEST-unmapped','Dimension','AE'),
      ('TEST-ready','Dimension','AE'), ('TEST-ready','Fact','DailyNovaTradeVolume'),
      ('TEST-static','dbo','SalesChannel_Excel'), ('TEST-static','Dimension','AE'),
      ('TEST-onlystatic','dbo','SalesChannel_Excel'),
      ('TEST-ovr0','dbo','TestNotInRegistry'),
      ('TEST-pub','Dimension','Client'),
      ('TEST-unknown','Dimension','AE'),
      ('TEST-live','Dimension','AE'),
      ('TEST-badpipe','TESTSCH','T1'),
      ('TEST-inactive','Dimension','AE');
    INSERT Integration.Tableau_DataSourceOverride (DataSource_id, NoReadinessCheck, Reason, SetBy) VALUES
      ('TEST-override',1,'test','test'), ('TEST-ovr0',0,'test','test');

    DECLARE @s TABLE (id nvarchar(128), st varchar(20), np int, pl nvarchar(400), kind varchar(10), tgt nvarchar(128));
    INSERT @s SELECT DataSource_id, ReadyStatus, RequiredPipelineCount, RequiredPipelines, RefreshKind, ExtractRefreshTargetId
              FROM Integration.vw_Tableau_DataSourceReadiness WHERE DataSource_id LIKE 'TEST-%';

    INSERT @r SELECT '1 no tables -> NoTables', CASE WHEN st='NoTables' THEN 1 ELSE 0 END, st FROM @s WHERE id='TEST-notables';
    INSERT @r SELECT '2 unregistered table -> UnmappedTable', CASE WHEN st='UnmappedTable' THEN 1 ELSE 0 END, st FROM @s WHERE id='TEST-unmapped';
    INSERT @r SELECT '3 all registered -> Ready + 2 pipelines', CASE WHEN st='Ready' AND np=2
        AND pl='CQGlobalSGDW: DailyETL|CQGlobalSGDW: NovaDailyETL' THEN 1 ELSE 0 END, st+' | '+ISNULL(pl,'NULL') FROM @s WHERE id='TEST-ready';
    INSERT @r SELECT '4a static table does not block', CASE WHEN st='Ready' AND np=1 AND pl='CQGlobalSGDW: DailyETL' THEN 1 ELSE 0 END, st+' | '+ISNULL(pl,'NULL') FROM @s WHERE id='TEST-static';
    INSERT @r SELECT '4b only-static source Ready, 0 pipelines', CASE WHEN st='Ready' AND np=0 THEN 1 ELSE 0 END, st FROM @s WHERE id='TEST-onlystatic';
    INSERT @r SELECT '4c override=1 -> NoReadinessCheck', CASE WHEN st='NoReadinessCheck' THEN 1 ELSE 0 END, st FROM @s WHERE id='TEST-override';
    INSERT @r SELECT '4d embedded uses published tables', CASE WHEN st='Ready' AND pl='CQGlobalSGDW: DailyETL' THEN 1 ELSE 0 END, st+' | '+ISNULL(pl,'NULL') FROM @s WHERE id='TEST-emb';
    INSERT @r SELECT '4e refresh target dedupe (embedded + published)', CASE WHEN COUNT(DISTINCT tgt)=1 AND MIN(tgt)='TEST-pub' THEN 1 ELSE 0 END, ISNULL(MIN(tgt),'NULL') FROM @s WHERE id IN ('TEST-pub','TEST-emb');
    INSERT @r SELECT 'M1a IsExtract NULL -> Unknown, not Ready, no target', CASE WHEN kind='Unknown' AND st='UnknownKind' AND tgt IS NULL THEN 1 ELSE 0 END, kind+' | '+st FROM @s WHERE id='TEST-unknown';
    INSERT @r SELECT 'M1b IsExtract 0 -> Live, Ready, no target', CASE WHEN kind='Live' AND st='Ready' AND tgt IS NULL THEN 1 ELSE 0 END, kind+' | '+st FROM @s WHERE id='TEST-live';
    INSERT @r SELECT 'M1c IsExtract 1 -> Extract with target', CASE WHEN kind='Extract' AND tgt='TEST-ready' THEN 1 ELSE 0 END, kind+' | '+ISNULL(tgt,'NULL') FROM @s WHERE id='TEST-ready';
    INSERT @r SELECT 'M2a pipeline not in ETL_Master_Log -> UnknownPipeline', CASE WHEN st='UnknownPipeline' THEN 1 ELSE 0 END, st FROM @s WHERE id='TEST-badpipe';
    INSERT @r SELECT 'M2b real pipelines are PipelineKnown', CASE WHEN COUNT(*)>0 AND MIN(CAST(PipelineKnown AS int))=1 THEN 1 ELSE 0 END, CONCAT(COUNT(*),' rows')
        FROM Integration.vw_Tableau_DataSourcePipeline WHERE DataSource_id='TEST-ready';
    INSERT @r SELECT 'N NoReadinessCheck=0 override is ignored', CASE WHEN st='UnmappedTable' THEN 1 ELSE 0 END, st FROM @s WHERE id='TEST-ovr0';
    INSERT @r SELECT 'R8a flagged file source, no DWH table -> NotChecked', CASE WHEN st='NotChecked' AND np=0 THEN 1 ELSE 0 END, st FROM @s WHERE id='TEST-file';
    INSERT @r SELECT 'R8b file + mapped DWH table -> Ready (that pipeline only)', CASE WHEN st='Ready' AND pl='CQGlobalSGDW: DailyETL' THEN 1 ELSE 0 END, st+' | '+ISNULL(pl,'NULL') FROM @s WHERE id='TEST-filedwh';
    INSERT @r SELECT 'R8c file + unmapped DWH table -> UnmappedTable', CASE WHEN st='UnmappedTable' THEN 1 ELSE 0 END, st FROM @s WHERE id='TEST-fileunm';
    INSERT @r SELECT 'R8d not flagged and no tables stays NoTables', CASE WHEN st='NoTables' THEN 1 ELSE 0 END, st FROM @s WHERE id='TEST-notables';
    INSERT @r SELECT 'R8e flagged but IsExtract unknown -> UnknownKind', CASE WHEN st='UnknownKind' THEN 1 ELSE 0 END, st FROM @s WHERE id='TEST-fileunk';
    INSERT @r SELECT 'H4a inactive data source hidden from source view', CASE WHEN COUNT(*)=0 THEN 1 ELSE 0 END, CONCAT(COUNT(*),' rows') FROM @s WHERE id='TEST-inactive';

    ----- readiness: workbooks ---------------------------------------------------------------------
    INSERT Integration.Tableau_Subscription (Subscription_id, Workbook_id, TargetType, TargetId, Format)
        VALUES ('TEST-sub-nods','TEST-wb-nods','Workbook','TEST-wb-nods','PDF');
    INSERT Integration.Tableau_WorkbookDataSource (Workbook_id, Workbook_Name, DataSource_id) VALUES
      ('TEST-wb-ready','w','TEST-ready'), ('TEST-wb-ready','w','TEST-static'), ('TEST-wb-ready','w','TEST-override'),
      ('TEST-wb-notready','w','TEST-ready'), ('TEST-wb-notready','w','TEST-unmapped'),
      ('TEST-wb-inactive','w','TEST-ready'), ('TEST-wb-inactive','w','TEST-inactive'),
      ('TEST-wb-unknown','w','TEST-ready'), ('TEST-wb-unknown','w','TEST-unknown'),
      ('TEST-wb-removed','w','TEST-unmapped'),
      ('TEST-wb-file','w','TEST-file'), ('TEST-wb-filedwh','w','TEST-file'), ('TEST-wb-filedwh','w','TEST-ready'),
      ('TEST-wb-fileunm','w','TEST-file'), ('TEST-wb-fileunm','w','TEST-fileunm');
    UPDATE Integration.Tableau_WorkbookDataSource SET Active_Flag = 0 WHERE Workbook_id='TEST-wb-removed';   -- link removed, no active links left
    DECLARE @w TABLE (id nvarchar(128), st varchar(20));
    INSERT @w SELECT Workbook_id, WorkbookStatus FROM Integration.vw_Tableau_WorkbookReadiness WHERE Workbook_id LIKE 'TEST-%';
    INSERT @r SELECT 'H4b workbook with no data sources -> NoDataSources', CASE WHEN st='NoDataSources' THEN 1 ELSE 0 END, st FROM @w WHERE id='TEST-wb-nods';
    INSERT @r SELECT 'H4c all sources Ready/override -> Ready', CASE WHEN st='Ready' THEN 1 ELSE 0 END, st FROM @w WHERE id='TEST-wb-ready';
    INSERT @r SELECT 'H4d one unmapped -> NotReady', CASE WHEN st='NotReady' THEN 1 ELSE 0 END, st FROM @w WHERE id='TEST-wb-notready';
    INSERT @r SELECT 'H4e inactive linked source -> InactiveDataSource', CASE WHEN st='InactiveDataSource' THEN 1 ELSE 0 END, st FROM @w WHERE id='TEST-wb-inactive';
    INSERT @r SELECT 'H4f Unknown-kind source -> NotReady', CASE WHEN st='NotReady' THEN 1 ELSE 0 END, st FROM @w WHERE id='TEST-wb-unknown';
    INSERT @r SELECT 'R8f workbook with only a file source -> Ready', CASE WHEN st='Ready' THEN 1 ELSE 0 END, st FROM @w WHERE id='TEST-wb-file';
    INSERT @r SELECT 'R8g file source + ready DWH source -> Ready', CASE WHEN st='Ready' THEN 1 ELSE 0 END, st FROM @w WHERE id='TEST-wb-filedwh';
    INSERT @r SELECT 'R8h file source + unmapped DWH source -> NotReady', CASE WHEN st='NotReady' THEN 1 ELSE 0 END, st FROM @w WHERE id='TEST-wb-fileunm';
    INSERT @r SELECT 'H4g removed link only -> not Ready (workbook absent or NoDataSources)', CASE WHEN NOT EXISTS (SELECT 1 FROM @w WHERE id='TEST-wb-removed' AND st='Ready') THEN 1 ELSE 0 END, ISNULL((SELECT st FROM @w WHERE id='TEST-wb-removed'),'absent');

    ----- L1: static vs registry --------------------------------------------------------------------
    BEGIN TRY
        INSERT Integration.Tableau_StaticTable (SchemaName, TableName, Reason, SetBy) VALUES ('Dimension','AE','test','test');
        INSERT @r VALUES ('L1a static insert of a registered table rejected', 0, 'accepted');
        DELETE FROM Integration.Tableau_StaticTable WHERE SchemaName='Dimension' AND TableName='AE';
    END TRY
    BEGIN CATCH
        INSERT @r VALUES ('L1a static insert of a registered table rejected', CASE WHEN ERROR_NUMBER()=50002 THEN 1 ELSE 0 END, LEFT(ERROR_MESSAGE(),100));
    END CATCH
    BEGIN TRY
        INSERT Integration.Tableau_PipelineTable (PipelineName, SchemaName, TableName, ConfirmedBy) VALUES ('CQGlobalSGDW: DailyETL','dbo','SalesChannel_Excel','test');
        INSERT @r VALUES ('L1b registry insert of a static table rejected', 0, 'accepted');
        DELETE FROM Integration.Tableau_PipelineTable WHERE SchemaName='dbo' AND TableName='SalesChannel_Excel';
    END TRY
    BEGIN CATCH
        INSERT @r VALUES ('L1b registry insert of a static table rejected', CASE WHEN ERROR_NUMBER()=50002 THEN 1 ELSE 0 END, LEFT(ERROR_MESSAGE(),100));
    END CATCH

    ----- send log -----------------------------------------------------------------------------------
    INSERT Integration.Tableau_Subscription (Subscription_id, Workbook_id, TargetType, TargetId, Format) VALUES
      ('TEST-sub','TEST-wb','Workbook','TEST-wb','PDF'), ('TEST-sub2','TEST-wb','Workbook','TEST-wb','PDF'),
      ('TEST-sub3','TEST-wb','Workbook','TEST-wb','PDF'), ('TEST-sub4','TEST-wb','Workbook','TEST-wb','PDF');
    INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status) VALUES ('TEST-sub','Claimed');

    DECLARE @st varchar(10);
    DECLARE c CURSOR LOCAL FAST_FORWARD FOR SELECT v FROM (VALUES ('Claimed'),('Sent'),('TimedOut'),('Failed'),('DryRun'),('Released')) t(v);
    OPEN c; FETCH NEXT FROM c INTO @st;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        BEGIN TRY
            INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status, SentAt)
                VALUES ('TEST-sub', @st, CASE WHEN @st='Sent' THEN SYSDATETIMEOFFSET() END);
            INSERT @r VALUES ('5 while Claimed exists: second '+@st+' row', CASE WHEN @st IN ('Failed','DryRun','Released') THEN 1 ELSE 0 END, 'accepted');
        END TRY
        BEGIN CATCH
            INSERT @r VALUES ('5 while Claimed exists: second '+@st+' row', CASE WHEN @st IN ('Claimed','Sent','TimedOut') AND ERROR_NUMBER()=2601 THEN 1 ELSE 0 END, 'rejected '+CAST(ERROR_NUMBER() AS varchar(10)));
        END CATCH
        FETCH NEXT FROM c INTO @st;
    END
    CLOSE c; DEALLOCATE c;

    -- H1: Claimed -> TimedOut still blocks a new Claimed; Failed/Released frees the slot
    UPDATE Integration.Tableau_SubscriptionSendLog SET Status='TimedOut' WHERE Subscription_id='TEST-sub' AND Status='Claimed';
    BEGIN TRY
        INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status) VALUES ('TEST-sub','Claimed');
        INSERT @r VALUES ('H1a new Claimed after TimedOut rejected', 0, 'accepted');
    END TRY
    BEGIN CATCH
        INSERT @r VALUES ('H1a new Claimed after TimedOut rejected', CASE WHEN ERROR_NUMBER()=2601 THEN 1 ELSE 0 END, 'rejected '+CAST(ERROR_NUMBER() AS varchar(10)));
    END CATCH
    UPDATE Integration.Tableau_SubscriptionSendLog SET Status='Failed' WHERE Subscription_id='TEST-sub' AND Status='TimedOut';
    BEGIN TRY
        INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status) VALUES ('TEST-sub','Claimed');
        INSERT @r VALUES ('H1b Failed (human release) allows re-claim', 1, 'accepted');
    END TRY
    BEGIN CATCH
        INSERT @r VALUES ('H1b Failed (human release) allows re-claim', 0, ERROR_MESSAGE());
    END CATCH
    -- Released path on another subscription
    INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status) VALUES ('TEST-sub2','Claimed');
    UPDATE Integration.Tableau_SubscriptionSendLog SET Status='TimedOut' WHERE Subscription_id='TEST-sub2';
    UPDATE Integration.Tableau_SubscriptionSendLog SET Status='Released' WHERE Subscription_id='TEST-sub2';
    BEGIN TRY
        INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status) VALUES ('TEST-sub2','Claimed');
        INSERT @r VALUES ('H1c Released allows re-claim', 1, 'accepted');
    END TRY
    BEGIN CATCH
        INSERT @r VALUES ('H1c Released allows re-claim', 0, ERROR_MESSAGE());
    END CATCH

    -- H2: Sent is final
    UPDATE Integration.Tableau_SubscriptionSendLog SET Status='Sent', SentAt=SYSDATETIMEOFFSET() WHERE Subscription_id='TEST-sub' AND Status IN ('Claimed','TimedOut');
    INSERT @r SELECT '5b Claimed -> Sent update', CASE WHEN @@ROWCOUNT=1 THEN 1 ELSE 0 END, '';
    DECLARE @tgt varchar(10);
    DECLARE c2 CURSOR LOCAL FAST_FORWARD FOR SELECT v FROM (VALUES ('Failed'),('Claimed'),('TimedOut'),('Released')) t(v);
    OPEN c2; FETCH NEXT FROM c2 INTO @tgt;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        BEGIN TRY
            UPDATE Integration.Tableau_SubscriptionSendLog SET Status=@tgt WHERE Subscription_id='TEST-sub' AND Status='Sent';
            INSERT @r VALUES ('H2 Sent -> '+@tgt+' rejected', 0, 'accepted');
        END TRY
        BEGIN CATCH
            INSERT @r VALUES ('H2 Sent -> '+@tgt+' rejected', CASE WHEN ERROR_NUMBER()=50001 THEN 1 ELSE 0 END, 'rejected '+CAST(ERROR_NUMBER() AS varchar(10)));
        END CATCH
        FETCH NEXT FROM c2 INTO @tgt;
    END
    CLOSE c2; DEALLOCATE c2;
    UPDATE Integration.Tableau_SubscriptionSendLog SET Message='note' WHERE Subscription_id='TEST-sub' AND Status='Sent';
    INSERT @r SELECT 'H2 Sent row can still get a Message (no status change)', CASE WHEN @@ROWCOUNT=1 THEN 1 ELSE 0 END, '';
    BEGIN TRY
        INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status, SentAt) VALUES ('TEST-sub3','Sent',NULL);
        INSERT @r VALUES ('H2 Sent without SentAt rejected', 0, 'accepted');
    END TRY
    BEGIN CATCH
        INSERT @r VALUES ('H2 Sent without SentAt rejected', CASE WHEN ERROR_NUMBER()=547 THEN 1 ELSE 0 END, 'rejected '+CAST(ERROR_NUMBER() AS varchar(10)));
    END CATCH

    ----- M3: SendDate computed ---------------------------------------------------------------------
    BEGIN TRY
        EXEC sp_executesql N'INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status, SendDate) VALUES (''TEST-sub3'',''Claimed'',''2001-01-01'')';
        INSERT @r VALUES ('M3a explicit SendDate impossible', 0, 'accepted');
    END TRY
    BEGIN CATCH
        INSERT @r VALUES ('M3a explicit SendDate impossible', CASE WHEN ERROR_NUMBER() IN (271,272) THEN 1 ELSE 0 END, 'rejected '+CAST(ERROR_NUMBER() AS varchar(10)));
    END CATCH
    INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status, ClaimedAt) VALUES ('TEST-sub3','Claimed','2030-01-01T20:00:00+00:00');
    INSERT @r SELECT 'M3b claim at 20:00 UTC lands on next Singapore date', CASE WHEN SendDate='2030-01-02' THEN 1 ELSE 0 END,
        'ClaimedAt 2030-01-01 20:00 UTC -> SendDate '+CONVERT(char(10),SendDate)
        FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id='TEST-sub3';
    INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status) VALUES ('TEST-sub3','Claimed');
    INSERT @r SELECT 'M3c default claim = Singapore today (server date shown)', CASE WHEN COUNT(*)=1 THEN 1 ELSE 0 END,
        'SendDate='+CONVERT(char(10),@today)+' ServerDate='+CONVERT(char(10),CAST(SYSDATETIME() AS date))+' ServerOffset='+DATENAME(TZoffset,SYSDATETIMEOFFSET())
        FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id='TEST-sub3' AND SendDate=@today;

    ----- refresh job ---------------------------------------------------------------------------------
    INSERT Integration.Tableau_RefreshJob (DataSource_id) VALUES ('TEST-pub');
    INSERT @r SELECT '7 refresh job default Submitted + Singapore date', CASE WHEN Status='Submitted' AND SubmittedDate=@today THEN 1 ELSE 0 END, Status
        FROM Integration.Tableau_RefreshJob WHERE DataSource_id='TEST-pub';

    ----- M4: key hash column + index -----------------------------------------------------------------
    UPDATE Integration.Tableau_Subscription SET SubscriptionKeyHash = HASHBYTES('SHA2_256','wb|target|owner|subject') WHERE Subscription_id='TEST-sub';
    INSERT @r SELECT 'M4 SubscriptionKeyHash stored, non-unique index exists', CASE WHEN @@ROWCOUNT=1 AND EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_Tableau_Subscription_KeyHash' AND is_unique=0) THEN 1 ELSE 0 END, '';
END TRY
BEGIN CATCH
    INSERT @r VALUES ('UNEXPECTED ERROR', 0, CONCAT(ERROR_NUMBER(), ' line ', ERROR_LINE(), ': ', ERROR_MESSAGE()));
END CATCH

-- cleanup (end) -------------------------------------------------------------------------------
DELETE FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id LIKE 'TEST-%';
DELETE FROM Integration.Tableau_Subscription WHERE Subscription_id LIKE 'TEST-%';
DELETE FROM Integration.Tableau_RefreshJob WHERE DataSource_id LIKE 'TEST-%';
DELETE FROM Integration.Tableau_WorkbookDataSource WHERE Workbook_id LIKE 'TEST-%';
DELETE FROM Integration.Tableau_DataSourceTable WHERE DataSource_id LIKE 'TEST-%';
DELETE FROM Integration.Tableau_DataSourceOverride WHERE DataSource_id LIKE 'TEST-%';
DELETE FROM Integration.Tableau_DataSource WHERE DataSource_id LIKE 'TEST-%';
DELETE FROM Integration.Tableau_PipelineTable WHERE SchemaName = 'TESTSCH';
DELETE FROM Integration.Tableau_StaticTable WHERE SchemaName = 'TESTSCH';

----- checks on the real data -----------------------------------------------------------------------
INSERT @r SELECT 'V1 registry pipelines exist in ETL_Master_Log', CASE WHEN COUNT(*)=0 THEN 1 ELSE 0 END,
    ISNULL(STRING_AGG(PipelineName,', '),'all present')
  FROM (SELECT DISTINCT PipelineName FROM Integration.Tableau_PipelineTable p
        WHERE NOT EXISTS (SELECT 1 FROM Integration.ETL_Master_Log l
                          WHERE l.PipelineName COLLATE Latin1_General_BIN2 = p.PipelineName COLLATE Latin1_General_BIN2)) m;
INSERT @r SELECT 'M2c every registry Schema.Table exists in the DWH', CASE WHEN COUNT(*)=0 THEN 1 ELSE 0 END,
    ISNULL(STRING_AGG(CONCAT(SchemaName,'.',TableName),', '),'all exist')
  FROM Integration.Tableau_PipelineTable
  WHERE OBJECT_ID(QUOTENAME(SchemaName)+'.'+QUOTENAME(TableName)) IS NULL;
INSERT @r SELECT 'T all objects tagged created_by=T1_mapping_schema', CASE WHEN COUNT(*)=13 THEN 1 ELSE 0 END, CONCAT(COUNT(*),' of 13 tagged')
  FROM sys.extended_properties WHERE class=1 AND minor_id=0 AND name='created_by' AND CAST(value AS nvarchar(128))='T1_mapping_schema';
INSERT @r SELECT 'C no test rows left', CASE WHEN
   (SELECT COUNT(*) FROM Integration.Tableau_DataSource WHERE DataSource_id LIKE 'TEST-%')
 + (SELECT COUNT(*) FROM Integration.Tableau_WorkbookDataSource WHERE Workbook_id LIKE 'TEST-%')
 + (SELECT COUNT(*) FROM Integration.Tableau_Subscription WHERE Subscription_id LIKE 'TEST-%')
 + (SELECT COUNT(*) FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id LIKE 'TEST-%')
 + (SELECT COUNT(*) FROM Integration.Tableau_PipelineTable WHERE SchemaName='TESTSCH')
 + (SELECT COUNT(*) FROM Integration.Tableau_RefreshJob WHERE DataSource_id LIKE 'TEST-%') = 0 THEN 1 ELSE 0 END, '';

SELECT Test, CASE Passed WHEN 1 THEN 'PASS' ELSE 'FAIL' END AS Result, Detail FROM @r;
GO
SET NOEXEC OFF;
GO
