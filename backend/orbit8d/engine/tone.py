"""按场景的音色补偿（SPEC §13.6）。

锚点（实测）：分析阶段用“经典 8D”真实渲染一遍，与原曲按 1/3 倍频程比长期平均谱 → 锚点校正量。
它包含一切效应（HRTF、背后压暗、混响、立体声对的相干叠加……），经典场景下音色与原曲一致。

其他场景（模型）：各声源沿自己的轨道取 HRTF 两耳功率 × 背后压暗 × 距离增益，按各声源的频谱加权，
再加上混响那一路 → 预测“这个场景把音色染成了什么样”。
EQ = 锚点 + 染色(经典) − 染色(当前场景)（dB）。当前场景就是经典时，正好等于锚点。
用户自己调的音量 / 静音 / 独奏不会被 EQ 拉回去：染色都是相对“同一组音量下不做空间化”的谱来算的。
"""

from typing import Protocol

import numpy as np
from scipy.signal import freqz, welch

from orbit8d.engine.hrtf import HrtfGrid, blend, interp_weights
from orbit8d.engine.render import distance_gain, rear_factor, rear_shelf_coeffs
from orbit8d.engine.scene import TRACKS, Scene, effective_track_gains
from orbit8d.engine.timeline import compile_track, track_position, wet_db_curve

BANDS_HZ = 1000.0 * 2.0 ** (np.arange(-14, 13) / 3)  # 39 Hz … 16 kHz，27 个 1/3 倍频程
HALF_BAND = 2.0 ** (1 / 6)
WELCH_NPERSEG = 8192
HRTF_NFFT = 1024
TRAJECTORY_HZ = 4.0  # 沿轨道每秒取 4 个点
MATCH_LIMIT_DB = 6.0
SCENE_LIMIT_DB = 9.0
SILENT_BAND_DB = -70.0  # 原曲里比最响频带低 70 dB 以上的频带不校正
MONO_TRACKS = ("vocals", "bass")
SUB_TRACKS = ("bass", "drums", "other")
EPS = 1e-20


def band_means(power: np.ndarray, freqs: np.ndarray) -> np.ndarray:
    """最后一维按频带取平均（频带里没有频点时取离中心最近的频点）。"""
    out = np.empty(power.shape[:-1] + (len(BANDS_HZ),))
    for k, fc in enumerate(BANDS_HZ):
        sel = (freqs >= fc / HALF_BAND) & (freqs < fc * HALF_BAND)
        out[..., k] = (
            power[..., sel].mean(axis=-1) if sel.any() else power[..., int(np.argmin(np.abs(freqs - fc)))]
        )
    return out


def band_power(x: np.ndarray, sr: int) -> np.ndarray:
    """长期平均功率谱按频带求和；立体声两个声道相加。"""
    freqs, p = welch(np.asarray(x, dtype=np.float64), sr, nperseg=min(WELCH_NPERSEG, len(x)), axis=0)
    if p.ndim == 2:
        p = p.sum(axis=1)
    out = np.empty(len(BANDS_HZ))
    for k, fc in enumerate(BANDS_HZ):
        sel = (freqs >= fc / HALF_BAND) & (freqs < fc * HALF_BAND)
        out[k] = p[sel].sum() if sel.any() else p[int(np.argmin(np.abs(freqs - fc)))]
    return out + EPS


def source_spectra(hi: dict[str, np.ndarray], sub: dict[str, np.ndarray], sr: int) -> dict[str, list[float]]:
    """渲染时每一路单声道声源（立体声轨拆成左、右，外加送混响用的单声道和）的频带功率。"""
    out = {}
    for name in TRACKS:
        x = hi[name]
        if x.ndim == 1:
            out[name] = band_power(x, sr)
        else:
            out[f"{name}_L"], out[f"{name}_R"] = band_power(x[:, 0], sr), band_power(x[:, 1], sr)
            out[f"{name}_M"] = band_power(x.mean(axis=1), sr)
    for name in SUB_TRACKS:
        out[f"{name}_sub"] = band_power(sub[name], sr)
    return {k: v.tolist() for k, v in out.items()}


def match_gain_db(reference: np.ndarray, rendered: np.ndarray, sr: int) -> np.ndarray:
    """把 rendered 的长期平均谱拉回 reference 需要的各频带增益（中位数归零，限幅 ±6 dB）。"""
    ref, got = band_power(reference, sr), band_power(rendered, sr)
    d = 10 * np.log10(ref / got)
    d[10 * np.log10(ref / ref.max()) < SILENT_BAND_DB] = 0.0
    return np.clip(d - np.median(d), -MATCH_LIMIT_DB, MATCH_LIMIT_DB)


class ToneInputs(Protocol):
    """分析结果（pipeline.Analysis）里做音色补偿要用的部分。"""

    duration_s: float
    bpm_norm: float
    t_ref: float
    calibration: dict[str, float]
    spectra: dict[str, list[float]]
    match_eq_db: list[float]


