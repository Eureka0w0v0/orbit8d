// 场景编辑（纯函数，不改入参）：段落切分 / 删除 / 拖分界、事件增删、预设套用到当前段。
// 规则与后端 Scene 校验一致（第一段从 0 秒开始、分界严格递增、同一音轨同类事件不重叠），不合法的操作返回原因。

import { BEATS_PER_BAR } from "../orbit/orbit";
import { barSeconds } from "../orbit/timeline";
import type { EventKind, Scene, SceneEvent, TrackName } from "../types";
import { HOLD_RAMP_S } from "../types";

export const MAX_SECTIONS = 16;
export const MAX_EVENTS = 64;
const MIN_SECTION_BARS = 1;
const EVENT_BARS: Record<EventKind, number> = { hold: 1, overhead: 2 };
const MAX_EVENT_S = 30;
const MIN_EVENT_S = 0.5;

/** 不合法操作的原因（界面按当前语言显示，见 i18n 的 edit）。 */
export type EditReason = "maxSections" | "tooClose" | "lastSection" | "maxEvents" | "holdExists" | "overheadExists";
export type EditResult = { ok: true; scene: Scene } | { ok: false; reason: EditReason };

/** 小节线：first + k·bar（与后端 structure.bar_starts 同相位）。 */
export interface BarGrid {
  first: number;
  bar: number;
}

export function barGrid(bpmNorm: number, tRef: number): BarGrid {
  const bar = barSeconds(bpmNorm);
  return { first: ((tRef % bar) + bar) % bar, bar };
}

export function snapToBar(g: BarGrid, t: number): number {
  return g.first + Math.round((t - g.first) / g.bar) * g.bar;
}

/** 吸附到最近的一拍（1/4 小节）。 */
export function snapToBeat(g: BarGrid, t: number): number {
  const beat = g.bar / BEATS_PER_BAR;
  return g.first + Math.round((t - g.first) / beat) * beat;
}

export function sectionIndexAt(scene: Scene, t: number): number {
  let k = 0;
  while (k + 1 < scene.sections.length && scene.sections[k + 1].start_s <= t) k++;
  return k;
}

export function sectionEnd(scene: Scene, k: number, durationS: number): number {
  return k + 1 < scene.sections.length ? scene.sections[k + 1].start_s : Math.max(durationS, scene.sections[k].start_s);
}

export function splitAt(scene: Scene, t: number, grid: BarGrid, durationS: number): EditResult {
  if (scene.sections.length >= MAX_SECTIONS) return { ok: false, reason: "maxSections" };
  const k = sectionIndexAt(scene, t);
  const at = snapToBar(grid, t);
  const minGap = MIN_SECTION_BARS * grid.bar * 0.999;
  if (at - scene.sections[k].start_s < minGap || sectionEnd(scene, k, durationS) - at < minGap) {
    return { ok: false, reason: "tooClose" };
  }
  const next = structuredClone(scene);
  next.sections.splice(k + 1, 0, { ...structuredClone(scene.sections[k]), start_s: at });
  return { ok: true, scene: next };
}

export function removeSection(scene: Scene, k: number): EditResult {
  if (scene.sections.length <= 1) return { ok: false, reason: "lastSection" };
  const next = structuredClone(scene);
  if (k === 0) next.sections[1].start_s = 0; // 删第一段：后一段往前接到 0 秒
  next.sections.splice(k, 1);
  return { ok: true, scene: next };
}

/** 拖第 k 段（k ≥ 1）的开头：落在小节线上，并且前后两段都至少留 1 小节；放不下就不动。 */
export function moveBoundary(scene: Scene, k: number, t: number, grid: BarGrid, durationS: number): Scene {
  if (k < 1 || k >= scene.sections.length) return scene;
  const index = (x: number) => (x - grid.first) / grid.bar;
  const lowest = Math.ceil(index(scene.sections[k - 1].start_s + MIN_SECTION_BARS * grid.bar) - 1e-9);
  const highest = Math.floor(index(sectionEnd(scene, k, durationS) - MIN_SECTION_BARS * grid.bar) + 1e-9);
  if (lowest > highest) return scene;
  const at = grid.first + Math.min(highest, Math.max(lowest, Math.round(index(t)))) * grid.bar;
  if (at === scene.sections[k].start_s) return scene;
  const next = structuredClone(scene);
  next.sections[k].start_s = at;
  return next;
}

function span(e: SceneEvent): [number, number] {
  return [e.t_s, e.t_s + e.duration_s + (e.kind === "hold" ? 2 * HOLD_RAMP_S : 0)];
}

