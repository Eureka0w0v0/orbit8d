"""单工位任务队列：分析、导出、存储迁移按提交顺序串行执行（分轨独占 GPU，也避免多份整曲数据同时占内存）。

存储（SPEC §5.7、§6）：分轨存 24 位 FLAC（每轨各自的缩放系数记在项目记录里，读回校验通过才删 WAV）；
解码出的 orig.wav 只在分析期间存在，用完即删，要重新分析时从上传的原文件再解码。
同一首歌有 WAV 就用 WAV：迁移中途崩溃时，WAV 只会在它的 FLAC 和缩放系数都写好之后才删。
"""

import json
import logging
import queue
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from orbit8d.config import SAMPLE_RATE
from orbit8d.engine.chunks import aligned_windows, chunk_ranges
from orbit8d.engine.pipeline import (
    ANALYSIS_VERSION,
    PREVIEW_FILES,
    SEPARATED_TRACKS,
    SOURCE_CHANNELS,
    Analysis,
    Renderer,
    Sources,
    analyze,
)
from orbit8d.engine.scene import Scene
from orbit8d.errors import CodedError
from orbit8d.jobs.states import ExportState, IllegalTransition, ProjectState
from orbit8d.jobs.store import NotFound, ProjectRecord, Store
from orbit8d.logging_setup import trace
from orbit8d.media.audiofile import AudioFile, write_audio
from orbit8d.media.ffmpeg import OUTPUT_FORMATS, decode_to_wav, encode

log = logging.getLogger(__name__)

SOURCE_FILE = "source"
ORIG_FILE = "orig.wav"
STEMS_DIR = "stems"
PREVIEW_DIR = "preview"
ORIGINAL_PREVIEW = "original"
ANALYSIS_FILE = "analysis.json"
RENDER_FILE = "render.wav"
OUTPUT_STEM = "output"
STEM_NAMES = ("vocals", "drums", "bass", "other")
PREVIEW_SUBTYPE = STEM_SUBTYPE = "PCM_24"
FLAC_TOLERANCE_LSB = 4  # 24 位 FLAC 读回与原值的允许误差（量化 + 读写归一化差，实际约 1.5 个最低位）
POLL_S = 0.2
ANALYZED_PROGRESS = 0.7
STEP_MODEL_DOWNLOAD = "model_download"  # 分轨阶段里第一次下载模型（界面单独显示）
PROGRESS_STEP = 0.005  # 进度至少涨这么多才写一次记录
MAX_NAME = 100
UNSAFE_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')


class Separator(Protocol):
    def ensure_model(self, on_progress: Callable[[float], None]) -> None:
        """模型不在本机就下载（只有第一次；on_progress 收到下载进度），然后加载。"""

    def separate(self, wav: Path, out_dir: Path, on_progress: Callable[[float], None]) -> None: ...


@dataclass(frozen=True)
class Job:
    kind: str  # "analysis" | "refresh" | "migrate" | "export"
    id: str  # 项目或导出的 ID
    project_id: str  # 这个任务属于哪首歌（删歌前据此判断是否还有任务）


def safe_name(text: str, fallback: str = "untitled") -> str:
    """去掉路径分隔符和控制字符，只用于显示与下载文件名，从不参与拼接存储路径。"""
    cleaned = UNSAFE_CHARS.sub(" ", text).strip(" .")
    return cleaned[:MAX_NAME].strip() or fallback


def song_title(source: dict) -> str:
    return source.get("title") or Path(source.get("filename", "")).stem or "untitled"


def export_file_name(source: dict, ext: str) -> str:
    artist = source.get("artist", "")
    base = f"{artist} - {song_title(source)}" if artist else song_title(source)
    return f"{safe_name(base)} (8D).{ext}"


