"""HTTP 接口端到端（SPEC §6、§7、§8）：上传 → 分析 → 试听资源 → 幂等导出 → 下载；安全边界。"""

import io
import subprocess
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from scipy.signal import butter, sosfiltfilt

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


def test_failed_song_imported_again_is_retried_until_ready(client, tmp_path, monkeypatch):
    """第一次分轨前下载模型失败（界面提示“检查网络后重新导入”）：再导入同一个文件会重试，
    工位空闲时一路走到就绪（SPEC §6：FAILED → DECODING），旧的错误清掉。"""
    from orbit8d.separate.model import ModelDownloadError

    def offline(on_progress):
        raise ModelDownloadError("simulated network failure")

    song = write_song(tmp_path / "song.wav")
    with monkeypatch.context() as m:
        m.setattr(client.separator, "ensure_model", offline)
        first = upload(client, song)
        assert first.status_code == 202
        wait_idle(client)
    pid = first.json()["id"]
    failed = client.get(f"/api/projects/{pid}").json()
    assert failed["state"] == "FAILED" and failed["error"]["code"] == "MODEL_DOWNLOAD_FAILED"
    again = upload(client, song)
    assert again.status_code == 200 and again.json()["id"] == pid
    wait_idle(client)
    body = client.get(f"/api/projects/{pid}").json()
    assert body["state"] == "READY" and body["error"] is None and client.separator.calls == 1


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


# ---------- 存储与删除（SPEC §5.7、§7） ----------


def test_new_project_stores_flac_stems_and_no_decoded_wav(client, ready_project, data_dir):
    pdir = data_dir / "projects" / ready_project
    record = client.get(f"/api/projects/{ready_project}").json()
    assert not (pdir / "orig.wav").exists() and not list((pdir / "stems").glob("*.wav"))
    assert sorted(p.name for p in (pdir / "stems").glob("*.flac")) == [
        "bass.flac",
        "drums.flac",
        "other.flac",
        "vocals.flac",
    ]
    assert set(record["stem_scales"]) == {"vocals", "drums", "bass", "other"}
    assert all(v >= 1.0 for v in record["stem_scales"].values())


def test_stems_round_trip_through_flac_with_their_scale(client, data_dir, tmp_path):
    """峰值超过 1 的分轨也能无损（24 位量化以内）存取。"""
    song = tmp_path / "loud.wav"
    write_song(song)
    x, sr = sf.read(song, always_2d=True)
    sf.write(song, x * 6.0, sr, subtype="FLOAT")  # 远超满幅：贝斯分轨峰值约 1.5
    pid = upload(client, song).json()["id"]
    wait_idle(client)
    record = client.get(f"/api/projects/{pid}").json()
    assert record["state"] == "READY" and record["stem_scales"]["bass"] > 1.0
    from orbit8d.jobs.layout import ProjectFiles
    from orbit8d.jobs.stems import stem_file

    files = ProjectFiles(data_dir / "projects" / pid)
    bass = np.concatenate([w.body for w in stem_file(files, "bass", record["stem_scales"]).windows()])
    expected = sosfiltfilt(butter(4, 150, "lowpass", fs=sr, output="sos"), x * 6.0, axis=0)
    assert np.abs(bass - expected).max() < 4 * record["stem_scales"]["bass"] / 2**23


def test_old_storage_is_migrated_on_startup_and_stays_usable(client, ready_project, data_dir):
    """旧版项目（WAV 分轨 + 常驻 orig.wav）：重启后迁移成 FLAC、删掉 orig.wav，仍能重新分析与导出。"""
    import json

    pdir = data_dir / "projects" / ready_project
    record_path = pdir / "record.json"
    data = json.loads(record_path.read_text())
    for name in ("vocals", "drums", "bass", "other"):
        flac = pdir / "stems" / f"{name}.flac"
        x, sr = sf.read(flac, always_2d=True)
        sf.write(pdir / "stems" / f"{name}.wav", x * data["stem_scales"][name], sr, subtype="FLOAT")
        flac.unlink()
    sf.write(pdir / "orig.wav", np.zeros((sr, 2)), sr, subtype="FLOAT")
    data["stem_scales"] = {}
    data["analysis"]["version"] = 1  # 同时需要重新分析：迁移先跑
    record_path.write_text(json.dumps(data))
    with TestClient(create_app(load_settings(), FakeSeparator()), base_url=BASE) as fresh:
        assert fresh.app.state.runner.wait_idle(WAIT_S)
        body = fresh.get(f"/api/projects/{ready_project}").json()
        resp = fresh.post(
            f"/api/projects/{ready_project}/exports", json={"scene": Scene().model_dump(), "format": "wav"}
        )
        assert fresh.app.state.runner.wait_idle(WAIT_S)
        assert fresh.get(f"/api/exports/{resp.json()['id']}").json()["state"] == "DONE"
    assert body["state"] == "READY" and body["analysis"]["version"] == ANALYSIS_VERSION
    assert not list((pdir / "stems").glob("*.wav")) and not (pdir / "orig.wav").exists()
    assert set(body["stem_scales"]) == {"vocals", "drums", "bass", "other"}


