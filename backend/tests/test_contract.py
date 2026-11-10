"""前后端契约（shared/golden/contract.json）：后端常量的快照，TS 端用同一份对照 TS 常量、类型与 i18n
（web/src/contract.test.ts）。后端改了这些常量就重新生成：uv run python -m tests.make_golden，
并在提交说明里写明前端要跟着改什么。"""

import ast
from typing import get_args

from fastapi.testclient import TestClient

from orbit8d.api.app import PREVIEW_NAMES, ExportRequest, create_app
from orbit8d.api.guards import REFUSAL_STATUS
from orbit8d.config import load_settings
from orbit8d.engine import choreo
from orbit8d.engine.scene import MAX_BARS, TrackName
from tests.conftest import FakeSeparator
from tests.make_golden import CONTRACT_FILE, PACKAGE_DIR, contract, contract_json

BASE = "http://127.0.0.1:8765"


def _field(c: dict, definition: str, name: str) -> dict:
    return c["scene_schema"]["$defs"][definition]["properties"][name]


def _bounds(c: dict, definition: str, name: str) -> list[float]:
    field = _field(c, definition, name)
    return [field["minimum"], field["maximum"]]


def _literal(model, name: str) -> list:
    return list(get_args(model.model_fields[name].annotation))


def test_contract_matches_the_committed_file():
    assert CONTRACT_FILE.exists(), "缺少契约文件：运行 uv run python -m tests.make_golden"
    assert contract_json() == CONTRACT_FILE.read_text(encoding="utf-8"), (
        "后端的枚举 / 布局 / 上限 / 错误码变了：重新生成 contract.json，再让前端跟上（cd web && npm test）"
    )


def test_backend_copies_agree_with_the_contract():
    """同一个枚举或上限在后端还有别的副本（场景模型的 Literal、接口白名单、引擎常量），都必须与契约一致。"""
    c = contract()
    copies = {
        "scene.TrackName": (list(get_args(TrackName)), c["tracks"]),
        "Orbit.shape": (_field(c, "Orbit", "shape")["enum"], c["shapes"]),
        "Room.name": (_field(c, "Room", "name")["enum"], c["rooms"]),
        "Speed.bars": (_field(c, "Speed", "bars")["enum"], c["bar_choices"]),
        "scene.MAX_BARS": (MAX_BARS, max(c["bar_choices"])),
        "ExportRequest.format": (_literal(ExportRequest, "format"), c["export_formats"]),
        "ExportRequest.loudness": (_literal(ExportRequest, "loudness"), c["loudness"]),
        "api.PREVIEW_NAMES": (set(PREVIEW_NAMES), {*c["preview_files"], c["original_preview"]}),
        "choreo 事件时长": ([choreo.MIN_EVENT_S, choreo.MAX_EVENT_S], _bounds(c, "Event", "duration_s")),
        "choreo 混响量": (list(choreo.WET_RANGE_DB), _bounds(c, "Section", "wet_db")),
    }
    assert {name: pair for name, pair in copies.items() if pair[0] != pair[1]} == {}


def test_every_code_the_use_cases_refuse_with_has_an_http_status():
    """jobs 层用 Refused(错误码, 说明) 拒绝请求，HTTP 状态码由入口层按错误码定（api/guards.py）。"""
    refused = {
        node.args[0].value
        for path in sorted(PACKAGE_DIR.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Refused"
    }
    assert refused == set(REFUSAL_STATUS)


def test_preview_files_cover_each_source_channel_once():
    c = contract()
    channels = sorted(ch for group in c["preview_files"].values() for ch in group)
    assert channels == list(range(len(c["source_channels"])))


def test_scene_schema_endpoint_serves_the_contract_schema(tmp_path, monkeypatch):
    """前端读字段名与滑杆范围的 GET /api/scene/schema 就是契约里的 scene_schema。
    不进 with：不跑启动流程，用不到 HRTF。"""
    monkeypatch.setenv("ORBIT8D_DATA_DIR", str(tmp_path / "data"))
    client = TestClient(create_app(load_settings(), FakeSeparator()), base_url=BASE)
    assert client.get("/api/scene/schema").json() == contract()["scene_schema"]
