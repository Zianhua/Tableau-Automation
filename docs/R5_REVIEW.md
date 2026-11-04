# R5 review (D-047): fixes for R3B_REVIEW F-1..F-8

Reviewer: fresh review, 2026-10-23. I read the whole diff against HEAD 99cee25 (`git diff`, `git status`), the new SQL and the new files, and the docs. I ran: `t11_checks`, `t6_checks`, `r1_checks`, `r2_checks`, `t4_checks` (all pass: 114, 115, 185, 119, 61 checks, fixtures on the dev DWH only, MAIL_TRANSPORT=local) and `t2_checks.py` ONCE (read-only Tableau sign-in twice, via its own re-run and dry-run; no Tableau write; T2 wrote nothing: `written=0`). No code edited, no mail sent, no `.env` value read. Dev DWH only.

## Summary

No double-send or mass-send path found. The fixes are real and the new tests are not vacuous. Two things need attention before the rehearsal:

1. **T2 cannot run against UAT right now, and it is not the builder's fault** (answer to the t2_checks question below). Because of that, the new R5 write path of T2 has never run against a real Tableau, and all 54 dev rows have `WorkbookUpdatedAt = NULL`, so t6 would hold every subscription as `awaiting_sync` (fail closed, quiet).
2. **F-3 is only half fixed**: the "republished during or after the T2 read" hole is closed; the "Metadata API lags behind the REST `updated_at`" hole is not, and is not listed as residual.

Everything else is Low.

## t2_checks.py: two failures, do they predate the builder?

**Yes. They predate the R5 change and are caused by the Tableau server, not by code.** Both T2 child runs (the real re-run and the `--dry-run`) die in the same place, before any R5 code could matter:

`t2_mapping_sync.py:294 collect_tableau -> read_custom_sql (line 240-242)` raises `RuntimeError: GraphQL errors reading customSQLTables page: [{'message': 'Obfuscation is disabled with your site settings so your results have been automatically filtered to contain only the results you have permissions to see', ... 'code': 'PERMISSIONS_MODE_SWITCHED' ...}]`.

- `read_custom_sql` is byte-identical to HEAD (it is not in the diff). It raises on ANY entry in the GraphQL `errors` list, and the server now sends this severity-WARNING entry.
- "rerun exits 0" fails because the run exits 1 (FATAL). The message the check prints ("Alert mail failed ... ConnectionRefused") is only a side effect: the child is started with the local test SMTP sink and `t2_checks.py` does not start a sink, so the failure alert cannot be delivered either.
- "dry-run after a run reports no inserts or updates" fails because the dry run dies with the same error, so it prints no `Tableau_*` lines.
- "rerun changes nothing" passes only because nothing was written. The last good T2 write on dev was today 16:29 (`max(LastSeenAt)`), so the server behaviour changed after that.
- Hypothesis, not verified: the "site-level API update was accepted" mentioned in NEXT_SESSION step 0b (the attempt to enable subscriptions) may have changed a site setting (permissions/obfuscation mode). Check the UAT site settings first.
- Important: do NOT simply make `read_custom_sql` ignore this warning. The message says results are filtered to what the PAT user may see. That can hide custom SQL tables, so a workbook could look like it has fewer sources (fail open in the readiness gate). Raising is the correct fail-closed behaviour. Fix the site setting, or give the PAT user full rights, then rerun.

## Findings (severity, location, failure scenario, suggested fix)

### R5-1 (Medium, blocks the rehearsal): the real T2 write path of F-3 is unverified, and dev has NULL everywhere
- Where: `t2_mapping_sync.py:259-263` (`wb_updated`), `:653-661` (MERGE), `t6_send.py:818-826`.
- State: `Tableau_Subscription.WorkbookUpdatedAt` is NULL for 54 of 54 active rows on dev. Tests prove the compare logic with fakes and a rolled-back MERGE, not with a real REST workbook list. Unverified against real Tableau: that `updated_at` from the list call (T2) equals `updated_at` from the single-workbook GET (t6, `describe`) to the second. If they differ even slightly, every subscription is `awaiting_sync` every day, quietly.
- Fix: after the site setting is repaired, run T2 once and compare, for a few workbooks, the stored value with what t6 reads live (`t6_send --dry-run` should show zero `awaiting_sync` right after T2). Add that line to the rehearsal list. Also decide whether a day with `awaiting_sync` for ALL due subscriptions should raise an alert (it means T2 stored nothing usable); today it is INFO only.

### R5-2 (Medium-Low): F-3 hole (b) is still open and not documented as residual
- Where: `t2_mapping_sync.py:259-263`, D-047(3), LIMITATIONS (only the embedded-extract question is listed).
- Scenario: workbook republished at 07:30 with a new data source; the Metadata API still returns the old list at 07:50. T2 stores the NEW REST `updated_at` (07:30) together with the OLD mapping. t6 compares live 07:30 to stored 07:30, passes, and sends with a mapping that lacks the new source (not gated, not refreshed). R3B suggested a margin (for example do not trust a stored row whose `updated_at` is within 15 minutes of the T2 read time, treat as `awaiting_sync`). Not implemented, not mentioned.
- Fix (small): in T2, store NULL (= awaiting_sync) when `read_time - updated_at < 15 min`; or at least list it in LIMITATIONS as accepted residual.

