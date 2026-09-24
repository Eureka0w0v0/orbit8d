import { describe, expect, it } from "vitest";
import { positionAtPhase, type OrbitParams } from "./orbit";
import { DIRECTIONS, matchDirection, orientationFromTilt, tiltFromOrientation } from "./orientation";

const angDiff = (a: number, b: number) => Math.abs(((a - b + 540) % 360) - 180);

function circle(o: { pitch_deg: number; roll_deg: number; yaw_deg: number }): OrbitParams {
  return {
    shape: "circle", radiusM: 1.2, periodS: 7, direction: 1, startDeg: 0, heightDeg: 0,
    pitchDeg: o.pitch_deg, rollDeg: o.roll_deg, yawDeg: o.yaw_deg, aspect: 0.6, swingDeg: 120, liftDeg: 30,
  };
}

describe("倾斜盘 ↔ 三个倾斜参数", () => {
  it("往返换算不变（倾斜 0 时方向无意义，不比较）", () => {
    for (let i = 0; i < 60; i++) {
      const tilt = { azDeg: ((i * 47) % 360) - 180, tiltDeg: (i * 13) % 90 };
      const back = tiltFromOrientation(orientationFromTilt(tilt));
      expect(back.tiltDeg).toBeCloseTo(tilt.tiltDeg, 6);
      if (tilt.tiltDeg > 0.5) expect(angDiff(back.azDeg, tilt.azDeg)).toBeLessThan(1e-6);
    }
  });

  it("前后倾斜 90° = 左耳→头顶→右耳的竖环；左右倾斜 90° = 正前→头顶→脑后的竖环", () => {
    const ears = circle({ pitch_deg: 90, roll_deg: 0, yaw_deg: 0 });
    expect(positionAtPhase(ears, 0).el).toBeCloseTo(90, 6); // 相位 0（原来的正前）被抬到头顶
    expect(angDiff(positionAtPhase(ears, 90).az, 90)).toBeLessThan(1e-6); // 右耳位置不动
    const frontBack = circle({ pitch_deg: 0, roll_deg: 90, yaw_deg: 0 });
    expect(positionAtPhase(frontBack, 90).el).toBeCloseTo(90, 6); // 原来的右侧被抬到头顶
    expect(angDiff(positionAtPhase(frontBack, 0).az, 0)).toBeLessThan(1e-6); // 正前不动
    expect(matchDirection({ pitch_deg: 90, roll_deg: 0, yaw_deg: 0 })).toBe("ears");
    expect(matchDirection({ pitch_deg: 0, roll_deg: 90, yaw_deg: 0 })).toBe("frontBack");
  });

  it("换算出的轨道，最高点正好在倾斜盘指定的方向和高度", () => {
    for (let i = 0; i < 24; i++) {
      const tilt = { azDeg: (i * 31) % 360, tiltDeg: 5 + ((i * 11) % 80) };
      const p = circle(orientationFromTilt(tilt));
      let best = { az: 0, el: -90 };
      for (let phi = 0; phi < 360; phi += 0.25) {
        const pos = positionAtPhase(p, phi);
        if (pos.el > best.el) best = { az: pos.az, el: pos.el };
      }
      expect(best.el).toBeCloseTo(tilt.tiltDeg, 1);
      expect(angDiff(best.az, tilt.azDeg)).toBeLessThan(1);
    }
  });

  it("方向快捷键都能被识别回来，水平转向保持在 ±180° 内", () => {
    for (const d of DIRECTIONS) {
      const o = orientationFromTilt(d.tilt);
      expect(Math.abs(o.yaw_deg)).toBeLessThanOrEqual(180);
      expect(matchDirection(o)).toBe(d.key);
    }
    expect(matchDirection({ pitch_deg: 20, roll_deg: 0, yaw_deg: 10 })).toBeNull();
  });
});
