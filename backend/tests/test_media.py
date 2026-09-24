"""ffmpeg 封装（SPEC §1、§7）：格式白名单、解码规格、五种输出格式、封面保留。"""

import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from orbit8d.media.ffmpeg import MediaError, available_formats, decode_to_wav, encode, probe

SR = 44100


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True, timeout=60)


@pytest.fixture
def tone_wav(tmp_path: Path) -> Path:
    t = np.arange(2 * SR) / SR
    path = tmp_path / "tone.wav"
    sf.write(path, np.stack([0.3 * np.sin(2 * np.pi * 440 * t)] * 2, axis=1), SR, subtype="FLOAT")
    return path


@pytest.fixture
def tone_mp3(tmp_path: Path, tone_wav: Path) -> Path:
    path = tmp_path / "in.mp3"
    ff(
        "-i",
        str(tone_wav),
        "-metadata",
        "title=Test Song",
        "-metadata",
        "artist=Tester",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        str(path),
    )
    return path


@pytest.fixture
def mp3_with_cover(tmp_path: Path, tone_wav: Path) -> Path:
    cover = tmp_path / "cover.jpg"
    ff("-f", "lavfi", "-i", "color=c=red:s=64x64:d=1", "-frames:v", "1", str(cover))
    path = tmp_path / "covered.mp3"
    ff(
        "-i",
        str(tone_wav),
        "-i",
        str(cover),
        "-map",
        "0:a",
        "-map",
        "1:v",
        "-c:a",
        "libmp3lame",
        "-c:v",
        "copy",
        "-id3v2_version",
        "3",
        "-disposition:v",
        "attached_pic",
        str(path),
    )
    return path


def test_probe_reads_codec_duration_and_tags(tone_mp3):
    info = probe(tone_mp3)
    assert info.codec == "mp3" and 1.9 < info.duration_s < 2.2
    assert info.title == "Test Song" and info.artist == "Tester" and not info.has_cover


def test_probe_detects_cover(mp3_with_cover):
    assert probe(mp3_with_cover).has_cover


def test_probe_rejects_non_audio(tmp_path):
    bad = tmp_path / "x.mp3"
    bad.write_text("definitely not audio")
    with pytest.raises(MediaError):
        probe(bad)


def test_probe_rejects_codec_outside_whitelist(tmp_path):
    path = tmp_path / "adpcm.wav"
    ff("-f", "lavfi", "-i", "sine=f=440:d=1", "-c:a", "adpcm_ima_wav", str(path))
    with pytest.raises(MediaError) as err:
        probe(path)
    assert err.value.code == "UNSUPPORTED_CODEC"


def test_decode_normalizes_to_44k_stereo_float(tmp_path):
    src = tmp_path / "mono48k.flac"
    ff("-f", "lavfi", "-i", "sine=f=440:d=1:sample_rate=48000", "-ac", "1", str(src))
    out = tmp_path / "out.wav"
    decode_to_wav(src, out, SR)
    info = sf.info(out)
    assert info.samplerate == SR and info.channels == 2 and info.subtype == "FLOAT"
    assert abs(info.duration - 1.0) < 0.01


@pytest.mark.parametrize("fmt", ["m4a", "mp3", "flac", "wav", "ogg"])
def test_encode_roundtrip_all_formats(tmp_path, tone_wav, fmt):
    assert fmt in available_formats()
    out = tmp_path / f"out.{fmt}"
    encode(tone_wav, out, fmt, {"title": "Song (8D)", "artist": "Tester"}, cover_from=None)
    info = probe(out)
    assert abs(info.duration_s - 2.0) < 0.06
    assert info.title == "Song (8D)"


@pytest.mark.parametrize("fmt", ["m4a", "mp3", "flac"])
def test_encode_keeps_cover_art(tmp_path, tone_wav, mp3_with_cover, fmt):
    out = tmp_path / f"encoded_with_cover.{fmt}"
    encode(tone_wav, out, fmt, {"title": "x"}, cover_from=mp3_with_cover)
    assert probe(out).has_cover


def test_encode_rejects_unknown_format(tmp_path, tone_wav):
    with pytest.raises(MediaError):
        encode(tone_wav, tmp_path / "x.exe", "exe", {}, cover_from=None)
