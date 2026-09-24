// 轨道朝向的两种表示互转：
//   1) 数据里的三个角：前后倾斜 pitch、左右倾斜 roll、水平转向 yaw（与 orbit.ts 同一套旋转约定）；
//   2) 倾斜盘上的直观表示：轨道最高点朝哪个方向（azDeg，顺时针，0 = 正前）、翘起多少度（tiltDeg，0–90）。
// 倾斜盘写回时统一用 pitch = 翘起角度、roll = 0、yaw = 翘起方向，这种写法能表示任意朝向的平面。

const DEG = Math.PI / 180;
const MATCH_TOLERANCE_DEG = 1;
const VERTICAL_DEG = 89;

export interface Tilt {
  azDeg: number;
  tiltDeg: number;
}

export interface Orientation {
  pitch_deg: number;
  roll_deg: number;
  yaw_deg: number;
}

/** 朝向快捷键（显示名见 i18n 的 direction）。 */
export const DIRECTIONS: ReadonlyArray<{ key: string; tilt: Tilt }> = [
  { key: "horizontal", tilt: { azDeg: 0, tiltDeg: 0 } },
  { key: "ears", tilt: { azDeg: 0, tiltDeg: 90 } },
  { key: "frontBack", tilt: { azDeg: 90, tiltDeg: 90 } },
  { key: "diagRight", tilt: { azDeg: 45, tiltDeg: 45 } },
  { key: "diagLeft", tilt: { azDeg: -45, tiltDeg: 45 } },
];

const wrap180 = (d: number) => {
  const w = (((d + 180) % 360) + 360) % 360 - 180;
  return w === -180 ? 180 : w;
};

/** 轨道平面法线（音频坐标：x 右、y 上、z 前），与 orbit.ts 的 Ry·Rz·Rx 顺序一致。 */
function normal(o: Orientation): [number, number, number] {
  const p = o.pitch_deg * DEG;
  const r = o.roll_deg * DEG;
  const w = o.yaw_deg * DEG;
  const x = -Math.cos(p) * Math.sin(r);
  const y = Math.cos(p) * Math.cos(r);
  const z = -Math.sin(p);
  return [x * Math.cos(w) + z * Math.sin(w), y, -x * Math.sin(w) + z * Math.cos(w)];
}

export function tiltFromOrientation(o: Orientation): Tilt {
  let [x, y, z] = normal(o);
  if (y < 0) [x, y, z] = [-x, -y, -z]; // 平面的法线正反等价，取朝上的那一侧
  const tiltDeg = Math.acos(Math.min(1, Math.max(-1, y))) / DEG;
  if (tiltDeg < 1e-9) return { azDeg: 0, tiltDeg: 0 };
  return { azDeg: wrap180(Math.atan2(-x, -z) / DEG), tiltDeg }; // 最高点在法线水平分量的反方向
}

export function orientationFromTilt(t: Tilt): Orientation {
  return { pitch_deg: t.tiltDeg, roll_deg: 0, yaw_deg: t.tiltDeg === 0 ? 0 : wrap180(t.azDeg) };
}

/** 当前朝向若与某个方向快捷键一致，返回它的 key；竖直时方向相差 180° 视为同一平面。 */
export function matchDirection(o: Orientation): string | null {
  const cur = tiltFromOrientation(o);
  for (const d of DIRECTIONS) {
    if (Math.abs(cur.tiltDeg - d.tilt.tiltDeg) > MATCH_TOLERANCE_DEG) continue;
    if (d.tilt.tiltDeg === 0) return d.key;
    const diff = Math.abs(wrap180(cur.azDeg - d.tilt.azDeg));
    const planeDiff = cur.tiltDeg >= VERTICAL_DEG ? Math.min(diff, 180 - diff) : diff;
    if (planeDiff <= MATCH_TOLERANCE_DEG) return d.key;
  }
  return null;
}
