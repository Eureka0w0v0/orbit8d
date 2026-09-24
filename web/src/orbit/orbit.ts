// 轨道公式：与 backend/orbit8d/engine/orbit.py 逐点一致（docs/SPEC.md §4）。
// 方位角顺时针（0 正前、90 正右），仰角向上为正，单位度；距离单位米。
// 该模块会在 AudioWorklet 里每 32 个采样调用一次，所以支持传入 out 对象避免分配内存。

import type { Orbit, Shape } from "../types";

export const BEATS_PER_BAR = 4;
const BPM_NORM_LOW = 70;
const BPM_NORM_HIGH = 140;
const SPIRAL_TURNS_PER_CYCLE = 4;
const DEG = Math.PI / 180;

export interface OrbitParams {
  shape: Shape;
  radiusM: number;
  periodS: number;
  direction: 1 | -1;
  startDeg: number;
  heightDeg: number;
  pitchDeg: number;
  rollDeg: number;
  yawDeg: number;
  aspect: number;
  swingDeg: number;
  liftDeg: number;
}

export interface Position {
  az: number;
  el: number;
  dist: number;
}

export function normalizeBpm(bpm: number): number {
  if (!(bpm > 0)) throw new Error(`BPM 必须为正: ${bpm}`);
  let b = bpm;
  while (b < BPM_NORM_LOW) b *= 2;
  while (b >= BPM_NORM_HIGH) b /= 2;
  return b;
}

export function periodSeconds(mode: "bars" | "seconds", bars: number, seconds: number, bpmNorm: number): number {
  return mode === "bars" ? (bars * BEATS_PER_BAR * 60) / bpmNorm : seconds;
}

export function toOrbitParams(orbit: Orbit, bpmNorm: number): OrbitParams {
  return {
    shape: orbit.shape,
    radiusM: orbit.radius_m,
    periodS: periodSeconds(orbit.speed.mode, orbit.speed.bars, orbit.speed.seconds, bpmNorm),
    direction: orbit.direction === "cw" ? 1 : -1,
    startDeg: orbit.start_deg,
    heightDeg: orbit.height_deg,
    pitchDeg: orbit.pitch_deg,
    rollDeg: orbit.roll_deg,
    yawDeg: orbit.yaw_deg,
    aspect: orbit.aspect,
    swingDeg: orbit.swing_deg,
    liftDeg: orbit.lift_deg,
  };
}

/** 相位（度）。fixed 形状与时间无关。 */
export function orbitPhase(p: OrbitParams, t: number, tRef: number, offsetDeg: number): number {
  if (p.shape === "fixed") return p.startDeg + offsetDeg;
  return p.startDeg + offsetDeg + (p.direction * 360 * (t - tRef)) / p.periodS;
}

export function orbitPosition(
  p: OrbitParams,
  t: number,
  tRef: number,
  offsetDeg = 0,
  out: Position = { az: 0, el: 0, dist: 0 },
): Position {
  return positionAtPhase(p, orbitPhase(p, t, tRef, offsetDeg), out);
}

export function positionAtPhase(p: OrbitParams, phi: number, out: Position = { az: 0, el: 0, dist: 0 }): Position {
  const rad = phi * DEG;
  let az: number;
  let el = p.heightDeg;
  let dist = p.radiusM;
  switch (p.shape) {
    case "circle":
    case "fixed":
      az = phi;
      break;
    case "ellipse": {
      const s = Math.sin(rad);
      const c = p.aspect * Math.cos(rad);
      az = Math.atan2(s, c) / DEG;
      dist = p.radiusM * Math.sqrt(s * s + c * c);
      break;
    }
    case "pendulum":
      az = p.swingDeg * Math.sin(rad);
      break;
    case "figure8":
      az = p.swingDeg * Math.sin(rad);
      el = p.heightDeg + p.liftDeg * Math.sin(2 * rad);
      break;
    case "spiral":
      az = phi;
      el = p.heightDeg + p.liftDeg * Math.sin(rad / SPIRAL_TURNS_PER_CYCLE);
      break;
  }
  const a = az * DEG;
  const e = el * DEG;
  let x = Math.cos(e) * Math.sin(a);
  let y = Math.sin(e);
  let z = Math.cos(e) * Math.cos(a);
  const pr = p.pitchDeg * DEG;
  const rr = p.rollDeg * DEG;
  const wr = p.yawDeg * DEG;
  let t1 = y * Math.cos(pr) + z * Math.sin(pr);
  z = -y * Math.sin(pr) + z * Math.cos(pr);
  y = t1;
  t1 = x * Math.cos(rr) - y * Math.sin(rr);
  y = x * Math.sin(rr) + y * Math.cos(rr);
  x = t1;
  t1 = x * Math.cos(wr) + z * Math.sin(wr);
  z = -x * Math.sin(wr) + z * Math.cos(wr);
  x = t1;
  out.az = (((Math.atan2(x, z) / DEG) % 360) + 360) % 360;
  out.el = Math.asin(Math.min(1, Math.max(-1, y))) / DEG;
  out.dist = dist;
  return out;
}
