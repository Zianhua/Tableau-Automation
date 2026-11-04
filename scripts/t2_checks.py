"""T2 checks (dev DWH, read-only queries; the rerun check invokes t2_mapping_sync.py, which uses the PAT).

1. Counts against docs/d1b_findings.md (38 workbooks, 54 subscriptions, 11 published extracts, 90 links).
2. Constraint spot checks (no recipients column, no orphan links, extract flags come from the published source).
3. Idempotency: snapshot the four tables (without LastSeenAt), run the sync again, compare snapshots.
Usage: python scripts/t2_checks.py [--no-rerun]
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import t2_mapping_sync as m  # noqa: E402

TABLES = {
    "Tableau_DataSource": "DataSource_id, DataSource_Name, LinkedPublishedDatasourceId, IsExtract, Active_Flag",
    "Tableau_WorkbookDataSource": "Workbook_id, Workbook_Name, DataSource_id, Active_Flag",
    "Tableau_DataSourceTable": "DataSource_id, SchemaName, TableName",
    "Tableau_Subscription": "Subscription_id, Workbook_id, TargetType, TargetId, Format, Subject, Message, PageSize, "
                            "Orientation, IsSuspended, ScheduleType, SubscriptionKeyHash, Active_Flag",
}
fails = []


def check(name, ok, detail=""):
    print("%s  %s %s" % ("PASS" if ok else "FAIL", name, detail))
    if not ok:
        fails.append(name)


def scalar(cur, sql, *a):
    return cur.execute(sql, *a).fetchone()[0]


def snapshot(cur):
    h = {}
    for t, cols in TABLES.items():
        rows = sorted(repr(tuple(r)) for r in cur.execute("SELECT %s FROM Integration.%s" % (cols, t)).fetchall())
        h[t] = (len(rows), hashlib.sha256("\n".join(rows).encode()).hexdigest()[:16])
    return h


def expected():
    """Expected counts come from docs/d1b_findings.json (read-only reference run), compared with a small tolerance
    because UAT can drift. Returns name -> (value, tolerance)."""
    d = json.loads((m.ROOT / "docs" / "d1b_findings.json").read_text(encoding="utf-8"))
    c = d["counts"]
    pub_ext = {p["luid"] for ds in d["data_sources"] for p in ds.get("parents", []) if p.get("meta_hasExtracts")}
    return {"workbooks": (c["workbooks"], 1), "subscriptions": (c["subscriptions"], 2),
            "suspended subscriptions": (c["suspended_subscriptions"], 2), "workbook-datasource links": (c["data_sources"], 2),
            "extract links": (c["extract_data_sources"], 2), "published extracts": (len(pub_ext), 1)}


FULL_SCOPE = {"yes": True}   # set in main(): False when dev holds only the small UAT test set (D-052), see near()


def near(name, got, exp):
    v, tol = exp[name]
    if not FULL_SCOPE["yes"]:
        print("SKIP  %s ~ %d (+/-%d): dev holds the small UAT test scope (got %d); the d1b baseline is for the full scope" % (name, v, tol, got))
        return
    check("%s ~ %d (+/-%d), got %d" % (name, v, tol, got), abs(got - v) <= tol)


def check_published_flags(cur):
    """IsExtract of every linked/direct source must equal the published source's flag on Tableau (REST)."""
    import tableauserverclient as TSC
    rows = cur.execute("SELECT DataSource_id, LinkedPublishedDatasourceId, CAST(IsExtract AS int) "
                       "FROM Integration.Tableau_DataSource WHERE Active_Flag = 1").fetchall()
    pub_ids = {r[1] or r[0] for r in rows if r[1]}
    auth = TSC.PersonalAccessTokenAuth(m.os.environ["TABLEAU_PAT_NAME"], m.os.environ["TABLEAU_PAT_SECRET"],
                                       m.os.environ.get("TABLEAU_SITE_ID", ""))
    server = TSC.Server(m.os.environ["TABLEAU_SERVER_URL"], use_server_version=True)
    flags = {}
    with server.auth.sign_in(auth):
        for luid in pub_ids:
            flags[luid] = server.datasources.get_by_id(luid).has_extracts
    bad = [r[0] for r in rows if r[1] and (flags[r[1]] is None or int(bool(flags[r[1]])) != r[2])]
    check("IsExtract of %d linked sources equals the published source flag on Tableau" % sum(1 for r in rows if r[1]),
          not bad, "mismatch: %s" % bad[:3])


