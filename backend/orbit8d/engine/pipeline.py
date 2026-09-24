"""整曲管线（SPEC §5、§13）：声源准备 → 分析（测速、校准、音色锚点、段落）→ 按场景渲染 → 母带。

导出与试听共用：试听用的声源文件（preview_stems）、校准增益、试听总增益、音色补偿都在分析阶段算好；
按场景的 EQ 由同一个函数给导出和浏览器试听（/api/projects/{id}/eq）。
"""

import json
from dataclasses import asdict, dataclass

import numpy as np
from scipy.signal import butter, oaconvolve, sosfiltfilt

from orbit8d.engine.hrtf import HrtfGrid, interpolate
from orbit8d.engine.master import (
    apply_eq,
    design_eq,
    diffuse_gain_db,
    integrated_loudness,
    limiter,
    master_gain,
    with_band_gains,
)
from orbit8d.engine.render import (
    BLOCK,
    BlockPath,
    block_times,
    distance_gain,
    grid_spectra,
    rear_factor,
    rear_shelf,
    render_path,
    render_static,
)
from orbit8d.engine.reverb import synth_brir
from orbit8d.engine.scene import TRACKS, Room, Scene, effective_track_gains, preset
from orbit8d.engine.structure import ENVELOPE_HOP_S, detect_sections, loudness_envelope
from orbit8d.engine.tempo import estimate_tempo, turn_plan
from orbit8d.engine.timeline import compile_track, track_position, wet_db_curve
from orbit8d.engine.tone import BANDS_HZ, ToneModel, match_gain_db, scene_eq_db, source_spectra

CROSSOVER_HZ = 120.0
LOW_SPLIT_TRACKS = ("bass", "drums", "other")  # 人声不分频
STEREO_TRACKS = ("drums", "other")  # 渲染成一对声源
CALIBRATION_KEYS = (*TRACKS, "sub")
REFERENCE_PRESET = "classic"
ANALYSIS_VERSION = 3  # 分析结果的内容变了就加一：旧项目启动时自动重新分析（不重新分轨）
RENDER_VERSION = 2  # 渲染算法变了就加一：同一场景的旧导出不再复用
ORIGINAL_MATCH_LIMIT_DB = 24.0
EPS = 1e-12


@dataclass
class Sources:
    hi: dict[str, np.ndarray]  # vocals/bass: (n,)；drums/other: (n, 2)
    sub: dict[str, np.ndarray]  # bass/drums/other 的 120 Hz 以下（单声道）
    energy_hi: dict[str, float]  # 各音轨运动部分的原始立体声能量
    energy_sub: float  # 全部超低频的原始立体声能量
    sr: int

    @property
    def n(self) -> int:
        return len(self.hi["vocals"])


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


