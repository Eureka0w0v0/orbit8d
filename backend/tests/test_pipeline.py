"""整曲管线（SPEC §5）：声源准备、超低频居中、静音/独奏、校准、导出母带。"""

import numpy as np
import pytest

from orbit8d.assets import HRTF_FILE
from orbit8d.config import load_settings
from orbit8d.engine.hrtf import from_sofa
from orbit8d.engine.master import CEILING_DBTP, true_peak
from orbit8d.engine.pipeline import Analysis, Renderer, analyze, prepare_sources
from orbit8d.engine.scene import Scene, preset

SR = 44100
SOFA = load_settings().assets_dir / HRTF_FILE
pytestmark = [pytest.mark.slow, pytest.mark.skipif(not SOFA.exists(), reason="需要先运行 make assets")]


def fake_stems(seconds: float = 6.0, seed: int = 0) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """人声=中置带通噪声，鼓=左右不同的噪声脉冲，贝斯=50 Hz + 泛音，其他=宽立体声噪声。"""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    voc = rng.standard_normal(n) * 0.1
    vocals = np.stack([voc, voc], axis=1)
    pulses = (np.sin(2 * np.pi * t * 1.5) > 0.95).astype(float)
    drums = np.stack([pulses * rng.standard_normal(n), pulses * rng.standard_normal(n)], axis=1) * 0.2
    bass_mono = 0.3 * np.sin(2 * np.pi * 50 * t) + 0.1 * np.sin(2 * np.pi * 300 * t)
    bass = np.stack([bass_mono, bass_mono], axis=1)
    other = rng.standard_normal((n, 2)) * 0.05
    stems = {"vocals": vocals, "drums": drums, "bass": bass, "other": other}
    orig = sum(stems.values()) + rng.standard_normal((n, 2)) * 1e-3  # 模拟分离残差
    return orig, stems


@pytest.fixture(scope="module")
def renderer() -> Renderer:
    return Renderer(from_sofa(SOFA, SR))


@pytest.fixture(scope="module")
def analyzed(renderer):
    orig, stems = fake_stems()
    src, analysis = analyze(orig, stems, SR, renderer)
    return orig, stems, src, analysis


def band_energy(x: np.ndarray, lo: float, hi: float) -> float:
    spec = np.abs(np.fft.rfft(x, axis=0)) ** 2
    f = np.fft.rfftfreq(len(x), 1 / SR)
    return float(spec[(f >= lo) & (f < hi)].sum())


def test_prepare_sources_is_complementary_and_keeps_residual():
    orig, stems = fake_stems()
    src = prepare_sources(orig, stems, SR)
    assert np.allclose(src.sub["bass"] + src.hi["bass"], stems["bass"].mean(axis=1), atol=1e-12)
    assert np.allclose(
        src.sub["drums"] + src.hi["drums"].mean(axis=1), stems["drums"].mean(axis=1), atol=1e-12
    )
    other_with_residual = stems["other"] + (orig - sum(stems.values()))
    assert np.allclose(
        src.sub["other"] + src.hi["other"].mean(axis=1), other_with_residual.mean(axis=1), atol=1e-12
    )
    assert src.hi["vocals"].ndim == 1 and src.hi["drums"].shape == (len(orig), 2)


def test_analysis_roundtrips_through_json(analyzed):
    *_, analysis = analyzed
    back = Analysis.from_json(analysis.to_json())
    assert back == analysis
    assert set(analysis.calibration) == {"vocals", "drums", "bass", "other", "sub"}
    assert analysis.default_bars in (1, 2, 4, 8) and analysis.preview_gain > 0


def test_calibration_restores_each_track_energy(renderer, analyzed):
    orig, stems, src, analysis = analyzed
    ref = preset("classic", analysis.default_bars)
    tracks = renderer.render_tracks(src, ref, analysis.bpm_norm, analysis.t_ref, analysis.calibration)
    for name in ("vocals", "drums", "bass", "other"):
        assert 10 * np.log10(np.sum(tracks[name] ** 2) / src.energy_hi[name]) == pytest.approx(0.0, abs=0.1)


def test_sub_bass_stays_centered_while_everything_rotates(renderer, analyzed):
    *_, src, analysis = analyzed
    scene = preset("classic", analysis.default_bars)
    scene.sections[0].orbits["bass"].speed.mode = "seconds"
    scene.sections[0].orbits["bass"].speed.seconds = 2.0
    mix = renderer.render_mix(src, scene, analysis)
    low = np.fft.irfft(
        np.fft.rfft(mix, axis=0) * (np.fft.rfftfreq(len(mix), 1 / SR) < 100)[:, None], len(mix), axis=0
    )
    assert np.corrcoef(low[:, 0], low[:, 1])[0, 1] > 0.999


def test_mute_removes_track_including_its_sub_bass(renderer, analyzed):
    *_, src, analysis = analyzed
    scene = Scene()
    scene.sections[0].wet_db = -24.0
    full = renderer.render_mix(src, scene, analysis)
    scene.mix["bass"].mute = True
    muted = renderer.render_mix(src, scene, analysis)
    assert band_energy(muted, 40, 60) < band_energy(full, 40, 60) * 1e-3  # 若超低频没跟着静音，只会降约 15 dB


def test_solo_silences_other_tracks(renderer, analyzed):
    *_, src, analysis = analyzed
    scene = Scene()
    scene.mix["vocals"].solo = True
    tracks = renderer.render_tracks(src, scene, analysis.bpm_norm, analysis.t_ref, analysis.calibration)
    assert np.sum(tracks["drums"] ** 2) == 0 and np.sum(tracks["sub"] ** 2) == 0
    assert np.sum(tracks["vocals"] ** 2) > 0


def test_closer_orbit_is_louder(renderer, analyzed):
    *_, src, analysis = analyzed
    near, far = Scene(), Scene()
    near.sections[0].orbits["vocals"].radius_m, far.sections[0].orbits["vocals"].radius_m = 0.5, 1.0
    e = [
        np.sum(
            renderer.render_tracks(src, s, analysis.bpm_norm, analysis.t_ref, analysis.calibration)["vocals"]
            ** 2
        )
        for s in (near, far)
    ]
    assert 10 * np.log10(e[0] / e[1]) == pytest.approx(6.02, abs=0.05)


def test_export_is_mastered_and_same_length(renderer, analyzed):
    orig, _, src, analysis = analyzed
    out = renderer.export(src, preset("dual", analysis.default_bars), analysis)
    assert out.shape == orig.shape and np.isfinite(out).all()
    assert 20 * np.log10(true_peak(out).max()) <= CEILING_DBTP + 0.05
