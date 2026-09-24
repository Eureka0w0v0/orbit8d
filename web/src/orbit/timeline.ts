// 时间轴运动：与 backend/orbit8d/engine/timeline.py 逐点一致（shared/golden/timeline_vectors.json，docs/SPEC.md §13.3）。
// - 相位：按“当前段的角速度 × 停顿系数”从 0 秒积分，t_ref 处为 0；角速度分段线性，积分有精确公式。
// - 段落过渡：分界前后各半小节（不超过相邻两段较短者的 40%），方向球面插值、距离线性插值。
// - 停顿：0.5 秒减速到 0 → 停住 → 0.5 秒加速回来；飞过头顶：sin² 钟形权重朝正上方插值。
// 编译结果是纯数据（可以 postMessage 给 AudioWorklet）；逐块求位置时不分配内存。

import type { Scene, TrackName } from "../types";
import { HOLD_RAMP_S } from "../types";
import { BEATS_PER_BAR, positionAtPhase, toOrbitParams, type OrbitParams, type Position } from "./orbit";

const TRANSITION_BARS = 1;
const MAX_TRANSITION_SHARE = 0.4;
const PARALLEL_EPS = 1e-6;
const DEG = Math.PI / 180;

export interface SectionMotion {
  startS: number;
  params: OrbitParams;
  omega: number; // 度/秒；固定形状为 0
}

export interface TrackMotion {
  sections: SectionMotion[];
  starts: Float64Array;
  half: Float64Array; // half[k]：第 k 段开头那条分界的过渡半宽（half[0] 不用）
  segT0: Float64Array; // 相位积分分段起点（升序，首项 0）
  segF0: Float64Array;
  segW0: Float64Array;
  segSlope: Float64Array;
  fRef: number;
  overheads: Float64Array; // [t0, d0, t1, d1, ...]
}

export interface WetCurve {
  starts: Float64Array;
  half: Float64Array;
  wets: Float64Array;
}

export function barSeconds(bpmNorm: number): number {
  return (BEATS_PER_BAR * 60) / bpmNorm;
}

function halfWidths(starts: Float64Array, bpmNorm: number, durationS: number): Float64Array {
  const bar = barSeconds(bpmNorm);
  const n = starts.length;
  const half = new Float64Array(n);
  const end = (k: number) => (k + 1 < n ? starts[k + 1] : Math.max(durationS, starts[n - 1]));
  for (let k = 1; k < n; k++) {
    const shorter = Math.min(end(k - 1) - starts[k - 1], end(k) - starts[k]);
    half[k] = Math.max(0, Math.min((TRANSITION_BARS * bar) / 2, MAX_TRANSITION_SHARE * shorter));
  }
  return half;
}

function holdFactor(t: number, holds: ReadonlyArray<readonly [number, number]>): number {
  for (const [ts, d] of holds) {
    if (ts <= t && t <= ts + 2 * HOLD_RAMP_S + d) {
      if (t < ts + HOLD_RAMP_S) return 1 - (t - ts) / HOLD_RAMP_S;
      if (t <= ts + HOLD_RAMP_S + d) return 0;
      return (t - (ts + HOLD_RAMP_S + d)) / HOLD_RAMP_S;
    }
  }
  return 1;
}

