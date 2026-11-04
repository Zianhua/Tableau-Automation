"""T2: mapping sync. Tableau UAT (read-only) -> dev DWH Integration.Tableau_* mapping tables.

Reads from Tableau (REST + Metadata API, PAT from .env) and from the dev DWH (view -> base table resolution).
Writes ONLY to the dev DWH (D-026), only into: Tableau_Subscription, Tableau_DataSource,
Tableau_WorkbookDataSource, Tableau_DataSourceTable. Never writes to Tableau. Stores no recipients.

Scope: workbooks that have at least one subscription AND more than one data source (D-007), i.e. the
subscribed affected workbooks (38 on UAT). All subscriptions of those workbooks are stored, suspended ones
included (flagged IsSuspended for information only: suspended is no special category, D-046).

Rules (T1 header, D1b):
- DataSource_id = workbook-side id (embedded id, or the published luid when used directly). A link to a
  published source sets LinkedPublishedDatasourceId; IsExtract comes from that PUBLISHED source, not the shell.
- Upstream tables: views are resolved to base tables in the DWH (sys.sql_expression_dependencies). Rows are stored
  under the published luid for linked sources, else under the source's own id. Only DWH tables are stored
  (SQL Server, database CQGlobalSGDW or none, D-010); file and other-database tables are counted and skipped.
  Custom SQL sources return no tables from the Metadata API and stay NoTables (D-024).
- Custom SQL (D-024, D-030): the Metadata API cannot list tables for custom SQL, so the query text is read from
  customSQLTables, attached to its published source (downstreamDatasources) or, for embedded sources, the
  workbook's unattached queries go to its parentless SQL Server embedded sources. Each query is parsed with sqlglot
  (T-SQL) and views are resolved like any other table. For a source with custom SQL the Metadata upstreamTables
  list is IGNORED. A query that does not parse, has several statements, a CTE, EXEC/#temp/OPENQUERY, or a
  name outside CQGlobalSGDW yields an unregisterable placeholder row, so the source stays UnmappedTable (fail closed).
  Tableau parameter placeholders (<Parameters.X>) do not parse and therefore fail closed too.
  A source Tableau flags as containing custom SQL (containsUnsupportedCustomSql) with no SQL attached fails closed.
  If Metadata lists tables that the parsed SQL does not contain, the source fails closed unless it is on
  ALLOW_IGNORE_METADATA (with the evidence written next to it).
- Idempotent: MERGE per table. Rows no longer in Tableau get Active_Flag = 0 (never deleted); upstream-table
  rows of a seen data source are replaced.

Scope (R1, D-038): derived LIVE from Tableau on every run, never from a fixed list: every workbook that has at least one
subscription and more than one data source. A subscription created after the first inventory (for example the "CFD Accounts v2"
test subscription on a workbook with 2 data sources) is picked up by the next run.

Schedules (R1, D-036): for every subscription the REST schedule object is read (frequency, weekdays, month day(s), start time,
interval, state) into Integration.Tableau_Schedule, and Tableau_Subscription.ScheduleId. t6/t4 use it to decide what is due.
Apply scripts/sql/R1_schedule_schema.sql and scripts/sql/R5_workbook_updated_at.sql first (the script stops with a message if missing).
R5/F-3: Tableau_Subscription.WorkbookUpdatedAt = the workbook updated_at as read from the REST list at the start of the run (naive UTC);
t6 compares the live updated_at with it.

Resilience (R1, F-08/F-09): one odd workbook, view, subscription or schedule is logged and SKIPPED; its existing rows stay
active and keep their old LastSeenAt, so after MAPPING_MAX_AGE_H (default 26 h) t6 stops sending them and alerts (that is the
"stale" flag); nothing is deactivated because a read failed. Exit code 0 = everything read; 2 = written, but some items were skipped
(alert); 1 = failed entirely, nothing written (alert). The shared PAT applock (TABLEAU_PAT_SESSION) is taken before sign-in
(retried T2_LOCK_RETRIES x 30 s). Log: out/t2_sync_<date>.log (one file per day, kept 7 days) with one RUN SUMMARY line. Calls are paced (T2_PACE_SEC between workbooks,
Metadata page size T2_METADATA_PAGE). SCHEDULING (D-046): run T2 ONCE A DAY at about 07:50, before the 08:30-16:30 cycle of t11, t4 and t6. A workbook
republished after the sync waits for the next one (t6 logs it quietly as awaiting_sync, no alert). If T2 fails, rerun it by hand.

Lagging Metadata (R1 review #9): the Metadata API indexes new content late. A workbook whose data source count dropped against the
stored active links, or whose tables dropped for a source without custom SQL, is SKIPPED (rows stay active, go stale, alert, exit 2)
unless --accept-drops. So the required-pipeline set (derived from those tables) can only shrink through an accepted drop.

Registry (D-066): after the mapping tables are written, registry_sync derives the pipeline of every base table the mapped data sources read
from the lineage tables and keeps the 'Auto' rows of Integration.Tableau_PipelineTable in step (new table, moved table); a 'Manual' row is
the override and is never changed. A dry run prints the difference and writes nothing. Tables with no or several candidate pipelines are
reported and stay unmapped (their extracts are not refreshed).
Extract refresh tasks (D-067): the Tableau extract refresh tasks and their schedules are read (read-only) into Integration.Tableau_ExtractTask
and Integration.Tableau_Schedule; t4 uses them for the extracts whose task extract_takeover.py has taken over.
Needs scripts/sql/ExtractAuto_schema.sql.

Usage:
  python scripts/t2_mapping_sync.py --dry-run     # read everything, print what would change, write nothing
  python scripts/t2_mapping_sync.py               # real run on the dev DWH
  python scripts/t2_mapping_sync.py --ses         # alert mails (failure, partial) through SES instead of the local test SMTP
Only one script may use the PAT at a time.
"""
import argparse
import hashlib
import os
import logging
import re
import sys
import time
from pathlib import Path

import pyodbc
import sqlglot
import tableauserverclient as TSC
from sqlglot import expressions as E
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
import schedule_due as sd  # noqa: E402
import t4_refresh as t4  # noqa: E402
import ops  # noqa: E402
import extract_tasks as et  # noqa: E402
import registry_sync as rs  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
log = logging.getLogger("t2_sync")


class DropError(RuntimeError):
    """The workbook shrank against what is stored: probably a lagging Metadata response, not a real change."""
PACE_SEC = float(os.environ.get("T2_PACE_SEC", 0.5))             # pause between two workbooks' Metadata queries
METADATA_PAGE = int(os.environ.get("T2_METADATA_PAGE", 200))     # page size for the customSQLTables listing
LOCK_RETRIES = int(os.environ.get("T2_LOCK_RETRIES", 10))        # PAT lock busy: retries, 30 s apart
LOCK_WAIT_S = 30
T2_MAX_LOCK_MIN = float(os.environ.get("T2_MAX_LOCK_MIN", 10))   # time cap while T2 holds the shared Tableau login; the rest is read at the next run (daily: raise it, e.g. 20, if the scope grows)

# Dev DWH guard: the only server/database this script may touch. From .env; unset = refuse to run (fail closed).
DEV_SERVER = (os.environ.get("DEV_DWH_SERVER") or "").strip().lower()
DEV_DB = (os.environ.get("DEV_DWH_DATABASE") or "").strip().lower()
DWH_DB = "cqglobalsgdw"

TBL_FIELDS = "name schema database{ name connectionType } connectionType"
WB_QUERY = """
query($luid:String!){
  workbooks(filter:{luid:$luid}){
    luid name
    embeddedDatasources{ id name hasExtracts containsUnsupportedCustomSql
      upstreamTables{ %(t)s }
      parentPublishedDatasources{ luid name hasExtracts containsUnsupportedCustomSql upstreamTables{ %(t)s } } }
    upstreamDatasources{ luid name hasExtracts containsUnsupportedCustomSql upstreamTables{ %(t)s } }
  }
}""" % {"t": TBL_FIELDS}

RESOLVE_SQL = """
WITH r AS (
  SELECT o.object_id, o.type FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id
  WHERE s.name = ? AND o.name = ?
  UNION ALL
  SELECT o2.object_id, o2.type FROM r
  JOIN sys.sql_expression_dependencies d ON d.referencing_id = r.object_id AND r.type = 'V'
  JOIN sys.objects o2 ON o2.object_id = d.referenced_id
)
SELECT DISTINCT s.name, o.name FROM r JOIN sys.objects o ON o.object_id = r.object_id
JOIN sys.schemas s ON s.schema_id = o.schema_id WHERE r.type = 'U'"""


def connect():
    server, db = os.environ["CONN_SERVER"], os.environ["CONN_DATABASE"]
    if not DEV_SERVER or not DEV_DB or server.lower() != DEV_SERVER or db.lower() != DEV_DB:
        sys.exit("Refusing to run: only the dev DWH (D-026) is allowed, got %s / %s" % (server, db))
    drv = os.environ["CONN_DRIVER"].strip("{}")
    conn = pyodbc.connect("Driver={%s};Server=%s;Database=%s;Trusted_Connection=yes;TrustServerCertificate=yes"
                          % (drv, server, db))
    sname, dname = conn.cursor().execute("SELECT @@SERVERNAME, DB_NAME()").fetchone()
    if not sname.lower().startswith(DEV_SERVER.split(".")[0]) or dname.lower() != DEV_DB:
        conn.close()
        sys.exit("Refusing to run: connected to %s / %s, not the dev DWH" % (sname, dname))
    return conn


