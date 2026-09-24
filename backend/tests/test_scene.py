"""场景 v2（SPEC §4.4、§13）：全曲混音 + 分段轨道 + 事件；白名单校验；v1 自动升级；预设。"""

import copy

import pytest
from pydantic import ValidationError

from orbit8d.engine.scene import (
    PRESETS,
    Scene,
    canonical_json,
    effective_track_gains,
    orbit_params,
    preset,
)


def dump() -> dict:
    return Scene().model_dump(mode="json")


def test_default_scene_is_valid_and_complete():
    s = Scene()
    assert s.version == 2 and set(s.mix) == {"vocals", "drums", "bass", "other"}
    assert s.mix["drums"].width_deg == 40 and s.mix["vocals"].width_deg == 0
    assert len(s.sections) == 1 and s.sections[0].start_s == 0 and s.sections[0].label == "全曲"
    assert set(s.sections[0].orbits) == set(s.mix) and s.sections[0].wet_db == -12
    assert s.events == [] and s.room.name == "hall" and s.rear_darken_db == 6


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(rear_darken_db=13),
        lambda d: d["room"].update(name="stadium"),
        lambda d: d["mix"]["vocals"].update(gain_db=30),
        lambda d: d["mix"]["vocals"].update(hack=1),
        lambda d: d["mix"].update(piano={}),
        lambda d: d["sections"][0]["orbits"]["vocals"].update(radius_m=0.1),
        lambda d: d["sections"][0]["orbits"]["vocals"].update(shape="square"),
        lambda d: d["sections"][0]["orbits"]["vocals"]["speed"].update(mode="bars", bars=3),
        lambda d: d["sections"][0]["orbits"]["vocals"]["speed"].update(mode="seconds", seconds=1),
        lambda d: d["sections"][0]["orbits"]["vocals"].update(pitch_deg=float("nan")),
        lambda d: d["sections"][0].update(wet_db=3),
        lambda d: d["sections"][0].update(label="超" * 17),
        lambda d: d["sections"][0].update(start_s=1.0),
        lambda d: d.update(version=3),
    ],
)
def test_out_of_range_or_unknown_fields_are_rejected(mutate):
    d = dump()
    mutate(d)
    with pytest.raises(ValidationError):
        Scene.model_validate(d)


def test_missing_track_is_rejected():
    d = dump()
    del d["mix"]["bass"]
    with pytest.raises(ValidationError):
        Scene.model_validate(d)
    d = dump()
    del d["sections"][0]["orbits"]["bass"]
    with pytest.raises(ValidationError):
        Scene.model_validate(d)


def test_sections_must_start_at_zero_and_increase():
    d = dump()
    second = copy.deepcopy(d["sections"][0]) | {"start_s": 30.0, "label": "副歌"}
    third = copy.deepcopy(d["sections"][0]) | {"start_s": 20.0, "label": "桥段"}
    d["sections"] = [d["sections"][0], second]
    assert len(Scene.model_validate(d).sections) == 2
    d["sections"] = [d["sections"][0], second, third]
    with pytest.raises(ValidationError):
        Scene.model_validate(d)
    d["sections"] = [d["sections"][0]] * 17
    with pytest.raises(ValidationError):
        Scene.model_validate(d)


@pytest.mark.parametrize(
    "event",
    [
        {"t_s": 10, "kind": "spin", "duration_s": 2, "targets": ["vocals"]},
        {"t_s": 10, "kind": "hold", "duration_s": 0.1, "targets": ["vocals"]},
        {"t_s": 10, "kind": "hold", "duration_s": 2, "targets": []},
        {"t_s": 10, "kind": "hold", "duration_s": 2, "targets": ["vocals", "vocals"]},
        {"t_s": -1, "kind": "hold", "duration_s": 2, "targets": ["vocals"]},
    ],
)
def test_invalid_events_are_rejected(event):
    d = dump()
    d["events"] = [event]
    with pytest.raises(ValidationError):
        Scene.model_validate(d)


