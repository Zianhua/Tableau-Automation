"""Automatic pipeline registry (D-066): which pipeline loads each base table that the mapped data sources read.

Called by the daily mapping sync (t2_mapping_sync.py) after the mapping tables are written. Reads the lineage tables
(Integration.Lineage, DW_NovaLineage, OAO_Lineage; their real columns are in LINEAGE_COLS) and Integration.ETL_Master_Log, compares the
result with Integration.Tableau_PipelineTable and prints the difference. A dry run writes nothing.

Per base table (schema, table) read by an active data source:
  1. Lineage entries that name it: '[Schema].[Table]' (Nova), '[Integration].[<Table>_Staging]' (OAO: the staging table is loaded, the
     Dimension table of the same name follows), or a bare table name (Lineage). A bare name that exists in several schemas is ambiguous.
  2. Pipeline of an entry: the pipeline whose ETL_Master_Log row ends within MATCH_WINDOW_MIN minutes after the entry's last loads
     (strong = one pipeline matched at least STRONG of at least MIN_LOADS loads: wins over the source default). Otherwise the default
     pipeline of the lineage table (Integration.Tableau_LineageSource) plus every pipeline the log matched at least MIN_MATCHES times.
  3. One candidate pipeline = derived. Two or more = AMBIGUOUS, none = no pipeline. Both are reported and nothing is written, so the
     table stays unmapped and its extract is not refreshed (fail closed, D-006).
Registry rules (Tableau_PipelineTable.Source):
  Manual rows are the override: never inserted, moved or removed here, only reported when the lineage disagrees.
  Auto rows: inserted when a table has none, moved when the derived pipeline changed (REPORTED, alert), removed when the table became
  ambiguous (REPORTED, alert); kept and warned when the lineage no longer names the table. Every insert, move and removal is mailed once.
  At most MAX_CHANGES inserts + moves + removals per run (MAX_CHANGES_FIRST on the very first run, explicit flag --registry-first-run).
  A table with ZERO log evidence (no ETL_Master_Log row ended within MATCH_WINDOW_MIN minutes after any of its loads) is UNCONFIRMED (D-075):
  the pipeline would only be the source default, a guess. It is reported and NOT registered (no insert, no move), so its extract stays
  unmapped and is neither refreshed nor taken over until the log confirms it or a person adds a Manual row.
"""
import logging
import os
import re
from dataclasses import dataclass, field

log = logging.getLogger("t2_sync")

# lineage table -> (table name column, load completed column, success column). Read on dev 2026-11-04.
LINEAGE_COLS = {
    "Lineage": ("[Table Name]", "[Data Load Completed]", "[Was Successful]"),
    "DW_NovaLineage": ("[Table Name]", "[Data Load Completed]", "[Was Successful]"),
    "OAO_Lineage": ("[table_name]", "[end_time]", "[was_successful]"),
}
LOADS_CONSIDERED = 14     # last successful loads per lineage name that are looked at
MATCH_WINDOW_MIN = 10     # a log row this many minutes after a load (at most) = the load belonged to that pipeline run
MIN_MATCHES = 3           # a pipeline counts as evidence only with this many matched loads
MIN_LOADS = 5             # strong evidence needs at least this many loads ...
STRONG = 0.8              # ... and this share of them matched to one pipeline
MAX_CHANGES = int(os.environ.get("REGISTRY_MAX_CHANGES", 5))   # inserts + moves + removals of Auto rows applied per run
MAX_CHANGES_FIRST = int(os.environ.get("REGISTRY_MAX_CHANGES_FIRST", 50))   # the same, on the very first run (explicit flag)


# ---------- lineage names ----------
def parse_name(raw):
    """'[Fact].[DailyNovaTradeVolume] - CFD Data' -> ('Fact', 'DailyNovaTradeVolume'); 'Client' -> (None, 'Client')."""
    s = re.sub(r"\s+-\s+.*$", "", (raw or "").strip())
    m = re.fullmatch(r"\[?([^\].]+)\]?\.\[?([^\]]+)\]?", s)
    return (m.group(1), m.group(2)) if m else (None, s.strip("[] "))


def entry_keys(schema, table):
    """Keys of base tables (schema or None, table) lower-cased that a lineage entry may stand for."""
    t = table.lower()
    keys = [(schema.lower() if schema else None, t)]
    if t.endswith("_staging") and schema and schema.lower() == "integration":
        keys.append((None, t[:-len("_staging")]))   # the staging table is loaded; the table of the same name in another schema follows
    return keys


