// 实时双耳渲染核心：与 backend/orbit8d/engine/render.py + pipeline.py 算法逐块一致（docs/SPEC.md §5.2）。
// 纯计算类，不依赖任何浏览器 API：AudioWorklet 调用它，单元测试也直接调用它。

import type { Position } from "../orbit/orbit";
import { trackPosition, wetDbAt } from "../orbit/timeline";
import { interpolateHrir, type HrtfTable } from "./hrtf";
import type { RenderParams, SourceParams } from "./params";

export const BLOCK = 32;
export const QUANTUM = 128;
const REAR_SHELF_HZ = 3000;
const MIN_DISTANCE_M = 0.5;
const MAX_DISTANCE_M = 4;
const REF_DISTANCE_M = 1;
const SUB_CHANNEL0 = 6;
const DEG = Math.PI / 180;

/** 每个运动声源的滤波与重叠相加状态。 */
class SourceState {
  hpX1 = 0;
  hpY1 = 0;
  readonly accL: Float64Array;
  readonly accR: Float64Array;
  constructor(taps: number) {
    this.accL = new Float64Array(BLOCK + taps - 1);
    this.accR = new Float64Array(BLOCK + taps - 1);
  }
  reset(): void {
    this.hpX1 = 0;
    this.hpY1 = 0;
    this.accL.fill(0);
    this.accR.fill(0);
  }
}

export class BinauralCore {
  private readonly table: HrtfTable;
  private readonly sampleRate: number;
  private readonly taps: number;
  private readonly hpB0: number;
  private readonly hpA1: number;
  private readonly frontL: Float64Array;
  private readonly frontR: Float64Array;
  private readonly hL: Float64Array;
  private readonly hR: Float64Array;
  private readonly shaped = new Float64Array(BLOCK);
  private readonly states: SourceState[] = [];
  private readonly sub: SourceState;
  private readonly pos: Position = { az: 0, el: 0, dist: 0 };
  private params: RenderParams | null = null;

  constructor(table: HrtfTable, sampleRate: number) {
    if (table.sampleRate !== sampleRate) throw new Error(`HRTF sample rate ${table.sampleRate} != audio ${sampleRate}`);
    this.table = table;
    this.sampleRate = sampleRate;
    this.taps = table.taps;
    const k = Math.tan((Math.PI * REAR_SHELF_HZ) / sampleRate); // 一阶 Butterworth 高通（双线性变换）
    this.hpB0 = 1 / (1 + k);
    this.hpA1 = (k - 1) / (k + 1);
    this.frontL = new Float64Array(this.taps);
    this.frontR = new Float64Array(this.taps);
    interpolateHrir(table, 0, 0, this.frontL, this.frontR);
    this.hL = new Float64Array(this.taps);
    this.hR = new Float64Array(this.taps);
    this.sub = new SourceState(this.taps);
  }

  setParams(params: RenderParams): void {
    while (this.states.length < params.sources.length) this.states.push(new SourceState(this.taps));
    this.params = params;
  }

  /** 跳转播放位置时清空滤波器与尾巴，避免把旧位置的声音带过来。 */
  reset(): void {
    for (const s of this.states) s.reset();
    this.sub.reset();
  }

  /**
   * 处理 frames 个采样（BLOCK 的整数倍），结果累加进 outL / outR / outSend。
   * songTime 是第一个采样对应的歌曲时间（秒）。
   */
  process(
    inputs: ArrayLike<number>[],
    songTime: number,
    outL: Float32Array,
    outR: Float32Array,
    outSend: Float32Array,
    frames: number,
  ): void {
    const p = this.params;
    if (!p) return;
    for (let start = 0; start < frames; start += BLOCK) {
      const tCenter = songTime + (start + BLOCK / 2) / this.sampleRate;
      const wet = 10 ** (wetDbAt(p.wet, tCenter) / 20); // 分段混响量：本块恒定
      for (let i = 0; i < p.sources.length; i++) {
        const src = p.sources[i];
        if (src.gain > 0) this.renderSource(src, this.states[i], inputs[src.channel], start, tCenter, p, outL, outR);
        if (src.send > 0) {
          const x = inputs[src.channel];
          const level = src.send * wet;
          for (let n = 0; n < BLOCK; n++) outSend[start + n] += level * x[start + n];
        }
      }
      this.renderSub(inputs, start, p.subGains, outL, outR);
    }
  }

  private renderSource(
    src: SourceParams,
    st: SourceState,
    x: ArrayLike<number>,
    start: number,
    tCenter: number,
    p: RenderParams,
    outL: Float32Array,
    outR: Float32Array,
  ): void {
    const pos = trackPosition(p.motions[src.track], tCenter, src.offsetDeg, this.pos);
    const dist = Math.min(MAX_DISTANCE_M, Math.max(MIN_DISTANCE_M, pos.dist));
    const gain = src.gain * (REF_DISTANCE_M / dist);
    const rear = Math.max(0, -Math.cos(pos.el * DEG) * Math.cos(pos.az * DEG));
    const cut = p.rearCut * rear;
    interpolateHrir(this.table, pos.az, pos.el, this.hL, this.hR);
    const b0 = this.hpB0;
    const a1 = this.hpA1;
    for (let n = 0; n < BLOCK; n++) {
      const xn = x[start + n];
      const hp = b0 * xn - b0 * st.hpX1 - a1 * st.hpY1; // 状态始终连续推进，与整段 lfilter 等价
      st.hpX1 = xn;
      st.hpY1 = hp;
      this.shaped[n] = (xn - cut * hp) * gain;
    }
    this.convolveBlock(this.shaped, this.hL, this.hR, st, start, outL, outR);
  }

  private renderSub(
    inputs: ArrayLike<number>[],
    start: number,
    gains: [number, number, number],
    outL: Float32Array,
    outR: Float32Array,
  ): void {
    for (let n = 0; n < BLOCK; n++) {
      this.shaped[n] =
        gains[0] * inputs[SUB_CHANNEL0][start + n] +
        gains[1] * inputs[SUB_CHANNEL0 + 1][start + n] +
        gains[2] * inputs[SUB_CHANNEL0 + 2][start + n];
    }
    this.convolveBlock(this.shaped, this.frontL, this.frontR, this.sub, start, outL, outR);
  }

  /** 本块输入与本块 HRIR 做完整线性卷积；前 BLOCK 个输出写出，其余作为尾巴留给后续块。 */
  private convolveBlock(
    x: Float64Array,
    hL: Float64Array,
    hR: Float64Array,
    st: SourceState,
    start: number,
    outL: Float32Array,
    outR: Float32Array,
  ): void {
    const taps = this.taps;
    const accL = st.accL;
    const accR = st.accR;
    for (let n = 0; n < BLOCK; n++) {
      const xn = x[n];
      if (xn === 0) continue;
      for (let k = 0; k < taps; k++) {
        accL[n + k] += xn * hL[k];
        accR[n + k] += xn * hR[k];
      }
    }
    for (let n = 0; n < BLOCK; n++) {
      outL[start + n] += accL[n];
      outR[start + n] += accR[n];
    }
    accL.copyWithin(0, BLOCK);
    accR.copyWithin(0, BLOCK);
    accL.fill(0, taps - 1);
    accR.fill(0, taps - 1);
  }
}
