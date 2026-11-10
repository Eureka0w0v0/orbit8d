"""单工位任务队列：分析、导出、存储迁移按提交顺序串行执行（分轨独占 GPU，也避免多份整曲数据同时占内存）。
音频文件怎么存、怎么读见 jobs/stems.py；解码出的 orig.wav 只在分析期间存在，用完即删，
要重新分析时从上传的原文件再解码。"""

import json
import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from orbit8d.config import SAMPLE_RATE
from orbit8d.engine.pipeline import ANALYSIS_VERSION, SEPARATED_TRACKS, Analysis, Renderer, analyze
from orbit8d.engine.scene import Scene
from orbit8d.errors import CodedError
from orbit8d.jobs.states import ExportState, IllegalTransition, ProjectState
from orbit8d.jobs.stems import compress_stems, needs_migration, preview_chunks, stem_file, write_previews
from orbit8d.jobs.store import NotFound, Store
from orbit8d.logging_setup import trace
from orbit8d.media.audiofile import AudioFile, write_audio
from orbit8d.media.ffmpeg import OUTPUT_FORMATS, decode_to_wav, encode

log = logging.getLogger(__name__)

POLL_S = 0.2
ANALYZED_PROGRESS = 0.7
STEP_MODEL_DOWNLOAD = "model_download"  # 分轨阶段里第一次下载模型（界面单独显示）
PROGRESS_STEP = 0.005  # 进度至少涨这么多才写一次记录


class Separator(Protocol):
    def ensure_model(self, on_progress: Callable[[float], None]) -> None:
        """模型不在本机就下载（只有第一次；on_progress 收到下载进度），然后加载。"""

    def separate(self, wav: Path, out_dir: Path, on_progress: Callable[[float], None]) -> None: ...


class JobKind(StrEnum):
    ANALYSIS = "analysis"  # 新导入的歌：解码 → 分轨 → 分析
    REFRESH = "refresh"  # 分析算法升级后重新分析，沿用已有分轨
    MIGRATE = "migrate"  # 旧版存储迁移
    EXPORT = "export"


@dataclass(frozen=True)
class Job:
    kind: JobKind
    id: str  # 项目或导出的 ID
    project_id: str  # 这个任务属于哪首歌（删歌前据此判断是否还有任务）