/** numpy.searchsorted(a, x, side="right")：第一个大于 x 的下标。 */
function searchRight(a: Float64Array, x: number): number {
  let lo = 0;
  let hi = a.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (a[mid] <= x) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

const byStart = (a: readonly [number, number], b: readonly [number, number]) => a[0] - b[0] || a[1] - b[1];

export function compileTrack(scene: Scene, track: TrackName, bpmNorm: number, tRef: number, durationS: number): TrackMotion {
  const sections = scene.sections.map((sec): SectionMotion => {
    const params = toOrbitParams(sec.orbits[track], bpmNorm);
    return { startS: sec.start_s, params, omega: params.shape === "fixed" ? 0 : (params.direction * 360) / params.periodS };
  });
  const starts = Float64Array.from(sections, (s) => s.startS);
  const pick = (kind: "hold" | "overhead") =>
    scene.events
      .filter((e) => e.kind === kind && e.targets.includes(track))
      .map((e) => [e.t_s, e.duration_s] as const)
      .sort(byStart);
  const holds = pick("hold");
  const overheads = pick("overhead");

  const points = [0, ...Array.from(starts.subarray(1))];
  for (const [ts, d] of holds) points.push(ts, ts + HOLD_RAMP_S, ts + HOLD_RAMP_S + d, ts + 2 * HOLD_RAMP_S + d);
  const t0 = Float64Array.from(new Set(points.filter((p) => p >= 0)).values()).sort();
  const omegaAt = (t: number) => sections[Math.max(searchRight(starts, t) - 1, 0)].omega;
  const n = t0.length;
  const w0 = new Float64Array(n);
  const slope = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const omega = omegaAt(t0[i]);
    w0[i] = omega * holdFactor(t0[i], holds);
    if (i + 1 < n) slope[i] = (omega * holdFactor(t0[i + 1], holds) - w0[i]) / (t0[i + 1] - t0[i]);
  }
  const f0 = new Float64Array(n);
  for (let i = 1; i < n; i++) {
    const d = t0[i] - t0[i - 1];
    f0[i] = f0[i - 1] + w0[i - 1] * d + (slope[i - 1] * d * d) / 2;
  }
  const motion: TrackMotion = {
    sections,
    starts,
    half: halfWidths(starts, bpmNorm, durationS),
    segT0: t0,
    segF0: f0,
    segW0: w0,
    segSlope: slope,
    fRef: 0,
    overheads: Float64Array.from(overheads.flat()),
  };
  motion.fRef = integral(motion, tRef);
  return motion;
}

function integral(m: TrackMotion, t: number): number {
  const i = Math.min(Math.max(searchRight(m.segT0, t) - 1, 0), m.segT0.length - 1);
  const d = t - m.segT0[i];
  return m.segF0[i] + m.segW0[i] * d + (m.segSlope[i] * d * d) / 2;
}

/** 累积相位（度），t_ref 处为 0。 */
export function phaseAt(m: TrackMotion, t: number): number {
  return integral(m, t) - m.fRef;
}

const smoothstep = (x: number) => {
  const c = Math.min(1, Math.max(0, x));
  return c * c * (3 - 2 * c);
};

interface Blend {
  a: number;
  b: number;
  w: number;
}

function blendPlan(starts: Float64Array, half: Float64Array, t: number, out: Blend): Blend {
  const n = starts.length;
  const k = Math.min(Math.max(searchRight(starts, t) - 1, 0), n - 1);
  out.a = k;
  out.b = k;
  out.w = 0;
  const next = Math.min(k + 1, n - 1);
  if (k + 1 < n && half[next] > 0 && t >= starts[next] - half[next]) {
    out.b = k + 1;
    out.w = smoothstep((t - (starts[next] - half[next])) / (2 * half[next]));
  } else if (k >= 1 && half[k] > 0 && t < starts[k] + half[k]) {
    out.a = k - 1;
    out.w = smoothstep((t - (starts[k] - half[k])) / (2 * half[k]));
  }
  return out;
}

type Vec = [number, number, number];

function toVec(az: number, el: number, out: Vec): Vec {
  const a = az * DEG;
  const e = el * DEG;
  out[0] = Math.cos(e) * Math.sin(a);
  out[1] = Math.sin(e);
  out[2] = Math.cos(e) * Math.cos(a);
  return out;
}

