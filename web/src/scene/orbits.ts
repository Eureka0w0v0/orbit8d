// 每条音轨一个轨道视图：彩色管状轨迹 + 一个表示声音位置的实心小色点（固定像素大小、不发光）。
// 立体声轨（鼓、其他乐器）也只画一个点，放在左右声道正中；选中时在轨道上加亮一段弧，弧长 = 声像宽度。
// 视觉层级：选中音轨的轨迹粗而亮、点大且带白边；其余音轨细而淡、点小。

import * as THREE from "three";
import { LineMaterial } from "three/addons/lines/LineMaterial.js";
import { LineSegments2 } from "three/addons/lines/LineSegments2.js";
import { LineSegmentsGeometry } from "three/addons/lines/LineSegmentsGeometry.js";
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
/** 点的贴图边长（像素）。实心部分约占一半，其余透明区域用来扩大点击范围。 */
export const DOT_PX = { idle: 16, selected: 26 };
const DOT_OPACITY = { idle: 0.85, selected: 1, muted: 0.3 };
const DOT_TEXTURE_PX = 64;
const DOT_RADIUS = { outline: 17, rim: 15, fill: 11 }; // 以 64 像素贴图计：深色描边 / 白边 / 实心
const OUTLINE_COLOR = "rgba(8, 10, 14, 0.55)"; // 压在白色人头上也看得清
export const DOT_RENDER_ORDER = 10; // 点永远画在轨迹上面；选中的点再高一级，几条音轨重叠时它在最上面
const ARC_SEGMENTS = 12;
const ARC_WIDTH_PX = 7; // 比选中轨迹（约 4–5 像素）粗，像套在轨迹上的一段亮套管
const ARC_TINT = 0.55; // 弧的颜色往白色混 55%，和轨迹区分开
const ARC_OPACITY = 1; // 不透明：相邻线段的圆头重叠处不会叠出深浅不一的“接缝”

/** 每帧由舞台提供：1 像素对应的精灵缩放、画布的 CSS 像素尺寸。 */
export interface FrameInfo {
  pixelScale: number;
  width: number;
  height: number;
}

export interface OrbitViewState {
  params: OrbitParams;
  widthDeg: number;
  stereo: boolean;
  audible: boolean;
  selected: boolean;
}

const hex = (c: number) => `#${c.toString(16).padStart(6, "0")}`;

export function worldPoint(pos: Position, out = new THREE.Vector3()): THREE.Vector3 {
  const v: Vec3 = directionToWorld(pos.az, pos.el);
  return out.set(v.x, v.y, v.z).multiplyScalar(visualRadius(pos.dist));
}

