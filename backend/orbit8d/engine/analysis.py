"""分析（SPEC §5.5、§5.7、§13.2）：原曲 + 分轨 → 9 路声源，以及测速、校准增益、音色锚点、试听增益、段落。

全部分段处理（SPEC §5.7）：整曲只保留 float32 的 9 路声源与校准用的混音，其余按约 9.6 秒一段算完即丢。
"""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass

import numpy as np

from orbit8d.engine.chunks import CHUNK, Signal, WelchMean, aligned_windows, chunk_ranges
from orbit8d.engine.master import LoudnessMeter, eq_window, gain_for, measure
from orbit8d.engine.mixdown import REFERENCE_PRESET, Renderer, SceneRender
from orbit8d.engine.orbit import bar_seconds
from orbit8d.engine.scene import Scene, preset
from orbit8d.engine.sources import (
    CALIBRATION_KEYS,
    CROSSOVER_MARGIN_S,
    SEPARATED_TRACKS,
    SOURCE_CHANNELS,
    SPECTRA_KEYS,
    Sources,
    spectra_rows,
    split_window,
)
from orbit8d.engine.structure import (
    ENVELOPE_HOP_S,
    BarPower,
    LoudnessEnvelope,
    MixFeatures,
    SectionInfo,
    StructureInputs,
    complete_bars,
    sections_from,
)
from orbit8d.engine.tempo import OnsetEnvelope, TempoInfo, tempo_from_onsets, turn_plan
from orbit8d.engine.tone import match_from_power, psd_bands, spectra_from_psd

ANALYSIS_VERSION = 3  # 分析结果的内容变了就加一：旧项目启动时自动重新分析（不重新分轨）
ORIGINAL_MATCH_LIMIT_DB = 24.0
EPS = 1e-12


@dataclass(frozen=True)
class Analysis:
    sample_rate: int
    duration_s: float
    bpm: float
    bpm_norm: float
    default_bars: int
    t_ref: float
    calibration: dict[str, float]
    preview_gain: float
    match_eq_db: list[float]  # 音色锚点：经典场景下各 1/3 倍频程的校正量
    spectra: dict[str, list[float]]  # 各路声源的频带功率（按场景预测染色用）
    sections: list[dict]  # 自动识别的段落（structure.SectionInfo）
    original_gain: float  # 原曲试听的增益：让原曲和 8D 试听一样响（A/B 对比只比音色与空间感）
    envelope_db: list[float]  # 原曲每 envelope_hop_s 秒的音量（相对最响处，dB）
    envelope_hop_s: float
    version: int = ANALYSIS_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "Analysis":
        return cls(**json.loads(text))


@dataclass
class Prepared:
    """第一遍的结果：9 路声源与所有与速度无关的统计量。"""

    sources: Sources
    energy: dict[str, float]  # 校准目标：各轨运动部分与全部超低频（"sub"）的原始立体声能量
    tempo: TempoInfo
    orig_power: np.ndarray  # 原曲频带功率（音色锚点的参考）
    orig_lufs: float
    envelope_db: list[float]
    features: tuple[np.ndarray, np.ndarray, float]  # 段落识别：逐帧频带功率、帧中心时刻、平均功率
    spectra: dict[str, list[float]]
    peak: float  # 9 路声源与原曲的最大绝对值（试听文件按它缩放）


