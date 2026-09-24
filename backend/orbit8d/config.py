"""运行配置：全部可由环境变量覆盖，没有硬编码的机器相关路径。"""

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOST = "127.0.0.1"          # 红线：只监听本机，不提供覆盖方式
SAMPLE_RATE = 44100         # 内部统一采样率（Demucs 模型的采样率）
MB = 1024 * 1024


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    port: int
    max_upload_bytes: int
    max_duration_s: float

    @property
    def assets_dir(self) -> Path:
        return self.data_dir / "assets"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"


def load_settings() -> Settings:
    return Settings(
        data_dir=Path(os.environ.get("ORBIT8D_DATA_DIR", REPO_ROOT / "data")).expanduser().resolve(),
        port=int(os.environ.get("ORBIT8D_PORT", "8765")),
        max_upload_bytes=int(float(os.environ.get("ORBIT8D_MAX_UPLOAD_MB", "300")) * MB),
        max_duration_s=float(os.environ.get("ORBIT8D_MAX_DURATION_S", "1200")),
    )
