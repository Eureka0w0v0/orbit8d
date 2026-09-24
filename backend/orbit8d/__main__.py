"""启动 Orbit 8D：uv run python -m orbit8d [--no-browser]"""

import argparse
import os
import threading
import webbrowser

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")  # 必须在导入 torch 之前

import uvicorn  # noqa: E402

from orbit8d.api.app import create_app  # noqa: E402
from orbit8d.config import HOST, load_settings  # noqa: E402
from orbit8d.logging_setup import setup_logging  # noqa: E402
from orbit8d.separate.demucs_runner import DemucsSeparator  # noqa: E402

BROWSER_DELAY_S = 1.5


def main() -> None:
    parser = argparse.ArgumentParser(prog="orbit8d", description="Orbit 8D 本地服务")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args()
    settings = load_settings()
    setup_logging(settings.logs_dir)
    app = create_app(settings, DemucsSeparator())
    if not args.no_browser:
        url = f"http://{HOST}:{settings.port}/"
        threading.Timer(BROWSER_DELAY_S, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=HOST, port=settings.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
