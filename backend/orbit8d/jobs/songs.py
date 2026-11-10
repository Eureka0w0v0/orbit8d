"""收歌、排导出、删歌（SPEC §6、§6.1、§7）。这三件事都是“查状态 + 改记录 + 排任务”，必须在存储锁里
一起做，互相之间才不会踩到：删到一半又排进新任务、同一个文件建出两条记录。入口层只调这里，不碰锁。"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from orbit8d.engine.pipeline import RENDER_VERSION, Analysis
from orbit8d.engine.scene import Scene, canonical_json
from orbit8d.errors import CodedError
from orbit8d.jobs.states import ExportState, ProjectState, is_terminal
from orbit8d.jobs.store import ExportRecord, ProjectRecord, SourceInfo, Store
from orbit8d.jobs.worker import JobKind, JobRunner
from orbit8d.media.ffmpeg import available_formats, probe

ID_LEN = 16  # 项目与导出的 ID：sha256 的前 16 位十六进制


class Refused(CodedError):
    """这个请求现在不能照办：歌还在处理、太长、本机编不了这种格式……入口层按错误码选 HTTP 状态码。"""


@dataclass(frozen=True)
class Upload:
    """一个已经收完的上传：落在临时文件里，收下之后挪进项目目录。"""

    path: Path
    filename: str  # 用户给的文件名：已清理，只用于显示
    size: int
    sha256: str


class Songs:
    def __init__(self, store: Store, runner: JobRunner, max_duration_s: float):
        self._store, self._runner, self._max_duration_s = store, runner, max_duration_s

    def add(self, upload: Upload) -> tuple[ProjectRecord, bool]:
        """收下一首歌，返回 (记录, 是不是新建的)。项目 ID 取内容哈希：同一个文件再传一次还是同一首歌，
        已有的直接返回；失败过的当场转回“已上传”（旧错误清掉）再排队分析，所以返回的记录就是排队中的样子。
        探测格式比较慢，放在锁外面做。"""
        pid = upload.sha256[:ID_LEN]
        info = None if self._store.find_project(pid) else probe(upload.path)
        if info and info.duration_s > self._max_duration_s:
            raise Refused("TOO_LONG", f"The song is longer than {self._max_duration_s / 60:.0f} minutes")
        with self._store.lock:  # 与删歌互斥：查记录、建记录、排任务一起做
            existing = self._store.find_project(pid)
            if existing:
                if existing.state is ProjectState.FAILED and not self._runner.busy(pid):
                    existing = self._store.transition_project(pid, ProjectState.UPLOADED)
                    self._runner.submit(JobKind.ANALYSIS, pid, pid)
                return existing, False
            if info is None:  # 查的时候还在、加锁前被删了：按新歌再来一次
                raise Refused("PROJECT_BUSY", "The song was just deleted; import it again")
            files = self._store.project_files(pid)
            files.root.mkdir(parents=True, exist_ok=True)
            upload.path.replace(files.source)
            source = SourceInfo(
                filename=upload.filename,
                size=upload.size,
                sha256=upload.sha256,
                codec=info.codec,
                duration_s=info.duration_s,
                title=info.title,
                artist=info.artist,
                album=info.album,
                has_cover=info.has_cover,
            )
            rec, _ = self._store.create_project_if_absent(ProjectRecord(pid, ProjectState.UPLOADED, source))
            self._runner.submit(JobKind.ANALYSIS, pid, pid)
            return rec, True

    def ready(self, pid: str) -> ProjectRecord:
        """已就绪的歌的记录；还在处理（或失败了）就拒绝。"""
        rec = self._store.get_project(pid)
        if rec.state is not ProjectState.READY:
            raise Refused("PROJECT_NOT_READY", "Still processing")
        return rec

    def analysis(self, pid: str) -> Analysis:
        return Analysis(**self.ready(pid).analysis)

    def queue_export(self, pid: str, scene: Scene, fmt: str, loudness: str) -> tuple[ExportRecord, bool]:
        """排一次导出，返回 (记录, 是不是排进了队列)。导出 ID 取渲染版本 + 歌 + 场景 + 格式 + 响度的哈希：
        同样的参数再提交得到同一个导出（幂等），失败过的重新排队；渲染算法升级后不复用旧导出。"""
        with self._store.lock:  # 与删歌互斥：确认歌还在、建导出记录、排任务一起做
            self.ready(pid)
            if fmt not in available_formats():
                raise Refused("UNSUPPORTED_FORMAT", f"The local ffmpeg cannot encode {fmt}")
            key = f"v{RENDER_VERSION}|{pid}|{canonical_json(scene)}|{fmt}|{loudness}"
            eid = hashlib.sha256(key.encode()).hexdigest()[:ID_LEN]
            rec, queued = self._store.create_export_if_absent(
                ExportRecord(
                    id=eid,
                    project_id=pid,
                    state=ExportState.QUEUED,
                    format=fmt,
                    scene=scene.model_dump(mode="json"),
                    loudness=loudness,
                )
            )
            if not queued and rec.state is ExportState.FAILED:
                rec = self._store.transition_export(eid, ExportState.QUEUED)
                queued = True
            if queued:
                self._runner.submit(JobKind.EXPORT, eid, pid)
            return rec, queued

    def delete(self, pid: str) -> None:
        """连同它的导出一起删。还有排队或正在跑的任务、或者还没处理完时拒绝，不删半截。"""
        with self._store.lock:
            rec = self._store.get_project(pid)
            if self._runner.busy(pid) or not is_terminal(rec.state):
                raise Refused("PROJECT_BUSY", "The song is still being processed")
            moved = self._store.trash_project(pid)
        self._store.empty_trash(moved)
