"""整曲管线（SPEC §5、§13）：声源准备 → 分析（测速、校准、音色锚点、段落）→ 按场景渲染 → 母带。

全部分段处理（SPEC §5.7）：整曲只保留 float32 的数据（分析时的 9 路声源与校准混音、导出时的混音），
其余按约 9.6 秒一段算完即丢。导出读的是试听文件里的 9 路声源，与浏览器试听同一份数据；
校准增益、试听总增益、音色补偿都在分析阶段算好；按场景的 EQ 由同一个函数给导出和浏览器试听
（/api/projects/{id}/eq）。
"""

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass

import numpy as np
from scipy.signal import butter, sosfiltfilt

from orbit8d.engine.chunks import (
    CHUNK,
    Signal,
    StreamConvolver,
    WelchMean,
    Window,
    aligned_windows,
    chunk_ranges,
    place,
)
from orbit8d.engine.hrtf import HrtfGrid, interpolate
from orbit8d.engine.master import (
    LOUDNESS_TARGETS,
    LoudnessMeter,
    design_eq,
    diffuse_gain_db,
    eq_window,
    gain_for,
    limited,
    measure,
    with_band_gains,
)
from orbit8d.engine.orbit import bar_seconds
from orbit8d.engine.render import (
    BLOCK,
    BlockPath,
    RearShelf,
    block_times,
    distance_gain,
    grid_spectra,
    rear_factor,
    render_blocks,
)
from orbit8d.engine.reverb import synth_brir
from orbit8d.engine.scene import TRACKS, Room, Scene, effective_track_gains, preset
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
from orbit8d.engine.timeline import TrackMotion, compile_track, track_position, wet_db_curve
from orbit8d.engine.tone import (
    BANDS_HZ,
    ToneModel,
    match_from_power,
    psd_bands,
    scene_eq_db,
    spectra_from_psd,
)

CROSSOVER_HZ = 120.0
CROSSOVER_MARGIN_S = 0.5  # 零相位分频每段前后补 0.5 秒：二阶 120 Hz 滤波器的瞬态衰减到 e^-266，远低于浮点精度
LOW_SPLIT_TRACKS = ("bass", "drums", "other")  # 人声不分频
SEPARATED_TRACKS = ("vocals", "drums", "bass")  # other = 原曲 − 这三轨：分离残差并入 other，一个音都不丢
SOURCE_CHANNELS = (
    "vocals_hi", "bass_hi", "drums_hi_L", "drums_hi_R", "other_hi_L", "other_hi_R",
    "bass_sub", "drums_sub", "other_sub",
)  # fmt: skip
TRACK_CHANNELS = {"vocals": (0,), "drums": (2, 3), "bass": (1,), "other": (4, 5)}  # 立体声轨渲染成一对声源
SUB_CHANNELS = {"bass": 6, "drums": 7, "other": 8}
PREVIEW_FILES = {
    "vocals_hi": (0,), "bass_hi": (1,), "drums_hi": (2, 3), "other_hi": (4, 5),
    "bass_sub": (6,), "drums_sub": (7,), "other_sub": (8,),
}  # fmt: skip
SPECTRA_KEYS = (
    "vocals", "drums_L", "drums_R", "drums_M", "bass", "other_L", "other_R", "other_M",
    "bass_sub", "drums_sub", "other_sub",
)  # fmt: skip
CALIBRATION_KEYS = (*TRACKS, "sub")
REFERENCE_PRESET = "classic"
ANALYSIS_VERSION = 3  # 分析结果的内容变了就加一：旧项目启动时自动重新分析（不重新分轨）
RENDER_VERSION = 3  # 渲染算法变了就加一：同一场景的旧导出不再复用（v3：导出读试听文件、分段渲染、响度可选）
ORIGINAL_MATCH_LIMIT_DB = 24.0
EPS = 1e-12


@dataclass
class Sources:
    """渲染用的 9 路单声道声源（顺序见 SOURCE_CHANNELS），float32 (9, n)。"""

    data: np.ndarray
    sr: int

    @property
    def n(self) -> int:
        return self.data.shape[1]

    def hi(self, name: str) -> np.ndarray:
        """运动部分：单声道音轨 (n,)，立体声音轨 (n, 2)。"""
        ch = TRACK_CHANNELS[name]
        return self.data[ch[0]] if len(ch) == 1 else self.data[list(ch)].T

    def sub(self, name: str) -> np.ndarray:
        return self.data[SUB_CHANNELS[name]]

    def chunks(self, size: int = CHUNK) -> Iterator[np.ndarray]:
        """按段给出 (9, L) 的 float64 副本。"""
        for s, e in chunk_ranges(self.n, size):
            yield self.data[:, s:e].astype(np.float64)


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


