// 3D 拖拽：点击轨道或小球选中音轨；选中音轨的轨道左侧有一个白色圆点，拖它改距离；
// 暂停时可直接拖小球改起点。倾斜由面板里的倾斜盘负责，这里不再放倾斜把手，保持画面简洁。

import * as THREE from "three";
import { positionAtPhase, type OrbitParams, type Position } from "../orbit/orbit";
import type { Orbit, TrackName } from "../types";
import { localToWorld, orbitLocalAngle, phaseDraggable, phaseForLocalAngle, radiusFromVisual } from "./mapping";
import { worldPoint, type OrbitView } from "./orbits";
import type { Stage } from "./stage";

type DragKind = "radius" | "phase";

const KNOB_RADIUS = 0.012;
const KNOB_COLOR = 0xffffff;
const RADIUS_KNOB_PHASE = 270; // 轨道左侧
const FIXED_KNOB_PUSH = 1.18;
const RADIUS_STEP = 0.01;

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
  sourceIndex: number;
}

const wrap180 = (d: number) => ((((d + 180) % 360) + 360) % 360) - 180;

export class Handles {
  private readonly knob: THREE.Mesh;
  private readonly raycaster = new THREE.Raycaster();
  private readonly pointer = new THREE.Vector2();
  private readonly plane = new THREE.Plane();
  private readonly hit = new THREE.Vector3();
  private readonly pos: Position = { az: 0, el: 0, dist: 0 };
  private drag: DragState | null = null;

  constructor(private readonly stage: Stage, private readonly deps: HandleDeps) {
    this.knob = new THREE.Mesh(
      new THREE.SphereGeometry(KNOB_RADIUS, 20, 14),
      new THREE.MeshStandardMaterial({ color: KNOB_COLOR, emissive: KNOB_COLOR, emissiveIntensity: 0.5 }),
    );
    this.knob.userData = { kind: "radius" };
    stage.scene.add(this.knob);
    const el = stage.renderer.domElement;
    el.addEventListener("pointerdown", this.onDown);
    el.addEventListener("pointermove", this.onMove);
    window.addEventListener("pointerup", this.onUp);
  }

  /** 每帧把距离圆点放到选中音轨的轨道左侧（固定声源则放在声源外侧）。 */
  update(): void {
    const p = this.deps.params(this.deps.selected());
    if (p.shape === "fixed") {
      worldPoint(positionAtPhase(p, p.startDeg, this.pos), this.knob.position).multiplyScalar(FIXED_KNOB_PUSH);
    } else {
      worldPoint(positionAtPhase(p, RADIUS_KNOB_PHASE, this.pos), this.knob.position);
    }
  }

  private setPointer(e: PointerEvent): void {
    const rect = this.stage.renderer.domElement.getBoundingClientRect();
    this.pointer.set(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(this.pointer, this.stage.camera);
  }

  private pick(): THREE.Intersection | null {
    const targets: THREE.Object3D[] = [this.knob];
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
    const data = hit.object.userData as { kind?: string; track?: TrackName; index?: number };
    const selected = this.deps.selected();
    if (data.track && data.track !== selected) {
      this.deps.select(data.track);
      return;
    }
    let kind: DragKind | null = null;
    if (data.kind === "radius") kind = "radius";
    else if (data.kind === "source" && !this.deps.playing() && phaseDraggable(this.deps.params(selected))) kind = "phase";
    if (!kind) return;
    this.drag = { kind, track: selected, sourceIndex: data.index ?? 0 };
    this.stage.controls.enabled = false;
    (e.target as Element).setPointerCapture?.(e.pointerId);
  };

  private onMove = (e: PointerEvent): void => {
    this.setPointer(e);
    if (!this.drag) {
      const kind = (this.pick()?.object.userData as { kind?: string } | undefined)?.kind;
      this.stage.renderer.domElement.style.cursor = kind === "radius" || kind === "source" ? "grab" : kind ? "pointer" : "";
      return;
    }
    const { kind, track } = this.drag;
    if (!this.raycaster.ray.intersectPlane(this.orbitPlane(track), this.hit)) return;
    if (kind === "radius") {
      const r = radiusFromVisual(this.hit.length());
      this.deps.change(track, { radius_m: Math.round(r / RADIUS_STEP) * RADIUS_STEP });
      return;
    }
    const p = this.deps.params(track);
    const offset = this.deps.views.get(track)?.offsets()[this.drag.sourceIndex] ?? 0;
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
