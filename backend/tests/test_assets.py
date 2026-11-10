"""外部数据的下载与校验（SPEC §4.2）：用本机假 HTTP 服务测下载、断网、校验失败、残留的半截文件、
分轨模型“先找缓存、缺的才下”，以及分轨进度的换算。"""

import hashlib
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from orbit8d.assets import prune_stale_assets
from orbit8d.download import AssetError, download
from orbit8d.separate.model import LOCAL_DIR, ModelDownloadError, bag_progress, model_files

PAYLOAD = bytes(range(256)) * 9000  # 约 2.3 MB：会分好几块下载


class Files(BaseHTTPRequestHandler):
    files: dict[str, bytes] = {}
    hits: list[str] = []
    logs: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 — http.server 的约定名
        self.hits.append(self.path)
        body = self.files.get(self.path)
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — 覆盖基类同名参数
        self.logs.append(format % args)  # 记下来而不是打印到测试输出


@pytest.fixture
def server() -> Iterator[str]:
    Files.files, Files.hits, Files.logs = {}, [], []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Files)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_download_verifies_and_reports_bytes(server, tmp_path):
    Files.files["/a.bin"] = PAYLOAD
    got: list[int] = []
    download(f"{server}/a.bin", tmp_path / "a.bin", sha(PAYLOAD), got.append)
    assert (tmp_path / "a.bin").read_bytes() == PAYLOAD
    assert got == sorted(got) and got[-1] == len(PAYLOAD) and len(got) > 1
    assert not list(tmp_path.glob("*.part"))


def test_download_rejects_a_wrong_checksum_and_leaves_nothing(server, tmp_path):
    Files.files["/a.bin"] = PAYLOAD
    with pytest.raises(AssetError):
        download(f"{server}/a.bin", tmp_path / "a.bin", sha(b"something else"))
    assert list(tmp_path.iterdir()) == []


def test_download_failure_raises_and_overwrites_stale_part(server, tmp_path):
    (tmp_path / "a.bin.part").write_bytes(b"half of an old attempt")
    with pytest.raises(OSError):  # 404 → urllib.error.HTTPError（OSError 的子类）
        download(f"{server}/missing.bin", tmp_path / "a.bin", sha(PAYLOAD))
    assert list(tmp_path.iterdir()) == []
    Files.files["/a.bin"] = PAYLOAD
    download(f"{server}/a.bin", tmp_path / "a.bin", sha(PAYLOAD))
    assert (tmp_path / "a.bin").read_bytes() == PAYLOAD


def fake_model(names=("bag.yaml", "m1.bin", "m2.bin")) -> dict[str, tuple[int, str]]:
    blobs = {name: PAYLOAD[: 1000 * (k + 1)] + name.encode() for k, name in enumerate(names)}
    Files.files.update({f"/files/{name}": data for name, data in blobs.items()})
    return {name: (len(data), sha(data)) for name, data in blobs.items()}


def test_model_files_prefers_a_valid_cache_and_downloads_only_what_is_missing(server, tmp_path):
    files = fake_model()
    cache = tmp_path / "hf"
    cache.mkdir()
    (cache / "bag.yaml").write_bytes(Files.files["/files/bag.yaml"])
    (cache / "m1.bin").write_bytes(b"corrupted")  # 缓存里坏掉的要重下
    progress: list[float] = []
    paths = model_files(
        tmp_path / "assets",
        progress.append,
        cached=lambda name: cache / name if (cache / name).exists() else None,
        files=files,
        url=server + "/files/{name}",
    )
    assert paths["bag.yaml"] == cache / "bag.yaml"
    assert paths["m1.bin"] == tmp_path / "assets" / LOCAL_DIR / "m1.bin"
    assert sorted(Files.hits) == ["/files/m1.bin", "/files/m2.bin"]
    assert progress == sorted(progress) and progress[-1] == pytest.approx(1.0)

    Files.hits.clear()
    again: list[float] = []
    model_files(
        tmp_path / "assets", again.append, cached=lambda name: None, files=files, url=server + "/files/{name}"
    )
    assert Files.hits == ["/files/bag.yaml"] and again[-1] == pytest.approx(1.0)  # 已下载的不再下


def test_model_download_failure_has_its_own_error_code(server, tmp_path):
    files = fake_model()
    del Files.files["/files/m2.bin"]
    with pytest.raises(ModelDownloadError) as info:
        model_files(
            tmp_path, lambda _: None, cached=lambda name: None, files=files, url=server + "/files/{name}"
        )
    assert info.value.code == "MODEL_DOWNLOAD_FAILED" and "m2.bin" in str(info.value)
    assert not list((tmp_path / LOCAL_DIR).glob("*.part"))


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"models": 4, "model_idx_in_bag": 0, "segment_offset": 0}, 0.0),
        ({"models": 4, "model_idx_in_bag": 1, "segment_offset": 500}, 0.375),
        (
            {"models": 4, "model_idx_in_bag": 3, "segment_offset": 2000},
            1.0,
        ),  # 最后一段起点可能超过原长（随机平移）
        ({"segment_offset": 250}, 0.25),  # 单个模型时 demucs 不给 models
    ],
)
def test_separation_progress_counts_models_and_segments(event, expected):
    assert bag_progress(event, 1000) == pytest.approx(expected)


def test_prune_keeps_current_assets(tmp_path: Path):
    for name in (
        "HRIR_FULL2DEG.sofa",
        "hrtf_44100.bin",
        "brir_v2_hall_44100.wav",
        "eq_44100.wav",
        "brir_room_44100.wav",
    ):
        (tmp_path / name).write_bytes(b"x")
    assert prune_stale_assets(tmp_path) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "HRIR_FULL2DEG.sofa",
        "brir_v2_hall_44100.wav",
        "hrtf_44100.bin",
    ]
