"""自动编排（SPEC §13.5）：按段落标签给每段配轨道与混响量，再放两类事件。

前奏 / 尾声：抬高、放慢一倍的螺旋，混响 +4 dB
主歌：经典 8D
副歌：速度翻倍，人声与其他轨沿两条相反的 45° 斜环
桥段：立体交叉（左耳→头顶→右耳 / 正前→头顶→脑后），混响 +2 dB
事件：最响的副歌一开始人声与其他轨飞过头顶（2 小节）；8 小节以上的主歌在 3/4 处让人声停 1 小节。
"""

from orbit8d.engine.scene import (
    HOLD_RAMP_S,
    MAX_BARS,
    Event,
    Orbit,
    Scene,
    Section,
    preset_parts,
)
from orbit8d.engine.structure import BRIDGE, CHORUS, INTRO, OUTRO, VERSE, SectionInfo, bar_seconds

AMBIENT_HEIGHT_DEG = 35.0
AMBIENT_WET_DB = 4.0
BRIDGE_WET_DB = 2.0
OVERHEAD_BARS = 2
OVERHEAD_TARGETS = ("vocals", "other")
HOLD_BARS = 1
HOLD_AT = 0.75
HOLD_MIN_BARS = 8
HOLD_TARGETS = ("vocals",)
MAX_EVENT_S = 30.0
MIN_EVENT_S = 0.5
WET_RANGE_DB = (-24.0, 0.0)


def section_look(label: str, default_bars: int) -> tuple[dict[str, Orbit], float]:
    """某种段落的各轨轨道与混响量。"""
    if label in (INTRO, OUTRO):
        orbits, _, wet = preset_parts("classic", min(default_bars * 2, MAX_BARS))
        for o in orbits.values():
            o.shape, o.height_deg = "spiral", AMBIENT_HEIGHT_DEG
        return orbits, wet + AMBIENT_WET_DB
    if label == CHORUS:
        orbits, _, wet = preset_parts("diagonal", max(default_bars // 2, 1))
        return orbits, wet
    if label == BRIDGE:
        orbits, _, wet = preset_parts("cross", default_bars)
        return orbits, wet + BRIDGE_WET_DB
    orbits, _, wet = preset_parts("classic", default_bars)
    return orbits, wet


def _events(sections: list[SectionInfo], bar: float) -> list[Event]:
    events = []
    choruses = [s for s in sections if s.label == CHORUS]
    if choruses:
        top = max(choruses, key=lambda s: s.energy_db)
        span = min(OVERHEAD_BARS * bar, max(top.bars, 1) * bar, MAX_EVENT_S)
        events.append(
            Event(
                t_s=top.start_s,
                kind="overhead",
                duration_s=max(span, MIN_EVENT_S),
                targets=list(OVERHEAD_TARGETS),
            )
        )
    for s in sections:
        if s.label == VERSE and s.bars >= HOLD_MIN_BARS:
            stop = s.start_s + round(HOLD_AT * s.bars) * bar  # 从这条小节线起完全停住
            events.append(
                Event(
                    t_s=max(stop - HOLD_RAMP_S, s.start_s),
                    kind="hold",
                    duration_s=min(HOLD_BARS * bar, MAX_EVENT_S),
                    targets=list(HOLD_TARGETS),
                )
            )
    return events


def choreograph(sections: list[SectionInfo], default_bars: int, bpm_norm: float) -> Scene:
    looks = [section_look(s.label, default_bars) for s in sections]
    _, mix, _ = preset_parts("classic", default_bars)
    return Scene(
        mix=mix,
        sections=[
            Section(
                start_s=s.start_s,
                label=s.label,
                orbits=orbits,
                wet_db=min(max(wet, WET_RANGE_DB[0]), WET_RANGE_DB[1]),
            )
            for s, (orbits, wet) in zip(sections, looks, strict=True)
        ],
        events=_events(sections, bar_seconds(bpm_norm)),
    )
