// 三层半球网格：半透明色带（环绕 / 高度 / 顶层）+ 纬线 + 每 30° 一条经线 + 层名与角度标注。
// 画在单位半径上，按选中音轨的可视半径整体缩放，让该音轨的轨道正好贴在球面上。

import * as THREE from "three";
import { LAYERS } from "./layers";
import { directionToWorld } from "./mapping";

const SEGMENTS = 96;
const BAND_OPACITY = 0.07;
const LINE_OPACITY = 0.32;
const MERIDIAN_STEP_DEG = 30;
const LATITUDES = [-15, 0, 15, 30, 45, 60, 75];
const LABEL_AZ = 45; // 标注放在听者右前方的经线上（默认视角正对这一侧）
const LABEL_SCALE = 0.11;

function textSprite(text: string, color: string): THREE.Sprite {
  const canvas = document.createElement("canvas");
  canvas.width = 256;
  canvas.height = 64;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("无法创建 2D 画布");
  ctx.font = "600 30px -apple-system, 'PingFang SC', sans-serif";
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

function colorAt(el: number): number {
  return (LAYERS.find((l) => el >= l.from && el <= l.to) ?? LAYERS[0]).color;
}

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

  constructor() {
    this.group.name = "layer-dome";
    for (const layer of LAYERS) {
      // SphereGeometry 的 theta 从 +y（头顶）量起：theta = 90° - 仰角
      const thetaStart = THREE.MathUtils.degToRad(90 - layer.to);
      const thetaLength = THREE.MathUtils.degToRad(layer.to - layer.from);
      const band = new THREE.Mesh(
        new THREE.SphereGeometry(1, SEGMENTS, 12, 0, Math.PI * 2, thetaStart, thetaLength),
        new THREE.MeshBasicMaterial({ color: layer.color, transparent: true, opacity: BAND_OPACITY, side: THREE.DoubleSide, depthWrite: false }),
      );
      band.renderOrder = -1;
      this.group.add(band);
      const mid = directionToWorld(LABEL_AZ + 18, (layer.from + layer.to) / 2);
      const label = textSprite(layer.label, hex(layer.color));
      label.position.set(mid.x, mid.y, mid.z).multiplyScalar(1.08);
      this.group.add(label);
    }
    for (const el of LATITUDES) {
      const material = new THREE.LineBasicMaterial({ color: colorAt(el), transparent: true, opacity: el === 0 ? LINE_OPACITY * 1.6 : LINE_OPACITY, depthWrite: false });
      this.group.add(circleAt(el, material));
      const tick = directionToWorld(LABEL_AZ, el);
      const label = textSprite(`${el}°`, "#9aa3b5");
      label.scale.multiplyScalar(0.7);
      label.position.set(tick.x, tick.y, tick.z).multiplyScalar(1.06);
      this.group.add(label);
    }
    const meridianMaterial = new THREE.LineBasicMaterial({ color: 0x8c96ad, transparent: true, opacity: LINE_OPACITY * 0.6, depthWrite: false });
    for (let az = 0; az < 360; az += MERIDIAN_STEP_DEG) {
      const pts: THREE.Vector3[] = [];
      for (let el = LAYERS[0].from; el <= 90; el += 3) {
        const v = directionToWorld(az, el);
        pts.push(new THREE.Vector3(v.x, v.y, v.z));
      }
      this.group.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), meridianMaterial));
    }
  }

  setRadius(visualRadius: number): void {
    this.group.scale.setScalar(visualRadius);
  }

  set visible(v: boolean) {
    this.group.visible = v;
  }

  get visible(): boolean {
    return this.group.visible;
  }
}
