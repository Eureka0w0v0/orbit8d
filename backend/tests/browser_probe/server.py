"""探针用的后台：真实的 create_app + 测试里的假分轨器（不跑 Demucs、不下载模型）。

    ORBIT8D_DATA_DIR=<临时目录> ORBIT8D_PORT=<端口> uv run python -m tests.browser_probe.server

只能用在临时数据目录上：仓库自带的 data/ 是用户的真实数据，对它起服务会触发存储迁移，所以直接拒绝。
"""

import sys

import uvicorn

from orbit8d.api.app import create_app
from orbit8d.config import HOST, REPO_ROOT, load_settings
from orbit8d.logging_setup import setup_logging
from tests.conftest import FakeSeparator

REAL_DATA_DIR = (REPO_ROOT / "data").resolve()
REAL_PORT = 8765  # 正式使用的端口留给用户


def main() -> int:
    settings = load_settings()
    if settings.data_dir == REAL_DATA_DIR or settings.port == REAL_PORT:
        print(
            "The probe server only runs on a scratch ORBIT8D_DATA_DIR and a port other than 8765",
            file=sys.stderr,
        )
        return 2
    setup_logging(settings.logs_dir)
    uvicorn.run(
        create_app(settings, FakeSeparator()),
        host=HOST,
        port=settings.port,
        log_config=None,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
