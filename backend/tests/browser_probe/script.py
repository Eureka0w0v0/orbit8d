"""探针走的流程：一步一步操作界面、断言界面状态，并收下每一步的音频记录（比什么、不比什么见包的说明）。"""

import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from tests.browser_probe.cdp import KEY_META, Page, PageError

FILE_INPUT = ".overlay input[type=file]"
PLAY = ".transport .play"
DRUMS_MUTE = ".track-row:nth-child(2) .chip"
SINGER_PRESET = ".presets .preset:nth-child(3)"  # ✨、经典、歌手绕着你转……
AUTO_CHOREOGRAPHY = ".presets .preset.auto"
SHAPE = '.panel.right .segmented.grid3 button[data-value="{}"]'
TAB = '.panel.right .segmented.tabs button[data-value="{}"]'
ROOM = '.panel.right .segmented:not(.tabs) button[data-value="{}"]'
OTHER_SONG = ".song-menu .song-row:not(.current)"
OPEN_SONG = ".song-menu .song-row.current"
SONG_SECONDS = 20.0
READY_TIMEOUT_S = 240
SETTLE_TIMEOUT_S = 90
EDIT_GAP_MS = 700  # 两次修改至少隔这么久：不到 0.5 秒的连续修改会并成一步撤销
MIN_DOWNLOAD_BYTES = 10_000
POSITION_TOLERANCE_S = 0.3
ALL_AUDIBLE = [False, False, False, False]
DRUMS_MUTED = [False, True, False, False]
# 意料之中的“错误”：还没保存过场景的歌 GET /scene 是 404（前端据此改用自动编排）；应用没有 favicon
EXPECTED = (("404", "/scene"), ("404", "/favicon.ico"))
OFFLINE = ("ERR_CONNECTION_REFUSED",)


class Failure(AssertionError):
    pass


def console_problems(events: list[dict]) -> list[str]:
    """CDP 事件里算“出错”的：未捕获的异常与 Promise 拒绝、console.error、error 级别的日志（含失败的请求）。"""
    out = []
    for event in events:
        params = event.get("params", {})
        if event.get("method") == "Runtime.exceptionThrown":
            details = params["exceptionDetails"]
            text = details.get("exception", {}).get("description") or details.get("text", "")
            out.append(f"uncaught: {text}")
        elif event.get("method") == "Runtime.consoleAPICalled" and params.get("type") in ("error", "assert"):
            parts = [str(arg.get("value", arg.get("description", ""))) for arg in params.get("args", [])]
            out.append(f"console.{params['type']}: {' '.join(parts)}")
        elif event.get("method") == "Log.entryAdded" and params["entry"].get("level") == "error":
            entry = params["entry"]
            out.append(f"{entry.get('source')}: {entry.get('text')} [{entry.get('url', '')}]")
    return out


