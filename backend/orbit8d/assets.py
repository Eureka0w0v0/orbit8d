"""外部数据集的下载与校验：uv run python -m orbit8d.assets"""

import hashlib
import logging
import urllib.request
from pathlib import Path

from orbit8d.config import load_settings

log = logging.getLogger(__name__)

HRTF_URL = "https://sofacoustics.org/data/database/thk/HRIR_FULL2DEG.sofa"
HRTF_FILE = "HRIR_FULL2DEG.sofa"
HRTF_SHA256 = "d3671e6829323b93b7ae95aff6d9316d15d6c128b7f2ab615c21cc3ab085ddb7"
DOWNLOAD_TIMEOUT_S = 120
CHUNK = 1 << 20


class AssetError(RuntimeError):
    pass


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def ensure_hrtf_sofa(assets_dir: Path) -> Path:
    """返回校验通过的 SOFA 文件路径；缺失或损坏时重新下载。"""
    target = assets_dir / HRTF_FILE
    if target.exists() and sha256_of(target) == HRTF_SHA256:
        return target
    assets_dir.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(".part")
    log.info("downloading hrtf", extra={"event": "asset.download", "url": HRTF_URL})
    try:
        with urllib.request.urlopen(HRTF_URL, timeout=DOWNLOAD_TIMEOUT_S) as resp, part.open("wb") as out:
            for block in iter(lambda: resp.read(CHUNK), b""):
                out.write(block)
        digest = sha256_of(part)
        if digest != HRTF_SHA256:
            raise AssetError(f"HRTF 校验失败：期望 {HRTF_SHA256}，实际 {digest}")
        part.replace(target)
    finally:
        part.unlink(missing_ok=True)
    return target


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    path = ensure_hrtf_sofa(load_settings().assets_dir)
    print(f"HRTF ready: {path}")


if __name__ == "__main__":
    main()
