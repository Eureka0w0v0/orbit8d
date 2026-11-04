"""分段处理（SPEC §5.7）：每个分段核心不管怎么切段，结果都与整段一次处理一致（只差浮点舍入）。

参考实现直接用 scipy / pyloudnorm 对整段数组计算，就是改成分段之前的算法。
"""

import numpy as np
import pyloudnorm as pyln
import pytest
from scipy.ndimage import minimum_filter1d, uniform_filter1d
from scipy.signal import butter, firwin, oaconvolve, resample_poly, sosfiltfilt, stft, welch

from orbit8d.engine.chunks import (
    CHUNK,
    ArraySignal,
    BlockMax,
    Framer,
    SegmentSums,
    StreamConvolver,
    WelchMean,
    chunk_ranges,
    place,
)
from orbit8d.engine.master import (
    CEILING_DBTP,
    EPS,
    LIMITER_WINDOW_S,
    OVERSAMPLE,
    LoudnessMeter,
    apply_eq,
    eq_window,
    integrated_loudness,
    limited,
    measure,
    true_peak,
)
from orbit8d.engine.pipeline import CALIBRATION_KEYS, CROSSOVER_HZ, Renderer, SceneRender, Sources, prepare
from orbit8d.engine.render import BLOCK
from orbit8d.engine.structure import (
    ENVELOPE_FLOOR_DB,
    BarPower,
    LoudnessEnvelope,
    MixFeatures,
    complete_bars,
    frame_bands,
)
from orbit8d.engine.tempo import DETREND_FRAMES, HOP, LOG_COMPRESS, NFFT, OnsetEnvelope
from tests.make_golden import render_grid, render_scene
from tests.test_pipeline import fake_stems

SR = 44100


def pieces(n: int, rng: np.random.Generator, longest: int) -> list[tuple[int, int]]:
    """把 [0, n) 随机切成长度 1..longest 的相邻片段。"""
    out, s = [], 0
    while s < n:
        e = min(n, s + int(rng.integers(1, longest + 1)))
        out.append((s, e))
        s = e
    return out


def test_chunk_ranges_cover_everything_once():
    assert chunk_ranges(10, 3) == [(0, 3), (3, 6), (6, 9), (9, 10)]
    assert chunk_ranges(0) == [] and chunk_ranges(5, 5) == [(0, 5)]
    assert CHUNK % BLOCK == 0 and CHUNK % int(LIMITER_WINDOW_S * SR) == 0
    with pytest.raises(ValueError):
        chunk_ranges(10, 0)


def test_array_signal_windows_carry_context():
    x = np.arange(20.0)
    windows = list(ArraySignal(x).windows(margin=3, size=8))
    assert [(w.start, w.end, w.before) for w in windows] == [(0, 8, 0), (8, 16, 3), (16, 20, 3)]
    assert np.array_equal(windows[1].data[:, 0], x[5:19]) and np.array_equal(windows[1].body[:, 0], x[8:16])


@pytest.mark.parametrize("longest", [1, 37, 400, 5000])
def test_stream_convolver_matches_full_convolution(longest):
    rng = np.random.default_rng(longest)
    x = rng.standard_normal(3000)
    x[1000:1600] = 0.0  # 中间整段静音：跳过 FFT 也要把尾巴接上
    kernels = rng.standard_normal((2, 257))
    conv = StreamConvolver(kernels)
    got = np.concatenate([conv.process(x[s:e]) for s, e in pieces(len(x), rng, longest)] + [conv.flush()])
    ref = np.stack([oaconvolve(x, h) for h in kernels], axis=1)
    assert np.abs(got - ref).max() < 1e-12

    stereo = rng.standard_normal((3000, 2))
    conv = StreamConvolver(kernels)
    got = np.concatenate([conv.process(stereo[s:e]) for s, e in pieces(3000, rng, longest)])
    ref = np.stack([oaconvolve(stereo[:, k], kernels[k])[:3000] for k in (0, 1)], axis=1)
    assert np.abs(got - ref).max() < 1e-12


