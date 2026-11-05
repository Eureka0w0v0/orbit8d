"""项目与导出记录的文件存储：每条记录一个 JSON（原子写）；状态变化只能经过状态机。
删除先整个目录改名挪进回收目录、再慢慢删，中途崩溃也不会留下半截项目（启动时清空回收目录）。"""

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from orbit8d.config import Settings
from orbit8d.jobs.states import ExportState, ProjectState, check_transition, is_terminal

log = logging.getLogger(__name__)

ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")
RECORD_FILE = "record.json"
SCENE_FILE = "scene.json"
PROJECT_INTERRUPTED = "INTERRUPTED"
EXPORT_INTERRUPTED = "EXPORT_INTERRUPTED"
UPLOAD_SUFFIX = ".part"


class NotFound(LookupError):
    pass


def valid_id(value: str) -> bool:
    return bool(ID_PATTERN.fullmatch(value))


@dataclass
class ProjectRecord:
    id: str
    state: ProjectState
    source: dict
    progress: float = 0.0
    step: str = ""  # 当前状态里的子步骤（如分轨前第一次下载模型），界面据此换提示
    error: dict | None = None
    analysis: dict | None = None
    preview_scale: float = 1.0
    stem_scales: dict[str, float] = field(default_factory=dict)  # 已存成 24 位 FLAC 的分轨 → 缩放系数
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {**asdict(self), "state": str(self.state)}

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectRecord":
        return cls(**{**data, "state": ProjectState(data["state"])})