def key_hash(workbook_id, target_type, target_id, owner_id, subject):
    """SubscriptionKeyHash: SHA-256 over workbook, target, owner (Tableau user id, no email), subject."""
    s = "|".join([workbook_id, target_type, target_id, owner_id or "", (subject or "").strip().lower()])
    return hashlib.sha256(s.encode("utf-8")).digest()


def norm_db(name):
    """A database name as Metadata gives it, without surrounding whitespace, brackets or quotes, lower case. None for nothing."""
    n = (name or "").strip()
    while len(n) >= 2 and (n[0], n[-1]) in (("[", "]"), ('"', '"'), ("'", "'"), ("`", "`")):
        n = n[1:-1].strip()
    return n.lower() or None


def is_dwh_table(t):
    db = norm_db((t.get("database") or {}).get("name"))
    ctype = ((t.get("database") or {}).get("connectionType") or t.get("connectionType") or "").strip().lower()
    return ctype == "sqlserver" and (not db or db == DWH_DB)


# D-053: the ONLY connection types that count as "positively not our DWH": files and database engines that cannot be our SQL Server DWH.
# Generic ODBC/JDBC/OLE DB, proxies, extract files and anything unknown are NOT on the list (a generic driver can point at our DWH).
# SQL Server itself counts only when the database name is a plain name other than ours.
POSITIVE_NON_DWH_CTYPES = frozenset((
    "excel-direct", "excel", "textscan", "csv", "text", "google-sheets", "googlesheets", "ogrdirect", "pdf", "json", "xml",
    "postgres", "postgresql", "mysql", "oracle", "snowflake", "bigquery", "redshift", "teradata", "saphana", "sap-hana", "vertica",
    "db2", "netezza", "mongodb", "salesforce", "sparksql", "databricks", "impala", "hive", "presto", "athena", "mariadb", "sybase"))
PLAIN_DB_NAME = re.compile(r"^[\w$#. -]+$")


def is_positive_non_dwh(t):
    """True only when Metadata POSITIVELY says this table is outside our DWH (allow-list above, or SQL Server with a plain database name
    other than ours). Missing, vague, generic-driver or unknown information is False (fail closed)."""
    db = t.get("database") or {}
    ctype = (db.get("connectionType") or t.get("connectionType") or "").strip().lower()
    if not ctype or not t.get("name"):
        return False
    if ctype == "sqlserver":
        n = norm_db(db.get("name"))
        return bool(n) and n != DWH_DB and bool(PLAIN_DB_NAME.match(n))
    return ctype in POSITIVE_NON_DWH_CTYPES


CUSTOM_SQL_QUERY = """{ customSQLTables { id query downstreamDatasources { luid } downstreamWorkbooks { luid } } }"""
# Checked on the raw text, string literals included: a false rejection only fails closed.
UNSAFE_SQL = re.compile(r"\b(exec|execute|sp_\w+|xp_\w+|openquery|openrowset|opendatasource|declare|apply|into|bulk)\b|#|@@",
                        re.I)
UNPARSED_SCHEMA = "(unparsed custom SQL)"
# Evidence per data source for ignoring Metadata's own table list when custom SQL is present (D-031).
ALLOW_IGNORE_METADATA = {
    "0789bd3a-9ba9-c3ce-53c1-5050ab1d6ec1": "parent acc with trade: the workbook XML (.twbx, 2026-10-23) of this data "
        "source never mentions vw_DailyNovaVolume; Metadata lists it only through an upstreamDatasources link",
}
_BAD_NODES = tuple(getattr(E, n) for n in ("Anonymous", "Dot", "Into", "Union", "Intersect", "Except", "Lateral",
                                          "Parameter", "Placeholder", "UserDefinedFunction", "Command") if hasattr(E, n))
_OK_FUNCS = tuple(getattr(E, n) for n in (
    "And", "Or", "Not", "Xor", "Max", "Min", "Sum", "Avg", "Count", "Coalesce", "Cast", "TryCast", "Abs", "Round",
    "Upper", "Lower", "Trim", "Length", "Year", "Month", "Day", "Concat", "Case", "If", "Exists", "Between",
    "Substring", "Floor", "Ceil", "DateAdd", "DateDiff", "CurrentTimestamp", "In") if hasattr(E, n))


def parse_custom_sql(sql):
    """Returns (set of (schema, table), None) or (None, reason). Strict on purpose: anything unusual -> reason.
    Allowed: one plain SELECT over schema-qualified tables/views of CQGlobalSGDW with built-in functions only."""
    if not sql or not sql.strip():
        return None, "empty query"
    if re.search(r"<\s*parameters\.", sql, re.I):
        return None, "contains a Tableau parameter placeholder"
    if UNSAFE_SQL.search(sql):
        return None, "contains EXEC/#temp/OPENQUERY/DECLARE/APPLY/INTO or similar"
    try:
        stmts = [x for x in sqlglot.parse(sql, read="tsql") if x is not None]
    except sqlglot.errors.SqlglotError as e:
        return None, "does not parse (%s)" % type(e).__name__
    if len(stmts) != 1 or not isinstance(stmts[0], E.Select):
        return None, "not exactly one SELECT statement"
    root = stmts[0]
    if root.find(E.With):
        return None, "contains a CTE"
    for node in root.walk():
        if isinstance(node, _BAD_NODES):
            return None, "contains %s (function call outside the allow-list, UNION, INTO, APPLY or parameter)" % type(node).__name__
        if isinstance(node, E.Func) and not isinstance(node, _OK_FUNCS):
            return None, "uses function %s, not on the allow-list" % type(node).__name__
    out = set()
    for t in root.find_all(E.Table):
        if not isinstance(t.this, E.Identifier) or not t.db:
            return None, "table name without schema or with an unsupported name form"
        if t.catalog and t.catalog.lower() != DWH_DB:
            return None, "database-qualified name outside CQGlobalSGDW"
        out.add((t.db, t.name))
    return (out, None) if out else (None, "no tables found")


BAD_DEP_SQL = """
WITH r AS (
  SELECT o.object_id, o.type FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id
  WHERE s.name = ? AND o.name = ?
  UNION ALL
  SELECT o2.object_id, o2.type FROM r
  JOIN sys.sql_expression_dependencies d ON d.referencing_id = r.object_id AND r.type = 'V'
  JOIN sys.objects o2 ON o2.object_id = d.referenced_id AND o2.type = 'V'
)
SELECT COUNT(*) FROM r JOIN sys.sql_expression_dependencies d ON d.referencing_id = r.object_id AND r.type = 'V'
LEFT JOIN sys.objects o3 ON o3.object_id = d.referenced_id
WHERE d.referenced_id IS NULL OR o3.object_id IS NULL OR o3.type NOT IN ('U', 'V')"""


class Resolver:
    """Views -> base tables, cached. A name that is not found, or a view that depends on anything other than
    tables and views (synonym, function, cross-database object, missing object), is kept as the object itself
    (plus any base tables found), so it is never in the registry and the source fails closed (UnmappedTable)."""

    def __init__(self, cur):
        self.cur, self.cache, self.unresolved = cur, {}, set()

    def base_tables(self, schema, name):
        k = (schema, name)
        if k not in self.cache:
            rows = {(r[0], r[1]) for r in self.cur.execute(RESOLVE_SQL, schema, name).fetchall()}
            bad = self.cur.execute(BAD_DEP_SQL, schema, name).fetchone()[0]
            if not rows or bad:
                self.unresolved.add(k)
                rows.add(k)
            self.cache[k] = rows
        return self.cache[k]


def read_custom_sql(server, page=METADATA_PAGE):
    """All customSQLTables through the Metadata API connection type, `page` per request (cursor paging; verified on UAT: the plain
    list does not accept first/offset). A page error aborts the run: a partial list would hide custom SQL."""
    out, cursor = [], None
    while True:
        q = ("query($a:String){ customSQLTablesConnection(first: %d, after: $a){ totalCount pageInfo{hasNextPage endCursor} "
             "nodes{ id query downstreamDatasources{ luid } downstreamWorkbooks{ luid } } } }" % page)
        r = server.metadata.query(q, {"a": cursor})
        if r.get("errors"):
            raise RuntimeError("GraphQL errors reading customSQLTables page: " + str(r["errors"])[:300])
        d = r["data"]["customSQLTablesConnection"]
        out += d["nodes"] or []
        if not d["pageInfo"]["hasNextPage"]:
            if len(out) != d["totalCount"]:
                raise RuntimeError("customSQLTables: read %d of %d, partial read" % (len(out), d["totalCount"]))
            return out
        cursor = d["pageInfo"]["endCursor"]
        time.sleep(PACE_SEC)


