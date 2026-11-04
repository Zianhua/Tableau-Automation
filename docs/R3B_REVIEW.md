# R3b review (D-046): suspended is no special category

Reviewer: fresh static review, 2026-10-23. I read code, SQL, tests and docs only. I ran nothing, did not use the PAT, wrote to no database, sent no mail and did not open `.env` values. Edited nothing except this file.

Read: CLAUDE.md, DECISIONS.md (D-023 reversed, D-035, D-036, D-041, D-045, D-046), LIMITATIONS.md, FULL_REVIEW.md (top half), t11_pause.py, t6_send.py, schedule_due.py, t4_refresh.py (scope and candidate SQL), daily_summary.py (the awaiting_sync part), review_list.py, R4 SQL + rollback, T11_pause_schema.sql, the relevant parts of t11_checks / t6_checks / r1_checks / r2_checks, RUNBOOK sections 1, 6, 11, 14, and the T2 MERGE code for `LastSeenAt`.

## Summary

I found no path to a double mail or a mass send in the default configuration. The core rule holds: t6 sends only a row that t11 has taken over (PausedAt set) AND that Tableau shows as suspended right now. A human un-suspend before the send, a re-pause on the same day, a deleted subscription and a suspended Tableau schedule are all handled. Nothing is sent until someone runs `t11 --execute` with a workbook-id allow-list, so nothing goes out before your clean-up unless that command is run early.

Three things are worth acting on before go-live:

1. **A report that is a month (or a week) late, silently.** Dropping `WasSuspended` removed the only fact that told the takeover-day guard "Tableau never mailed this one". A previously suspended monthly subscription taken over after its schedule time on the 1st is skipped as "taken_over_today", with no alert, and next goes out on the 1st of the next month. For 2026-11-01 (a Sunday) this affects the 22 monthly subscriptions if takeover happens late.
2. **Clean-up and takeover are connected only by discipline.** There is no code fence between "review list" and "send". Allow-listing a workbook takes over every subscription on it, including the long-suspended ones of other owners, and `MAX_SENDS_PER_RUN=10` is a throttle, not a cap: all 54 can go in one day.
3. **`awaiting_sync` is fail-closed for a workbook republished after the sync, but the T2 staleness check has a hole** for a workbook changed or lagging while T2 itself runs, and with T2 once a day that hole now lasts 24 hours.

Everything else is Low.

---

## Findings, ranked

### F-1 (Medium, probable on 1 Nov): takeover-day guard now loses the report for an already-suspended subscription

- Where: `schedule_due.py:166-171` (`_counts`: an occurrence at or before PausedAt is ignored), `schedule_due.py:193` (`taken_today`), `t6_send.py:770-772` (returns `taken_over_today`, no event), `t11_pause.py:445-447` (a found-suspended subscription gets PausedAt = now), `RUNBOOK.md:272` (item 6 says "same as for the others"), `LIMITATIONS.md:58`.
- Why the guard exists: for a live subscription, Tableau may already have mailed today's occurrence before the pause, so the script skips it. That is right for a live subscription. It is wrong for one that was suspended before takeover, because Tableau sent nothing, so skipping the occurrence means nobody sends it. Before R4 the row carried `WasSuspended`, which could have told the two apart; now nothing does. LIMITATIONS.md line 58 says so ("the script cannot tell") but treats it as conservative, not as a lost report.
- Scenario: monthly "1100AM_1stDayEveryMonth", suspended today. t11 takes it over on Sunday 2026-11-01 at 12:00 (or any time after 11:00 up to the 4th). The Nov 1 occurrence is ignored because its start time is before PausedAt; it is not "unsatisfied", so the 3-day catch-up window does not help either. The next occurrence is 2026-12-01. No mail, no alert (`taken_over_today` is explicitly "not an error"), the daily summary does not list it (status is not_due). Same for a Saturday weekly report taken over on Saturday afternoon: a week lost.
- Fix (small): keep one fact on the backup row (a bit `FoundSuspended`, or text in the existing `Note`), set it in `pause()` when `live.suspended`, and pass `not_before=None` for those rows in `process()` / `account_skipped` / `daily_summary` / `t4.due_workbooks`. Cheaper operational alternative: take over everything that is due on 1 Nov before 11:00 on 31 Oct or earlier, and say so in RUNBOOK section 14 item 6. Either way, correct item 6, which currently reads as if nothing is lost.

