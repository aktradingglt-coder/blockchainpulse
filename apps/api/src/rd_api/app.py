"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from sqlalchemy.ext.asyncio import AsyncEngine

from rd_api.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from rd_api.routes import system
from rd_core.bootstrap import Runtime, bootstrap
from rd_db.engine import make_engine

SERVICE = "rd-api"


@dataclass
class AppState:
    runtime: Runtime
    engine: AsyncEngine
    redis: aioredis.Redis


def create_app(runtime: Runtime | None = None) -> FastAPI:
    rt = runtime or bootstrap(SERVICE)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(rt.settings)
        client = aioredis.from_url(str(rt.settings.redis_url))
        app.state.rd = AppState(runtime=rt, engine=engine, redis=client)
        try:
            yield
        finally:
            await client.aclose()
            await engine.dispose()

    app = FastAPI(
        title=f"{rt.config.product.identity.product_name} API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if rt.settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if rt.settings.is_production else "/openapi.json",
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=rt.settings.is_production)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=rt.settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["authorization", "content-type", "x-request-id", "idempotency-key"],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware)
    app.include_router(system.router)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
    return app