def collect_tableau(server, pace=None, stored=None, accept_drops=(), deadline=None):
    """Returns (subs, wb_names, sources, pub_tables, links, stats). Read-only. Recipients are never read.
    R1: a problem with one workbook, view, subscription or schedule is recorded in stats["problems"] and the item is skipped
    (stats["skipped_wbs"], stats["skipped_subs"]); only a failure that makes the whole picture unreliable raises."""
    pace = PACE_SEC if pace is None else pace
    problems, skipped_wbs, skipped_subs, schedules = [], set(), set(), {}
    wb_items = list(TSC.Pager(server.workbooks))
    wbs = {w.id: w.name for w in wb_items}
    # R5 / F-3: the workbook change time exactly as read HERE, before any Metadata query of that workbook. t6 compares the live
    # updated_at with this value (not with a clock), so a republish after this read is always noticed.
    wb_updated = {w.id: ops.utc_naive(getattr(w, "updated_at", None)) for w in wb_items}
    view_wb = {}  # view id -> workbook id, one GET per subscribed view (paging all views is slow and flaky)

    def workbook_of_view(vid):
        if vid not in view_wb:
            view_wb[vid] = server.views.get_by_id(vid).workbook_id
        return view_wb[vid]

    subs_raw = list(TSC.Pager(server.subscriptions))
    by_wb = {}
    for s in subs_raw:
        try:
            wid = s.target.id if s.target.type.lower() == "workbook" else workbook_of_view(s.target.id)
        except Exception as e:
            problems.append(("subscription", s.id, "target view could not be resolved (%s)" % type(e).__name__))
            skipped_subs.add(s.id)
            continue
        if wid:
            by_wb.setdefault(wid, []).append(s)

    pub_cache = {}

    def published_is_extract(luid):
        if luid not in pub_cache:
            try:
                v = server.datasources.get_by_id(luid).has_extracts
                pub_cache[luid] = None if v is None else bool(v)
            except Exception:
                pub_cache[luid] = None  # unknown -> fails closed
        return pub_cache[luid]

    cq_rows = read_custom_sql(server)
    sql_by_ds, sql_by_wb = {}, {}  # published luid -> queries; workbook luid -> queries not attached to a published source
    n_custom = len(cq_rows)
    for t in cq_rows:
        q = t["query"] or ""
        dsl = [d["luid"] for d in (t["downstreamDatasources"] or []) if d and d.get("luid")]
        for l in dsl:
            sql_by_ds.setdefault(l, []).append(q)
        if not dsl:
            for w in t["downstreamWorkbooks"] or []:
                if w and w.get("luid"):
                    sql_by_wb.setdefault(w["luid"], []).append(q)
    sources, links, subs, wb_names, pub_tables = {}, set(), [], {}, {}
    stats = {"workbooks_with_subs": len(by_wb), "skipped_single_source": 0, "dup_in_workbook": 0,
             "shell_unconfirmed": 0, "parents_disagree": 0, "attached_embedded": [],
             "custom_sql_tables": n_custom, "flagged_sources": 0}

    def read_workbook(wid):
        """Everything for one workbook into local containers; merged by the caller only if it all worked."""
        lsrc, llinks, lpub, lattached = {}, set(), {}, []
        lstats = {"dup": 0, "shell": 0, "disagree": 0, "flagged": 0}
        r = server.metadata.query(WB_QUERY, {"luid": wid})
        if r.get("errors"):
            raise RuntimeError("GraphQL errors for the workbook: " + str(r["errors"])[:200])
        nodes = r["data"]["workbooks"]
        if not nodes:
            raise RuntimeError("Metadata API returned nothing for this subscribed workbook (not indexed yet?)")
        n = nodes[0]
        emb, direct = n["embeddedDatasources"], n["upstreamDatasources"]
        ids = [e["id"] for e in emb] + [d["luid"] for d in direct]
        if len(ids) != len(set(ids)):
            lstats["dup"] += 1  # same id listed twice inside one workbook (expected 0)
        n_now = len(emb) + len(direct)
        if stored and stored.get(wid, 0) > n_now:
            if wid not in accept_drops:
                raise DropError("data sources dropped from %d stored to %d now (lagging Metadata response?); if real, rerun with --accept-drops %s"
                                % (stored[wid], n_now, wid))
            log.warning("ACCEPTED DROP (--accept-drops %s): workbook '%s' data sources %d -> %d%s", wid, wbs.get(wid, "?"), stored[wid], n_now,
                        "; it is now single-source, so it leaves the scope and its rows are deactivated" if n_now <= 1 else "")
        if n_now <= 1:
            return None
        conns = None  # REST connections of this workbook, fetched only if a source has no parent

        def conn_types(name):
            nonlocal conns
            if conns is None:
                wbi = server.workbooks.get_by_id(wid)
                server.workbooks.populate_connections(wbi)
                conns = [(c.datasource_name, (c.connection_type or "").lower()) for c in wbi.connections]
            return {t for nm, t in conns if nm == name}

        # Embedded custom SQL has no link to its source in the API, only to the workbook. Attach the workbook's
        # unattached queries to the parentless SQL Server embedded sources that Tableau flags as custom SQL, and
        # only if the numbers match (one query per candidate). Anything else fails closed in build_rows.
        unatt = list(dict.fromkeys(sql_by_wb.get(wid, [])))
        cands = [e["id"] for e in emb if not (e["parentPublishedDatasources"] or []) and e["containsUnsupportedCustomSql"]
                 and "sqlserver" in conn_types(e["name"])]
        for e in emb:
            parents = e["parentPublishedDatasources"] or []
            if len(parents) > 1:
                log.warning("embedded source %s has %d parents, the first is used for the link", e["id"], len(parents))
            linked = parents[0]["luid"] if parents else None
            for p in parents:
                lpub.setdefault(p["luid"], list(p["upstreamTables"] or []))
            # IsExtract from the published source(s) (REST, authoritative), not the workbook-side shell.
            if parents:
                flags = {published_is_extract(p["luid"]) for p in parents}
                if len(flags) > 1:
                    lstats["disagree"] += 1
                is_ext = next(iter(flags)) if len(flags) == 1 else None  # parents disagree -> Unknown
            else:
                # No parent: the shell flag is trusted only when REST confirms a real embedded connection of this
                # name (connection type is not sqlproxy). Otherwise Unknown (fails closed).
                types = conn_types(e["name"])
                if types and "sqlproxy" not in types and e["hasExtracts"] is not None:
                    is_ext = bool(e["hasExtracts"])
                else:
                    is_ext = None
                    lstats["shell"] += 1
            sql = [q for p in parents for q in sql_by_ds.get(p["luid"], [])]
            flag = any(p["containsUnsupportedCustomSql"] for p in parents) if parents else bool(e["containsUnsupportedCustomSql"])
            err = None
            if not parents and unatt:
                if e["id"] in cands:
                    if len(unatt) == len(cands):
                        sql = list(unatt)
                        lattached.append("%s / %s (%d queries for %d sources)" % (n["name"], e["name"], len(unatt), len(cands)))
                    else:
                        err = "%d unattached queries for %d candidate sources: cannot tell which is which" % (len(unatt), len(cands))
                elif not cands:
                    err = "workbook has unattached custom SQL but no embedded source is flagged as custom SQL"
            lstats["flagged"] += flag
            lsrc[e["id"]] = {"name": e["name"], "linked": linked, "is_extract": is_ext, "flag": flag, "sql_error": err,
                             "union": bool(sql) and not parents and len(cands) > 1,
                             "tables": list(e["upstreamTables"] or []), "sql": list(dict.fromkeys(sql)) or None}
            llinks.add((wid, e["id"]))
        for d in direct:
            lstats["flagged"] += bool(d["containsUnsupportedCustomSql"])
            lsrc[d["luid"]] = {"name": d["name"], "linked": None, "is_extract": published_is_extract(d["luid"]),
                               "flag": bool(d["containsUnsupportedCustomSql"]), "sql_error": None, "union": False,
                               "tables": list(d["upstreamTables"] or []),
                               "sql": list(dict.fromkeys(sql_by_ds.get(d["luid"], []))) or None}
            llinks.add((wid, d["luid"]))
        return n["name"], lsrc, llinks, lpub, lattached, lstats

    for wid in sorted(by_wb, key=lambda w: wbs.get(w, "")):
        if deadline is not None and time.monotonic() > deadline:
            problems.append(("workbook", wid, "not read: the T2 time cap (T2_MAX_LOCK_MIN) was reached"))
            skipped_wbs.add(wid)
            continue
        time.sleep(pace)   # paced: the Tableau server is not strong
        try:
            res = read_workbook(wid)
        except Exception as e:
            problems.append(("workbook", wid, "%s: %s" % (type(e).__name__, str(e)[:160])))
            skipped_wbs.add(wid)
            log.error("WORKBOOK SKIPPED %s ('%s'): %s: %s. Its existing rows stay active and go stale.", wid, wbs.get(wid, "?"),
                      type(e).__name__, str(e)[:160])
            continue
        if res is None:
            stats["skipped_single_source"] += 1
            continue
        name, lsrc, llinks, lpub, lattached, lst = res
        wb_names[wid] = name
        sources.update(lsrc)
        links |= llinks
        for k, v in lpub.items():
            pub_tables.setdefault(k, v)
        stats["attached_embedded"] += lattached
        stats["dup_in_workbook"] += lst["dup"]
        stats["shell_unconfirmed"] += lst["shell"]
        stats["parents_disagree"] += lst["disagree"]
        stats["flagged_sources"] += lst["flagged"]

    if stats["flagged_sources"] and n_custom == 0:
        raise RuntimeError("Sources are flagged as custom SQL but customSQLTables came back empty: partial read, aborting")
    for wid in wb_names:
        for s in by_wb[wid]:
            tt = "Workbook" if s.target.type.lower() == "workbook" else "View"
            fmt = ("PDFAndImage" if s.attach_pdf and s.attach_image else
                   "PDF" if s.attach_pdf else "Image" if s.attach_image else None)
            if fmt is None:
                problems.append(("subscription", s.id, "attaches neither PDF nor image"))
                skipped_subs.add(s.id)
                log.error("SUBSCRIPTION SKIPPED %s: attaches neither PDF nor image", s.id)
                continue
            if s.schedule_id and s.schedule_id not in schedules:
                try:
                    item = server.schedules.get_by_id(s.schedule_id)
                    schedules[s.schedule_id] = sd.from_tsc(item)
                except Exception as e:
                    schedules[s.schedule_id] = None
                    problems.append(("schedule", s.schedule_id, "%s: %s" % (type(e).__name__, str(e)[:120])))
                    log.error("SCHEDULE SKIPPED %s: %s: %s (the subscriptions on it stay on their old schedule row until it goes stale)",
                              s.schedule_id, type(e).__name__, str(e)[:120])
            sch = schedules.get(s.schedule_id)
            subs.append({"id": s.id, "wb": wid, "tt": tt, "tid": s.target.id, "fmt": fmt,
                         "subject": (s.subject or None) and s.subject[:500],
                         "message": (s.message or None) and s.message[:2000],
                         "psize": s.page_size_option, "orient": s.page_orientation,
                         "susp": bool(s.suspended),
                         "sched": (sch.name if sch else "")[:50] or None, "sched_id": s.schedule_id,
                         "hash": key_hash(wid, tt, s.target.id, s.user_id, s.subject), "wb_updated": wb_updated.get(wid)})
    stats.update(problems=problems, skipped_wbs=skipped_wbs, skipped_subs=skipped_subs,
                 schedules={k: v for k, v in schedules.items() if v is not None})
    return subs, wb_names, sources, pub_tables, links, stats


