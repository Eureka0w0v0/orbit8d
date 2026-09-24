// 拖拽交互：点击轨道或小球选中音轨；选中音轨显示 3 个把手（半径 / 前后倾斜 / 左右倾斜）；
// 暂停时可直接拖小球改起点。拖动期间暂停相机控制。

import * as THREE from "three";
import { positionAtPhase, type OrbitParams, type Position } from "../orbit/orbit";
import type { Orbit, TrackName } from "../types";
import { localToWorld, orbitLocalAngle, phaseDraggable, phaseForLocalAngle, radiusFromVisual } from "./mapping";
import { worldPoint, type OrbitView } from "./orbits";
import type { Stage } from "./stage";

type DragKind = "radius" | "pitch" | "roll" | "phase";

const DEG_PER_PIXEL = 0.35;
const TILT_LIMIT = 90;
const KNOB_RADIUS = 0.017;
const KNOB_COLOR = { radius: 0xffffff, tilt: 0xb9c3ff };
const FIXED_KNOB_PUSH = 1.18;

export interface HandleDeps {
  views: Map<TrackName, OrbitView>;
  selected(): TrackName;
  params(track: TrackName): OrbitParams;
  orbit(track: TrackName): Orbit;
  playing(): boolean;
  songTime(): number;
  tRef(): number;
  select(track: TrackName): void;
  change(track: TrackName, patch: Partial<Orbit>): void;
}

interface DragState {
  kind: DragKind;
  track: TrackName;
  startY: number;
  startValue: number;
  sourceIndex: number;
}

const wrap180 = (d: number) => ((((d + 180) % 360) + 360) % 360) - 180;
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

export class Handles {
  private readonly knobs = new THREE.Group();
  private readonly knob: Record<"radius" | "pitch" | "roll", THREE.Mesh>;
  private readonly raycaster = new THREE.Raycaster();
  private readonly pointer = new THREE.Vector2();
  private readonly plane = new THREE.Plane();
  private readonly hit = new THREE.Vector3();
  private readonly pos: Position = { az: 0, el: 0, dist: 0 };
  private drag: DragState | null = null;

