"""段落识别（SPEC §13.4）：小节网格 → 每小节的音色 + 人声特征 → 自相似矩阵上的新颖度 → 分界（落在小节线上）
→ 按能量与人声贴标签（前奏 / 主歌 / 副歌 / 桥段 / 尾声）。

启发式，只求大致对：用户可以在时间轴上拖分界、改标签；标签只决定自动编排给这一段配哪种轨道。
"""

from dataclasses import asdict, dataclass

import numpy as np

from orbit8d.engine.orbit import BEATS_PER_BAR

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


def loudness_envelope(x: np.ndarray, sr: int, hop_s: float = ENVELOPE_HOP_S) -> list[float]:
    """每 hop_s 秒的平均功率（立体声两声道相加），相对全曲最响处的 dB，最低 -60，保留 1 位小数。"""
    power = np.asarray(x, dtype=np.float64) ** 2
    if power.ndim == 2:
        power = power.sum(axis=1)
    hop = max(1, int(round(hop_s * sr)))
    n = -(-len(power) // hop)
    padded = np.zeros(n * hop)
    padded[: len(power)] = power
    frames = padded.reshape(n, hop).mean(axis=1)
    peak = float(frames.max()) if n else 0.0
    if peak <= EPS:
        return [ENVELOPE_FLOOR_DB] * n
    db = np.maximum(10 * np.log10(np.maximum(frames, EPS) / peak), ENVELOPE_FLOOR_DB)
    return [round(float(v), 1) for v in db]


def bar_seconds(bpm_norm: float) -> float:
    return BEATS_PER_BAR * 60.0 / bpm_norm


def bar_starts(duration_s: float, bpm_norm: float, t_ref: float) -> np.ndarray:
    """与 t_ref（正前方对齐的拍）同相位的小节线；第一条在 [0, 一小节) 内。"""
    bar = bar_seconds(bpm_norm)
    return np.arange(t_ref % bar, duration_s, bar)


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


def _per_bar_mean(values: np.ndarray, times: np.ndarray, starts: np.ndarray, bar: float) -> np.ndarray:
    k = np.searchsorted(starts, times, side="right") - 1
    ok = (k >= 0) & (times < starts[np.clip(k, 0, None)] + bar)
    sums = np.zeros((len(starts), values.shape[1]))
    np.add.at(sums, k[ok], values[ok])
    counts = np.bincount(k[ok], minlength=len(starts))
    return sums / np.maximum(counts, 1)[:, None]


def _bar_power(x: np.ndarray, sr: int, starts: np.ndarray, bar: float) -> np.ndarray:
    cum = np.concatenate([[0.0], np.cumsum(np.asarray(x, dtype=np.float64) ** 2)])
    a = np.round(starts * sr).astype(int)
    b = np.minimum(np.round((starts + bar) * sr).astype(int), len(x))
    return (cum[b] - cum[a]) / np.maximum(b - a, 1)


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


def detect_sections(
    mix: np.ndarray, vocals: np.ndarray, sr: int, bpm_norm: float, t_ref: float
) -> list[SectionInfo]:
    """mix / vocals 可以是单声道或 (n, 2)。第一段从 0 秒开始，其余分界都在小节线上。"""
    mono = mix.mean(axis=1) if mix.ndim == 2 else mix
    voc = vocals.mean(axis=1) if vocals.ndim == 2 else vocals
    duration, bar = len(mono) / sr, bar_seconds(bpm_norm)
    starts = bar_starts(duration, bpm_norm, t_ref)
    full = starts[starts + bar <= duration + EPS]  # 只用完整小节
    energy = 10 * np.log10(_bar_power(mono, sr, full, bar) + EPS) if len(full) else np.zeros(0)
    if len(full) < 2 * MIN_SECTION_BARS:
        total = 10 * np.log10(float(np.mean(mono.astype(np.float64) ** 2)) + EPS)
        return [SectionInfo(0.0, len(full), WHOLE, round(total, 2))]
    bands, centers = frame_bands(mono, sr)
    vocal = 10 * np.log10(_bar_power(voc, sr, full, bar) + EPS)
    features = np.hstack(
        [
            _zscore(np.log10(_per_bar_mean(bands, centers, full, bar) + EPS)),
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