class _PrepMeters:
    """第一遍里顺带累计的统计量。"""

    def __init__(self, n: int, sr: int):
        self.sr = sr
        self.onsets = OnsetEnvelope(sr)
        self.orig_welch = WelchMean(n, sr)
        self.source_welch = [WelchMean(n, sr) for _ in SPECTRA_KEYS]  # 逐路累计，临时内存只有一路
        self.loudness = LoudnessMeter(n, 2, sr)
        self.envelope = LoudnessEnvelope(n, sr)
        self.features = MixFeatures(sr)
        self.energy = dict.fromkeys(CALIBRATION_KEYS, 0.0)
        self.peak = 0.0

    def push(self, orig: np.ndarray, drums: np.ndarray, chunk: np.ndarray, energy: dict[str, float]) -> None:
        self.onsets.push(drums.mean(axis=1))
        self.orig_welch.push(orig.T)
        self.loudness.push(orig)
        self.envelope.push(orig)
        self.features.push(orig.mean(axis=1))
        for welch, row in zip(self.source_welch, spectra_rows(chunk), strict=True):
            welch.push(row)
        for key, value in energy.items():
            self.energy[key] += value

    def note_peak(self, *parts: np.ndarray) -> None:
        self.peak = max(self.peak, *(float(np.abs(x).max(initial=0.0)) for x in parts))

    def result(self, sources: Sources) -> Prepared:
        freqs, orig_psd = self.orig_welch.result()
        source_psd = np.stack([welch.result()[1] for welch in self.source_welch])
        return Prepared(
            sources=sources,
            energy=self.energy,
            tempo=tempo_from_onsets(*self.onsets.result(), self.sr),
            orig_power=psd_bands(freqs, orig_psd.sum(axis=0)),
            orig_lufs=self.loudness.result(),
            envelope_db=self.envelope.result(),
            features=self.features.result(),
            spectra=spectra_from_psd(freqs, source_psd, SPECTRA_KEYS),
            peak=self.peak,
        )


def prepare(orig: Signal, stems: Mapping[str, Signal], sr: int, size: int = CHUNK) -> Prepared:
    """第一遍：原曲 + 人声 / 鼓 / 贝斯分轨 → 9 路声源（float32），顺带统计所有与速度无关的量。"""
    data = np.empty((len(SOURCE_CHANNELS), orig.frames), dtype=np.float32)
    meters = _PrepMeters(orig.frames, sr)
    signals = [orig, *(stems[name] for name in SEPARATED_TRACKS)]
    for wo, wv, wd, wb in aligned_windows(signals, int(CROSSOVER_MARGIN_S * sr), size):
        chunk, energy = split_window(wo, wv, wd, wb, sr)
        data[:, wo.start : wo.end] = chunk
        meters.push(wo.body, wd.body, chunk, energy)
        meters.note_peak(
            wo.body, data[:, wo.start : wo.end]
        )  # 按存下来的 float32 取最大值，缩放后不会超过 ±1
    return meters.result(Sources(data, sr))


def calibrate(
    renderer: Renderer, prep: Prepared, bpm_norm: float, t_ref: float, default_bars: int
) -> tuple[dict[str, float], np.ndarray, float, float]:
    """用“经典 8D”预设渲染：第一遍得每轨校准增益 = √(原始能量 / 渲染能量)；第二遍渲染校准后的混音，
    与原曲比长期平均谱得到音色锚点，再算试听总增益与试听响度（LUFS）。"""
    ref, src = preset(REFERENCE_PRESET, default_bars), prep.sources
    rendered = _track_energies(renderer, src, ref, bpm_norm, t_ref)
    cal = {
        key: float(np.sqrt(prep.energy[key] / rendered[key]))
        if rendered[key] > EPS and prep.energy[key] > EPS
        else 1.0
        for key in CALIBRATION_KEYS
    }
    raw, pos = np.empty((src.n, 2), dtype=np.float32), 0
    for chunk in renderer.raw_chunks(src.chunks(), src.n, ref, bpm_norm, t_ref, cal):
        raw[pos : pos + len(chunk)] = chunk
        pos += len(chunk)
    anchor = match_from_power(prep.orig_power, _eq_band_power(raw, renderer.eq, renderer.sr))
    fir = renderer.band_eq(anchor)
    stats = measure(lambda a, b: eq_window(raw, fir, a, b), src.n, src.sr)
    gain = gain_for(stats)
    return cal, anchor, gain, stats.loudness + 20 * np.log10(max(gain, EPS))