def split_low(x: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """零相位分频：low + high 完全还原 x。"""
    low = sosfiltfilt(butter(2, CROSSOVER_HZ, fs=sr, output="sos"), x, axis=0)
    return low, x - low


def prepare_sources(orig: np.ndarray, stems: dict[str, np.ndarray], sr: int) -> Sources:
    stems = dict(stems)
    stems["other"] = stems["other"] + (orig - sum(stems.values()))  # 分离残差并入 other，一个音都不丢
    hi = {"vocals": stems["vocals"].mean(axis=1)}
    energy_hi = {"vocals": float(np.sum(stems["vocals"] ** 2))}
    sub, low_total = {}, np.zeros_like(orig)
    for name in LOW_SPLIT_TRACKS:
        low, high = split_low(stems[name], sr)
        sub[name] = low.mean(axis=1)
        low_total += low
        hi[name] = high if name in STEREO_TRACKS else high.mean(axis=1)
        energy_hi[name] = float(np.sum(high**2))
    return Sources(hi=hi, sub=sub, energy_hi=energy_hi, energy_sub=float(np.sum(low_total**2)), sr=sr)


def preview_stems(src: Sources) -> dict[str, np.ndarray]:
    """浏览器试听用的 7 路声源（文件名 → 数据）。"""
    files = {f"{name}_hi": src.hi[name] for name in TRACKS}
    files.update({f"{name}_sub": src.sub[name] for name in LOW_SPLIT_TRACKS})
    return files


class Renderer:
    """持有 HRTF 频谱、补偿 EQ 和各房间的 BRIR 缓存；本身无业务状态。"""

    def __init__(self, grid: HrtfGrid):
        self.grid = grid
        self.spectra = grid_spectra(grid)
        self.front = interpolate(grid, np.array([0.0]), np.array([0.0]))[0]
        self.eq_freqs, self.eq_base_db = diffuse_gain_db(grid)
        self.eq = design_eq(self.eq_freqs, self.eq_base_db, grid.sample_rate)  # 只有 HRTF 平均补偿
        self.tone = ToneModel(grid)
        self._brirs: dict[str, np.ndarray] = {}

    def brir(self, room: str) -> np.ndarray:
        if room not in self._brirs:
            self._brirs[room] = synth_brir(self.grid, room, self.grid.sample_rate)
        return self._brirs[room]

    def _render_track(
        self,
        x: np.ndarray,
        scene: Scene,
        name: str,
        t: np.ndarray,
        t_ref: float,
        bpm_norm: float,
        gain: float,
        duration_s: float,
    ) -> np.ndarray:
        width = scene.mix[name].width_deg
        motion = compile_track(scene, name, bpm_norm, t_ref, duration_s)
        if x.ndim == 1:
            channels, offsets = [x], [0.0]
        else:
            channels, offsets = [x[:, 0], x[:, 1]], [-width / 2, width / 2]
        out = np.zeros((len(channels[0]), 2))
        for ch, offset in zip(channels, offsets, strict=True):
            az, el, dist = track_position(motion, t, offset)
            path = BlockPath(az=az, el=el, gain=distance_gain(dist) * gain, rear=rear_factor(az, el))
            shaped = rear_shelf(ch, path.rear, scene.rear_darken_db, self.grid.sample_rate)
            out += render_path(shaped, path, self.spectra, self.grid)
        return out

    def render_tracks(
        self, src: Sources, scene: Scene, bpm_norm: float, t_ref: float, calibration: dict[str, float]
    ) -> dict[str, np.ndarray]:
        gains = effective_track_gains(scene)
        t = block_times(src.n, src.sr)
        out = {}
        for name in TRACKS:
            gain = gains[name] * calibration[name]
            out[name] = (
                self._render_track(src.hi[name], scene, name, t, t_ref, bpm_norm, gain, src.n / src.sr)
                if gain > 0
                else np.zeros((src.n, 2))
            )
        sub_mix = sum(gains[name] * src.sub[name] for name in LOW_SPLIT_TRACKS)
        out["sub"] = render_static(sub_mix, self.front) * calibration["sub"]
        return out

    def send_signal(
        self, src: Sources, scene: Scene, calibration: dict[str, float], bpm_norm: float
    ) -> np.ndarray:
        """送进混响的单声道信号：各轨送出量之和 × 分段混响量（逐块，与轨道同步过渡）。"""
        gains = effective_track_gains(scene)
        send = np.zeros(src.n)
        for name in TRACKS:
            level = scene.mix[name].reverb_send * gains[name] * calibration[name]
            if level > 0:
                x = src.hi[name]
                send += level * (x if x.ndim == 1 else x.mean(axis=1))
        wet_db = wet_db_curve(scene, bpm_norm, src.n / src.sr, block_times(src.n, src.sr))
        return send * np.repeat(10 ** (wet_db / 20), BLOCK)[: src.n]

    def reverb(self, send: np.ndarray, room: Room) -> np.ndarray:
        if not np.any(send):
            return np.zeros((len(send), 2))
        brir = self.brir(room.name).astype(np.float64)
        return np.stack([oaconvolve(send, brir[ear])[: len(send)] for ear in (0, 1)], axis=1)

    def band_eq(self, band_db: np.ndarray) -> np.ndarray:
        """HRTF 平均补偿 + 按频带的校正 → FIR。"""
        gain = with_band_gains(
            self.eq_freqs, self.eq_base_db, BANDS_HZ, np.asarray(band_db, dtype=np.float64)
        )
        return design_eq(self.eq_freqs, gain, self.grid.sample_rate)

    def scene_eq(self, scene: Scene, analysis: Analysis) -> np.ndarray:
        """这个场景的补偿 EQ（导出与浏览器试听共用）。"""
        ref = preset(REFERENCE_PRESET, analysis.default_bars)
        brirs = {name: self.brir(name) for name in {scene.room.name, ref.room.name}}
        return self.band_eq(scene_eq_db(self.tone, scene, ref, analysis, brirs))

    def render_mix(self, src: Sources, scene: Scene, analysis: Analysis) -> np.ndarray:
        """未经母带的完整混音（干声 + 混响，已过按场景的补偿 EQ）。"""
        tracks = self.render_tracks(src, scene, analysis.bpm_norm, analysis.t_ref, analysis.calibration)
        wet = self.reverb(self.send_signal(src, scene, analysis.calibration, analysis.bpm_norm), scene.room)
        return apply_eq(sum(tracks.values()) + wet, self.scene_eq(scene, analysis))

    def export(self, src: Sources, scene: Scene, analysis: Analysis) -> np.ndarray:
        mix = self.render_mix(src, scene, analysis)
        mix = mix * master_gain(mix, src.sr)
        return limiter(mix, src.sr)[0]

    def calibrate(
        self, src: Sources, orig: np.ndarray, bpm_norm: float, t_ref: float, default_bars: int
    ) -> tuple[dict[str, float], np.ndarray, float, float]:
        """用“经典 8D”预设渲染一次：每轨校准增益 = √(原始能量 / 渲染能量)；
        与原曲比长期平均谱得到音色锚点；再算试听总增益与试听响度（LUFS）。"""
        ref = preset(REFERENCE_PRESET, default_bars)
        tracks = self.render_tracks(src, ref, bpm_norm, t_ref, dict.fromkeys(CALIBRATION_KEYS, 1.0))
        targets = {**src.energy_hi, "sub": src.energy_sub}
        cal = {}
        for key in CALIBRATION_KEYS:
            rendered = float(np.sum(tracks[key] ** 2))
            cal[key] = (
                float(np.sqrt(targets[key] / rendered)) if rendered > EPS and targets[key] > EPS else 1.0
            )
        dry = sum(cal[key] * tracks[key] for key in CALIBRATION_KEYS)
        raw = dry + self.reverb(self.send_signal(src, ref, cal, bpm_norm), ref.room)
        anchor = match_gain_db(orig, apply_eq(raw, self.eq), src.sr)
        mix = apply_eq(raw, self.band_eq(anchor))
        gain = master_gain(mix, src.sr)
        return cal, anchor, gain, integrated_loudness(mix, src.sr) + 20 * np.log10(max(gain, EPS))


def analyze(
    orig: np.ndarray, stems: dict[str, np.ndarray], sr: int, renderer: Renderer
) -> tuple[Sources, Analysis]:
    src = prepare_sources(orig, stems, sr)
    info = estimate_tempo(stems["drums"].mean(axis=1), sr)
    bars, t_ref = turn_plan(info)
    calibration, anchor, preview_gain, preview_lufs = renderer.calibrate(
        src, orig, info.bpm_norm, t_ref, bars
    )
    sections = detect_sections(orig, stems["vocals"], sr, info.bpm_norm, t_ref)
    return src, Analysis(
        sample_rate=sr,
        duration_s=len(orig) / sr,
        bpm=info.bpm,
        bpm_norm=info.bpm_norm,
        default_bars=bars,
        t_ref=t_ref,
        calibration=calibration,
        preview_gain=preview_gain,
        match_eq_db=[float(v) for v in anchor],
        spectra=source_spectra(src.hi, src.sub, sr),
        sections=[s.to_dict() for s in sections],
        original_gain=loudness_match_gain(preview_lufs, integrated_loudness(orig, sr)),
        envelope_db=loudness_envelope(orig, sr),
        envelope_hop_s=ENVELOPE_HOP_S,
    )


def loudness_match_gain(target_lufs: float, source_lufs: float) -> float:
    """把 source 调到和 target 一样响的线性增益（限幅 ±24 dB；任一边测不出响度时不调）。"""
    if not (np.isfinite(target_lufs) and np.isfinite(source_lufs)):
        return 1.0
    return float(
        10 ** (np.clip(target_lufs - source_lufs, -ORIGINAL_MATCH_LIMIT_DB, ORIGINAL_MATCH_LIMIT_DB) / 20)
    )
