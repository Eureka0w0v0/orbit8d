// 每条音轨一个轨道视图：彩色管状轨迹 + 发光声源小球（立体声轨是一对小球）。
// 视觉层级：选中音轨醒目，其余音轨细而淡，避免画面杂乱。

import * as THREE from "three";
import { positionAtPhase, type OrbitParams, type Position } from "../orbit/orbit";
import { trackPosition, type TrackMotion } from "../orbit/timeline";
import type { TrackName } from "../types";
import { directionToWorld, visualRadius, type Vec3 } from "./mapping";

export const TRACK_COLORS: Record<TrackName, number> = {
  vocals: 0xff6b9a,
  drums: 0xffc24b,
  bass: 0x8f7dff,
  other: 0x35d6c3,
};

const PATH_SEGMENTS = 360;
const SPIRAL_CYCLES = 4;
const TUBE_RADIUS = { idle: 0.0016, selected: 0.0038 };
const PATH_OPACITY = { idle: 0.16, selected: 0.9, muted: 0.05 };
const SPHERE_RADIUS = 0.026;
const PAIR_SCALE = 0.75; // 立体声轨的一对小球略小
const IDLE_SCALE = 0.72; // 未选中音轨的小球再小一点
const EMISSIVE = { idle: 2.6, selected: 4.0, muted: 0.3 };

export interface OrbitViewState {
  params: OrbitParams;
  widthDeg: number;
  stereo: boolean;
  audible: boolean;
  selected: boolean;
}

export function worldPoint(pos: Position, out = new THREE.Vector3()): THREE.Vector3 {
  const v: Vec3 = directionToWorld(pos.az, pos.el);
  return out.set(v.x, v.y, v.z).multiplyScalar(visualRadius(pos.dist));
}

export class OrbitView {
  readonly group = new THREE.Group();
  readonly spheres: THREE.Mesh[] = [];
  private readonly sphereGeometry = new THREE.SphereGeometry(SPHERE_RADIUS, 24, 16);
  private readonly pathMaterial: THREE.MeshBasicMaterial;
  private readonly sphereMaterial: THREE.MeshStandardMaterial;
  private path: THREE.Mesh | null = null;
  private pathKey = "";
  private state: OrbitViewState | null = null;
  private readonly pos: Position = { az: 0, el: 0, dist: 0 };

  constructor(readonly track: TrackName) {
    const color = TRACK_COLORS[track];
    this.pathMaterial = new THREE.MeshBasicMaterial({ color, transparent: true, depthWrite: false });
    this.sphereMaterial = new THREE.MeshStandardMaterial({ color, emissive: color, roughness: 0.3 });
    this.group.name = `orbit-${track}`;
  }

  get params(): OrbitParams | null {
    return this.state?.params ?? null;
  }

  offsets(): number[] {
    if (!this.state) return [0];
    return this.state.stereo ? [-this.state.widthDeg / 2, this.state.widthDeg / 2] : [0];
  }

  update(state: OrbitViewState): void {
    this.state = state;
    const key = JSON.stringify({ ...state.params, startDeg: 0, periodS: 0, direction: 0, sel: state.selected });
    if (key !== this.pathKey) {
      this.pathKey = key;
      this.rebuildPath(state);
    }
    const count = state.stereo ? 2 : 1;
    while (this.spheres.length < count) this.addSphere();
    while (this.spheres.length > count) this.removeSphere();
    const scale = (state.stereo ? PAIR_SCALE : 1) * (state.selected ? 1 : IDLE_SCALE);
    for (const s of this.spheres) s.scale.setScalar(scale);
    this.sphereMaterial.emissiveIntensity = !state.audible ? EMISSIVE.muted : state.selected ? EMISSIVE.selected : EMISSIVE.idle;
    this.pathMaterial.opacity = !state.audible ? PATH_OPACITY.muted : state.selected ? PATH_OPACITY.selected : PATH_OPACITY.idle;
  }

  private rebuildPath(state: OrbitViewState): void {
    if (this.path) {
      this.group.remove(this.path);
      this.path.geometry.dispose();
      this.path = null;
    }
    if (state.params.shape === "fixed") return;
    const cycles = state.params.shape === "spiral" ? SPIRAL_CYCLES : 1;
    const points: THREE.Vector3[] = [];
    const n = PATH_SEGMENTS * cycles;
    for (let i = 0; i < n; i++) {
      points.push(worldPoint(positionAtPhase(state.params, (360 * cycles * i) / n, this.pos)));
    }
    const curve = new THREE.CatmullRomCurve3(points, true);
    const radius = state.selected ? TUBE_RADIUS.selected : TUBE_RADIUS.idle;
    this.path = new THREE.Mesh(new THREE.TubeGeometry(curve, n, radius, 6, true), this.pathMaterial);
    this.path.userData = { track: this.track, kind: "path" };
    this.group.add(this.path);
  }

  private addSphere(): void {
    const sphere = new THREE.Mesh(this.sphereGeometry, this.sphereMaterial);
    sphere.userData = { track: this.track, kind: "source", index: this.spheres.length };
    this.spheres.push(sphere);
    this.group.add(sphere);
  }

  private removeSphere(): void {
    const sphere = this.spheres.pop();
    if (sphere) this.group.remove(sphere);
  }

  /** 按歌曲时间摆放声源小球（与音频渲染用同一条时间轴：段落过渡、停顿、飞过头顶都看得见）。 */
  setPositions(motion: TrackMotion, songTime: number): void {
    if (!this.state) return;
    const offsets = this.offsets();
    this.spheres.forEach((sphere, i) => {
      trackPosition(motion, songTime, offsets[i], this.pos);
      worldPoint(this.pos, sphere.position);
    });
  }
}
