// 场景编辑（纯函数，不改入参）：段落切分 / 删除 / 拖分界、事件增删、预设套用到当前段。
// 规则与后端 Scene 校验一致（第一段从 0 秒开始、分界严格递增、同一音轨同类事件不重叠），不合法的操作返回原因。

import { barSeconds } from "../orbit/timeline";
import type { EventKind, Scene, SceneEvent, TrackName } from "../types";
import { HOLD_RAMP_S } from "../types";

export const MAX_SECTIONS = 16;
export const MAX_EVENTS = 64;
const MIN_SECTION_BARS = 1;
const EVENT_BARS: Record<EventKind, number> = { hold: 1, overhead: 2 };
const MAX_EVENT_S = 30;
const MIN_EVENT_S = 0.5;

export type EditResult = { ok: true; scene: Scene } | { ok: false; reason: string };

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

export function sectionIndexAt(scene: Scene, t: number): number {
  let k = 0;
  while (k + 1 < scene.sections.length && scene.sections[k + 1].start_s <= t) k++;
  return k;
}

export function sectionEnd(scene: Scene, k: number, durationS: number): number {
  return k + 1 < scene.sections.length ? scene.sections[k + 1].start_s : Math.max(durationS, scene.sections[k].start_s);
}

export function splitAt(scene: Scene, t: number, grid: BarGrid, durationS: number): EditResult {
  if (scene.sections.length >= MAX_SECTIONS) return { ok: false, reason: `最多 ${MAX_SECTIONS} 段` };
  const k = sectionIndexAt(scene, t);
  const at = snapToBar(grid, t);
  const minGap = MIN_SECTION_BARS * grid.bar * 0.999;
  if (at - scene.sections[k].start_s < minGap || sectionEnd(scene, k, durationS) - at < minGap) {
    return { ok: false, reason: "离段落边界太近（每段至少 1 小节）" };
  }
  const next = structuredClone(scene);
  next.sections.splice(k + 1, 0, { ...structuredClone(scene.sections[k]), start_s: at });
  return { ok: true, scene: next };
}

export function removeSection(scene: Scene, k: number): EditResult {
  if (scene.sections.length <= 1) return { ok: false, reason: "只剩一段了" };
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
  if (scene.events.length >= MAX_EVENTS) return { ok: false, reason: `最多 ${MAX_EVENTS} 个事件` };
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
  if (clash) return { ok: false, reason: kind === "hold" ? "这里已经有停顿了" : "这里已经在飞过头顶了" };
  const next = structuredClone(scene);
  next.events.push(event);
  next.events.sort((x, y) => x.t_s - y.t_s);
  return { ok: true, scene: next };
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
