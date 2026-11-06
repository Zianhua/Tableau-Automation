# Database changes

Service account: [DOMAIN\svc_tableau]. The objects are created from the DBJR document (ITSC_384043_TableauEnhancement_ML), no longer from files in this repo; create them before deploying the code. Detail: DB_CHANGES_DETAIL.md.

## Create (tables)
- Integration.Tableau_PipelineTable
- Integration.Tableau_StaticTable
- Integration.Tableau_DataSource
- Integration.Tableau_WorkbookDataSource
- Integration.Tableau_DataSourceTable
- Integration.Tableau_Subscription
- Integration.Tableau_SubscriptionSendLog
- Integration.Tableau_RefreshJob
- Integration.Tableau_Schedule
- Integration.Tableau_SubscriptionBackup
- Integration.Tableau_LineageSource
- Integration.Tableau_ExtractTask
- Integration.Tableau_ExtractTaskBackup

## Create (views)
- Integration.vw_Tableau_DataSourceTableStatus
- Integration.vw_Tableau_DataSourcePipeline
- Integration.vw_Tableau_DataSourceReadiness
- Integration.vw_Tableau_WorkbookReadiness
- Integration.vw_Tableau_ExtractSchedule

## Insert data
- Integration.Tableau_PipelineTable: 33 rows
- Integration.Tableau_StaticTable: 1 row
- Integration.Tableau_LineageSource: 3 rows

## Modify (existing table)
- Integration.ETL_Tableau_ExtractMapping: Active_Flag set to 0 on takeover, set back on restore

## Grant (existing tables)
- Integration.ETL_Master_Log: SELECT
- Integration.Lineage: SELECT
- Integration.DW_NovaLineage: SELECT
- Integration.OAO_Lineage: SELECT
- Integration.ETL_Tableau_ExtractMapping: SELECT, UPDATE

## Grant (new tables)
- Integration.Tableau_PipelineTable: SELECT, INSERT, DELETE
- Integration.Tableau_DataSourceTable: SELECT, INSERT, UPDATE, DELETE
- Integration.Tableau_DataSource: SELECT, INSERT, UPDATE
- Integration.Tableau_WorkbookDataSource: SELECT, INSERT, UPDATE
- Integration.Tableau_Subscription: SELECT, INSERT, UPDATE
- Integration.Tableau_SubscriptionSendLog: SELECT, INSERT, UPDATE
- Integration.Tableau_RefreshJob: SELECT, INSERT, UPDATE
- Integration.Tableau_Schedule: SELECT, INSERT, UPDATE
- Integration.Tableau_SubscriptionBackup: SELECT, INSERT, UPDATE
- Integration.Tableau_ExtractTask: SELECT, INSERT, UPDATE
- Integration.Tableau_ExtractTaskBackup: SELECT, INSERT, UPDATE
- Integration.Tableau_StaticTable: SELECT
- Integration.Tableau_LineageSource: SELECT

## Grant (views)
- SELECT on all five views above

## Grant (database level)
- VIEW DEFINITION
- sp_getapplock (public role by default)

## Rights for whoever applies the SQL
- CREATE TABLE, CREATE VIEW, ALTER on schema Integration
