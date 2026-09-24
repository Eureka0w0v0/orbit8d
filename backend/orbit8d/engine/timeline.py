"""时间轴运动（SPEC §13.3）：导出与浏览器试听逐点一致（web/src/orbit/timeline.ts）。

- 相位：按“当前段的角速度 × 停顿系数”从 0 秒积分，t_ref 处为 0；角速度分段线性，积分有精确公式。
- 段落过渡：分界前后各半小节（不超过相邻两段较短者的 40%），前后两段的方向做球面插值、距离线性插值。
- 停顿：0.5 秒减速到 0 → 停住 duration → 0.5 秒加速回来（仅作用于目标音轨）。
- 飞过头顶：事件期间方向按 sin² 钟形权重朝正上方插值，中点到达头顶。
- 固定声源的相位 = 起点 + 偏移，不累积其他段的转动。
"""

from dataclasses import dataclass

import numpy as np

from orbit8d.engine.orbit import BEATS_PER_BAR, OrbitParams, position_at_phase
from orbit8d.engine.scene import HOLD_RAMP_S, Scene, orbit_params

TRANSITION_BARS = 1.0
MAX_TRANSITION_SHARE = 0.4
PARALLEL_EPS = 1e-6
UP = np.array([0.0, 1.0, 0.0])
FALLBACK_AXIS = np.array([1.0, 0.0, 0.0])


@dataclass(frozen=True)
class SectionMotion:
    start_s: float
    params: OrbitParams
    omega: float  # 度/秒；固定形状为 0


@dataclass(frozen=True)
class TrackMotion:
    sections: tuple[SectionMotion, ...]
    starts: np.ndarray
    half: np.ndarray  # half[k]：第 k 段开头那条分界的过渡半宽（half[0] 不用）
    seg_t0: np.ndarray  # 相位积分分段起点（升序，首项 0）
    seg_f0: np.ndarray  # 各分段起点处的积分值 F(t0)
    seg_w0: np.ndarray  # 各分段起点处的角速度
    seg_slope: np.ndarray  # 分段内角速度的斜率
    f_ref: float  # F(t_ref)
    overheads: tuple[tuple[float, float], ...]


def _half_widths(starts: np.ndarray, bpm_norm: float, duration_s: float) -> np.ndarray:
    bar = BEATS_PER_BAR * 60.0 / bpm_norm
    ends = np.append(starts[1:], max(duration_s, starts[-1]))
    lengths = ends - starts
    half = np.zeros(len(starts))
    for k in range(1, len(starts)):
        half[k] = max(
            0.0, min(TRANSITION_BARS * bar / 2, MAX_TRANSITION_SHARE * min(lengths[k - 1], lengths[k]))
        )
    return half


def _hold_factor(t: float, holds: list[tuple[float, float]]) -> float:
    for ts, d in holds:
        if ts <= t <= ts + 2 * HOLD_RAMP_S + d:
            if t < ts + HOLD_RAMP_S:
                return 1.0 - (t - ts) / HOLD_RAMP_S
            if t <= ts + HOLD_RAMP_S + d:
                return 0.0
            return (t - (ts + HOLD_RAMP_S + d)) / HOLD_RAMP_S
    return 1.0


def compile_track(scene: Scene, track: str, bpm_norm: float, t_ref: float, duration_s: float) -> TrackMotion:
    sections = []
    for sec in scene.sections:
        p = orbit_params(sec.orbits[track], bpm_norm)
        omega = 0.0 if p.shape == "fixed" else p.direction * 360.0 / p.period_s
        sections.append(SectionMotion(sec.start_s, p, omega))
    starts = np.array([s.start_s for s in sections])
    holds = sorted((e.t_s, e.duration_s) for e in scene.events if e.kind == "hold" and track in e.targets)
    overheads = tuple(
        sorted((e.t_s, e.duration_s) for e in scene.events if e.kind == "overhead" and track in e.targets)
    )

    points = {0.0, *starts[1:].tolist()}
    for ts, d in holds:
        points.update({ts, ts + HOLD_RAMP_S, ts + HOLD_RAMP_S + d, ts + 2 * HOLD_RAMP_S + d})
    t0 = np.array(sorted(p for p in points if p >= 0.0))
    t1 = np.append(t0[1:], np.inf)

    def omega_at(t: float) -> float:
        k = int(np.searchsorted(starts, t, side="right") - 1)
        return sections[max(k, 0)].omega

    w0 = np.array([omega_at(a) * _hold_factor(a, holds) for a in t0])
    w1 = np.array(
        [
            omega_at(a) * _hold_factor(b, holds) if np.isfinite(b) else w0[i]
            for i, (a, b) in enumerate(zip(t0, t1, strict=True))
        ]
    )
    span = t1 - t0
    slope = np.where(np.isfinite(span), (w1 - w0) / np.where(np.isfinite(span), span, 1.0), 0.0)
    f0 = np.zeros(len(t0))
    for i in range(1, len(t0)):
        d = t0[i] - t0[i - 1]
        f0[i] = f0[i - 1] + w0[i - 1] * d + slope[i - 1] * d * d / 2
    motion = TrackMotion(
        tuple(sections), starts, _half_widths(starts, bpm_norm, duration_s), t0, f0, w0, slope, 0.0, overheads
    )
    f_ref = float(_integral(motion, np.array([t_ref]))[0])
    return TrackMotion(tuple(sections), starts, motion.half, t0, f0, w0, slope, f_ref, overheads)


def _integral(m: TrackMotion, t: np.ndarray) -> np.ndarray:
    i = np.clip(np.searchsorted(m.seg_t0, t, side="right") - 1, 0, len(m.seg_t0) - 1)
    d = t - m.seg_t0[i]
    return m.seg_f0[i] + m.seg_w0[i] * d + m.seg_slope[i] * d * d / 2