# ---------- evidence from the ETL log ----------
@dataclass
class Entry:
    source: str                 # lineage table
    raw: str                    # name as written in the lineage table
    loads: int = 0
    matched: dict = field(default_factory=dict)    # pipeline -> loads that ended shortly before one of its log rows


def read_entries(cur, sources):
    """sources: {lineage table: default pipeline}. Returns (entries, problems)."""
    out, problems = [], []
    for lt, default in sources.items():
        cols = LINEAGE_COLS.get(lt)
        if cols is None:
            problems.append("lineage table %s has no column map in registry_sync.LINEAGE_COLS: skipped" % lt)
            continue
        n, done, ok = cols
        rows = cur.execute(
            "WITH l AS (SELECT %(n)s AS n, %(d)s AS d, ROW_NUMBER() OVER (PARTITION BY %(n)s ORDER BY %(d)s DESC) AS rn "
            "FROM Integration.%(t)s WHERE %(ok)s = 1 AND %(d)s IS NOT NULL) "
            "SELECT l.n, (SELECT TOP 1 m.PipelineName FROM Integration.ETL_Master_Log m WHERE m.EndTime >= l.d "
            "AND m.EndTime <= DATEADD(minute, ?, l.d) ORDER BY m.EndTime, m.JobID) FROM l WHERE l.rn <= ?" % {"n": n, "d": done, "t": lt, "ok": ok},
            MATCH_WINDOW_MIN, LOADS_CONSIDERED).fetchall()
        by = {}
        for name, pipe in rows:
            e = by.setdefault(name, Entry(lt, name))
            e.loads += 1
            if pipe:
                e.matched[pipe] = e.matched.get(pipe, 0) + 1
        out += by.values()
    return out, problems


def entry_candidates(e, default):
    """(candidate pipelines, how). Strong log evidence beats the source default; otherwise default plus every pipeline seen."""
    seen = {p: c for p, c in e.matched.items() if c >= MIN_MATCHES}
    for p, c in seen.items():
        if e.loads >= MIN_LOADS and c >= STRONG * e.loads:
            return {p}, "%d of %d loads ended shortly before a %s log row" % (c, e.loads, p)
    cands = {default} | set(seen)
    return cands, ("source default %s" % default) if len(cands) == 1 else \
        "source default %s, log evidence %s" % (default, ", ".join("%s %d/%d" % (p, c, e.loads) for p, c in sorted(seen.items())))


# ---------- the plan (pure) ----------
@dataclass
class Plan:
    unchanged: list = field(default_factory=list)       # (schema, table, pipeline)
    manual_agree: list = field(default_factory=list)
    manual_no_lineage: list = field(default_factory=list)   # (schema, table): manual row, lineage does not name the table (or is ambiguous)
    insert: list = field(default_factory=list)          # (schema, table, pipeline, evidence)
    remap: list = field(default_factory=list)           # (schema, table, old, new, evidence)
    drop_auto: list = field(default_factory=list)       # (schema, table, old, reason)
    manual_differs: list = field(default_factory=list)  # (schema, table, manual pipelines, derived, evidence)
    ambiguous: list = field(default_factory=list)       # (schema, table, reason)
    unmapped: list = field(default_factory=list)        # (schema, table, reason)
    auto_unconfirmed: list = field(default_factory=list)
    unconfirmed: list = field(default_factory=list)     # (schema, table, pipeline, evidence): the derived pipeline has no log evidence, NOT registered (D-075, D-078)
    problems: list = field(default_factory=list)