/** 球面插值（结果写回 a）；几乎同向时归一化线性插值，正好相反时绕一条垂直轴转过去。 */
function slerpInto(a: Vec, b: Vec, w: number): void {
  const dot = Math.min(1, Math.max(-1, a[0] * b[0] + a[1] * b[1] + a[2] * b[2]));
  if (dot > 1 - PARALLEL_EPS) {
    const x = (1 - w) * a[0] + w * b[0];
    const y = (1 - w) * a[1] + w * b[1];
    const z = (1 - w) * a[2] + w * b[2];
    const norm = Math.hypot(x, y, z);
    a[0] = x / norm;
    a[1] = y / norm;
    a[2] = z / norm;
  } else if (dot < -1 + PARALLEL_EPS) {
    let px = -a[2]; // a × (0, 1, 0)
    let py = 0;
    let pz = a[0];
    if (Math.hypot(px, py, pz) < PARALLEL_EPS) {
      px = 0; // a × (1, 0, 0)
      py = a[2];
      pz = -a[1];
    }
    const norm = Math.hypot(px, py, pz);
    const c = Math.cos(Math.PI * w);
    const s = Math.sin(Math.PI * w);
    a[0] = c * a[0] + (s * px) / norm;
    a[1] = c * a[1] + (s * py) / norm;
    a[2] = c * a[2] + (s * pz) / norm;
  } else {
    const omega = Math.acos(dot);
    const s = Math.sin(omega);
    const wa = Math.sin((1 - w) * omega) / s;
    const wb = Math.sin(w * omega) / s;
    a[0] = wa * a[0] + wb * b[0];
    a[1] = wa * a[1] + wb * b[1];
    a[2] = wa * a[2] + wb * b[2];
  }
}

const UP: Vec = [0, 1, 0];
const scratch = { blend: { a: 0, b: 0, w: 0 } as Blend, pos: { az: 0, el: 0, dist: 0 } as Position, va: [0, 0, 0] as Vec, vb: [0, 0, 0] as Vec };

function sectionPoint(sec: SectionMotion, phi: number, offsetDeg: number, vec: Vec): number {
  const p = sec.params;
  const pos = positionAtPhase(p, p.shape === "fixed" ? p.startDeg + offsetDeg : p.startDeg + offsetDeg + phi, scratch.pos);
  toVec(pos.az, pos.el, vec);
  return pos.dist;
}

/** 时刻 t 的 (方位角 [0,360), 仰角, 距离)。 */
export function trackPosition(m: TrackMotion, t: number, offsetDeg = 0, out: Position = { az: 0, el: 0, dist: 0 }): Position {
  const phi = phaseAt(m, t);
  const { a, b, w } = blendPlan(m.starts, m.half, t, scratch.blend);
  const va = scratch.va;
  let dist = sectionPoint(m.sections[a], phi, offsetDeg, va);
  if (b !== a) {
    const distB = sectionPoint(m.sections[b], phi, offsetDeg, scratch.vb);
    slerpInto(va, scratch.vb, w);
    dist = (1 - w) * dist + w * distB;
  }
  for (let i = 0; i < m.overheads.length; i += 2) {
    const ts = m.overheads[i];
    const d = m.overheads[i + 1];
    if (t >= ts && t <= ts + d) slerpInto(va, UP, Math.sin((Math.PI * (t - ts)) / d) ** 2);
  }
  out.az = (((Math.atan2(va[0], va[2]) / DEG) % 360) + 360) % 360;
  out.el = Math.asin(Math.min(1, Math.max(-1, va[1]))) / DEG;
  out.dist = dist;
  return out;
}

export function compileWet(scene: Scene, bpmNorm: number, durationS: number): WetCurve {
  const starts = Float64Array.from(scene.sections, (s) => s.start_s);
  return { starts, half: halfWidths(starts, bpmNorm, durationS), wets: Float64Array.from(scene.sections, (s) => s.wet_db) };
}

/** 时刻 t 送进混响的增益（dB），段落交界处与轨道同步平滑过渡。 */
export function wetDbAt(c: WetCurve, t: number): number {
  const { a, b, w } = blendPlan(c.starts, c.half, t, scratch.blend);
  return (1 - w) * c.wets[a] + w * c.wets[b];
}
