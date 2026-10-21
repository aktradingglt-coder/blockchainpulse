"""``python -m rd_db.migrate`` — apply migrations to head (used by the compose ``migrate`` service and setup)."""

from __future__ import annotations

import time
from collections.abc import Callable

from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError

from rd_core.bootstrap import bootstrap
from rd_core.logging import get_logger
from rd_db.health import MIGRATIONS_DIR


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


#: Attempts and pause when the database refuses connections (e.g. restarting at the end of first-boot init).
CONNECT_ATTEMPTS = 5
CONNECT_PAUSE_S = 2.0


def upgrade_to_head(*, sleep: Callable[[float], None] = time.sleep) -> None:
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            command.upgrade(alembic_config(), "head")
        except (OSError, DBAPIError) as exc:
            if attempt == CONNECT_ATTEMPTS or not _is_connection_error(exc):
                raise
            get_logger(__name__).warning("migrate.db_unavailable", attempt=attempt, error=type(exc).__name__)
            sleep(CONNECT_PAUSE_S)
        else:
            return


def _is_connection_error(exc: BaseException) -> bool:
    """Refused/reset connections are worth a retry; SQL errors in a migration are not."""
    if isinstance(exc, DBAPIError):
        return bool(exc.connection_invalidated) or isinstance(exc.orig, OSError)
    return isinstance(exc, (ConnectionError, OSError))


def main() -> None:
    bootstrap("rd-migrate")
    log = get_logger(__name__)
    log.info("migrate.start")
    upgrade_to_head()
    log.info("migrate.done")


if __name__ == "__main__":
    main()