def detect_table_drops(stored_tables, table_rows, sources, links, accept=()):
    """{workbook id: reason}: workbooks that link a source (without custom SQL) whose stored upstream table count is larger than the new
    one. stored_tables: {upstream data source id: count}; table_rows: the new (owner id, schema, table) rows."""
    new = {}
    for owner, _, _ in table_rows:
        new[owner] = new.get(owner, 0) + 1
    out = {}
    for wid, dsid in links:
        src = sources.get(dsid)
        if wid in accept:
            continue
        if not src or src.get("sql") or src.get("flag") or src.get("sql_error"):
            continue
        owner = src["linked"] or dsid
        if stored_tables.get(owner, 0) > new.get(owner, 0):
            out[wid] = "tables of data source '%s' dropped from %d stored to %d now (lagging Metadata response?)" % (
                src["name"], stored_tables[owner], new.get(owner, 0))
    return out


def build_rows(sources, pub_tables, links, subs, resolver):
    """Flatten to the four tables' rows. Upstream tables: DWH only, views resolved to base tables."""
    ds_rows = {i: (i, s["name"], s["linked"], None if s["is_extract"] is None else int(s["is_extract"]))
               for i, s in sources.items()}
    table_rows, skipped = set(), {"non_dwh_or_file": 0, "mixed": {}, "null_schema": 0}
    stored, skipped_by = {}, {}
    nondwh_by, unsure_by = {}, {}   # D-053: owner -> tables positively outside the DWH / skipped tables we cannot place

    def add_tables(owner_id, tables):
        for t in tables:
            if not is_dwh_table(t) or not t.get("name"):
                skipped["non_dwh_or_file"] += 1
                skipped_by.setdefault(owner_id, set()).add(t.get("name") or "(no name)")
                (nondwh_by if is_positive_non_dwh(t) else unsure_by).setdefault(owner_id, set()).add(t.get("name") or "(no name)")
                continue
            if not t.get("schema"):  # never default silently: a placeholder row that can never be registered
                skipped["null_schema"] += 1
                resolver.unresolved.add((None, t["name"]))
                table_rows.add((owner_id, "(unknown schema)", t["name"]))
                stored[owner_id] = stored.get(owner_id, 0) + 1
                continue
            for sch, nm in resolver.base_tables(t["schema"], t["name"]):
                table_rows.add((owner_id, sch, nm))
                stored[owner_id] = stored.get(owner_id, 0) + 1

    skipped["custom_sql_sources"], skipped["custom_sql_failed"], skipped["custom_sql_notes"] = set(), [], []
    names = {i: s["name"] for i, s in sources.items()}
    for s in sources.values():
        if s["linked"]:
            names.setdefault(s["linked"], s["name"])

    def placeholder(owner_id, key, reason):
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        table_rows.add((owner_id, UNPARSED_SCHEMA, "sql_" + h))  # can never be registered -> fails closed
        skipped["custom_sql_failed"].append((names.get(owner_id, owner_id), reason))

    def base_set(tables):
        out = set()
        for t in tables:
            if is_dwh_table(t) and t.get("name") and t.get("schema"):
                out |= resolver.base_tables(t["schema"], t["name"])
        return out

    # Sources that have, or should have, custom SQL are handled here, grouped by owner (published luid or own id).
    # Linked sources: the Metadata API repeats the published source's tables on the workbook-side shell, so
    # store them once under the published luid (UpstreamDataSourceId).
    owners = {}
    for i, s in sources.items():
        if s.get("sql") or s.get("flag") or s.get("sql_error"):
            owners.setdefault(s["linked"] or i, []).append((i, s))
    sql_owners = set(owners)
    for owner, members in owners.items():
        s0 = members[0][1]
        queries = list(dict.fromkeys(q for _, s in members for q in (s.get("sql") or [])))
        errors = [s["sql_error"] for _, s in members if s.get("sql_error")]
        if errors:
            placeholder(owner, "err:" + errors[0], errors[0])
            continue
        if not queries:
            placeholder(owner, "flag-no-sql", "flagged as custom SQL by Tableau but no SQL was found for it")
            continue
        parsed, bad = [], False
        for q in queries:
            tabs, reason = parse_custom_sql(q)
            if tabs is None:
                placeholder(owner, q, reason)
                bad = True
            else:
                parsed.append(set().union(*(resolver.base_tables(sc, nm) for sc, nm in tabs)))
        if bad:
            continue
        if any(s.get("union") for _, s in members) and any(x != parsed[0] for x in parsed):
            placeholder(owner, "union:" + "|".join(queries), "several embedded queries read different tables and cannot be told apart")
            continue
        parsed_all = set().union(*parsed)
        meta = set()
        for _, s in members:
            meta |= base_set(s["tables"])
        if s0["linked"]:
            meta |= base_set(pub_tables.get(owner, []))
        extra = meta - parsed_all
        if extra and owner not in ALLOW_IGNORE_METADATA:
            placeholder(owner, "meta:" + repr(sorted(extra)), "Metadata lists tables not in the parsed SQL: %s" % sorted(extra))
            continue
        if extra:
            skipped["custom_sql_notes"].append("%s: Metadata-only tables ignored %s. Evidence: %s" % (
                names.get(owner, owner), sorted(extra), ALLOW_IGNORE_METADATA[owner]))
        else:
            skipped["custom_sql_notes"].append("%s: parsed SQL tables %d, Metadata tables %d, all contained in the SQL" % (
                names.get(owner, owner), len(parsed_all), len(meta)))
        for sc, nm in parsed_all:
            table_rows.add((owner, sc, nm))
    skipped["custom_sql_sources"] = sql_owners
    for i, s in sources.items():
        if (s["linked"] or i) in sql_owners:
            continue
        add_tables(s["linked"] or i, s["tables"])
    for luid in {s["linked"] for s in sources.values() if s["linked"]} - sql_owners:
        add_tables(luid, pub_tables.get(luid, []))
    # Flag a data source only when EVERY skipped table is positively non-DWH (at least one), and it has no custom SQL: a skipped table we
    # cannot place keeps the source unflagged, so it stays NoTables / NotReady (fail closed).
    flag_owners = {o for o in nondwh_by if o not in unsure_by and o not in sql_owners}
    skipped["nondwh_ds_ids"] = {i for i, s in sources.items() if (s["linked"] or i) in flag_owners}
    skipped["nondwh_names"] = {names.get(o, o): sorted(nondwh_by[o]) for o in flag_owners}
    skipped["mixed"] = {names.get(o, o): sorted(skipped_by[o]) for o in skipped_by if stored.get(o)}
    sub_rows = {s["id"]: (s["id"], s["wb"], s["tt"], s["tid"], s["fmt"], s["subject"], s["message"], s["psize"],
                          s["orient"], int(s["susp"]), s["sched"], s["hash"], s.get("sched_id"), s.get("wb_updated")) for s in subs}
    return ds_rows, sorted(table_rows), sub_rows, skipped


