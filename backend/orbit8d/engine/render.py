"""分块双线性 HRTF 渲染（导出与浏览器试听共用同一算法，SPEC §5.2）。

每 32 个采样为一块：块内方向、增益、背后压暗系数恒定；本块输入与本块 HRIR 做完整线性卷积，
尾巴重叠相加到后续块。浏览器 AudioWorklet 把 128 采样的渲染量子拆成 4 个 32 采样的小块，
两边算法逐块一致。块越短方向变化越平滑（128 → 32 时，与更细分块的差异约低 13 dB）。

可以分段渲染（SPEC §5.7）：每段从块边界开始，背后压暗滤波器的状态与卷积尾巴带进下一段
（RearShelf、render_blocks）。
"""

from dataclasses import dataclass

import numpy as np
from scipy.signal import butter, lfilter

from orbit8d.engine.hrtf import HrtfGrid, blend, interp_weights

BLOCK = 32
NFFT = 160  # BLOCK 的整数倍，且 ≥ BLOCK + taps - 1
CHUNK_BLOCKS = 16384  # 一段之内再分批做 FFT，限制内存
REAR_SHELF_HZ = 3000.0
MIN_DISTANCE_M = 0.5
MAX_DISTANCE_M = 4.0
REF_DISTANCE_M = 1.0


@dataclass(frozen=True)
class BlockPath:
    """一个声源逐块的方向与增益（长度 = 块数）。"""

    az: np.ndarray
    el: np.ndarray
    gain: np.ndarray
    rear: np.ndarray


def block_times(n_samples: int, sr: int, block: int = BLOCK, first: int = 0) -> np.ndarray:
    """覆盖 n_samples 个采样的各块中心时刻（秒）；first：第一块在整曲中的序号。"""
    nb = -(-n_samples // block)
    return (np.arange(first, first + nb) * block + block / 2) / sr


def rear_factor(az, el) -> np.ndarray:
    """声源在身后的程度：max(0, -z)，z 为方向向量朝前的分量。"""
    z = np.cos(np.radians(el)) * np.cos(np.radians(az))
    return np.clip(-z, 0.0, None)


def distance_gain(dist) -> np.ndarray:
    return REF_DISTANCE_M / np.clip(np.asarray(dist, dtype=np.float64), MIN_DISTANCE_M, MAX_DISTANCE_M)


def rear_shelf_coeffs(sr: int):
    """一阶 Butterworth 高通（因果）。浏览器端用同一组系数。"""
    b, a = butter(1, REAR_SHELF_HZ, "highpass", fs=sr)
    return b, a


class RearShelf:
    """背后压暗 x - r·cut·hp(x)：r 逐块恒定，cut = 1 - 10^(-cut_db/20)。
    逐段处理时一阶高通的状态带进下一段。"""

    def __init__(self, cut_db: float, sr: int):
        self.cut = 1.0 - 10.0 ** (-cut_db / 20.0)
        self.b, self.a = rear_shelf_coeffs(sr)
        self.zi = np.zeros(1)

    def process(self, x: np.ndarray, rear_blocks: np.ndarray, block: int = BLOCK) -> np.ndarray:
        if self.cut == 0.0:
            return x
        hp, self.zi = lfilter(self.b, self.a, x, zi=self.zi)  # 不压暗的段也要滤，状态才接得上
        if not np.any(rear_blocks):
            return x
        r = np.repeat(rear_blocks, block)[: len(x)]
        return x - self.cut * r * hp


def rear_shelf(
    x: np.ndarray, rear_blocks: np.ndarray, cut_db: float, sr: int, block: int = BLOCK
) -> np.ndarray:
    return RearShelf(cut_db, sr).process(np.asarray(x, dtype=np.float64), rear_blocks, block)


def grid_spectra(grid: HrtfGrid, nfft: int = NFFT) -> np.ndarray:
    """(n_el, n_az, 2, nfft//2+1) 的 HRIR 频谱。"""
    if nfft < BLOCK + grid.taps - 1 or nfft % BLOCK:
        raise ValueError(f"nfft={nfft} 必须是 {BLOCK} 的整数倍且至少 {BLOCK + grid.taps - 1}")
    return np.fft.rfft(grid.data.astype(np.float64), nfft, axis=-1).astype(np.complex128)


def render_blocks(
    x: np.ndarray,
    path: BlockPath,
    spectra: np.ndarray,
    grid: HrtfGrid,
    tail: np.ndarray | None = None,
    block: int = BLOCK,
    nfft: int = NFFT,
) -> tuple[np.ndarray, np.ndarray]:
    """单声道 x 沿 path 渲染成 (n, 2) 双耳信号。x 从块边界开始（只有整曲最后一段的长度可以不是块长的整数倍）；
    tail 是上一段留下的卷积尾巴（None = 从静音开始）。返回 (输出, 留给下一段的尾巴)。"""
    n, nb = len(x), len(path.az)
    if nb != -(-n // block):
        raise ValueError(f"块数不匹配：信号 {n} 采样需要 {-(-n // block)} 块，路径给了 {nb} 块")
    padded = np.zeros(nb * block)
    padded[:n] = x
    blocks = padded.reshape(nb, block) * path.gain[:, None]
    weights = interp_weights(grid, path.az, path.el)
    segs = nfft // block
    out = np.zeros((nb + segs - 1, block, 2))
    if tail is not None:
        out[: segs - 1] += tail
    for s in range(0, nb, CHUNK_BLOCKS):
        e = min(nb, s + CHUNK_BLOCKS)
        spec = np.fft.rfft(blocks[s:e], nfft, axis=1)
        w = tuple(arr[s:e] for arr in weights)
        for ear in (0, 1):
            h = blend(spectra[:, :, ear], w)
            y = np.fft.irfft(spec * h, nfft, axis=1).reshape(e - s, segs, block)
            for j in range(segs):
                out[s + j : e + j, :, ear] += y[:, j, :]
    return out[:nb].reshape(-1, 2)[:n], out[nb:]


def render_path(
    x: np.ndarray, path: BlockPath, spectra: np.ndarray, grid: HrtfGrid, block: int = BLOCK, nfft: int = NFFT
) -> np.ndarray:
    """整段版：单声道 x 沿 path 渲染成 (n, 2) 双耳信号。"""
    return render_blocks(x, path, spectra, grid, None, block, nfft)[0]
