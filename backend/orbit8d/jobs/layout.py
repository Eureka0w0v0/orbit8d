"""磁盘布局（SPEC §6.1）：一首歌、一次导出的目录里每个文件叫什么、放在哪。只算路径，不读不写。
换存储布局只改这里；目录本身在哪（按 ID 找、ID 先校验）由 Store 决定。"""

from dataclasses import dataclass
from pathlib import Path

RECORD_FILE = "record.json"
ORIGINAL_PREVIEW = "original"  # 原曲的试听文件名（A/B 对比用）；其余试听文件名见 engine 的 PREVIEW_FILES


@dataclass(frozen=True)
class ProjectFiles:
    """一首歌的目录。"""

    root: Path

    @property
    def record(self) -> Path:
        return self.root / RECORD_FILE

    @property
    def source(self) -> Path:
        """上传的原文件（只按生成的 ID 存放，不带用户给的文件名）。"""
        return self.root / "source"

    @property
    def decoded(self) -> Path:
        """解码出的整曲 WAV：只在分析期间存在。"""
        return self.root / "orig.wav"

    @property
    def scene(self) -> Path:
        """用户保存的场景。"""
        return self.root / "scene.json"

    @property
    def stems(self) -> Path:
        return self.root / "stems"

    def stem_wav(self, track: str) -> Path:
        """分轨器刚输出、还没压成 FLAC 的分轨（旧版存储里一直是它）。"""
        return self.stems / f"{track}.wav"

    def stem_flac(self, track: str) -> Path:
        return self.stems / f"{track}.flac"

    @property
    def previews(self) -> Path:
        return self.root / "preview"

    def preview(self, name: str) -> Path:
        return self.previews / f"{name}.flac"


@dataclass(frozen=True)
class ExportFiles:
    """一次导出的目录。"""

    root: Path

    @property
    def record(self) -> Path:
        return self.root / RECORD_FILE

    @property
    def render(self) -> Path:
        """母带处理后的 WAV：编码完就删。"""
        return self.root / "render.wav"

    def output(self, ext: str) -> Path:
        return self.root / f"output.{ext}"