def split_window(
    orig: Window, vocals: Window, drums: Window, bass: Window, sr: int
) -> tuple[np.ndarray, dict[str, float]]:
    """一段原曲 + 三条分轨（带同样的上下文）→ 这一段的 9 路声源 (9, L) 与各轨原始能量（校准目标）。"""
    keep = slice(orig.before, orig.before + orig.end - orig.start)
    stems = {
        "bass": bass.data,
        "drums": drums.data,
        "other": orig.data - vocals.data - drums.data - bass.data,
    }
    out = np.empty((len(SOURCE_CHANNELS), orig.end - orig.start))
    out[0] = vocals.body.mean(axis=1)
    energy = {"vocals": float(np.sum(vocals.body**2))}
    low_total = np.zeros((out.shape[1], 2))
    for name in LOW_SPLIT_TRACKS:
        low, high = (part[keep] for part in split_low(stems[name], sr))
        out[SUB_CHANNELS[name]] = low.mean(axis=1)
        low_total += low
        channels = TRACK_CHANNELS[name]
        out[list(channels)] = high.T if len(channels) == 2 else high.mean(axis=1)
        energy[name] = float(np.sum(high**2))
    energy["sub"] = float(np.sum(low_total**2))
    return out, energy


def spectra_rows(chunk: np.ndarray) -> Iterator[np.ndarray]:
    """(9, L) 声源 → 按 SPECTRA_KEYS 顺序逐路给出单声道（立体声轨再加左右平均，即送混响的那一路）。"""
    c = chunk
    yield from (c[0], c[2], c[3], c[2:4].mean(axis=0), c[1], c[4], c[5], c[4:6].mean(axis=0))
    yield from (c[6], c[7], c[8])


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


class _PathRender:
    """一路单声道声源（一条轨道 + 声像偏移）的逐段渲染，带着背后压暗与卷积尾巴的状态。"""

    def __init__(self, renderer: "Renderer", motion: TrackMotion, offset: float, gain: float, darken: float):
        self._renderer, self._motion, self._offset, self._gain = renderer, motion, offset, gain
        self._shelf = RearShelf(darken, renderer.grid.sample_rate)
        self._tail: np.ndarray | None = None

    def process(self, x: np.ndarray, t: np.ndarray) -> np.ndarray:
        az, el, dist = track_position(self._motion, t, self._offset)
        path = BlockPath(az=az, el=el, gain=distance_gain(dist) * self._gain, rear=rear_factor(az, el))
        shaped = self._shelf.process(x, path.rear)
        y, self._tail = render_blocks(shaped, path, self._renderer.spectra, self._renderer.grid, self._tail)
        return y


def send_chunk(scene: Scene, levels: dict[str, float], chunk: np.ndarray, wet_db: np.ndarray) -> np.ndarray:
    """送进混响的单声道信号：各轨送出量之和 × 分段混响量（逐块，与轨道同步过渡）。"""
    send = np.zeros(chunk.shape[1])
    for name in TRACKS:
        if levels[name] > 0:
            ch = TRACK_CHANNELS[name]
            send += levels[name] * (chunk[ch[0]] if len(ch) == 1 else chunk[list(ch)].mean(axis=0))
    return send * np.repeat(10 ** (wet_db / 20), BLOCK)[: chunk.shape[1]]


def send_levels(scene: Scene, calibration: dict[str, float]) -> dict[str, float]:
    gains = effective_track_gains(scene)
    return {name: scene.mix[name].reverb_send * gains[name] * calibration[name] for name in TRACKS}