### R5-3 (Low-Medium): the new-takeover cap is per run, not per day
- Where: `t11_pause.py:597-603`, `:634-637`.
- t11 runs every 15 minutes (33 runs in 08:30-16:30). A wrong allow-list takes all 54 in about 6 runs, roughly 75 minutes. The `takeover_cap` alert mail is the only brake, and it goes out once per day after the first capped run. This matches the user's decision (per-run cap, default 10), so it is not a defect, but say so in RUNBOOK 14 item 5 so nobody reads it as a daily cap. A real day cap would be: count open takeover rows created today (`BackedUpAt`) and refuse beyond N.
- Also: `TAKEOVER_MAX_NEW=` (set but empty) is a hard `ConfigError`; fine (fail closed), just know the t11 task would then fail every run.

### R5-4 (Low): `--restore` refusal also blocks the retry of a half-done restore
- Where: `t11_pause.py:683-696`.
- Scenario: `--restore` marks the row restored, the resume call fails (`restore_failed`). The same day t6 had already sent it. Re-running `--restore` for the retry is now refused (`sent_today`), so the subscription stays suspended and "restored" (neither t6 nor Tableau sends it) until someone restores tomorrow or uses `--allow-restore-after-send`. No double mail, but a silent gap of a day for the following occurrence is possible only if nobody retries tomorrow.
- Fix: skip the guard when `bid is None` (already marked restored): the mail risk is exactly the same either way, but then the refusal should not leave a half-restored state. Or at least log that the row is already marked restored and what to do.
- Also the refusal is only `log.error` plus exit code 1 (no alert event). Fine for a manual command.

### R5-5 (Low): `new_native` alert now fires for suspended, not-yet-taken-over subscriptions
- Where: `t6_send.py:733-737`, `SUBS_SQL` (the `IsSuspended` exclusion was removed).
- Scenario: during the pilot (one workbook allow-listed, owner-fenced), the 30 suspended subscriptions of OTHER owners on that workbook are now loaded and each raises `new_native` "Tableau sends it itself", which is false for a suspended one. Noise in the alert mail, not a safety issue.
- Fix: suppress `new_native` when the live/snapshot `IsSuspended = 1`, or reword.

### R5-6 (Low): wording and documentation gaps
- `daily_summary.py:165-166`: "no action needed" for `awaiting_sync`. Wrong if it repeats every day (R5-1, or the embedded-extract question). Say "no action needed unless this repeats on several days".
- F-6 was "accepted and documented" (D-047(6)) but neither half is in LIMITATIONS or RUNBOOK: stored `ScheduleId` can be up to 24 h old, and `SCHEDULE_MODE=ignore` removes the "schedule suspended/unsupported" fence in t11 (`t11_pause.py` skip is inside `if ctx.due is not None`). Add two lines.
- `ops.event_log_once` writes the once-per-day marker BEFORE the Event Log entry (`ops.py:179-198`). If the Event Log source is not registered (`event_log` returns False) the marker still suppresses later tries that day. The log line still exists. Acceptable; mention in RUNBOOK 15 that the source must be registered or the takeover-day warning is log-only.

### R5-7 (Low): the SQL test runs real DDL on the shared dev tables
- Where: `t11_checks.py:test_sql_files` (`:683-740`).
- It runs R4/R5 and their rollbacks for real inside one transaction that is rolled back, and then checks the final state. This is a real test (it proves the NOEXEC stop and default-constraint handling), but it holds schema-modification locks on `Tableau_SubscriptionBackup` and `Tableau_Subscription` while it runs, so a scheduled dev run (T2/t6) at the same moment would block, and the last assertion assumes dev already has R5 applied and R4 applied. Do not run `t11_checks.py` while the dev scheduler tasks are live. Note it in the check's docstring.

## Answers to the review questions

**Double-send / mass-send:** none new. Takeover-day guard unchanged; restore-after-send now refused; takeover still needs the row, the PausedAt and Tableau showing it suspended. The owner fence is now mandatory for `--execute` with an allow-list (including the env allow-list, tested) and `register_tasks.ps1 -Live` demands it too. The cap only limits; it does not open anything.

