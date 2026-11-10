"""自动编排（SPEC §13.5）：按段落标签给每段配轨道与混响量，同类段落按出现次序轮换花样，再放两类事件。

前奏：抬高、放慢一倍的螺旋（顺时针），混响 +4 dB；尾声：同上，逆时针
主歌：经典 8D → 经典逆时针 → 经典 → …（用户自己起的段名按主歌算）；全曲（整首只有一段）：经典 8D
副歌：斜向环绕（速度翻倍）→ 镜像斜环（倾斜朝另一边、方向全反）→ 三层环绕（速度翻倍）→ …
桥段：立体交叉（左耳→头顶→右耳 / 正前→头顶→脑后）→ 上下翻滚 → …，混响 +2 dB
事件：最响的副歌一开始人声与其他轨飞过头顶（2 小节）；8 小节以上的主歌在 3/4 处让人声停 1 小节。

重新编排（✨）沿用用户改过的分界与段名，只换各段轨道、混响量与事件；全曲混音、房间、背后压暗不动。
用户的分段没有能量，用原曲音量起伏（envelope_db）按段求平均来找最响的副歌。
"""

from collections.abc import Callable

import numpy as np

from orbit8d.engine.orbit import bar_seconds
from orbit8d.engine.scene import (
    HOLD_RAMP_S,
    MAX_BARS,
    Event,
    Orbit,
    Scene,
    Section,
    preset_parts,
)
from orbit8d.engine.structure import (
    BRIDGE,
    CHORUS,
    ENVELOPE_FLOOR_DB,
    INTRO,
    OUTRO,
    VERSE,
    WHOLE,
    SectionInfo,
)

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
KNOWN_LABELS = (INTRO, VERSE, CHORUS, BRIDGE, OUTRO, WHOLE)

Look = tuple[dict[str, Orbit], float]  # 一段的各轨轨道与混响量


def _reversed(orbits: dict[str, Orbit]) -> dict[str, Orbit]:
    for o in orbits.values():
        o.direction = "ccw" if o.direction == "cw" else "cw"
    return orbits


def _mirrored(orbits: dict[str, Orbit]) -> dict[str, Orbit]:
    """左右镜像：倾斜朝向翻到另一边，转动方向反过来。"""
    for o in orbits.values():
        o.yaw_deg = -o.yaw_deg
    return _reversed(orbits)


def _faster(bars: int) -> int:
    return max(bars // 2, 1)


def _ambient(bars: int, direction: str) -> Look:
    orbits, _, wet = preset_parts("classic", min(bars * 2, MAX_BARS))
    for o in orbits.values():
        o.shape, o.height_deg, o.direction = "spiral", AMBIENT_HEIGHT_DEG, direction
    return orbits, wet + AMBIENT_WET_DB


def _preset(name: str, bars: int, wet_offset: float = 0.0, change=None) -> Look:
    orbits, _, wet = preset_parts(name, bars)
    return (change(orbits) if change else orbits), wet + wet_offset


ROTATIONS: dict[str, tuple[Callable[[int], Look], ...]] = {
    VERSE: (
        lambda bars: _preset("classic", bars),
        lambda bars: _preset("classic", bars, change=_reversed),
    ),
    CHORUS: (
        lambda bars: _preset("diagonal", _faster(bars)),
        lambda bars: _preset("diagonal", _faster(bars), change=_mirrored),
        lambda bars: _preset("layers", _faster(bars)),
    ),
    BRIDGE: (
        lambda bars: _preset("cross", bars, BRIDGE_WET_DB),
        lambda bars: _preset("tumble", bars, BRIDGE_WET_DB),
    ),
    INTRO: (lambda bars: _ambient(bars, "cw"),),
    OUTRO: (lambda bars: _ambient(bars, "ccw"),),
    WHOLE: (lambda bars: _preset("classic", bars),),
}


def kind_of(label: str) -> str:
    """段名 → 编排用的段落类型（用户自己起的名字按主歌算）。"""
    return label if label in KNOWN_LABELS else VERSE


def section_look(label: str, default_bars: int, occurrence: int = 0) -> Look:
    """某种段落第 occurrence 次出现（从 0 数）时的各轨轨道与混响量。"""
    looks = ROTATIONS[kind_of(label)]
    return looks[occurrence % len(looks)](default_bars)


def section_looks(labels: list[str], default_bars: int) -> list[Look]:
    """按出现次序轮换：同类段落第 1、2、3 次出现用不同的花样（主歌与自定义段名共用一个计数）。"""
    seen: dict[str, int] = {}
    looks = []
    for label in labels:
        kind = kind_of(label)
        looks.append(section_look(kind, default_bars, seen.get(kind, 0)))
        seen[kind] = seen.get(kind, 0) + 1
    return looks


def _events(sections: list[SectionInfo], bar: float) -> list[Event]:
    events = []
    choruses = [s for s in sections if kind_of(s.label) == CHORUS]
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
        if kind_of(s.label) == VERSE and s.bars >= HOLD_MIN_BARS:
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


def _sections(sections: list[SectionInfo], looks: list[Look]) -> list[Section]:
    return [
        Section(
            start_s=s.start_s,
            label=s.label,
            orbits=orbits,
            wet_db=min(max(wet, WET_RANGE_DB[0]), WET_RANGE_DB[1]),
        )
        for s, (orbits, wet) in zip(sections, looks, strict=True)
    ]


def choreograph(sections: list[SectionInfo], default_bars: int, bpm_norm: float) -> Scene:
    """新歌的默认场景：按识别出的段落编排。"""
    _, mix, _ = preset_parts("classic", default_bars)
    looks = section_looks([s.label for s in sections], default_bars)
    return Scene(
        mix=mix, sections=_sections(sections, looks), events=_events(sections, bar_seconds(bpm_norm))
    )


def section_energy_db(envelope_db: list[float], hop_s: float, start_s: float, end_s: float) -> float:
    """原曲音量起伏在 [start, end) 里的平均功率（dB，相对全曲最响处）。"""
    db = np.asarray(envelope_db, dtype=np.float64)
    t = np.arange(len(db)) * hop_s
    inside = db[(t >= start_s) & (t < end_s)]
    return float(10 * np.log10(np.mean(10 ** (inside / 10)))) if len(inside) else ENVELOPE_FLOOR_DB


def rechoreograph(
    scene: Scene,
    duration_s: float,
    default_bars: int,
    bpm_norm: float,
    envelope_db: list[float],
    hop_s: float,
) -> Scene:
    """按当前时间轴的分界与段名重新编排：只换各段轨道、混响量与事件，其余保留。"""
    bar = bar_seconds(bpm_norm)
    ends = [s.start_s for s in scene.sections[1:]] + [max(duration_s, scene.sections[-1].start_s)]
    infos = [
        SectionInfo(
            start_s=sec.start_s,
            bars=max(0, int(round((end - sec.start_s) / bar))),
            label=sec.label,
            energy_db=section_energy_db(envelope_db, hop_s, sec.start_s, end),
        )
        for sec, end in zip(scene.sections, ends, strict=True)
    ]
    looks = section_looks([s.label for s in infos], default_bars)
    return Scene(  # 重新走一遍校验
        mix=scene.mix,
        sections=_sections(infos, looks),
        events=_events(infos, bar),
        room=scene.room,
        rear_darken_db=scene.rear_darken_db,
    )
