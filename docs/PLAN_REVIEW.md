# Plan review

Reviewer: read-only plan review, 2026-10-21. Nothing was run against Tableau or any database. Evidence comes from the project docs, `docs/d1_inventory.json` (analysed offline), `scripts/d1_inventory.py`, `scripts/sql/T1_mapping_schema.sql`, the existing refresh script (`ITSC_0001.../main.py`, identical to the deployed copy's `source_reference/main.py`) and the deployed scheduler XML and batch file under `ITSC_0002.../tableau-extract-and-maintenance`. `.env` was not opened.

Numbers below were computed from the inventory JSON. "Subscribed" means one of the 38 affected workbooks with at least one subscription (54 subscriptions).

## 0. Headline

1. The inventory contradicts the project premise. Of the 88 data sources in the 38 subscribed workbooks, 86 are embedded live connections (`hasExtracts = false`), 2 are published (extract status not captured) and only 2 are extracts, both in one workbook ("Shu Shuang custom SQL data") that fail-closed blocks anyway. Of the 16 workbooks that pass D-006, none has an extract. For them the "refresh and wait" half of the project (T4, T5) has nothing to do. What they need is ETL gating, PDF export and a send. Across all 266 affected workbooks, only 9 have two or more extract data sources and only 23 have one or more. Either the premise "several extracts" is wrong for most of this population (the subscriptions are probably time-based and fire before the ETLs finish), or the `hasExtracts` flag is wrong (19 of the 88 are named "(Extract) ..."). This must be checked before T4 and T5 are built.
2. The plan is silent on what happens to the 54 existing subscriptions, which is the single most likely cause of double emails and of lost behaviour.
3. D-006 fail-closed leaves 22 of 38 workbooks (29 of 54 subscriptions) unserved. TIMELINE.md says 33 and "only 5 clean". Those figures predate D-010 and are stale (see section 5).

## 1. Requirements

Functional (F) and operational (O). "Source" is the doc or decision. "implied" means no document states it but the design cannot work without it.

| # | Requirement | Source |
|---|---|---|
| F1 | Cover every workbook with more than one data source (inventory generated from Tableau, not hand-kept) | D-007 |
| F2 | Reports with one data source keep ordinary Tableau subscriptions | CLAUDE.md goal, PM prompt |
| F3 | Mapping workbook, data source, upstream table, ETL pipeline is automated; table to pipeline link is stored in the DWH | D-002, D-005, PLAN.md |
| F4 | Refresh a data source only when all its ETLs succeeded today | CLAUDE.md goal 1, T4 |
| F5 | Wait for the refresh job, record success or failure when it finishes, retry on next run | goal 2, T5 |
| F6 | Export the report as PDF once every data source is fresh today | goal 3, T6 |
| F7 | Email the PDF to the report's existing subscription recipients through the Tableau-configured SMTP (SES) | goal 3, Q2, Q5, T7 |
| F8 | At most one send per report per day, enforced in the DB | goal 4, T7, T1 filtered unique index |
| F9 | A data source with unmapped tables fails closed: no send, gap logged | D-006 |
| F10 | Explicit "no readiness check" override per data source | PLAN.md, T1 |
| F11 | Dry-run flag | scope cut, CLAUDE.md rule 6 |
| F12 | All recipients internal | D-008 (stated as a fact, no enforcement stated) |
| F13 | Log a refresh when it finishes, not when it is submitted | CLAUDE.md known defect, T1 table 5 |
| F14 | Mapping stays current as workbooks or pipelines change | D-002, D-005 step 4 (implied for T2 to run repeatedly) |
| F15 | A live connection needs no refresh, only an ETL readiness check | implied (d1_unresolved.md says so) |
| F16 | The sent report must have the same content as the subscription it replaces (view vs workbook, filters, subject, message, format) | implied |
| F17 | A workbook that gains or loses a data source moves between the old and new flows without a double or missing email | implied (seed b) |
| O1 | Runs every 15 minutes via Task Scheduler on the Tableau server host | D-004, PM prompt (host: stated, not proven) |
| O2 | Dev DWH for development; DWH server is an environment setting that must point at the DWH the Tableau data sources actually read before go-live | D-010, D-011 |
| O3 | No writes to prod, no real email, no DDL without the user's go-ahead | CLAUDE.md rule 6 |
| O4 | Secrets only in `.env` or environment, never committed or printed | CLAUDE.md rule 5 |
| O5 | The existing refresh job keeps working during development (T1 is additive) | T1 header |
| O6 | Someone owns the job and learns when a report was not sent | implied, not in any doc |
| O7 | Logs are kept somewhere that survives and can be read by ops | implied (existing job logs to `F:\datateam_implementation\log`) |
| O8 | Credential rotation (PAT, SES, DB) is possible without code change | implied |
| O9 | A run finishes inside the scheduler limits (see GAP 8) | implied; existing task: 08:30 to 16:30, every 15 min, 1 h limit, IgnoreNew |
| O10 | Rollback: switch the new flow off and the old subscription path resumes | implied |

## 2. Assumptions

| # | Assumption | From | Status | Evidence or what is missing |
|---|---|---|---|---|
| A1 | Affected workbooks need a refresh step | CLAUDE.md goal, T4, T5 | GUESSED, and contradicted | 86 of 88 data sources in subscribed workbooks are `hasExtracts=false`. Only 1 subscribed workbook has extracts. |
| A2 | `hasExtracts` from the Metadata API is reliable for embedded data sources | implied by D1 and d1_unresolved.md ("25 of 28 live") | GUESSED | 19 of 88 names start "(Extract)" yet all but 2 report false. Published data sources have no extract flag captured at all. Cross-check with extract refresh tasks (`server.tasks`). |
| A3 | The 54 subscriptions are "when data refreshes" subscriptions | CLAUDE.md goal, D-001 | GUESSED | Inventory stores only `schedule_id` (9 distinct ids, the biggest `0a4eb615...` used by 22). No schedule type or frequency was read. |
| A4 | Subscriptions do not fire because of multiple extracts | D-001 "confirmed by the user" | GUESSED for this population | User-confirmed in principle, but not shown for any of the 16 deliverable workbooks, which have no extracts. |
| A5 | Existing script refreshes a data source after any one of its ETLs succeeds | CLAUDE.md | VERIFIED | `main.py`: loop over mapping rows, first row whose ETL succeeded today triggers `datasources.refresh` and writes the log row, later rows then skip with "already refreshed today". Logged at submit time (`log_refresh_to_db` straight after `refresh`). |
| A6 | A data source is a published data source (so `datasources.refresh` works) | main.py, T1 `DataSource_id` | VERIFIED false for this population | 86 of 88 are embedded. `main.py` only calls `server.datasources.get_by_id` and `datasources.refresh`. An embedded extract is refreshed with the workbook refresh call (`PUT .../workbooks/{id}/refresh`, TSC `workbooks.refresh`), a different object. Not mentioned in PLAN.md or T1. |
| A7 | Embedded data sources can be keyed by `DataSource_id` | T1 | VERIFIED false | `d1_inventory.py` queries `embeddedDatasources{ id name hasExtracts }` but the output drops `id`. Embedded names repeat across workbooks (`vw_DataSource_DastOpenPosition` in about 20 workbooks) and inside one (`... (2)`). The Metadata API `id` is not a REST LUID. |
| A8 | Recipients can be read from subscriptions | PLAN.md | PARTLY VERIFIED | 54 of 54 have an email, none missing. A subscription is owned by a user; the "recipient" is that user's email. Owners are `<user-1>` (27 subs, hosy), `<tableau-admin-user>` (15, <recipient-user>), `<user-6>` (8, wilfredchongch), `<user-3>` (4, limjk). Whether `<tableau-admin-user>` / `ExplorerN` are shared or service accounts is unknown. |
| A9 | One send per workbook per day is what subscribers get today | goal 4 | VERIFIED false | 12 of 38 workbooks have one recipient on 2 or 3 different schedules (e.g. Gold & Silver Story: three schedules). "Once per day" removes those extra sends. 13 workbooks have 2+ subscriptions. |
| A10 | A subscription is on the whole workbook | PLAN.md ("export the report as PDF") | VERIFIED false | 15 of 54 are on a single view, across 12 workbooks (e.g. "Stocks Monthly Newsletter", "PFK Dashboard"). A workbook-level PDF is a different document. |
| A11 | Subject and message can be dropped | not discussed | VERIFIED false | 40 of 54 subjects differ from the workbook name (custom subjects such as "AE Quarterly Sales Performance Report - ZCK"). The inventory does not read message, `attach_pdf`, `attach_image`, page orientation or size. |
| A12 | Metadata API enabled | D1 | VERIFIED | `d1_inventory.md`: "Metadata API enabled: True". STATUS.md still says "answers 403, treated as disabled" and lists it as an open item. |
| A13 | Metadata upstream tables are base tables | T1 comment "views already resolved to base tables" | VERIFIED false | Inventory tables are views: 7 of the 17 distinct tables used by subscribed workbooks are `Tableau.vw_*` / `dbo.vw_*` views. View to base-table resolution was done by hand in the Q1 dev check (`sys.sql_expression_dependencies`), and no backlog task automates it. |
| A14 | A table with no database name is `CQGlobalSGDW` | D-010 | GUESSED (user-stated) | The same view appears with `database = None` in some workbooks and `cqglobalsgdw` in others, so it is plausible. 99 of 726 table rows have no database. Not verified against the connection. |
| A15 | Database name case does not matter | implied | GUESSED | Inventory has `cqglobalsgdw` lowercase while CLAUDE.md uses `CQGlobalSGDW`. Registry is keyed on schema and table only, so this is fine in SQL Server; it would matter if the DWH is ever case-sensitive. |
| A16 | Only 3 pipelines exist | Q1 sanity check | PARTLY VERIFIED | Dev `ETL_Master_Log` shows 3. Prod and UAT DWH not checked. Not verified that these 3 cover everything loading `Dimension.*`, `Fact.*`, `dbo.vw_Application_Remark_K2`. |
| A17 | A pipeline's "success today" means its tables are fresh | PLAN.md step 2 | GUESSED | `main.py` reads the single latest `ETL_Master_Log` row with `EndTime` today. Job-level success does not prove each table loaded (lineage tables record per table). |
| A18 | The pipeline to table link is stable enough to hand-confirm once | D-005 | GUESSED | Item 4 of the Q1 approach ("deploying a pipeline includes registering tables") is a process promise, not enforced anywhere. |
| A19 | The script runs on the Tableau server host | CLAUDE.md, PM prompt | GUESSED | The existing deployment is at `F:\datateam_implementation`, run as `<UAT_DOMAIN>\<service-account>` (UAT) and `<PROD_DOMAIN>\<service-account>` (prod). Nothing in the docs shows that is the Tableau host. |
| A20 | SES accepts mail from the script | Q5 | GUESSED | Tableau shows port 25, ssl false. Credential, sandbox status and verified sender not checked. Prod not checked. |
| A21 | Mail size is fine | not discussed | GUESSED | PDFs of dashboards on `vw_DataSource_PNL` can be large. SES has a per-message size cap. |
| A22 | One PAT can serve both the existing job and the new script | implied | GUESSED | A PAT is meant for a single active session; two jobs signing in with the same PAT can invalidate each other (verify with Tableau docs). |
| A23 | The PAT user can see everything needed | implied | PARTLY VERIFIED | It lists all 126 subscriptions and 668 workbooks, so it is at least a site admin. |
| A24 | Tableau Server Client 0.38 / REST 3.25 can export a workbook PDF and cannot send a subscription now | Q6 | GUESSED | Q6 states the second half as unconfirmed. Workbook-level PDF (`workbooks.populate_pdf`) is not confirmed for 3.25. View PDF is long-standing. |
| A25 | Dev data stand-in is good enough | D-011 | VERIFIED as accepted | Dev DWH does not reflect what UAT data sources read (71 of 78 published sources read prod). Accepted by the user. Side effect: Tableau LUIDs in dev mapping tables are UAT ids; prod Tableau has different ids. |
| A26 | "Today" is a single calendar day | `main.py` `CAST(GETDATE() AS DATE)` | GUESSED | Three clocks are involved: DWH server (`GETDATE`, `SYSDATETIME`), the script host, and the Singapore business day. Not stated which one governs. |
| A27 | The scheduler window covers the ETL finish times | D-004 | GUESSED | Deployed task starts 08:30 and repeats every 15 minutes for 8 hours (last run about 16:30), `ExecutionTimeLimit` 1 h, `MultipleInstancesPolicy` IgnoreNew. An ETL finishing at 17:00 gets no send that day. |
| A28 | Same underlying source connected twice counts once | not decided | GUESSED | See GAP 3. |
| A29 | Recipients are internal | D-008 | GUESSED (user-stated) | All 4 recipient addresses in the inventory are `@example.com`. That is true today; nothing in code enforces it. |
| A30 | Old Tableau subscriptions can be left alone | TIMELINE.md "open" | UNDECIDED | See GAP 2. |

## 3. Verified vs guessed

Verified from evidence: existing script behaviour (A5, A6), the 54/38/266 counts, 4 recipients and no missing emails, subscription mix (39 workbook / 15 view), subject customisation (40 of 54), multi-schedule subscriptions (12 workbooks), Metadata API enabled, views not base tables, scheduler window and limits.

Guessed or unproven and load-bearing: A1 to A4 (do these workbooks even have extracts, and what kind of subscriptions do they have), A7 (embedded keys), A17 (job-level readiness), A19 (host), A20 (SES), A22 (PAT sessions), A24 (PDF API), A26 (time zone).

## 4. Open questions for the user

Short; recommendation in brackets.

1. Do the 16 deliverable workbooks have extracts or are they live? If live, drop the refresh step for them and treat T4/T5 as only needed for extract workbooks (1 subscribed workbook today). [Recommend: confirm in Tableau UI for 2 workbooks, then re-scope T4/T5 to a small optional step.]
2. Are the existing subscriptions time-based or "when data refreshes"? [Recommend: read schedule type and frequency for the 9 schedule ids; one read-only call.]
3. Cut-over: when the script takes over a workbook, pause or delete that workbook's subscriptions? [Recommend: pause (suspend) them via REST where supported, otherwise delete after recording a full copy; never leave both live. Script sends per subscription, not per workbook, so each owner keeps his own subject, view and schedule.]
4. Should the new flow send per original subscription (same owner, subject, view, message) or one mail per workbook per day to a recipient union? [Recommend: per original subscription, deduplicated per (subscription, day). It keeps content and view targets, and avoids a union. Twelve workbooks with several schedules then still send several times unless you say otherwise.]
5. What to do with the 22 blocked workbooks? [Recommend: leave their Tableau subscriptions running and tell their owners; only convert workbooks that pass. Revisit after the mapping analyst resolves the 17 no-table sources.]
6. Counting rule: is `X` and `X (2)` one data source? [Recommend: yes when the connection (server, database, table or custom SQL) is identical, which turns the 9 AE Routine Report workbooks and Monthly HNX/KRX Vol into single-source workbooks that stay on Tableau subscriptions. Needs a check that the two connections really are identical.]
7. Which clock defines "today"? [Recommend: Singapore date, applied both to ETL end times and to the send log.]
8. Who owns the job and gets the "not sent" alert? [Recommend: name an owner now; daily summary email to them listing every workbook not sent and why.]
9. Does the new script replace the existing refresh job or run beside it? [Recommend: run beside, with the new flow using its own tables and its own PAT; do not touch the old job until the new flow is proven. Remove a data source from the old mapping only if the new flow also refreshes it.]
10. Is a service account allowed to export PDFs on behalf of subscribers, given that it may see more data than the subscriber (row-level security, user filters)? [Recommend: check the 16 workbooks for user filters or RLS; if present, use impersonation (`ImpersonateUser`) or keep them on Tableau.]
11. Does the script host run on the Tableau server? [Recommend: confirm, because the change-control and resource question follows from it. If not, drop "host" from the design and make SES allowlisting an explicit task.]
12. Who owns the SES credential and is the account out of the sandbox? [Recommend: raise now; it has a multi-day wait.]

## 5. Gaps

Severity: High breaks the goal or causes a wrong email; Med causes rework or ops pain; Low is cosmetic.

| # | Gap | Sev | Why it matters | Concrete change |
|---|---|---|---|---|
| G1 | Premise vs data. 86 of 88 subscribed data sources are live; 0 of 16 deliverable workbooks have an extract; only 9 of 266 affected workbooks have 2+ extracts | High | T4 and T5 (about 14 h likely) may build for a population that is nearly empty. The real fault may be that subscriptions are time-based and fire before ETLs finish. | Add a D1b read-only task: per subscribed workbook, extract flag (cross-checked with `server.tasks` extract refresh tasks), subscription schedule type and frequency. Gate T4/T5 on the result. Re-estimate. |
| G2 | Existing subscription cut-over undecided and unowned | High | Double emails (old and new) or a lost report. 54 subscriptions in scope, 25 of them on the 16 workbooks that can move now. | Add decision D-012 (pause or delete) and task T11: record each subscription's full definition to a backup table, suspend on cut-over, restore on rollback. Per-workbook cut-over flag, off by default. |
| G3 | Subscription attributes lost. Inventory stores 7 fields. Not stored: message, `attach_pdf`, `attach_image`, page orientation and size, custom view / filters. 15 of 54 target a view, 40 of 54 have custom subjects, 12 workbooks have multiple schedules to one recipient | High | A PDF of the wrong object, wrong subject, or fewer mails. Recipients complain on day one. | Extend `d1_inventory.py` and T2 to capture full subscription definitions. Decide one script mail per subscription (Q4). Export the same view (`views.populate_pdf`) for view subscriptions. |
| G4 | D-006 blocks 22 of 38 workbooks (29 of 54 subscriptions). 17 no-table (16 only that, 1 also file), 6 file (5 only that), overlap 1. TIMELINE.md says 33 and "only 5 clean" (pre D-010); PM prompt says 16 clean. The 16 that pass: 15 are `hosy` and 1 is `<recipient-user>`. Blocked recipients: `<recipient-user>` 14, `wilfredchongch` 5, `hosy` 2, `limjk` 2 | High | The deliverable value is 16 workbooks and two recipients' worth. The other two recipients (wilfredchongch, limjk) get nothing from this project. TIMELINE.md "Decision needed" is based on the old number. | Update TIMELINE.md and BACKLOG D1 notes to 16 pass / 22 blocked. Take the user decision on what happens to the blocked 22 (recommendation: leave on Tableau subscriptions). Do not call the project "all reports". |
| G5 | Counting rule (D-007). 18 of 28 unresolved sources are `vw_RetailTeam-DataSource (Retail Team)` and `(2)` in 9 AE Routine Report workbooks, all owned by `<tableau-admin-user>` and sent to `<recipient-user>`. Monthly HNX/KRX Vol has the same name published and embedded | High | If counted once, 10 of the 17 unresolved workbooks are single-source and need no new flow. The "affected" set shrinks from 38 to 28 and the unresolved set from 17 to 7. If counted twice, the registry would have two rows for one view and readiness is trivially the same. | Add D-013 with the rule. Extend the sync to hash the connection (server, database, table or custom SQL) and compare. |
| G6 | Embedded extracts. 86 of 88 are embedded. Existing job refreshes published data sources only. T1 keys `DataSource_id` and puts `(DataSource_id, Schema, Table)` as the primary key of `Tableau_DataSourceTable`, which cannot represent an embedded source. D1 dropped the embedded `id`. | High | The mapping cannot represent most of the population, and the refresh call for embedded extracts is a different REST call (workbook refresh, which refreshes all extracts in the workbook at once). | Change T1: key embedded sources by (Workbook_id, name) and add `Kind` and `HasExtract` columns; make the unit of refresh "workbook" when embedded. Add `workbooks.refresh` to T4. State this in PLAN.md. |
| G7 | PAT identity and data exposure. PDF export runs as the PAT user. A subscription runs as the owner (`<user-1>`, `<user-3>`, `<user-6>`, `<tableau-admin-user>`). Row-level security, user filters and per-user permissions are not considered. PDFs of client and financial data are emailed from a service. | High | A subscriber could receive data they cannot see in Tableau, or a different cut of it. | Add a check task: for each of the 16 workbooks list user filters and RLS (data policies). Decide impersonation (REST `ImpersonateUser` on sign-in needs server admin) or exclusion. Add an allowlist: recipient must match `@example.com` in code (D-008 enforcement). |
| G8 | Scheduler window and limits. Deployed task: 08:30 start, every 15 min for 8 h (last start about 16:30), 1 h execution limit, IgnoreNew. | High | An ETL finishing after 16:30 sends nothing that day (silently). A run that waits for jobs and exports many PDFs can hit the 1 h limit and be killed mid-send. Workbooks are processed one after another; 16 PDF exports plus job waits are plausible at 1 h. | Decide the run window in a decision. Make each run bounded (time budget; exit cleanly before the limit; never wait for a job inside one run, check it on the next). |
| G9 | Idempotency of the send. The unique index only blocks a second row with status `Sent`. Mail is sent before or after the row? PLAN says "send it by SMTP, and log the send" (after). `Status` has no `Sending` state. | High | Mail sent and then DB write fails gives a duplicate on the next run (every 15 min until it succeeds). The reverse (write first) can lose a mail. Two overlapping runs can both mail. | Insert a `Sending` row first (add to the CHECK), unique per (subscription, SendDate) while status in (`Sending`,`Sent`); update to `Sent` after SMTP accepts; on startup mark stale `Sending` rows as `Unknown` and alert rather than resend. Decide at-most-once vs at-least-once explicitly and record it. Also add a file lock or DB application lock (`sp_getapplock`) for overlap. |
| G10 | Silent failure. D-006 fail-closed, a failed PDF, SES rejection and a stuck job only write to a log. | High | Nobody learns a report did not arrive. The old subscriptions fail the same way today, but a replacement must do better because it adds more failure paths. | Add task: daily "not sent by 11:00" summary mail to a named owner, listing each blocked or failed workbook and the reason; non-zero exit code on failures so Task Scheduler history shows it. |
| G11 | "Today" semantics. `main.py` uses `CAST(GETDATE() AS DATE)` on the DWH clock and `EndTime` date. The send log uses `SYSDATETIME()`. Singapore business day, DWH server zone and host zone are not defined. An ETL finishing at 00:10 counts for the next day. A rerun after a failure writes a later row; success is judged from the latest row only. A refresh that ran before a rerun of an ETL is kept as "fresh today". A report sent at 09:00 is not resent when an ETL reruns at 14:00. | Med | Stale or partially stale PDF, or a missed send, in edge cases. | Define in a decision: day = Singapore date, taken from the DWH `ETL_Master_Log.EndTime` converted explicitly. Readiness = for each required pipeline the latest run today is a success, and the refresh `SubmittedAt` is later than the latest ETL `EndTime`. Document that a later ETL rerun after sending is not resent (or add an explicit resend rule). |
| G12 | Pipeline-level readiness is coarse. `ETL_Master_Log` gives job-level success. Lineage tables give per-table success. One pipeline can fail late in a run after most tables loaded. | Med | The registry's target (table to pipeline) is exact, but readiness may be too strict (blocking on an unrelated table) or too loose. Only 3 pipelines; `vw_DataSource_PNL` feeds 21 of 38 workbooks so a single DailyETL failure stops almost all. | Keep pipeline-level (D-009 option decision stands) but record it as a known limit. Add a manual per-day override "release anyway" to the override table so ops can unblock. |
| G13 | View resolution not scheduled. The Metadata API returns views (`Tableau.vw_DataSource_PNL`, etc.), not base tables. 7 of 17 distinct tables are views; 3 views serve most of the 16. The schema comment claims "views already resolved". No backlog item does the resolution; D-005 says the registry holds tables. Views that wrap views, custom SQL (Interns Team) and views changed after the sync make the registry stale. Dev vs prod view definitions may differ (D-011). | Med | Wrong or empty pipeline set for a data source gives a send too early, or never. | Add to T2: resolve views through `sys.sql_expression_dependencies` against the DWH the data actually reads, stored as `Tableau_DataSourceTable` rows with `ViaView`. Re-run on every sync. Registry rows keyed on schema and table are portable; the Tableau ids are not (G17). |
| G14 | Backlog structure vs scope cut. T8 (UAT run) and T9 (exe, scheduler XML, README) remain in the backlog although scope excludes packaging and a test phase. T4 depends only on T1 but needs T2 and T3 data. T6 depends on T5 although a live-only workbook needs no refresh. T7 depends on T10 (SES) though it can use a local server. No task for cut-over, alerting, locking, view resolution, subscription capture, the time window or logging. | Med | Plan does not match what must be built. | See section 8. |
| G15 | Docs contradict each other. STATUS.md: "Phase 0, no code written", "Last updated 2026-10-20", Next lists D1 although done, "Open items" asks for SMTP host (resolved) and Metadata API (403 treated as disabled; D1 says enabled), PAT rotated 10-20 vs 10-21 in PM prompt. DECISIONS Q4 still lists "Metadata API enabled" unconfirmed. BACKLOG D1: "only 5 clean" (before D-010) vs PM prompt "16 of 38 fully resolved". TIMELINE: "33 blocked", "5 clean". D-010 says 19 tables behind 18 workbooks; the subscribed set has 1 distinct table with no database name (`Tableau.vw_DataSource_DastOpenPosition`). CLAUDE.md "packaged as an exe" vs scope cut. | Low | Builders will start from wrong numbers. | Section 8 lists corrections. |
| G16 | Single-data-source workbooks (seed a, b, c). (a) The existing job refreshes after any one ETL, so a single-source workbook fed by several ETLs through one view still gets partial data; its Tableau subscription fires on that refresh. Out of scope per D-007, but the same defect and the same fix. The inventory covers only affected workbooks; the other 72 subscriptions (126 minus 54) on 402 single-source workbooks were not examined. (b) A workbook gaining a second data source: nothing detects it between syncs; the next sync adds it, and its old subscription then also fires. No double-send guard exists. (c) Shared data sources: `vw_DataSource_PNL` is used by 21 of 38 subscribed workbooks and probably by many single-source ones; the old job refreshes published data sources. Changing the readiness rule inside the old job changes behaviour for all of them. | Med | Hidden coupling; surprise double emails on conversion day. | Keep the new flow additive (new tables, new script, own PAT). Do not patch the old job in T4; build the new readiness rule inside the new script. Run the T2 sync daily and diff "workbooks now affected / no longer affected"; on any change, report instead of acting (a workbook moves in only through the cut-over flag from G2). Add a read-only check of the 72 other subscriptions to see how many follow the same partial-refresh pattern, and report that to the user as a separate finding. |
| G17 | Environment differences. Tableau ids (workbook, data source, subscription, schedule) differ between UAT and prod. The existing `ETL_Tableau_ExtractMapping` holds ids keyed to UAT. Prod Tableau has not been inspected (SMTP, workbooks, Metadata API). The dev DWH `DailyETL` has one `ETL_Master_Log` row (2026-10-14), so readiness tests need fresh runs. UAT reads the prod DWH. | Med | Go-live is a re-run of discovery, not a config change; the "environment setting" in D-010/D-011 is only the DWH server. | Declare in a decision that mapping tables are rebuilt per environment by T2, never copied. Add `TableauServerUrl` to the mapping key or use separate schemas. Add a prod discovery task (read-only) before go-live. |
| G18 | Mail path. Port 25, ssl false on Tableau; SES requires STARTTLS and authentication. SES sandbox status unknown. SES message size cap and throttling not assessed. Attachment of PDFs of unknown size. | Med | The first real send may fail on credentials, sandbox or size. | T10 stays first on the critical path (it has the longest external wait). Add a size check: if PDF exceeds a limit, send a link instead (or fail with an alert). |
| G19 | PAT and credentials. PAT is tied to a user and expires (inactivity and absolute limits). Reusing one PAT across the old job, the inventory script and the new script can invalidate sessions. SES credential and DB credentials not covered by rotation. `main.py` calls `input()` on a missing `.env`, which blocks under Task Scheduler. | Med | Intermittent 401 failures nobody can reproduce. | Separate PAT per script. Document PAT owner, expiry and rotation in the ops notes. In the new script, never call `input()`; exit non-zero with a log line. Keep `.env` out of the exe folder permissions for others. |
| G20 | Refresh job handling. Stuck or long jobs; the job queue on the server may delay; a job that "succeeds" with zero rows; failure of one of several data sources leaves a half-refreshed workbook. `Tableau_RefreshJob.Status` has no timeout state. | Med | Report never sent, or sent on stale extracts. | Define a maximum wait; then mark `TimedOut` and alert. A workbook is sendable only when every extract data source has a `Succeeded` job whose `SubmittedAt` is later than the latest required ETL end today. |
| G21 | Logging and change policy. Where logs go is undecided (existing job: `F:\datateam_implementation\log`). Temp PDFs on disk contain client data. Running on the Tableau host adds CPU load at the same time as extracts and exports. Change approval for a new scheduled task on that host is not mentioned. | Low | Unplanned approvals and disk residue. | Write PDFs to a temp folder that is deleted after the send; log subscription id and counts only, never recipient lists in full. Raise the change request early. |
| G22 | Dry-run semantics undefined. Does dry-run trigger refreshes, write `DryRun` rows, export PDFs? `DryRun` is in the status CHECK but not described. | Low | A "dry run" that refreshes real extracts is not dry. | Define: dry-run does no refresh, no mail; it evaluates readiness, writes nothing except a log, and may export PDFs only on request. |
| G23 | T1 schema problems. (1) `Tableau_WorkbookDataSource` PK `(Workbook_id, DataSource_id)` and `Tableau_DataSourceTable` PK on `DataSource_id` cannot hold embedded sources (G6). (2) `vw_Tableau_UnmappedTable` and `vw_Tableau_DataSourcePipeline` use inner or left joins; a data source with no `Tableau_DataSourceTable` rows (all 17 unresolved) has no row in either view, so it looks neither mapped nor unmapped. In a join-based readiness test that shows as "no required pipelines", i.e. ready. This is a fail-open hole that contradicts D-006. (3) `SendDate` default uses `SYSDATETIME()` (DWH clock). (4) `Status` check lacks `Sending`, `TimedOut`. (5) `SendLog` has no `Subscription_id`, recipient, or content hash. (6) `ConfirmedBy` NOT NULL with no default blocks the T3 bulk load. (7) File-based tables (`.csv`, `Tabelle1`) have empty schema and would be inserted as registry gaps. (8) Needs a `Run` table or a lock for overlap. | High | (2) is a correctness defect that sends reports built on unmapped data. | Add a `Tableau_DataSourceStatus` view that starts from the full list of data sources (left join to tables) and returns `NoTables` explicitly; readiness must require status `Mapped`. Add the other columns above before running DDL. |
| G24 | Mapping automation scope. T2 syncs all 266 affected workbooks and 97 tables (TIMELINE: 12 h likely), but only 38 workbooks have subscriptions and the 16 deliverable ones touch about 17 tables, mostly 3 views. 228 workbooks have nobody to send to. | Med | Effort spent on what cannot be used (D-007 says "every workbook"). | Make the sync subscription-driven: a workbook is in scope when it has a subscription and more than one distinct data source. Everything else is recorded as out of scope with a reason. Keeps D-002 (automation) and D-007 (inventory generated from Tableau). |
| G25 | Rollback. Nothing says how to switch off. | Med | If the new flow misbehaves in prod, there is no way to restore the old state quickly. | Per-workbook `Enabled` flag; script does nothing for disabled workbooks; subscriptions backup from G2 restores the old state in one step. |

## 6. Top 10 gaps, ranked

1. G1: the refresh half of the project may apply to almost none of the deliverable workbooks (0 of 16 have extracts). Verify before building T4/T5.
2. G2: no decision on existing Tableau subscriptions; double email on cut-over.
3. G23: schema fail-open. A data source with no table rows does not appear in the unmapped view, so it can look ready. Contradicts D-006.
4. G6: embedded extracts (86 of 88 sources) cannot be represented by the schema or refreshed by the existing call.
5. G3: subscription content fidelity (15 view subscriptions, 40 custom subjects, 12 workbooks with several schedules, no message or format captured).
6. G7: PDF is exported as the PAT user, not the subscriber; row-level security and user filters unchecked; recipients not enforced in code.
7. G9: send idempotency: log written after mail, no claimed state, no lock; duplicate versus lost mail is undecided.
8. G4 and G5: only 16 of 38 workbooks deliver under D-006; counting rule (D-007) is undecided and would remove up to 10 of the 17 unresolved workbooks.
9. G8: scheduler window (08:30 to about 16:30) and 1 h limit; late ETLs and long runs fail silently.
10. G10: no alerting; every failure path is silent.

## 7. Assumptions the user must confirm

1. The 16 passing workbooks use live connections (no extract refresh needed), or give the extract refresh tasks.
2. The existing subscriptions are what you think they are (time-based vs after refresh) and the nine schedules (read-only check).
3. Cut-over: pause or delete the old subscriptions, per workbook, when the new flow takes over.
4. Send per original subscription (keeps owner, subject, view), not one mail per workbook.
5. The blocked 22 stay on Tableau subscriptions.
6. Counting rule: same connection twice counts once.
7. Day = Singapore date; scheduler window may need extending past 16:30.
8. A service account PDF is acceptable in place of each subscriber's own view (or none of the 16 uses user filters).
9. The script host is the Tableau server and may run the job; a named person owns it and receives the daily "not sent" summary.
10. The new job runs beside the old one with its own PAT.
11. SES: credential owner, sandbox status, attachment size limit.
12. `database = None` means `CQGlobalSGDW` (D-010), already stated; nothing else to confirm.

## 8. Proposed changes (not applied)

### BACKLOG.md

- D1: replace "only 5 clean" with "16 pass D-006 (25 subscriptions), 22 blocked (29 subscriptions): 17 no-table, 6 file, 1 both. 86 of 88 data sources in subscribed workbooks are live embedded, 2 extracts in one blocked workbook."
- Add **D1b** (phase 0, read-only): for the 38 workbooks capture extract flag (cross-check with extract refresh tasks), full subscription definitions (message, format, orientation, custom view), schedule type and frequency for the 9 schedule ids, user filters or RLS for the 16 passing workbooks, the embedded data source id, the connection hash for `X` and `X (2)`. Depends on none. Gate T4/T5 on it.
- T1: change status to "revise before run". Required edits: embedded source key, left-join status view (G23), `Sending` and `TimedOut` statuses, `Subscription_id` on the send log, Singapore date, lock mechanism.
- T2: add view resolution, connection hash, subscription capture, daily diff of affected workbooks, subscription-driven scope. Depends on T1, D1b.
- T4: depends on T1, T2, T3 and D1b. Implement inside the new script only; do not modify the old job. Include the workbook refresh call for embedded extracts and a "no extract: skip" path.
- T5: add timeout state and alert.
- T6: remove dependency on T5; depends on T2. Add view versus workbook export and the recipient domain allowlist.
- T7: per-subscription send, claim row before send, stale claim handling, application lock.
- Add **T11** subscription cut-over: backup, pause, restore, per-workbook enable flag.
- Add **T12** alerting: daily not-sent summary, exit codes, log location.
- Add **T13** time window and run budget: decision on the scheduler window and execution limit; time-bounded loop.
- T8: reword as "dry run, then one real send to the user's own address on one workbook". T9: mark "parked per scope cut" (packaging) and keep only the ops note (owner, PAT expiry, rotation, logs).
- T10: keep first on the critical path; add size limit check and STARTTLS test.
- Add **D3** (read-only): list how many of the other 72 subscriptions are on single-source workbooks with multi-ETL views (seed G16a). Report only.

### DECISIONS.md (proposals)

- **D-012** Old subscriptions: pause (not delete) per workbook at cut-over, definitions backed up first, restored on rollback.
- **D-013** Counting rule: data sources with an identical connection count once; `X` and `X (2)` are one source; published and embedded copies of the same name count once if the connection matches.
- **D-014** Send granularity: one email per original subscription per day (not per workbook).
- **D-015** Day definition: Singapore date; a source is fresh when its refresh job was submitted after the latest required ETL end today.
- **D-016** New flow is additive: own tables, own script, own PAT; the old refresh job is not modified.
- **D-017** Blocked workbooks (D-006) keep their Tableau subscriptions; the script logs and reports them.
- **D-018** Send idempotency: claim row first, at-most-once, with an alert for stale claims.
- **D-019** Recipients must be in an allowlist domain (`@example.com`), enforced in code (implements D-008).
- Correct D-010: the "19 tables behind 18 workbooks" figure does not match the subscribed set; state the current count.
- Q4: close the Metadata API item (enabled).
- Q6 (REST "send now"): keep open, low value now that G3 and G7 point to the script sending itself.

### Other documents

- STATUS.md: refresh the header (phase, date), remove D1 from Next, remove the SMTP and Metadata API items from "Open items", fix the PAT rotation date, and add the 16/22 numbers.
- TIMELINE.md: replace "33 blocked" and "only 5 clean" with 22 blocked and 16 pass; re-estimate T2 on the subscription-driven scope and T4/T5 after D1b.
- CLAUDE.md: remove "packaged as an exe" from conventions (or mark parked), note the separate-PAT rule.
