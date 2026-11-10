"""下载一个文件并校验：先写到目标旁的 .part，sha256 对上了才原子改名，失败不留半截文件。
HRTF 数据（assets）和分轨模型（separate.model）都用它。"""

import hashlib
import urllib.request
from collections.abc import Callable
from pathlib import Path

from orbit8d.errors import CodedError

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
