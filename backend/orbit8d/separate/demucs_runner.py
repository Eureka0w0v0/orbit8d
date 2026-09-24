"""Demucs htdemucs_ft 分轨：优先用 GPU（Apple 芯片为 MPS），出错自动退回 CPU。进度按经验耗时估算。"""

import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import soundfile as sf

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")  # 必须在导入 torch 之前

log = logging.getLogger(__name__)

MODEL_NAME = "htdemucs_ft"
REALTIME_FACTOR = {"mps": 0.35, "cuda": 0.2, "cpu": 2.5}  # 每秒音频大约耗时（秒），只用于估算进度
DEFAULT_FACTOR = 2.5
PROGRESS_TICK_S = 0.5
PROGRESS_CAP = 0.95
MIN_STD = 1e-8


class SeparationError(RuntimeError):
    pass


class DemucsSeparator:
    def __init__(self, device: str | None = None):
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

    def _load(self):
        if self._model is None:
            from demucs.pretrained import get_model

            self._model = get_model(MODEL_NAME)
            self._model.eval()
        return self._model

    @staticmethod
    def _apply(model, x, device: str):
        import torch
        from demucs.apply import apply_model

        with torch.no_grad():
            return apply_model(
                model, x[None], device=device, shifts=1, split=True, overlap=0.25, progress=False
            )[0].cpu()

    @staticmethod
    def _tick(on_progress: Callable[[float], None], expected_s: float, stop: threading.Event) -> None:
        start = time.time()
        while not stop.wait(PROGRESS_TICK_S):
            on_progress(min(PROGRESS_CAP, (time.time() - start) / expected_s))

    def separate(self, wav: Path, out_dir: Path, on_progress: Callable[[float], None]) -> None:
        import torch

        with self._lock:
            model = self._load()
            audio, sr = sf.read(wav, always_2d=True, dtype="float32")
            if sr != model.samplerate:
                raise SeparationError(f"采样率 {sr} 与模型 {model.samplerate} 不一致")
            x = torch.from_numpy(np.ascontiguousarray(audio.T))
            ref = x.mean(0)
            mean, std = ref.mean(), ref.std().clamp_min(MIN_STD)
            device = self._pick_device()
            expected = max(len(audio) / sr * REALTIME_FACTOR.get(device, DEFAULT_FACTOR), 1.0)
            stop = threading.Event()
            ticker = threading.Thread(target=self._tick, args=(on_progress, expected, stop), daemon=True)
            ticker.start()
            try:
                try:
                    stems = self._apply(model, (x - mean) / std, device)
                except RuntimeError:
                    if device == "cpu":
                        raise
                    log.warning(
                        "gpu separation failed, retry on cpu",
                        exc_info=True,
                        extra={"event": "separate.fallback"},
                    )
                    stems = self._apply(model, (x - mean) / std, "cpu")
            finally:
                stop.set()
                ticker.join()
            stems = stems * std + mean
            out_dir.mkdir(parents=True, exist_ok=True)
            for name, stem in zip(model.sources, stems, strict=True):
                sf.write(out_dir / f"{name}.wav", stem.numpy().T, sr, subtype="FLOAT")
            on_progress(1.0)
