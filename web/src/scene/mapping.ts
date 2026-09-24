// 音频坐标（x 右、y 上、z 前）↔ Three.js 世界坐标，以及轨道半径的可视化压缩。
// 人头模型面朝世界 +z，所以听者右侧 = 世界 -x。所有 3D 位置换算都只经过这里。

import type { OrbitParams } from "../orbit/orbit";

const DEG = Math.PI / 180;
const MIN_R = 0.5;
const MAX_R = 4;
const VISUAL_BASE = 0.32; // 0.5 m 的轨道画在离耳朵中点 0.32 个单位处（人头高约 0.28）

export interface Vec3 {
  x: number;
  y: number;
  z: number;
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/** 真实距离 0.5–4 m 压缩成画面上的半径（开平方，近处变化更明显）。 */
export function visualRadius(distM: number): number {
  return VISUAL_BASE * Math.sqrt(clamp(distM, MIN_R, MAX_R) / MIN_R);
}

export function radiusFromVisual(v: number): number {
  return clamp(MIN_R * (v / VISUAL_BASE) ** 2, MIN_R, MAX_R);
}

/**
 * 固定像素大小的精灵（sizeAttenuation = false）要设的 scale：
 * 透视投影下精灵高度 = scale / tan(fov/2) × 视口高 / 2 像素，反过来求 scale。
 */
export function spriteScaleForPixels(px: number, fovDeg: number, viewportHeightPx: number): number {
  return (2 * px * Math.tan((fovDeg * DEG) / 2)) / Math.max(1, viewportHeightPx);
}

export function directionToWorld(azDeg: number, elDeg: number, out: Vec3 = { x: 0, y: 0, z: 0 }): Vec3 {
  const a = azDeg * DEG;
  const e = elDeg * DEG;
  out.x = -Math.cos(e) * Math.sin(a);
  out.y = Math.sin(e);
  out.z = Math.cos(e) * Math.cos(a);
  return out;
}

export function worldToDirection(x: number, y: number, z: number): { az: number; el: number } {
  const len = Math.hypot(x, y, z) || 1;
  const az = ((Math.atan2(-x, z) / DEG) % 360 + 360) % 360;
  return { az, el: Math.asin(clamp(y / len, -1, 1)) / DEG };
}

/** 世界方向 → 轨道局部坐标里的方位角（撤销 yaw、roll、pitch 旋转）。 */
export function orbitLocalAngle(p: OrbitParams, wx: number, wy: number, wz: number): number {
  let x = -wx; // 世界 → 音频坐标
  let y = wy;
  let z = wz;
  const w = -p.yawDeg * DEG;
  let t = x * Math.cos(w) + z * Math.sin(w);
  z = -x * Math.sin(w) + z * Math.cos(w);
  x = t;
  const r = -p.rollDeg * DEG;
  t = x * Math.cos(r) - y * Math.sin(r);
  y = x * Math.sin(r) + y * Math.cos(r);
  x = t;
  const pr = -p.pitchDeg * DEG;
  z = -y * Math.sin(pr) + z * Math.cos(pr);
  return ((Math.atan2(x, z) / DEG) % 360 + 360) % 360;
}

/** 局部方位角 → 相位 φ（圆/螺旋/固定：相位即方位；椭圆：tan φ = a·tan az）。 */
export function phaseForLocalAngle(p: OrbitParams, azLocal: number): number {
  if (p.shape !== "ellipse") return azLocal;
  const a = azLocal * DEG;
  return ((Math.atan2(p.aspect * Math.sin(a), Math.cos(a)) / DEG) % 360 + 360) % 360;
}

/** 这些形状的相位可以从位置反推（拖小球改起点）；钟摆和 8 字只能用滑杆。 */
export function phaseDraggable(p: OrbitParams): boolean {
  return p.shape === "circle" || p.shape === "ellipse" || p.shape === "spiral" || p.shape === "fixed";
}

/** 轨道局部向量 → 世界坐标（依次 pitch、roll、yaw，再换到世界坐标）。用于求轨道平面法线等。 */
export function localToWorld(p: OrbitParams, lx: number, ly: number, lz: number, out: Vec3 = { x: 0, y: 0, z: 0 }): Vec3 {
  const pr = p.pitchDeg * DEG;
  const rr = p.rollDeg * DEG;
  const wr = p.yawDeg * DEG;
  let x = lx;
  let y = ly * Math.cos(pr) + lz * Math.sin(pr);
  let z = -ly * Math.sin(pr) + lz * Math.cos(pr);
  const t = x * Math.cos(rr) - y * Math.sin(rr);
  y = x * Math.sin(rr) + y * Math.cos(rr);
  x = t;
  out.x = -(x * Math.cos(wr) + z * Math.sin(wr));
  out.y = y;
  out.z = -x * Math.sin(wr) + z * Math.cos(wr);
  return out;
}
