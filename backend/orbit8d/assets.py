"""外部数据集的下载与校验：uv run python -m orbit8d.assets"""

import hashlib
import logging
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

from orbit8d.config import load_settings
from orbit8d.engine.reverb import BRIR_VERSION
from orbit8d.errors import CodedError

log = logging.getLogger(__name__)

HRTF_URL = "https://sofacoustics.org/data/database/thk/HRIR_FULL2DEG.sofa"
HRTF_FILE = "HRIR_FULL2DEG.sofa"
HRTF_SHA256 = "d3671e6829323b93b7ae95aff6d9316d15d6c128b7f2ab615c21cc3ab085ddb7"
DOWNLOAD_TIMEOUT_S = 60  # 单次网络读写的超时（不是整个下载的总时长）
CHUNK = 1 << 20


class AssetError(CodedError):
    def __init__(self, message: str):
        super().__init__("ASSET_DOWNLOAD_FAILED", message)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def download(url: str, target: Path, sha256: str, on_bytes: Callable[[int], None] | None = None) -> None:
    """下载到 target 旁的 .part（边下边报已下载的字节数），sha256 校验通过才改名；失败不留半截文件。
    网络错误照常抛出（URLError / TimeoutError 等），校验不过抛 AssetError。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f"{target.name}.part")
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as resp, part.open("wb") as out:
            got = 0
            for block in iter(lambda: resp.read(CHUNK), b""):
                out.write(block)
                got += len(block)
                if on_bytes:
                    on_bytes(got)
        digest = sha256_of(part)
        if digest != sha256:
            raise AssetError(f"{target.name}: checksum mismatch (expected {sha256}, got {digest})")
        part.replace(target)
    finally:
        part.unlink(missing_ok=True)


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
    from orbit8d.separate.model import model_files

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
