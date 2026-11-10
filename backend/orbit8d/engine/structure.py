"""段落识别（SPEC §13.4）：小节网格 → 每小节的音色 + 人声特征 → 自相似矩阵上的新颖度 → 分界（落在小节线上）
→ 按能量与人声贴标签（前奏 / 主歌 / 副歌 / 桥段 / 尾声）。

启发式，只求大致对：用户可以在时间轴上拖分界、改标签；标签只决定自动编排给这一段配哪种轨道。

统计量都可以分段累计（SPEC §5.7）：与小节无关的（逐帧频带、整曲功率、音量起伏）在测速前就能算，
每小节功率要等测出速度、知道小节线以后再过一遍。
"""

from dataclasses import asdict, dataclass

import numpy as np

from orbit8d.engine.chunks import Framer, SegmentSums, chunk_ranges
from orbit8d.engine.orbit import bar_seconds

FRAME = 4096
HOP = 2048
CHUNK_FRAMES = 256  # 分批做 FFT，限制内存
BAND_EDGES_HZ = np.geomspace(60.0, 12000.0, 25)  # 24 个对数频带描述音色
VOCAL_WEIGHT = 3.0  # 人声进出是很强的段落信号，特征里加权
KERNEL_BARS = 4  # 新颖度核：分界前后各看 4 小节
MIN_SECTION_BARS = 4
MAX_SECTIONS = 8
NOVELTY_FLOOR = 0.15  # 低于最强分界 15% 的候选不要（人声进出的分界往往特别强，阈值不能跟着它定太高）
QUIET_DB = 3.0  # 首 / 尾段比各段中位能量低 3 dB 以上 → 前奏 / 尾声
VOCAL_ABSENT_DB = 12.0  # 首 / 尾段人声比人声最响的段低 12 dB 以上 → 前奏 / 尾声
CHORUS_SPLIT = 0.5  # 中间各段：能量高于 最低 + 50% 极差 → 副歌
MIN_CONTRAST_DB = 1.5  # 中间各段能量差不到 1.5 dB：只把最响的一段当副歌
EPS = 1e-12

ENVELOPE_HOP_S = 0.25  # 时间轴底下的音量起伏：每 0.25 秒一个点
ENVELOPE_FLOOR_DB = -60.0

INTRO, VERSE, CHORUS, BRIDGE, OUTRO, WHOLE = "前奏", "主歌", "副歌", "桥段", "尾声", "全曲"


@dataclass(frozen=True)
class SectionInfo:
    start_s: float
    bars: int  # 段内完整小节数
    label: str
    energy_db: float  # 段内平均功率（dBFS）

    def to_dict(self) -> dict:
        return asdict(self)


class LoudnessEnvelope:
    """原曲每 hop_s 秒的平均功率（立体声两声道相加），按顺序逐段喂 (帧,) 或 (帧, 声道)。"""

    def __init__(self, n_total: int, sr: int, hop_s: float = ENVELOPE_HOP_S):
        self.hop = max(1, int(round(hop_s * sr)))
        self._sums = SegmentSums(np.append(np.arange(0, n_total, self.hop), n_total))

    def push(self, x: np.ndarray) -> None:
        power = np.asarray(x, dtype=np.float64) ** 2
        self._sums.push(power if power.ndim == 1 else power.sum(axis=1))

    def result(self) -> list[float]:
        """相对全曲最响处的 dB，最低 -60，保留 1 位小数；最后不满一格的也算一格（按整格平均）。"""
        frames = self._sums.sums[:, 0] / self.hop
        peak = float(frames.max()) if len(frames) else 0.0
        if peak <= EPS:
            return [ENVELOPE_FLOOR_DB] * len(frames)
        db = np.maximum(10 * np.log10(np.maximum(frames, EPS) / peak), ENVELOPE_FLOOR_DB)
        return [round(float(v), 1) for v in db]


def loudness_envelope(x: np.ndarray, sr: int, hop_s: float = ENVELOPE_HOP_S) -> list[float]:
    envelope = LoudnessEnvelope(len(x), sr, hop_s)
    for s, e in chunk_ranges(len(x)):
        envelope.push(x[s:e])
    return envelope.result()


def bar_starts(duration_s: float, bpm_norm: float, t_ref: float) -> np.ndarray:
    """与 t_ref（正前方对齐的拍）同相位的小节线；第一条在 [0, 一小节) 内。"""
    bar = bar_seconds(bpm_norm)
    return np.arange(t_ref % bar, duration_s, bar)


def complete_bars(duration_s: float, bpm_norm: float, t_ref: float) -> np.ndarray:
    """完整落在曲内的小节的起点。"""
    starts = bar_starts(duration_s, bpm_norm, t_ref)
    return starts[starts + bar_seconds(bpm_norm) <= duration_s + EPS]


def _band_matrix(sr: int) -> np.ndarray:
    freqs = np.fft.rfftfreq(FRAME, 1 / sr)
    band = np.digitize(freqs, BAND_EDGES_HZ) - 1
    m = np.zeros((len(freqs), len(BAND_EDGES_HZ) - 1))
    ok = (band >= 0) & (band < m.shape[1])
    m[np.nonzero(ok)[0], band[ok]] = 1.0
    return m


