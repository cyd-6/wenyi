"""Serve the bundled SPA and existing API on one loopback origin."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware


def create_desktop_app(web_dir: Path) -> FastAPI:
    from ..main import create_app
    from ..routers.ws import router as ws_router

    api = create_app()

    @asynccontextmanager
    async def lifespan(app):
        # Starlette does not run mounted applications' lifespan automatically.
        async with api.router.lifespan_context(api):
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )
    app.mount("/api", api)
    app.include_router(ws_router)
    directory = web_dir.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path == "api" or path.startswith(("api/", "ws/")):
            raise HTTPException(404, "Not found")
        file = (directory / path).resolve()
        if not file.is_relative_to(directory):
            raise HTTPException(404, "Not found")
        if file.is_file():
            return FileResponse(file, headers={"Cache-Control": "no-cache"})
        # Missing assets must be 404 rather than HTML masquerading as JavaScript.
        if Path(path).suffix or path.startswith("assets/"):
            raise HTTPException(404, "Not found")
        return FileResponse(directory / "index.html", headers={"Cache-Control": "no-store"})

    return app


async def serve(web_dir: Path, port: int) -> None:
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(
            create_desktop_app(web_dir),
            host="127.0.0.1",
            port=port,
            access_log=False,
            loop="asyncio",
            log_level="info",
        )
    )
    stop_file = Path(os.environ["WENYI_STOP_FILE"])

    async def watch():
        while not server.should_exit:
            if stop_file.exists():
                server.should_exit = True
                return
            await asyncio.sleep(0.25)

    watcher = asyncio.create_task(watch())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
