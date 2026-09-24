"""HTTP 接口端到端（SPEC §6、§7、§8）：上传 → 分析 → 试听资源 → 幂等导出 → 下载；安全边界。"""

import io
import subprocess
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from orbit8d.api.app import create_app
from orbit8d.config import load_settings
from orbit8d.engine.pipeline import ANALYSIS_VERSION
from orbit8d.engine.scene import Scene, preset
from tests.conftest import FakeSeparator, write_song

BASE = "http://127.0.0.1:8765"
WAIT_S = 120
PREVIEW = ("vocals_hi", "bass_hi", "drums_hi", "other_hi", "bass_sub", "drums_sub", "other_sub", "original")
STEREO_PREVIEW = ("drums_hi", "other_hi", "original")


@pytest.fixture
def client(data_dir: Path):
    separator = FakeSeparator()
    app = create_app(load_settings(), separator)
    with TestClient(app, base_url=BASE) as c:
        c.separator = separator
        yield c


def upload(client: TestClient, path: Path, name: str | None = None):
    return client.post(
        "/api/projects",
        content=path.read_bytes(),
        headers={"Content-Type": "application/octet-stream", "X-Filename": quote(name or path.name)},
    )


def wait_idle(client: TestClient) -> None:
    assert client.app.state.runner.wait_idle(WAIT_S), "后台任务超时"


@pytest.fixture
def ready_project(client: TestClient, tmp_path: Path) -> str:
    resp = upload(client, write_song(tmp_path / "song.wav"), "My Song.wav")
    assert resp.status_code == 202, resp.text
    wait_idle(client)
    body = client.get(f"/api/projects/{resp.json()['id']}").json()
    assert body["state"] == "READY", body
    return body["id"]