### F-2 (Medium): nothing in code connects the clean-up to the first takeover; one allow-list entry sweeps up everything on the workbook

- Where: `t11_pause.py:501-503` (`enabled`: workbook id OR name OR subscription id), `t11_pause.py:213-216` (the owner fence is mandatory only for Gmail test mode), `t6_send.py:849` (`MAX_SENDS_PER_RUN`), `RUNBOOK.md:263-272`.
- Before D-046 a long-suspended subscription on an allow-listed workbook was never touched, so the workbook-id allow-list was a tight pilot fence. Now every subscription on that workbook is eligible, whoever owns it and however long it has been suspended. The only barrier is that someone deleted it in Tableau first.
- Scenario (question 2): you allow-list the 38 in-scope workbooks (or one workbook that holds a stale suspended subscription) and run `t11 --execute` before finishing the CSV clean-up. All 54 get PausedAt. t6 then sends each one on its next due day: whatever is due today goes out at 10 per 15-minute run, i.e. everything due today within about an hour or two. Throttling 10 per run only spaces them; it does not cap the day. SEND_ALLOWED_RECIPIENTS stops outsiders only in smtp mode and is a recipient list, not a subscription list.
- What does protect you today: empty allow-list means nothing is taken over and Tableau is not even signed in to (`t11_pause.py` header); t6 and t4 (default scope) never act without a takeover row; the 22 monthly subscriptions are only sent on or after 1 Nov because they are not due before; `review_list.py` is read-only.
- Fix (small, worth it): give `t11 --execute` a per-run cap on NEW takeovers (for example `--max-new`, default 5, error if more would be taken over) so a wrong allow-list cannot take 54 at once; use `--subscription-ids` for the pilot rather than workbook ids; require the owner fence (`--owner-user-id`) for any first production `--execute`, not only Gmail.

### F-3 (Medium): the "workbook changed after sync" check cannot see a change during or lagging behind the T2 run, and T2 is now once a day

- Where: `t6_send.py:802-807`, `t2_mapping_sync.py:623-650` (`LastSeenAt = SYSDATETIME()` in the MERGE, i.e. stamped AFTER the whole read), D-046 item 5.
- The check compares the live workbook `updated_at` with the subscription's `LastSeenAt`. Two holes:
  (a) T2 reads the workbook at 07:52, someone republishes at 07:56, the MERGE stamps `LastSeenAt` at 07:58. `updated_at` (07:56) is older than `LastSeenAt` (07:58), so the old mapping passes as fresh.
  (b) Tableau's Metadata API indexes asynchronously (recall, verify; it is why D-042 added the drop guard). If a workbook was republished at 07:30 with a new data source and the Metadata API still returns the old list at 07:50, T2 stores the old list. The drop guard only catches FEWER sources. `LastSeenAt` is later than `updated_at`, so t6 sends against a mapping that lacks the new source: its ETL is not gated and its extract is not refreshed.
- Before D-046 T2 ran hourly, so (a) and (b) lasted at most an hour; now they last until the next day's sync. Published data source changes are not covered by workbook `updated_at` at all, same 24-hour exposure.
- The part you asked about is fine: `awaiting_sync` is reached only AFTER `gate.check` (`t6_send.py:778-781`), so a workbook that is not Ready or has an unknown/failed pipeline is still blocked. It returns before any claim. A republish after the sync therefore fails closed, not open.
- Visibility is acceptable, not great: quiet at the INFO level, but a subscription that is due and not sent shows in the daily summary's "DUE BUT NOT SENT" (which counts toward "needs attention"), and a window that ends unsent raises `missed_due` (exit 1). The summary text says "no action needed", which is wrong if it repeats daily.
- Open to verify: if `updated_at` also moves when a workbook's embedded extract is refreshed (I believe it may, not sure), then a workbook whose embedded extract refreshes after 07:50 would be `awaiting_sync` every day, i.e. permanently unsent with no alert for a day or more. Check on UAT with one embedded-extract workbook.
- Fix: have T2 store the REST `updated_at` it actually read (one datetime column per workbook) and have t6 compare live `updated_at` to that stored value (not to a clock). This removes hole (a), and (b) partly (a later Metadata catch-up shows as a changed stored value only at the next sync, so also consider requiring `updated_at` + 15 min < the T2 read time).

