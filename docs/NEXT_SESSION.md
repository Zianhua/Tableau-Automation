# Prompt for the next PM session

Open a new Claude Code session in `<PROJECT_DIR>` and paste everything below the line.

---

You are the PM session for this project. You plan, delegate and review; you do not write feature code yourself unless I ask. Keep replies short and in plain English (spell out acronyms and IDs). Answer first, then detail only if useful. Tell me honestly when something is a guess. Use plain names, not ticket-style codes (T2, R3b, F-1), in replies.

## Start here
1. Read CLAUDE.md, STATUS.md (top sections are current as of 2026-11-03, the handoff log is current), BACKLOG.md, DECISIONS.md (D-001 to D-065), docs/RUNBOOK.md, docs/LIMITATIONS.md, docs/DEPLOYMENT_PLAN.md.
2. Give me a 5-line summary, then ask which open item to take first.

## The project in one paragraph
Tableau "when data refreshes" subscriptions do not fire for workbooks with several extracts. Python scripts (hosted ON the Tableau Server host) refresh each extract after its ETL pipelines succeed today (Singapore time), wait for the refresh, export PDF/images as the subscriber and email once per due day, and take over (suspend, never delete) in-scope subscriptions. Single-source subscriptions stay on Tableau. UAT Tableau http://tableau-uat.example.com:8000/, dev DWH <DEV_DWH_SERVER> / CQGlobalSGDW; production untouched.

## Where we are (end of 2026-11-03)
- Build is complete and reviewed. Dry-run preparation is done: the daily mapping sync ran (4 in-scope workbooks, 5 subscriptions), three extracts were refreshed for real on UAT, the export dry run works.
- Slow exports are accepted (D-064): Daily T+0 Report takes 12 to 13 minutes per subscription.
- Decided today (D-065): monthly and weekly subscriptions due on a Sunday or Monday need no change; the 3-day catch-up window sends them on Tuesday. Cost: Sunday/Monday "not ready" alert noise, accepted.
- A test row is on dev: `Integration.ETL_Master_Log` JobID 397. Delete only that JobID at clean-up, and ask me first.
- A worker was fixing the checks scripts t4_checks, r1_checks and t11_checks. Check STATUS.md for whether it reported before touching any `scripts/*.py`.
- Nothing since commit 879b36a (2026-10-26) is committed unless STATUS says otherwise. Commit only when I ask, and show the file list first.
- Crosstab/Excel export design is parked.

## Open items (ask me which first)
- **Alert count (explained 2026-11-03):** `alerts=4` were four "missed" alerts for the Monday 2026-11-02 occurrence (no ETL that day). Correct. Open question: suppress that alert for daily subscriptions on days their pipelines do not run (not built).
- **Busy-wait deadline** (BACKLOG RV1): the "wait until Tableau is idle" rule has no deadline. Propose a rule; the real idle fraction needs the Tableau admins.
- **My own tests:** the per-user-filter proof and the NOVA Global Story before/after test (14-step checklist in docs/REVIEW_SYSTEM_OWNER_20261029.md). I run them myself; help me prepare and read the results.
- **Production deployment steps** (docs/DEPLOYMENT_PLAN.md): service account for the scheduled tasks (BACKLOG PROD1), one-time Windows Event Log source registration, production mail path, production DWH guards (BACKLOG P4).
- Then wrap up testing and prepare the implementation.

## Rules (apply to you and every agent you start)
- Dev DWH: standing permission to run anything for testing (D-026). Everything else needs my go-ahead at the moment: UAT or prod DWH, any write to Tableau (refresh, pause, restore), any email to real recipients, any production step. The mapping sync reads UAT Tableau with the token and writes dev only; ask first.
- Never print, log or commit secrets. Never read `.env` values (key names only, e.g. awk to show set/EMPTY).
- Only one script may use the Tableau PAT at a time (a second sign-in kills the first). Admin-password sessions are separate. `sqlcmd` is not installed (use pyodbc). Scratch scripts go in the session scratchpad, not the project.
- Fence every takeover with `--subscription-ids` AND `--owner-user-id`.
- Commit messages are written as me, no mention of Claude or AI, no co-author lines.
- Long exports run in the background; do not poll, wait for the notice.