**Fail-open:**
- NULL `WorkbookUpdatedAt`: fail closed (`awaiting_sync`, no claim). Live `updated_at` unknown: fail closed. Tests: `T6TEST-U1..U5` are real (the clock-based check would have passed U1, the new one blocks it; U5 is NULL).
- Owner fence: `(wbs or subs)` is evaluated after the env fallback, so an env-only allow-list also needs the fence. Restore and an empty allow-list need none, as intended. An owner id that matches nobody takes nobody over (skipped, INFO).
- `--max-new`: negative or non-number is refused; 0 takes nobody new but still keeps existing ones paused (tested). Retried unfinished rows (PausedAt NULL) are not counted against the cap, but they can only exist from earlier capped attempts.
- Restore-after-send guard: reads `SendDate = today` with Status in Sent/Claimed/TimedOut, the same set the unique index uses; SendDate is Singapore day and `ctx.today` is the DWH Singapore date. Override flag `--allow-restore-after-send` is explicit, warns, and is not whitelisted in `register_tasks.ps1`. Gap: R5-4.
- Takeover-day warning: emitted in both branches (schedule honoured and ignored), only after the cheap exits (Sent, claimed, etc.), once per subscription and Singapore day, execute runs only (tested: dry run writes nothing, next day sends and no warning, `ignore` path warns). It goes to log and Event Log only, not to the alert mail or the daily summary; that is what the user chose, but note the Event Log source must exist on the host.

**R4 / R5 SQL:**
- R4 now uses `RAISERROR` + `SET NOEXEC ON` and ends with `SET NOEXEC OFF`. The test runs it batch by batch like SSMS: with a `WasSuspended = 1` row, column and CHECK are still there afterwards (so the later batches really were skipped), and the session works again. Default constraint dropped by lookup in `sys.default_constraints` (tested with a system-generated name). Idempotent (second run clean), rollback idempotent, rollback header warns about open rows. Note the CHECK drop in batch 3 is skipped under NOEXEC as well, so a refused run changes nothing.
- R5: one nullable column, guarded by `COL_LENGTH`, idempotent, rollback drops it. T2's `schema_ready` now requires the column, so a missing R5 stops T2 with a clear message instead of a half write.
- All 14 files in `scripts/sql` still start with the dev-server guard (`DB_NAME()` / `@@SERVERNAME` = <DEV_DWH_SERVER>, `SET NOEXEC ON`); the two new pairs copy it exactly. `t4.connect()` still refuses any non-dev server in Python. The test only checks the guard text (it cannot run on another server), which is acceptable.

**Secret leaks:** none found. New log/Event Log text contains workbook name, subscription id, schedule name, counts; no addresses, no env values. `review_list.csv` has owner user ids and subjects (may hold personal names), written to `out/` which is gitignored.

**Tests real or vacuous:** the old `t11_checks.py:338` (`ctx.n["skip_pre_suspended"] == 0`) and `t6_checks.py:439` (`"suspended_live" not in s["kinds"]`) are gone; no reference to `skip_pre_suspended`/`suspended_live` remains. Replacements assert behaviour: t6 `T6TEST-7L` asserts `sent == 1`, exactly one log row `Sent`, one mail, and `sum(kinds) == 1`; t11 `I4` asserts a backup row exists, PausedAt set, no extra Tableau update call, then `Sent` once and not twice. The cap, restore-after-send, owner fence, review_list (formula cells, next_due, CSV, read-only by source scan and DWH checksum), event_log_once and SQL tests all assert observable state. Weak spots: `test_t4_counts_takeover` first asserts a substring of the SQL text (but the following checks call `load_targets` and `due_workbooks` for real, which is the F-8 ask); the dev-data check on "suspended-only target" prints SKIP when the data has none (so that D-046 case is not proven on real data).

**F-1..F-8 status:**
- F-1: done as decided (warning, no WasSuspended). Residual is the operational rule in RUNBOOK 14 item 6; warning is Event Log/log only.
- F-2: done (cap, fence everywhere, scheduler). See R5-3.
- F-3: half done, see R5-1 and R5-2.
- F-4: done; see R5-4.
- F-5: done and proven by a real run.
- F-6: accepted; documentation incomplete (R5-6).
- F-7: done (formula quote, next_due with guard/catch-up). Still missing from the old list: a column saying whether t11 would take it over; not needed for safety.
- F-8: done, tests are real.

**Line endings:** no CRLF/LF damage. Every changed file stays CRLF (all lines end in CRLF) and the new files are CRLF too; `git diff --numstat` is identical with and without `--ignore-space-at-eol`. Only trivia: `t4_checks.py` and `t4_refresh.py` had a bare LF on their last line at HEAD and now have CRLF there, and git prints "LF will be replaced by CRLF" warnings for 7 files because `core.autocrlf=true` while HEAD stores CRLF. No action needed; if you want it quiet, add a `.gitattributes` with `* text=auto eol=crlf` in a separate commit.

## Suggested order
1. Fix the UAT site setting behind the GraphQL warning, rerun `t2_checks.py` (expect 32 pass), then run T2 once and confirm `awaiting_sync = 0` in a t6 dry run (R5-1).
2. R5-2 margin or an explicit residual note.
3. R5-4 and R5-5 (small), then the wording items in R5-6.
