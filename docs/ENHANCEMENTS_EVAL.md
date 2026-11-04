# Part 2 enhancements: evaluation

Written 2026-10-23 from the docs and a read-only look at the scripts. All of these are nice to have. None may delay the production pilot. Rule used: large or able to cause a double-send or a missed report means drop.

"Core flow" means the chain: T2 mapping, t11 takeover, t4 gated refresh, t6 claim-then-send. Double-send risk lives in the t6 send log and in t11 pausing. Missed-report risk lives in the readiness gate (t4/t6) and in the schedule and takeover rules.

## 1. Wide-crosstab images (unreadable slivers)

- **Warning only (flag it in the daily summary): small.** The detection already exists in `subscription_mail.py` (3200 px cap, `MIN_READABLE_HEIGHT`) and t6 already warns in the log. Work: have t6 write the list of affected subscriptions into its last-run marker (`ops.py`), and print it in `daily_summary.py`. Risk to core flow: very low if it only records a note after the export and never changes whether a mail goes. Recommendation: **keep**.
- **Export the dashboard instead of the worksheet: medium, and not ours to decide.** It changes what recipients receive, touches `subscription_mail.py` and the takeover fidelity rules, and the REST API cannot tell dashboard from worksheet (docs/preview_notes.md). The real fix is the report owner subscribing to a dashboard. Recommendation: **drop**, tell the report owner.

## 2. Health view (sends, refreshes, not-sent reasons, stale mappings)

- **A SQL view over existing tables: small.** Pure new read-only SQL over `Tableau_SubscriptionSendLog`, `Tableau_RefreshJob`, `vw_Tableau_WorkbookReadiness`. No existing script changes. Risk: none to the flow (only one new view, and the SQL guard files already show the pattern). Recommendation: **keep, but only after the pilot**.
- **A Tableau workbook on top of it: medium.** New Tableau content that someone must publish and maintain, and it would use the PAT or a DB login. Recommendation: **drop**. The daily summary mail already covers the day-to-day need (`daily_summary.py`).

## 3. Per-table readiness instead of per-pipeline

- **Large.** It changes the heart of the gate: `ETL_SQL` and `decide()` in `t4_refresh.py`, the readiness views in T1, the registry load (T3) and the t6 freshness check, and needs a new "this table was loaded today" signal that `ETL_Master_Log` does not give (it only has pipeline, end time, success). The lineage tables (`Integration.Lineage`, `DW_NovaLineage`, `OAO_Lineage`) would have to be trusted as a loader signal, and they are known to have name mismatches (see T3 notes). Risk: high in both directions, a wrong rule either refreshes too early (stale data sent) or blocks reports for ever (missed report). Recommendation: **drop**. The documented limitation (a pipeline that ran counts as ready) is acceptable.

## 4. Excel/CSV freshness (E1) and external database sources (E2)

- **Large for both.** Needs file-timestamp access from the host or a new connection type, a new readiness kind in the views and in t4/t6, and for E2 credentials to other databases. The simple interim answer is already decided in D-028 (a "no readiness check" override for the four Excel/CSV workbooks, 6 subscriptions). Risk: high (new fail-closed paths that block sends). Recommendation: **drop**; answer D-028 instead.

## 5. Bounce/complaint visibility, mail size and delivery tracking

- **Medium to large, and depends on SES.** Real bounce data needs an SES configuration set plus SNS or CloudWatch, owned by the team that owns the SES account, and a way to read it back. Nothing in the scripts can see a bounce after the server accepts the mail. Mail size is already limited and logged (`OVERSIZE:`). Risk: low to the flow if read-only, but the effort sits outside our code. Recommendation: **drop** for now. Ask the SES owner to switch on bounce notifications on their side, and revisit only if recipients report missing mails.

## 6. Detect a workbook that drops to one data source, or a re-created subscription

- **Small if alert-only.** The pieces exist: `GoneSince` and the 7-day close alert in t11, the drop guard and `--accept-drops` in T2. What is missing is a visible alert when a workbook with a taken-over subscription leaves scope, which today means that report silently stops (LIMITATIONS). Work: one query and one line in `daily_summary.py` (or an alert in `t2_mapping_sync.py` when it deactivates rows). It must only report, never restore or pause anything. Risk: low. Recommendation: **keep, alert only**. Automatic restore is **drop** (touches t11, risks a double mail).

## 7. Exe packaging with scheduler XML (T9, parked)

- **Medium.** No flow risk, but it adds a build step, and the scripts already run under Task Scheduler as plain Python (RUNBOOK section 1). Packaging adds new failure modes (antivirus, path handling for `.env` and `out/`). Recommendation: **drop** unless the host cannot have Python. If wanted, ship only the scheduler XML files, which is small and has no code risk.

## Recommended order

1. Pilot first, nothing from this list before it works.
2. Item 6, alert-only (small, closes a silent-stop gap).
3. Item 1, warning only in the daily summary (small).
4. Item 2, SQL health view (small, optional).
5. Scheduler XML from item 7, only if ops asks.
6. Drop: per-table readiness, E1/E2 (use D-028), bounce tracking (ask the SES owner), dashboard export, Tableau health workbook, automatic restore, exe packaging.

Net: three small, alert-or-read-only items are worth doing after the pilot. Everything else costs more than it gives.