class ToneModel:
    """HRTF 各方向的频带功率表、正前方频带功率、背后压暗的频响；混响频带功率按房间缓存。"""

    def __init__(self, grid: HrtfGrid):
        self.grid = grid
        freqs = np.fft.rfftfreq(HRTF_NFFT, 1 / grid.sample_rate)
        table = np.empty((grid.n_el, grid.n_az, len(BANDS_HZ)))
        for i in range(grid.n_el):  # 按仰角逐行做，避免一次性占用几百 MB
            spec = np.fft.rfft(grid.data[i].astype(np.float64), HRTF_NFFT, axis=-1)
            table[i] = band_means((np.abs(spec) ** 2).sum(axis=1), freqs)
        self.table = table
        self.front = blend(table, interp_weights(grid, np.array([0.0]), np.array([0.0])))[0]
        b, a = rear_shelf_coeffs(grid.sample_rate)
        self.shelf_hp = freqz(b, a, worN=BANDS_HZ, fs=grid.sample_rate)[1]
        self._brir: dict[str, np.ndarray] = {}

    def brir_power(self, room: str, brir: np.ndarray) -> np.ndarray:
        if room not in self._brir:
            spec = np.fft.rfft(brir.astype(np.float64), axis=-1)
            freqs = np.fft.rfftfreq(brir.shape[-1], 1 / self.grid.sample_rate)
            self._brir[room] = band_means((np.abs(spec) ** 2).sum(axis=0), freqs)
        return self._brir[room]

    def path_power(
        self, az: np.ndarray, el: np.ndarray, dist: np.ndarray, rear_darken_db: float
    ) -> np.ndarray:
        """一路声源沿轨道的平均频带功率（两耳之和）。"""
        hrtf = blend(self.table, interp_weights(self.grid, az, el))
        cut = 1.0 - 10.0 ** (-rear_darken_db / 20.0)
        shelf = np.abs(1.0 - (rear_factor(az, el) * cut)[:, None] * self.shelf_hp[None, :]) ** 2
        return ((distance_gain(dist) ** 2)[:, None] * shelf * hrtf).mean(axis=0)

    def coloration(self, scene: Scene, a: ToneInputs, brir: np.ndarray) -> np.ndarray:
        """空间化后的频带功率 / 同一组音量下不做空间化的频带功率。"""
        gains = effective_track_gains(scene)
        spectra = {k: np.asarray(v) for k, v in a.spectra.items()}
        t = (np.arange(max(1, int(a.duration_s * TRAJECTORY_HZ))) + 0.5) / TRAJECTORY_HZ
        spatial, flat, send = np.zeros(len(BANDS_HZ)), np.zeros(len(BANDS_HZ)), np.zeros(len(BANDS_HZ))
        for name in TRACKS:
            g2 = (gains[name] * a.calibration[name]) ** 2
            if g2 == 0.0:
                continue
            motion = compile_track(scene, name, a.bpm_norm, a.t_ref, a.duration_s)
            half = scene.mix[name].width_deg / 2
            channels = [(name, 0.0)] if name in MONO_TRACKS else [(f"{name}_L", -half), (f"{name}_R", half)]
            for key, offset in channels:
                az, el, dist = track_position(motion, t, offset)
                spatial += g2 * spectra[key] * self.path_power(az, el, dist, scene.rear_darken_db)
                flat += g2 * spectra[key]
            send += (
                (scene.mix[name].reverb_send ** 2)
                * g2
                * spectra[name if name in MONO_TRACKS else f"{name}_M"]
            )
        for name in SUB_TRACKS:
            g2 = (gains[name] * a.calibration["sub"]) ** 2
            spatial += g2 * spectra[f"{name}_sub"] * self.front
            flat += g2 * spectra[f"{name}_sub"]
        wet = float(np.mean(10 ** (wet_db_curve(scene, a.bpm_norm, a.duration_s, t) / 10)))
        spatial += wet * send * self.brir_power(scene.room.name, brir)
        return (spatial + EPS) / (flat + EPS)


def scene_eq_db(
    model: ToneModel, scene: Scene, reference: Scene, a: ToneInputs, brirs: dict[str, np.ndarray]
) -> np.ndarray:
    """当前场景的各频带 EQ 增益（dB）：锚点 + 染色(参考场景) − 染色(当前场景)。"""
    anchor = np.asarray(a.match_eq_db, dtype=np.float64)
    if scene == reference:
        return anchor
    ratio = 10 * np.log10(
        model.coloration(reference, a, brirs[reference.room.name])
        / model.coloration(scene, a, brirs[scene.room.name])
    )
    return np.clip(anchor + ratio - np.median(ratio), -SCENE_LIMIT_DB, SCENE_LIMIT_DB)
