"""项目与导出的显式状态机（SPEC §6）。所有状态变化都必须经过 check_transition。"""

from enum import StrEnum


class ProjectState(StrEnum):
    UPLOADED = "UPLOADED"
    DECODING = "DECODING"
    SEPARATING = "SEPARATING"
    ANALYZING = "ANALYZING"
    READY = "READY"
    FAILED = "FAILED"


class ExportState(StrEnum):
    QUEUED = "QUEUED"
    RENDERING = "RENDERING"
    ENCODING = "ENCODING"
    DONE = "DONE"
    FAILED = "FAILED"


PROJECT_TRANSITIONS: dict[ProjectState, frozenset[ProjectState]] = {
    ProjectState.UPLOADED: frozenset({ProjectState.DECODING, ProjectState.FAILED}),
    ProjectState.DECODING: frozenset({ProjectState.SEPARATING, ProjectState.FAILED}),
    ProjectState.SEPARATING: frozenset({ProjectState.ANALYZING, ProjectState.FAILED}),
    ProjectState.ANALYZING: frozenset({ProjectState.READY, ProjectState.FAILED}),
    ProjectState.READY: frozenset(),
    ProjectState.FAILED: frozenset({ProjectState.DECODING}),  # 同一文件重新导入时重试
}

EXPORT_TRANSITIONS: dict[ExportState, frozenset[ExportState]] = {
    ExportState.QUEUED: frozenset({ExportState.RENDERING, ExportState.FAILED}),
    ExportState.RENDERING: frozenset({ExportState.ENCODING, ExportState.FAILED}),
    ExportState.ENCODING: frozenset({ExportState.DONE, ExportState.FAILED}),
    ExportState.DONE: frozenset(),
    ExportState.FAILED: frozenset({ExportState.QUEUED}),  # 同参数重新提交时重试
}

TERMINAL = frozenset({ProjectState.READY, ProjectState.FAILED, ExportState.DONE, ExportState.FAILED})


class IllegalTransition(RuntimeError):
    def __init__(self, current: StrEnum, target: StrEnum):
        super().__init__(f"非法状态跳转: {current} → {target}")
        self.current, self.target = current, target


def check_transition(current: StrEnum, target: StrEnum) -> None:
    if type(current) is not type(target):
        raise IllegalTransition(current, target)
    table = PROJECT_TRANSITIONS if isinstance(current, ProjectState) else EXPORT_TRANSITIONS
    if target not in table[current]:
        raise IllegalTransition(current, target)


def is_terminal(state: StrEnum) -> bool:
    return state in TERMINAL