@dataclass
class Session:
    page: Page
    url: str
    songs: tuple[Path, Path]
    downloads: Path
    stop_backend: Callable[[], None]
    start_backend: Callable[[], None]
    steps: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @contextmanager
    def step(self, name: str, tolerate: tuple[str, ...] = ()) -> Iterator[None]:
        """一步：做完后等页面安静下来，收下这一步的音频记录与界面状态，检查有没有意料之外的错误。"""
        yield
        self.page.run("__probe.settle()", timeout=SETTLE_TIMEOUT_S)
        audio = self.page.run("__probe.take()")
        self.steps.append(
            {
                "step": name,
                "dom": self.state(),
                "params": dict(sorted(audio["params"].items())),
                "messages": audio["messages"],
                "graph": sorted(audio["graph"]),
            }
        )
        for problem in console_problems(self.page.cdp.take_events()):
            expected = any(all(part in problem for part in parts) for parts in EXPECTED)
            if not expected and not any(part in problem for part in tolerate):
                self.problems.append(f"{name}: {problem}")
        print(f"  ok  {name}", flush=True)

    def state(self) -> dict:
        return self.page.run("__ui.state()")

    def expect(self, what: str, **fields) -> None:
        state = self.state()
        wrong = {key: (state.get(key), value) for key, value in fields.items() if state.get(key) != value}
        if wrong:
            raise Failure(
                f"{what}: "
                + ", ".join(f"{k} is {got!r}, expected {want!r}" for k, (got, want) in wrong.items())
            )

    def wait_state(self, what: str, timeout: float = 30, **fields) -> None:
        compact = {
            k: json.dumps(v, separators=(",", ":"), ensure_ascii=False) for k, v in fields.items()
        }  # 与 JSON.stringify 同样的写法
        condition = " && ".join(
            f"JSON.stringify(s.{k}) === {json.dumps(text)}" for k, text in compact.items()
        )
        self.page.wait(f"(() => {{ const s = __ui.state(); return {condition}; }})()", what, timeout)

    def wait_position(self, seconds: float, what: str) -> None:
        self.page.wait(f"Math.abs(__ui.seconds() - {seconds}) < {POSITION_TOLERANCE_S}", what)

    def edit(self, selector: str) -> None:
        """点一个会改场景的控件；先隔开一段时间，保证它在撤销记录里单独成一步。"""
        self.page.run(f"__probe.sleep({EDIT_GAP_MS})")
        self.page.click(selector)

    def press(self, name: str) -> None:
        self.page.run(f"__ui.button({json.dumps(name)}).click()")

    def click_play(self, playing: bool) -> None:
        self.page.click(PLAY)
        self.wait_state("the play button to change", playing=playing)

    def space(self) -> None:
        self.page.key(" ", "Space", 32)

    def drag_playhead(self, start: float, end: float, during: Callable[[], None]) -> None:
        """在时间轴色块上从 start 拖到 end（占全长的比例）；during 在松手之前执行。"""
        lane = self.page.run("__ui.lane()")
        self.page.mouse("mousePressed", lane["left"] + lane["width"] * start, lane["y"])
        for fraction in ((start + end) / 2, end):
            self.page.mouse("mouseMoved", lane["left"] + lane["width"] * fraction, lane["y"])
        during()
        self.page.mouse("mouseReleased", lane["left"] + lane["width"] * end, lane["y"])


def walk(s: Session) -> None:
    """方案里“人工过一遍”的清单。"""
    first, second = s.songs
    open_and_import(s, first)
    transport(s)
    scrub(s)
    edit_undo_redo(s)
    change_room(s)
    lose_and_regain_the_backend(s)
    second_song_and_back(s, first, second)
    export(s, first)
    delete_songs(s)


def open_and_import(s: Session, song: Path) -> None:
    with s.step("open the app"):
        s.page.navigate(s.url)
        s.page.wait("window.__ui && document.body.dataset.phase === 'empty'", "the import page", 90)
        s.expect("the drop zone is shown", overlay="drop", title="")
    with s.step("import a song"):
        s.page.set_files(FILE_INPUT, [song])
        s.wait_state("the song to be ready", READY_TIMEOUT_S, phase="ready", title=song.name)
        s.expect("a freshly opened song", save="saved", listen="8d", playing=False, duration="0:20")
        s.expect("a freshly opened song", overlay="hidden", shape="circle", muted=ALL_AUDIBLE, tab="orbit")