def test_same_kind_events_may_not_overlap_on_one_track():
    d = dump()
    hold = {"t_s": 10.0, "kind": "hold", "duration_s": 2.0, "targets": ["vocals"]}
    d["events"] = [hold, hold | {"t_s": 11.0}]
    with pytest.raises(ValidationError):
        Scene.model_validate(d)
    d["events"] = [hold, hold | {"t_s": 11.0, "targets": ["drums"]}, hold | {"t_s": 11.0, "kind": "overhead"}]
    assert len(Scene.model_validate(d).events) == 3  # 不同音轨或不同类型可以重叠
    d["events"] = [hold, hold | {"t_s": 20.0}]
    assert len(Scene.model_validate(d).events) == 2


def test_v1_scene_is_upgraded():
    v1 = {
        "version": 1,
        "tracks": {
            name: {
                "orbit": {"shape": "circle", "pitch_deg": 30.0} if name == "vocals" else {},
                "gain_db": -3.0 if name == "drums" else 0.0,
                "width_deg": 10.0,
                "reverb_send": 0.5,
                "mute": name == "bass",
                "solo": False,
            }
            for name in ("vocals", "drums", "bass", "other")
        },
        "room": {"name": "church", "wet_db": -9.0},
        "rear_darken_db": 3.0,
    }
    s = Scene.model_validate(v1)
    assert s.version == 2 and s.room.name == "church" and s.rear_darken_db == 3.0
    assert s.mix["drums"].gain_db == -3.0 and s.mix["bass"].mute and s.mix["vocals"].width_deg == 10.0
    assert len(s.sections) == 1 and s.sections[0].wet_db == -9.0
    assert s.sections[0].orbits["vocals"].pitch_deg == 30.0


def test_canonical_json_is_order_independent():
    a = Scene()
    shuffled = dict(reversed(list(a.model_dump(mode="json").items())))
    assert canonical_json(a) == canonical_json(Scene.model_validate(shuffled))


@pytest.mark.parametrize("name", PRESETS)
def test_presets_are_single_section_and_valid(name):
    scene = preset(name, default_bars=2)
    assert isinstance(scene, Scene) and len(scene.sections) == 1
    if name != "single":  # 单点环绕固定 12 秒一圈，其余预设跟随小节
        assert any(o.speed.bars == 2 for o in scene.sections[0].orbits.values())


def test_unknown_preset_is_rejected():
    with pytest.raises(ValueError):
        preset("chaos", default_bars=2)


def test_solo_and_mute_gains():
    scene = Scene()
    scene.mix["drums"].gain_db = -6.0
    g = effective_track_gains(scene)
    assert g["drums"] == pytest.approx(10 ** (-6 / 20)) and g["vocals"] == 1.0
    scene.mix["bass"].mute = True
    assert effective_track_gains(scene)["bass"] == 0.0
    scene.mix["vocals"].solo = True
    g = effective_track_gains(scene)
    assert g["vocals"] == 1.0 and g["drums"] == 0.0 and g["other"] == 0.0


def test_orbit_params_converts_units():
    o = Scene().sections[0].orbits["vocals"]
    o.direction = "ccw"
    o.speed.mode, o.speed.bars = "bars", 2
    p = orbit_params(o, bpm_norm=90.0)
    assert p.direction == -1 and p.period_s == pytest.approx(16 / 3)


def test_height_reaches_top_layer():
    o = Scene().sections[0].orbits["vocals"]
    o.height_deg = 90.0
    with pytest.raises(ValidationError):
        o.height_deg = 91.0


def test_single_point_preset_imitates_reference_video():
    scene = preset("single", default_bars=4)
    assert scene.sections[0].wet_db == -10.0 and all(m.width_deg == 0.0 for m in scene.mix.values())
    for o in scene.sections[0].orbits.values():
        assert o.shape == "circle" and o.speed.mode == "seconds" and o.speed.seconds == 12.0


def test_layers_diagonal_cross_presets():
    o = preset("layers", default_bars=4).sections[0].orbits
    assert (o["vocals"].height_deg, o["other"].height_deg, o["drums"].height_deg) == (75.0, 35.0, 0.0)
    o = preset("diagonal", default_bars=4).sections[0].orbits
    assert (o["vocals"].pitch_deg, o["vocals"].yaw_deg, o["other"].yaw_deg) == (45.0, 45.0, -45.0)
    o = preset("cross", default_bars=4).sections[0].orbits
    assert (o["vocals"].pitch_deg, o["other"].pitch_deg, o["other"].yaw_deg) == (90.0, 90.0, 90.0)
