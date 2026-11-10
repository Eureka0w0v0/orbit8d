"""外部数据集的下载与校验，以及由它们算出来的缓存文件（HRTF 表、混响冲激响应）。

uv run python -m orbit8d.assets
"""

import logging
import sys
from collections.abc import Callable
from pathlib import Path

import numpy as np
import soundfile as sf

from orbit8d.config import load_settings
from orbit8d.download import download, sha256_of
from orbit8d.engine.hrtf import HrtfGrid, from_bytes, from_sofa, to_bytes
from orbit8d.engine.reverb import BRIR_VERSION
from orbit8d.media.sofa import read_sofa
from orbit8d.separate.model import model_files

log = logging.getLogger(__name__)

HRTF_URL = "https://sofacoustics.org/data/database/thk/HRIR_FULL2DEG.sofa"
HRTF_FILE = "HRIR_FULL2DEG.sofa"
HRTF_SHA256 = "d3671e6829323b93b7ae95aff6d9316d15d6c128b7f2ab615c21cc3ab085ddb7"
BRIR_PREFIX = f"brir_v{BRIR_VERSION}_"  # 缓存文件名带算法版本：算法变了，旧文件就成了“不再使用的缓存”


def ensure_hrtf_sofa(assets_dir: Path) -> Path:
    """返回校验通过的 SOFA 文件路径；缺失或损坏时重新下载。"""
    target = assets_dir / HRTF_FILE
    if target.exists() and sha256_of(target) == HRTF_SHA256:
        return target
    log.info("downloading hrtf", extra={"event": "asset.download", "url": HRTF_URL})
    download(HRTF_URL, target, HRTF_SHA256)
    return target


def load_hrtf_grid(sofa_path: Path, cache_dir: Path, sample_rate: int) -> HrtfGrid:
    """优先读缓存的二进制表；没有就从 SOFA 生成并缓存（原子写）。"""
    cache = cache_dir / f"hrtf_{sample_rate}.bin"
    if cache.exists():
        return from_bytes(cache.read_bytes())
    grid = from_sofa(read_sofa(sofa_path), sample_rate)
    tmp = cache.with_suffix(".tmp")
    tmp.write_bytes(to_bytes(grid))
    tmp.replace(cache)
    return grid


def cached_brir(assets_dir: Path, room: str, sample_rate: int, make: Callable[[], np.ndarray]) -> Path:
    """这个房间的混响冲激响应缓存文件（浏览器试听下载它）；没有就用 make() 生成，先写临时文件再改名。"""
    path = assets_dir / f"{BRIR_PREFIX}{room}_{sample_rate}.wav"
    if not path.exists():
        tmp = path.with_suffix(".tmp.wav")
        sf.write(tmp, make(), sample_rate, subtype="FLOAT")
        tmp.replace(path)
    return path


def prune_stale_assets(assets_dir: Path) -> int:
    """删掉不再使用的旧缓存：全局 EQ（已改为按项目、按场景）、旧版本的 BRIR、没写完的临时文件。
    返回删了几个。"""
    stale = [
        path
        for path in assets_dir.glob("*")
        if path.is_file()
        and (
            path.name.startswith("eq_")
            or (path.name.startswith("brir_") and not path.name.startswith(BRIR_PREFIX))
            or ".tmp" in path.name
            or path.suffix == ".part"
        )
    ]
    for path in stale:
        path.unlink()
        log.info("stale asset removed", extra={"event": "asset.prune", "file": path.name})
    return len(stale)


def main() -> None:
    """make assets：HRTF 数据 + 分轨模型（已在 HuggingFace 缓存里就不重下）。"""
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    print(f"HRTF ready: {ensure_hrtf_sofa(settings.assets_dir)}")

    def show(fraction: float) -> None:
        sys.stdout.write(f"\rDownloading the Demucs model: {fraction:6.1%}")
        sys.stdout.flush()

    paths = model_files(settings.assets_dir, show)
    print(f"\nDemucs model ready: {sorted({str(p.parent) for p in paths.values()})}")


if __name__ == "__main__":
    main()
