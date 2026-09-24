// 三层半球：只画层与层的分界线（-15° / 0° / 15° / 60°）+ 极淡的色带 + 三个层名。
// 选中音轨所在的层会微微亮起。按选中音轨的可视半径缩放，使其轨道贴在球面上。

import * as THREE from "three";
import { T } from "../i18n";
import { LAYERS, type LayerName } from "./layers";
import { directionToWorld } from "./mapping";

const SEGMENTS = 128;
const BAND_OPACITY = { idle: 0.015, active: 0.05 }; // 半透明球面前后两层会叠加，所以取得很淡
const LINE_OPACITY = { boundary: 0.22, equator: 0.34 };
const BOUNDARIES = [-15, 0, 15, 60];
const LABEL_AZ = 63; // 层名放在听者右前方（默认视角正对这一侧）
const LABEL_SCALE = 0.075;
const LABEL_OPACITY = { idle: 0.45, active: 0.95 };

function textSprite(text: string, color: string): THREE.Sprite {
  const canvas = document.createElement("canvas");
  canvas.width = 256;
  canvas.height = 64;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("2D canvas is not available");
  ctx.font = `600 30px ${T.fontStack}`;
  ctx.fillStyle = color;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, 128, 32);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, transparent: true, depthWrite: false }));
  sprite.scale.set(LABEL_SCALE * 4, LABEL_SCALE, 1);
  return sprite;
}

const hex = (c: number) => `#${c.toString(16).padStart(6, "0")}`;

function circleAt(el: number, material: THREE.LineBasicMaterial): THREE.LineLoop {
  const pts: THREE.Vector3[] = [];
  for (let i = 0; i < SEGMENTS; i++) {
    const v = directionToWorld((360 * i) / SEGMENTS, el);
    pts.push(new THREE.Vector3(v.x, v.y, v.z));
  }
  return new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts), material);
}

export class Dome {
  readonly group = new THREE.Group();
  private readonly bands = new Map<LayerName, THREE.MeshBasicMaterial>();
  private readonly labels = new Map<LayerName, THREE.SpriteMaterial>();

  constructor() {
    this.group.name = "layer-dome";
    for (const layer of LAYERS) {
      // SphereGeometry 的 theta 从 +y（头顶）量起：theta = 90° - 仰角
      const material = new THREE.MeshBasicMaterial({
        color: layer.color,
        transparent: true,
        opacity: BAND_OPACITY.idle,
        side: THREE.DoubleSide,
        depthWrite: false,
      });
      const band = new THREE.Mesh(
        new THREE.SphereGeometry(1, SEGMENTS, 12, 0, Math.PI * 2, THREE.MathUtils.degToRad(90 - layer.to), THREE.MathUtils.degToRad(layer.to - layer.from)),
        material,
      );
      band.renderOrder = -1;
      this.bands.set(layer.name, material);
      const mid = directionToWorld(LABEL_AZ, (layer.from + layer.to) / 2);
      const label = textSprite(T.layer[layer.name], hex(layer.color));
      label.position.set(mid.x, mid.y, mid.z).multiplyScalar(1.07);
      this.labels.set(layer.name, label.material);
      this.group.add(band, label);
    }
    for (const el of BOUNDARIES) {
      const color = (LAYERS.find((l) => el >= l.from && el <= l.to) ?? LAYERS[0]).color;
      const opacity = el === 0 ? LINE_OPACITY.equator : LINE_OPACITY.boundary;
      this.group.add(circleAt(el, new THREE.LineBasicMaterial({ color, transparent: true, opacity, depthWrite: false })));
    }
    this.setActiveLayer(null);
  }

  setRadius(visualRadius: number): void {
    this.group.scale.setScalar(visualRadius);
  }

  /** 选中音轨所在的层微微提亮，其余保持很淡。 */
  setActiveLayer(active: LayerName | null): void {
    for (const [name, material] of this.bands) material.opacity = name === active ? BAND_OPACITY.active : BAND_OPACITY.idle;
    for (const [name, material] of this.labels) material.opacity = name === active ? LABEL_OPACITY.active : LABEL_OPACITY.idle;
  }

  set visible(v: boolean) {
    this.group.visible = v;
  }

  get visible(): boolean {
    return this.group.visible;
  }
}