@dataclass
class ExportRecord:
    id: str
    project_id: str
    state: ExportState
    format: str
    scene: dict
    progress: float = 0.0
    error: dict | None = None
    file_name: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {**asdict(self), "state": str(self.state)}

    @classmethod
    def from_dict(cls, data: dict) -> "ExportRecord":
        return cls(**{**data, "state": ExportState(data["state"])})


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        for d in (settings.projects_dir, settings.exports_dir, settings.uploads_dir, settings.trash_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def lock(self) -> threading.RLock:
        """“查状态 + 改记录 / 排任务”要一起做时由调用方持有（与删除互斥）。"""
        return self._lock

    # ---- 路径（ID 先校验格式，杜绝路径穿越） ----
    def project_dir(self, pid: str) -> Path:
        if not valid_id(pid):
            raise NotFound(pid)
        return self.settings.projects_dir / pid

    def export_dir(self, eid: str) -> Path:
        if not valid_id(eid):
            raise NotFound(eid)
        return self.settings.exports_dir / eid

    # ---- 底层读写 ----
    @staticmethod
    def _write(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _read(path: Path) -> dict | None:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    # ---- 项目 ----
    def find_project(self, pid: str) -> ProjectRecord | None:
        data = self._read(self.project_dir(pid) / RECORD_FILE)
        return ProjectRecord.from_dict(data) if data else None

    def get_project(self, pid: str) -> ProjectRecord:
        rec = self.find_project(pid)
        if rec is None:
            raise NotFound(pid)
        return rec

    def create_project_if_absent(self, rec: ProjectRecord) -> tuple[ProjectRecord, bool]:
        with self._lock:
            existing = self.find_project(rec.id)
            if existing:
                return existing, False
            self._write(self.project_dir(rec.id) / RECORD_FILE, rec.to_dict())
            return rec, True

    def save_project(self, rec: ProjectRecord) -> None:
        rec.updated_at = time.time()
        self._write(self.project_dir(rec.id) / RECORD_FILE, rec.to_dict())

    def transition_project(self, pid: str, target: ProjectState, **changes) -> ProjectRecord:
        with self._lock:
            rec = self.get_project(pid)
            check_transition(rec.state, target)
            previous = rec.state
            rec.state, rec.progress, rec.step = target, 0.0, ""
            if target is not ProjectState.FAILED:
                rec.error = None
            for key, value in changes.items():
                setattr(rec, key, value)
            self.save_project(rec)
        log.info("project transition", extra={"event": "project.transition", "from": previous, "to": target})
        return rec

    def set_project_progress(self, pid: str, progress: float, step: str | None = None) -> None:
        """step 为 None 时不改子步骤。"""
        with self._lock:
            rec = self.get_project(pid)
            rec.progress = min(max(progress, 0.0), 1.0)
            if step is not None:
                rec.step = step
            self.save_project(rec)

    def set_stem_scale(self, pid: str, name: str, scale: float) -> None:
        with self._lock:
            rec = self.get_project(pid)
            rec.stem_scales = {**rec.stem_scales, name: scale}
            self.save_project(rec)

    def list_projects(self) -> list[ProjectRecord]:
        return [ProjectRecord.from_dict(d) for d in self._records(self.settings.projects_dir)]

    def last_used(self, rec: ProjectRecord) -> float:
        """最近一次用这首歌的时间：记录更新（导入、分析）或保存场景，取较晚者。"""
        scene = self.project_dir(rec.id) / SCENE_FILE
        return max(rec.updated_at, scene.stat().st_mtime if scene.exists() else 0.0)

    def disk_usage(self, pid: str) -> int:
        """这首歌占的磁盘空间（字节）：项目目录 + 它的导出。"""
        dirs = [self.project_dir(pid), *(self.export_dir(e.id) for e in self.exports_of(pid))]
        return sum(f.stat().st_size for d in dirs for f in d.rglob("*") if f.is_file())

    def exports_of(self, pid: str) -> list[ExportRecord]:
        records = (ExportRecord.from_dict(d) for d in self._records(self.settings.exports_dir))
        return [rec for rec in records if rec.project_id == pid]

    def trash_project(self, pid: str) -> list[Path]:
        """把这首歌的导出和项目目录改名挪进回收目录（导出先挪，崩溃时不会剩下没有项目的导出）。
        调用方持有 lock，并已确认没有它的任务。返回挪过去的路径，交给 empty_trash。"""
        with self._lock:
            self.get_project(pid)
            moved = []
            for rec in self.exports_of(pid):
                moved.append(self._to_trash(self.export_dir(rec.id), f"export-{rec.id}"))
            moved.append(self._to_trash(self.project_dir(pid), f"project-{pid}"))
        extra = {"event": "project.delete", "project_id": pid, "exports": len(moved) - 1}
        log.info("project trashed", extra=extra)
        return moved

    def _to_trash(self, path: Path, name: str) -> Path:
        target = self.settings.trash_dir / f"{name}-{uuid.uuid4().hex[:8]}"
        os.replace(path, target)
        return target

    def empty_trash(self, paths: list[Path] | None = None) -> None:
        """删掉回收目录里的东西（默认全部，启动时用）。"""
        for path in paths if paths is not None else list(self.settings.trash_dir.iterdir()):
            shutil.rmtree(path) if path.is_dir() else path.unlink()

    def clean_uploads(self) -> int:
        """启动时还没有上传在进行：上次没传完的临时文件都删掉。返回删了几个。"""
        leftovers = list(self.settings.uploads_dir.glob(f"*{UPLOAD_SUFFIX}"))
        for path in leftovers:
            path.unlink()
        return len(leftovers)

    @staticmethod
    def _records(root: Path) -> list[dict]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(root.glob(f"*/{RECORD_FILE}"))]

    def fail_project(self, pid: str, code: str, message: str) -> None:
        with self._lock:
            rec = self.get_project(pid)
            if is_terminal(rec.state):
                log.error(
                    "cannot fail terminal project", extra={"event": "project.fail_ignored", "code": code}
                )
                return
            self.transition_project(pid, ProjectState.FAILED, error={"code": code, "message": message})

    # ---- 用户保存的场景（整份替换，原子写，天然幂等） ----
    def save_scene(self, pid: str, text: str) -> None:
        with self._lock:  # 与删除互斥：确认项目还在再写
            self.get_project(pid)
            path = self.project_dir(pid) / SCENE_FILE
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)

    def load_scene(self, pid: str) -> str | None:
        path = self.project_dir(pid) / SCENE_FILE
        return path.read_text(encoding="utf-8") if path.exists() else None

    # ---- 导出 ----
    def find_export(self, eid: str) -> ExportRecord | None:
        data = self._read(self.export_dir(eid) / RECORD_FILE)
        return ExportRecord.from_dict(data) if data else None

    def get_export(self, eid: str) -> ExportRecord:
        rec = self.find_export(eid)
        if rec is None:
            raise NotFound(eid)
        return rec

    def create_export_if_absent(self, rec: ExportRecord) -> tuple[ExportRecord, bool]:
        with self._lock:
            existing = self.find_export(rec.id)
            if existing:
                return existing, False
            self._write(self.export_dir(rec.id) / RECORD_FILE, rec.to_dict())
            return rec, True

    def save_export(self, rec: ExportRecord) -> None:
        rec.updated_at = time.time()
        self._write(self.export_dir(rec.id) / RECORD_FILE, rec.to_dict())

    def transition_export(self, eid: str, target: ExportState, **changes) -> ExportRecord:
        with self._lock:
            rec = self.get_export(eid)
            check_transition(rec.state, target)
            previous = rec.state
            rec.state, rec.progress = target, 0.0
            if target is not ExportState.FAILED:
                rec.error = None
            for key, value in changes.items():
                setattr(rec, key, value)
            self.save_export(rec)
        log.info("export transition", extra={"event": "export.transition", "from": previous, "to": target})
        return rec

    def fail_export(self, eid: str, code: str, message: str) -> None:
        with self._lock:
            rec = self.get_export(eid)
            if is_terminal(rec.state):
                log.error("cannot fail terminal export", extra={"event": "export.fail_ignored", "code": code})
                return
            self.transition_export(eid, ExportState.FAILED, error={"code": code, "message": message})

    def stale_projects(self, version: int) -> list[ProjectRecord]:
        """已就绪、但分析结果版本低于 version 的项目（旧版分析没有 version 字段，按 1 算）。"""
        return [
            rec
            for rec in self.list_projects()
            if rec.state is ProjectState.READY and (rec.analysis or {}).get("version", 1) < version
        ]

    # ---- 启动恢复：上次没跑完的任务一律标记为中断 ----
    def recover_interrupted(self) -> None:
        for rec in self.list_projects():
            if not is_terminal(rec.state):
                message = "The last run was interrupted; import the song again to retry"
                self.fail_project(rec.id, PROJECT_INTERRUPTED, message)
        for data in self._records(self.settings.exports_dir):
            rec = ExportRecord.from_dict(data)
            if not is_terminal(rec.state):
                self.fail_export(
                    rec.id, EXPORT_INTERRUPTED, "The last export was interrupted; export again to retry"
                )
