import { describe, expect, it } from "vitest";
import type { Orbit, Scene, Section } from "../types";
import { TRACKS } from "../types";
import {
  addEvent,
  applyPreset,
  barGrid,
  eventAnchor,
  eventsIn,
  moveBoundary,
  moveEvent,
  removeEvent,
  removeSection,
  resizeEvent,
  sectionIndexAt,
  snapToBar,
  snapToBeat,
  splitAt,
} from "./edit";

const BPM = 120; // 一小节 2 秒
const GRID = barGrid(BPM, 0.5); // 小节线在 0.5, 2.5, 4.5, ...
const DUR = 60;

function orbit(start = 0): Orbit {
  return {
    shape: "circle",
    radius_m: 1.2,
    speed: { mode: "bars", bars: 4, seconds: 7 },
    direction: "cw",
    start_deg: start,
    height_deg: 0,
    pitch_deg: 0,
    roll_deg: 0,
    yaw_deg: 0,
    aspect: 0.6,
    swing_deg: 120,
    lift_deg: 30,
  };
}

function section(start_s: number, label: string, startDeg = 0): Section {
  return { start_s, label, orbits: { vocals: orbit(startDeg), drums: orbit(), bass: orbit(), other: orbit() }, wet_db: -12 };
}

function scene(...sections: Section[]): Scene {
  const mix = { gain_db: 0, width_deg: 0, reverb_send: 1, mute: false, solo: false };
  return {
    version: 2,
    mix: { vocals: { ...mix }, drums: { ...mix }, bass: { ...mix }, other: { ...mix } },
    sections,
    events: [],
    room: { name: "hall" },
    rear_darken_db: 6,
  };
}

describe("小节网格", () => {
  it("与 t_ref 同相位，吸附到最近的小节线", () => {
    expect(GRID.first).toBeCloseTo(0.5);
    expect(snapToBar(GRID, 3.4)).toBeCloseTo(2.5);
    expect(snapToBar(GRID, 3.6)).toBeCloseTo(4.5);
    expect(barGrid(BPM, 7.3).first).toBeCloseTo(1.3);
  });
});

describe("段落编辑", () => {
  const s = scene(section(0, "主歌"), section(20.5, "副歌", 90));

  it("按时间找段", () => {
    expect(sectionIndexAt(s, 0)).toBe(0);
    expect(sectionIndexAt(s, 20.49)).toBe(0);
    expect(sectionIndexAt(s, 20.5)).toBe(1);
  });

  it("在播放头处切分：新段复制原段、分界对齐小节线，原场景不变", () => {
    const r = splitAt(s, 9.2, GRID, DUR);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.scene.sections.map((x) => x.start_s)).toEqual([0, 8.5, 20.5]);
    expect(r.scene.sections[1].label).toBe("主歌");
    expect(s.sections.length).toBe(2);
    r.scene.sections[1].orbits.vocals.start_deg = 45;
    expect(r.scene.sections[0].orbits.vocals.start_deg).toBe(0); // 深拷贝
  });

  it("离边界不到 1 小节不能切", () => {
    expect(splitAt(s, 21.0, GRID, DUR).ok).toBe(false);
    expect(splitAt(s, 59.5, GRID, DUR).ok).toBe(false);
  });

  it("删除：删中间段由前一段接上；删第一段由后一段接到 0 秒；只剩一段不能删", () => {
    const three = scene(section(0, "前奏"), section(10.5, "主歌"), section(30.5, "副歌"));
    const mid = removeSection(three, 1);
    expect(mid.ok && mid.scene.sections.map((x) => [x.start_s, x.label])).toEqual([[0, "前奏"], [30.5, "副歌"]]);
    const first = removeSection(three, 0);
    expect(first.ok && first.scene.sections.map((x) => [x.start_s, x.label])).toEqual([[0, "主歌"], [30.5, "副歌"]]);
    expect(removeSection(scene(section(0, "全曲")), 0).ok).toBe(false);
  });

  it("拖分界：吸附小节线，两边至少各留 1 小节", () => {
    expect(moveBoundary(s, 1, 13.2, GRID, DUR).sections[1].start_s).toBeCloseTo(12.5);
    expect(moveBoundary(s, 1, 0.1, GRID, DUR).sections[1].start_s).toBeCloseTo(2.5);
    expect(moveBoundary(s, 1, 100, GRID, DUR).sections[1].start_s).toBeCloseTo(56.5);
    expect(moveBoundary(s, 0, 5, GRID, DUR)).toBe(s);
  });
});

describe("事件", () => {
  const s = scene(section(0, "全曲"));

  it("同一音轨同类事件不能重叠（停顿含前后各 0.5 秒）", () => {
    const a = addEvent(s, "hold", 10, "vocals", GRID);
    expect(a.ok).toBe(true);
    if (!a.ok) return;
    expect(a.scene.events[0]).toEqual({ t_s: 10, kind: "hold", duration_s: 2, targets: ["vocals"] });
    expect(addEvent(a.scene, "hold", 12.9, "vocals", GRID).ok).toBe(false); // 10 → 13 被占用
    expect(addEvent(a.scene, "hold", 13, "vocals", GRID).ok).toBe(true);
    expect(addEvent(a.scene, "hold", 11, "drums", GRID).ok).toBe(true);
    expect(addEvent(a.scene, "overhead", 11, "vocals", GRID).ok).toBe(true);
  });

  it("飞过头顶默认 2 小节；删除与按段筛选", () => {
    const two = scene(section(0, "主歌"), section(20.5, "副歌"));
    const r = addEvent(two, "overhead", 25, "other", GRID);
    expect(r.ok && r.scene.events[0].duration_s).toBe(4);
    if (!r.ok) return;
    expect(eventsIn(r.scene, 0, DUR)).toEqual([]);
    expect(eventsIn(r.scene, 1, DUR).map((x) => x.index)).toEqual([0]);
    expect(removeEvent(r.scene, 0).events).toEqual([]);
  });
});

