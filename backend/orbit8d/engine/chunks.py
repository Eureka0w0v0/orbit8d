"""分段处理（SPEC §5.7）：整曲切成约 10 秒一段依次处理，峰值内存只与段长有关，
结果与整段一次处理一致（只差浮点舍入）。

- 因果的滤波 / 卷积：状态（IIR 状态、FIR 尾巴）带进下一段（StreamConvolver，各模块里的滤波器状态）。
- 要“看前看后”的处理（零相位分频、线性相位 EQ、真峰值、限幅）：每段前后各补一截上下文
  （Signal.windows 的 margin，或对内存数组直接切片），只保留本段。
- 按固定帧切分的统计（Welch 平均谱、谱通量、逐帧频带）：Framer 保证帧的位置与整段处理时相同。
- 按区间求和的统计（响度门限块、小节功率、音量起伏）：SegmentSums 按区间边界累计。
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from scipy.signal import oaconvolve, welch

CHUNK = 423_360  # 约 9.6 秒（44.1 kHz）= 32 采样渲染块 × 13230 = 30 ms 限幅窗（1323 采样）× 320
WELCH_NPERSEG = 8192
FRAME_BATCH = 64  # 按帧统计时一次最多算多少帧


def chunk_ranges(n: int, size: int = CHUNK) -> list[tuple[int, int]]:
    """把 [0, n) 切成相邻的 [s, e)，除最后一段外长度都是 size。"""
    if size <= 0:
        raise ValueError(f"段长必须为正：{size}")
    return [(s, min(n, s + size)) for s in range(0, n, size)]


@dataclass(frozen=True)
class Window:
    """一段信号及其上下文：data 覆盖 [start - before, end + after)，越过整曲两端的部分不给。"""

    data: np.ndarray  # float64，(帧, 声道)
    start: int
    end: int
    before: int

    @property
    def body(self) -> np.ndarray:
        """只含本段 [start, end) 的部分。"""
        return self.data[self.before : self.before + self.end - self.start]


class Signal(Protocol):
    """可以从头到尾按段读取的整曲信号（内存数组见 ArraySignal，音频文件见 orbit8d.media.audiofile）。"""

    @property
    def frames(self) -> int: ...

    def windows(self, margin: int = 0, size: int = CHUNK) -> Iterator[Window]: ...


class ArraySignal:
    """内存里的整曲数组 (帧,) 或 (帧, 声道)，按段给出 float64 副本。"""

    def __init__(self, data: np.ndarray):
        self._data = data if data.ndim == 2 else data[:, None]

    @property
    def frames(self) -> int:
        return len(self._data)

    def windows(self, margin: int = 0, size: int = CHUNK) -> Iterator[Window]:
        n = len(self._data)
        for s, e in chunk_ranges(n, size):
            a, b = max(0, s - margin), min(n, e + margin)
            yield Window(self._data[a:b].astype(np.float64), s, e, s - a)


def aligned_windows(
    signals: list[Signal], margin: int = 0, size: int = CHUNK
) -> Iterator[tuple[Window, ...]]:
    """几路等长信号同步按段读取。"""
    lengths = {sig.frames for sig in signals}
    if len(lengths) != 1:
        raise ValueError(f"各路信号长度不一致：{sorted(lengths)}")
    yield from zip(*(sig.windows(margin, size) for sig in signals), strict=True)


class StreamConvolver:
    """逐段 FIR 卷积（重叠相加，尾巴带进下一段）。kernels 形状 (K, taps)：输入 (L,) 时 K 个核各卷这一路，
    输入 (L, K) 时第 k 列用第 k 个核；每段输出 (L, K)，就是整段卷积在这一段位置上的结果。"""

    def __init__(self, kernels: np.ndarray):
        self.kernels = np.atleast_2d(np.asarray(kernels, dtype=np.float64))
        self.tail = np.zeros((self.kernels.shape[1] - 1, len(self.kernels)))

    def process(self, x: np.ndarray) -> np.ndarray:
        length = len(x)
        acc = np.zeros((length + len(self.tail), len(self.kernels)))
        if np.any(x):  # 全静音的段卷积结果就是 0，省掉 FFT
            for k, h in enumerate(self.kernels):
                acc[:, k] = oaconvolve(x if x.ndim == 1 else x[:, k], h)
        acc[: len(self.tail)] += self.tail
        self.tail = acc[length:].copy()
        return acc[:length]

    def flush(self) -> np.ndarray:
        """整曲结束后剩下的尾巴（taps - 1 帧），交出后状态清零。"""
        tail, self.tail = self.tail, np.zeros_like(self.tail)
        return tail


class Framer:
    """把逐段到来的信号（最后一维是时间）切成长 frame、帧移 hop 的完整帧，
    帧位置与整段处理相同（第 k 帧起于 k·hop）。push 返回恰好含若干个完整帧的一截
    （不够一帧时返回 None），剩下的留到下一次。"""

    def __init__(self, frame: int, hop: int):
        if not 0 < hop <= frame:
            raise ValueError(f"需要 0 < hop <= frame：hop={hop}, frame={frame}")
        self.frame, self.hop = frame, hop
        self._buf: np.ndarray | None = None

    def count(self, length: int) -> int:
        """长度为 length 的一截里完整帧的个数。"""
        return 0 if length < self.frame else 1 + (length - self.frame) // self.hop

    def push(self, x: np.ndarray) -> np.ndarray | None:
        buf = x if self._buf is None else np.concatenate([self._buf, x], axis=-1)
        m = self.count(buf.shape[-1])
        if m == 0:
            self._buf = buf
            return None
        self._buf = buf[..., m * self.hop :].copy()
        return buf[..., : (m - 1) * self.hop + self.frame]


class WelchMean:
    """scipy.signal.welch（hann 窗、半重叠、每段去均值、功率谱密度、各段平均）的分段版，与整段调用一致。
    输入 (L,) 或 (声道, L)；nperseg 与 tone.band_power 相同：min(8192, 整曲长度)。"""

    def __init__(self, n_total: int, sr: int, nperseg: int = WELCH_NPERSEG):
        self.sr = sr
        self.nperseg = min(nperseg, n_total)
        self._framer = Framer(self.nperseg, self.nperseg - self.nperseg // 2)
        self._sum: np.ndarray | None = None
        self._count = 0
        self.freqs: np.ndarray | None = None

    def push(self, x: np.ndarray) -> None:
        span = self._framer.push(x)
        if span is None:
            return
        hop = self._framer.hop
        m = self._framer.count(span.shape[-1])
        for k0 in range(0, m, FRAME_BATCH):  # 每次最多算 FRAME_BATCH 帧，限制临时内存
            k1 = min(m, k0 + FRAME_BATCH)
            part = span[..., k0 * hop : (k1 - 1) * hop + self.nperseg]
            self.freqs, p = welch(part, self.sr, nperseg=self.nperseg, axis=-1)
            self._sum = p * (k1 - k0) if self._sum is None else self._sum + p * (k1 - k0)
            self._count += k1 - k0

    def result(self) -> tuple[np.ndarray, np.ndarray]:
        if self._sum is None:
            raise ValueError("信号比一个 Welch 分段还短，没有可平均的分段")
        return self.freqs, self._sum / self._count


class SegmentSums:
    """逐段累计一个逐采样量在相邻边界之间的和。bounds 升序、不重复、首项 0、末项 = 整曲长度。"""

    def __init__(self, bounds: np.ndarray, channels: int = 1):
        self.bounds = np.asarray(bounds, dtype=np.int64)
        self.sums = np.zeros((max(len(self.bounds) - 1, 0), channels))
        self._pos = 0

    def push(self, values: np.ndarray) -> None:
        values = values.reshape(len(values), -1)
        s, e = self._pos, self._pos + len(values)
        if e == s:
            return
        inner = self.bounds[(self.bounds > s) & (self.bounds < e)] - s
        starts = np.concatenate([[0], inner])
        k0 = int(np.searchsorted(self.bounds, s, side="right")) - 1
        self.sums[k0 : k0 + len(starts)] += np.add.reduceat(values, starts, axis=0)
        self._pos = e

    def between(self, a: int, b: int) -> np.ndarray:
        """[a, b) 内的和（a、b 都必须是边界）。"""
        i, j = np.searchsorted(self.bounds, [a, b])
        return self.sums[i:j].sum(axis=0)


class BlockMax:
    """从 0 开始每 w 个采样一块（只算完整块）的最大值，逐段喂入。"""

    def __init__(self, w: int):
        self.w = w
        self._carry = np.zeros(0)
        self._out: list[np.ndarray] = []

    def push(self, values: np.ndarray) -> None:
        v = np.concatenate([self._carry, values])
        m = len(v) // self.w
        if m:
            self._out.append(v[: m * self.w].reshape(m, self.w).max(axis=1))
        self._carry = v[m * self.w :]

    def result(self) -> np.ndarray:
        return np.concatenate(self._out) if self._out else np.zeros(0)


def place(out: np.ndarray, start: int, values: np.ndarray) -> None:
    """把 values 写进 out[start : start + len(values)]，越出 out 两端的部分丢掉。"""
    a, b = max(0, start), min(len(out), start + len(values))
    if b > a:
        out[a:b] = values[a - start : b - start]
