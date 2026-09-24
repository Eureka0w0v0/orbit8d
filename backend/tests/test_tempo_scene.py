"""测速与默认转速（SPEC §4.2）、场景白名单校验与预设（SPEC §4.4）。"""

import numpy as np
import pytest
from pydantic import ValidationError

from orbit8d.engine.scene import PRESETS, Scene, canonical_json, effective_track_gains, orbit_params, preset
from orbit8d.engine.tempo import estimate_tempo, turn_plan

SR = 44100


def drum_loop(bpm: float, seconds: float, accent_first_beat: bool = True) -> np.ndarray:
    """合成鼓：每拍一个底鼓（每小节第一拍更重），每八分音符一个踩镲。"""
    n = int(seconds * SR)
    x = np.zeros(n)
    rng = np.random.default_rng(0)
    beat = 60.0 / bpm
    kick_t = np.arange(int(0.08 * SR)) / SR
    kick = np.sin(2 * np.pi * 60 * kick_t) * np.exp(-kick_t / 0.03)
    hat = rng.standard_normal(int(0.02 * SR)) * np.exp(-np.arange(int(0.02 * SR)) / (0.004 * SR))
    for k in range(int(seconds / beat * 2)):
        start = int((0.25 + k * beat / 2) * SR)
        if start + len(kick) >= n:
            break
        x[start : start + len(hat)] += 0.3 * hat
        if k % 2 == 0:
            accent = 1.6 if accent_first_beat and (k // 2) % 4 == 0 else 1.0
            x[start : start + len(kick)] += accent * kick
    return x


@pytest.mark.parametrize(("bpm", "bars"), [(90.0, 2), (134.0, 4), (67.0, 4)])
def test_tempo_and_default_turn(bpm, bars):
    info = estimate_tempo(drum_loop(bpm, 40.0), SR)
    expected_norm = bpm if 70 <= bpm < 140 else bpm * 2
    assert info.bpm_norm == pytest.approx(expected_norm, abs=0.1)
    got_bars, t_ref = turn_plan(info)
    assert got_bars == bars
    bar = 4 * 60.0 / bpm
    phase = (t_ref - 0.25) % bar
    assert min(phase, bar - phase) < 0.03  # 正前方对准重拍（每小节第一拍）


def test_default_scene_is_valid_and_complete():
    scene = Scene()
    assert set(scene.tracks) == {"vocals", "drums", "bass", "other"}
    assert scene.tracks["drums"].width_deg == 40 and scene.tracks["vocals"].width_deg == 0
    assert scene.room.name == "hall" and scene.rear_darken_db == 6


@pytest.mark.parametrize(
    "patch",
    [
        {"rear_darken_db": 13},
        {"room": {"name": "stadium"}},
        {"room": {"wet_db": 3}},
        {"tracks": {"vocals": {"gain_db": 30}}},
        {"tracks": {"vocals": {"orbit": {"radius_m": 0.1}}}},
        {"tracks": {"vocals": {"orbit": {"shape": "square"}}}},
        {"tracks": {"vocals": {"orbit": {"speed": {"mode": "bars", "bars": 3}}}}},
        {"tracks": {"vocals": {"orbit": {"speed": {"mode": "seconds", "seconds": 1}}}}},
        {"tracks": {"vocals": {"orbit": {"pitch_deg": float("nan")}}}},
        {"tracks": {"vocals": {"hack": 1}}},
        {"tracks": {"piano": {}}},
        {"version": 2},
    ],
)
def test_out_of_range_or_unknown_fields_are_rejected(patch):
    data = Scene().model_dump(mode="json")
    for key, value in patch.items():
        if key == "tracks":
            for name, fields in value.items():
                track = data["tracks"].setdefault(name, {})
                for fk, fv in fields.items():
                    if fk == "orbit":
                        track["orbit"].update(fv)
                    else:
                        track[fk] = fv
        elif isinstance(value, dict):
            data[key].update(value)
        else:
            data[key] = value
    with pytest.raises(ValidationError):
        Scene.model_validate(data)


def test_missing_track_is_rejected():
    data = Scene().model_dump(mode="json")
    del data["tracks"]["bass"]
    with pytest.raises(ValidationError):
        Scene.model_validate(data)


def test_canonical_json_is_order_independent():
    a = Scene()
    shuffled = dict(reversed(list(a.model_dump(mode="json").items())))
    assert canonical_json(a) == canonical_json(Scene.model_validate(shuffled))


# "single" 故意固定 12 秒一圈（参考视频实测值），不跟随小节；由下方专门的测试覆盖
@pytest.mark.parametrize("name", [p for p in PRESETS if p != "single"])
def test_presets_are_valid_and_use_default_bars(name):
    scene = preset(name, default_bars=2)
    assert isinstance(scene, Scene)
    assert any(t.orbit.speed.bars == 2 for t in scene.tracks.values())


def test_unknown_preset_is_rejected():
    with pytest.raises(ValueError):
        preset("chaos", default_bars=2)


def test_solo_and_mute_gains():
    scene = Scene()
    scene.tracks["drums"].gain_db = -6.0
    g = effective_track_gains(scene)
    assert g["drums"] == pytest.approx(10 ** (-6 / 20)) and g["vocals"] == 1.0
    scene.tracks["bass"].mute = True
    assert effective_track_gains(scene)["bass"] == 0.0
    scene.tracks["vocals"].solo = True
    g = effective_track_gains(scene)
    assert g["vocals"] == 1.0 and g["drums"] == 0.0 and g["other"] == 0.0


def test_orbit_params_converts_units():
    scene = Scene()
    o = scene.tracks["vocals"].orbit
    o.direction = "ccw"
    o.speed.mode, o.speed.bars = "bars", 2
    p = orbit_params(o, bpm_norm=90.0)
    assert p.direction == -1 and p.period_s == pytest.approx(16 / 3)


def test_height_reaches_top_layer():
    scene = Scene()
    scene.tracks["vocals"].orbit.height_deg = 90.0
    assert scene.tracks["vocals"].orbit.height_deg == 90.0
    with pytest.raises(ValidationError):
        scene.tracks["vocals"].orbit.height_deg = 91.0


def test_single_point_preset_imitates_reference_video():
    scene = preset("single", default_bars=4)
    for track in scene.tracks.values():
        assert track.orbit.shape == "circle" and track.width_deg == 0.0
        assert track.orbit.speed.mode == "seconds" and track.orbit.speed.seconds == 12.0