def existing(cur, sql):
    return cur.execute(sql).fetchall()


def dry_run_report(cur, wb_names, ds_rows, table_rows, links, sub_rows, nondwh=(), tasks=None):
    def diff(name, new, old):
        ins = sum(k not in old for k in new)
        upd = sum(k in old and tuple(old[k]) != tuple(new[k]) for k in new)
        gone = sum(k not in new and old[k][-1] for k in old)
        print("  %-28s insert %-4d update %-4d deactivate %-4d" % (name, ins, upd, gone))

    old_ds = {r[0]: (r[0], r[1], r[2], None if r[3] is None else int(r[3]), int(r[4]), int(r[5])) for r in
              existing(cur, "SELECT DataSource_id, DataSource_Name, LinkedPublishedDatasourceId, CAST(IsExtract AS int), "
                            "CAST(NonDwhConnection AS int), CAST(Active_Flag AS int) FROM Integration.Tableau_DataSource")}
    old_wd = {(r[0], r[1]): (r[0], r[1], r[2], int(r[3])) for r in
              existing(cur, "SELECT Workbook_id, DataSource_id, Workbook_Name, CAST(Active_Flag AS int) "
                            "FROM Integration.Tableau_WorkbookDataSource")}
    old_dt = {tuple(r) for r in existing(cur, "SELECT DataSource_id, SchemaName, TableName "
                                              "FROM Integration.Tableau_DataSourceTable")}
    old_sub = {r[0]: tuple(r[:-1]) + (int(r[-1]),) for r in
               existing(cur, "SELECT Subscription_id, Workbook_id, TargetType, TargetId, Format, Subject, Message, "
                             "PageSize, Orientation, CAST(IsSuspended AS int), ScheduleType, SubscriptionKeyHash, ScheduleId, WorkbookUpdatedAt, "
                             "CAST(Active_Flag AS int) FROM Integration.Tableau_Subscription")}
    print("Would change (dev DWH):")
    new_ds = {k: tuple(v[:4]) + (int(k in nondwh), 1) for k, v in ds_rows.items()}   # NonDwhConnection is part of the diff
    diff("Tableau_DataSource", new_ds, old_ds)
    new_wd = {(w, d): (w, wb_names[w], d, 1) for w, d in links}
    new_wd = {k: (k[0], v[2], v[1], 1) for k, v in new_wd.items()}
    diff("Tableau_WorkbookDataSource", new_wd, old_wd)
    seen_ids = set(ds_rows) | {r[2] for r in ds_rows.values() if r[2]} | {t[0] for t in table_rows}
    newt, oldt = set(table_rows), {t for t in old_dt if t[0] in seen_ids}
    print("  %-28s insert %-4d update 0    delete     %-4d" % ("Tableau_DataSourceTable", len(newt - oldt),
                                                              len(oldt - newt)))
    new_sub = {k: (v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8], v[9], v[10], bytes(v[11]), v[12], v[13], 1)
               for k, v in sub_rows.items()}
    old_sub = {k: tuple(bytes(x) if isinstance(x, (bytes, bytearray)) else x for x in v) for k, v in old_sub.items()}
    diff("Tableau_Subscription", new_sub, old_sub)
    if tasks is not None:
        old_t = {r[0]: tuple(r[1:]) for r in cur.execute("SELECT Task_id, Target_id, TargetType, Schedule_id, Priority, ConsecutiveFailedCount, "
                                                         "CAST(Active_Flag AS int) FROM Integration.Tableau_ExtractTask").fetchall()}
        new_t = {x["id"]: (x["target"], x["ttype"], x["sched"], x["priority"], x["failed"], 1) for x in tasks}
        diff("Tableau_ExtractTask", new_t, old_t)


MERGE_SQL = {
    "ds": """
MERGE Integration.Tableau_DataSource WITH (HOLDLOCK) AS t USING #ds AS s ON t.DataSource_id = s.DataSource_id
WHEN MATCHED THEN UPDATE SET DataSource_Name = s.DataSource_Name,
     LinkedPublishedDatasourceId = s.LinkedPublishedDatasourceId, IsExtract = s.IsExtract, NonDwhConnection = s.NonDwh,
     LastSeenAt = SYSDATETIME(), Active_Flag = 1
WHEN NOT MATCHED BY TARGET THEN INSERT (DataSource_id, DataSource_Name, LinkedPublishedDatasourceId, IsExtract, NonDwhConnection)
     VALUES (s.DataSource_id, s.DataSource_Name, s.LinkedPublishedDatasourceId, s.IsExtract, s.NonDwh)
WHEN NOT MATCHED BY SOURCE AND t.Active_Flag = 1 AND t.DataSource_id NOT IN (SELECT DataSource_id FROM #protds) THEN UPDATE SET Active_Flag = 0
OUTPUT $action;""",
    "wd": """
MERGE Integration.Tableau_WorkbookDataSource WITH (HOLDLOCK) AS t USING #wd AS s
  ON t.Workbook_id = s.Workbook_id AND t.DataSource_id = s.DataSource_id
WHEN MATCHED THEN UPDATE SET Workbook_Name = s.Workbook_Name, LastSeenAt = SYSDATETIME(), Active_Flag = 1
WHEN NOT MATCHED BY TARGET THEN INSERT (Workbook_id, Workbook_Name, DataSource_id)
     VALUES (s.Workbook_id, s.Workbook_Name, s.DataSource_id)
WHEN NOT MATCHED BY SOURCE AND t.Active_Flag = 1 AND t.Workbook_id NOT IN (SELECT Workbook_id FROM #skipwb) THEN UPDATE SET Active_Flag = 0
OUTPUT $action;""",
    # Upstream tables are fully derived: replace the rows of every data source id seen in this run.
    "dt": """
MERGE Integration.Tableau_DataSourceTable WITH (HOLDLOCK) AS t USING #dt AS s
  ON t.DataSource_id = s.DataSource_id AND t.SchemaName = s.SchemaName AND t.TableName = s.TableName
WHEN MATCHED THEN UPDATE SET LastSeenAt = SYSDATETIME()
WHEN NOT MATCHED BY TARGET THEN INSERT (DataSource_id, SchemaName, TableName)
     VALUES (s.DataSource_id, s.SchemaName, s.TableName)
WHEN NOT MATCHED BY SOURCE AND t.DataSource_id IN (SELECT DataSource_id FROM #seen) THEN DELETE
OUTPUT $action;""",
    "sub": """
MERGE Integration.Tableau_Subscription WITH (HOLDLOCK) AS t USING #sub AS s ON t.Subscription_id = s.Subscription_id
WHEN MATCHED THEN UPDATE SET Workbook_id = s.Workbook_id, TargetType = s.TargetType, TargetId = s.TargetId,
     Format = s.Format, Subject = s.Subject, Message = s.Message, PageSize = s.PageSize,
     Orientation = s.Orientation, IsSuspended = s.IsSuspended, ScheduleType = s.ScheduleType,
     SubscriptionKeyHash = s.SubscriptionKeyHash, ScheduleId = s.ScheduleId, WorkbookUpdatedAt = s.WorkbookUpdatedAt,
     LastSeenAt = SYSDATETIME(), Active_Flag = 1
WHEN NOT MATCHED BY TARGET THEN INSERT (Subscription_id, Workbook_id, TargetType, TargetId, Format, Subject,
     Message, PageSize, Orientation, IsSuspended, ScheduleType, SubscriptionKeyHash, ScheduleId, WorkbookUpdatedAt)
     VALUES (s.Subscription_id, s.Workbook_id, s.TargetType, s.TargetId, s.Format, s.Subject, s.Message,
     s.PageSize, s.Orientation, s.IsSuspended, s.ScheduleType, s.SubscriptionKeyHash, s.ScheduleId, s.WorkbookUpdatedAt)
WHEN NOT MATCHED BY SOURCE AND t.Active_Flag = 1 AND t.Workbook_id NOT IN (SELECT Workbook_id FROM #skipwb)
     AND t.Subscription_id NOT IN (SELECT Subscription_id FROM #skipsub) THEN UPDATE SET Active_Flag = 0
OUTPUT $action;""",
    # Tasks that vanished are deactivated, except a task the script took over (deleted on purpose, still needed for its schedule).
    "task": """
MERGE Integration.Tableau_ExtractTask WITH (HOLDLOCK) AS t USING #task AS s ON t.Task_id = s.Task_id
WHEN MATCHED THEN UPDATE SET Target_id = s.Target_id, TargetType = s.TargetType, Schedule_id = s.Schedule_id, Priority = s.Priority,
     ConsecutiveFailedCount = s.Failed, LastSeenAt = SYSDATETIME(), Active_Flag = 1
WHEN NOT MATCHED BY TARGET THEN INSERT (Task_id, Target_id, TargetType, Schedule_id, Priority, ConsecutiveFailedCount)
     VALUES (s.Task_id, s.Target_id, s.TargetType, s.Schedule_id, s.Priority, s.Failed)
WHEN NOT MATCHED BY SOURCE AND t.Active_Flag = 1 AND NOT EXISTS (SELECT 1 FROM Integration.Tableau_ExtractTaskBackup b
     WHERE b.Task_id = t.Task_id AND b.RestoredAt IS NULL) THEN UPDATE SET Active_Flag = 0
OUTPUT $action;""",
    "sched": """
MERGE Integration.Tableau_Schedule WITH (HOLDLOCK) AS t USING #sched AS s ON t.Schedule_id = s.Schedule_id
WHEN MATCHED THEN UPDATE SET Name = s.Name, State = s.State, Frequency = s.Frequency, WeekDays = s.WeekDays, MonthDays = s.MonthDays,
     StartTime = s.StartTime, IntervalHours = s.IntervalHours, LastSeenAt = SYSDATETIME(), Active_Flag = 1
WHEN NOT MATCHED BY TARGET THEN INSERT (Schedule_id, Name, State, Frequency, WeekDays, MonthDays, StartTime, IntervalHours)
     VALUES (s.Schedule_id, s.Name, s.State, s.Frequency, s.WeekDays, s.MonthDays, s.StartTime, s.IntervalHours)
OUTPUT $action;""",
}


