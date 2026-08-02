"""Application entry point: FastAPI app factory and Uvicorn runner.

The engine's heavy state — the proxy bridge daemon, the curl_cffi session and
the cookie-refresh watchdog — is owned by the app lifespan, so exactly one of
each exists per process and all three are torn down cleanly on shutdown.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.endpoints import router
from .config import Settings, get_settings
from .core.requester import HybridRequester

logger = logging.getLogger(__name__)


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)-38s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    # These two are extremely chatty at DEBUG and drown out our own logs.
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("starting %s (%s)", settings.app_name, settings.environment)

    requester = HybridRequester(settings)
    try:
        await requester.start()
    except Exception:
        logger.exception("engine failed to start")
        raise
    app.state.requester = requester
    app.state.settings = settings

    if settings.has_proxies:
        try:
            results = await requester.pool.health_check(
                settings.proxy_healthcheck_url,
                timeout=settings.proxy_healthcheck_timeout,
                impersonate=requester.profile.impersonate,
            )
            healthy = sum(1 for ok in results.values() if ok)
            logger.info("proxy health: %d/%d usable", healthy, len(results))
            if healthy == 0:
                # Not fatal: the pool force-releases a benched proxy rather than
                # failing a scrape, and the exits may simply be cold.
                logger.error("no proxy passed the health check — expect blocks")
        except Exception:
            logger.exception("initial proxy health check failed")

    try:
        yield
    finally:
        logger.info("shutting down engine")
        await requester.close()
        app.state.requester = None


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="2.0.0",
        description=(
            "Enterprise B2B analytics engine for Ozon sellers: review harvesting, "
            "regional pricing and SPP, SERP tracking, stock estimation, negative-"
            "review clustering and dynamic repricing."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.environment == "development" else [],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    app.include_router(router)

    @app.get("/", include_in_schema=False)
    async def root() -> JSONResponse:
        return JSONResponse(
            {
                "service": settings.app_name,
                "version": "2.0.0",
                "docs": "/docs",
                "health": "/api/v1/health",
            }
        )

    return app


app = create_app()


def run() -> None:
    """Console entry point: ``python -m ozon_anti_bot_engine.main``."""
    import uvicorn

    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        "ozon_anti_bot_engine.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        reload=settings.environment == "development",
    )


if __name__ == "__main__":
    run()
