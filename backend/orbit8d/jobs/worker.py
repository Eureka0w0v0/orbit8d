"""单工位任务队列：分析与导出按提交顺序串行执行（分轨独占 GPU，也避免多份整曲数据同时占内存）。"""

import json
import logging
import queue
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
import soundfile as sf

from orbit8d.config import SAMPLE_RATE
from orbit8d.engine.pipeline import (
    ANALYSIS_VERSION,
    Analysis,
    Renderer,
    analyze,
    prepare_sources,
    preview_stems,
)
from orbit8d.engine.scene import Scene
from orbit8d.jobs.states import ExportState, IllegalTransition, ProjectState
from orbit8d.jobs.store import Store
from orbit8d.logging_setup import trace
from orbit8d.media.ffmpeg import OUTPUT_FORMATS, MediaError, decode_to_wav, encode

log = logging.getLogger(__name__)

SOURCE_FILE = "source"
ORIG_FILE = "orig.wav"
STEMS_DIR = "stems"
PREVIEW_DIR = "preview"
ANALYSIS_FILE = "analysis.json"
RENDER_FILE = "render.wav"
OUTPUT_STEM = "output"
STEM_NAMES = ("vocals", "drums", "bass", "other")
PREVIEW_SUBTYPE = "PCM_24"
POLL_S = 0.2
ANALYZED_PROGRESS = 0.7
MAX_NAME = 100
UNSAFE_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')


class Separator(Protocol):
    def separate(self, wav: Path, out_dir: Path, on_progress: Callable[[float], None]) -> None: ...


@dataclass(frozen=True)
class Job:
    kind: str  # "analysis" | "refresh" | "export"
    id: str


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
        self._cond = threading.Condition()
        self._thread = threading.Thread(target=self._loop, name="orbit8d-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout)

    def submit(self, kind: str, job_id: str) -> None:
        with self._cond:
            self._pending += 1
        self._queue.put(Job(kind, job_id))

    def refresh_stale(self) -> int:
        """分析算法升级后：已就绪的旧项目转回“分析中”，排队重新分析（沿用已有分轨）。返回排队个数。"""
        stale = self._store.stale_projects(ANALYSIS_VERSION)
        for rec in stale:
            self._store.transition_project(rec.id, ProjectState.ANALYZING)
            self.submit("refresh", rec.id)
            log.info(
                "project queued for re-analysis", extra={"event": "project.refresh", "project_id": rec.id}
            )
        return len(stale)

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
                    self._cond.notify_all()

    def _run(self, job: Job) -> None:
        handler, fail = {
            "analysis": (self._run_analysis, self._store.fail_project),
            "refresh": (self._run_refresh, self._store.fail_project),
            "export": (self._run_export, self._store.fail_export),
        }[job.kind]
        with trace(job.id):
            log.info("job started", extra={"event": "job.start", "kind": job.kind})
            try:
                handler(job.id)
                log.info("job finished", extra={"event": "job.done", "kind": job.kind})
            except MediaError as exc:
                log.warning("job media error", extra={"event": "job.media_error", "code": exc.code})
                fail(job.id, exc.code, str(exc))
            except IllegalTransition as exc:
                log.error("job state conflict", exc_info=True, extra={"event": "job.state_conflict"})
                fail(job.id, "STATE_CONFLICT", str(exc))
            except Exception as exc:  # noqa: BLE001 — 兜底：记录完整堆栈并把任务标为失败，绝不静默
                log.exception("job crashed", extra={"event": "job.crash", "kind": job.kind})
                fail(job.id, "INTERNAL", f"{type(exc).__name__}: {exc}")

    def _load_audio(self, pdir: Path) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        orig, _ = sf.read(pdir / ORIG_FILE, always_2d=True)
        stems = {name: sf.read(pdir / STEMS_DIR / f"{name}.wav", always_2d=True)[0] for name in STEM_NAMES}
        return orig, stems

    def _run_analysis(self, pid: str) -> None:
        store, pdir = self._store, self._store.project_dir(pid)
        store.transition_project(pid, ProjectState.DECODING)
        decode_to_wav(pdir / SOURCE_FILE, pdir / ORIG_FILE, SAMPLE_RATE)
        store.transition_project(pid, ProjectState.SEPARATING)
        self._separator.separate(
            pdir / ORIG_FILE, pdir / STEMS_DIR, lambda p: store.set_project_progress(pid, p)
        )
        store.transition_project(pid, ProjectState.ANALYZING)
        self._finish_analysis(pid)

    def _run_refresh(self, pid: str) -> None:
        """项目已处于“分析中”（refresh_stale 转过去的）：只重做分析，不重新解码、分轨。"""
        self._finish_analysis(pid)

    def _finish_analysis(self, pid: str) -> None:
        store, pdir = self._store, self._store.project_dir(pid)
        orig, stems = self._load_audio(pdir)
        src, analysis = analyze(orig, stems, SAMPLE_RATE, self._renderer)
        store.set_project_progress(pid, ANALYZED_PROGRESS)
        files = preview_stems(src)
        scale = max(1.0, max(float(np.abs(x).max()) for x in files.values()))  # 24-bit 不能超过 ±1，统一缩放
        (pdir / PREVIEW_DIR).mkdir(exist_ok=True)
        for name, data in files.items():
            sf.write(pdir / PREVIEW_DIR / f"{name}.flac", data / scale, SAMPLE_RATE, subtype=PREVIEW_SUBTYPE)
        (pdir / ANALYSIS_FILE).write_text(analysis.to_json(), encoding="utf-8")
        store.transition_project(
            pid, ProjectState.READY, analysis=json.loads(analysis.to_json()), preview_scale=scale
        )

    def _run_export(self, eid: str) -> None:
        store = self._store
        rec = store.transition_export(eid, ExportState.RENDERING)
        project = store.get_project(rec.project_id)
        pdir, edir = store.project_dir(project.id), store.export_dir(eid)
        orig, stems = self._load_audio(pdir)
        out = self._renderer.export(
            prepare_sources(orig, stems, SAMPLE_RATE),
            Scene.model_validate(rec.scene),
            Analysis(**project.analysis),
        )
        render_path = edir / RENDER_FILE
        sf.write(render_path, out, SAMPLE_RATE, subtype="FLOAT")
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