export function addEvent(scene: Scene, kind: EventKind, t: number, track: TrackName, grid: BarGrid): EditResult {
  if (scene.events.length >= MAX_EVENTS) return { ok: false, reason: "maxEvents" };
  const event: SceneEvent = {
    t_s: Math.max(0, t),
    kind,
    duration_s: Math.min(MAX_EVENT_S, Math.max(MIN_EVENT_S, EVENT_BARS[kind] * grid.bar)),
    targets: [track],
  };
  const [a0, a1] = span(event);
  const clash = scene.events.some((e) => {
    if (e.kind !== kind || !e.targets.includes(track)) return false;
    const [b0, b1] = span(e);
    return a0 < b1 && b0 < a1;
  });
  if (clash) return { ok: false, reason: kind === "hold" ? "holdExists" : "overheadExists" };
  const next = structuredClone(scene);
  next.events.push(event);
  next.events.sort((x, y) => x.t_s - y.t_s);
  return { ok: true, scene: next };
}

/**
 * 时间轴上事件条的起点：停顿 = 完全停住的那一刻（开始减速后 0.5 秒），飞过头顶 = 开始时刻。
 * 事件条从这里画到 起点 + duration_s；拖动与吸附也都以它为准。
 */
export function eventAnchor(e: SceneEvent): number {
  return e.t_s + (e.kind === "hold" ? HOLD_RAMP_S : 0);
}

/** 第 index 个事件现在所在的空档：前后是同类、共用任一音轨的事件（拖动时不能越过它们）。 */
function freeGap(scene: Scene, index: number, durationS: number): [number, number] {
  const self = scene.events[index];
  const [start, end] = span(self);
  let lo = 0;
  let hi = durationS;
  scene.events.forEach((e, i) => {
    if (i === index || e.kind !== self.kind || !e.targets.some((t) => self.targets.includes(t))) return;
    const [a, b] = span(e);
    if (b <= start) lo = Math.max(lo, b);
    else if (a >= end) hi = Math.min(hi, a);
  });
  return [lo, hi];
}

/** 拖动事件：起点（见 eventAnchor）吸附到拍（free 时不吸附），只在自己的空档里移动；不改数组顺序。 */
export function moveEvent(scene: Scene, index: number, anchor: number, grid: BarGrid, durationS: number, free = false): Scene {
  const e = scene.events[index];
  if (!e) return scene;
  const lead = eventAnchor(e) - e.t_s;
  const [lo, hi] = freeGap(scene, index, durationS);
  const length = span(e)[1] - e.t_s;
  const target = (free ? anchor : snapToBeat(grid, anchor)) - lead;
  const t = Math.min(Math.max(target, lo), Math.max(lo, hi - length));
  if (t === e.t_s) return scene;
  const next = structuredClone(scene);
  next.events[index].t_s = t;
  return next;
}

/** 拖事件条右边缘改长短：终点吸附到拍（free 时不吸附），0.5–30 秒，不压到后面的同类事件、不超出歌曲。 */
export function resizeEvent(scene: Scene, index: number, end: number, grid: BarGrid, durationS: number, free = false): Scene {
  const e = scene.events[index];
  if (!e) return scene;
  const [, hi] = freeGap(scene, index, durationS);
  const ramps = span(e)[1] - e.t_s - e.duration_s; // 停顿前后的减速 / 加速
  const room = hi - e.t_s - ramps;
  const wanted = (free ? end : snapToBeat(grid, end)) - eventAnchor(e);
  const duration = Math.min(Math.max(wanted, MIN_EVENT_S), MAX_EVENT_S, Math.max(MIN_EVENT_S, room));
  if (duration === e.duration_s) return scene;
  const next = structuredClone(scene);
  next.events[index].duration_s = duration;
  return next;
}

export function removeEvent(scene: Scene, index: number): Scene {
  const next = structuredClone(scene);
  next.events.splice(index, 1);
  return next;
}

/** 落在第 k 段里的事件（带原下标，方便删除）。 */
export function eventsIn(scene: Scene, k: number, durationS: number): Array<{ index: number; event: SceneEvent }> {
  const start = scene.sections[k].start_s;
  const end = sectionEnd(scene, k, durationS);
  return scene.events.map((event, index) => ({ index, event })).filter(({ event }) => event.t_s >= start && event.t_s < end);
}

/** 只有一段时整个换成预设（与 v1 行为一致，保留已加的事件）；多段时只换第 k 段的轨道与混响量。 */
export function applyPreset(scene: Scene, preset: Scene, k: number): Scene {
  if (scene.sections.length === 1) return { ...structuredClone(preset), events: structuredClone(scene.events) };
  const next = structuredClone(scene);
  next.sections[k].orbits = structuredClone(preset.sections[0].orbits);
  next.sections[k].wet_db = preset.sections[0].wet_db;
  return next;
}