def test_migration_keeps_the_wav_when_verification_fails(client, ready_project, data_dir, monkeypatch):
    from orbit8d.jobs import stems

    pdir = data_dir / "projects" / ready_project
    flac = pdir / "stems" / "vocals.flac"
    x, sr = sf.read(flac, always_2d=True)
    sf.write(pdir / "stems" / "vocals.wav", x, sr, subtype="FLOAT")

    def broken_verify(original, copy, tolerance):
        raise ValueError("simulated corruption")

    monkeypatch.setattr(stems, "verify_copy", broken_verify)
    runner = client.app.state.runner
    assert runner.migrate_storage() == 1
    wait_idle(client)
    assert (pdir / "stems" / "vocals.wav").exists()  # 校验没过，原文件保留


def test_projects_are_listed_most_recent_first(client, ready_project, tmp_path):
    second = upload(client, write_song(tmp_path / "other.wav", seed=3), "Other.wav").json()["id"]
    wait_idle(client)
    client.put(f"/api/projects/{ready_project}/scene", json=Scene().model_dump(mode="json"))
    items = client.get("/api/projects").json()
    assert [item["id"] for item in items] == [ready_project, second]
    first = items[0]
    assert first["title"] == "My Song" and first["state"] == "READY"
    assert first["duration_s"] == pytest.approx(6.0, abs=0.01) and first["size_bytes"] > 100_000


def test_deleting_a_song_removes_it_and_its_exports(client, ready_project, data_dir, tmp_path):
    resp = client.post(
        f"/api/projects/{ready_project}/exports", json={"scene": Scene().model_dump(), "format": "wav"}
    )
    wait_idle(client)
    eid = resp.json()["id"]
    assert client.delete(f"/api/projects/{ready_project}").status_code == 204
    assert client.get(f"/api/projects/{ready_project}").status_code == 404
    assert client.get(f"/api/exports/{eid}").status_code == 404
    assert not (data_dir / "projects" / ready_project).exists() and not list((data_dir / "trash").iterdir())
    assert client.get("/api/projects").json() == []
    assert client.delete(f"/api/projects/{ready_project}").status_code == 404
    again = upload(client, write_song(tmp_path / "song.wav"), "My Song.wav")
    assert again.status_code == 202 and again.json()["id"] == ready_project  # 删了可以重新导入同一个文件
    wait_idle(client)


def test_deleting_is_refused_while_the_song_has_jobs(client, ready_project, monkeypatch):
    """有排队或正在跑的任务时不删（409），任务结束后可以删。"""
    import threading

    started, release = threading.Event(), threading.Event()
    runner = client.app.state.runner

    def slow_migrate(pid):
        started.set()
        release.wait(WAIT_S)

    monkeypatch.setattr(runner, "_run_migrate", slow_migrate)
    runner.submit("migrate", ready_project, ready_project)
    assert started.wait(WAIT_S)
    try:
        resp = client.delete(f"/api/projects/{ready_project}")
        assert resp.status_code == 409 and resp.json()["code"] == "PROJECT_BUSY"
    finally:
        release.set()
        wait_idle(client)
    cross = client.delete(f"/api/projects/{ready_project}", headers={"Origin": "https://evil.example"})
    assert cross.status_code == 403 and client.get(f"/api/projects/{ready_project}").status_code == 200
    assert client.delete("/api/projects/0123456789abcdef").status_code == 404
    assert client.delete("/api/projects/not-an-id").status_code == 404
    assert client.delete(f"/api/projects/{ready_project}").status_code == 204


def test_startup_cleans_trash_uploads_and_stale_assets(data_dir):
    (data_dir / "trash" / "project-x").mkdir(parents=True)
    (data_dir / "uploads").mkdir(parents=True, exist_ok=True)
    (data_dir / "uploads" / "half.part").write_bytes(b"x")
    for name in ("eq_44100.wav", "brir_hall_44100.wav", "brir_v1_room_44100.wav", "hrtf.tmp"):
        (data_dir / "assets" / name).write_bytes(b"x")
    with TestClient(create_app(load_settings(), FakeSeparator()), base_url=BASE) as fresh:  # 启动时清理
        assert fresh.get("/api/health").status_code == 200
    assert not list((data_dir / "trash").iterdir()) and not list((data_dir / "uploads").iterdir())
    left = {p.name for p in (data_dir / "assets").iterdir()}
    assert not {"eq_44100.wav", "brir_hall_44100.wav", "brir_v1_room_44100.wav", "hrtf.tmp"} & left
    assert any(name.endswith(".sofa") for name in left)  # HRTF 原始数据不动


# ---------- 分轨模型（SPEC §2、§12） ----------


