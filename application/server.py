import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from application.api.routes_auth import router as auth_router
from application.api.routes_config import router as config_router
from application.api.routes_tasks import router as tasks_router
from application.api.routes_chat import router as chat_router
from application.task_store import init_db
from application.task_store_persistence import (
    flush_persist,
    persistence_enabled,
    persistent_db_path,
    restore_tasks_db,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("server")

_APPLICATION_DIR = os.path.dirname(os.path.abspath(__file__))
_WEB_DIST = os.path.join(_APPLICATION_DIR, "web", "dist")

# Swagger/ReDoc/OpenAPI — off by default (ECS/production).
# Set ENABLE_API_DOCS=1 for local debugging (run_local.sh sets this).
_ENABLE_API_DOCS = os.environ.get("ENABLE_API_DOCS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    restore_tasks_db()
    init_db()
    if persistence_enabled():
        logger.info("Task store persistence enabled: %s", persistent_db_path())
    else:
        logger.info("Task store using local SQLite only")
    yield
    flush_persist()
    logger.info("Task store shutdown persist complete")


app = FastAPI(
    title="Harness UI",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _ENABLE_API_DOCS else None,
    redoc_url="/redoc" if _ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if _ENABLE_API_DOCS else None,
)

app.include_router(auth_router)
app.include_router(config_router)
app.include_router(tasks_router)
app.include_router(chat_router)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


if os.path.isdir(_WEB_DIST):
    assets_dir = os.path.join(_WEB_DIST, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        if full_path:
            file_path = os.path.join(_WEB_DIST, full_path)
            if os.path.isfile(file_path):
                return FileResponse(file_path)
        index_path = os.path.join(_WEB_DIST, "index.html")
        if os.path.isfile(index_path):
            return FileResponse(index_path)
        return HTMLResponse(
            "<h1>Frontend not built</h1>"
            "<p>Run <code>cd application/web && npm install && npm run build</code></p>",
            status_code=503,
        )
else:

    @app.get("/")
    def frontend_missing() -> HTMLResponse:
        return HTMLResponse(
            "<!doctype html><html lang='ko'><head><meta charset='UTF-8' />"
            "<title>Harness UI</title></head><body>"
            "<h1>Frontend not built</h1>"
            "<p>Run <code>cd application/web && npm install && npm run build</code>, "
            "then restart the server.</p>"
            "</body></html>",
            status_code=503,
        )
