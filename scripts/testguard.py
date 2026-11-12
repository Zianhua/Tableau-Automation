"""The *_checks.py suites write fixture rows (some inside transactions on ETL_Master_Log) into the warehouse they connect to.
They may only ever run against the dev warehouse. The server name is hard-coded here on purpose: unlike the scripts, the check
suites do not trust .env, so a host whose .env points at production cannot run them by mistake. Never deploy them to production."""
import os
import sys

DEV_ONLY_SERVER = "<DEV_DWH_SERVER>"


def require_dev(env=os.environ):
    server = str(env.get("CONN_SERVER", "")).strip().lower()
    if server.split(".")[0] != DEV_ONLY_SERVER:
        sys.exit("Refusing to run the check suite: CONN_SERVER is %r. Check suites run only against the dev warehouse (%s)."
                 % (env.get("CONN_SERVER"), DEV_ONLY_SERVER.upper()))
