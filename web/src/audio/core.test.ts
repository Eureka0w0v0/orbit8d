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

describe("BinauralCore 与 Python 渲染逐样本一致（三段 + 过渡 + 停顿 + 飞过头顶 + 分段混响量）", () => {
  const table = parseHrtf(toArrayBuffer(readFileSync(golden("render_grid.bin"))));
  const c = JSON.parse(readFileSync(golden("render_case.json"), "utf8")) as RenderCase;
  const data = new Float32Array(toArrayBuffer(readFileSync(golden("render_case.f32"))));
  const n = c.samples;
  const inputs = c.channels.map((_, ch) => data.subarray(ch * n, (ch + 1) * n));
  const base = c.channels.length * n;
  const refL = data.subarray(base, base + n);
  const refR = data.subarray(base + n, base + 2 * n);
  const refSend = data.subarray(base + 2 * n, base + 3 * n);

  it("声道顺序与后端一致，用例是多段场景", () => {
    expect(c.channels).toEqual([...INPUT_CHANNELS]);
    expect(c.scene.sections.length).toBe(3);
    expect(new Set(c.scene.events.map((e) => e.kind))).toEqual(new Set(["hold", "overhead"]));
  });

  it("干声与混响送出误差 < -90 dB", () => {
    const core = new BinauralCore(table, c.sample_rate);
    const timing = { bpmNorm: c.bpm_norm, tRef: c.t_ref, durationS: n / c.sample_rate };
    core.setParams(buildRenderParams(c.scene, timing, c.calibration));
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

describe("试听改参数不爆音（音量、静音逐块过渡；取消静音从干净状态开始）", () => {
  const table = parseHrtf(toArrayBuffer(readFileSync(golden("render_grid.bin"))));
  const c = JSON.parse(readFileSync(golden("render_case.json"), "utf8")) as RenderCase;
  const n = 16384;
  const timing = { bpmNorm: c.bpm_norm, tRef: c.t_ref, durationS: n / c.sample_rate };
  // 每 32 个采样正好一个周期：稳态时每块的电平完全相同，块与块之间的电平变化只来自参数过渡
  const sine = Float32Array.from({ length: n }, (_, i) => 0.1 * Math.sin((2 * Math.PI * i) / 32));
  const inputs = INPUT_CHANNELS.map(() => sine);

  /** 只有人声出声、各轨固定在正后方（背后压暗滤波器起作用）的单段场景：除了参数，什么都不随时间变。
   * gainDb = null 表示人声也静音。 */
  function params(gainDb: number | null) {
    const scene = structuredClone(c.scene);
    scene.events = [];
    scene.sections = [scene.sections[0]];
    for (const o of Object.values(scene.sections[0].orbits)) Object.assign(o, { shape: "fixed", start_deg: 180, height_deg: 0 });
    for (const [name, m] of Object.entries(scene.mix)) {
      m.mute = name !== "vocals" || gainDb === null;
      m.gain_db = gainDb ?? 0;
    }
    return buildRenderParams(scene, timing, c.calibration);
  }

  function render(core: BinauralCore, out: Float32Array, from: number, to: number): void {
    const scratchR = new Float32Array(QUANTUM);
    const scratchSend = new Float32Array(QUANTUM);
    for (let start = from; start < to; start += QUANTUM) {
      scratchR.fill(0);
      scratchSend.fill(0);
      core.process(inputs.map((ch) => ch.subarray(start, start + QUANTUM)), start / c.sample_rate, out.subarray(start, start + QUANTUM), scratchR, scratchSend, QUANTUM);
    }
  }

  const blockRms = (x: Float32Array, block: number) => Math.sqrt(x.subarray(block * 32, block * 32 + 32).reduce((s, v) => s + v * v, 0) / 32);

  it("改音量：20 ms 内逐块过渡（相邻块电平变化很小），过渡完与一开始就是新音量的结果一致", () => {
    const change = 4096;
    const ramped = new Float32Array(n);
    const core = new BinauralCore(table, c.sample_rate);
    core.setParams(params(0));
    render(core, ramped, 0, change);
    core.setParams(params(-12));
    render(core, ramped, change, n);
    const reference = new Float32Array(n);
    const ref = new BinauralCore(table, c.sample_rate);
    ref.setParams(params(-12));
    render(ref, reference, 0, n);
    let worst = 1;
    for (let b = change / 32 - 1; b < change / 32 + 40; b++) worst = Math.min(worst, blockRms(ramped, b + 1) / blockRms(ramped, b));
    expect(worst).toBeGreaterThan(0.85); // 硬切的话这一块会掉到约 0.25
    const settled = change + 2048;
    expect(Math.max(...ramped.subarray(settled).map((v, i) => Math.abs(v - reference[settled + i])))).toBeLessThan(1e-7);
  });

  it("静音：淡出后把卷积尾巴放完，之后严格为 0", () => {
    const mute = 4096;
    const out = new Float32Array(n);
    const core = new BinauralCore(table, c.sample_rate);
    core.setParams(params(0));
    render(core, out, 0, mute);
    core.setParams(params(null));
    render(core, out, mute, n);
    const before = blockRms(out, mute / 32 - 1);
    let biggestDrop = 0;
    for (let b = mute / 32 - 1; b < mute / 32 + 40; b++) biggestDrop = Math.max(biggestDrop, blockRms(out, b) - blockRms(out, b + 1));
    expect(biggestDrop).toBeLessThan(before / 10); // 过渡约 28 块，每块只降约 1/28
    expect(out.subarray(mute + 2048).every((v) => v === 0)).toBe(true);
  });

  it("静音一阵后再取消静音：与从干净状态开始渲染完全相同（不冒出旧的尾巴）", () => {
    const [mute, unmute] = [2048, 8192];
    const out = new Float32Array(n);
    const core = new BinauralCore(table, c.sample_rate);
    core.setParams(params(0));
    render(core, out, 0, mute);
    core.setParams(params(null));
    render(core, out, mute, unmute);
    core.setParams(params(0));
    render(core, out, unmute, n);
    const fresh = new Float32Array(n);
    const clean = new BinauralCore(table, c.sample_rate);
    clean.setParams(params(null));
    clean.setParams(params(0)); // 同样从 0 开始过渡
    render(clean, fresh, unmute, n);
    expect(Math.max(...out.subarray(unmute).map((v, i) => Math.abs(v - fresh[unmute + i])))).toBe(0);
  });
});
