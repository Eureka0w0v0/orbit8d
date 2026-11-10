"""按场景渲染（SPEC §5、§13）：9 路声源 → 各轨干声 + 超低频 + 混响 → 按场景的补偿 EQ → 母带输出。

全部分段处理（SPEC §5.7）：导出时整曲只保留 float32 的混音，其余按约 9.6 秒一段算完即丢。
导出读的是试听文件里的 9 路声源，与浏览器试听同一份数据；校准增益、试听总增益、音色锚点由分析阶段
（engine/analysis.py）算好传进来；按场景的 EQ 由同一个函数给导出和浏览器试听（/api/projects/{id}/eq）。
"""

from collections.abc import Iterable, Iterator

import numpy as np

from orbit8d.engine.chunks import StreamConvolver, chunk_ranges, place
from orbit8d.engine.hrtf import HrtfGrid, interpolate
from orbit8d.engine.master import (
    LOUDNESS_TARGETS,
    design_eq,
    diffuse_gain_db,
    gain_for,
    limited,
    measure,
    with_band_gains,
)
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
from orbit8d.engine.sources import CALIBRATION_KEYS, LOW_SPLIT_TRACKS, SUB_CHANNELS, TRACK_CHANNELS, Sources
from orbit8d.engine.timeline import TrackMotion, compile_track, track_position, wet_db_curve
from orbit8d.engine.tone import BANDS_HZ, ToneInputs, ToneModel, scene_eq_db

REFERENCE_PRESET = "classic"
RENDER_VERSION = 3  # 渲染算法变了就加一：同一场景的旧导出不再复用（v3：导出读试听文件、分段渲染、响度可选）


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

    def scene_eq(self, scene: Scene, analysis: ToneInputs) -> np.ndarray:
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
        self, chunks: Iterable[np.ndarray], n: int, scene: Scene, analysis: ToneInputs
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
        self, chunks: Iterable[np.ndarray], n: int, scene: Scene, analysis: ToneInputs, loudness: str = "loud"
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

    def render_mix(self, src: Sources, scene: Scene, analysis: ToneInputs) -> np.ndarray:
        return self.mix_from_chunks(src.chunks(), src.n, scene, analysis)

    def export(self, src: Sources, scene: Scene, analysis: ToneInputs, loudness: str = "loud") -> np.ndarray:
        return np.concatenate(list(self.export_chunks(src.chunks(), src.n, scene, analysis, loudness)))
