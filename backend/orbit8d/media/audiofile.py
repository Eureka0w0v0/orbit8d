"""音频文件的分段读写（SPEC §5.7）：读成可按段读取的整曲信号（engine.chunks.Signal），按段顺序写。"""

import os
from collections.abc import Iterable, Iterator
from pathlib import Path

import numpy as np
import soundfile as sf

from orbit8d.engine.chunks import CHUNK, Window, chunk_ranges


class AudioFile:
    """磁盘上的整曲音频：从头到尾按段读，读出的值乘以 scale（存成 24 位整数时为不超过 ±1 而除掉的系数）。"""

    def __init__(self, path: Path, scale: float = 1.0):
        info = sf.info(path)
        self.path, self.scale = path, scale
        self.channels = info.channels
        self._frames = info.frames

    @property
    def frames(self) -> int:
        return self._frames

    def windows(self, margin: int = 0, size: int = CHUNK) -> Iterator[Window]:
        """顺序读，不回头 seek：上一段多读的上下文留在缓冲里给下一段用。"""
        with sf.SoundFile(self.path) as f:
            buf, buf_start = np.zeros((0, f.channels), dtype=np.float32), 0
            for s, e in chunk_ranges(self._frames, size):
                a, b = max(0, s - margin), min(self._frames, e + margin)
                buf, buf_start = buf[a - buf_start :], a
                need = b - a - len(buf)
                if need > 0:
                    got = f.read(need, dtype="float32", always_2d=True)
                    if len(got) < need:
                        raise ValueError(f"{self.path.name} 只读到 {buf_start + len(buf) + len(got)} 帧")
                    buf = np.concatenate([buf, got])
                yield Window(buf[: b - a].astype(np.float64) * self.scale, s, e, s - a)


def write_audio(path: Path, chunks: Iterable[np.ndarray], channels: int, sr: int, subtype: str) -> None:
    """按段写一个音频文件（格式看扩展名）：先写临时文件，写完再原子替换，中途出错不留半截文件。"""
    tmp = path.with_suffix(f".tmp{path.suffix}")
    try:
        with sf.SoundFile(tmp, "w", sr, channels, subtype) as f:
            for chunk in chunks:
                f.write(np.ascontiguousarray(np.asarray(chunk).reshape(len(chunk), channels)))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
