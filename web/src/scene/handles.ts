// 3D 拖拽：点击轨道或色点选中音轨；选中音轨的轨道左侧有一个白色小圆环，拖它改距离；
// 暂停时可直接拖色点改起点（改的是播放头所在那一段）。倾斜由面板里的倾斜盘负责，这里不放倾斜把手，保持画面简洁。

import * as THREE from "three";
import { positionAtPhase, type OrbitParams, type Position } from "../orbit/orbit";
import type { Orbit, TrackName } from "../types";
import { localToWorld, orbitLocalAngle, phaseDraggable, phaseForLocalAngle, radiusFromVisual } from "./mapping";
import { DOT_RENDER_ORDER, worldPoint, type OrbitView } from "./orbits";
import type { Stage } from "./stage";

type DragKind = "radius" | "phase";

const KNOB_PX = 22; // 贴图边长（像素）；圆环约占一半，其余透明区域扩大点击范围
const KNOB_TEXTURE_PX = 64;
const KNOB_RING = { outer: 16, inner: 10.5, outline: 18 }; // 以 64 像素贴图计
const RADIUS_KNOB_PHASE = 270; // 轨道左侧
const FIXED_KNOB_PUSH = 1.18;
const RADIUS_STEP = 0.01;

export interface HandleDeps {
  views: Map<TrackName, OrbitView>;
  selected(): TrackName;
  params(track: TrackName): OrbitParams;
  orbit(track: TrackName): Orbit;
  playing(): boolean;
  /** 该音轨在当前播放时刻的累积相位（度，不含起点与声像偏移）。 */
  phase(track: TrackName): number;
  select(track: TrackName): void;
  change(track: TrackName, patch: Partial<Orbit>): void;
}

interface DragState {
  kind: DragKind;
  track: TrackName;
}

const wrap180 = (d: number) => ((((d + 180) % 360) + 360) % 360) - 180;

/** 白色空心圆环（深色描边），与实心色点区分开。 */
function knobTexture(): THREE.CanvasTexture {
  const canvas = document.createElement("canvas");
  canvas.width = KNOB_TEXTURE_PX;
  canvas.height = KNOB_TEXTURE_PX;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("无法创建 2D 画布");
  const c = KNOB_TEXTURE_PX / 2;
  const ring = (outer: number, inner: number, fill: string) => {
    ctx.beginPath();
    ctx.arc(c, c, outer, 0, Math.PI * 2);
    ctx.arc(c, c, inner, 0, Math.PI * 2, true);
    ctx.fillStyle = fill;
    ctx.fill();
  };
  ring(KNOB_RING.outline, KNOB_RING.inner - 2, "rgba(8, 10, 14, 0.55)");
  ring(KNOB_RING.outer, KNOB_RING.inner, "#ffffff");
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

export class Handles {
  private readonly knob: THREE.Sprite;
  private readonly raycaster = new THREE.Raycaster();
  private readonly pointer = new THREE.Vector2();
  private readonly plane = new THREE.Plane();
  private readonly hit = new THREE.Vector3();
  private readonly pos: Position = { az: 0, el: 0, dist: 0 };
  private drag: DragState | null = null;

  constructor(private readonly stage: Stage, private readonly deps: HandleDeps) {
    this.knob = new THREE.Sprite(
      new THREE.SpriteMaterial({ map: knobTexture(), transparent: true, depthWrite: false, sizeAttenuation: false, toneMapped: false }),
    );
    this.knob.renderOrder = DOT_RENDER_ORDER + 2;
    this.knob.userData = { kind: "radius" };
    stage.scene.add(this.knob);
    const el = stage.renderer.domElement;
    el.addEventListener("pointerdown", this.onDown);
    el.addEventListener("pointermove", this.onMove);
    window.addEventListener("pointerup", this.onUp);
  }

  /** 每帧把距离圆环放到选中音轨的轨道左侧（固定声源则放在声源外侧），并保持固定像素大小。 */
  update(): void {
    this.knob.scale.setScalar(KNOB_PX * this.stage.frameInfo().pixelScale);
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
    const data = hit.object.userData as { kind?: string; track?: TrackName };
    const selected = this.deps.selected();
    if (data.track && data.track !== selected) {
      this.deps.select(data.track);
      return;
    }
    let kind: DragKind | null = null;
    if (data.kind === "radius") kind = "radius";
    else if (data.kind === "source" && !this.deps.playing() && phaseDraggable(this.deps.params(selected))) kind = "phase";
    if (!kind) return;
    this.drag = { kind, track: selected };
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
    const target = phaseForLocalAngle(p, orbitLocalAngle(p, this.hit.x, this.hit.y, this.hit.z));
    const advance = p.shape === "fixed" ? 0 : this.deps.phase(track);
    this.deps.change(track, { start_deg: Math.round(wrap180(target - advance)) }); // 色点在左右声道正中，偏移为 0
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
