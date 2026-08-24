#!/usr/bin/env python3
"""Wait until PostgreSQL answers SELECT 1. Used by `make up-db`."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text

from app.config import get_settings


def main() -> int:
    url = get_settings().database_url
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            engine = create_engine(url, connect_args={"connect_timeout": 3})
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            print("database ready")
            return 0
        except Exception as exc:
            sys.stderr.write(f"waiting for database... {exc}\n")
            sys.stderr.flush()
            time.sleep(2)
    print("database not ready in time", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
