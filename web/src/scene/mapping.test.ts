import { describe, expect, it } from "vitest";
import { positionAtPhase, type OrbitParams } from "../orbit/orbit";
import {
  localToWorld,
  directionToWorld,
  orbitLocalAngle,
  phaseForLocalAngle,
  radiusFromVisual,
  spriteScaleForPixels,
  visualRadius,
  worldToDirection,
} from "./mapping";

const angDiff = (a: number, b: number) => Math.abs(((a - b + 540) % 360) - 180);

function params(over: Partial<OrbitParams>): OrbitParams {
  return {
    shape: "circle", radiusM: 1.2, periodS: 7, direction: 1, startDeg: 0, heightDeg: 0,
    pitchDeg: 0, rollDeg: 0, yawDeg: 0, aspect: 0.6, swingDeg: 120, liftDeg: 30, ...over,
  };
}

describe("可视化映射", () => {
  it("半径映射可逆且单调", () => {
    for (const r of [0.5, 0.8, 1.2, 2, 4]) expect(radiusFromVisual(visualRadius(r))).toBeCloseTo(r, 9);
    expect(visualRadius(0.5)).toBeLessThan(visualRadius(4));
  });

  it("听者右侧（90°）在世界坐标 -x", () => {
    const v = directionToWorld(90, 0);
    expect(v.x).toBeCloseTo(-1, 9);
    expect(directionToWorld(0, 0).z).toBeCloseTo(1, 9);
  });

  it("方向与世界坐标互逆", () => {
    for (let i = 0; i < 50; i++) {
      const az = (i * 37.3) % 360;
      const el = ((i * 13.7) % 170) - 85;
      const v = directionToWorld(az, el);
      const back = worldToDirection(v.x, v.y, v.z);
      expect(angDiff(back.az, az)).toBeLessThan(1e-9);
      expect(back.el).toBeCloseTo(el, 9);
    }
  });

  it.each(["circle", "ellipse"] as const)("%s：由世界方向反推相位", (shape) => {
    for (let i = 0; i < 40; i++) {
      const p = params({ shape, pitchDeg: ((i * 23) % 150) - 75, rollDeg: ((i * 41) % 150) - 75, yawDeg: ((i * 67) % 340) - 170 });
      const phi = (i * 29.5) % 360;
      const pos = positionAtPhase(p, phi);
      const w = directionToWorld(pos.az, pos.el);
      const local = orbitLocalAngle(p, w.x, w.y, w.z);
      expect(angDiff(phaseForLocalAngle(p, local), phi)).toBeLessThan(1e-6);
    }
  });
});

describe("局部 → 世界", () => {
  it("与 positionAtPhase 的方向一致（高度为 0 时）", () => {
    for (let i = 0; i < 30; i++) {
      const p = params({ pitchDeg: ((i * 31) % 160) - 80, rollDeg: ((i * 17) % 160) - 80, yawDeg: ((i * 53) % 340) - 170 });
      const phi = (i * 47) % 360;
      const pos = positionAtPhase(p, phi);
      const a = directionToWorld(pos.az, pos.el);
      const rad = (phi * Math.PI) / 180;
      const b = localToWorld(p, Math.sin(rad), 0, Math.cos(rad));
      expect(Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z)).toBeLessThan(1e-9);
    }
  });
});

describe("固定像素大小的精灵", () => {
  it("scale 投影回屏幕正好是要求的像素高", () => {
    const fov = 34;
    const height = 935;
    const scale = spriteScaleForPixels(12, fov, height);
    const projectedPx = (scale / Math.tan(((fov / 2) * Math.PI) / 180)) * (height / 2);
    expect(projectedPx).toBeCloseTo(12, 9);
  });

  it("与像素数成正比、与视口高度成反比，视口为 0 时不除零", () => {
    expect(spriteScaleForPixels(20, 34, 900)).toBeCloseTo(2 * spriteScaleForPixels(10, 34, 900), 12);
    expect(spriteScaleForPixels(10, 34, 450)).toBeCloseTo(2 * spriteScaleForPixels(10, 34, 900), 12);
    expect(Number.isFinite(spriteScaleForPixels(10, 34, 0))).toBe(true);
  });
});