class DownloadingSeparator(FakeSeparator):
    """第一次分轨前“下载模型”：下载途中看得到子步骤与进度；fail=True 时下载失败。"""

    def __init__(self, store_probe, fail: bool = False):
        super().__init__()
        self.store_probe, self.fail, self.seen = store_probe, fail, []

    def ensure_model(self, on_progress):
        on_progress(0.5)
        self.seen.append(self.store_probe())
        if self.fail:
            from orbit8d.separate.model import ModelDownloadError

            raise ModelDownloadError("simulated network failure")


@pytest.mark.parametrize("fail", [False, True])
def test_first_separation_reports_the_model_download(data_dir, tmp_path, fail):
    holder = {}

    def probe():
        rec = holder["app"].state.store.find_project(holder["pid"])
        return rec.state, rec.step, rec.progress

    separator = DownloadingSeparator(probe, fail)
    app = create_app(load_settings(), separator)
    holder["app"] = app
    song = write_song(tmp_path / "song.wav")
    import hashlib

    holder["pid"] = hashlib.sha256(song.read_bytes()).hexdigest()[:16]
    with TestClient(app, base_url=BASE) as c:
        assert upload(c, song).status_code == 202
        wait_idle(c)
        body = c.get(f"/api/projects/{holder['pid']}").json()
    assert separator.seen == [("SEPARATING", "model_download", 0.5)]
    if fail:
        assert body["state"] == "FAILED" and body["error"]["code"] == "MODEL_DOWNLOAD_FAILED"
    else:
        assert body["state"] == "READY" and body["step"] == ""


def test_interrupted_export_gets_its_own_error_code(client, ready_project, data_dir):
    """上次导出到一半服务被关：重启后标为失败，错误码与“导入被中断”分开（界面提示“重新导出即可”）。"""
    import json

    resp = client.post(
        f"/api/projects/{ready_project}/exports", json={"scene": Scene().model_dump(), "format": "wav"}
    )
    wait_idle(client)
    eid = resp.json()["id"]
    record = data_dir / "exports" / eid / "record.json"
    data = json.loads(record.read_text())
    data["state"] = "RENDERING"
    record.write_text(json.dumps(data))
    with TestClient(create_app(load_settings(), FakeSeparator()), base_url=BASE) as fresh:
        body = fresh.get(f"/api/exports/{eid}").json()
    assert body["state"] == "FAILED" and body["error"]["code"] == "EXPORT_INTERRUPTED"


def test_export_loudness_option(client, tmp_path):
    """标准响度导出到 -14 LUFS，比默认的“响亮”小声约 5 dB；两个选项是不同的导出（各自幂等）。
    用波峰平缓的和弦当歌：两个目标都不会先被峰值限制卡住。"""
    from orbit8d.engine.master import integrated_loudness

    t = np.arange(int(8.0 * 44100)) / 44100
    chord = sum(0.04 * np.sin(2 * np.pi * f * t) for f in (220.0, 277.2, 329.6, 440.0))
    song = tmp_path / "chord.wav"
    sf.write(
        song, np.stack([chord, chord], axis=1) * (0.8 + 0.2 * np.sin(2 * np.pi * 0.25 * t))[:, None], 44100
    )
    pid = upload(client, song).json()["id"]
    wait_idle(client)
    levels = {}
    for loudness in ("loud", "standard"):
        payload = {"scene": Scene().model_dump(mode="json"), "format": "wav", "loudness": loudness}
        first = client.post(f"/api/projects/{pid}/exports", json=payload)
        assert client.post(f"/api/projects/{pid}/exports", json=payload).json()["id"] == first.json()["id"]
        wait_idle(client)
        y, sr = sf.read(
            io.BytesIO(client.get(f"/api/exports/{first.json()['id']}/file").content), always_2d=True
        )
        levels[loudness] = integrated_loudness(y, sr)
    assert levels["standard"] == pytest.approx(-14.0, abs=0.3)
    assert levels["loud"] == pytest.approx(-9.0, abs=0.3)
    bad = {"scene": Scene().model_dump(mode="json"), "format": "wav", "loudness": "deafening"}
    assert client.post(f"/api/projects/{pid}/exports", json=bad).status_code == 422


def test_rechoreograph_endpoint_uses_the_posted_sections(client, ready_project):
    url = f"/api/projects/{ready_project}/choreography"
    scene = Scene.model_validate(client.get(url).json())
    scene.sections[0].label = "我的段落"
    scene.mix["vocals"].gain_db = -3.0
    resp = client.post(url, json={"scene": scene.model_dump(mode="json")})
    assert resp.status_code == 200
    out = Scene.model_validate(resp.json())
    assert out.sections[0].label == "我的段落" and out.mix["vocals"].gain_db == -3.0
    bad = scene.model_dump(mode="json")
    bad["rear_darken_db"] = 99
    assert client.post(url, json={"scene": bad}).status_code == 422
    assert (
        client.post(
            "/api/projects/0123456789abcdef/choreography", json={"scene": scene.model_dump(mode="json")}
        ).status_code
        == 404
    )
