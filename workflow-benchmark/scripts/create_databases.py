"""Create engine databases (operaton, flowable) in the shared PostgreSQL.

Idempotent: skips databases that already exist.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text

from app.config import get_settings

ENGINE_DBS = ("operaton", "flowable")


def main() -> int:
    base_url = get_settings().database_url
    # connect to the 'benchmark' database (or postgres) to issue CREATE DATABASE
    admin_url = base_url
    try:
        engine = create_engine(admin_url, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 5})
        with engine.connect() as conn:
            for db in ENGINE_DBS:
                exists = conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": db}
                ).scalar()
                if exists:
                    print(f"database '{db}' already exists")
                else:
                    conn.execute(text(f'CREATE DATABASE "{db}"'))
                    print(f"created database '{db}'")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
