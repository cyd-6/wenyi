"""Read-only source export entry point; uses the installed application's locks/schema."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List or export idle official Wenyi Docker projects"
    )
    parser.add_argument("command", choices=["list", "export"])
    parser.add_argument(
        "--project", action="append", default=[], help="Repeat for selected project IDs"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true", help="Print the project list as JSON")
    args = parser.parse_args()
    import wenyi_api

    # Supply only migration code; all source business code comes from the running image.
    wenyi_api.__path__.insert(0, str(Path(sys.argv[0]).resolve()) + "/wenyi_api")
    from psycopg_pool import ConnectionPool
    from wenyi_api.config import settings
    from wenyi_api.db import pool as db
    from wenyi_api.transfer.archive import check_schema
    from wenyi_api.transfer.exporter import export_projects

    try:
        with ConnectionPool(settings.psycopg_dsn, min_size=1, max_size=3) as pool:
            pool.wait()
            db._pool = pool
            with pool.connection() as conn:
                check_schema(conn)
                rows = conn.execute(
                    "SELECT id,name,status,fmt FROM projects ORDER BY created_at,id"
                ).fetchall()
            if args.command == "list":
                if args.json:
                    print(
                        json.dumps(
                            [dict(zip(("id", "name", "status", "format"), row)) for row in rows],
                            ensure_ascii=False,
                        )
                    )
                else:
                    print("ID\tSTATUS\tFORMAT\tNAME")
                    for pid, name, status, fmt in rows:
                        print(f"{pid}\t{status}\t{fmt}\t{name}")
            else:
                if not args.output or not args.project:
                    parser.error("export requires --output and at least one --project")
                if args.output.exists():
                    raise ValueError("Output already exists; choose a new filename")
                manifest = export_projects(pool, Path(settings.data_dir), args.project, args.output)
                print(f"Exported {len(manifest['projects'])} project(s) to {args.output}")
    except Exception as error:
        print(f"Transfer failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