def make_plan(tables, entries, defaults, registry, static, schemas_of):
    """tables: {(schema, table)} read by active data sources. entries: [Entry]. defaults: {lineage table: pipeline}.
    registry: {(schema, table): {pipeline: 'Manual'|'Auto'}}. static: {(schema, table)}. schemas_of(name): schemas holding a table of that name."""
    index = {}
    for e in entries:
        cands, how = entry_candidates(e, defaults[e.source])
        s, t = parse_name(e.raw)
        for k in entry_keys(s, t):
            index.setdefault(k, []).append((e, cands, how))
    plan = Plan()
    for schema, table in sorted(tables):
        if (schema, table) in static:
            continue
        sl, tl = schema.lower(), table.lower()
        exact = list(index.get((sl, tl), []))
        bare_hits = index.get((None, tl), []) if sl != "integration" else []   # entries that do not name the schema
        cands, why = set(), []
        for e, c, how in exact + bare_hits:
            cands |= c
            why.append("%s '%s': %s" % (e.source, e.raw, how))
        bare = bool(bare_hits)
        have = registry.get((schema, table), {})
        manual = {p for p, src in have.items() if src == "Manual"}
        auto = {p for p, src in have.items() if src == "Auto"}
        evidence = "lineage sync: " + "; ".join(why)
        reason = None
        if len(cands) > 1:
            reason = "several candidate pipelines %s (%s)" % (sorted(cands), "; ".join(why))
        elif cands and bare:
            others = [x for x in schemas_of(table) if x.lower() != sl]
            if others:
                reason = "a bare lineage name that also exists in schema(s) %s" % ", ".join(sorted(others))
        if manual:   # the override: never changed here
            if reason or not cands:
                plan.manual_no_lineage.append((schema, table))   # nothing derived, nothing to compare
                continue
            if cands <= manual:
                plan.manual_agree.append((schema, table, sorted(cands)[0]))
            else:
                plan.manual_differs.append((schema, table, sorted(manual), sorted(cands)[0], evidence))
        elif reason:
            plan.ambiguous.append((schema, table, reason))
            for p in sorted(auto):
                plan.drop_auto.append((schema, table, p, "ambiguous: " + reason))
        elif not cands:
            if auto:
                plan.auto_unconfirmed.append((schema, table, sorted(auto)[0]))
            else:
                plan.unmapped.append((schema, table, "no lineage entry names this table"))
        else:
            (p,) = cands
            if auto == {p}:
                plan.unchanged.append((schema, table, p))
            elif not any(p in e.matched for e, _, _ in exact + bare_hits):   # the derived pipeline was never seen in the log (only the source default): not registered
                plan.unconfirmed.append((schema, table, p, evidence))
            elif auto:
                plan.remap.append((schema, table, sorted(auto)[0], p, evidence))
            else:
                plan.insert.append((schema, table, p, evidence))
    return plan


# ---------- DWH ----------
def load_inputs(cur):
    defaults = {r[0]: r[1] for r in cur.execute("SELECT LineageTable, PipelineName FROM Integration.Tableau_LineageSource")}
    registry = {}
    for s, t, p, src in cur.execute("SELECT SchemaName, TableName, PipelineName, Source FROM Integration.Tableau_PipelineTable"):
        registry.setdefault((s, t), {})[p] = src
    static = {(r[0], r[1]) for r in cur.execute("SELECT SchemaName, TableName FROM Integration.Tableau_StaticTable")}
    return defaults, registry, static


def schemas_of_factory(cur):
    cache = {}

    def schemas_of(name):
        if name not in cache:
            cache[name] = [r[0] for r in cur.execute("SELECT s.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
                                                     "WHERE t.name = ?", name).fetchall()]
        return cache[name]
    return schemas_of


def stored_tables(cur):
    """Base tables of active data sources (the rows T2 stored)."""
    return {(r[0], r[1]) for r in cur.execute(
        "SELECT DISTINCT dst.SchemaName, dst.TableName FROM Integration.Tableau_DataSourceTable dst "
        "JOIN Integration.Tableau_DataSource ds ON dst.DataSource_id IN (ds.DataSource_id, ds.UpstreamDataSourceId) WHERE ds.Active_Flag = 1")}


def static_clashes(cur):
    """Tables that are in BOTH Tableau_StaticTable and Tableau_PipelineTable (ambiguous: need a check or not?), as 'Schema.Table'.
    Read-only. Replaces the two database triggers that used to refuse such a row (D-081)."""
    return ["%s.%s" % (r[0], r[1]) for r in cur.execute(
        "SELECT DISTINCT s.SchemaName, s.TableName FROM Integration.Tableau_StaticTable s "
        "JOIN Integration.Tableau_PipelineTable p ON p.SchemaName = s.SchemaName AND p.TableName = s.TableName "
        "ORDER BY s.SchemaName, s.TableName")]


def derive(cur, tables):
    defaults, registry, static = load_inputs(cur)
    entries, problems = read_entries(cur, defaults)
    plan = make_plan(tables, entries, defaults, registry, static, schemas_of_factory(cur))
    plan.problems += problems
    return plan