def frame_bands(x: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """每帧各频带功率 (帧数, 24) 与帧中心时刻（秒）。"""
    x = np.asarray(x, dtype=np.float32)
    n = 0 if len(x) < FRAME else 1 + (len(x) - FRAME) // HOP
    window = np.hanning(FRAME).astype(np.float32)
    m = _band_matrix(sr)
    out = np.zeros((n, m.shape[1]))
    offsets = np.arange(FRAME)
    for s in range(0, n, CHUNK_FRAMES):
        idx = np.arange(s, min(s + CHUNK_FRAMES, n))
        frames = x[idx[:, None] * HOP + offsets] * window
        out[idx] = (np.abs(np.fft.rfft(frames, axis=1)) ** 2) @ m
    return out, (np.arange(n) * HOP + FRAME / 2) / sr


class MixFeatures:
    """段落识别要从原曲（单声道）里统计、与小节无关的部分：逐帧频带功率与整曲平均功率。按顺序逐段喂入。"""

    def __init__(self, sr: int):
        self.sr = sr
        self._framer = Framer(FRAME, HOP)
        self._parts: list[np.ndarray] = []
        self._power, self._count = 0.0, 0

    def push(self, mono: np.ndarray) -> None:
        mono = np.asarray(mono, dtype=np.float64)
        self._power += float(np.sum(mono**2))
        self._count += len(mono)
        span = self._framer.push(mono)
        if span is not None:
            self._parts.append(frame_bands(span, self.sr)[0])

    def result(self) -> tuple[np.ndarray, np.ndarray, float]:
        """(逐帧频带功率, 帧中心时刻, 平均功率)。"""
        bands = np.concatenate(self._parts) if self._parts else np.zeros((0, len(BAND_EDGES_HZ) - 1))
        centers = (np.arange(len(bands)) * HOP + FRAME / 2) / self.sr
        return bands, centers, self._power / max(self._count, 1)


class BarPower:
    """每个完整小节内的平均功率（单声道），按顺序逐段喂入。"""

    def __init__(self, full: np.ndarray, bar: float, sr: int, n_total: int):
        self._a = np.round(full * sr).astype(int)
        self._b = np.minimum(np.round((full + bar) * sr).astype(int), n_total)
        self._sums = SegmentSums(np.unique(np.concatenate([[0, n_total], self._a, self._b])))

    def push(self, mono: np.ndarray) -> None:
        self._sums.push(np.asarray(mono, dtype=np.float64) ** 2)

    def result(self) -> np.ndarray:
        sums = np.array([self._sums.between(a, b)[0] for a, b in zip(self._a, self._b, strict=True)])
        return sums / np.maximum(self._b - self._a, 1) if len(sums) else np.zeros(0)


def _per_bar_mean(values: np.ndarray, times: np.ndarray, starts: np.ndarray, bar: float) -> np.ndarray:
    k = np.searchsorted(starts, times, side="right") - 1
    ok = (k >= 0) & (times < starts[np.clip(k, 0, None)] + bar)
    sums = np.zeros((len(starts), values.shape[1]))
    np.add.at(sums, k[ok], values[ok])
    counts = np.bincount(k[ok], minlength=len(starts))
    return sums / np.maximum(counts, 1)[:, None]


def _zscore(x: np.ndarray) -> np.ndarray:
    return (x - x.mean(axis=0)) / (x.std(axis=0) + EPS)


def novelty(features: np.ndarray) -> np.ndarray:
    """out[b] = 第 b 小节开头那条小节线的新颖度（b = 0..n）；两侧不足 KERNEL_BARS 小节的位置为 0。"""
    n = len(features)
    f = _zscore(features)
    unit = f / (np.linalg.norm(f, axis=1, keepdims=True) + EPS)
    ssm = unit @ unit.T
    pos = np.arange(-KERNEL_BARS, KERNEL_BARS) + 0.5
    taper = np.exp(-0.5 * (pos / (KERNEL_BARS / 2)) ** 2) * np.sign(pos)
    kernel = np.outer(taper, taper)  # 同侧为正、跨越分界为负的棋盘核
    out = np.zeros(n + 1)
    for b in range(KERNEL_BARS, n - KERNEL_BARS + 1):
        out[b] = float(
            np.sum(kernel * ssm[b - KERNEL_BARS : b + KERNEL_BARS, b - KERNEL_BARS : b + KERNEL_BARS])
        )
    return out


def pick_boundaries(nov: np.ndarray, n_bars: int) -> list[int]:
    """从强到弱挑局部峰：每段至少 MIN_SECTION_BARS 小节、最多 MAX_SECTIONS 段。"""
    cand = [
        b
        for b in range(MIN_SECTION_BARS, n_bars - MIN_SECTION_BARS + 1)
        if nov[b] > 0 and nov[b] >= nov[b - 1] and nov[b] >= nov[b + 1]
    ]
    if not cand:
        return []
    floor = NOVELTY_FLOOR * max(nov[b] for b in cand)
    chosen: list[int] = []
    for b in sorted(cand, key=lambda b: -nov[b]):
        if nov[b] < floor or len(chosen) == MAX_SECTIONS - 1:
            break
        if all(abs(b - c) >= MIN_SECTION_BARS for c in chosen):
            chosen.append(b)
    return sorted(chosen)


def label_sections(energy_db: np.ndarray, vocal_db: np.ndarray) -> list[str]:
    n = len(energy_db)
    if n == 1:
        return [WHOLE]
    labels = [VERSE] * n
    middle = list(range(n))
    ref, vocal_top = float(np.median(energy_db)), float(vocal_db.max())

    def quiet(k: int) -> bool:
        return energy_db[k] < ref - QUIET_DB or vocal_db[k] < vocal_top - VOCAL_ABSENT_DB

    for k, label in ((0, INTRO), (n - 1, OUTRO)):
        if n >= 3 and quiet(k):
            labels[k] = label
            middle.remove(k)
    e = energy_db[middle]
    lo, hi = float(e.min()), float(e.max())
    if hi - lo < MIN_CONTRAST_DB:
        chorus = {middle[int(np.argmax(e))]} if len(middle) >= 2 else set()
    else:
        chorus = {k for k in middle if energy_db[k] >= lo + CHORUS_SPLIT * (hi - lo)}
    for k in chorus:
        labels[k] = CHORUS
    seen = 0  # 桥段：两次副歌之后、紧接着又回到副歌的第一段非副歌
    for k in middle:
        if labels[k] == CHORUS:
            seen += 1
        elif seen >= 2 and k + 1 < n and labels[k + 1] == CHORUS:
            labels[k] = BRIDGE
            break
    return labels


@dataclass(frozen=True)
class StructureInputs:
    """段落识别用到的整曲统计量（都可以分段累计）。"""

    duration_s: float
    bands: np.ndarray  # (帧数, 24) 原曲每帧各频带功率
    centers: np.ndarray  # 帧中心时刻（秒）
    mean_power: float  # 原曲单声道平均功率（太短的歌只分一段时用）
    bar_power: np.ndarray  # 每个完整小节的平均功率：原曲单声道
    vocal_power: np.ndarray  # 同上：人声


def sections_from(inputs: StructureInputs, bpm_norm: float, t_ref: float) -> list[SectionInfo]:
    """第一段从 0 秒开始，其余分界都在小节线上。"""
    bar = bar_seconds(bpm_norm)
    full = complete_bars(inputs.duration_s, bpm_norm, t_ref)
    if len(full) < 2 * MIN_SECTION_BARS:
        total = 10 * np.log10(inputs.mean_power + EPS)
        return [SectionInfo(0.0, len(full), WHOLE, round(total, 2))]
    energy = 10 * np.log10(inputs.bar_power + EPS)
    vocal = 10 * np.log10(inputs.vocal_power + EPS)
    features = np.hstack(
        [
            _zscore(np.log10(_per_bar_mean(inputs.bands, inputs.centers, full, bar) + EPS)),
            VOCAL_WEIGHT * _zscore(vocal[:, None]),
        ]
    )
    edges = [0, *pick_boundaries(novelty(features), len(full)), len(full)]
    spans = list(zip(edges, edges[1:], strict=False))
    sec_energy = np.array([10 * np.log10(np.mean(10 ** (energy[a:b] / 10))) for a, b in spans])
    sec_vocal = np.array([10 * np.log10(np.mean(10 ** (vocal[a:b] / 10))) for a, b in spans])
    labels = label_sections(sec_energy, sec_vocal)
    return [
        SectionInfo(0.0 if k == 0 else float(full[a]), b - a, labels[k], round(float(sec_energy[k]), 2))
        for k, (a, b) in enumerate(spans)
    ]


def detect_sections(
    mix: np.ndarray, vocals: np.ndarray, sr: int, bpm_norm: float, t_ref: float
) -> list[SectionInfo]:
    """mix / vocals 可以是单声道或 (n, 2)（整段数组版，统计量与分段累计的同一套）。"""
    mono = mix.mean(axis=1) if mix.ndim == 2 else mix
    voc = vocals.mean(axis=1) if vocals.ndim == 2 else vocals
    duration, bar = len(mono) / sr, bar_seconds(bpm_norm)
    full = complete_bars(duration, bpm_norm, t_ref)
    features = MixFeatures(sr)
    bar_power, vocal_power = BarPower(full, bar, sr, len(mono)), BarPower(full, bar, sr, len(voc))
    for s, e in chunk_ranges(len(mono)):
        features.push(mono[s:e])
        bar_power.push(mono[s:e])
        vocal_power.push(voc[s:e])
    bands, centers, mean_power = features.result()
    inputs = StructureInputs(duration, bands, centers, mean_power, bar_power.result(), vocal_power.result())
    return sections_from(inputs, bpm_norm, t_ref)
