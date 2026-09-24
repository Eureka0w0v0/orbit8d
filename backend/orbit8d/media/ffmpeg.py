"""ffmpeg / ffprobe 封装：参数列表调用、不经 shell、带超时；失败抛 MediaError（含错误码与 stderr 摘要）。"""

import json
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROBE_TIMEOUT_S = 30
DECODE_TIMEOUT_S = 600
ENCODE_TIMEOUT_S = 900
STDERR_TAIL = 600
ALLOWED_CODECS = frozenset({"mp3", "aac", "alac", "flac", "vorbis", "opus"})
ALLOWED_CODEC_PREFIX = "pcm_"


class MediaError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProbeInfo:
    format_name: str
    codec: str
    duration_s: float
    sample_rate: int
    channels: int
    title: str
    artist: str
    album: str
    has_cover: bool


@dataclass(frozen=True)
class OutputFormat:
    ext: str
    encoders: tuple[str, ...]  # 按优先级尝试
    args: tuple[str, ...]
    cover: bool
    mime: str


OUTPUT_FORMATS: dict[str, OutputFormat] = {
    "m4a": OutputFormat(
        "m4a", ("aac_at", "aac"), ("-b:a", "256k", "-movflags", "+faststart"), True, "audio/mp4"
    ),
    "mp3": OutputFormat("mp3", ("libmp3lame",), ("-b:a", "320k", "-id3v2_version", "3"), True, "audio/mpeg"),
    "flac": OutputFormat(
        "flac", ("flac",), ("-sample_fmt", "s32", "-bits_per_raw_sample", "24"), True, "audio/flac"
    ),
    "wav": OutputFormat("wav", ("pcm_s24le",), (), False, "audio/wav"),
    "ogg": OutputFormat("ogg", ("libopus",), ("-b:a", "192k", "-ar", "48000"), False, "audio/ogg"),
}


def _binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise MediaError("FFMPEG_MISSING", f"找不到 {name}，请先安装 ffmpeg")
    return path


def _run(args: list[str], timeout: float, code: str) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{code}_TIMEOUT", f"{Path(args[0]).name} 超时（>{timeout}s）") from exc
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-STDERR_TAIL:].strip()
        raise MediaError(code, tail or f"{Path(args[0]).name} 返回 {proc.returncode}")
    return proc


def _codec_allowed(codec: str) -> bool:
    return codec in ALLOWED_CODECS or codec.startswith(ALLOWED_CODEC_PREFIX)


def probe(path: Path) -> ProbeInfo:
    proc = _run(
        [
            _binary("ffprobe"),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        PROBE_TIMEOUT_S,
        "PROBE_FAILED",
    )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError("PROBE_FAILED", "ffprobe 输出无法解析") from exc
    streams = data.get("streams", [])
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio:
        raise MediaError("NO_AUDIO", "文件里没有音频")
    codec = audio[0].get("codec_name", "")
    if not _codec_allowed(codec):
        raise MediaError("UNSUPPORTED_CODEC", f"不支持的音频编码: {codec}")
    fmt = data.get("format", {})
    # Ogg/Opus 等把标签存在音轨上，容器级标签优先
    tags = {k.lower(): v for k, v in (audio[0].get("tags") or {}).items()}
    tags.update({k.lower(): v for k, v in (fmt.get("tags") or {}).items()})
    has_cover = any(
        s.get("codec_type") == "video" and (s.get("disposition") or {}).get("attached_pic") == 1
        for s in streams
    )
    try:
        duration = float(fmt.get("duration") or audio[0].get("duration"))
    except (TypeError, ValueError) as exc:
        raise MediaError("PROBE_FAILED", "无法读取时长") from exc
    return ProbeInfo(
        format_name=fmt.get("format_name", ""),
        codec=codec,
        duration_s=duration,
        sample_rate=int(audio[0].get("sample_rate", 0)),
        channels=int(audio[0].get("channels", 0)),
        title=tags.get("title", ""),
        artist=tags.get("artist", ""),
        album=tags.get("album", ""),
        has_cover=has_cover,
    )


def decode_to_wav(src: Path, dst: Path, sample_rate: int) -> None:
    """解码为立体声 32 位浮点 WAV（单声道复制到两边，多声道缩混）。"""
    _run(
        [
            _binary("ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(src),
            "-map",
            "0:a:0",
            "-ac",
            "2",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_f32le",
            "-f",
            "wav",
            str(dst),
        ],
        DECODE_TIMEOUT_S,
        "DECODE_FAILED",
    )


@lru_cache(maxsize=1)
def available_encoders() -> frozenset[str]:
    proc = _run([_binary("ffmpeg"), "-hide_banner", "-encoders"], PROBE_TIMEOUT_S, "FFMPEG_MISSING")
    names = set()
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("A"):
            names.add(parts[1])
    return frozenset(names)


def _encoder_for(fmt: OutputFormat) -> str | None:
    return next((e for e in fmt.encoders if e in available_encoders()), None)


def available_formats() -> list[str]:
    return [key for key, fmt in OUTPUT_FORMATS.items() if _encoder_for(fmt)]


def encode(src_wav: Path, dst: Path, fmt_key: str, meta: dict[str, str], cover_from: Path | None) -> None:
    fmt = OUTPUT_FORMATS.get(fmt_key)
    encoder = _encoder_for(fmt) if fmt else None
    if not fmt or not encoder:
        raise MediaError("UNSUPPORTED_FORMAT", f"不支持的输出格式: {fmt_key}")
    args = [_binary("ffmpeg"), "-nostdin", "-v", "error", "-y", "-i", str(src_wav)]
    with_cover = fmt.cover and cover_from is not None and probe(cover_from).has_cover
    if with_cover:
        args += [
            "-i",
            str(cover_from),
            "-map",
            "0:a",
            "-map",
            "1:v",
            "-c:v",
            "copy",
            "-disposition:v",
            "attached_pic",
        ]
    else:
        args += ["-map", "0:a"]
    args += ["-map_metadata", "-1", "-c:a", encoder, *fmt.args]
    for key, value in meta.items():
        args += ["-metadata", f"{key}={value}"]
    _run([*args, str(dst)], ENCODE_TIMEOUT_S, "ENCODE_FAILED")