  constructor(private readonly stage: Stage, private readonly deps: HandleDeps) {
    const sphere = new THREE.SphereGeometry(KNOB_RADIUS, 20, 14);
    const torus = new THREE.TorusGeometry(KNOB_RADIUS, KNOB_RADIUS * 0.35, 10, 24);
    const make = (geo: THREE.BufferGeometry, color: number, kind: DragKind) => {
      const m = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color, emissive: color, emissiveIntensity: 0.6 }));
      m.userData = { kind };
      this.knobs.add(m);
      return m;
    };
    this.knob = {
      radius: make(sphere, KNOB_COLOR.radius, "radius"),
      pitch: make(torus, KNOB_COLOR.tilt, "pitch"),
      roll: make(torus, KNOB_COLOR.tilt, "roll"),
    };
    stage.scene.add(this.knobs);
    const el = stage.renderer.domElement;
    el.addEventListener("pointerdown", this.onDown);
    el.addEventListener("pointermove", this.onMove);
    window.addEventListener("pointerup", this.onUp);
  }

  /** 每帧把把手放到选中音轨的轨道上（左侧 = 半径，正前 = 前后倾斜，右侧 = 左右倾斜）。 */
  update(): void {
    const p = this.deps.params(this.deps.selected());
    const fixed = p.shape === "fixed";
    this.knob.pitch.visible = !fixed;
    this.knob.roll.visible = !fixed;
    if (fixed) {
      worldPoint(positionAtPhase(p, p.startDeg, this.pos), this.knob.radius.position).multiplyScalar(FIXED_KNOB_PUSH);
      return;
    }
    worldPoint(positionAtPhase(p, 270, this.pos), this.knob.radius.position);
    worldPoint(positionAtPhase(p, 0, this.pos), this.knob.pitch.position);
    worldPoint(positionAtPhase(p, 90, this.pos), this.knob.roll.position);
    for (const k of [this.knob.pitch, this.knob.roll]) k.lookAt(this.stage.camera.position);
  }

  private setPointer(e: PointerEvent): void {
    const rect = this.stage.renderer.domElement.getBoundingClientRect();
    this.pointer.set(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(this.pointer, this.stage.camera);
  }

  private pick(): THREE.Intersection | null {
    const targets: THREE.Object3D[] = [...this.knobs.children.filter((k) => k.visible)];
    for (const view of this.deps.views.values()) targets.push(...view.group.children);
    return this.raycaster.intersectObjects(targets, false)[0] ?? null;
  }

  private orbitPlane(track: TrackName): THREE.Plane {
    const n = localToWorld(this.deps.params(track), 0, 1, 0);
    return this.plane.set(new THREE.Vector3(n.x, n.y, n.z), 0);
  }

  private onDown = (e: PointerEvent): void => {
    if (e.button !== 0) return;
    this.setPointer(e);
    const hit = this.pick();
    if (!hit) return;
    const data = hit.object.userData as { kind?: DragKind | "path" | "source"; track?: TrackName; index?: number };
    const selected = this.deps.selected();
    if (data.track && data.track !== selected) {
      this.deps.select(data.track);
      return;
    }
    const orbit = this.deps.orbit(selected);
    let kind: DragKind | null = null;
    let startValue = 0;
    if (data.kind === "radius") [kind, startValue] = ["radius", orbit.radius_m];
    else if (data.kind === "pitch") [kind, startValue] = ["pitch", orbit.pitch_deg];
    else if (data.kind === "roll") [kind, startValue] = ["roll", orbit.roll_deg];
    else if (data.kind === "source" && !this.deps.playing() && phaseDraggable(this.deps.params(selected))) {
      [kind, startValue] = ["phase", orbit.start_deg];
    }
    if (!kind) return;
    this.drag = { kind, track: selected, startY: e.clientY, startValue, sourceIndex: data.index ?? 0 };
    this.stage.controls.enabled = false;
    (e.target as Element).setPointerCapture?.(e.pointerId);
  };

  private onMove = (e: PointerEvent): void => {
    this.setPointer(e);
    if (!this.drag) {
      const hit = this.pick();
      const kind = (hit?.object.userData as { kind?: string } | undefined)?.kind;
      this.stage.renderer.domElement.style.cursor = kind && kind !== "path" ? "grab" : hit ? "pointer" : "";
      return;
    }
    const { kind, track, startY, startValue } = this.drag;
    if (kind === "pitch" || kind === "roll") {
      const value = clamp(startValue - (e.clientY - startY) * DEG_PER_PIXEL, -TILT_LIMIT, TILT_LIMIT);
      this.deps.change(track, kind === "pitch" ? { pitch_deg: value } : { roll_deg: value });
      return;
    }
    if (!this.raycaster.ray.intersectPlane(this.orbitPlane(track), this.hit)) return;
    if (kind === "radius") {
      this.deps.change(track, { radius_m: Math.round(radiusFromVisual(this.hit.length()) * 100) / 100 });
      return;
    }
    const p = this.deps.params(track);
    const view = this.deps.views.get(track);
    const offset = view?.offsets()[this.drag.sourceIndex] ?? 0;
    const target = phaseForLocalAngle(p, orbitLocalAngle(p, this.hit.x, this.hit.y, this.hit.z));
    const advance = p.shape === "fixed" ? 0 : (p.direction * 360 * (this.deps.songTime() - this.deps.tRef())) / p.periodS;
    this.deps.change(track, { start_deg: Math.round(wrap180(target - offset - advance)) });
  };

  private onUp = (): void => {
    if (!this.drag) return;
    this.drag = null;
    this.stage.controls.enabled = true;
  };

  dispose(): void {
    const el = this.stage.renderer.domElement;
    el.removeEventListener("pointerdown", this.onDown);
    el.removeEventListener("pointermove", this.onMove);
    window.removeEventListener("pointerup", this.onUp);
  }
}
