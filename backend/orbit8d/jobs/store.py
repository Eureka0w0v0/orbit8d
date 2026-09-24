"""项目与导出记录的文件存储：每条记录一个 JSON（原子写）；状态变化只能经过状态机。"""

import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from orbit8d.config import Settings
from orbit8d.jobs.states import ExportState, ProjectState, check_transition, is_terminal

log = logging.getLogger(__name__)

ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")
RECORD_FILE = "record.json"
SCENE_FILE = "scene.json"
INTERRUPTED = "INTERRUPTED"


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
    error: dict | None = None
    analysis: dict | None = None
    preview_scale: float = 1.0
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
        for d in (settings.projects_dir, settings.exports_dir, settings.uploads_dir):
            d.mkdir(parents=True, exist_ok=True)

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
            rec.state, rec.progress = target, 0.0
            if target is not ProjectState.FAILED:
                rec.error = None
            for key, value in changes.items():
                setattr(rec, key, value)
            self.save_project(rec)
        log.info("project transition", extra={"event": "project.transition", "from": previous, "to": target})
        return rec

    def set_project_progress(self, pid: str, progress: float) -> None:
        with self._lock:
            rec = self.get_project(pid)
            rec.progress = min(max(progress, 0.0), 1.0)
            self.save_project(rec)

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
        out = []
        for path in self.settings.projects_dir.glob(f"*/{RECORD_FILE}"):
            rec = ProjectRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            if rec.state is ProjectState.READY and (rec.analysis or {}).get("version", 1) < version:
                out.append(rec)
        return out

    # ---- 启动恢复：上次没跑完的任务一律标记为中断 ----
    def recover_interrupted(self) -> None:
        for path in self.settings.projects_dir.glob(f"*/{RECORD_FILE}"):
            rec = ProjectRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            if not is_terminal(rec.state):
                self.fail_project(rec.id, INTERRUPTED, "上次处理被中断，重新导入即可重试")
        for path in self.settings.exports_dir.glob(f"*/{RECORD_FILE}"):
            rec = ExportRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            if not is_terminal(rec.state):
                self.fail_export(rec.id, INTERRUPTED, "上次导出被中断，重新导出即可")
