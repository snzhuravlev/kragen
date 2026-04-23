"""FastAPI application entrypoint."""

import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from kragen import __version__
from kragen.api.routes import (
    admin,
    files,
    health,
    messages,
    plugins as plugins_routes,
    sessions,
    tasks,
    workspaces,
)
from kragen.config import get_settings
from kragen.db.session import engine
from kragen.logging_config import configure_logging, get_logger
from kragen.plugins.manager import get_plugin_manager
from kragen.storage import object_store

logger = get_logger(__name__)


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Attach X-Request-ID to response headers for clients."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        cid = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.correlation_id = cid
        response = await call_next(request)
        response.headers["X-Request-ID"] = cid
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """Startup: object storage + plugins; shutdown: plugins, then dispose DB pool."""
    try:
        await object_store.ensure_bucket_exists()
    except Exception as exc:
        logger.warning("object_storage_bucket_init_failed", error=str(exc))
    try:
        # Plugins are already discovered and setup() was invoked in create_app()
        # before the router mount — the block here only handles shutdown.
        pass
    except Exception as exc:
        logger.warning("plugin_manager_init_failed", error=str(exc))
    yield
    try:
        await get_plugin_manager().shutdown()
    except Exception as exc:  # noqa: BLE001
        logger.warning("plugin_manager_shutdown_failed", error=str(exc))
    await engine.dispose()


def create_app() -> FastAPI:
    """Build FastAPI app with all routers."""
    settings = get_settings()
    configure_logging(settings.app.log_level)

    app = FastAPI(
        title=settings.app.name,
        version=__version__,
        description="Multi-channel agent platform API gateway",
        lifespan=lifespan,
    )
    app.add_middleware(CorrelationMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.http.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(workspaces.router)
    app.include_router(sessions.router)
    app.include_router(messages.router)
    app.include_router(tasks.router)
    app.include_router(files.router)
    app.include_router(admin.router)
    app.include_router(plugins_routes.router)

    # Plugin bootstrap: discover + setup(), then mount backend-kind routers.
    # Initialization happens here (not in lifespan) so backend routers are
    # known before the FastAPI router tree is finalized.
    plugin_manager = get_plugin_manager()
    try:
        plugin_manager.initialize()
    except Exception as exc:  # noqa: BLE001
        logger.warning("plugin_manager_init_failed", error=str(exc))

    for backend in plugin_manager.all_backends():
        app.include_router(
            backend.router,
            prefix=backend.prefix,
            tags=list(backend.tags) if backend.tags else None,
        )

    web_root = Path(__file__).resolve().parents[3] / "web"
    if web_root.is_dir():
        app.mount("/ui", StaticFiles(directory=web_root, html=True), name="ui")
    return app


app = create_app()


def run() -> None:
    """CLI entrypoint for uvicorn."""
    settings = get_settings()
    uvicorn.run(
        "kragen.api.main:app",
        host=settings.api.host,
        port=settings.api.port,
        reload=settings.app.environment == "local",
    )


if __name__ == "__main__":
    run()
