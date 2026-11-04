# TableauSubscriptionAutomation: project instructions

## Goal
Tableau "when data refreshes" subscriptions do not work for workbooks with several extracts. Replace them with a script that, for each report:
1. refreshes each underlying data source once all its ETLs have succeeded,
2. waits for the refresh jobs and records the outcome,
3. exports the report as PDF and emails it to the report's recipients once every data source is fresh today,
4. sends each report at most once per day.

The report to data source to ETL mapping must be automated, not maintained by hand. See [docs/PLAN.md](docs/PLAN.md).

## Roles
- **The PM session** (the session that owns this file) keeps [STATUS.md](STATUS.md), [BACKLOG.md](BACKLOG.md) and [DECISIONS.md](DECISIONS.md) accurate, splits work into tasks, and reviews what worker sessions report back. It does not write feature code unless the user asks.
- **Worker sessions** pick one backlog item, do only that, and update the backlog and status when done.

## Rules for every session
1. Read STATUS.md, then BACKLOG.md, before doing anything. Do not start work that is not in the backlog. Propose it to the PM or user first.
2. Mark your item `in-progress` with your session's purpose before you start, and `done` or `blocked` when you finish. Add a one-line handoff note to STATUS.md.
3. Record any decision that changes scope, schema or behaviour in DECISIONS.md. Do not re-litigate decisions already there.
4. Out of scope until the user says otherwise: the fixes for the edge cases in [docs/edge_cases.md](docs/edge_cases.md). Documented only.
5. Never commit secrets. Credentials (Tableau PAT, DB, SMTP) come from `.env` or the environment and are gitignored.
6. Never write to production tables, Tableau Server, or send real emails without the user's explicit go-ahead. Build with dry-run flags and test against UAT.
7. Git: commit messages are written as the user. No mention of Claude, AI or generation tools, and no co-author lines.

## Environments
| Env | DWH (SQL Server) | Tableau Server |
|---|---|---|
| Dev (use this for development and testing for now) | `<DEV_DWH_SERVER>`, database `CQGlobalSGDW` | UAT: http://tableau-uat.example.com:8000/ |
| UAT | `<UAT_DWH_SERVER>` | UAT: http://tableau-uat.example.com:8000/ |

The Tableau UAT server is the only Tableau server used so far. Dev DWH data feeds it for testing.

## How to inspect the DWH (read-only)
- Use the user's Windows credentials (Integrated Security). No passwords. Connect with `Server=...;Database=CQGlobalSGDW;Integrated Security=True;TrustServerCertificate=True`.
- `sqlcmd` is not installed. Use PowerShell with `System.Data.SqlClient` (works), or Python with pyodbc.
- Run SELECT-only queries. Never run INSERT, UPDATE, DELETE, DDL or EXEC of procedures that write, against any DWH, without the user's explicit go-ahead.
- Useful sources: `Integration.ETL_Master_Log`, `Integration.ETL_Tableau_ExtractMapping`, `Integration.Lineage` / `DW_NovaLineage` / `OAO_Lineage`, `msdb.dbo.sysjobs` and `sysjobsteps`, `SSISDB`, `sys.sql_expression_dependencies`.

## Reference material
- Existing refresh script (ETL check, then extract refresh): `<PROJECTS_DIR>\ITSC_0001_TableauServer_ExtractAutomation\python\source_reference\main.py`. A deployed copy with schedulers and batch files is under `ITSC_0002_TableauAutomationEnhancement\tableau-extract-and-maintenance`.
- Existing tables: `Integration.ETL_Master_Log`, `Integration.ETL_Tableau_ExtractMapping`, `Integration.Tableau_ExtractRefreshLog`.
- Known defect in the existing script: it refreshes a data source as soon as any one of its ETLs succeeds, so multi-ETL data sources get partial data. It also logs the refresh when the job is submitted, not when it finishes.
- Deployment documents: [docs/DB_CHANGES.md](docs/DB_CHANGES.md) (every database change in order, permissions, dev-only test changes) and [docs/DEPLOYMENT_PLAN.md](docs/DEPLOYMENT_PLAN.md) (step-by-step deployment to the server that hosts the scripts, rollout, monitoring, what is untested). Operating guide: [docs/RUNBOOK.md](docs/RUNBOOK.md).
- Diagram (original sketch, partly superseded): https://www.figma.com/board/BOARD_ID/Tableau-ETL-gated-refresh-and-report-delivery

## Stack and conventions
Python, tableauserverclient, pyodbc, SQL Server (default dialect), Windows Task Scheduler every 15 minutes, packaged as an exe. Prefer boring, proven solutions. No abstractions or error handling for cases that cannot happen.

## Writing for the user
Do not refer to work by internal tags (T2, R3b, R5, F-1, D-028...) in replies or docs meant for the user. Use plain names first ("the daily mapping sync"); a tag may follow in brackets for file lookup only.
