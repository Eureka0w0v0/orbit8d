"""无头 Chrome 与 DevTools 协议（CDP）的最小客户端：只包了探针用到的那几条命令。

Chrome 用独立的临时用户目录启动（不碰正在用的浏览器），静音，WebGL 走 SwiftShader 软件渲染，
允许不经点击就播放（探针里的“点击”是脚本触发的）。WebSocket 用 uvicorn[standard] 带来的 websockets。
"""

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

CHROME_ENV = "ORBIT8D_CHROME"
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
    "chromium-browser",
)
PORT_FILE = "DevToolsActivePort"
STARTUP_TIMEOUT_S = 30
CALL_TIMEOUT_S = 30
POLL_S = 0.1
WINDOW = (1280, 800)
KEY_META, KEY_SHIFT = 4, 8


class CdpError(RuntimeError):
    pass


def find_chrome() -> str:
    for candidate in (os.environ.get(CHROME_ENV), *CHROME_CANDIDATES):
        if not candidate:
            continue
        found = candidate if Path(candidate).is_file() else shutil.which(candidate)
        if found:
            return found
    raise CdpError(f"Chrome was not found; set {CHROME_ENV} to its executable")


class Chrome:
    """一个无头 Chrome 进程；退出时连同它的临时用户目录一起清掉（目录由调用方提供并删除）。"""

    def __init__(self, profile: Path, log: Path):
        args = [
            find_chrome(),
            "--headless=new",
            f"--user-data-dir={profile}",
            "--remote-debugging-port=0",  # 系统分配端口，写在用户目录的 DevToolsActivePort 里
            "--autoplay-policy=no-user-gesture-required",
            "--mute-audio",
            "--use-angle=swiftshader",
            "--enable-unsafe-swiftshader",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-extensions",
            "--disable-sync",
            "--lang=en-US",
            f"--window-size={WINDOW[0]},{WINDOW[1]}",
            "about:blank",
        ]
        self._log = log.open("wb")
        self.proc = subprocess.Popen(args, stdout=self._log, stderr=subprocess.STDOUT)
        self.endpoint = self._endpoint(profile / PORT_FILE)

    def _endpoint(self, port_file: Path) -> str:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise CdpError(f"Chrome exited with {self.proc.returncode} during startup")
            if port_file.exists():
                lines = port_file.read_text(encoding="utf-8").split()
                if len(lines) >= 2:
                    return f"ws://127.0.0.1:{lines[0]}{lines[1]}"
            time.sleep(POLL_S)
        raise CdpError("Chrome did not open its DevTools port in time")

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self._log.close()


class Cdp:
    """发命令、等对应的应答；事件由后台线程收进队列，调用方自己取。"""

    def __init__(self, endpoint: str):
        self._ws = connect(endpoint, max_size=None)
        self._lock = threading.Lock()
        self._next_id = 0
        self._waiting: dict[int, queue.Queue] = {}
        self._events: list[dict] = []
        self._reader = threading.Thread(target=self._read, name="cdp-reader", daemon=True)
        self._reader.start()

    def _read(self) -> None:
        try:
            for raw in self._ws:
                message = json.loads(raw)
                if "id" in message:
                    with self._lock:
                        box = self._waiting.pop(message["id"], None)
                    if box is not None:
                        box.put(message)
                else:
                    with self._lock:
                        self._events.append(message)
        except ConnectionClosed:
            pass  # 浏览器关了：还在等应答的调用会各自超时报错

    def call(
        self,
        method: str,
        params: dict | None = None,
        session: str | None = None,
        timeout: float = CALL_TIMEOUT_S,
    ):
        box: queue.Queue = queue.Queue(1)
        with self._lock:
            self._next_id += 1
            call_id = self._next_id
            self._waiting[call_id] = box
        payload = {"id": call_id, "method": method, "params": params or {}}
        if session:
            payload["sessionId"] = session
        self._ws.send(json.dumps(payload))
        try:
            message = box.get(timeout=timeout)
        except queue.Empty:
            raise CdpError(f"{method} got no answer within {timeout} s") from None
        if "error" in message:
            raise CdpError(f"{method}: {message['error'].get('message')}")
        return message.get("result", {})

    def take_events(self) -> list[dict]:
        with self._lock:
            events, self._events = self._events, []
        return events

    def close(self) -> None:
        self._ws.close()
        self._reader.join(timeout=5)


class PageError(RuntimeError):
    """页面里的脚本抛了错，或者等的条件一直没满足。"""


class Page:
    """一个标签页：执行脚本、等条件、点击、按键、鼠标拖动、给文件框塞文件。"""

    def __init__(self, cdp: Cdp, recorder: str):
        self.cdp = cdp
        target = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.session = cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        for domain in ("Page", "Runtime", "Log", "DOM"):
            self.call(f"{domain}.enable")
        self.call("Page.addScriptToEvaluateOnNewDocument", {"source": recorder})

    def call(self, method: str, params: dict | None = None, timeout: float = CALL_TIMEOUT_S):
        return self.cdp.call(method, params, self.session, timeout)

    def navigate(self, url: str) -> None:
        self.call("Page.navigate", {"url": url})

    def run(self, expression: str, timeout: float = CALL_TIMEOUT_S):
        """在页面里求值（Promise 会等它完成），返回 JSON 化的结果。"""
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True, "userGesture": True},
            timeout,
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            text = details.get("exception", {}).get("description") or details.get("text")
            raise PageError(f"{text}\n  while evaluating: {expression[:200]}")
        return result["result"].get("value")

    def wait(self, condition: str, what: str, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while not self.run(f"Boolean({condition})"):
            if time.monotonic() > deadline:
                raise PageError(f"timed out after {timeout:.0f} s waiting for: {what}")
            time.sleep(POLL_S)

    def click(self, selector: str) -> None:
        self.run(
            f"""(() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) throw new Error("no element matches " + {json.dumps(selector)});
                if (el.disabled) throw new Error("the element is disabled: " + {json.dumps(selector)});
                el.click();
            }})()"""
        )

    def key(self, key: str, code: str, key_code: int, modifiers: int = 0) -> None:
        base = {"key": key, "code": code, "windowsVirtualKeyCode": key_code, "modifiers": modifiers}
        self.call("Input.dispatchKeyEvent", {"type": "rawKeyDown", **base})
        self.call("Input.dispatchKeyEvent", {"type": "keyUp", **base})

    def mouse(self, kind: str, x: float, y: float) -> None:
        """kind：mousePressed / mouseMoved / mouseReleased；移动时左键一直按着（拖动）。"""
        held = 0 if kind == "mouseReleased" else 1
        self.call(
            "Input.dispatchMouseEvent",
            {"type": kind, "x": x, "y": y, "button": "left", "buttons": held, "clickCount": 1},
        )

    def set_files(self, selector: str, files: list[Path]) -> None:
        found = self.call(
            "Runtime.evaluate", {"expression": f"document.querySelector({json.dumps(selector)})"}
        )
        object_id = found["result"].get("objectId")
        if not object_id:
            raise PageError(f"no element matches {selector}")
        self.call("DOM.setFileInputFiles", {"files": [str(f) for f in files], "objectId": object_id})