class JobRunner:
    def __init__(self, store: Store, renderer: Renderer, separator: Separator):
        self._store = store
        self._renderer = renderer
        self._separator = separator
        self._queue: queue.Queue[Job] = queue.Queue()
        self._stop = threading.Event()
        self._pending = 0
        self._by_project: dict[str, int] = {}
        self._cond = threading.Condition()
        self._thread = threading.Thread(target=self._loop, name="orbit8d-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout)

    def submit(self, kind: JobKind, job_id: str, project_id: str) -> None:
        job = Job(JobKind(kind), job_id, project_id)  # 不认识的任务种类在这里就报错，不留到工作线程里
        with self._cond:
            self._pending += 1
            self._by_project[project_id] = self._by_project.get(project_id, 0) + 1
        self._queue.put(job)

    def busy(self, project_id: str) -> bool:
        """这首歌还有排队或正在跑的任务。"""
        with self._cond:
            return self._by_project.get(project_id, 0) > 0

    def refresh_stale(self) -> int:
        """分析算法升级后：已就绪的旧项目转回“分析中”，排队重新分析（沿用已有分轨）。返回排队个数。"""
        stale = self._store.stale_projects(ANALYSIS_VERSION)
        for rec in stale:
            self._store.transition_project(rec.id, ProjectState.ANALYZING)
            self.submit(JobKind.REFRESH, rec.id, rec.id)
            log.info(
                "project queued for re-analysis", extra={"event": "project.refresh", "project_id": rec.id}
            )
        return len(stale)

    def migrate_storage(self) -> int:
        """旧版存储（分轨 WAV、常驻的 orig.wav）的已就绪项目排队迁移。返回排队个数。"""
        old = [
            rec
            for rec in self._store.list_projects()
            if rec.state is ProjectState.READY and needs_migration(self._store.project_files(rec.id))
        ]
        for rec in old:
            self.submit(JobKind.MIGRATE, rec.id, rec.id)
            log.info(
                "project queued for storage migration",
                extra={"event": "project.migrate", "project_id": rec.id},
            )
        return len(old)

    def wait_idle(self, timeout: float) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: self._pending == 0, timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=POLL_S)
            except queue.Empty:
                continue
            try:
                self._run(job)
            finally:
                with self._cond:
                    self._pending -= 1
                    left = self._by_project.pop(job.project_id, 1) - 1
                    if left:
                        self._by_project[job.project_id] = left
                    self._cond.notify_all()

    def _run(self, job: Job) -> None:
        handler, fail = {
            JobKind.ANALYSIS: (self._run_analysis, self._store.fail_project),
            JobKind.REFRESH: (self._run_refresh, self._store.fail_project),
            JobKind.MIGRATE: (self._run_migrate, self._store.fail_project),
            JobKind.EXPORT: (self._run_export, self._store.fail_export),
        }[job.kind]
        with trace(job.id):
            log.info("job started", extra={"event": "job.start", "kind": job.kind})
            try:
                handler(job.id)
                log.info("job finished", extra={"event": "job.done", "kind": job.kind})
            except NotFound:  # 不该发生（有任务的歌不能删）：记下来，不往已删的目录里写失败记录
                log.error("job target is gone", exc_info=True, extra={"event": "job.gone", "kind": job.kind})
            except CodedError as exc:  # 解码 / 编码 / 下载模型 / 分轨等有错误码的失败：界面按错误码提示
                log.warning("job failed", exc_info=True, extra={"event": "job.failed", "code": exc.code})
                fail(job.id, exc.code, str(exc))
            except IllegalTransition as exc:
                log.error("job state conflict", exc_info=True, extra={"event": "job.state_conflict"})
                fail(job.id, "STATE_CONFLICT", str(exc))
            except Exception as exc:  # noqa: BLE001 — 兜底：记录完整堆栈并把任务标为失败，绝不静默
                log.exception("job crashed", extra={"event": "job.crash", "kind": job.kind})
                fail(job.id, "INTERNAL", f"{type(exc).__name__}: {exc}")

    def _compress_stems(self, pid: str) -> None:
        """分轨压成 FLAC；每轨的缩放系数写进项目记录之后，那一轨的 WAV 才会被删。"""
        store = self._store
        compress_stems(store.project_files(pid), lambda name, scale: store.set_stem_scale(pid, name, scale))

    def _run_analysis(self, pid: str) -> None:
        store, files = self._store, self._store.project_files(pid)
        store.transition_project(pid, ProjectState.DECODING)
        try:
            decode_to_wav(files.source, files.decoded, SAMPLE_RATE)
            store.transition_project(pid, ProjectState.SEPARATING)
            self._separator.ensure_model(self._progress(pid, STEP_MODEL_DOWNLOAD))
            store.set_project_progress(pid, 0.0, step="")
            self._separator.separate(files.decoded, files.stems, self._progress(pid))
            store.transition_project(pid, ProjectState.ANALYZING)
            self._compress_stems(pid)
            self._finish_analysis(pid)
        finally:
            files.decoded.unlink(missing_ok=True)

    def _progress(self, pid: str, step: str | None = None) -> Callable[[float], None]:
        """进度回调：每涨 PROGRESS_STEP 才写一次记录（下载、分轨都是每 MB / 每段回调一次）。"""
        last = [-1.0]

        def report(fraction: float) -> None:
            if fraction >= 1.0 or fraction - last[0] >= PROGRESS_STEP:
                last[0] = fraction
                self._store.set_project_progress(pid, fraction, step=step)

        return report

    def _run_refresh(self, pid: str) -> None:
        """项目已处于“分析中”（refresh_stale 转过去的）：从原文件重新解码再分析，不重新分轨。"""
        files = self._store.project_files(pid)
        try:
            decode_to_wav(files.source, files.decoded, SAMPLE_RATE)
            self._finish_analysis(pid)
        finally:
            files.decoded.unlink(missing_ok=True)

    def _run_migrate(self, pid: str) -> None:
        """旧版存储迁移：分轨压成 FLAC，删掉常驻的 orig.wav（重新分析时会从原文件再解码）。"""
        self._compress_stems(pid)
        self._store.project_files(pid).decoded.unlink(missing_ok=True)

    def _finish_analysis(self, pid: str) -> None:
        store, files = self._store, self._store.project_files(pid)
        rec = store.get_project(pid)
        orig = AudioFile(files.decoded)
        stems = {name: stem_file(files, name, rec.stem_scales) for name in SEPARATED_TRACKS}
        result = analyze(orig, stems, SAMPLE_RATE, self._renderer)
        store.set_project_progress(pid, ANALYZED_PROGRESS)
        scale = max(1.0, result.peak)  # 24 位 FLAC 不能超过 ±1：所有试听文件统一缩放
        write_previews(files, result.sources, orig, scale)
        files.analysis.write_text(result.analysis.to_json(), encoding="utf-8")
        store.transition_project(
            pid, ProjectState.READY, analysis=json.loads(result.analysis.to_json()), preview_scale=scale
        )

    def _run_export(self, eid: str) -> None:
        store = self._store
        rec = store.transition_export(eid, ExportState.RENDERING)
        project = store.get_project(rec.project_id)
        files, out = store.project_files(project.id), store.export_files(eid)
        n, chunks = preview_chunks(files, project.preview_scale)
        mastered = self._renderer.export_chunks(
            chunks, n, Scene.model_validate(rec.scene), Analysis(**project.analysis), rec.loudness
        )
        write_audio(out.render, mastered, 2, SAMPLE_RATE, "FLOAT")
        store.transition_export(eid, ExportState.ENCODING)
        fmt = OUTPUT_FORMATS[rec.format]
        cover = files.source if project.source.has_cover else None
        try:
            encode(out.render, out.output(fmt.ext), rec.format, project.source.export_metadata(), cover)
        finally:
            out.render.unlink(missing_ok=True)
        store.transition_export(eid, ExportState.DONE, file_name=project.source.export_file_name(fmt.ext))
