"""测试公共夹具：假分轨器（按频段粗分四轨，避免在测试里跑 Demucs）、隔离的数据目录、API 客户端。"""

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import h5py
import numpy as np
import pytest
import soundfile as sf
from scipy.signal import butter, sosfiltfilt

from orbit8d.assets import HRTF_FILE
from orbit8d.config import load_settings

REAL_ASSETS = load_settings().assets_dir
SR = 44100


class FakeSeparator:
    """人声 = 300–3000 Hz，贝斯 = <150 Hz，鼓 = >5 kHz，其他 = 剩余部分。"""

    def __init__(self) -> None:
        self.calls = 0
        self.model_checks = 0

    def ensure_model(self, on_progress: Callable[[float], None]) -> None:
        self.model_checks += 1  # 假分轨器不需要模型，只记下被问过

    def separate(self, wav: Path, out_dir: Path, on_progress: Callable[[float], None]) -> None:
        self.calls += 1
        x, sr = sf.read(wav, always_2d=True)
        vocals = sosfiltfilt(butter(4, [300, 3000], "bandpass", fs=sr, output="sos"), x, axis=0)
        bass = sosfiltfilt(butter(4, 150, "lowpass", fs=sr, output="sos"), x, axis=0)
        drums = sosfiltfilt(butter(4, 5000, "highpass", fs=sr, output="sos"), x, axis=0)
        other = x - vocals - bass - drums
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, data in {"vocals": vocals, "bass": bass, "drums": drums, "other": other}.items():
            sf.write(out_dir / f"{name}.wav", data, sr, subtype="FLOAT")
        on_progress(1.0)


def write_song(path: Path, seconds: float = 6.0, seed: int = 0) -> Path:
    """合成一首“歌”：中置旋律 + 90 BPM 鼓点 + 贝斯 + 宽立体声噪声。"""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    melody = 0.2 * np.sin(2 * np.pi * 440 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 0.5 * t))
    beat = (np.mod(t, 60 / 90) < 0.03).astype(float) * rng.standard_normal(n) * 0.3
    bass = 0.25 * np.sin(2 * np.pi * 55 * t)
    x = np.stack([melody + beat + bass, melody + beat + bass], axis=1) + rng.standard_normal((n, 2)) * 0.02
    sf.write(path, x, SR, subtype="PCM_16")
    return path


SOFA_ELEVATIONS = (-30.0, 0.0, 30.0)
SOFA_AZ_STEP = 45.0


def write_sofa(
    path: Path, *, left_ear_first: bool = True, taps: int = 16, skip: int = 0, seed: int = 0
) -> tuple[np.ndarray, np.ndarray]:
    """合成一个很小的 SOFA 文件（HDF5）：3 档仰角 × 8 档方位（SOFA 的方位是逆时针），各方向的先后打乱，
    采样率 44.1 kHz。skip > 0 时少写几个方向（不再是规整网格）；left_ear_first=False 时文件里先放右耳，
    内容不变。返回 (各方向的 [方位, 仰角, 距离], 冲激响应 [方向, 左右耳, 采样])。"""
    rng = np.random.default_rng(seed)
    positions = np.array([(k * SOFA_AZ_STEP, el, 1.5) for el in SOFA_ELEVATIONS for k in range(8)])
    positions = positions[rng.permutation(len(positions))]
    positions = positions[: len(positions) - skip]
    ir = rng.standard_normal((len(positions), 2, taps))
    ears = np.array([[0.0, 0.09, 0.0], [0.0, -0.09, 0.0]])  # +y 指向左侧
    with h5py.File(path, "w") as f:
        f["Data.IR"] = ir if left_ear_first else ir[:, ::-1]
        f["Data.SamplingRate"] = np.array([float(SR)])
        f["SourcePosition"] = positions
        f["ReceiverPosition"] = ears if left_ear_first else ears[::-1]
    return positions, ir


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """每个测试一个独立数据目录；HRTF 资源以软链接复用真实下载。"""
    if not (REAL_ASSETS / HRTF_FILE).exists():
        pytest.skip("需要先运行 make assets")
    assets = tmp_path / "data" / "assets"
    assets.mkdir(parents=True)
    for item in REAL_ASSETS.iterdir():
        if item.suffix in (".sofa", ".bin"):
            os.symlink(item, assets / item.name)
    monkeypatch.setenv("ORBIT8D_DATA_DIR", str(tmp_path / "data"))
    yield tmp_path / "data"
