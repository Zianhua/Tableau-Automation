"""Shared fixture helper for the check scripts (t4_checks, t11_checks, r1_checks).

Dev's Integration.ETL_Master_Log has no row for some pipelines the registry names (no DailyETL row at all), so every extract-mapped
source shows PipelineKnown = 0 / UnknownPipeline and the checks that need a real refresh target fail on that data, not on the code (D-071).
The checks must not depend on today's real ETL rows and must not leave a permanent row: call this INSIDE a transaction that the caller
always rolls back (autocommit connection: BEGIN TRAN ... finally ROLLBACK).
"""


def known_pipelines(cur):
    """Inside a rolled-back transaction: one ETL_Master_Log row (year 2001, so it never counts as 'today') for every registered pipeline
    that has none. Only PipelineKnown changes; no pipeline gets a run today."""
    cur.execute("IF @@TRANCOUNT = 0 THROW 50000, 'known_pipelines needs an open transaction', 1")
    cur.execute("INSERT Integration.ETL_Master_Log (PipelineName, EndTime, Success) "
                "SELECT DISTINCT p.PipelineName, '2001-01-01 00:00:00', 1 FROM Integration.Tableau_PipelineTable p "
                "WHERE NOT EXISTS (SELECT 1 FROM Integration.ETL_Master_Log l "
                "WHERE l.PipelineName COLLATE Latin1_General_BIN2 = p.PipelineName COLLATE Latin1_General_BIN2)")
