"""HRTF 网格：从 SOFA 加载 → 顺时针方位索引 → 重采样 → 归一；双线性插值；前端用的二进制格式。

约定见 docs/SPEC.md §4.1 与 §5.6。data[el, az, ear, tap]，ear 0 = 左耳。
"""

import json
import struct
from dataclasses import dataclass
from pathlib import Path

import h5py
import numpy as np
from scipy.signal import resample_poly

MAGIC = b"O8DH"
FORMAT_VERSION = 1
TAPS = 128
AZ_TOLERANCE_DEG = 1e-3


@dataclass(frozen=True)
class HrtfGrid:
    sample_rate: int
    az_step_deg: float
    el_nodes: np.ndarray  # (n_el,) 升序，单位度
    data: np.ndarray  # (n_el, n_az, 2, taps) float32

    @property
    def n_el(self) -> int:
        return self.data.shape[0]

    @property
    def n_az(self) -> int:
        return self.data.shape[1]

    @property
    def taps(self) -> int:
        return self.data.shape[3]


def _left_ear_first(receivers: np.ndarray, ir: np.ndarray) -> np.ndarray:
    """SOFA 坐标 +y 指向左侧；保证 ear 0 是左耳。"""
    y = receivers.reshape(receivers.shape[0], -1)[:, 1]
    return ir if y[0] > y[1] else ir[:, ::-1, :]


def from_sofa(path: Path, sample_rate: int) -> HrtfGrid:
    with h5py.File(path, "r") as f:
        ir = np.array(f["Data.IR"], dtype=np.float64)  # (M, 2, N)
        fs = int(round(float(np.array(f["Data.SamplingRate"]).ravel()[0])))
        pos = np.array(f["SourcePosition"], dtype=np.float64)
        receivers = np.array(f["ReceiverPosition"], dtype=np.float64)
    ir = _left_ear_first(receivers, ir)

    el_nodes = np.unique(np.round(pos[:, 1], 6))
    az_ccw = np.mod(np.round(pos[:, 0], 6), 360.0)
    az_cw = np.mod(360.0 - az_ccw, 360.0)
    az_values = np.unique(np.round(az_cw, 6))
    steps = np.diff(az_values)
    if not len(steps) or np.ptp(steps) > AZ_TOLERANCE_DEG or len(az_values) * len(el_nodes) != len(pos):
        raise ValueError(f"{path.name} 不是规整的方位×仰角网格")
    step = float(steps[0])

    el_idx = np.searchsorted(el_nodes, np.round(pos[:, 1], 6))
    az_idx = np.round(az_cw / step).astype(int) % len(az_values)
    grid = np.zeros((len(el_nodes), len(az_values), 2, ir.shape[-1]))
    grid[el_idx, az_idx] = ir

    g = np.gcd(sample_rate, fs)
    grid = resample_poly(grid, sample_rate // g, fs // g, axis=-1)
    grid = (
        grid[..., :TAPS]
        if grid.shape[-1] >= TAPS
        else np.pad(grid, [(0, 0)] * 3 + [(0, TAPS - grid.shape[-1])])
    )

    row = int(np.argmin(np.abs(el_nodes)))
    grid /= np.sqrt((grid[row] ** 2).sum(-1).mean())  # 水平一圈平均每耳能量 = 1
    return HrtfGrid(
        sample_rate=sample_rate,
        az_step_deg=step,
        el_nodes=el_nodes.astype(np.float64),
        data=grid.astype(np.float32),
    )


def interp_weights(grid: HrtfGrid, az: np.ndarray, el: np.ndarray):
    """返回 (i0, i1, j0, j1, t_el, t_az)，用于在 (el, az) 网格上双线性插值。"""
    az = np.mod(np.asarray(az, dtype=np.float64), 360.0)
    el = np.asarray(el, dtype=np.float64)
    nodes = grid.el_nodes
    i0 = np.clip(np.searchsorted(nodes, el, side="right") - 1, 0, grid.n_el - 2)
    i1 = i0 + 1
    t_el = np.clip((el - nodes[i0]) / (nodes[i1] - nodes[i0]), 0.0, 1.0)
    u = az / grid.az_step_deg
    j0f = np.floor(u)
    t_az = u - j0f
    j0 = j0f.astype(int) % grid.n_az
    j1 = (j0 + 1) % grid.n_az
    return i0, i1, j0, j1, t_el, t_az


def blend(table: np.ndarray, weights) -> np.ndarray:
    """table 的前两维是 (el, az)；按插值权重混合，保留其余维度。"""
    i0, i1, j0, j1, t_el, t_az = weights
    shape = (-1,) + (1,) * (table.ndim - 2)
    te, ta = t_el.reshape(shape), t_az.reshape(shape)
    return (
        (1 - te) * (1 - ta) * table[i0, j0]
        + (1 - te) * ta * table[i0, j1]
        + te * (1 - ta) * table[i1, j0]
        + te * ta * table[i1, j1]
    )


def interpolate(grid: HrtfGrid, az, el) -> np.ndarray:
    """(n, 2, taps)：每个方向的左右耳 HRIR。"""
    return blend(grid.data, interp_weights(grid, az, el)).astype(np.float32)


def to_bytes(grid: HrtfGrid) -> bytes:
    header = json.dumps(
        {
            "version": FORMAT_VERSION,
            "sample_rate": grid.sample_rate,
            "taps": grid.taps,
            "az_step_deg": grid.az_step_deg,
            "n_az": grid.n_az,
            "el_nodes": grid.el_nodes.tolist(),
            "layout": "el,az,ear,tap",
        }
    ).encode()
    pad = (-len(header)) % 4  # 数据段 4 字节对齐
    header += b" " * pad
    return MAGIC + struct.pack("<I", len(header)) + header + grid.data.astype("<f4").tobytes()


def from_bytes(blob: bytes) -> HrtfGrid:
    if blob[:4] != MAGIC:
        raise ValueError("不是 Orbit 8D 的 HRTF 数据")
    (hlen,) = struct.unpack("<I", blob[4:8])
    meta = json.loads(blob[8 : 8 + hlen])
    if meta.get("version") != FORMAT_VERSION:
        raise ValueError(f"不支持的 HRTF 数据版本: {meta.get('version')}")
    nodes = np.array(meta["el_nodes"], dtype=np.float64)
    data = np.frombuffer(blob[8 + hlen :], dtype="<f4").reshape(len(nodes), meta["n_az"], 2, meta["taps"])
    return HrtfGrid(
        sample_rate=meta["sample_rate"],
        az_step_deg=meta["az_step_deg"],
        el_nodes=nodes,
        data=data.astype(np.float32),
    )


def load_grid(sofa_path: Path, cache_dir: Path, sample_rate: int) -> HrtfGrid:
    """优先读缓存的二进制表；没有就从 SOFA 生成并缓存（原子写）。"""
    cache = cache_dir / f"hrtf_{sample_rate}.bin"
    if cache.exists():
        return from_bytes(cache.read_bytes())
    grid = from_sofa(sofa_path, sample_rate)
    tmp = cache.with_suffix(".tmp")
    tmp.write_bytes(to_bytes(grid))
    tmp.replace(cache)
    return grid