def report(plan, execute):
    p = plan
    log.info("Registry from lineage (%s): %d unchanged, %d manual rows agree (%d more have no lineage entry), %d to insert, %d to move, %d to remove, "
             "%d manual rows disagree, %d ambiguous, %d without lineage, %d auto rows no longer confirmed, %d UNCONFIRMED (no log evidence, not registered)",
             "applied" if execute else "dry run, nothing written", len(p.unchanged), len(p.manual_agree), len(p.manual_no_lineage), len(p.insert), len(p.remap),
             len(p.drop_auto), len(p.manual_differs), len(p.ambiguous), len(p.unmapped), len(p.auto_unconfirmed), len(p.unconfirmed))
    for s, t, pl, ev in p.insert:
        log.info("  REGISTRY INSERT %s.%s -> %s  [%s]", s, t, pl, ev)
    for s, t, old, new, ev in p.remap:
        log.warning("  REGISTRY MOVE %s.%s: %s -> %s  [%s]", s, t, old, new, ev)
    for s, t, old, why in p.drop_auto:
        log.warning("  REGISTRY REMOVE %s.%s (was %s): %s", s, t, old, why)
    for s, t, man, new, ev in p.manual_differs:
        log.warning("  REGISTRY MANUAL ROW DIFFERS %s.%s: manual %s, lineage says %s (manual kept)  [%s]", s, t, man, new, ev)
    for s, t, why in p.ambiguous:
        log.warning("  REGISTRY AMBIGUOUS %s.%s: %s. Not registered, its extract is not refreshed; add a Manual row to decide.", s, t, why)
    for s, t, why in p.unmapped:
        log.warning("  REGISTRY NO PIPELINE %s.%s: %s. Not registered, its extract is not refreshed; add a Manual row.", s, t, why)
    for s, t, pl, ev in p.unconfirmed:
        log.warning("  REGISTRY UNCONFIRMED %s.%s: no ETL log row ended within %d minutes after any of its recent loads, so %s would be a guess from the "
                    "source default. NOT registered, its extract stays unmapped (not refreshed, not taken over); add a Manual row to decide.  [%s]",
                    s, t, MATCH_WINDOW_MIN, pl, ev)
    for s, t, pl in p.auto_unconfirmed:
        log.warning("  REGISTRY auto row no longer named by any lineage entry (kept): %s.%s -> %s", s, t, pl)
    for x in p.problems:
        log.warning("  REGISTRY PROBLEM %s", x)


def change_limit(first_run=False):
    """Inserts + moves + removals applied per run: MAX_CHANGES, or MAX_CHANGES_FIRST on the very first run (explicit flag --registry-first-run)."""
    return MAX_CHANGES_FIRST if first_run else MAX_CHANGES


def apply(conn, plan, max_changes=MAX_CHANGES):
    """Writes the plan in one transaction. Returns (counts, alerts): inserts, moves and removals beyond max_changes are NOT applied (held).
    Every applied change gives one alert line."""
    cur = conn.cursor()
    counts = {"insert": 0, "move": 0, "remove": 0, "held": 0}
    alerts = []
    changes = [("insert", x) for x in plan.insert] + [("move", x) for x in plan.remap] + [("remove", x) for x in plan.drop_auto]
    held = changes[max_changes:]
    changes = changes[:max_changes]
    try:
        for kind, x in changes:
            s, t = x[0], x[1]
            if kind == "insert":
                cur.execute("INSERT Integration.Tableau_PipelineTable (PipelineName, SchemaName, TableName, Evidence, ConfirmedBy, Source) "
                            "VALUES (?, ?, ?, ?, 'registry sync', 'Auto')", x[2], s, t, x[3][:400])
                alerts.append("%s.%s registered on %s (new Auto row)" % (s, t, x[2]))
            else:
                cur.execute("DELETE FROM Integration.Tableau_PipelineTable WHERE SchemaName = ? AND TableName = ? AND Source = 'Auto'", s, t)
                if kind == "move":
                    cur.execute("INSERT Integration.Tableau_PipelineTable (PipelineName, SchemaName, TableName, Evidence, ConfirmedBy, Source) "
                                "VALUES (?, ?, ?, ?, 'registry sync', 'Auto')", x[3], s, t, x[4][:400])
                alerts.append("%s.%s %s" % (s, t, "moved from %s to %s" % (x[2], x[3]) if kind == "move" else "removed (was %s)" % x[2]))
            counts[kind] += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    counts["held"] = len(held)
    for kind, x in held:
        log.error("  REGISTRY %s of %s.%s NOT applied: more than %d inserts/moves/removals in one run (REGISTRY_MAX_CHANGES, --registry-first-run for the very first "
                  "run); look at the lineage tables first", kind.upper(), x[0], x[1], max_changes)
    return counts, alerts
