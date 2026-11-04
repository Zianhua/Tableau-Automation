# Tableau Subscription Automation: Edge Cases

Status: documented only, none of these are being fixed yet.

## Background

Tableau "when data refreshes" subscriptions do not work for workbooks that use several extracts. The existing refresh script (ITSC_0001) only maps ETL to data source, so it cannot tell when every data source behind a report is ready. The plan is to automate the report to data source to ETL mapping, then have the script refresh the data sources, wait for the jobs, and export and send the report itself.

## Edge case 1: report uses a local Excel file plus DWH tables

Difficulty: easy to moderate, depending on where the file lives.

- The file is on a UNC share that the Tableau Server run-as account can read. It behaves like any other source and the extract refresh picks it up. Easy.
- The file is on a user's PC. The server cannot reach it. It needs Tableau Bridge or a manual republish, which is hard to automate.
- Recommended fix: move the file to a share, or load it into a DWH table through SSIS.
- Readiness: skip the check, or compare the file's modified time with today's date.

## Edge case 2: report uses the DWH plus an external system DB

Difficulty: easy in code, moderate operationally. No readiness check is needed for the external DB.

- Code: a data source with no ETL mapping rows counts as ready. The mapping must say so explicitly, so a missing mapping is not mistaken for "no check needed".
- Server: the DB driver must be installed on Tableau Server (see ITSC_0002_TableauDriverInstall), the firewall must allow access, and credentials must be stored on the published data source.
- Risk: with no readiness check, the external data may be mid-load when the refresh runs. This is accepted.