def transport(s: Session) -> None:
    page = s.page
    with s.step("play"):
        s.click_play(True)
        page.wait("__ui.seconds() >= 1", "the playhead to move")
        page.wait("Math.max(...__ui.meters()) > -60", "signal on the level meters")
    with s.step("pause"):
        s.click_play(False)
        held = page.run("__ui.seconds()")
        page.run("__probe.sleep(500)")
        if page.run("__ui.seconds()") != held:
            raise Failure("the playhead keeps moving after pause")
    with s.step("space plays"):
        s.space()
        s.wait_state("playback to start", playing=True)
        page.wait(f"__ui.seconds() > {held + 0.3}", "the playhead to move on")
    with s.step("right arrow jumps ahead while playing"):  # 趁离结尾还远先跳：整首歌只有 20 秒，不能播到头
        before = page.run("__ui.seconds()")
        page.key("ArrowRight", "ArrowRight", 39)
        page.wait(f"__ui.seconds() > {before + 4}", "the playhead to jump 5 s ahead")
        s.expect("still playing after the jump", playing=True)
    with s.step("B switches to the original"):
        page.key("b", "KeyB", 66)
        s.wait_state("the original to be selected", listen="original", playing=True)
    with s.step("B switches back to 8D"):
        page.key("b", "KeyB", 66)
        s.wait_state("8D to be selected", listen="8d", playing=True)
    with s.step("space pauses"):
        s.space()
        s.wait_state("playback to stop", playing=False)
    with s.step("left arrow jumps back while paused"):
        before = page.run("__ui.seconds()")
        page.key("ArrowLeft", "ArrowLeft", 37)
        s.wait_position(before - 5, "the playhead to jump 5 s back")
        s.expect("still paused after the jump", playing=False)


def scrub(s: Session) -> None:
    middle, quarter = SONG_SECONDS * 0.5, SONG_SECONDS * 0.25
    with s.step("drag the playhead while paused"):
        s.drag_playhead(0.2, 0.5, lambda: s.wait_position(middle, "the playhead to follow the pointer"))
        s.wait_position(middle, "the playhead to stay where it was dropped")
        s.expect("dragging while paused does not start playback", playing=False)
    with s.step("play"):
        s.click_play(True)
    with s.step("drag the playhead while playing"):

        def during() -> None:
            s.wait_state("playback to pause during the drag", playing=False)
            s.wait_position(quarter, "the playhead to follow the pointer")

        s.drag_playhead(0.7, 0.25, during)
        s.wait_state("playback to resume after the drag", playing=True)
        s.page.wait(f"__ui.seconds() > {quarter + 0.3}", "the playhead to move on from the drop point")
    with s.step("pause"):
        s.click_play(False)


def edit_undo_redo(s: Session) -> None:
    with s.step("mute the drums"):
        s.edit(DRUMS_MUTE)
        s.wait_state("the mute to be saved", muted=DRUMS_MUTED, save="saved")
        s.expect("one step to undo", undo=True, redo=False)
    with s.step("apply a preset"):
        s.edit(SINGER_PRESET)
        s.wait_state("the preset to replace the mix", muted=ALL_AUDIBLE, save="saved")
    with s.step("auto choreography"):
        s.edit(AUTO_CHOREOGRAPHY)
        s.page.wait("document.querySelector('.toast.info')", "the notice that it was re-choreographed")
        s.wait_state("the new scene to be saved", save="saved")
    with s.step("change the vocals orbit to an ellipse"):
        s.edit(SHAPE.format("ellipse"))
        s.wait_state("the shape to be saved", shape="ellipse", save="saved")
    with s.step("undo with the keyboard"):
        s.page.run(f"__probe.sleep({EDIT_GAP_MS})")
        s.page.key("z", "KeyZ", 90, KEY_META)
        s.wait_state("the shape to go back", shape="circle", save="saved", redo=True)
    with s.step("redo with the button"):
        s.page.run(f"__probe.sleep({EDIT_GAP_MS})")
        s.press("redo")
        s.wait_state("the shape to come back", shape="ellipse", save="saved", redo=False)


def change_room(s: Session) -> None:
    """换房间再换回来：第二次用的是缓存的混响数据（解码会拿走缓冲区，所以每次都要传副本）。"""
    with s.step("open the space tab"):
        s.page.click(TAB.format("space"))
        s.wait_state("the space tab", tab="space", room="hall")
    with s.step("change the room"):
        s.edit(ROOM.format("room"))
        s.wait_state("the small room to be saved", room="room", save="saved")
    with s.step("change the room back"):
        s.edit(ROOM.format("hall"))
        s.wait_state("the hall to be saved", room="hall", save="saved")
    with s.step("back to the orbit tab"):
        s.page.click(TAB.format("orbit"))
        s.wait_state("the orbit tab", tab="orbit", shape="ellipse")


