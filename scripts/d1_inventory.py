"""D1: read-only inventory of Tableau workbooks with more than one data source.

Reads TABLEAU_* from .env (or environment). Only GET/query calls are made.
Outputs docs/d1_inventory.json and docs/d1_inventory.md. No secrets are written.
Usage: python scripts/d1_inventory.py
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import tableauserverclient as TSC
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

FILE_TYPES = {"excel-direct", "textscan", "csv", "hyper", "dataengine", "ogrdirect", "pdf"}
DWH_HINT = "CQGlobalSGDW"

WB_QUERY = """
query($first:Int,$offset:Int){
  workbooksConnection(first:$first, offset:$offset){
    totalCount
    nodes{
      luid name projectName
      upstreamDatasources{ luid name projectName
        upstreamTables{ name schema fullName connectionType isEmbedded database{ name connectionType } } }
      embeddedDatasources{ id name hasExtracts
        upstreamTables{ name schema fullName connectionType isEmbedded database{ name connectionType } } }
    }
  }
}"""


def tbl(t):
    db = t.get("database") or {}
    ctype = db.get("connectionType") or t.get("connectionType")
    return {
        "database": db.get("name"),
        "host": db.get("hostName"),
        "schema": t.get("schema"),
        "table": t.get("name"),
        "connection_type": ctype,
        "is_file_based": (ctype or "").lower() in FILE_TYPES,
        # Assumption (DECISIONS D-010): a SQL Server table with no database name is in CQGlobalSGDW.
        "is_dwh": DWH_HINT.lower() in (db.get("name") or "").lower()
                  or (not db.get("name") and (ctype or "").lower() == "sqlserver"),
    }


def tkey(t):
    return (t["host"], t["database"], t["schema"], t["table"])


def flag(t):
    return "FILE" if t["is_file_based"] else ("" if t["is_dwh"] else "non-DWH")


def main():
    url, site = os.environ["TABLEAU_SERVER_URL"], os.environ.get("TABLEAU_SITE_ID", "")
    auth = TSC.PersonalAccessTokenAuth(os.environ["TABLEAU_PAT_NAME"], os.environ["TABLEAU_PAT_SECRET"], site)
    server = TSC.Server(url, use_server_version=True)
    with server.auth.sign_in(auth):
        out = {"generated": datetime.now().isoformat(timespec="seconds"), "server": url,
               "rest_api_version": server.version}
        # 1. Metadata API probe
        try:
            r = server.metadata.query("{ workbooksConnection(first:1){ totalCount } }")
            ok = not r.get("errors")
            out["metadata_api"] = {"enabled": ok, "detail": None if ok else str(r.get("errors"))[:300]}
        except Exception as e:
            out["metadata_api"] = {"enabled": False, "detail": str(e)[:300]}
        print("Metadata API:", out["metadata_api"], "REST", server.version)

        # 2. REST: workbooks, views, users, subscriptions
        wbs = list(TSC.Pager(server.workbooks))
        views = list(TSC.Pager(server.views))
        users = {u.id: {"name": u.name, "email": u.email, "fullname": u.fullname} for u in TSC.Pager(server.users)}
        subs = list(TSC.Pager(server.subscriptions))
        view_to_wb = {v.id: (v.workbook_id, v.name) for v in views}
        wb_subs = defaultdict(list)
        for s in subs:
            tid, ttype = s.target.id, s.target.type
            wid, vname = (tid, None) if ttype.lower() == "workbook" else view_to_wb.get(tid, (None, None))
            u = users.get(s.user_id, {})
            wb_subs[wid].append({"subscription_id": s.id, "subject": s.subject, "target_type": ttype,
                                 "view_name": vname, "user": u.get("name"), "email": u.get("email"),
                                 "schedule_id": s.schedule_id})
        out["counts"] = {"workbooks_total": len(wbs), "subscriptions_total": len(subs)}

        # 3. Metadata: data sources and upstream tables
        meta = {}
        if out["metadata_api"]["enabled"]:
            offset, total = 0, None
            while total is None or offset < total:
                r = server.metadata.query(WB_QUERY, {"first": 50, "offset": offset})
                if r.get("errors"):
                    sys.exit("GraphQL errors: " + str(r["errors"])[:500])
                c = r["data"]["workbooksConnection"]
                total = c["totalCount"]
                for n in c["nodes"]:
                    meta[n["luid"]] = n
                offset += 50
        else:
            if "BACKFILL_RUNNING" in str(out["metadata_api"]["detail"]):
                print("Metadata API is still building its store (BACKFILL_RUNNING): results would be incomplete, rerun later")
            else:
                print("Metadata API disabled: upstream tables skipped (needs: tsm maintenance metadata-services enable)")

        # 4. Assemble
        items = []
        for luid, n in meta.items():
            dss = []
            for d in n["upstreamDatasources"]:
                dss.append({"kind": "published", "name": d["name"], "luid": d["luid"], "project": d["projectName"],
                            "tables": [tbl(t) for t in d["upstreamTables"]]})
            for d in n["embeddedDatasources"]:
                dss.append({"kind": "embedded", "name": d["name"], "has_extract": d["hasExtracts"],
                            "tables": [tbl(t) for t in d["upstreamTables"]]})
            if len(dss) <= 1:
                continue
            s = wb_subs.get(luid, [])
            items.append({"workbook": n["name"], "luid": luid, "project": n["projectName"],
                          "n_published_ds": sum(d["kind"] == "published" for d in dss),
                          "n_embedded_ds": sum(d["kind"] == "embedded" for d in dss),
                          "data_sources": dss, "subscriptions": s,
                          "recipients": sorted({x["email"] or "(no email: %s)" % x["user"] for x in s})})
        items.sort(key=lambda i: (i["project"] or "", i["workbook"]))
        distinct = {}
        for i in items:
            for d in i["data_sources"]:
                for t in d["tables"]:
                    e = distinct.setdefault(tkey(t), {**t, "workbooks": set()})
                    e["workbooks"].add(i["workbook"])
        distinct = [{**v, "workbooks": sorted(v["workbooks"])} for v in distinct.values()]
        distinct.sort(key=lambda t: tuple(x or "" for x in tkey(t)))
        out["counts"].update({"workbooks_in_metadata": len(meta), "affected_workbooks": len(items),
                              "data_sources_in_affected": sum(len(i["data_sources"]) for i in items),
                              "distinct_upstream_tables": len(distinct),
                              "affected_without_subscriptions": sum(not i["subscriptions"] for i in items)})
        out["affected"], out["distinct_tables"] = items, distinct

    (ROOT / "docs" / "d1_inventory.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    write_md(out)
    print(json.dumps(out["counts"], indent=2))


def write_md(o):
    L = ["# D1 affected-report inventory", "",
         "Generated %s from %s (REST API %s). Read-only." % (o["generated"], o["server"], o["rest_api_version"]), "",
         "Metadata API enabled: **%s**" % o["metadata_api"]["enabled"], "",
         "Counting rule: a workbook's data sources = published data sources it uses + embedded data sources "
         "(as listed by the Metadata API). Affected = more than one.", "",
         "## Counts", ""]
    L += ["- %s: %s" % (k, v) for k, v in o["counts"].items()]
    L += ["", "## Affected workbooks", ""]
    for i in o.get("affected", []):
        L += ["### %s / %s" % (i["project"], i["workbook"]), "",
              "- Data sources: %d published, %d embedded" % (i["n_published_ds"], i["n_embedded_ds"]),
              "- Recipients: %s" % (", ".join(i["recipients"]) or "NONE (no subscriptions)")]
        for s in i["subscriptions"]:
            L.append("  - subscription on %s%s: %s <%s>, subject '%s'" % (
                s["target_type"], " " + s["view_name"] if s["view_name"] else "", s["user"], s["email"], s["subject"]))
        L += ["", "| Data source | Kind | Database | Schema | Table | Conn type | Flag |", "|---|---|---|---|---|---|---|"]
        for d in i["data_sources"]:
            if not d["tables"]:
                L.append("| %s | %s | - | - | (no upstream tables) | - | CHECK |" % (d["name"], d["kind"]))
            for t in d["tables"]:
                L.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                    d["name"], d["kind"], t["database"], t["schema"], t["table"], t["connection_type"], flag(t)))
        L.append("")
    L += ["## Distinct upstream tables (feeds T3)", "",
          "| Database | Schema | Table | Conn type | Flag | Workbooks |", "|---|---|---|---|---|---|"]
    for t in o.get("distinct_tables", []):
        L.append("| %s | %s | %s | %s | %s | %d |" % (
            t["database"], t["schema"], t["table"], t["connection_type"], flag(t), len(t["workbooks"])))
    L += ["", "Note: the Metadata API does not say whether a table is a view or a base table; resolve in the DWH (T3)."]
    (ROOT / "docs" / "d1_inventory.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
