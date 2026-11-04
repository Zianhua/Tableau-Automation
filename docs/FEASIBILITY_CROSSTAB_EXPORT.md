# Feasibility: crosstab (CSV / Excel) mail, maintained in the database

Study only. No code changed, nothing written to Tableau, no mail sent, no database writes, no Tableau sign-in (another test holds the token). Sources: the installed library (tableauserverclient 0.42), this repo, public Tableau documentation.

Scope decided by you: summary / crosstab data of a view is enough (not the full underlying data), and the list of who gets what is kept by hand in a database table. No Tableau-side setting and no check against Tableau subscriptions.

## Short answer

Feasible, medium size. A new hand-kept table says which view gets a CSV or Excel file, to whom, and on which days. A new small sender script reuses the existing readiness check, mail builder, mail transport and alerting. It needs its own send log (the existing one is tied to Tableau subscriptions). Go with conditions (end of document).

## 1. Background facts

| Question | Answer |
|---|---|
| Can a Tableau subscription attach Excel / CSV? | No. Only image and PDF, in the user interface and in REST. So the data file has to be sent by our script, independent of any Tableau subscription. |
| Export calls (library source read) | `views.populate_csv` (REST 2.7) returns the view's data as CSV. `views.populate_excel` (REST 3.8) returns the crosstab as .xlsx. Server version was not read now; earlier sessions recorded REST 3.25, so both exist. |
| Level | One view per call. There is no workbook-level CSV or Excel. |
| Options | Column filters (`vf`), parameters, `max_age` (0 to 240 minutes, or -1 for cache), language. No row limit and no paging. |
| Summary data versus full underlying data | The export gives the numbers as the view shows them (aggregated, same as Download Crosstab). Every raw row behind the marks is not available through REST. If raw rows are ever needed, that is a warehouse query, a separate project. The mail and the table must say "summary data". |

## 2. Table shape

One new table, filled by hand, for example `Integration.Tableau_DataExport`:

| Column | Meaning |
|---|---|
| Export_id | Identity key, also the key used in the send log |
| Workbook_id | Tableau workbook id (the existing mapping and readiness are keyed by this) |
| ViewId | Tableau view id (the sheet or dashboard to export) |
| ExportFormat | `csv` or `xlsx` |
| Recipients | Semicolon list of addresses (kept in the table, unlike the subscription path where addresses are never stored; see risks) |
| Subject / Message | Optional text for the mail |
| ScheduleRule | `Daily`, `Weekdays`, or a weekday list plus an optional month-day, evaluated in Singapore time. Keep to a small fixed set; do not read Tableau schedules |
| ViewFilters | Optional `name=value` pairs passed as `vf` filters, so the file matches what the recipient expects |
| Active_Flag | On/off, so a row can be paused without deleting history |
| Note / Owner | Who asked for it and who approved sensitive data |

One row means one file to one recipient list. Several formats or views are several rows (a mail per row, simple and easy to reason about).

Second table, `Integration.Tableau_DataExportSendLog`, a copy of the existing send log with `Export_id` as key: Claimed / Sent / Failed / TimedOut / Released, unique per Export_id and Singapore day, delete protection and the claim-first rule as today. The existing log cannot be reused because its key points at Tableau subscriptions.

## 3. How it reuses what exists

| Part | Reuse |
|---|---|
| Readiness | The existing readiness check works per workbook id (mapping is Ready, every required ETL pipeline succeeded today, extract refreshed after the last load). The new script calls the same gate with the row's Workbook_id. Catch: the mapping only holds workbooks the daily sync covers (subscribed, more than one data source). A workbook outside that has no mapping, so the new script refuses it (fail closed). Either enter only covered workbooks, or widen the sync scope. |
| Refresh | The extract refresh script (t4) decides what to refresh from subscriptions. A data-export row must be added to its target query, otherwise a workbook with no sendable subscription would never be refreshed. This is a real change, see the file list. |
| Schedule | Not read from Tableau. A small function turns ScheduleRule into "due today" (Singapore date, catch-up days as for subscriptions if wanted). |
| Mail build | `subscription_mail.build_message` gets an attachments list. A data-only mail needs no image or PDF part. |
| Mail transport, allow-list, size check, circuit breaker, alerts, kill switch, PAT lock, export pacing | Reused as they are. The size check already covers the whole message. |
| Takeover / backup | Not involved: there is no Tableau subscription to pause. This removes the whole takeover-day problem. |

## 4. Export step