def protected_counts(cur, skipped_wbs):
    """Active rows (workbooks, data sources, subscriptions) that belong to skipped workbooks: kept, so they count as present."""
    if not skipped_wbs:
        return 0, 0, 0
    ph = ",".join("?" * len(skipped_wbs))
    w = list(skipped_wbs)
    return (cur.execute("SELECT COUNT(DISTINCT Workbook_id) FROM Integration.Tableau_WorkbookDataSource WHERE Active_Flag = 1 AND Workbook_id IN (%s)" % ph, w).fetchone()[0],
            cur.execute("SELECT COUNT(DISTINCT DataSource_id) FROM Integration.Tableau_WorkbookDataSource WHERE Active_Flag = 1 AND Workbook_id IN (%s)" % ph, w).fetchone()[0],
            cur.execute("SELECT COUNT(*) FROM Integration.Tableau_Subscription WHERE Active_Flag = 1 AND Workbook_id IN (%s)" % ph, w).fetchone()[0])


def guard(cur, wb_names, ds_rows, sub_rows, skipped_wbs=()):
    """Abort before any write if the read looks partial: zero rows, or (workbooks and data sources only) a drop of more
    than 25% vs active rows. Subscriptions have no percentage rule: people delete them, and a deletion is not a partial read.
    Rows of skipped workbooks are kept by the MERGE, so they are added to the new counts."""
    pw, pd_, ps = protected_counts(cur, set(skipped_wbs))
    cur_counts = {
        "in-scope workbooks": (len(wb_names) + pw, "SELECT COUNT(DISTINCT Workbook_id) FROM Integration.Tableau_WorkbookDataSource WHERE Active_Flag = 1"),
        "data sources": (len(ds_rows) + pd_, "SELECT COUNT(*) FROM Integration.Tableau_DataSource WHERE Active_Flag = 1"),
        "subscriptions": (len(sub_rows) + ps, "SELECT COUNT(*) FROM Integration.Tableau_Subscription WHERE Active_Flag = 1"),
    }
    for what, (new, sql) in cur_counts.items():
        old = cur.execute(sql).fetchone()[0]
        if new == 0 or (what != "subscriptions" and old > 0 and new < 0.75 * old):
            raise RuntimeError("ABORT (partial read?): %s = %d now vs %d active in the DWH. Nothing written." % (what, new, old))


def write_dwh(conn, wb_names, ds_rows, table_rows, links, sub_rows, schedules=(), skipped_wbs=(), skipped_subs=(), commit=True, nondwh=(), tasks=None):
    cur = conn.cursor()
    cur.fast_executemany = False
    cur.execute("CREATE TABLE #ds (DataSource_id nvarchar(128), DataSource_Name nvarchar(256), "
                "LinkedPublishedDatasourceId nvarchar(128), IsExtract bit, NonDwh bit NOT NULL DEFAULT 0)")
    cur.execute("CREATE TABLE #wd (Workbook_id nvarchar(128), Workbook_Name nvarchar(256), DataSource_id nvarchar(128))")
    cur.execute("CREATE TABLE #dt (DataSource_id nvarchar(128), SchemaName sysname, TableName sysname)")
    cur.execute("CREATE TABLE #seen (DataSource_id nvarchar(128))")
    cur.execute("CREATE TABLE #sub (Subscription_id nvarchar(128), Workbook_id nvarchar(128), TargetType varchar(10), "
                "TargetId nvarchar(128), Format varchar(12), Subject nvarchar(500), Message nvarchar(2000), "
                "PageSize varchar(20), Orientation varchar(20), IsSuspended bit, ScheduleType varchar(50), "
                "SubscriptionKeyHash varbinary(32), ScheduleId nvarchar(128), WorkbookUpdatedAt datetime2(0))")
    cur.execute("CREATE TABLE #skipwb (Workbook_id nvarchar(128))")
    cur.execute("CREATE TABLE #skipsub (Subscription_id nvarchar(128))")
    cur.execute("CREATE TABLE #protds (DataSource_id nvarchar(128))")
    cur.execute("CREATE TABLE #sched (Schedule_id nvarchar(128), Name nvarchar(200), State varchar(12), Frequency varchar(10), "
                "WeekDays varchar(40), MonthDays varchar(80), StartTime time(0), IntervalHours decimal(6,2))")
    if tasks is not None:
        cur.execute("CREATE TABLE #task (Task_id nvarchar(128), Target_id nvarchar(128), TargetType varchar(12), Schedule_id nvarchar(128), "
                    "Priority int, Failed int)")
    if tasks:
        cur.executemany("INSERT #task VALUES (?,?,?,?,?,?)", [(x["id"], x["target"], x["ttype"], x["sched"], x["priority"], x["failed"]) for x in tasks])
    if skipped_wbs:
        cur.executemany("INSERT #skipwb VALUES (?)", [(w,) for w in sorted(skipped_wbs)])
        ph = ",".join("?" * len(skipped_wbs))
        cur.execute("INSERT #protds SELECT d.DataSource_id FROM Integration.Tableau_DataSource d JOIN Integration.Tableau_WorkbookDataSource w "
                    "ON w.DataSource_id = d.DataSource_id WHERE w.Workbook_id IN (%s) UNION "
                    "SELECT d.LinkedPublishedDatasourceId FROM Integration.Tableau_DataSource d JOIN Integration.Tableau_WorkbookDataSource w "
                    "ON w.DataSource_id = d.DataSource_id WHERE w.Workbook_id IN (%s) AND d.LinkedPublishedDatasourceId IS NOT NULL"
                    % (ph, ph), list(skipped_wbs) * 2)
    if skipped_subs:
        cur.executemany("INSERT #skipsub VALUES (?)", [(x,) for x in sorted(skipped_subs)])
    if schedules:
        cur.executemany("INSERT #sched VALUES (?,?,?,?,?,?,?,?)", [sd.to_row(x) for x in schedules.values()])
    cur.executemany("INSERT #ds (DataSource_id, DataSource_Name, LinkedPublishedDatasourceId, IsExtract, NonDwh) VALUES (?,?,?,?,?)",
                    [tuple(r[:4]) + (int(r[0] in nondwh),) for r in ds_rows.values()])
    cur.executemany("INSERT #wd VALUES (?,?,?)", [(w, wb_names[w], d) for w, d in sorted(links)])
    if table_rows:
        cur.executemany("INSERT #dt VALUES (?,?,?)", table_rows)
    linked = {r[2] for r in ds_rows.values() if r[2]}
    cur.executemany("INSERT #seen VALUES (?)", [(i,) for i in sorted(set(ds_rows) | linked | {t[0] for t in table_rows})])
    cur.executemany("INSERT #sub VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", list(sub_rows.values()))
    out = {}
    for k in ("sched", "ds", "wd", "dt", "sub") + (("task",) if tasks is not None else ()):  # order matters: FK WorkbookDataSource -> DataSource
        acts = [r[0] for r in cur.execute(MERGE_SQL[k]).fetchall()]
        out[k] = {a: acts.count(a) for a in ("INSERT", "UPDATE", "DELETE")}
    # A data source nothing active links to any more is inactive (handles rows deactivated above).
    cur.execute("UPDATE ds SET Active_Flag = 0 FROM Integration.Tableau_DataSource ds WHERE ds.Active_Flag = 1 "
                "AND NOT EXISTS (SELECT 1 FROM Integration.Tableau_WorkbookDataSource w "
                "WHERE w.DataSource_id = ds.DataSource_id AND w.Active_Flag = 1)")
    if commit:
        conn.commit()
    return out


