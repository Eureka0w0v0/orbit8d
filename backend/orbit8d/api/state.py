"""服务启动时（lifespan）才建好的对象：HRTF 要先加载，建 app 的时候还没有渲染器和任务队列。
它们挂在 app.state 上（测试也从那里取）；路由经这里读，带上类型。"""

from fastapi import Request

from orbit8d.engine.mixdown import Renderer
from orbit8d.jobs.songs import Songs


def songs_of(request: Request) -> Songs:
    return request.app.state.songs


def renderer_of(request: Request) -> Renderer:
    return request.app.state.renderer