class SceneRender:
    """按场景逐段渲染：依次喂 9 路声源相邻的 (9, L) 片段（除最后一段外 L 须是 BLOCK 的整数倍），
    得到这一段的各轨干声 (L, 2)（静音的轨不出现）、超低频 (L, 2) 与混响送出 (L,)。"""

    def __init__(self, renderer: "Renderer", scene: Scene, bpm_norm: float, t_ref: float, cal: dict, n: int):
        gains = effective_track_gains(scene)
        self._scene, self._bpm_norm, self._sr = scene, bpm_norm, renderer.grid.sample_rate
        self._duration = n / self._sr
        self._paths: dict[str, list[tuple[_PathRender, int]]] = {}
        for name in TRACKS:
            if gains[name] * cal[name] > 0:
                motion = compile_track(scene, name, bpm_norm, t_ref, self._duration)
                width, channels = scene.mix[name].width_deg, TRACK_CHANNELS[name]
                offsets = [0.0] if len(channels) == 1 else [-width / 2, width / 2]
                self._paths[name] = [
                    (_PathRender(renderer, motion, off, gains[name] * cal[name], scene.rear_darken_db), ch)
                    for ch, off in zip(channels, offsets, strict=True)
                ]
        self._sub_gains = {name: gains[name] for name in LOW_SPLIT_TRACKS}
        self._sub, self._cal_sub = StreamConvolver(renderer.front), cal["sub"]
        self._levels = send_levels(scene, cal)
        self._pos = 0

    def process(self, chunk: np.ndarray) -> tuple[dict[str, np.ndarray], np.ndarray, np.ndarray]:
        if self._pos % BLOCK:
            raise ValueError("只有整曲最后一段的长度可以不是 BLOCK 的整数倍")
        length = chunk.shape[1]
        t = block_times(length, self._sr, BLOCK, self._pos // BLOCK)
        tracks = {
            name: sum(p.process(chunk[ch], t) for p, ch in paths) for name, paths in self._paths.items()
        }
        sub_mix = sum(g * chunk[SUB_CHANNELS[name]] for name, g in self._sub_gains.items())
        sub = self._sub.process(sub_mix) * self._cal_sub
        wet_db = wet_db_curve(self._scene, self._bpm_norm, self._duration, t)
        self._pos += length
        return tracks, sub, send_chunk(self._scene, self._levels, chunk, wet_db)


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

    @property
    def sr(self) -> int:
        return self.grid.sample_rate

    def brir(self, room: str) -> np.ndarray:
        if room not in self._brirs:
            self._brirs[room] = synth_brir(self.grid, room, self.grid.sample_rate)
        return self._brirs[room]

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

    # ---- 逐段渲染 ----
    def raw_chunks(
        self, chunks: Iterable[np.ndarray], n: int, scene: Scene, bpm_norm: float, t_ref: float, cal: dict
    ) -> Iterator[np.ndarray]:
        """干声各轨 + 超低频 + 混响（未过 EQ），逐段 (L, 2)。"""
        render = SceneRender(self, scene, bpm_norm, t_ref, cal, n)
        wet = StreamConvolver(self.brir(scene.room.name))
        for chunk in chunks:
            tracks, sub, send = render.process(chunk)
            yield sum(tracks.values()) + sub + wet.process(send)

    def mix_from_chunks(
        self, chunks: Iterable[np.ndarray], n: int, scene: Scene, analysis: Analysis
    ) -> np.ndarray:
        """未经母带的完整混音（干声 + 混响，已过按场景的补偿 EQ），float32 (n, 2)。"""
        fir = self.scene_eq(scene, analysis)
        delay = (len(fir) - 1) // 2
        eq = StreamConvolver(np.stack([fir, fir]))
        mix = np.zeros((n, 2), dtype=np.float32)
        pos = 0
        for raw in self.raw_chunks(chunks, n, scene, analysis.bpm_norm, analysis.t_ref, analysis.calibration):
            place(mix, pos - delay, eq.process(raw))  # 线性相位 EQ 的延迟在这里补回来
            pos += len(raw)
        place(mix, n - delay, eq.flush())
        return mix

    def export_chunks(
        self, chunks: Iterable[np.ndarray], n: int, scene: Scene, analysis: Analysis, loudness: str = "loud"
    ) -> Iterator[np.ndarray]:
        """导出：整曲混音 → 测 30 ms 片段峰值与响度 → 总增益 → 逐段前视限幅，按顺序给出 (L, 2)。"""
        mix = self.mix_from_chunks(chunks, n, scene, analysis)

        def window(a: int, b: int) -> np.ndarray:
            return mix[a:b].astype(np.float64)

        gain = gain_for(measure(window, n, self.sr), LOUDNESS_TARGETS[loudness])
        for out, _ in limited(window, n, self.sr, gain):
            yield out

    # ---- 整段版（测试与小工具用；内部走同一套逐段实现） ----
    def render_tracks(
        self, src: Sources, scene: Scene, bpm_norm: float, t_ref: float, calibration: dict[str, float]
    ) -> dict[str, np.ndarray]:
        """各轨干声与超低频 (n, 2)；静音的轨为全 0。"""
        render = SceneRender(self, scene, bpm_norm, t_ref, calibration, src.n)
        parts: dict[str, list[np.ndarray]] = {key: [] for key in CALIBRATION_KEYS}
        for chunk in src.chunks():
            tracks, sub, _ = render.process(chunk)
            for name in TRACKS:
                parts[name].append(tracks.get(name, np.zeros((chunk.shape[1], 2))))
            parts["sub"].append(sub)
        return {key: np.concatenate(v) if v else np.zeros((0, 2)) for key, v in parts.items()}

    def send_signal(
        self, src: Sources, scene: Scene, calibration: dict[str, float], bpm_norm: float
    ) -> np.ndarray:
        levels, duration, out = send_levels(scene, calibration), src.n / src.sr, []
        for (s, _), chunk in zip(chunk_ranges(src.n), src.chunks(), strict=True):
            t = block_times(chunk.shape[1], src.sr, BLOCK, s // BLOCK)
            out.append(send_chunk(scene, levels, chunk, wet_db_curve(scene, bpm_norm, duration, t)))
        return np.concatenate(out) if out else np.zeros(0)

    def reverb(self, send: np.ndarray, room: Room) -> np.ndarray:
        wet = StreamConvolver(self.brir(room.name))
        return np.concatenate([wet.process(send[s:e]) for s, e in chunk_ranges(len(send))])

    def render_mix(self, src: Sources, scene: Scene, analysis: Analysis) -> np.ndarray:
        return self.mix_from_chunks(src.chunks(), src.n, scene, analysis)

    def export(self, src: Sources, scene: Scene, analysis: Analysis, loudness: str = "loud") -> np.ndarray:
        return np.concatenate(list(self.export_chunks(src.chunks(), src.n, scene, analysis, loudness)))

    # ---- 校准 ----
    def calibrate(
        self, prep: Prepared, bpm_norm: float, t_ref: float, default_bars: int
    ) -> tuple[dict[str, float], np.ndarray, float, float]:
        """用“经典 8D”预设渲染：第一遍得每轨校准增益 = √(原始能量 / 渲染能量)；第二遍渲染校准后的混音，
        与原曲比长期平均谱得到音色锚点，再算试听总增益与试听响度（LUFS）。"""
        ref, src = preset(REFERENCE_PRESET, default_bars), prep.sources
        rendered = self._track_energies(src, ref, bpm_norm, t_ref)
        cal = {
            key: float(np.sqrt(prep.energy[key] / rendered[key]))
            if rendered[key] > EPS and prep.energy[key] > EPS
            else 1.0
            for key in CALIBRATION_KEYS
        }
        raw, pos = np.empty((src.n, 2), dtype=np.float32), 0
        for chunk in self.raw_chunks(src.chunks(), src.n, ref, bpm_norm, t_ref, cal):
            raw[pos : pos + len(chunk)] = chunk
            pos += len(chunk)
        anchor = match_from_power(prep.orig_power, self._eq_band_power(raw, self.eq))
        fir = self.band_eq(anchor)
        stats = measure(lambda a, b: eq_window(raw, fir, a, b), src.n, src.sr)
        gain = gain_for(stats)
        return cal, anchor, gain, stats.loudness + 20 * np.log10(max(gain, EPS))

    def _track_energies(self, src: Sources, scene: Scene, bpm_norm: float, t_ref: float) -> dict[str, float]:
        render = SceneRender(self, scene, bpm_norm, t_ref, dict.fromkeys(CALIBRATION_KEYS, 1.0), src.n)
        energy = dict.fromkeys(CALIBRATION_KEYS, 0.0)
        for chunk in src.chunks():
            tracks, sub, _ = render.process(chunk)
            for name, y in tracks.items():
                energy[name] += float(np.sum(y**2))
            energy["sub"] += float(np.sum(sub**2))
        return energy

    def _eq_band_power(self, x: np.ndarray, fir: np.ndarray) -> np.ndarray:
        welch = WelchMean(len(x), self.sr)
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
    calibration, anchor, preview_gain, preview_lufs = renderer.calibrate(prep, info.bpm_norm, t_ref, bars)
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
