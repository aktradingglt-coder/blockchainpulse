"""ASGI entry point: ``uvicorn rd_api.main:app``."""

from __future__ import annotations

from rd_api.app import create_app

app = create_app()