def test_framer_gives_the_same_frames_as_the_whole_signal():
    rng = np.random.default_rng(1)
    x = rng.standard_normal(1000)
    framer, frames = Framer(64, 24), []
    for s, e in pieces(len(x), rng, 90):
        span = framer.push(x[s:e])
        if span is not None:
            frames += [span[k * 24 : k * 24 + 64] for k in range(framer.count(len(span)))]
    expected = [x[k * 24 : k * 24 + 64] for k in range(framer.count(len(x)))]
    assert len(frames) == len(expected) and all(
        np.array_equal(a, b) for a, b in zip(frames, expected, strict=True)
    )


@pytest.mark.parametrize("n", [5000, 8192, 70_000])
def test_welch_mean_matches_scipy(n):
    rng = np.random.default_rng(n)
    x = rng.standard_normal((2, n)) * np.array([[1.0], [0.3]])
    acc = WelchMean(n, SR)
    for s, e in pieces(n, rng, 20_000):
        acc.push(x[:, s:e])
    freqs, got = acc.result()
    ref_freqs, ref = welch(x, SR, nperseg=min(8192, n), axis=-1)
    assert np.array_equal(freqs, ref_freqs) and np.allclose(got, ref, rtol=1e-10, atol=0)


def test_segment_sums_and_block_max():
    rng = np.random.default_rng(2)
    v = rng.standard_normal((500, 2))
    bounds = np.unique(np.concatenate([[0, 500], rng.integers(1, 500, 30)]))
    sums = SegmentSums(bounds, channels=2)
    for s, e in pieces(500, rng, 60):
        sums.push(v[s:e])
    expected = np.array([v[a:b].sum(axis=0) for a, b in zip(bounds, bounds[1:], strict=False)])
    assert np.allclose(sums.sums, expected, rtol=1e-12)
    assert np.allclose(sums.between(int(bounds[2]), int(bounds[7])), v[bounds[2] : bounds[7]].sum(axis=0))

    peaks, w = rng.random(1000), 37
    blocks = BlockMax(w)
    for s, e in pieces(1000, rng, 80):
        blocks.push(peaks[s:e])
    assert np.array_equal(blocks.result(), peaks[: 1000 // w * w].reshape(-1, w).max(axis=1))


def test_place_clips_at_both_ends():
    out = np.zeros(5)
    place(out, -2, np.arange(1.0, 5.0))
    place(out, 4, np.array([9.0, 9.0]))
    assert out.tolist() == [3.0, 4.0, 0.0, 0.0, 9.0]


def gated_song(rng: np.random.Generator) -> np.ndarray:
    """有响有轻、有一段完全静音（门限要起作用）。"""
    levels = np.repeat([0.3, 0.01, 0.0, 0.5, 0.002], SR)
    return rng.standard_normal((len(levels), 2)) * levels[:, None]


@pytest.mark.parametrize("longest", [3000, 100_000])
def test_loudness_meter_matches_pyloudnorm(longest):
    rng = np.random.default_rng(longest)
    x = gated_song(rng)
    meter = LoudnessMeter(len(x), 2, SR)
    for s, e in pieces(len(x), rng, longest):
        meter.push(x[s:e])
    assert meter.result() == pytest.approx(pyln.Meter(SR).integrated_loudness(x), abs=1e-9)
    mono = x[: SR * 3 + 777, 0]
    assert integrated_loudness(mono, SR) == pytest.approx(pyln.Meter(SR).integrated_loudness(mono), abs=1e-9)


def test_loudness_is_minus_inf_when_unmeasurable():
    assert integrated_loudness(np.zeros((SR, 2)), SR) == float("-inf")
    assert integrated_loudness(np.ones((SR // 4, 2)) * 0.5, SR) == float("-inf")  # 比 400 ms 门限块还短


def reference_true_peak(x: np.ndarray) -> np.ndarray:
    up = resample_poly(x, OVERSAMPLE, 1, axis=0)[: OVERSAMPLE * len(x)]
    return np.abs(up).max(axis=1).reshape(len(x), OVERSAMPLE).max(axis=1)


def reference_limiter(x: np.ndarray) -> np.ndarray:
    gain = np.minimum(1.0, 10 ** (CEILING_DBTP / 20) / np.maximum(reference_true_peak(x), EPS))
    w = int(LIMITER_WINDOW_S * SR)
    gain = uniform_filter1d(minimum_filter1d(gain, 2 * w + 1, mode="nearest"), w, mode="nearest")
    return x * gain[:, None]


def peaky(rng: np.random.Generator, seconds: float) -> np.ndarray:
    x = rng.standard_normal((int(seconds * SR), 2)) * 0.2
    x[:: SR // 3] *= 8.0  # 稀疏的尖峰：限幅器要动作
    return x


def test_true_peak_in_blocks_equals_whole_signal():
    x = peaky(np.random.default_rng(3), 4.0)
    assert np.array_equal(true_peak(x), reference_true_peak(x))


@pytest.mark.parametrize("size", [9261, 50_000, 200_000])
def test_measure_and_limiter_are_independent_of_chunk_size(size):
    rng = np.random.default_rng(size)
    x = peaky(rng, 3.0)
    n, w = len(x), int(LIMITER_WINDOW_S * SR)
    stats = measure(lambda a, b: x[a:b], n, SR, size=size)
    assert np.array_equal(stats.block_peaks, reference_true_peak(x)[: n // w * w].reshape(-1, w).max(axis=1))
    assert stats.loudness == pytest.approx(pyln.Meter(SR).integrated_loudness(x), abs=1e-9)
    out = np.concatenate([y for y, _ in limited(lambda a, b: x[a:b], n, SR, gain=1.5, size=size)])
    ref = reference_limiter(x * 1.5)
    assert np.abs(out - ref).max() < 1e-10  # 整段参考的 30 ms 滑动平均本身就有约 1e-12 的累积舍入误差
    assert 20 * np.log10(true_peak(out).max()) <= CEILING_DBTP + 0.05


def test_eq_window_matches_whole_signal_eq():
    rng = np.random.default_rng(4)
    x = rng.standard_normal((20_000, 2)).astype(np.float32)
    fir = firwin(1025, 0.3)
    ref = apply_eq(x.astype(np.float64), fir)
    for a, b in [(0, 700), (700, 20_000), (19_990, 20_000), (5000, 5001), (0, 20_000)]:
        assert np.abs(eq_window(x, fir, a, b) - ref[a:b]).max() < 1e-12


def reference_onsets(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    _, _, z = stft(x, SR, nperseg=NFFT, noverlap=NFFT - HOP, boundary=None, padded=False)
    flux = np.maximum(0, np.diff(np.log1p(LOG_COMPRESS * np.abs(z)), axis=1)).sum(0)
    flux = np.maximum(0, flux - uniform_filter1d(flux, DETREND_FRAMES))
    return flux, (np.arange(len(flux)) + 1) * HOP / SR + NFFT / (2 * SR)


def test_onset_envelope_matches_whole_signal_stft():
    rng = np.random.default_rng(5)
    x = rng.standard_normal(SR * 3) * (np.arange(SR * 3) % 11025 < 800)
    onsets = OnsetEnvelope(SR)
    for s, e in pieces(len(x), rng, 70_000):
        onsets.push(x[s:e])
    flux, t = onsets.result()
    ref_flux, ref_t = reference_onsets(x)
    assert np.array_equal(t, ref_t) and np.allclose(flux, ref_flux, rtol=1e-9, atol=1e-9)


def test_structure_statistics_match_whole_signal():
    rng = np.random.default_rng(6)
    sr, n = 22050, 22050 * 20
    mono = rng.standard_normal(n) * np.repeat(rng.random(20), sr)
    full = complete_bars(n / sr, 120.0, 0.3)
    features, bars, envelope = MixFeatures(sr), BarPower(full, 2.0, sr, n), LoudnessEnvelope(n, sr)
    for s, e in pieces(n, rng, 30_000):
        features.push(mono[s:e])
        bars.push(mono[s:e])
        envelope.push(np.stack([mono[s:e], mono[s:e]], axis=1))
    bands, centers, mean_power = features.result()
    ref_bands, ref_centers = frame_bands(mono, sr)
    assert np.array_equal(centers, ref_centers) and np.allclose(bands, ref_bands, rtol=1e-6)
    assert mean_power == pytest.approx(np.mean(mono**2), rel=1e-12)
    cum = np.concatenate([[0.0], np.cumsum(mono**2)])
    a, b = np.round(full * sr).astype(int), np.minimum(np.round((full + 2.0) * sr).astype(int), n)
    assert np.allclose(bars.result(), (cum[b] - cum[a]) / (b - a), rtol=1e-9)
    hop = int(round(0.25 * sr))
    frames = np.pad(2 * mono**2, (0, -n % hop)).reshape(-1, hop).mean(axis=1)
    ref_env = np.maximum(10 * np.log10(np.maximum(frames, 1e-12) / frames.max()), ENVELOPE_FLOOR_DB)
    assert envelope.result() == [round(float(v), 1) for v in ref_env]


@pytest.mark.parametrize("size", [10_000, 44_100])
def test_prepare_is_independent_of_chunk_size(size):
    """分频在段与段之间补 0.5 秒上下文（比段还长也行），结果与整段一次处理一致。"""
    orig, stems = fake_stems(seconds=4.0)
    signals = ArraySignal(orig), {name: ArraySignal(x) for name, x in stems.items()}
    whole, split = prepare(*signals, SR, size=len(orig)), prepare(*signals, SR, size=size)
    assert np.abs(whole.sources.data - split.sources.data).max() < 1e-6
    for key, value in whole.energy.items():
        assert split.energy[key] == pytest.approx(value, rel=1e-9)
    assert split.tempo.bpm == whole.tempo.bpm and split.orig_lufs == pytest.approx(whole.orig_lufs, abs=1e-9)
    assert np.allclose(split.orig_power, whole.orig_power, rtol=1e-9)
    assert split.envelope_db == whole.envelope_db and split.peak == whole.peak
    for key, value in whole.spectra.items():
        assert np.allclose(split.spectra[key], value, rtol=1e-9)
    low = sosfiltfilt(butter(2, CROSSOVER_HZ, fs=SR, output="sos"), stems["bass"], axis=0)
    assert np.abs(split.sources.sub("bass") - low.mean(axis=1)).max() < 1e-6


@pytest.fixture(scope="module")
def small_renderer() -> Renderer:
    return Renderer(render_grid(np.random.default_rng(8)))


def scene_render_outputs(renderer: Renderer, src: Sources, sizes: list[int]):
    cal = dict.fromkeys(CALIBRATION_KEYS, 0.8)
    render = SceneRender(renderer, render_scene(), 97.5, 0.05, cal, src.n)
    tracks, subs, sends, pos = {}, [], [], 0
    for size in [*sizes, src.n]:
        e = min(src.n, pos + size)
        if e <= pos:
            break
        t, sub, send = render.process(src.data[:, pos:e].astype(np.float64))
        for name, y in t.items():
            tracks.setdefault(name, []).append(y)
        subs.append(sub)
        sends.append(send)
        pos = e
    return {k: np.concatenate(v) for k, v in tracks.items()}, np.concatenate(subs), np.concatenate(sends)


def test_scene_render_is_independent_of_chunk_size(small_renderer):
    """渲染核心逐段接续：HRTF 卷积尾巴、背后压暗滤波器、超低频卷积、段落过渡与事件都跨段连续。"""
    rng = np.random.default_rng(9)
    src = Sources((rng.standard_normal((9, 9000)) * 0.1).astype(np.float32), SR)
    whole = scene_render_outputs(small_renderer, src, [src.n])
    split = scene_render_outputs(small_renderer, src, [32, 96, 4096, 1024, 1600])
    for name, y in whole[0].items():
        assert np.abs(split[0][name] - y).max() < 1e-12
    assert np.abs(split[1] - whole[1]).max() < 1e-12 and np.abs(split[2] - whole[2]).max() < 1e-12
    with pytest.raises(ValueError):
        scene_render_outputs(small_renderer, src, [33, 64])
