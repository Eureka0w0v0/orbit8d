import { describe, expect, it } from "vitest";
import golden from "../../../shared/golden/orbit_vectors.json";
import { normalizeBpm, orbitPosition, periodSeconds, type OrbitParams } from "./orbit";

interface GoldenCase {
  params: {
    shape: OrbitParams["shape"];
    radius_m: number;
    period_s: number;
    direction: number;
    start_deg: number;
    height_deg: number;
    pitch_deg: number;
    roll_deg: number;
    yaw_deg: number;
    aspect: number;
    swing_deg: number;
    lift_deg: number;
  };
  t: number[];
  t_ref: number;
  offset_deg: number;
  az: number[];
  el: number[];
  dist: number[];
}

function fromGolden(p: GoldenCase["params"]): OrbitParams {
  return {
    shape: p.shape,
    radiusM: p.radius_m,
    periodS: p.period_s,
    direction: p.direction === 1 ? 1 : -1,
    startDeg: p.start_deg,
    heightDeg: p.height_deg,
    pitchDeg: p.pitch_deg,
    rollDeg: p.roll_deg,
    yawDeg: p.yaw_deg,
    aspect: p.aspect,
    swingDeg: p.swing_deg,
    liftDeg: p.lift_deg,
  };
}

const angDiff = (a: number, b: number) => Math.abs(((a - b + 540) % 360) - 180);

describe("orbitPosition 与 Python 黄金向量逐点一致", () => {
  const cases = (golden as { cases: GoldenCase[] }).cases;

  it("覆盖全部形状", () => {
    expect(new Set(cases.map((c) => c.params.shape)).size).toBe(6);
  });

  it("误差 < 1e-6", () => {
    let worst = 0;
    for (const c of cases) {
      const p = fromGolden(c.params);
      c.t.forEach((t, i) => {
        const pos = orbitPosition(p, t, c.t_ref, c.offset_deg);
        worst = Math.max(worst, angDiff(pos.az, c.az[i]), Math.abs(pos.el - c.el[i]), Math.abs(pos.dist - c.dist[i]));
      });
    }
    expect(worst).toBeLessThan(1e-6);
  });
});

describe("速度换算", () => {
  it("BPM 归一到 [70, 140)", () => {
    expect(normalizeBpm(180)).toBeCloseTo(90);
    expect(normalizeBpm(67)).toBeCloseTo(134);
    expect(normalizeBpm(140)).toBeCloseTo(70);
  });
  it("小节与秒", () => {
    expect(periodSeconds("bars", 2, 10, 90)).toBeCloseTo(16 / 3);
    expect(periodSeconds("seconds", 2, 10, 90)).toBe(10);
  });
});
