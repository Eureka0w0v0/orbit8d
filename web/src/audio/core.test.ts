import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import type { CalibrationKey, Scene } from "../types";
import { BinauralCore, QUANTUM } from "./core";
import { interpolateHrir, parseHrtf } from "./hrtf";
import { INPUT_CHANNELS, buildRenderParams } from "./params";

const golden = (name: string) => fileURLToPath(new URL(`../../../shared/golden/${name}`, import.meta.url));
const toArrayBuffer = (b: Buffer) => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer;

interface RenderCase {
  sample_rate: number;
  samples: number;
  channels: string[];
  scene: Scene;
  bpm_norm: number;
  t_ref: number;
  calibration: Record<CalibrationKey, number>;
}

function errDb(a: Float32Array, ref: Float32Array): number {
  let e = 0;
  let r = 0;
  for (let i = 0; i < ref.length; i++) {
    e += (a[i] - ref[i]) ** 2;
    r += ref[i] ** 2;
  }
  return 10 * Math.log10(e / r);
}

describe("HRTF 表", () => {
  const table = parseHrtf(toArrayBuffer(readFileSync(golden("render_grid.bin"))));

  it("解析头部与数据", () => {
    expect(table.elNodes.length).toBe(5);
    expect(table.nAz).toBe(12);
    expect(table.data.length).toBe(5 * 12 * 2 * table.taps);
  });

  it("网格点上的插值等于原始数据", () => {
    const l = new Float64Array(table.taps);
    const r = new Float64Array(table.taps);
    interpolateHrir(table, 60, 30, l, r); // el 索引 3、az 索引 2
    const base = (3 * table.nAz + 2) * 2 * table.taps;
    expect(Math.abs(l[5] - table.data[base + 5])).toBeLessThan(1e-7);
    expect(Math.abs(r[5] - table.data[base + table.taps + 5])).toBeLessThan(1e-7);
  });
});

describe("BinauralCore 与 Python 渲染逐样本一致", () => {
  const table = parseHrtf(toArrayBuffer(readFileSync(golden("render_grid.bin"))));
  const c = JSON.parse(readFileSync(golden("render_case.json"), "utf8")) as RenderCase;
  const data = new Float32Array(toArrayBuffer(readFileSync(golden("render_case.f32"))));
  const n = c.samples;
  const inputs = c.channels.map((_, ch) => data.subarray(ch * n, (ch + 1) * n));
  const base = c.channels.length * n;
  const refL = data.subarray(base, base + n);
  const refR = data.subarray(base + n, base + 2 * n);
  const refSend = data.subarray(base + 2 * n, base + 3 * n);

  it("声道顺序与后端一致", () => {
    expect(c.channels).toEqual([...INPUT_CHANNELS]);
  });

  it("干声与混响送出误差 < -90 dB", () => {
    const core = new BinauralCore(table, c.sample_rate);
    core.setParams(buildRenderParams(c.scene, c.bpm_norm, c.t_ref, c.calibration));
    const outL = new Float32Array(n);
    const outR = new Float32Array(n);
    const send = new Float32Array(n);
    for (let start = 0; start < n; start += QUANTUM) {
      const view = inputs.map((ch) => ch.subarray(start, start + QUANTUM));
      core.process(view, start / c.sample_rate, outL.subarray(start), outR.subarray(start), send.subarray(start), QUANTUM);
    }
    expect(errDb(outL, refL)).toBeLessThan(-90);
    expect(errDb(outR, refR)).toBeLessThan(-90);
    expect(errDb(send, refSend)).toBeLessThan(-90);
  });
});