def schema_ready(cur):
    return (cur.execute("SELECT COUNT(*) FROM sys.tables WHERE name = 'Tableau_Schedule' AND schema_id = SCHEMA_ID('Integration')").fetchone()[0] == 1
            and cur.execute("SELECT COL_LENGTH('Integration.Tableau_Subscription', 'ScheduleId')").fetchone()[0] is not None
            and cur.execute("SELECT COL_LENGTH('Integration.Tableau_Subscription', 'WorkbookUpdatedAt')").fetchone()[0] is not None
            and cur.execute("SELECT COL_LENGTH('Integration.Tableau_DataSource', 'NonDwhConnection')").fetchone()[0] is not None
            and cur.execute("SELECT COL_LENGTH('Integration.Tableau_PipelineTable', 'Source')").fetchone()[0] is not None
            and cur.execute("SELECT COUNT(*) FROM sys.tables WHERE name IN ('Tableau_ExtractTask', 'Tableau_ExtractTaskBackup', 'Tableau_LineageSource') "
                            "AND schema_id = SCHEMA_ID('Integration')").fetchone()[0] == 3)


def setup_logging():
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    sh = logging.StreamHandler(sys.stdout)
    fh = ops.log_file_handler("t2_sync")
    for h in (sh, fh):
        h.setFormatter(fmt)
        log.addHandler(h)
    for other in (logging.getLogger("t4_refresh"), logging.getLogger("ops")):   # LazyTableau / ops log through these
        other.setLevel(logging.INFO)
        if not other.handlers:
            other.addHandler(sh)
            other.addHandler(fh)

    def hook(et, ev, tb):
        log.critical("Unhandled error", exc_info=(et, ev, tb))
    sys.excepthook = hook


def read_extract_tasks(cur, server, ds_rows, stats, problems):
    """D-067: the extract refresh tasks and their schedules (read-only). Returns the tasks, or None when they could not be read or the
    answer looks partial (nothing is then written or deactivated for tasks); the schedules are added to stats["schedules"]."""
    tasks, scheds = None, {}
    try:
        tasks, scheds, tproblems = et.read_tasks(server)
        problems += tproblems
        # a taken-over task is gone from Tableau on purpose and stays Active_Flag = 1: it must not count (same test as extract_takeover.read_live)
        stored = cur.execute("SELECT COUNT(*) FROM Integration.Tableau_ExtractTask t WHERE t.Active_Flag = 1 AND NOT EXISTS "
                             "(SELECT 1 FROM Integration.Tableau_ExtractTaskBackup b WHERE b.Task_id = t.Task_id AND b.RestoredAt IS NULL)").fetchone()[0]
        if not tasks and stored:
            raise RuntimeError("no extract refresh task came back but %d are stored: partial read" % stored)
    except Exception as e:
        log.error("EXTRACT TASKS NOT READ: %s: %s (stored tasks stay as they are)", type(e).__name__, str(e)[:150])
        problems.append(("extract tasks", "-", "%s: %s" % (type(e).__name__, str(e)[:120])))
        tasks, scheds = None, {}
    stats["schedules"].update(scheds)
    try:   # D-073: the schedule of a taken-over task is no longer named by any live task: read it for its own sake or it goes stale.
        # Also when the task read failed or came back empty (every task left on the site may be one we took over).
        bsched, bprob = et.read_backup_schedules(server, cur, scheds)
        stats["schedules"].update(bsched)
        problems += bprob
    except Exception as e:
        log.error("SCHEDULES OF TAKEN-OVER TASKS NOT READ: %s: %s", type(e).__name__, str(e)[:150])
        problems.append(("extract schedules of taken-over tasks", "-", "%s: %s" % (type(e).__name__, str(e)[:120])))
    if tasks is None:
        return None
    mapped = {r[2] or r[0]: r[1] for r in ds_rows.values() if r[3] == 1}
    on_mapped = [x for x in tasks if x["ttype"] == "datasource" and x["target"] in mapped]
    log.info("Extract refresh tasks (read-only): %d (%d on data sources, %d on workbooks), %d schedules; %d task(s) on the %d mapped extracts",
             len(tasks), sum(x["ttype"] == "datasource" for x in tasks), sum(x["ttype"] == "workbook" for x in tasks), len(scheds),
             len(on_mapped), len(mapped))
    for x in sorted(on_mapped, key=lambda x: mapped[x["target"]] or ""):
        sc = scheds.get(x["sched"])
        log.info("    %s: %s priority %s, failed %s times", mapped[x["target"]], sc.name if sc else "(schedule unreadable)",
                 x["priority"], x["failed"])
    for tid in sorted(set(mapped) - {x["target"] for x in on_mapped}, key=lambda i: mapped[i] or ""):
        log.info("    %s: no Tableau extract refresh task (refreshed by us only for due subscriptions)", mapped[tid])
    return tasks