describe("预设套用", () => {
  const preset = scene({ ...section(0, "全曲", 180), wet_db: -8 });
  preset.room = { name: "church" };

  it("只有一段：整个换成预设，保留事件", () => {
    const s = scene(section(0, "全曲"));
    s.events = [{ t_s: 5, kind: "hold", duration_s: 2, targets: ["vocals"] }];
    const out = applyPreset(s, preset, 0);
    expect(out.room.name).toBe("church");
    expect(out.events).toEqual(s.events);
    expect(out.sections[0].orbits.vocals.start_deg).toBe(180);
  });

  it("多段：只换当前段的轨道与混响量", () => {
    const s = scene(section(0, "主歌"), section(20.5, "副歌"));
    const out = applyPreset(s, preset, 1);
    expect(out.room.name).toBe("hall");
    expect(out.sections[0].orbits.vocals.start_deg).toBe(0);
    expect(out.sections[1].orbits.vocals.start_deg).toBe(180);
    expect(out.sections[1].wet_db).toBe(-8);
    expect(out.sections[1].label).toBe("副歌");
    for (const t of TRACKS) expect(out.sections[1].orbits[t]).not.toBe(preset.sections[0].orbits[t]);
  });
});

describe("拖动事件", () => {
  // 一拍 0.5 秒，拍点在 0.5 + k·0.5 上
  const base = scene(section(0, "全曲"));
  base.events = [
    { t_s: 9.5, kind: "hold", duration_s: 2, targets: ["vocals"] }, // 停住 10 → 12，占用 9.5 → 12.5
    { t_s: 20, kind: "hold", duration_s: 2, targets: ["vocals", "drums"] },
    { t_s: 15, kind: "overhead", duration_s: 4, targets: ["vocals"] },
  ];

  it("拍网格与事件条起点", () => {
    expect(snapToBeat(GRID, 1.2)).toBeCloseTo(1.0);
    expect(eventAnchor(base.events[0])).toBeCloseTo(10);
    expect(eventAnchor(base.events[2])).toBeCloseTo(15);
  });

  it("停顿：完全停住的那一刻吸附到拍；按住 ⌥ 不吸附；不改原场景和数组顺序", () => {
    const moved = moveEvent(base, 0, 13.1, GRID, DUR);
    expect(eventAnchor(moved.events[0])).toBeCloseTo(13.0);
    expect(moved.events.map((e) => e.kind)).toEqual(["hold", "hold", "overhead"]);
    expect(base.events[0].t_s).toBe(9.5);
    expect(eventAnchor(moveEvent(base, 0, 13.1, GRID, DUR, true).events[0])).toBeCloseTo(13.1);
  });

  it("碰到同一音轨的同类事件就停住，别的音轨或别的类型不挡", () => {
    // 后面 20 秒有一个人声停顿（占用 20 → 23）：第一个停顿（总长 3 秒）最晚只能从 17 秒开始减速
    expect(moveEvent(base, 0, 30, GRID, DUR).events[0].t_s).toBeCloseTo(17);
    // 往前拖到头：从 0 秒开始
    expect(moveEvent(base, 0, -5, GRID, DUR).events[0].t_s).toBeCloseTo(0);
    // 飞过头顶只受别的飞过头顶约束（这里没有），停顿挡不住它；最晚到 歌曲末尾 − 4 秒
    expect(moveEvent(base, 2, 100, GRID, DUR).events[2].t_s).toBeCloseTo(56);
  });

  it("拖右边缘改长短：终点吸附到拍，0.5–30 秒，不压到后面的事件", () => {
    expect(resizeEvent(base, 0, 13.2, GRID, DUR).events[0].duration_s).toBeCloseTo(3); // 停住 10 → 13
    expect(resizeEvent(base, 0, 10.1, GRID, DUR).events[0].duration_s).toBeCloseTo(0.5);
    // 后面 20 秒有人声停顿：9.5 起减速，最多停住 10 → 19.5，再加速 0.5 秒正好到 20
    expect(resizeEvent(base, 0, 40, GRID, DUR).events[0].duration_s).toBeCloseTo(9.5);
    expect(resizeEvent(base, 2, 80, GRID, DUR).events[2].duration_s).toBeCloseTo(30);
  });

  it("位置没变时返回原对象（不产生多余的撤销步骤）", () => {
    expect(moveEvent(base, 0, 10.1, GRID, DUR)).toBe(base);
    expect(resizeEvent(base, 0, 12.1, GRID, DUR)).toBe(base);
    expect(moveEvent(base, 7, 3, GRID, DUR)).toBe(base);
  });
});