For each due row, after the gate is green: sign in (shared PAT lock), `views.get_by_id(ViewId)`, build `CSVRequestOptions` or `ExcelRequestOptions` with `max_age=1` and the row's filters, call `populate_csv` or `populate_excel`, join the bytes, check size and (for CSV) the line count, attach as `<view name>.csv/.xlsx`. Exports are serial with the existing pause between them.

## 5. Changes per file

| File | Change | Size |
|---|---|---|
| New SQL file (+ rollback) | The two tables, constraints, send-log guard trigger like the existing one; applied on dev first, DBA for production | S |
| `scripts/subscription_mail.py` | `export_data(server, view_id, fmt, filters)`; `build_message` accepts attachments and a no-image/no-PDF mail | S to M |
| New `scripts/t8_data_export.py` (name free) | Load due rows, gate, claim-first, export, build, send, log, alerts, dry run default, summary line, exit codes. Most of it is the shape of `t6_send.py` and calls its functions | M |
| `scripts/t6_send.py` | Only the import side: make the send-and-claim helpers importable instead of copied (no behaviour change) | S |
| `scripts/t4_refresh.py` | Include extracts behind active data-export rows in the refresh set | S to M |
| `scripts/ops.py`, daily summary, scheduler registration | Counters and a scheduled task after t4/t6 | S |
| Checks file (new) | Fake export, due rule, size cap, readiness refusal, claim-once, local SMTP | M |
| Docs (RUNBOOK, LIMITATIONS, DB_CHANGES) | Table maintenance steps, "summary data" wording, release SQL | S |

Overall: M (a few days of build and review). The T2 sync and the takeover scripts are untouched.

## 6. Risks

| Risk | Detail and handling |
|---|---|
| Row limits / huge views | REST has no row parameter; Tableau may time out or return a very large file for a big view. Cap the downloaded size and the CSV line count; over the cap: no send, alert. Test on a large UAT view once the token is free. |
| Mail size | Gmail 20 MB, 25 MB elsewhere. CSV is uncompressed; xlsx is compressed. Prefer xlsx for large views; a zipped CSV is an option if needed. Existing oversize handling applies. |
| Sensitive data by email | A data file is easier to forward and edit than a PDF. The export runs as the script's Tableau account, so row-level security (for example the group filter in the two NOVA Multi-Asset reports) is not applied per recipient. Keep those two out. Recipient addresses sit in a table (readable by anyone with table access); restrict access, and record an approver in the Note column. The existing allow-list applies to production. |
| Filters and parameters | The view is exported with its default state plus the row's own filters. Subscription custom views are not visible, so match the report by setting ViewFilters. |
| Dashboards | A dashboard may not export as a data table like a worksheet; behaviour not verified. Use a worksheet view id, one row per sheet. |
| Fresh data | `max_age=1`, and the gate requires the extract refreshed after the load, as for PDFs. |
| Hand-kept table drift | A deleted or republished view makes the call fail (404): alert, no send. A workbook outside the mapping scope is refused. |
| Load on Tableau | Same pacing as exports today; one extra call per row. |

## 7. Questions for you

1. Who maintains the table and who approves sensitive data? (Recipients are stored in it.)
2. Schedule: is "daily / listed weekdays / month day" enough?
3. CSV, xlsx, or both? Recommendation: xlsx default.
4. Should a row be allowed on a workbook outside the current mapping scope (then the daily sync scope must grow), or only on covered workbooks?
5. Plain mail with the file only, or should the mail also carry the report image/PDF? (The second needs a Tableau subscription again, so recommendation: file only.)

## 8. Recommendation

Go with conditions.

Conditions: summary data only, worded that way; production keeps the recipient allow-list; size and line-count caps with an alert; no workbook with per-viewer filtering; one read-only UAT probe of both export calls (small view, large view, dashboard) before the build.

Build plan:

1. Probe on UAT when the token is free: CSV and Excel export of a small view, a large view and a dashboard; note size, time, behaviour with a filter. Fix the caps from this.
2. Settle the questions above and the table columns.
3. Write and apply the SQL on dev (tables, guard, rollback).
4. Add `export_data` and the attachment mail layout; unit tests with a fake server.
5. Write the new sender script, reusing the gate, claim-first log and mail path; dry run by default.
6. Add data-export rows to the refresh script's target set; extend the checks and run all of them.
7. Real test with your go-ahead: one UAT row, one recipient, then one production report with its approval.

Sources: [Tableau help, subscribe](https://help.tableau.com/current/server/en-us/subscribe_user.htm), [REST API subscriptions](https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api_ref_subscriptions.htm), installed tableauserverclient 0.42 source.