def _track_energies(
    renderer: Renderer, src: Sources, scene: Scene, bpm_norm: float, t_ref: float
) -> dict[str, float]:
    render = SceneRender(renderer, scene, bpm_norm, t_ref, dict.fromkeys(CALIBRATION_KEYS, 1.0), src.n)
    energy = dict.fromkeys(CALIBRATION_KEYS, 0.0)
    for chunk in src.chunks():
        tracks, sub, _ = render.process(chunk)
        for name, y in tracks.items():
            energy[name] += float(np.sum(y**2))
        energy["sub"] += float(np.sum(sub**2))
    return energy


def _eq_band_power(x: np.ndarray, fir: np.ndarray, sr: int) -> np.ndarray:
    welch = WelchMean(len(x), sr)
    for s, e in chunk_ranges(len(x)):
        welch.push(eq_window(x, fir, s, e).T)
    freqs, psd = welch.result()
    return psd_bands(freqs, psd.sum(axis=0))


@dataclass(frozen=True)
class Analyzed:
    sources: Sources  # 9 路声源（float32），写成试听文件
    analysis: Analysis
    peak: float  # 9 路声源与原曲的最大绝对值


def find_sections(
    orig: Signal, vocals: Signal, prep: Prepared, bpm_norm: float, t_ref: float, sr: int
) -> list[SectionInfo]:
    """第二遍读原曲与人声：每个完整小节的平均功率 → 段落。"""
    n, bar = orig.frames, bar_seconds(bpm_norm)
    full = complete_bars(n / sr, bpm_norm, t_ref)
    mix_bars, vocal_bars = BarPower(full, bar, sr, n), BarPower(full, bar, sr, n)
    for wo, wv in aligned_windows([orig, vocals]):
        mix_bars.push(wo.body.mean(axis=1))
        vocal_bars.push(wv.body.mean(axis=1))
    bands, centers, mean_power = prep.features
    inputs = StructureInputs(n / sr, bands, centers, mean_power, mix_bars.result(), vocal_bars.result())
    return sections_from(inputs, bpm_norm, t_ref)


def analyze(orig: Signal, stems: Mapping[str, Signal], sr: int, renderer: Renderer) -> Analyzed:
    """原曲 + 人声 / 鼓 / 贝斯分轨（other 由残差得到，不需要）→ 9 路声源与分析结果。"""
    prep = prepare(orig, stems, sr)
    info = prep.tempo
    bars, t_ref = turn_plan(info)
    calibration, anchor, preview_gain, preview_lufs = calibrate(renderer, prep, info.bpm_norm, t_ref, bars)
    sections = find_sections(orig, stems["vocals"], prep, info.bpm_norm, t_ref, sr)
    analysis = Analysis(
        sample_rate=sr,
        duration_s=orig.frames / sr,
        bpm=info.bpm,
        bpm_norm=info.bpm_norm,
        default_bars=bars,
        t_ref=t_ref,
        calibration=calibration,
        preview_gain=preview_gain,
        match_eq_db=[float(v) for v in anchor],
        spectra=prep.spectra,
        sections=[s.to_dict() for s in sections],
        original_gain=loudness_match_gain(preview_lufs, prep.orig_lufs),
        envelope_db=prep.envelope_db,
        envelope_hop_s=ENVELOPE_HOP_S,
    )
    return Analyzed(prep.sources, analysis, prep.peak)


def loudness_match_gain(target_lufs: float, source_lufs: float) -> float:
    """把 source 调到和 target 一样响的线性增益（限幅 ±24 dB；任一边测不出响度时不调）。"""
    if not (np.isfinite(target_lufs) and np.isfinite(source_lufs)):
        return 1.0
    return float(
        10 ** (np.clip(target_lufs - source_lufs, -ORIGINAL_MATCH_LIMIT_DB, ORIGINAL_MATCH_LIMIT_DB) / 20)
    )
