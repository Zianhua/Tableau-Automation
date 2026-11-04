# Plan

## Problem
Subscriptions on workbooks with several extracts do not fire. The existing script only maps ETL to data source, so it cannot tell when all data behind a report is ready, and it refreshes multi-ETL data sources too early.

## Target flow (every 15 minutes)
1. Load the mapping: report, its data sources, each data source's ETLs, and the report's recipients.
2. For each data source: if all its ETLs succeeded today and it has no successful refresh today, trigger the extract refresh.
3. Poll the refresh job to completion. Log success or failure. A failed job is retried on the next run.
4. For each report: if every data source has a successful refresh today and the report has not been sent today, export the PDF, send it by SMTP, and log the send.

A data source with no ETL mapping rows counts as ready only if it is explicitly marked "no readiness check" (for example, an external DB).

## Data model (proposed, to be confirmed in task T1)
- Report mapping: report, data source (one row per pair).
- Data source to ETL mapping: extends or replaces `ETL_Tableau_ExtractMapping`, with an explicit "no readiness check" flag.
- Refresh log: job id, status, start and end time. Extends `Tableau_ExtractRefreshLog`.
- Report send log: report, send time, status. Used to enforce one send per day.
- Recipients: read from the report's existing Tableau subscriptions, not stored separately.

## Mapping automation
- Report to data sources: from Tableau Server Client, which lists a workbook's connections. The Metadata API (GraphQL) gives upstream tables.
- Data source to ETL: Tableau only knows tables. A table to ETL link is needed, either a one-time table or derived from SQL Agent or pipeline metadata. Open question Q1 in DECISIONS.md.
- Recipients: from existing subscriptions.

## Phases
- **Phase 0, discovery:** answer the open questions, inventory the affected reports.
- **Phase 1, mapping:** schema plus the automated sync job.
- **Phase 2, refresh with wait:** fix the partial-refresh defect, add job polling and logging.
- **Phase 3, delivery:** PDF export, SMTP send, one send per day.
- **Phase 4, UAT, then prod:** run on UAT with dry-run first, then prod rollout and scheduler.

## Out of scope for now
Local Excel sources and external-DB sources: see [edge_cases.md](edge_cases.md).
