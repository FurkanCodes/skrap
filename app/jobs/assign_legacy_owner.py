from __future__ import annotations

import argparse
from datetime import UTC, datetime

from app.core.config import get_settings
from app.infrastructure.postgres_tracking_repository import PostgresTrackingRepository


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assign legacy tracked data rows to one app user_id.",
    )
    parser.add_argument(
        "--user-id",
        required=True,
        help="Destination app user_id for legacy rows.",
    )
    args = parser.parse_args()

    settings = get_settings()
    repository = PostgresTrackingRepository(
        database_url=settings.tracking_database_url,
        min_pool_size=settings.tracking_db_pool_min_size,
        max_pool_size=settings.tracking_db_pool_max_size,
    )
    repository.initialize()
    try:
        repository.assign_all_legacy_data_to_user(
            user_id=args.user_id.strip(),
            now=datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        )
    finally:
        repository.close()


if __name__ == "__main__":
    main()

