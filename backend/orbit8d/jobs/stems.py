"""一首歌的音频文件怎么存、怎么读（SPEC §5.7、§6.1）：分轨、试听文件，以及旧版 WAV 分轨到 FLAC 的迁移。

分轨存 24 位 FLAC：每轨缩放到 ±1 以内，缩放系数记在项目记录里，读回校验通过才删 WAV。
同一轨有 WAV 就用 WAV：迁移中途崩溃时，WAV 只会在它的 FLAC 和缩放系数都写好之后才删，重复执行没有副作用。
"""

from collections.abc import Callable, Iterator, Mapping

import numpy as np

from orbit8d.config import SAMPLE_RATE
from orbit8d.engine.chunks import aligned_windows, chunk_ranges
from orbit8d.engine.scene import TRACKS
from orbit8d.engine.sources import PREVIEW_FILES, SOURCE_CHANNELS, Sources
from orbit8d.jobs.layout import ORIGINAL_PREVIEW, ProjectFiles
from orbit8d.media.audiofile import AudioFile, write_audio

PREVIEW_NAMES = frozenset({*PREVIEW_FILES, ORIGINAL_PREVIEW})  # write_previews 写出的全部试听文件
PREVIEW_SUBTYPE = STEM_SUBTYPE = "PCM_24"
FLAC_TOLERANCE_LSB = 4  # 24 位 FLAC 读回与原值的允许误差（量化 + 读写归一化差，实际约 1.5 个最低位）


def needs_migration(files: ProjectFiles) -> bool:
    """旧版存储：还有 WAV 分轨，或者常驻的 orig.wav。"""
    return files.decoded.exists() or any(files.stems.glob("*.wav"))


def stem_file(files: ProjectFiles, name: str, scales: Mapping[str, float]) -> AudioFile:
    """有 WAV 用 WAV（还没迁移或迁移到一半），否则用 FLAC × 记录里的缩放系数。"""
    wav = files.stem_wav(name)
    if wav.exists():
        return AudioFile(wav)
    if name not in scales:
        raise FileNotFoundError(f"{name} 分轨缺少缩放系数，无法读取 FLAC")
    return AudioFile(files.stem_flac(name), scales[name])


def compress_stems(files: ProjectFiles, record_scale: Callable[[str, float], None]) -> None:
    """分轨 32 位浮点 WAV → 24 位 FLAC（约省一半空间）：每轨缩放到 ±1 以内，读回校验通过、
    缩放系数交给 record_scale 记下之后才删 WAV。可以重复执行（迁移到一半崩溃后再跑一遍即可）。"""
    for name in TRACKS:
        wav = files.stem_wav(name)
        if not wav.exists():
            continue
        source = AudioFile(wav)
        scale = max(1.0, max(float(np.abs(w.body).max(initial=0.0)) for w in source.windows()))
        flac = files.stem_flac(name)
        write_audio(
            flac, (w.body / scale for w in source.windows()), source.channels, SAMPLE_RATE, STEM_SUBTYPE
        )
        verify_copy(source, AudioFile(flac, scale), FLAC_TOLERANCE_LSB * scale / 2**23)
        record_scale(name, scale)
        wav.unlink()


def verify_copy(original: AudioFile, copy: AudioFile, tolerance: float) -> None:
    """逐段比对两份音频，误差超过 tolerance 就报错（不删原文件）。"""
    if (copy.frames, copy.channels) != (original.frames, original.channels):
        raise ValueError(f"{copy.path.name} 的长度或声道数与原文件不一致")
    for a, b in zip(original.windows(), copy.windows(), strict=True):
        err = float(np.abs(a.body - b.body).max(initial=0.0))
        if err > tolerance:
            raise ValueError(f"{copy.path.name} 读回校验失败：误差 {err:.3g} > {tolerance:.3g}")


def preview_chunks(files: ProjectFiles, scale: float) -> tuple[int, Iterator[np.ndarray]]:
    """试听文件 × scale → 按段的 9 路声源 (9, L)，与浏览器试听同一份数据。"""
    previews = [AudioFile(files.preview(name), scale) for name in PREVIEW_FILES]

    def chunks() -> Iterator[np.ndarray]:
        for windows in aligned_windows(previews):
            chunk = np.empty((len(SOURCE_CHANNELS), windows[0].end - windows[0].start))
            for channels, w in zip(PREVIEW_FILES.values(), windows, strict=True):
                chunk[list(channels)] = w.body.T
            yield chunk

    return previews[0].frames, chunks()


def write_previews(files: ProjectFiles, src: Sources, orig: AudioFile, scale: float) -> None:
    """9 路声源 + 原曲（A/B 对比用）写成 24 位 FLAC，统一除以 scale 保证不超过 ±1。"""
    files.previews.mkdir(exist_ok=True)
    for name, channels in PREVIEW_FILES.items():
        parts = (src.data[list(channels), s:e].T / scale for s, e in chunk_ranges(src.n))
        write_audio(files.preview(name), parts, len(channels), SAMPLE_RATE, PREVIEW_SUBTYPE)
    parts = (w.body / scale for w in orig.windows())
    write_audio(files.preview(ORIGINAL_PREVIEW), parts, orig.channels, SAMPLE_RATE, PREVIEW_SUBTYPE)
