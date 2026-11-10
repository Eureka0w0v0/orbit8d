"""入口层的防护与统一的错误应答（SPEC §7）：只接受本机的 Host（防 DNS 重绑定），跨站的写请求一律 403，
出错时一律回 {code, message}（message 是英文技术说明，界面按 code 翻译）。"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from orbit8d.jobs.store import NotFound
from orbit8d.media.ffmpeg import MediaError

ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
DEV_WEB_PORT = 5173
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class LocalOriginMiddleware:
    """浏览器发起的跨站写请求一定带 Origin；不在白名单里的直接 403（防 CSRF）。"""

    def __init__(self, app: ASGIApp, allowed_origins: set[str]):
        self.app, self.allowed = app, allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            origin = dict(scope["headers"]).get(b"origin")
            if origin is not None and origin.decode("latin-1") not in self.allowed:
                body = {"code": "FORBIDDEN_ORIGIN", "message": "Cross-site request refused"}
                await JSONResponse(body, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)


async def _media_error(_: Request, exc: MediaError) -> JSONResponse:
    return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=400)


async def _not_found(_: Request, exc: NotFound) -> JSONResponse:
    return JSONResponse({"code": "NOT_FOUND", "message": "Not found"}, status_code=404)


def install_guards(app: FastAPI, port: int) -> None:
    """port：本服务的端口；它和 Vite 开发服务器的源是仅有的两个可以发写请求的源。"""
    origins = {f"http://{host}:{p}" for host in ALLOWED_HOSTS for p in (port, DEV_WEB_PORT)}
    app.add_middleware(LocalOriginMiddleware, allowed_origins=origins)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)  # 最外层
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(MediaError, _media_error)
    app.add_exception_handler(NotFound, _not_found)
