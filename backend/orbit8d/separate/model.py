"""分轨模型 htdemucs_ft 的本地文件（SPEC §4.2）：4 个子模型 + 组合配置，共约 336 MB。

先找 HuggingFace 缓存（demucs 4.1 自己也下载到那里）；没有或校验不过，就从 HuggingFace 下载到
data/assets/demucs/（只有第一次，带进度、逐个文件 sha256 校验）。加载只读本地文件，之后启动不再联网。
"""

import logging
from collections.abc import Callable, Mapping
from pathlib import Path

from orbit8d.download import download, sha256_of
from orbit8d.errors import CodedError

log = logging.getLogger(__name__)

MODEL_REPO = "adefossez/HTDemucs-ft"
MODEL_REVISION = "d74ac89c3a1e874fc78f152555cf4d8533f06cd4"  # 固定版本：文件内容与下面的 sha256 一一对应
MODEL_URL = "https://huggingface.co/{repo}/resolve/{revision}/{name}"
BAG_FILE = "htdemucs_ft.yaml"
MODEL_FILES: dict[str, tuple[int, str]] = {  # 文件名 → (字节数, sha256)
    BAG_FILE: (149, "69470b8c1bbd674437b51bc9fb491327a10ab0396b702c93389b9cf750016346"),
    "f7e0c4bc.safetensors": (84_025_440, "2c85ab3c62dd6edd8e0b965e38b16fd1cdde357cc25de6b6bc9ce7c83f60925f"),
    "d12395a8.safetensors": (84_025_440, "5b01a97567ae9a3178a6236fb520251045c03eb8834bc8c24a4eec11d6c8fb56"),
    "92cfc3b6.safetensors": (84_025_440, "a241863551f30d01c42bd7b97da40839922ead3acb0f1fcab25682f55b4eeb59"),
    "04573f0d.safetensors": (84_025_440, "68854b0d7c2b3274723b5761f6fd9f5aec5f1bcd3f0de7c1669546fdb7871b7c"),
}
LOCAL_DIR = Path("demucs") / MODEL_REVISION[:12]


class ModelDownloadError(CodedError):
    def __init__(self, message: str):
        super().__init__("MODEL_DOWNLOAD_FAILED", message)


def hf_cached(name: str) -> Path | None:
    """HuggingFace 缓存里这个版本的文件（只查本地，不联网）。"""
    from huggingface_hub import try_to_load_from_cache

    found = try_to_load_from_cache(MODEL_REPO, name, revision=MODEL_REVISION)
    return Path(found) if isinstance(found, str) else None


def _valid(path: Path | None, size: int, digest: str) -> bool:
    return path is not None and path.is_file() and path.stat().st_size == size and sha256_of(path) == digest


def model_files(
    assets_dir: Path,
    on_progress: Callable[[float], None],
    cached: Callable[[str], Path | None] = hf_cached,
    files: Mapping[str, tuple[int, str]] = MODEL_FILES,
    url: str = MODEL_URL,
) -> dict[str, Path]:
    """每个模型文件的本地路径。缺的才下载，on_progress 收到 0..1 的下载进度（什么都不缺就不调用）。"""
    local = assets_dir / LOCAL_DIR
    paths: dict[str, Path] = {}
    for name, (size, digest) in files.items():
        hit = next((p for p in (cached(name), local / name) if _valid(p, size, digest)), None)
        if hit:
            paths[name] = hit
    missing = [name for name in files if name not in paths]
    total, done = sum(files[name][0] for name in missing), 0
    for name in missing:
        log.info("downloading model file", extra={"event": "model.download", "file": name})
        source = url.format(repo=MODEL_REPO, revision=MODEL_REVISION, name=name)
        try:
            download(
                source, local / name, files[name][1], lambda got, base=done: on_progress((base + got) / total)
            )
        except (OSError, CodedError) as exc:  # 断网、超时、HTTP 错误、校验不过
            raise ModelDownloadError(f"Could not download the separation model ({name}): {exc}") from exc
        done += files[name][0]
        paths[name] = local / name
    return paths


def load_bag(paths: Mapping[str, Path]):
    """按组合配置从本地文件组装 demucs 的 BagOfModels（与 demucs.hf.get_hf_model 相同，只是不联网）。"""
    import yaml
    from demucs.apply import BagOfModels
    from demucs.hf import load_safetensors_model

    bag = yaml.safe_load(paths[BAG_FILE].read_text(encoding="utf-8"))
    models = [load_safetensors_model(paths[f"{sig}.safetensors"]) for sig in bag["models"]]
    model = BagOfModels(models, bag.get("weights"), bag.get("segment"))
    model.eval()
    return model


def bag_progress(event: Mapping, length: int) -> float:
    """demucs apply_model 的回调（每段开始、结束各一次）→ 整体进度：第几个子模型 + 子模型里处理到的位置。"""
    models = max(1, int(event.get("models", 1)))
    within = min(1.0, max(0.0, float(event.get("segment_offset", 0)) / max(length, 1)))
    return min(1.0, (int(event.get("model_idx_in_bag", 0)) + within) / models)
