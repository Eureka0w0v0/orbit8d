"""外部数据集的下载与校验：uv run python -m orbit8d.assets"""

import logging
import sys
from pathlib import Path

from orbit8d.config import load_settings
from orbit8d.download import download, sha256_of
from orbit8d.engine.reverb import BRIR_VERSION
from orbit8d.separate.model import model_files

log = logging.getLogger(__name__)

HRTF_URL = "https://sofacoustics.org/data/database/thk/HRIR_FULL2DEG.sofa"
HRTF_FILE = "HRIR_FULL2DEG.sofa"
HRTF_SHA256 = "d3671e6829323b93b7ae95aff6d9316d15d6c128b7f2ab615c21cc3ab085ddb7"


def ensure_hrtf_sofa(assets_dir: Path) -> Path:
    """返回校验通过的 SOFA 文件路径；缺失或损坏时重新下载。"""
    target = assets_dir / HRTF_FILE
    if target.exists() and sha256_of(target) == HRTF_SHA256:
        return target
    log.info("downloading hrtf", extra={"event": "asset.download", "url": HRTF_URL})
    download(HRTF_URL, target, HRTF_SHA256)
    return target


def prune_stale_assets(assets_dir: Path) -> int:
    """删掉不再使用的旧缓存：全局 EQ（已改为按项目、按场景）、旧版本的 BRIR、没写完的临时文件。
    返回删了几个。"""
    current = f"brir_v{BRIR_VERSION}_"
    stale = [
        path
        for path in assets_dir.glob("*")
        if path.is_file()
        and (
            path.name.startswith("eq_")
            or (path.name.startswith("brir_") and not path.name.startswith(current))
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