def lose_and_regain_the_backend(s: Session) -> None:
    with s.step("the backend stops: saving fails", OFFLINE):
        s.stop_backend()
        s.edit(DRUMS_MUTE)
        s.wait_state("the save-failed notice", muted=DRUMS_MUTED, save="failed")
    with s.step("the backend is back: the scene is saved", OFFLINE):
        s.start_backend()
        s.wait_state("the retry to succeed", 60, save="saved")


def second_song_and_back(s: Session, first: Path, second: Path) -> None:
    page = s.page
    with s.step("import a second song"):
        page.set_files(FILE_INPUT, [second])
        s.wait_state("the second song to be ready", READY_TIMEOUT_S, phase="ready", title=second.name)
        s.expect("a freshly opened song", save="saved", undo=False, redo=False, shape="circle")
        s.expect("a freshly opened song", muted=ALL_AUDIBLE, playing=False, listen="8d")
    with s.step("play the second song"):
        s.click_play(True)
        page.wait("__ui.seconds() >= 0.5", "the playhead to move")
        page.wait("Math.max(...__ui.meters()) > -60", "signal on the level meters")
    with s.step("pause"):
        s.click_play(False)
    with s.step("open the song menu"):
        s.press("menu")
        s.wait_state("both songs in the menu", menu=True, songs=2)
    with s.step("switch to the first song from the menu"):
        page.click(f"{OTHER_SONG} .song-open")
        s.wait_state("the first song to be ready", READY_TIMEOUT_S, phase="ready", title=first.name)
        s.expect("the saved scene is restored", shape="ellipse", muted=DRUMS_MUTED, save="saved")
        s.expect("a freshly opened song", menu=False, playing=False, undo=False)


def export(s: Session, song: Path) -> None:
    page = s.page
    with s.step("export and download"):
        s.press("export")
        s.wait_state("the export dialog", dialog=True)
        page.click(".dialog .actions button.primary")
        page.wait("document.querySelector('.dialog a[download]')", "the download link", 120)
        name = page.run("document.querySelector('.dialog a[download]').getAttribute('download')")
        if name != f"{song.stem} (8D).m4a":
            raise Failure(f"unexpected download name: {name!r}")
        wait_for_download(s.downloads)
        page.click(".dialog .actions button.ghost")
        s.wait_state("the dialog to close", dialog=False)


def wait_for_download(folder: Path, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        files = [f for f in folder.iterdir() if f.suffix != ".crdownload"]
        if any(f.stat().st_size >= MIN_DOWNLOAD_BYTES for f in files):
            return
        time.sleep(0.2)
    raise PageError(f"no finished download of at least {MIN_DOWNLOAD_BYTES} bytes appeared")


def delete_songs(s: Session) -> None:
    page = s.page
    with s.step("delete the song that is not open"):
        s.press("menu")
        s.wait_state("both songs in the menu", menu=True, songs=2)
        page.click(f"{OTHER_SONG} .song-delete")
        page.wait(f"document.querySelector('{OTHER_SONG} .song-delete.armed')", "the button to ask again")
        page.click(f"{OTHER_SONG} .song-delete")
        s.wait_state("one song left in the menu", songs=1)
        s.expect("the open song is untouched", phase="ready", menu=True)
    with s.step("delete the open song"):
        page.click(f"{OPEN_SONG} .song-delete")
        page.wait(f"document.querySelector('{OPEN_SONG} .song-delete.armed')", "the button to ask again")
        page.click(f"{OPEN_SONG} .song-delete")
        s.wait_state("the import page", phase="empty", overlay="drop", title="", menu=False)
        left = page.run("fetch('/api/projects').then((r) => r.json()).then((songs) => songs.length)")
        if left != 0:
            raise Failure(f"{left} songs are still listed after deleting both")