def run_sync(a, cur, conn, tableau):
    """Read Tableau and (unless --dry-run) write the DWH. Returns (exit code, summary text, problems, registry notes, unconfirmed tables).
    a.registry_first_run: the higher per-run registry cap, for the very first run only (D-075)."""
    accept = set(getattr(a, "accept_drops", None) or [])
    stored = {r[0]: r[1] for r in cur.execute("SELECT Workbook_id, COUNT(*) FROM Integration.Tableau_WorkbookDataSource "
                                              "WHERE Active_Flag = 1 GROUP BY Workbook_id").fetchall()}
    stored_tables = {r[0]: r[1] for r in cur.execute("SELECT DataSource_id, COUNT(*) FROM Integration.Tableau_DataSourceTable "
                                                     "GROUP BY DataSource_id").fetchall()}
    subs, wb_names, sources, pub_tables, links, stats = collect_tableau(tableau(), stored=stored, accept_drops=accept,
                                                                              deadline=time.monotonic() + T2_MAX_LOCK_MIN * 60)
    resolver = Resolver(cur)
    ds_rows, table_rows, sub_rows, skipped = build_rows(sources, pub_tables, links, subs, resolver)
    problems = stats["problems"]
    drops = detect_table_drops(stored_tables, table_rows, sources, links, accept)
    if drops:
        for wid, why in drops.items():
            problems.append(("workbook", wid, why))
            stats["skipped_wbs"].add(wid)
            log.error("WORKBOOK SKIPPED %s ('%s'): %s. Its existing rows stay active and go stale.", wid, wb_names.get(wid, "?"), why)
        wb_names = {w: n for w, n in wb_names.items() if w not in drops}
        links = {(w, d) for w, d in links if w not in drops}
        keep = {d for _, d in links}
        sources = {i: v for i, v in sources.items() if i in keep}
        subs = [x for x in subs if x["wb"] not in drops]
        ds_rows, table_rows, sub_rows, skipped = build_rows(sources, pub_tables, links, subs, resolver)
    log.info("Tableau read (read-only): %d workbooks with subscriptions, %d in scope (more than one data source), %d skipped as single-source, "
             "%d SKIPPED because of read problems", stats["workbooks_with_subs"], len(wb_names), stats["skipped_single_source"], len(stats["skipped_wbs"]))
    log.info("  subscriptions %d (%d suspended), schedules %d, data sources %d (%d extract, %d live, %d unknown), workbook-datasource links %d",
             len(sub_rows), sum(r[9] for r in sub_rows.values()), len(stats["schedules"]), len(ds_rows),
             sum(r[3] == 1 for r in ds_rows.values()), sum(r[3] == 0 for r in ds_rows.values()),
             sum(r[3] is None for r in ds_rows.values()), len(links))
    log.info("  distinct published extracts behind links: %d", len({s["linked"] for s in sources.values() if s["linked"] and s["is_extract"]}))
    log.info("  upstream table rows %d (view -> base table), skipped non-DWH/file tables %d, unresolved names %s",
             len(table_rows), skipped["non_dwh_or_file"], sorted(resolver.unresolved) or "none")
    log.info("  data sources POSITIVELY identified as non-DWH (file / other database; counted ready, not checked, D-053): %d",
             len(skipped["nondwh_names"]))
    for nm, tb in sorted(skipped["nondwh_names"].items()):
        log.info("    - %s: %s", nm, ", ".join(tb))
    guard(cur, wb_names, ds_rows, sub_rows, stats["skipped_wbs"])
    ext_tasks = read_extract_tasks(cur, tableau(), ds_rows, stats, problems)
    log.info("  custom SQL: %d customSQLTables read, %d sources handled, %d flagged data-source links, FAIL-CLOSED placeholders %d",
             stats["custom_sql_tables"], len(skipped["custom_sql_sources"]), stats["flagged_sources"], len(skipped["custom_sql_failed"]))
    for nm, reason in skipped["custom_sql_failed"]:
        log.info("    - PLACEHOLDER %s: %s", nm, reason)
    for note in sorted(skipped["custom_sql_notes"]):
        log.info("    - %s", note)
    for x in stats["attached_embedded"]:
        log.info("    embedded custom SQL attached by workbook: %s", x)
    if skipped["mixed"]:
        log.info("  data sources with BOTH stored and skipped (non-DWH/file/custom-SQL) tables, review:")
        for nm, sk in sorted(skipped["mixed"].items()):
            log.info("    - %s: skipped %s", nm, ", ".join(sk))
    log.info("  checks: shell flag unconfirmed -> Unknown %d, parents disagree %d, source id duplicated inside one workbook %d, "
             "tables with no schema %d", stats["shell_unconfirmed"], stats["parents_disagree"], stats["dup_in_workbook"], skipped["null_schema"])
    log.info("  scope workbooks: %s", "; ".join(sorted(wb_names.values())))
    if a.dry_run:
        dry_run_report(cur, wb_names, ds_rows, table_rows, links, sub_rows, skipped["nondwh_ds_ids"], ext_tasks)
        log.info("DRY RUN: nothing written.")
    else:
        out = write_dwh(conn, wb_names, ds_rows, table_rows, links, sub_rows, stats["schedules"], stats["skipped_wbs"], stats["skipped_subs"],
                        nondwh=skipped["nondwh_ds_ids"], tasks=ext_tasks)
        log.info("Written to dev DWH (MERGE actions; UPDATE includes deactivations and LastSeenAt refreshes):")
        for k, name in (("sched", "Tableau_Schedule"), ("ds", "Tableau_DataSource"), ("wd", "Tableau_WorkbookDataSource"),
                        ("dt", "Tableau_DataSourceTable"), ("sub", "Tableau_Subscription"), ("task", "Tableau_ExtractTask")):
            if k in out:
                log.info("  %-28s %s", name, out[k])
    notes, unconfirmed = [], []
    try:
        tables = {(sc, nm) for _, sc, nm in table_rows if sc not in (UNPARSED_SCHEMA, "(unknown schema)")} | rs.stored_tables(cur)
        plan = rs.derive(cur, tables)
        rs.report(plan, not a.dry_run)
        if not a.dry_run:
            limit = rs.change_limit(getattr(a, "registry_first_run", False))
            counts, notes = rs.apply(conn, plan, limit)
            log.info("Registry written: %s", counts)
            if counts["held"]:
                problems.append(("registry", "-", "%d insert/move/removal(s) not applied (more than %d in one run)" % (counts["held"], limit)))
        unconfirmed = ["%s.%s (would be %s)" % (s_, t_, p_) for s_, t_, p_, _ in plan.unconfirmed]
        reg = ("registry_insert=%d registry_move=%d registry_remove=%d registry_manual_differs=%d registry_ambiguous=%d registry_no_pipeline=%d "
               "registry_unconfirmed=%d" % (len(plan.insert), len(plan.remap), len(plan.drop_auto), len(plan.manual_differs), len(plan.ambiguous),
                                            len(plan.unmapped), len(plan.unconfirmed)))
    except Exception as e:
        conn.rollback()
        unconfirmed = []
        log.exception("Registry derivation failed")
        problems.append(("registry", "-", "%s: %s" % (type(e).__name__, str(e)[:150])))
        reg = "registry=FAILED"
    code = 2 if problems else 0
    text = ("workbooks=%d subscriptions=%d schedules=%d data_sources=%d extract_tasks=%s skipped_workbooks=%d skipped_subscriptions=%d problems=%d %s"
            % (len(wb_names), len(sub_rows), len(stats["schedules"]), len(ds_rows), "unread" if ext_tasks is None else len(ext_tasks),
               len(stats["skipped_wbs"]), len(stats["skipped_subs"]), len(problems), reg))
    return code, text, problems, notes, unconfirmed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="read Tableau and the DWH, print the plan, write nothing")
    ap.add_argument("--smtp", "--ses", dest="ses", action="store_true",
                    help="alert mails through the real SMTP server (needs the SMTP_* settings and SEND_ALLOWED_RECIPIENTS, which is not needed on UAT with SEND_ALLOW_ALL_UAT=1); or MAIL_TRANSPORT=smtp")
    ap.add_argument("--local-smtp", action="store_true", help="force the local test sink even if MAIL_TRANSPORT=smtp")
    ap.add_argument("--accept-drops", action="append", metavar="WORKBOOK_ID",
                    help="accept this workbook's shrinkage (fewer data sources / tables) as real; repeatable, one id per use, each is logged")
    ap.add_argument("--registry-first-run", action="store_true",
                    help="very first registry run only: allow up to REGISTRY_MAX_CHANGES_FIRST (default 50) registry inserts/moves/removals instead of REGISTRY_MAX_CHANGES (5)")
    ap.add_argument("--local-host", default="127.0.0.1")
    ap.add_argument("--local-port", type=int, default=1025)
    a = ap.parse_args(argv)
    setup_logging()
    mode = "dry-run" if a.dry_run else "execute"
    try:
        a.ses = ops.effective_ses(a.ses, not a.dry_run, os.environ, a.local_smtp)
    except ValueError as e:
        sys.exit(str(e))
    ops.log_transport("t2", a.ses, not a.dry_run)
    if ops.check_kill_switch("t2", not a.dry_run, a.ses, a.local_host, a.local_port):
        return 0
    import t6_send as t6
    acfg = ops.alert_config(a.ses, os.environ, "t2", execute=not a.dry_run, local_smtp=a.local_smtp)
    ops.check_alert_recipient(acfg, "t2")
    mailer = t6.make_mailer(acfg, a.local_host, a.local_port)

    def alert(code, text):
        if a.dry_run:
            return
        try:
            ops.alert_once(acfg, mailer, "t2", code, text)
        except Exception as e:
            log.error("Alert could not be sent (%s)", type(e).__name__)

    def fail(what, exit_code=1):
        what = t6.sanitize(what, 300)
        log.error("RUN SUMMARY mode=%s FATAL=%s written=0 needs_human=1%s", mode, what, ops.allow_all_marker())
        alert("t2_failed", "the mapping sync failed entirely and wrote nothing: %s. t6 stops sending once the mapping is older than %s h."
              % (what, os.environ.get("MAPPING_MAX_AGE_H", "26")))
        ops.write_marker("t2", mode, exit_code, "FATAL " + what, outcome="fatal")
        return exit_code

    try:
        conn = connect()
        lock_conn = t4.connect()
    except (Exception, SystemExit) as e:
        return fail("DWH connect or guard failed: %s" % (e if isinstance(e, SystemExit) else type(e).__name__))
    cur = conn.cursor()
    if not schema_ready(cur):
        return fail("Integration.Tableau_Schedule / Tableau_Subscription.ScheduleId / WorkbookUpdatedAt / Tableau_DataSource.NonDwhConnection missing: run scripts/sql/R1_schedule_schema.sql, R5_workbook_updated_at.sql, R8_nondwh_sources.sql and ExtractAuto_schema.sql")
    tableau = None
    try:
        for attempt in range(LOCK_RETRIES + 1):
            tableau = t4.LazyTableau(lock_conn.cursor(), owner="t2")
            try:
                tableau()   # takes the shared PAT lock, then signs in
                break
            except t4.PatBusy:
                if attempt >= LOCK_RETRIES:
                    return fail("Tableau PAT session lock still held after %d tries" % (LOCK_RETRIES + 1))
                log.warning("PAT lock busy, waiting %d s (try %d of %d)", LOCK_WAIT_S, attempt + 1, LOCK_RETRIES + 1)
                time.sleep(LOCK_WAIT_S)
        code, text, problems, notes, unconfirmed = run_sync(a, cur, conn, tableau)
    except SystemExit:
        return fail("Tableau sign-in failed")
    except Exception as e:
        conn.rollback()
        log.exception("Sync failed")
        return fail("%s: %s" % (type(e).__name__, str(e)[:200]))
    finally:
        if tableau is not None:
            tableau.close()
    for kind, ident, why in problems:
        log.warning("  PROBLEM %s %s: %s", kind, ident, why)
    log.info("RUN SUMMARY mode=%s exit=%d %s needs_human=%d%s", mode, code, text, int(bool(problems)), ops.allow_all_marker())
    if problems:
        alert("t2_partial", "the mapping sync skipped %d item(s): %s. Their rows stay active but are not refreshed, so t6 stops sending them "
              "after MAPPING_MAX_AGE_H hours." % (len(problems), "; ".join("%s %s" % (k, i) for k, i, _ in problems[:5])))
    if notes:
        alert("registry_changed", "the pipeline registry was changed by the lineage sync (%d change(s)): %s. Check them; a wrong pipeline changes when the "
              "extract refreshes. An extract with a table registered less than a day ago is not taken over yet." % (len(notes), "; ".join(notes)))
    if unconfirmed:
        alert("registry_unconfirmed", "%d table(s) have no ETL log evidence for their pipeline and were NOT registered, so their extracts are not "
              "refreshed or taken over: %s. Add a Manual row in Tableau_PipelineTable to decide." % (len(unconfirmed), "; ".join(unconfirmed[:20])))
    ops.write_marker("t2", mode, code, "RUN SUMMARY " + text)
    return code


if __name__ == "__main__":
    sys.exit(main())