def phase(m: TrackMotion, t: np.ndarray) -> np.ndarray:
    """累积相位（度），t_ref 处为 0。"""
    return _integral(m, np.asarray(t, dtype=np.float64)) - m.f_ref


def _to_vec(az: np.ndarray, el: np.ndarray) -> np.ndarray:
    a, e = np.radians(az), np.radians(el)
    return np.stack([np.cos(e) * np.sin(a), np.sin(e), np.cos(e) * np.cos(a)], axis=-1)


def slerp(a: np.ndarray, b: np.ndarray, w: np.ndarray) -> np.ndarray:
    """逐行球面插值；几乎同向时退化为归一化线性插值，正好相反时绕一条垂直轴转过去。"""
    w = w[:, None]
    dot = np.clip((a * b).sum(-1, keepdims=True), -1.0, 1.0)
    out = np.empty_like(a)
    near = (dot > 1 - PARALLEL_EPS)[:, 0]
    anti = (dot < -1 + PARALLEL_EPS)[:, 0]
    mid = ~(near | anti)
    if near.any():
        v = (1 - w[near]) * a[near] + w[near] * b[near]
        out[near] = v / np.linalg.norm(v, axis=-1, keepdims=True)
    if anti.any():
        perp = np.cross(a[anti], UP)
        small = np.linalg.norm(perp, axis=-1) < PARALLEL_EPS
        perp[small] = np.cross(a[anti][small], FALLBACK_AXIS)
        perp /= np.linalg.norm(perp, axis=-1, keepdims=True)
        out[anti] = np.cos(np.pi * w[anti]) * a[anti] + np.sin(np.pi * w[anti]) * perp
    if mid.any():
        omega = np.arccos(dot[mid])
        s = np.sin(omega)
        out[mid] = (np.sin((1 - w[mid]) * omega) * a[mid] + np.sin(w[mid] * omega) * b[mid]) / s
    return out


def _smoothstep(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def _blend_plan(starts: np.ndarray, half: np.ndarray, t: np.ndarray):
    """每个时刻参与混合的两段 (A, B) 与权重 w（w=0 表示只用 A）。"""
    n = len(starts)
    k = np.clip(np.searchsorted(starts, t, side="right") - 1, 0, n - 1)
    a, b, w = k.copy(), k.copy(), np.zeros_like(t)
    nxt = np.minimum(k + 1, n - 1)
    h_next, b_next = half[nxt], starts[nxt]
    to_next = (k + 1 < n) & (h_next > 0) & (t >= b_next - h_next)
    b[to_next] = k[to_next] + 1
    w[to_next] = _smoothstep((t[to_next] - (b_next[to_next] - h_next[to_next])) / (2 * h_next[to_next]))
    h_prev, b_prev = half[k], starts[k]
    from_prev = ~to_next & (k >= 1) & (h_prev > 0) & (t < b_prev + h_prev)
    a[from_prev] = k[from_prev] - 1
    w[from_prev] = _smoothstep(
        (t[from_prev] - (b_prev[from_prev] - h_prev[from_prev])) / (2 * h_prev[from_prev])
    )
    return a, b, w


def track_position(m: TrackMotion, t, offset_deg: float = 0.0):
    """返回与 t 同形状的 (az[0,360), el, dist)。"""
    t = np.asarray(t, dtype=np.float64)
    shape = t.shape
    t = t.reshape(-1)
    phi_all = phase(m, t)
    a, b, w = _blend_plan(m.starts, m.half, t)
    vec_a, vec_b = np.zeros((len(t), 3)), np.zeros((len(t), 3))
    dist_a, dist_b = np.zeros(len(t)), np.zeros(len(t))
    for j, sec in enumerate(m.sections):
        for sel, vec, dist in ((a == j, vec_a, dist_a), (b == j, vec_b, dist_b)):
            if not sel.any():
                continue
            if sec.params.shape == "fixed":
                phi = np.full(int(sel.sum()), sec.params.start_deg + offset_deg)
            else:
                phi = sec.params.start_deg + offset_deg + phi_all[sel]
            az, el, d = position_at_phase(sec.params, phi)
            vec[sel] = _to_vec(az, el)
            dist[sel] = d
    vec = slerp(vec_a, vec_b, w)
    dist = (1 - w) * dist_a + w * dist_b
    for ts, d in m.overheads:
        sel = (t >= ts) & (t <= ts + d)
        if sel.any():
            e = np.sin(np.pi * (t[sel] - ts) / d) ** 2
            vec[sel] = slerp(vec[sel], np.broadcast_to(UP, (int(sel.sum()), 3)).copy(), e)
    az = np.mod(np.degrees(np.arctan2(vec[:, 0], vec[:, 2])), 360.0)
    el = np.degrees(np.arcsin(np.clip(vec[:, 1], -1.0, 1.0)))
    return az.reshape(shape), el.reshape(shape), dist.reshape(shape)


def wet_db_curve(scene: Scene, bpm_norm: float, duration_s: float, t) -> np.ndarray:
    """每个时刻送进混响的增益（dB），段落交界处与轨道同步平滑过渡。"""
    t = np.asarray(t, dtype=np.float64)
    starts = np.array([s.start_s for s in scene.sections])
    wets = np.array([s.wet_db for s in scene.sections])
    a, b, w = _blend_plan(starts, _half_widths(starts, bpm_norm, duration_s), t.reshape(-1))
    return ((1 - w) * wets[a] + w * wets[b]).reshape(t.shape)
