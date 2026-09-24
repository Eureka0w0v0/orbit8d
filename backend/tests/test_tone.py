"""按场景的音色补偿（SPEC §13.6）：锚点、模型预测的染色、用户音量不被拉回。"""

import numpy as np
import pytest
from scipy.signal import butter, sosfilt

from orbit8d.engine.pipeline import Renderer, analyze
from orbit8d.engine.scene import preset
from orbit8d.engine.tone import BANDS_HZ, band_power, match_gain_db, scene_eq_db
from tests.test_pipeline import SOFA, SR, fake_stems

pytestmark = [pytest.mark.slow, pytest.mark.skipif(not SOFA.exists(), reason="需要先运行 make assets")]
MID = (BANDS_HZ >= 200) & (BANDS_HZ <= 12000)


@pytest.fixture(scope="module")
def setup():
    from orbit8d.engine.hrtf import from_sofa

    renderer = Renderer(from_sofa(SOFA, SR))
    orig, stems = fake_stems(seconds=12.0)
    src, analysis = analyze(orig, stems, SR, renderer)
    return renderer, src, analysis


def eq_db(renderer, scene, analysis):
    ref = preset("classic", analysis.default_bars)
    brirs = {name: renderer.brir(name) for name in {scene.room.name, ref.room.name}}
    return scene_eq_db(renderer.tone, scene, ref, analysis, brirs)


def raw_mix(renderer, src, scene, analysis):
    tracks = renderer.render_tracks(src, scene, analysis.bpm_norm, analysis.t_ref, analysis.calibration)
    send = renderer.send_signal(src, scene, analysis.calibration, analysis.bpm_norm)
    return sum(tracks.values()) + renderer.reverb(send, scene.room)


def test_match_gain_recovers_a_known_tilt():
    x = np.random.default_rng(0).standard_normal((SR * 4, 2))
    dull = x - 0.6 * sosfilt(butter(1, 3000, "highpass", fs=SR, output="sos"), x, axis=0)
    d = match_gain_db(x, dull, SR)
    assert np.abs(np.median(d)) < 1e-9 and np.abs(d).max() <= 6.0
    expected = 10 * np.log10(band_power(x, SR) / band_power(dull, SR))
    expected -= np.median(expected)
    assert np.abs(d - np.clip(expected, -6, 6)).max() < 1e-9
    assert d[BANDS_HZ >= 8000].min() > 5.0  # 高频被压暗了，就要往回提


def test_reference_scene_gets_exactly_the_anchor(setup):
    renderer, _, analysis = setup
    assert np.array_equal(
        eq_db(renderer, preset("classic", analysis.default_bars), analysis), analysis.match_eq_db
    )


@pytest.mark.parametrize("name", ["layers", "cross", "diagonal"])
def test_model_predicts_how_other_scenes_color_the_sound(setup, name):
    """模型预测的“经典 → 其他场景”的频谱变化，与真实渲染测出来的一致（中高频 ±1.5 dB 内）。"""
    renderer, src, analysis = setup
    ref, scene = preset("classic", analysis.default_bars), preset(name, analysis.default_bars)
    measured = 10 * np.log10(
        band_power(raw_mix(renderer, src, ref, analysis), SR)
        / band_power(raw_mix(renderer, src, scene, analysis), SR)
    )
    predicted = eq_db(renderer, scene, analysis) - np.asarray(analysis.match_eq_db)
    diff = (measured - np.median(measured[MID])) - (predicted - np.median(predicted[MID]))
    assert np.abs(diff[MID]).max() < 1.5


def test_user_mix_choices_are_not_equalized_back(setup):
    """静音鼓、调低人声不是“音色被空间化染了”，EQ 不应该跟着大改。"""
    renderer, _, analysis = setup
    scene = preset("classic", analysis.default_bars)
    scene.mix["drums"].mute = True
    scene.mix["vocals"].gain_db = -6.0
    delta = eq_db(renderer, scene, analysis) - np.asarray(analysis.match_eq_db)
    assert np.abs(delta[MID]).max() < 0.75
