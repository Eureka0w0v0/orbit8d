"""轨道公式：场景参数 + 时间 → (方位角, 仰角, 距离)。

约定见 docs/SPEC.md §4：方位角顺时针（0 正前、90 正右），仰角向上为正，单位度；
距离单位米。与 web/src/orbit/orbit.ts 逐点一致（shared/golden/orbit_vectors.json）。
"""

from dataclasses import dataclass

import numpy as np

SHAPES = ("circle", "ellipse", "pendulum", "figure8", "spiral", "fixed")
BEATS_PER_BAR = 4
BPM_NORM_LOW = 70.0
BPM_NORM_HIGH = 140.0
SPIRAL_TURNS_PER_CYCLE = 4.0     # 螺旋：每 4 圈完成一次升降


@dataclass(frozen=True)
class OrbitParams:
    shape: str
    radius_m: float
    period_s: float
    direction: int               # +1 顺时针，-1 逆时针
    start_deg: float
    height_deg: float
    pitch_deg: float
    roll_deg: float
    yaw_deg: float
    aspect: float
    swing_deg: float
    lift_deg: float

    def __post_init__(self) -> None:
        if self.shape not in SHAPES:
            raise ValueError(f"未知形状: {self.shape}")
        if self.direction not in (1, -1):
            raise ValueError(f"direction 只能是 ±1: {self.direction}")
        if self.period_s <= 0:
            raise ValueError(f"period_s 必须为正: {self.period_s}")


def normalize_bpm(bpm: float) -> float:
    """把 BPM 通过 ×2 / ÷2 归一到 [70, 140)，让“小节”在不同测速倍频下含义一致。"""
    if bpm <= 0:
        raise ValueError(f"BPM 必须为正: {bpm}")
    while bpm < BPM_NORM_LOW:
        bpm *= 2.0
    while bpm >= BPM_NORM_HIGH:
        bpm /= 2.0
    return bpm


def period_seconds(mode: str, bars: int, seconds: float, bpm_norm: float) -> float:
    if mode == "bars":
        return bars * BEATS_PER_BAR * 60.0 / bpm_norm
    if mode == "seconds":
        return float(seconds)
    raise ValueError(f"未知速度模式: {mode}")


def _shape_angles(p: OrbitParams, phi: np.ndarray):
    """局部坐标下的 (az, el, dist)，phi 单位度。"""
    rad = np.radians(phi)
    dist = np.full_like(phi, p.radius_m)
    el = np.full_like(phi, p.height_deg)
    if p.shape in ("circle", "fixed"):
        az = phi
    elif p.shape == "ellipse":
        az = np.degrees(np.arctan2(np.sin(rad), p.aspect * np.cos(rad)))
        dist = p.radius_m * np.sqrt(np.sin(rad) ** 2 + (p.aspect * np.cos(rad)) ** 2)
    elif p.shape == "pendulum":
        az = p.swing_deg * np.sin(rad)
    elif p.shape == "figure8":
        az = p.swing_deg * np.sin(rad)
        el = p.height_deg + p.lift_deg * np.sin(2.0 * rad)
    else:  # spiral
        az = phi
        el = p.height_deg + p.lift_deg * np.sin(rad / SPIRAL_TURNS_PER_CYCLE)
    return az, el, dist


def orbit_position(p: OrbitParams, t, t_ref: float, offset_deg: float = 0.0):
    """返回与 t 同形状的 (az[0,360), el[-90,90], dist) 三个数组。"""
    t = np.asarray(t, dtype=np.float64)
    if p.shape == "fixed":
        phi = np.full_like(t, p.start_deg + offset_deg)
    else:
        phi = p.start_deg + offset_deg + p.direction * 360.0 * (t - t_ref) / p.period_s
    az, el, dist = _shape_angles(p, phi)

    a, e = np.radians(az), np.radians(el)
    x, y, z = np.cos(e) * np.sin(a), np.sin(e), np.cos(e) * np.cos(a)
    pr, rr, wr = np.radians(p.pitch_deg), np.radians(p.roll_deg), np.radians(p.yaw_deg)
    y, z = y * np.cos(pr) + z * np.sin(pr), -y * np.sin(pr) + z * np.cos(pr)
    x, y = x * np.cos(rr) - y * np.sin(rr), x * np.sin(rr) + y * np.cos(rr)
    x, z = x * np.cos(wr) + z * np.sin(wr), -x * np.sin(wr) + z * np.cos(wr)

    az_out = np.mod(np.degrees(np.arctan2(x, z)), 360.0)
    el_out = np.degrees(np.arcsin(np.clip(y, -1.0, 1.0)))
    return az_out, el_out, dist
