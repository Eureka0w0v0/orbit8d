"""显式状态机（SPEC §6）：只允许表里列出的跳转。"""

import pytest

from orbit8d.jobs.states import (
    EXPORT_TRANSITIONS,
    PROJECT_TRANSITIONS,
    ExportState,
    IllegalTransition,
    ProjectState,
    check_transition,
    is_terminal,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ProjectState.UPLOADED, ProjectState.DECODING),
        (ProjectState.DECODING, ProjectState.SEPARATING),
        (ProjectState.SEPARATING, ProjectState.ANALYZING),
        (ProjectState.ANALYZING, ProjectState.READY),
        (ProjectState.SEPARATING, ProjectState.FAILED),
        (ProjectState.FAILED, ProjectState.DECODING),
        (ProjectState.READY, ProjectState.ANALYZING),
        (ExportState.QUEUED, ExportState.RENDERING),
        (ExportState.RENDERING, ExportState.ENCODING),
        (ExportState.ENCODING, ExportState.DONE),
        (ExportState.FAILED, ExportState.QUEUED),
    ],
)
def test_legal_transitions(current, target):
    check_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ProjectState.UPLOADED, ProjectState.READY),
        (ProjectState.READY, ProjectState.DECODING),
        (ProjectState.READY, ProjectState.FAILED),
        (ProjectState.DECODING, ProjectState.ANALYZING),
        (ExportState.DONE, ExportState.QUEUED),
        (ExportState.QUEUED, ExportState.DONE),
        (ProjectState.DECODING, ExportState.RENDERING),
    ],
)
def test_illegal_transitions_raise(current, target):
    with pytest.raises(IllegalTransition):
        check_transition(current, target)


def test_every_state_has_a_row_and_terminals_are_correct():
    assert set(PROJECT_TRANSITIONS) == set(ProjectState)
    assert set(EXPORT_TRANSITIONS) == set(ExportState)
    assert is_terminal(ProjectState.READY) and is_terminal(ProjectState.FAILED)
    assert is_terminal(ExportState.DONE) and not is_terminal(ExportState.ENCODING)
