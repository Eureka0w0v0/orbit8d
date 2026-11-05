"""Demucs htdemucs_ft 分轨：优先用 GPU（Apple 芯片为 MPS），出错自动退回 CPU。
模型只从本地文件加载（缺了先下载，见 separate/model.py）；进度来自 demucs 的逐段回调。"""

import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np
import soundfile as sf

from orbit8d.errors import CodedError
from orbit8d.separate.model import bag_progress, load_bag, model_files

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")  # 必须在导入 torch 之前

log = logging.getLogger(__name__)

MIN_STD = 1e-8


class SeparationError(CodedError):
    def __init__(self, message: str):
        super().__init__("SEPARATION_FAILED", message)


class DemucsSeparator:
    def __init__(self, assets_dir: Path, device: str | None = None):
        self._assets_dir = assets_dir
        self._device = device
        self._model = None
        self._lock = threading.Lock()

    def _pick_device(self) -> str:
        import torch

        if self._device:
            return self._device
        if torch.backends.mps.is_available():
            return "mps"
        return "cuda" if torch.cuda.is_available() else "cpu"

    def ensure_model(self, on_progress: Callable[[float], None]) -> None:
        """模型文件不在本机就下载（只有第一次，on_progress 收到下载进度），然后加载。"""
        with self._lock:
            if self._model is None:
                self._model = load_bag(model_files(self._assets_dir, on_progress))

    @staticmethod
    def _apply(model, x, device: str, callback: Callable[[dict], None]):
        import torch
        from demucs.apply import apply_model

        with torch.no_grad():
            return apply_model(
                model,
                x[None],
                device=device,
                shifts=1,
                split=True,
                overlap=0.25,
                progress=False,
                callback=callback,
            )[0].cpu()

    def _run(self, model, x, length: int, on_progress: Callable[[float], None]):
        device = self._pick_device()

        def report(event: dict) -> None:
            if event.get("state") == "end":
                on_progress(bag_progress(event, length))

        try:
            return self._apply(model, x, device, report)
        except RuntimeError as exc:
            if device == "cpu":
                raise SeparationError(f"Stem separation failed: {exc}") from exc
            log.warning(
                "gpu separation failed, retry on cpu", exc_info=True, extra={"event": "separate.fallback"}
            )
            try:
                return self._apply(model, x, "cpu", report)
            except RuntimeError as cpu_exc:
                raise SeparationError(f"Stem separation failed: {cpu_exc}") from cpu_exc

    def separate(self, wav: Path, out_dir: Path, on_progress: Callable[[float], None]) -> None:
        import torch

        self.ensure_model(lambda _: None)
        with self._lock:
            model = self._model
            audio, sr = sf.read(wav, always_2d=True, dtype="float32")
            if sr != model.samplerate:
                raise SeparationError(f"Sample rate {sr} Hz does not match the model ({model.samplerate} Hz)")
            x = torch.from_numpy(np.ascontiguousarray(audio.T))
            ref = x.mean(0)
            mean, std = ref.mean(), ref.std().clamp_min(MIN_STD)
            stems = self._run(model, (x - mean) / std, len(audio), on_progress) * std + mean
            out_dir.mkdir(parents=True, exist_ok=True)
            for name, stem in zip(model.sources, stems, strict=True):
                sf.write(out_dir / f"{name}.wav", stem.numpy().T, sr, subtype="FLOAT")
            on_progress(1.0)