function dotTexture(color: number, selected: boolean): THREE.CanvasTexture {
  const canvas = document.createElement("canvas");
  canvas.width = DOT_TEXTURE_PX;
  canvas.height = DOT_TEXTURE_PX;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("2D canvas is not available");
  const c = DOT_TEXTURE_PX / 2;
  const disc = (r: number, fill: string) => {
    ctx.beginPath();
    ctx.arc(c, c, r, 0, Math.PI * 2);
    ctx.fillStyle = fill;
    ctx.fill();
  };
  disc(DOT_RADIUS.outline, OUTLINE_COLOR);
  if (selected) disc(DOT_RADIUS.rim, "#ffffff");
  disc(selected ? DOT_RADIUS.fill : DOT_RADIUS.rim, hex(color));
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

export class OrbitView {
  readonly group = new THREE.Group();
  readonly dot: THREE.Sprite;
  private readonly textures: { idle: THREE.CanvasTexture; selected: THREE.CanvasTexture };
  private readonly dotMaterial: THREE.SpriteMaterial;
  private readonly pathMaterial: THREE.MeshBasicMaterial;
  private readonly arc: LineSegments2;
  private readonly arcMaterial: LineMaterial;
  private readonly arcPositions = new Float32Array(ARC_SEGMENTS * 6);
  private path: THREE.Mesh | null = null;
  private pathKey = "";
  private state: OrbitViewState | null = null;
  private dotPx = DOT_PX.idle;
  private readonly pos: Position = { az: 0, el: 0, dist: 0 };
  private readonly a = new THREE.Vector3();
  private readonly b = new THREE.Vector3();

  constructor(readonly track: TrackName) {
    const color = TRACK_COLORS[track];
    this.group.name = `orbit-${track}`;
    this.pathMaterial = new THREE.MeshBasicMaterial({ color, transparent: true, depthWrite: false });

    this.textures = { idle: dotTexture(color, false), selected: dotTexture(color, true) };
    this.dotMaterial = new THREE.SpriteMaterial({
      map: this.textures.idle,
      transparent: true,
      depthWrite: false,
      sizeAttenuation: false, // 远近不变大小
      toneMapped: false, // 保持音轨原色
    });
    this.dot = new THREE.Sprite(this.dotMaterial);
    this.dot.renderOrder = DOT_RENDER_ORDER;
    this.dot.userData = { track, kind: "source" };

    const tint = new THREE.Color(color).lerp(new THREE.Color(0xffffff), ARC_TINT);
    this.arcMaterial = new LineMaterial({
      color: tint.getHex(),
      linewidth: ARC_WIDTH_PX,
      transparent: true,
      opacity: ARC_OPACITY,
      depthWrite: false,
      toneMapped: false,
    });
    const arcGeometry = new LineSegmentsGeometry();
    arcGeometry.setPositions(this.arcPositions); // 直接引用这块数组，之后原地改写
    this.arc = new LineSegments2(arcGeometry, this.arcMaterial);
    this.arc.frustumCulled = false; // 位置每帧都变，包围球不准
    this.arc.renderOrder = DOT_RENDER_ORDER - 1;
    this.arc.visible = false;
    this.arc.raycast = () => undefined; // 弧不参与点选
    this.group.add(this.dot, this.arc);
    this.group.visible = false; // 还没有场景时不画：否则色点按默认缩放会铺满整个屏幕
  }

  update(state: OrbitViewState): void {
    this.state = state;
    this.group.visible = true;
    const key = JSON.stringify({ ...state.params, startDeg: 0, periodS: 0, direction: 0, sel: state.selected });
    if (key !== this.pathKey) {
      this.pathKey = key;
      this.rebuildPath(state);
    }
    this.dotMaterial.map = state.selected ? this.textures.selected : this.textures.idle;
    this.dotMaterial.opacity = !state.audible ? DOT_OPACITY.muted : state.selected ? DOT_OPACITY.selected : DOT_OPACITY.idle;
    this.dotPx = state.selected ? DOT_PX.selected : DOT_PX.idle;
    this.dot.renderOrder = state.selected ? DOT_RENDER_ORDER + 1 : DOT_RENDER_ORDER;
    this.pathMaterial.opacity = !state.audible ? PATH_OPACITY.muted : state.selected ? PATH_OPACITY.selected : PATH_OPACITY.idle;
    this.arc.visible = state.selected && state.stereo && state.widthDeg > 0;
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

  /** 按歌曲时间摆放色点（与音频渲染用同一条时间轴），选中的立体声轨同时更新声像宽度弧。 */
  setPositions(motion: TrackMotion, songTime: number, frame: FrameInfo): void {
    if (!this.state) return;
    worldPoint(trackPosition(motion, songTime, 0, this.pos), this.dot.position);
    this.dot.scale.setScalar(this.dotPx * frame.pixelScale);
    if (this.arc.visible) this.updateArc(motion, songTime, frame);
  }

  private updateArc(motion: TrackMotion, songTime: number, frame: FrameInfo): void {
    const width = this.state!.widthDeg;
    for (let i = 0; i <= ARC_SEGMENTS; i++) {
      worldPoint(trackPosition(motion, songTime, -width / 2 + (width * i) / ARC_SEGMENTS, this.pos), this.b);
      if (i > 0) {
        const p = this.arcPositions;
        const o = (i - 1) * 6;
        p[o] = this.a.x;
        p[o + 1] = this.a.y;
        p[o + 2] = this.a.z;
        p[o + 3] = this.b.x;
        p[o + 4] = this.b.y;
        p[o + 5] = this.b.z;
      }
      this.a.copy(this.b);
    }
    (this.arc.geometry.getAttribute("instanceStart") as THREE.InterleavedBufferAttribute).data.needsUpdate = true;
    this.arcMaterial.resolution.set(frame.width, frame.height);
  }
}
