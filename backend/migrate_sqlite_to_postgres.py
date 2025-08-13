"""
Run once to copy all tables from SQLite -> Postgres.
Usage:
  docker compose exec app python -m backend.migrate_sqlite_to_postgres
"""

import os
from typing import List, Type
from sqlmodel import SQLModel, Session, create_engine, select

from .models import (
    SQLModel,  # already defines metadata
    TelegramMessage,
    MetricsCache,
    # TODO: add any other models you have (OAuthToken, Strava models, etc.)
)

SRC_DB_URL = os.getenv("SRC_DB_URL", "sqlite:///data/app.db")
DST_DB_URL = os.getenv("DST_DB_URL", "postgresql+psycopg2://fwm:change-me@db:5432/fwm")

def main():
    src_engine = create_engine(SRC_DB_URL, connect_args={"check_same_thread": False})
    dst_engine = create_engine(DST_DB_URL, pool_pre_ping=True)

    SQLModel.metadata.create_all(dst_engine)

    models: List[Type[SQLModel]] = [
        TelegramMessage,
        MetricsCache,
        # add additional models here if present
    ]

    with Session(src_engine) as s_src, Session(dst_engine) as s_dst:
        for Model in models:
            rows = s_src.exec(select(Model)).all()
            print(f"Migrating {Model.__name__}: {len(rows)} rows")
            for r in rows:
                s_dst.merge(r)
            s_dst.commit()

    print("Migration complete.")

if __name__ == "__main__":
    main()
