"""段落识别与自动编排（SPEC §13.4、§13.5）：合成一首结构已知的歌，分界与标签都要对上。"""

import numpy as np
import pytest
from scipy.signal import butter, sosfilt

from orbit8d.engine.choreo import AMBIENT_HEIGHT_DEG, choreograph
from orbit8d.engine.scene import HOLD_RAMP_S, Scene
from orbit8d.engine.structure import (
    BRIDGE,
    CHORUS,
    ENVELOPE_FLOOR_DB,
    INTRO,
    OUTRO,
    VERSE,
    WHOLE,
    SectionInfo,
    bar_starts,
    detect_sections,
    loudness_envelope,
)

SR = 22050
BPM = 120.0
BAR = 2.0
T_REF = 0.3
PLAN = [INTRO, VERSE, CHORUS, VERSE, CHORUS, BRIDGE, CHORUS, OUTRO]
BARS_EACH = 8


def band_noise(rng, n, lo, hi):
    return sosfilt(butter(4, [lo, hi], "bandpass", fs=SR, output="sos"), rng.standard_normal(n))


def kicks(n, every_s):
    t = np.arange(n) / SR
    return np.sin(2 * np.pi * 60 * t) * np.exp(-((t % every_s) / 0.08))


def level(x, db):
    return x / np.sqrt(np.mean(x**2)) * 10 ** (db / 20)


def synth_song(seed=0):
    """前奏（低频长音、无人声）→ 主歌 → 副歌 → 主歌 → 副歌 → 桥段（高频、中等响度）→ 副歌 → 尾声。"""
    rng = np.random.default_rng(seed)
    n_bar = int(BAR * SR)
    lead = int(T_REF * SR)
    t = np.arange(n_bar * BARS_EACH) / SR
    pad = sum(np.sin(2 * np.pi * f * t) for f in (110, 165, 220))
    voice = sum(np.sin(2 * np.pi * 220 * k * t) / k for k in range(1, 6))
    inst = {
        INTRO: lambda n: level(pad[:n], -30),
        VERSE: lambda n: level(band_noise(rng, n, 200, 2000) + kicks(n, 0.5), -21),
        CHORUS: lambda n: level(band_noise(rng, n, 100, 8000) + 2 * kicks(n, 0.5), -12),
        BRIDGE: lambda n: level(band_noise(rng, n, 2000, 6000) + 0.3 * kicks(n, 1.0), -18),
        OUTRO: lambda n: level(pad[:n], -31),
    }
    mix, vocals = [inst[INTRO](lead)], [np.zeros(lead)]
    for label in PLAN:
        n = n_bar * BARS_EACH
        v = np.zeros(n) if label in (INTRO, OUTRO) else level(voice[:n], -20)
        mix.append(inst[label](n) + v)
        vocals.append(v)
    return np.concatenate(mix), np.concatenate(vocals)


@pytest.fixture(scope="module")
def detected():
    mix, vocals = synth_song()
    return detect_sections(np.stack([mix, mix], axis=1), vocals, SR, BPM, T_REF)


def test_boundaries_land_on_the_true_bar_lines(detected):
    grid = bar_starts(len(synth_song()[0]) / SR, BPM, T_REF)
    truth = [0.0] + [float(grid[k * BARS_EACH]) for k in range(1, len(PLAN))]
    assert [s.start_s for s in detected] == pytest.approx(truth)
    assert [s.bars for s in detected] == [BARS_EACH] * len(PLAN)


def test_labels_follow_energy_and_vocals(detected):
    assert [s.label for s in detected] == PLAN
    energy = {s.label: s.energy_db for s in detected}
    assert energy[CHORUS] > energy[BRIDGE] > energy[VERSE] > energy[INTRO]


def test_short_song_is_one_whole_section():
    n = int(5 * BAR * SR)
    x = np.random.default_rng(1).standard_normal(n) * 0.1
    sections = detect_sections(x, x, SR, BPM, T_REF)
    assert len(sections) == 1 and sections[0].label == WHOLE and sections[0].start_s == 0.0


def test_silence_does_not_crash():
    x = np.zeros(int(40 * BAR * SR))
    sections = detect_sections(x, x, SR, BPM, T_REF)
    assert sections[0].start_s == 0.0 and all(np.isfinite(s.energy_db) for s in sections)


def test_choreography_maps_labels_to_orbits_and_events(detected):
    scene = choreograph(detected, default_bars=4, bpm_norm=BPM)
    assert isinstance(Scene.model_validate(scene.model_dump()), Scene)
    by_label = {}
    for sec in scene.sections:
        by_label.setdefault(sec.label, sec)
    intro, verse, chorus, bridge = (by_label[k] for k in (INTRO, VERSE, CHORUS, BRIDGE))
    assert (
        intro.orbits["vocals"].shape == "spiral" and intro.orbits["vocals"].height_deg == AMBIENT_HEIGHT_DEG
    )
    assert intro.orbits["vocals"].speed.bars == 8 and intro.wet_db == pytest.approx(-8.0)
    assert verse.orbits["vocals"].shape == "circle" and verse.orbits["vocals"].speed.bars == 4
    assert chorus.orbits["vocals"].pitch_deg == 45 and chorus.orbits["other"].yaw_deg == -45
    assert chorus.orbits["vocals"].speed.bars == 2
    assert bridge.orbits["vocals"].pitch_deg == 90 and bridge.wet_db == pytest.approx(-10.0)

    overhead = [e for e in scene.events if e.kind == "overhead"]
    loudest = max((s for s in detected if s.label == CHORUS), key=lambda s: s.energy_db)
    assert len(overhead) == 1 and overhead[0].t_s == pytest.approx(loudest.start_s)
    assert overhead[0].duration_s == pytest.approx(2 * BAR) and set(overhead[0].targets) == {
        "vocals",
        "other",
    }

    holds = [e for e in scene.events if e.kind == "hold"]
    verses = [s for s in detected if s.label == VERSE]
    assert len(holds) == len(verses)
    for e, s in zip(holds, verses, strict=True):
        assert e.targets == ["vocals"] and e.duration_s == pytest.approx(BAR)
        assert e.t_s + HOLD_RAMP_S == pytest.approx(
            s.start_s + 6 * BAR
        )  # 8 小节的主歌，第 6 小节线处完全停住


def test_choreography_of_single_section_is_classic():
    scene = choreograph([SectionInfo(0.0, 3, WHOLE, -20.0)], default_bars=2, bpm_norm=BPM)
    assert len(scene.sections) == 1 and scene.events == []
    assert all(o.shape == "circle" and o.speed.bars == 2 for o in scene.sections[0].orbits.values())


def test_loudness_envelope_is_relative_to_the_loudest_moment():
    sr = 8000  # 0.25 秒正好 2000 个采样
    x = np.concatenate([np.full(sr, 0.5), np.full(sr, 0.05), np.zeros(sr // 2)])
    env = loudness_envelope(np.stack([x, x], axis=1), sr, hop_s=0.25)
    assert len(env) == 10
    assert env[:4] == [0.0] * 4 and all(v == pytest.approx(-20.0, abs=0.1) for v in env[4:8])
    assert env[8:] == [ENVELOPE_FLOOR_DB] * 2  # 静音压到下限
    assert loudness_envelope(np.zeros(sr), sr) == [ENVELOPE_FLOOR_DB] * 4
    assert len(loudness_envelope(np.ones(sr + 1), sr)) == 5  # 最后不满一格的也算一格
