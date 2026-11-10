"""浏览器探针的入口（做什么、怎么比见包的说明）：uv run python -m tests.browser_probe [--update]"""

import argparse
import base64
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from orbit8d.assets import HRTF_FILE
from orbit8d.config import HOST, REPO_ROOT, load_settings
from tests.browser_probe.cdp import Cdp, CdpError, Chrome, Page, PageError
from tests.browser_probe.script import SONG_SECONDS, Failure, Session, walk
from tests.browser_probe.server import REAL_PORT
from tests.conftest import write_song

HERE = Path(__file__).resolve().parent
BACKEND_DIR = HERE.parents[1]
BASELINE = HERE / "baseline.json"
WEB = REPO_ROOT / "web"
DEFAULT_PORT = 8791
STARTUP_TIMEOUT_S = 90
STOP_TIMEOUT_S = 15
SONG_SEEDS = (11, 12)


class Backend:
    """探针后台进程：可以停掉再起（“保存失败再恢复”那一步要用）。"""

    def __init__(self, data_dir: Path, port: int, log: Path):
        self.url = f"http://{HOST}:{port}/"
        self._env = {**os.environ, "ORBIT8D_DATA_DIR": str(data_dir), "ORBIT8D_PORT": str(port)}
        self._log = log
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        with self._log.open("ab") as log:
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "tests.browser_probe.server"],
                cwd=BACKEND_DIR,
                env=self._env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(f"the probe backend exited with {self._proc.returncode}; see {self._log}")
            try:
                with urllib.request.urlopen(f"{self.url}api/health", timeout=2) as resp:
                    if resp.status == 200:
                        return
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(0.2)
        raise RuntimeError(f"the probe backend did not come up within {STARTUP_TIMEOUT_S} s; see {self._log}")

    def stop(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=STOP_TIMEOUT_S)


def check_build() -> None:
    """前端构建产物必须在，而且比源码新：探针测的是 web/dist。"""
    index = WEB / "dist" / "index.html"
    if not index.exists():
        raise SystemExit("web/dist is missing: run `cd web && npm run build` first")
    built = index.stat().st_mtime
    sources = [WEB / "index.html", *(p for p in (WEB / "src").rglob("*") if p.is_file())]
    newer = [p for p in sources if p.stat().st_mtime > built]
    if newer:
        raise SystemExit(
            f"web/dist is older than {newer[0].relative_to(REPO_ROOT)}: run `cd web && npm run build` first"
        )


def link_assets(target: Path) -> None:
    """HRTF 数据只读复用（软链接），和测试的 data_dir 夹具一样。"""
    source = load_settings().assets_dir
    if not (source / HRTF_FILE).exists():
        raise SystemExit(
            f"{HRTF_FILE} is not in {source}: run `make assets`, or point ORBIT8D_DATA_DIR at a copy"
        )
    target.mkdir(parents=True)
    for item in source.iterdir():
        if item.suffix in (".sofa", ".bin"):
            os.symlink(item, target / item.name)


def compare(steps: list[dict]) -> bool:
    if not BASELINE.exists():
        print(f"no baseline yet: run again with --update to create {BASELINE.name}")
        return False
    expected = json.loads(BASELINE.read_text(encoding="utf-8"))["steps"]
    if steps == expected:
        return True
    old, new = (json.dumps(x, indent=1, ensure_ascii=False).splitlines() for x in (expected, steps))
    print("\n".join(difflib.unified_diff(old, new, "baseline", "this run", lineterm="", n=4)))
    return False


def write_baseline(steps: list[dict]) -> None:
    doc = {
        "about": "Recorded by tests.browser_probe (see its docstring for what is compared)",
        "steps": steps,
    }
    BASELINE.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def run(work: Path, port: int) -> tuple[list[dict], list[str], dict]:
    link_assets(work / "data" / "assets")
    (work / "songs").mkdir()
    (work / "downloads").mkdir()
    songs = tuple(
        write_song(work / "songs" / f"probe-song-{name}.wav", SONG_SECONDS, seed)
        for name, seed in zip("ab", SONG_SEEDS, strict=True)
    )
    backend = Backend(work / "data", port, work / "backend.log")
    chrome = None
    cdp = None
    try:
        backend.start()
        chrome = Chrome(work / "chrome", work / "chrome.log")
        cdp = Cdp(chrome.endpoint)
        cdp.call(
            "Browser.setDownloadBehavior",
            {"behavior": "allowAndName", "downloadPath": str(work / "downloads")},
        )
        injected = "\n".join((HERE / name).read_text(encoding="utf-8") for name in ("recorder.js", "ui.js"))
        page = Page(cdp, injected)
        session = Session(page, backend.url, songs, work / "downloads", backend.stop, backend.start)
        try:
            walk(session)
        except (Failure, PageError, CdpError) as exc:
            shot = work / "failure.png"
            save_screenshot(page, shot)
            session.problems.append(f"stopped after {len(session.steps)} steps: {exc} (screenshot: {shot})")
        return session.steps, session.problems, page_stats(page)
    finally:
        if cdp is not None:
            cdp.close()
        if chrome is not None:
            chrome.close()
        backend.stop()


def page_stats(page: Page) -> dict:
    """动画帧数与最长的帧间隔：只是参考（机器太忙时探针更容易出偶发差异），不参与比对。"""
    try:
        return page.run("__probe.stats()")
    except (PageError, CdpError):
        return {}


def save_screenshot(page: Page, path: Path) -> None:
    try:
        data = page.call("Page.captureScreenshot", {"format": "png"})["data"]
        path.write_bytes(base64.b64decode(data))
    except CdpError as exc:
        print(f"could not take a screenshot: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.browser_probe", description="Orbit 8D browser probe"
    )
    parser.add_argument("--update", action="store_true", help="rewrite baseline.json from this run")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"backend port (not {REAL_PORT})")
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory (data, logs)")
    parser.add_argument("--out", type=Path, help="also write this run's record to a JSON file")
    args = parser.parse_args()
    if args.port == REAL_PORT:
        parser.error(f"port {REAL_PORT} is reserved for the real server")
    check_build()
    work = Path(tempfile.mkdtemp(prefix="orbit8d-probe-"))
    started = time.monotonic()
    failed = True
    try:
        steps, problems, stats = run(work, args.port)
        if args.out:
            args.out.write_text(json.dumps(steps, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        for problem in problems:
            print(f"PROBLEM  {problem}")
        if problems:
            verdict = "FAILED"
        elif args.update:
            write_baseline(steps)
            verdict = "BASELINE WRITTEN"
        else:
            verdict = "MATCHES THE BASELINE" if compare(steps) else "DIFFERS FROM THE BASELINE"
        failed = bool(problems) or verdict.startswith("DIFFERS")
        events = sum(
            len(s["messages"]) + len(s["graph"]) + sum(map(len, s["params"].values())) for s in steps
        )
        print(
            f"browser probe: {len(steps)} steps, {events} audio events, {len(problems)} problems, "
            f"{time.monotonic() - started:.0f} s, frames {stats.get('frames', '?')}, "
            f"longest frame gap {stats.get('maxFrameGapMs', '?')} ms -> {verdict}"
        )
    finally:
        if args.keep or failed:
            print(f"scratch directory kept: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