def child_env():
    """Environment for the t2 subprocess: it must not send real mail or write real markers/logs, whatever .env says."""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="t2checks_")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SMTP_", "SES_")) and k not in
           ("SEND_ALLOWED_RECIPIENTS", "SEND_ALLOW_ALL_UAT", "SEND_FROM", "ALERT_TO", "ADDR_HASH_SALT", "TABLEAU_AUTOMATION_DISABLED")}
    env.update(MAIL_TRANSPORT="local", LASTRUN_DIR=tmp, OPS_STATE_DIR=tmp, LOG_DIR=tmp, EVENTLOG_DISABLED="1")
    return env


def main():
    os.environ["EVENTLOG_DISABLED"] = "1"
    cur = m.connect().cursor()
    exp = expected()
    n_wb = scalar(cur, "SELECT COUNT(DISTINCT Workbook_id) FROM Integration.Tableau_WorkbookDataSource WHERE Active_Flag=1")
    FULL_SCOPE["yes"] = n_wb >= exp["workbooks"][0] - 5
    if not FULL_SCOPE["yes"]:
        print("INFO  dev holds %d active workbooks (baseline %d): baseline count checks are skipped, the other checks use the data itself" % (n_wb, exp["workbooks"][0]))
    near("workbooks", scalar(cur, "SELECT COUNT(DISTINCT Workbook_id) FROM Integration.Tableau_WorkbookDataSource WHERE Active_Flag=1"), exp)
    check("every subscribed workbook has links", scalar(cur, "SELECT COUNT(DISTINCT s.Workbook_id) FROM Integration.Tableau_Subscription s WHERE s.Active_Flag=1 AND NOT EXISTS (SELECT 1 FROM Integration.Tableau_WorkbookDataSource w WHERE w.Workbook_id=s.Workbook_id AND w.Active_Flag=1)") == 0)
    near("subscriptions", scalar(cur, "SELECT COUNT(*) FROM Integration.Tableau_Subscription WHERE Active_Flag=1"), exp)
    near("suspended subscriptions", scalar(cur, "SELECT COUNT(*) FROM Integration.Tableau_Subscription WHERE Active_Flag=1 AND IsSuspended=1"), exp)
    near("workbook-datasource links", scalar(cur, "SELECT COUNT(*) FROM Integration.Tableau_WorkbookDataSource WHERE Active_Flag=1"), exp)
    near("extract links", scalar(cur, "SELECT COUNT(*) FROM Integration.Tableau_WorkbookDataSource w JOIN Integration.Tableau_DataSource d ON d.DataSource_id=w.DataSource_id WHERE w.Active_Flag=1 AND d.IsExtract=1"), exp)
    near("published extracts", scalar(cur, "SELECT COUNT(DISTINCT LinkedPublishedDatasourceId) FROM Integration.Tableau_DataSource WHERE Active_Flag=1 AND IsExtract=1 AND LinkedPublishedDatasourceId IS NOT NULL"), exp)
    nulls = scalar(cur, "SELECT COUNT(*) FROM Integration.Tableau_DataSource WHERE Active_Flag=1 AND IsExtract IS NULL")
    unk = scalar(cur, "SELECT COUNT(*) FROM Integration.vw_Tableau_DataSourceReadiness WHERE ReadyStatus='UnknownKind'")
    check("unknown IsExtract fails closed (%d NULL, %d shown UnknownKind)" % (nulls, unk), nulls == unk)
    if nulls:
        print("WARN  %d data sources have unknown IsExtract; review before relying on them" % nulls)
    check_published_flags(cur)
    check("every subscription has a key hash", scalar(cur, "SELECT COUNT(*) FROM Integration.Tableau_Subscription WHERE SubscriptionKeyHash IS NULL") == 0)
    check("no email/recipient column", scalar(cur, "SELECT COUNT(*) FROM sys.columns c JOIN sys.tables t ON t.object_id=c.object_id WHERE t.name LIKE 'Tableau[_]%' AND (c.name LIKE '%mail%' OR c.name LIKE '%ecipient%')") == 0)
    at = scalar(cur, "SELECT COUNT(*) FROM Integration.Tableau_Subscription WHERE Message LIKE '%@%'")
    print("%s  subscriptions whose Message contains '@': %d (flag only; nothing changed)" % ("WARN" if at else "INFO", at))
    check("no '@' in Subject", scalar(cur, "SELECT COUNT(*) FROM Integration.Tableau_Subscription WHERE Subject LIKE '%@%'") == 0)
    check("no duplicate upstream rows", scalar(cur, "SELECT COUNT(*) FROM (SELECT DataSource_id,SchemaName,TableName FROM Integration.Tableau_DataSourceTable GROUP BY DataSource_id,SchemaName,TableName HAVING COUNT(*)>1) x") == 0)
    check("readiness view covers every in-scope workbook",
          scalar(cur, "SELECT COUNT(*) FROM Integration.vw_Tableau_WorkbookReadiness") >= scalar(cur, "SELECT COUNT(DISTINCT Workbook_id) FROM Integration.Tableau_WorkbookDataSource WHERE Active_Flag=1"))
    # --- custom SQL (C1, D-024, D-030, D-031) ---
    d1b = json.loads((m.ROOT / "docs" / "d1b_findings.json").read_text(encoding="utf-8"))
    cust = {ds["wb_luid"] for ds in d1b["data_sources"]
            if ds.get("containsUnsupportedCustomSql") or any(p.get("containsUnsupportedCustomSql") for p in ds.get("parents", []))}
    status = {r[0]: (r[1], r[2]) for r in cur.execute(
        "SELECT r.Workbook_id, r.WorkbookStatus, (SELECT TOP 1 Workbook_Name FROM Integration.Tableau_WorkbookDataSource w "
        "WHERE w.Workbook_id = r.Workbook_id) FROM Integration.vw_Tableau_WorkbookReadiness r "
        "WHERE EXISTS (SELECT 1 FROM Integration.Tableau_WorkbookDataSource w2 WHERE w2.Workbook_id = r.Workbook_id AND w2.Active_Flag = 1)")}   # in-scope = active links
    check("custom-SQL workbooks from d1b json: %d (expect about 17)" % len(cust), abs(len(cust) - 17) <= 1)
    in_scope_cust = {w for w in cust if w in {r[0] for r in cur.execute("SELECT DISTINCT Workbook_id FROM Integration.Tableau_WorkbookDataSource WHERE Active_Flag=1")}}
    notready_cust = [status[w][1] for w in in_scope_cust if w in status and status[w][0] != "Ready"]
    if FULL_SCOPE["yes"]:
        check("every custom-SQL workbook present and Ready", all(w in status for w in cust) and not notready_cust, "not ready: %s" % notready_cust)
    else:
        check("every custom-SQL workbook that is in the small dev scope is Ready (%d of %d in scope)" % (len(in_scope_cust), len(cust)),
              not notready_cust, "not ready: %s" % notready_cust)
    nr = {w: v[1] for w, v in status.items() if v[0] != "Ready"}
    # D-053: Excel/CSV-only workbooks are no longer NotReady, so there is no expected count: derive it from the data.
    check("NotReady workbooks (%d) are not custom-SQL ones" % len(nr), not (set(nr) & cust), sorted(str(x) for x in nr.values()))
    check("every NotReady workbook has a source that is not ready (not Ready, NoReadinessCheck or NotChecked)",
          all(scalar(cur, "SELECT COUNT(*) FROM Integration.Tableau_WorkbookDataSource w JOIN Integration.vw_Tableau_DataSourceReadiness r "
                          "ON r.DataSource_id = w.DataSource_id WHERE w.Workbook_id = ? AND w.Active_Flag = 1 "
                          "AND r.ReadyStatus NOT IN ('Ready', 'NoReadinessCheck', 'NotChecked')", wid) > 0
              or scalar(cur, "SELECT COUNT(*) FROM Integration.vw_Tableau_WorkbookReadiness WHERE Workbook_id = ? AND WorkbookStatus IN ('InactiveDataSource', 'NoDataSources')", wid) > 0
              for wid in nr))
    check("NotChecked sources are exactly the flagged ones without a DWH table",
          scalar(cur, "SELECT COUNT(*) FROM Integration.vw_Tableau_DataSourceReadiness r JOIN Integration.Tableau_DataSource d ON d.DataSource_id = r.DataSource_id "
                      "WHERE CASE WHEN r.ReadyStatus = 'NotChecked' THEN 1 ELSE 0 END <> "
                      "CASE WHEN d.NonDwhConnection = 1 AND r.TableCount = 0 AND r.ReadyStatus NOT IN ('NoReadinessCheck', 'UnknownKind') THEN 1 ELSE 0 END") == 0)
    near("Ready workbooks", len(status) - len(nr), {"Ready workbooks": (exp["workbooks"][0], 5)})
    check("registry row Integration.MultipleAccounts_Staging -> DailyETL", scalar(cur, "SELECT COUNT(*) FROM Integration.Tableau_PipelineTable WHERE SchemaName='Integration' AND TableName='MultipleAccounts_Staging' AND PipelineName='CQGlobalSGDW: DailyETL'") == 1)
    if scalar(cur, "SELECT COUNT(*) FROM Integration.vw_Tableau_DataSourceReadiness WHERE DataSource_Name='parent acc with trade'") == 0:
        print("SKIP  'parent acc with trade' is not in the current dev scope")
    else:
        check("'parent acc with trade' uses parsed SQL tables (DailyETL only, not the spurious Nova view)",
              scalar(cur, "SELECT COUNT(*) FROM Integration.vw_Tableau_DataSourceReadiness WHERE DataSource_Name='parent acc with trade' AND RequiredPipelines='CQGlobalSGDW: DailyETL'") == 1)
    check("no unparsed-SQL placeholder rows stored", scalar(cur, "SELECT COUNT(*) FROM Integration.Tableau_DataSourceTable WHERE SchemaName = ?", m.UNPARSED_SCHEMA) == 0)
    # parser and fail-closed placeholder (no DB write, no Tableau access)
    ok_sql = "SELECT a FROM [CQGlobalSGDW].[Tableau].[vw_x] x LEFT JOIN Fact.Y y ON 1=1 WHERE d=(SELECT MAX(r) FROM Fact.Y)"
    check("parser: reads db-qualified and plain names", m.parse_custom_sql(ok_sql)[0] == {("Tableau", "vw_x"), ("Fact", "Y")})
    okfn = "SELECT ISNULL(a, 0), CAST(a AS int), MAX(a) FROM Fact.Y WHERE a IN (1, 2) AND EXISTS (SELECT 1 FROM Fact.Z z)"
    check("parser: built-in functions are accepted", m.parse_custom_sql(okfn)[0] == {("Fact", "Y"), ("Fact", "Z")})
    bad = ["EXEC dbo.p", "SELECT * FROM #t", "SELEC FROM", "SELECT 1; SELECT 2", "SELECT * FROM [Other].[dbo].[t]",
           "SELECT * FROM a.b.c.d", "SELECT * FROM tbl", "WITH c AS (SELECT 1 x) SELECT * FROM c", "",
           "DECLARE @a int; SELECT * FROM Fact.Y",
           "SELECT a FROM Fact.Y y CROSS APPLY dbo.tvf(y.a) t", "SELECT dbo.fnX(a) FROM Fact.Y",
           "SELECT a FROM Fact.Y WHERE dbo.f(a) = 1", "SELECT a FROM Fact.fnTvf(1)",
           "SELECT a FROM Fact.Y UNION SELECT a FROM Fact.Z",
           "SELECT a FROM Fact.Y WHERE a IN (SELECT a FROM Fact.Z UNION ALL SELECT a FROM Fact.W)",
           "SELECT a FROM OPENQUERY(x, 'q')", "SELECT a FROM OPENROWSET('x', 'y', 'z')",
           "SELECT a INTO dbo.newt FROM Fact.Y", "SELECT a INTO #t FROM Fact.Y",
           "SELECT a FROM Fact.Y WHERE b = <Parameters.P>", "SELECT a FROM Fact.Y WHERE b = @p",
           "SELECT [a'b] FROM Fact.Y; EXEC xp_cmdshell 'dir' -- it's",
           "SELECT 'x' FROM Fact.Y; -- it's\nEXEC sp_who",
           "SELECT [x'] FROM Fact.Y /* ' */ WHERE 1 = 1; DROP TABLE Fact.Y"]
    check("parser: every unsafe or unparsable string is rejected", all(m.parse_custom_sql(q)[0] is None for q in bad),
          "accepted: %s" % [q for q in bad if m.parse_custom_sql(q)[0] is not None])
    fake = {"x1": {"name": "fake", "linked": None, "is_extract": True, "tables": [], "sql": ["EXEC dbo.p 1; SELECT * FROM Fact.Y"]}}
    rows = m.build_rows(fake, {}, set(), [], m.Resolver(cur))[1]
    check("unparsable custom SQL stores an unregisterable placeholder (source would be UnmappedTable)",
          len(rows) == 1 and rows[0][1] == m.UNPARSED_SCHEMA and scalar(cur, "SELECT COUNT(*) FROM Integration.Tableau_PipelineTable WHERE SchemaName = ?", m.UNPARSED_SCHEMA) == 0)
    fake2 = {"x2": {"name": "flagged", "linked": None, "is_extract": True, "flag": True, "sql_error": None, "tables": [], "sql": None}}
    r2 = m.build_rows(fake2, {}, set(), [], m.Resolver(cur))[1]
    check("source flagged as custom SQL with no SQL attached gets a placeholder", len(r2) == 1 and r2[0][1] == m.UNPARSED_SCHEMA)
    fake3 = {"x3": {"name": "mixed", "linked": None, "is_extract": True, "flag": True, "sql_error": None, "union": False,
                    "tables": [{"name": "Account", "schema": "Dimension", "connectionType": "sqlserver", "database": {"name": "cqglobalsgdw", "connectionType": "sqlserver"}}],
                    "sql": ["SELECT a FROM Dimension.AE"]}}
    r3 = m.build_rows(fake3, {}, set(), [], m.Resolver(cur))[1]
    check("Metadata tables missing from the parsed SQL give a placeholder (not on the allow-list)",
          len(r3) == 1 and r3[0][1] == m.UNPARSED_SCHEMA)
    # D-053: a data source is flagged non-DWH only when EVERY skipped connection is positively named as non-DWH
    def csv_tbl(name="Users.csv", ctype="textscan", db=None):
        return {"name": name, "schema": None, "connectionType": ctype, "database": {"name": db, "connectionType": ctype} if db else None}
    dwh_tbl = {"name": "Account", "schema": "Dimension", "connectionType": "sqlserver", "database": {"name": "cqglobalsgdw", "connectionType": "sqlserver"}}

    def flagged(tables, **kw):
        src = {"id1": dict({"name": "f", "linked": None, "is_extract": False, "tables": tables, "sql": None}, **kw)}
        out = m.build_rows(src, {}, set(), [], m.Resolver(cur))
        return "id1" in out[3]["nondwh_ds_ids"], out[1]
    f, rows = flagged([csv_tbl()])
    check("non-DWH: a CSV-only source is flagged, no table rows", f and rows == [])
    f, rows = flagged([csv_tbl("a.xlsx", "excel-direct"), csv_tbl()])
    check("non-DWH: several file connections are flagged", f)
    f, rows = flagged([csv_tbl("t", "oracle", "ORCL")])
    check("non-DWH: another database engine is flagged", f)
    f, rows = flagged([csv_tbl("t", "sqlserver", "SomeOtherDb")])
    check("non-DWH: SQL Server in ANOTHER database is flagged", f)
    f, rows = flagged([csv_tbl(), dwh_tbl])
    check("non-DWH: file + DWH table is flagged AND keeps its DWH table rows (waits on them)", f and len(rows) >= 1)
    f, rows = flagged([])
    check("non-DWH: no tables at all (read failed) is NOT flagged", not f and rows == [])
    f, rows = flagged([csv_tbl("x", "")])
    check("non-DWH: a table with no connection type is NOT flagged (not positively identified)", not f)
    f, rows = flagged([csv_tbl("x", "sqlproxy")])
    check("non-DWH: a published-source proxy is NOT evidence", not f)
    f, rows = flagged([csv_tbl("x", "hyper")])
    check("non-DWH: a Tableau extract file type is NOT evidence", not f)
    f, rows = flagged([csv_tbl(), csv_tbl("y", "")])
    check("non-DWH: one positive file plus one unidentifiable table is NOT flagged (fail closed)", not f)
    f, rows = flagged([csv_tbl()], flag=True, sql_error=None, union=False, sql=["SELECT a FROM Dimension.AE"])
    check("non-DWH: a custom-SQL source is never flagged", not f)
    f, rows = flagged([csv_tbl()], flag=True, sql_error=None, union=False)
    check("non-DWH: custom SQL flagged without SQL stays unflagged with its placeholder", not f and len(rows) == 1 and rows[0][1] == m.UNPARSED_SCHEMA)
    f, rows = flagged([csv_tbl("x", "genericodbc")])
    check("non-DWH: a generic ODBC connection is NOT evidence (it can point at our DWH)", not f)
    for ct in ("otherodbc", "odbc", "jdbc", "oledb", "msolap", "sqlserver-odbc", "somethingnew"):
        check("non-DWH: connection type %r is not on the allow-list, so not evidence" % ct, not m.is_positive_non_dwh(csv_tbl("x", ct)))
    for ct in ("Excel-Direct", "textscan", "CSV", "postgres", "oracle", "snowflake"):
        check("non-DWH: connection type %r is on the allow-list" % ct, m.is_positive_non_dwh(csv_tbl("x", ct)))
    check("non-DWH: SQL Server database name with brackets/quotes/spaces is normalised to ours (still DWH, not flagged)",
          all(m.is_dwh_table(csv_tbl("t", "sqlserver", d)) and not m.is_positive_non_dwh(csv_tbl("t", "sqlserver", d))
              for d in ("[CQGlobalSGDW]", " cqglobalsgdw ", '"CQGlobalSGDW"', "'CQGlobalSGDW'", "[ CQGlobalSGDW ]")))
    check("non-DWH: a bracketed OTHER database name is still positively another database",
          m.is_positive_non_dwh(csv_tbl("t", "sqlserver", "[OtherDb]")) and not m.is_dwh_table(csv_tbl("t", "sqlserver", "[OtherDb]")))
    check("non-DWH: an odd SQL Server database name (not a plain name) is NOT evidence (fail closed)",
          not m.is_positive_non_dwh(csv_tbl("t", "sqlserver", "Other;Db")) and not m.is_positive_non_dwh(csv_tbl("t", "sqlserver", "")))
    check("non-DWH: is_positive_non_dwh needs a name and a named type", not m.is_positive_non_dwh({"name": None, "connectionType": "textscan"})
          and not m.is_positive_non_dwh({}) and m.is_positive_non_dwh({"name": "a", "connectionType": "Excel-Direct"}))
    print("\nWorkbook readiness:")
    for r in cur.execute("SELECT WorkbookStatus, COUNT(*) FROM Integration.vw_Tableau_WorkbookReadiness GROUP BY WorkbookStatus"):
        print("  %-20s %d" % (r[0], r[1]))
    print("Data source readiness (active):")
    for r in cur.execute("SELECT ReadyStatus, COUNT(*) FROM Integration.vw_Tableau_DataSourceReadiness GROUP BY ReadyStatus"):
        print("  %-20s %d" % (r[0], r[1]))

    if "--no-rerun" not in sys.argv:
        before = snapshot(cur)
        env = child_env()
        res = subprocess.run([sys.executable, str(Path(__file__).with_name("t2_mapping_sync.py")), "--local-smtp"],
                             capture_output=True, text=True, env=env)
        check("rerun exits 0", res.returncode == 0, res.stderr[-200:])
        after = snapshot(m.connect().cursor())
        check("rerun changes nothing (row counts and content hash, LastSeenAt excluded)", before == after, "" if before == after else "%s -> %s" % (before, after))
        dry = subprocess.run([sys.executable, str(Path(__file__).with_name("t2_mapping_sync.py")), "--dry-run", "--local-smtp"],
                             capture_output=True, text=True, env=env)
        lines = [l for l in dry.stdout.splitlines() if l.strip().startswith("Tableau_")]
        zero = len(lines) == 5 and all(int(n) == 0 for l in lines for n in re.findall(r"(?:insert|update|deactivate|delete)\s+(\d+)", l))
        check("dry-run after a run reports no inserts or updates", dry.returncode == 0 and zero)
        check("dry-run wrote nothing", snapshot(m.connect().cursor()) == after)
    print("\n%s" % ("ALL PASSED" if not fails else "FAILED: " + ", ".join(fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