def export_metadata(source: dict) -> dict[str, str]:
    meta = {
        "title": f"{song_title(source)} (8D)",
        "artist": source.get("artist", ""),
        "album": source.get("album", ""),
    }
    return {k: v for k, v in meta.items() if v}


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

    def submit(self, kind: str, job_id: str, project_id: str) -> None:
        with self._cond:
            self._pending += 1
            self._by_project[project_id] = self._by_project.get(project_id, 0) + 1
        self._queue.put(Job(kind, job_id, project_id))

    def busy(self, project_id: str) -> bool:
        """这首歌还有排队或正在跑的任务。"""
        with self._cond:
            return self._by_project.get(project_id, 0) > 0

    def refresh_stale(self) -> int:
        """分析算法升级后：已就绪的旧项目转回“分析中”，排队重新分析（沿用已有分轨）。返回排队个数。"""
        stale = self._store.stale_projects(ANALYSIS_VERSION)
        for rec in stale:
            self._store.transition_project(rec.id, ProjectState.ANALYZING)
            self.submit("refresh", rec.id, rec.id)
            log.info(
                "project queued for re-analysis", extra={"event": "project.refresh", "project_id": rec.id}
            )
        return len(stale)

    def migrate_storage(self) -> int:
        """旧版存储（分轨 WAV、常驻的 orig.wav）的已就绪项目排队迁移。返回排队个数。"""
        old = [
            rec
            for rec in self._store.list_projects()
            if rec.state is ProjectState.READY and self._unmigrated(rec.id)
        ]
        for rec in old:
            self.submit("migrate", rec.id, rec.id)
            log.info(
                "project queued for storage migration",
                extra={"event": "project.migrate", "project_id": rec.id},
            )
        return len(old)

    def _unmigrated(self, pid: str) -> bool:
        pdir = self._store.project_dir(pid)
        return (pdir / ORIG_FILE).exists() or any((pdir / STEMS_DIR).glob("*.wav"))

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
            "analysis": (self._run_analysis, self._store.fail_project),
            "refresh": (self._run_refresh, self._store.fail_project),
            "migrate": (self._run_migrate, self._store.fail_project),
            "export": (self._run_export, self._store.fail_export),
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

    @staticmethod
    def _stem_file(pdir: Path, name: str, rec: ProjectRecord) -> AudioFile:
        """有 WAV 用 WAV（还没迁移或迁移到一半），否则用 FLAC × 记录里的缩放系数。"""
        wav = pdir / STEMS_DIR / f"{name}.wav"
        if wav.exists():
            return AudioFile(wav)
        if name not in rec.stem_scales:
            raise FileNotFoundError(f"{name} 分轨缺少缩放系数，无法读取 FLAC")
        return AudioFile(pdir / STEMS_DIR / f"{name}.flac", rec.stem_scales[name])

    def _compress_stems(self, pid: str) -> None:
        """分轨 32 位浮点 WAV → 24 位 FLAC（约省一半空间）：每轨缩放到 ±1 以内，读回校验通过、
        缩放系数写进记录之后才删 WAV。可以重复执行（迁移到一半崩溃后再跑一遍即可）。"""
        stems_dir = self._store.project_dir(pid) / STEMS_DIR
        for name in STEM_NAMES:
            wav = stems_dir / f"{name}.wav"
            if not wav.exists():
                continue
            source = AudioFile(wav)
            scale = max(1.0, max(float(np.abs(w.body).max(initial=0.0)) for w in source.windows()))
            flac = stems_dir / f"{name}.flac"
            write_audio(
                flac, (w.body / scale for w in source.windows()), source.channels, SAMPLE_RATE, STEM_SUBTYPE
            )
            _verify_copy(source, AudioFile(flac, scale), FLAC_TOLERANCE_LSB * scale / 2**23)
            self._store.set_stem_scale(pid, name, scale)
            wav.unlink()

    @staticmethod
    def _preview_chunks(pdir: Path, scale: float) -> tuple[int, Iterator[np.ndarray]]:
        """试听文件 × scale → 按段的 9 路声源 (9, L)，与浏览器试听同一份数据。"""
        files = [AudioFile(pdir / PREVIEW_DIR / f"{name}.flac", scale) for name in PREVIEW_FILES]

        def chunks() -> Iterator[np.ndarray]:
            for windows in aligned_windows(files):
                chunk = np.empty((len(SOURCE_CHANNELS), windows[0].end - windows[0].start))
                for channels, w in zip(PREVIEW_FILES.values(), windows, strict=True):
                    chunk[list(channels)] = w.body.T
                yield chunk

        return files[0].frames, chunks()

    @staticmethod
    def _write_previews(pdir: Path, src: Sources, orig: AudioFile, scale: float) -> None:
        """9 路声源 + 原曲（A/B 对比用）写成 24 位 FLAC，统一除以 scale 保证不超过 ±1。"""
        out = pdir / PREVIEW_DIR
        out.mkdir(exist_ok=True)
        for name, channels in PREVIEW_FILES.items():
            parts = (src.data[list(channels), s:e].T / scale for s, e in chunk_ranges(src.n))
            write_audio(out / f"{name}.flac", parts, len(channels), SAMPLE_RATE, PREVIEW_SUBTYPE)
        parts = (w.body / scale for w in orig.windows())
        write_audio(out / f"{ORIGINAL_PREVIEW}.flac", parts, orig.channels, SAMPLE_RATE, PREVIEW_SUBTYPE)

    def _run_analysis(self, pid: str) -> None:
        store, pdir = self._store, self._store.project_dir(pid)
        store.transition_project(pid, ProjectState.DECODING)
        try:
            decode_to_wav(pdir / SOURCE_FILE, pdir / ORIG_FILE, SAMPLE_RATE)
            store.transition_project(pid, ProjectState.SEPARATING)
            self._separator.ensure_model(self._progress(pid, STEP_MODEL_DOWNLOAD))
            store.set_project_progress(pid, 0.0, step="")
            self._separator.separate(pdir / ORIG_FILE, pdir / STEMS_DIR, self._progress(pid))
            store.transition_project(pid, ProjectState.ANALYZING)
            self._compress_stems(pid)
            self._finish_analysis(pid)
        finally:
            (pdir / ORIG_FILE).unlink(missing_ok=True)

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
        pdir = self._store.project_dir(pid)
        try:
            decode_to_wav(pdir / SOURCE_FILE, pdir / ORIG_FILE, SAMPLE_RATE)
            self._finish_analysis(pid)
        finally:
            (pdir / ORIG_FILE).unlink(missing_ok=True)

    def _run_migrate(self, pid: str) -> None:
        """旧版存储迁移：分轨压成 FLAC，删掉常驻的 orig.wav（重新分析时会从原文件再解码）。"""
        self._compress_stems(pid)
        (self._store.project_dir(pid) / ORIG_FILE).unlink(missing_ok=True)

    def _finish_analysis(self, pid: str) -> None:
        store, pdir = self._store, self._store.project_dir(pid)
        rec = store.get_project(pid)
        orig = AudioFile(pdir / ORIG_FILE)
        stems = {name: self._stem_file(pdir, name, rec) for name in SEPARATED_TRACKS}
        result = analyze(orig, stems, SAMPLE_RATE, self._renderer)
        store.set_project_progress(pid, ANALYZED_PROGRESS)
        scale = max(1.0, result.peak)  # 24 位 FLAC 不能超过 ±1：所有试听文件统一缩放
        self._write_previews(pdir, result.sources, orig, scale)
        (pdir / ANALYSIS_FILE).write_text(result.analysis.to_json(), encoding="utf-8")
        store.transition_project(
            pid, ProjectState.READY, analysis=json.loads(result.analysis.to_json()), preview_scale=scale
        )

    def _run_export(self, eid: str) -> None:
        store = self._store
        rec = store.transition_export(eid, ExportState.RENDERING)
        project = store.get_project(rec.project_id)
        pdir, edir = store.project_dir(project.id), store.export_dir(eid)
        n, chunks = self._preview_chunks(pdir, project.preview_scale)
        mastered = self._renderer.export_chunks(
            chunks, n, Scene.model_validate(rec.scene), Analysis(**project.analysis), rec.loudness
        )
        render_path = edir / RENDER_FILE
        write_audio(render_path, mastered, 2, SAMPLE_RATE, "FLOAT")
        store.transition_export(eid, ExportState.ENCODING)
        fmt = OUTPUT_FORMATS[rec.format]
        cover = pdir / SOURCE_FILE if project.source.get("has_cover") else None
        try:
            encode(
                render_path,
                edir / f"{OUTPUT_STEM}.{fmt.ext}",
                rec.format,
                export_metadata(project.source),
                cover,
            )
        finally:
            render_path.unlink(missing_ok=True)
        store.transition_export(eid, ExportState.DONE, file_name=export_file_name(project.source, fmt.ext))


def _verify_copy(original: AudioFile, copy: AudioFile, tolerance: float) -> None:
    """逐段比对两份音频，误差超过 tolerance 就报错（不删原文件）。"""
    if (copy.frames, copy.channels) != (original.frames, original.channels):
        raise ValueError(f"{copy.path.name} 的长度或声道数与原文件不一致")
    for a, b in zip(original.windows(), copy.windows(), strict=True):
        err = float(np.abs(a.body - b.body).max(initial=0.0))
        if err > tolerance:
            raise ValueError(f"{copy.path.name} 读回校验失败：误差 {err:.3g} > {tolerance:.3g}")
