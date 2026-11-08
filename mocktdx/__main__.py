"""CLI: python -m mocktdx <serve|seed|import-people <csv>|token>"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import db, settings as settings_module, tdx
from .seed import seed
from .util import configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mocktdx", description="TeamDynamix-style ITSM stand-in")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="Run the agent UI, client portal and Web API")
    sub.add_parser("seed", help="Create the database and reference data (no-op if seeded)")
    p_imp = sub.add_parser("import-people", help="Import an HR roster CSV into People (match on Associate ID)")
    p_imp.add_argument("csv")
    sub.add_parser("token", help="Print an admin API token (same as POST /TDWebApi/api/auth/loginadmin)")
    args = parser.parse_args(argv)
    configure_logging()
    s = settings_module.load()

    if args.cmd == "serve":
        import uvicorn
        uvicorn.run("mocktdx.web:app", host=s.host, port=s.port, log_config=None)
        return 0
    conn = db.connect(s.db_path)
    try:
        if args.cmd == "seed":
            print(json.dumps(seed(conn, s), indent=2))
        elif args.cmd == "import-people":
            if not db.is_seeded(conn):
                seed(conn, s)
            print(json.dumps(tdx.import_people_csv(conn, Path(args.csv).read_text(encoding="utf-8-sig"), source=f"cli:{Path(args.csv).name}"), indent=2))
        elif args.cmd == "token":
            token = tdx.login_admin(s, s.beid, s.web_services_key)
            if not token:
                print("TDX_WEB_SERVICES_KEY is not set", file=sys.stderr)
                return 1
            print(token)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
