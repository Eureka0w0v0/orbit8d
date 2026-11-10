"""组装 HTTP 服务（SPEC §7）：启动流程、防护、各组路由、托管前端构建产物。只监听 127.0.0.1。
Host 与跨站防护、错误应答的格式见 api/guards.py；接口按领域分在 api/routes_*.py。"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles

from orbit8d import __version__
from orbit8d.api.guards import install_guards
from orbit8d.api.routes_assets import assets_router
from orbit8d.api.routes_exports import exports_router
from orbit8d.api.routes_projects import projects_router
from orbit8d.api.routes_scene import scene_router
from orbit8d.assets import ensure_hrtf_sofa, prune_stale_assets
from orbit8d.config import REPO_ROOT, SAMPLE_RATE, Settings
from orbit8d.engine.hrtf import load_grid, to_bytes
from orbit8d.engine.pipeline import Renderer
from orbit8d.jobs.songs import Songs
from orbit8d.jobs.store import Store
from orbit8d.jobs.worker import JobRunner, Separator

log = logging.getLogger(__name__)

WEB_DIST = REPO_ROOT / "web" / "dist"


def housekeeping(store: Store, settings: Settings) -> None:
    """启动时清理：回收目录、没传完的上传、不再使用的旧缓存。"""
    store.empty_trash()
    uploads = store.clean_uploads()
    assets = prune_stale_assets(settings.assets_dir)
    log.info("housekeeping done", extra={"event": "server.cleanup", "uploads": uploads, "assets": assets})


def create_app(settings: Settings, separator: Separator) -> FastAPI:
    store = Store(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        grid = await run_in_threadpool(
            load_grid, ensure_hrtf_sofa(settings.assets_dir), settings.assets_dir, SAMPLE_RATE
        )
        renderer = Renderer(grid)
        runner = JobRunner(store, renderer, separator)
        housekeeping(store, settings)
        store.recover_interrupted()
        runner.migrate_storage()  # 先迁移存储，再按需重新分析
        runner.refresh_stale()
        runner.start()
        app.state.store, app.state.runner, app.state.renderer = store, runner, renderer
        app.state.songs = Songs(store, runner, settings.max_duration_s)
        app.state.hrtf_bytes = to_bytes(grid)
        log.info("server ready", extra={"event": "server.ready", "port": settings.port})
        try:
            yield
        finally:
            runner.stop()

    app = FastAPI(
        title="Orbit 8D",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_guards(app, settings.port)
    for router in (
        assets_router(settings),
        scene_router(store),
        projects_router(store, settings),
        exports_router(store),
    ):
        app.include_router(router)
    if WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
    return app
