"""测速与默认转速（SPEC §4.2）。"""

import numpy as np
import pytest

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
