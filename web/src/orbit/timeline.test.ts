import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import type { Scene, TrackName } from "../types";
import { TRACKS } from "../types";
import { compileTrack, compileWet, trackPosition, wetDbAt } from "./timeline";

interface TrackCase {
  offset_deg: number;
  az: number[];
  el: number[];
  dist: number[];
}

interface TimelineCase {
  scene: Scene;
  bpm_norm: number;
  t_ref: number;
  duration_s: number;
  t: number[];
  tracks: Record<TrackName, TrackCase>;
  wet_db: number[];
}

const file = fileURLToPath(new URL("../../../shared/golden/timeline_vectors.json", import.meta.url));
const cases = (JSON.parse(readFileSync(file, "utf8")) as { cases: TimelineCase[] }).cases;
const DEG = Math.PI / 180;
const unit = (az: number, el: number) => [Math.cos(el * DEG) * Math.sin(az * DEG), Math.sin(el * DEG), Math.cos(el * DEG) * Math.cos(az * DEG)];

describe("时间轴运动与 Python 黄金向量逐点一致", () => {
  it("用例覆盖多段、停顿、飞过头顶", () => {
    expect(cases.length).toBeGreaterThanOrEqual(10);
    const kinds = new Set(cases.flatMap((c) => c.scene.events.map((e) => e.kind)));
    expect(kinds).toEqual(new Set(["hold", "overhead"]));
    expect(Math.max(...cases.map((c) => c.scene.sections.length))).toBeGreaterThanOrEqual(4);
  });

  it("方向（单位向量）与距离误差 < 1e-9，混响量误差 < 1e-9", () => {
    let worstDir = 0;
    let worstDist = 0;
    let worstWet = 0;
    for (const c of cases) {
      for (const track of TRACKS) {
        const ref = c.tracks[track];
        const m = compileTrack(c.scene, track, c.bpm_norm, c.t_ref, c.duration_s);
        c.t.forEach((t, i) => {
          const p = trackPosition(m, t, ref.offset_deg);
          const got = unit(p.az, p.el);
          const want = unit(ref.az[i], ref.el[i]);
          worstDir = Math.max(worstDir, ...got.map((v, k) => Math.abs(v - want[k])));
          worstDist = Math.max(worstDist, Math.abs(p.dist - ref.dist[i]));
        });
      }
      const wet = compileWet(c.scene, c.bpm_norm, c.duration_s);
      c.t.forEach((t, i) => (worstWet = Math.max(worstWet, Math.abs(wetDbAt(wet, t) - c.wet_db[i]))));
    }
    expect(worstDir).toBeLessThan(1e-9);
    expect(worstDist).toBeLessThan(1e-9);
    expect(worstWet).toBeLessThan(1e-9);
  });
});