### F-4 (Low-Medium): restore has no same-day guard, and revives auto-suspended subscriptions

- Where: `t11_pause.py:630-689`, LIMITATIONS lines 36 and 51.
- Scenario: t6 sends the Tuesday report at 09:40 (schedule 11:00). At 10:00 someone runs `--restore`. Tableau now sends it at 11:00: two mails. Also, `--restore` of a subscription that Tableau had auto-suspended for repeated failures turns it live again; it will fail again and be re-suspended.
- Fix: `--restore` refuses (or warns and requires `--force`) when a Sent/Claimed row exists for today, and prints whether the subscription was found suspended (see F-1 flag).

### F-5 (Low): R4 SQL guard does not stop the script

- `R4_takeover_all.sql:15-21`: the `THROW` ends only batch 2. SSMS continues with the next batches after `GO`, so batches 3-4 still drop the CHECK and the column, defeating the "refuses to run" promise. (Dev had 0 rows and the guard is almost moot, since the old CHECK forbade `WasSuspended = 1` on any taken-over row, but the pattern is wrong.) The other SQL files use `SET NOEXEC ON` for this, so use the same here: `THROW` plus `SET NOEXEC ON` in a TRY/CATCH, or `RAISERROR` then `SET NOEXEC ON`.
- `DROP COLUMN` at line 27 fails if the original column had a default constraint (I cannot see the original DDL any more). It would fail loudly, leaving the CHECK dropped and the column present; a rerun fails the same way. If it did apply on dev, this is moot, but then check prod is built from the same DDL. Safer: drop any default constraint on the column first (lookup in `sys.default_constraints`).
- Idempotency is otherwise fine (every step is guarded by `COL_LENGTH` / `sys.check_constraints`). Rollback is idempotent and correct for an empty table. With rows present it sets every `WasSuspended = 0`, so old code would treat a row taken over from suspended as "paused by us" and `--restore` would resume it; that is the same behaviour as the new code, but it should be said in the rollback header.
- Deployment order is not in the runbook: new t11 inserts no `WasSuspended`; on a database where R4 was not applied and the column has no default, every takeover fails (loudly, as `pause_failed`). Add "apply R4 first" to RUNBOOK section 14.

### F-6 (Low): t6 uses the stored schedule, not the live one; `SCHEDULE_MODE=ignore` bypasses the schedule-state fence

- `t6_send.py:808` compares only the target. If an owner edits a taken-over subscription's schedule in the UI, t6 keeps using the old `ScheduleId` until the next daily sync (up to 24 h, previously about 1 h).
- `t11_pause.py:518-523`: the "schedule suspended / unsupported" skip is inside `if ctx.due is not None`, so with `SCHEDULE_MODE=ignore` a subscription on a suspended schedule is taken over and t6 would send it every day. `ignore` is a documented fallback only, but the runbook should say it removes this fence.

### F-7 (Low): review_list.py

- No email addresses are read or written (owner is a user id). Subjects and workbook names are written and can contain personal names; a subject beginning with `=`, `+`, `-` or `@` is a spreadsheet-formula risk when the CSV is opened in Excel (prefix such cells with a quote). Output goes to `out/review_list.csv` by default; `--out` can point anywhere, which is fine.
- PAT lock is used correctly (`LazyTableau(cur, owner=...)`, taken before sign-in, released in `finally`, `PatBusy` ends the script without reading anything). Dev guard applies via `t4.connect`.
- Due-date logic agrees with `schedule_due.fires_on` / `next_occurrence`, but it is a simpler question than t6's: it says "next date the schedule fires, starting today", so it (a) shows today as due even when the schedule time has passed (first send would be next occurrence, F-1), (b) ignores the staleness limit on the stored schedule, (c) cannot show catch-up days. For the clean-up purpose (what will be sent in the next 14 days) it is a reasonable approximation; label the column "next fire date". The 22 monthly rows (1 Nov, 9 days away) will correctly show as due in 14 days.
- Missing for the purpose: a column saying whether t11 would take it over (single source, stale T2 row, no schedule id, NULL key hash), and the count of subscriptions per owner. Not needed for safety.

