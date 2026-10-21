"""``python -m rd_db.migrate`` — apply migrations to head (used by the compose ``migrate`` service and setup)."""

from __future__ import annotations

from alembic import command
from alembic.config import Config

from rd_core.logging import configure_logging, get_logger
from rd_core.settings import get_settings
from rd_db.health import MIGRATIONS_DIR


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


def upgrade_to_head() -> None:
    command.upgrade(alembic_config(), "head")


def main() -> None:
    settings = get_settings()
    configure_logging(service="rd-migrate", level=settings.log_level, fmt=settings.log_format)
    log = get_logger(__name__)
    log.info("migrate.start")
    upgrade_to_head()
    log.info("migrate.done")


if __name__ == "__main__":
    main()
