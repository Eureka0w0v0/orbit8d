"""项目记录的形状（SPEC §6.1）：上传文件的元数据、由它得出的显示名与导出文件名，以及旧记录还读得出来。"""

from orbit8d.jobs.states import ProjectState
from orbit8d.jobs.store import MAX_NAME, ProjectRecord, SourceInfo, safe_name

SOURCE = {
    "filename": "Demo Song.mp3",
    "size": 1234,
    "sha256": "ab" * 32,
    "codec": "mp3",
    "duration_s": 200.5,
    "title": "Demo Song",
    "artist": "Somebody",
    "album": "Demos",
    "has_cover": True,
}
OLD_PROJECT = {  # 分轨改存 FLAC 之前写的记录：没有 step、stem_scales
    "id": "0123456789abcdef",
    "state": "READY",
    "source": SOURCE,
    "progress": 0.0,
    "error": None,
    "analysis": {"version": 3, "bpm": 120.0},
    "preview_scale": 1.3,
    "created_at": 1.0,
    "updated_at": 2.0,
}


def test_song_title_prefers_the_tag_then_the_file_name():
    assert SourceInfo(title="Tagged", filename="file.mp3").song_title == "Tagged"
    assert SourceInfo(filename="My Song.final.wav").song_title == "My Song.final"
    assert SourceInfo().song_title == "untitled"


def test_export_file_name_and_tags_come_from_the_source():
    source = SourceInfo.from_dict(SOURCE)
    assert source.export_file_name("m4a") == "Somebody - Demo Song (8D).m4a"
    assert source.export_metadata() == {"title": "Demo Song (8D)", "artist": "Somebody", "album": "Demos"}
    untagged = SourceInfo(filename="a/b: c?.flac")
    assert untagged.export_file_name("flac") == "b  c (8D).flac" and untagged.export_metadata() == {
        "title": "b: c? (8D)"
    }
    assert SourceInfo(title='x"y|z', artist="A/B").export_file_name("wav") == "A B - x y z (8D).wav"


def test_safe_name_drops_separators_and_control_characters():
    assert safe_name("../../etc/passwd") == "etc passwd"
    assert safe_name(" . name\x00\x1f . ") == "name"
    assert safe_name("") == "untitled" and safe_name("...", "fallback") == "fallback"
    assert len(safe_name("长" * 300)) == MAX_NAME


def test_an_old_project_record_loads_and_is_written_back_unchanged():
    rec = ProjectRecord.from_dict(OLD_PROJECT)
    assert rec.state is ProjectState.READY and rec.source == SourceInfo(**SOURCE)
    assert rec.step == "" and rec.stem_scales == {}
    assert rec.to_dict() == {**OLD_PROJECT, "step": "", "stem_scales": {}}


def test_a_source_with_missing_or_extra_keys_still_loads():
    source = SourceInfo.from_dict({"filename": "x.wav", "bitrate": 320})
    assert source == SourceInfo(filename="x.wav") and source.artist == "" and source.has_cover is False