def test_health_lists_formats(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True and {"m4a", "mp3", "flac", "wav", "ogg"} <= set(body["formats"])


def test_upload_to_ready_exposes_analysis_and_preview_stems(client, ready_project):
    body = client.get(f"/api/projects/{ready_project}").json()
    analysis = body["analysis"]
    assert analysis["default_bars"] in (1, 2, 4, 8) and analysis["preview_gain"] > 0
    assert set(analysis["calibration"]) == {"vocals", "drums", "bass", "other", "sub"}
    assert body["source"]["filename"] == "My Song.wav" and body["source"]["duration_s"] == pytest.approx(
        6.0, abs=0.01
    )
    for name in PREVIEW:
        resp = client.get(f"/api/projects/{ready_project}/stems/{name}.flac")
        assert resp.status_code == 200 and resp.headers["content-type"] == "audio/flac"
        data, sr = sf.read(io.BytesIO(resp.content), always_2d=True)
        assert sr == 44100 and data.shape[1] == (2 if name in STEREO_PREVIEW else 1)


def test_duplicate_upload_reuses_project(client, ready_project, tmp_path):
    again = upload(client, write_song(tmp_path / "copy.wav"), "copy.wav")
    assert again.status_code == 200 and again.json()["id"] == ready_project
    wait_idle(client)
    assert client.separator.calls == 1


def test_non_audio_upload_is_rejected_without_leftovers(client, data_dir, tmp_path):
    bad = tmp_path / "evil.mp3"
    bad.write_text("not audio at all")
    resp = upload(client, bad)
    assert resp.status_code == 400 and resp.json()["code"] in ("NO_AUDIO", "PROBE_FAILED")
    assert not any((data_dir / "uploads").iterdir()) and not any((data_dir / "projects").iterdir())


def test_oversized_upload_is_rejected(data_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("ORBIT8D_MAX_UPLOAD_MB", "0.01")
    with TestClient(create_app(load_settings(), FakeSeparator()), base_url=BASE) as c:
        resp = upload(c, write_song(tmp_path / "big.wav"))
    assert resp.status_code == 413
    assert not any((data_dir / "uploads").iterdir())


def test_filename_cannot_escape_data_dir(client, data_dir, tmp_path):
    resp = upload(client, write_song(tmp_path / "x.wav", seed=3), "../../../../etc/passwd")
    assert resp.status_code == 202
    project_dir = data_dir / "projects" / resp.json()["id"]
    assert project_dir.is_dir() and not (data_dir.parent / "etc").exists()
    assert resp.json()["source"]["filename"] == "passwd"
    wait_idle(client)


def test_foreign_host_header_is_rejected(client):
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400


def test_cross_site_post_is_rejected(client, tmp_path):
    resp = client.post(
        "/api/projects",
        content=write_song(tmp_path / "y.wav").read_bytes(),
        headers={"Origin": "https://evil.example", "Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 403


@pytest.mark.parametrize("bad_id", ["..", "..%2F..%2Fetc", "ZZZZ", "0123456789abcdef0"])
def test_malformed_ids_are_not_found(client, bad_id):
    assert client.get(f"/api/projects/{bad_id}").status_code == 404
    assert client.get(f"/api/exports/{bad_id}").status_code == 404


def test_assets_are_served(client):
    hrtf = client.get("/api/assets/hrtf.bin")
    assert hrtf.status_code == 200 and hrtf.content[:4] == b"O8DH"
    assert client.get("/api/assets/eq.wav").status_code != 200  # 已改为按项目、按场景的 EQ
    brir, sr = sf.read(io.BytesIO(client.get("/api/assets/brir/room.wav").content), always_2d=True)
    assert sr == 44100 and brir.shape[1] == 2
    assert client.get("/api/assets/brir/stadium.wav").status_code == 404


def test_export_is_idempotent_and_downloadable(client, ready_project, tmp_path):
    payload = {"scene": Scene().model_dump(mode="json"), "format": "m4a"}
    first = client.post(f"/api/projects/{ready_project}/exports", json=payload)
    second = client.post(f"/api/projects/{ready_project}/exports", json=payload)
    assert first.status_code == 202 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    wait_idle(client)
    eid = first.json()["id"]
    assert client.get(f"/api/exports/{eid}").json()["state"] == "DONE"
    resp = client.get(f"/api/exports/{eid}/file")
    assert resp.status_code == 200 and "My%20Song%20%288D%29.m4a" in resp.headers["content-disposition"]
    out = tmp_path / "dl.m4a"
    out.write_bytes(resp.content)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert abs(float(probe.stdout) - 6.0) < 0.06


@pytest.mark.parametrize("fmt", ["mp3", "flac", "wav", "ogg"])
def test_other_export_formats(client, ready_project, tmp_path, fmt):
    resp = client.post(
        f"/api/projects/{ready_project}/exports", json={"scene": Scene().model_dump(), "format": fmt}
    )
    assert resp.status_code == 202
    wait_idle(client)
    data = client.get(f"/api/exports/{resp.json()['id']}/file")
    assert data.status_code == 200 and len(data.content) > 1000


def test_invalid_export_requests(client, ready_project):
    scene = Scene().model_dump(mode="json")
    scene["rear_darken_db"] = 99
    assert (
        client.post(
            f"/api/projects/{ready_project}/exports", json={"scene": scene, "format": "m4a"}
        ).status_code
        == 422
    )
    good = Scene().model_dump(mode="json")
    assert (
        client.post(
            f"/api/projects/{ready_project}/exports", json={"scene": good, "format": "exe"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/projects/0123456789abcdef/exports", json={"scene": good, "format": "m4a"}
        ).status_code
        == 404
    )


def test_rendered_export_moves_sound_between_ears(client, ready_project, tmp_path):
    """冒烟：导出的 wav 里，两耳能量差确实随时间变化（声音在转）。"""
    resp = client.post(
        f"/api/projects/{ready_project}/exports", json={"scene": Scene().model_dump(), "format": "wav"}
    )
    wait_idle(client)
    y, sr = sf.read(io.BytesIO(client.get(f"/api/exports/{resp.json()['id']}/file").content), always_2d=True)
    frame = sr // 10
    frames = y[: len(y) // frame * frame].reshape(-1, frame, 2)
    ild = 10 * np.log10((frames[:, :, 0] ** 2).sum(1) / (frames[:, :, 1] ** 2).sum(1))
    assert ild.max() - ild.min() > 6


@pytest.mark.parametrize("name", ["classic", "singer", "dual", "tumble"])
def test_presets_endpoint_returns_valid_scenes(client, name):
    resp = client.get(f"/api/presets/{name}", params={"bars": 2})
    assert resp.status_code == 200
    scene = Scene.model_validate(resp.json())
    assert any(o.speed.bars == 2 for o in scene.sections[0].orbits.values())


def test_presets_endpoint_rejects_bad_input(client):
    assert client.get("/api/presets/chaos").status_code == 404
    assert client.get("/api/presets/classic", params={"bars": 3}).status_code == 422


def test_scene_schema_exposes_parameter_ranges(client):
    schema = client.get("/api/scene/schema").json()
    radius = schema["$defs"]["Orbit"]["properties"]["radius_m"]
    assert radius["minimum"] == 0.5 and radius["maximum"] == 4.0


def test_presets_list_endpoint(client):
    names = client.get("/api/presets").json()
    assert names == ["classic", "singer", "dual", "tumble", "layers", "diagonal", "cross"]
    assert client.get("/api/presets/single").status_code == 404  # 单点环绕已移除


def test_analysis_has_sections_tone_anchor_and_version(client, ready_project):
    analysis = client.get(f"/api/projects/{ready_project}").json()["analysis"]
    assert analysis["version"] == ANALYSIS_VERSION and len(analysis["match_eq_db"]) == 27
    assert analysis["sections"][0]["start_s"] == 0.0 and {"vocals", "drums_L", "other_M"} <= set(
        analysis["spectra"]
    )


def test_choreography_endpoint_returns_a_valid_scene(client, ready_project):
    resp = client.get(f"/api/projects/{ready_project}/choreography")
    assert resp.status_code == 200
    scene = Scene.model_validate(resp.json())
    assert scene.sections[0].start_s == 0.0
    assert client.get("/api/projects/0123456789abcdef/choreography").status_code == 404


def test_scene_eq_depends_on_the_scene(client, ready_project):
    def fir(scene):
        resp = client.post(f"/api/projects/{ready_project}/eq", json={"scene": scene.model_dump(mode="json")})
        assert resp.status_code == 200 and resp.headers["content-type"] == "audio/wav"
        data, sr = sf.read(io.BytesIO(resp.content))
        assert sr == 44100 and data.ndim == 1 and len(data) == 1025
        return data

    bars = client.get(f"/api/projects/{ready_project}").json()["analysis"]["default_bars"]
    classic, again, layers = (
        fir(preset("classic", bars)),
        fir(preset("classic", bars)),
        fir(preset("layers", bars)),
    )
    assert np.array_equal(classic, again) and not np.allclose(classic, layers)
    bad = {"scene": {**preset("classic", bars).model_dump(mode="json"), "rear_darken_db": 99}}
    assert client.post(f"/api/projects/{ready_project}/eq", json=bad).status_code == 422


def test_stale_projects_are_reanalyzed_on_startup_without_reseparating(client, ready_project, data_dir):
    """模拟 v1 时代分析的项目：重启后自动转回“分析中”，重新分析但不重新分轨，完成后可用。"""
    import json

    record = data_dir / "projects" / ready_project / "record.json"
    data = json.loads(record.read_text())
    for key in ("version", "match_eq_db", "spectra", "sections"):
        data["analysis"].pop(key)
    record.write_text(json.dumps(data))
    separator = FakeSeparator()
    with TestClient(create_app(load_settings(), separator), base_url=BASE) as fresh:
        assert fresh.get(f"/api/projects/{ready_project}").json()["state"] in ("ANALYZING", "READY")
        assert fresh.app.state.runner.wait_idle(WAIT_S)
        body = fresh.get(f"/api/projects/{ready_project}").json()
    assert body["state"] == "READY" and body["analysis"]["version"] == ANALYSIS_VERSION
    assert separator.calls == 0


def test_analysis_v3_has_original_gain_and_envelope(client, ready_project):
    analysis = client.get(f"/api/projects/{ready_project}").json()["analysis"]
    assert analysis["version"] == 3 and 0 < analysis["original_gain"] < 16
    env, hop = analysis["envelope_db"], analysis["envelope_hop_s"]
    assert hop == 0.25 and len(env) == pytest.approx(6.0 / hop, abs=1)
    assert max(env) == 0.0 and min(env) >= -60.0


def test_scene_is_saved_per_project_and_validated(client, ready_project):
    url = f"/api/projects/{ready_project}/scene"
    assert client.get(url).status_code == 404  # 还没保存过
    scene = Scene().model_dump(mode="json")
    scene["sections"][0]["label"] = "我的版本"
    scene["mix"]["drums"]["gain_db"] = -4.5
    assert client.put(url, json=scene).status_code == 204
    assert client.put(url, json=scene).status_code == 204  # 整份替换，重复保存结果不变
    back = client.get(url).json()
    assert Scene.model_validate(back) == Scene.model_validate(scene)

    bad = {**scene, "rear_darken_db": 99}
    resp = client.put(url, json=bad)
    assert resp.status_code == 422 and resp.json()["code"] == "INVALID_SCENE"
    assert client.get(url).json()["mix"]["drums"]["gain_db"] == -4.5  # 不合法的不会覆盖已保存的

    huge = b'{"x": "' + b"a" * 300_000 + b'"}'
    resp = client.put(url, content=huge, headers={"Content-Type": "application/json"})
    assert resp.status_code == 413
    assert client.put("/api/projects/0123456789abcdef/scene", json=scene).status_code == 404
    assert client.put(url, json=scene, headers={"Origin": "https://evil.example"}).status_code == 403