### F-8 (Low): tests

- Vacuous: `t11_checks.py:338` asserts `ctx.n["skip_pre_suspended"] == 0` on a `Counter` (always true; the real assertion is `paused == 1`). `t6_checks.py:439` `"suspended_live" not in s["kinds"]` is true for any Counter that never counted it; the real assertion is `sent == 1`.
- `test_t4_counts_takeover` (`t11_checks.py:550-569`) re-runs the `SCOPES` SQL fragment itself against fixtures; it does not call `load_targets` or `due_workbooks` for the scope difference (r1_checks 884-889 does call `due_workbooks`, good). Weak but not wrong.
- No test: a previously suspended subscription on a takeover day (F-1; the t11/t6 check helpers set `takeover_day_guard=False`, `t11_checks.py:171`, `t6_checks.py:122`, so the interaction test at `t11_checks.py:507-511` sends a formerly suspended subscription on its takeover day, which production would not); `review_list.py` at all; the R4 SQL (only the final schema is asserted, `t11_checks.py:298`, so it depends on dev having had R4 applied); restore after a same-day send (F-4).
- No test still asserts the old "suspended is never sent" behaviour; the old assertions were converted (`t6_checks.py:429-439`, `t11_checks.py:332-346, 435-438, 495-538`).

---

## Answers to the specific questions

**Double mail**
- Is a "previously suspended" subscription really suspended? t6 asks Tableau live (`t6_send.py:797-801`) and re-reads the backup row just before the claim; not suspended means not sent plus an alert, and t11 re-pauses it next run and blocks that day (`repaused_today`). Residual: a person un-suspending during the minutes of export/send, after the live check. Unfixable without a Tableau-side lock; low likelihood.
- Subscription flag versus schedule state: the code treats them as separate. A suspended SCHEDULE is read from `Tableau_Schedule.State`; t6 returns `schedule_suspended` (quiet), t11 refuses to take such a subscription over (honour mode only, F-6). A subscription that is itself suspended on an ACTIVE schedule is the normal takeover case and Tableau sends nothing for it. A subscription that is suspended AND on a suspended schedule is not taken over, so it stays silent. No double-mail path.
- Human un-suspend between takeover and send: covered, see above. Restore then a native send the same day: not covered (F-4).
- Takeover-day guard for previously suspended subscriptions: correct to avoid a double mail, wrong in the sense of F-1 (a missed monthly report).
- Owner fence and workbook allow-list: both still apply (`enabled`, owner check at `t11_pause.py:567`). They are weaker than before, F-2.

**The 22 monthly (due 2026-11-01) and 30 suspended now**
- Nothing is sent before your clean-up unless `t11 --execute` runs first, as above. Order of safety is: clean-up, T2 rerun, review list again, then allow-list. That order is documented (`RUNBOOK.md:263-272`) but not enforced; see F-2.
- Monthly ones are only due from 1 Nov, with a catch-up window to 4 Nov, so taking them over this week sends nothing this week.
- What stops all 54 at once: the takeover allow-list (empty = nothing), the due-date rule (only subscriptions due that day), `MAX_SENDS_PER_RUN` (10 claims per run, throttle only), SEND_ALLOWED_RECIPIENTS in smtp mode. None of them is a cap on the day.

**Fail-open**
- `awaiting_sync` blocks; the gate is evaluated first. See F-3 for the two cases where staleness is not noticed at all.

**R4 SQL** — F-5. **review_list.py** — F-7. **Tests** — F-8.

## Suggested order of fixes

1. F-1: add the found-suspended fact, or write the "take over the 22 before 1 Nov 11:00" rule into the runbook now.
2. F-2: cap on new takeovers per run, and the owner fence outside Gmail.
3. F-3: store the read `updated_at` in T2 and compare to it in t6.
4. F-4, F-5, then the Low items.
