// 实时双耳渲染核心：与 backend/orbit8d/engine/render.py + pipeline.py 算法逐块一致（docs/SPEC.md §5.2）。
// 纯计算类，不依赖任何浏览器 API：AudioWorklet 调用它，单元测试也直接调用它。
// 试听时改参数（音量、静音、混响送出）不硬切：各声源的增益在 20 ms 内逐块线性过渡；
// 第一次设参数和跳转之后直接到位（导出没有中途改参数，逐块结果仍与 Python 一致）。
// 声源静音后再把卷积尾巴放完（4 块）就清空状态，取消静音时从干净状态开始，不会冒出很久以前的尾巴。

import type { Position } from "../orbit/orbit";
import { trackPosition, wetDbAt } from "../orbit/timeline";
import { interpolateHrir, type HrtfTable } from "./hrtf";
import { SUB_CHANNEL0, type RenderParams, type SourceParams } from "./params";

export const BLOCK = 32;
export const QUANTUM = 128;
const REAR_SHELF_HZ = 3000;
const MIN_DISTANCE_M = 0.5;
const MAX_DISTANCE_M = 4;
const REF_DISTANCE_M = 1;
const DEG = Math.PI / 180;
export const RAMP_S = 0.02; // 参数变化的过渡时间

/** 逐块线性过渡到目标值；blocks = 0 时直接到位。 */
export class Ramp {
  value = 0;
  private target = 0;
  private step = 0;
  private left = 0;

  set(target: number, blocks: number): void {
    this.target = target;
    this.left = blocks > 0 && target !== this.value ? blocks : 0;
    this.step = this.left ? (target - this.value) / this.left : 0;
    if (!this.left) this.value = target;
  }

  /** 跳到目标值（跳转播放位置时用）。 */
  snap(): void {
    this.set(this.target, 0);
  }

  /** 本块用的值。 */
  next(): number {
    if (this.left > 0) {
      this.left--;
      this.value = this.left ? this.value + this.step : this.target;
    }
    return this.value;
  }
}

/** 每个运动声源的滤波与重叠相加状态。 */
class SourceState {
  hpX1 = 0;
  hpY1 = 0;
  readonly accL: Float64Array;
  readonly accR: Float64Array;
  readonly gain = new Ramp();
  readonly send = new Ramp();
  tailBlocks = 0; // 静音后还要放完几块卷积尾巴
  constructor(taps: number) {
    this.accL = new Float64Array(BLOCK + taps - 1);
    this.accR = new Float64Array(BLOCK + taps - 1);
  }
  reset(): void {
    this.hpX1 = 0;
    this.hpY1 = 0;
    this.accL.fill(0);
    this.accR.fill(0);
    this.tailBlocks = 0;
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
  private readonly subGains: [Ramp, Ramp, Ramp] = [new Ramp(), new Ramp(), new Ramp()];
  private readonly rampBlocks: number;
  private readonly tailBlocks: number;
  private readonly zeros = new Float64Array(BLOCK);
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
    this.rampBlocks = Math.max(1, Math.round((RAMP_S * sampleRate) / BLOCK));
    this.tailBlocks = Math.ceil((this.taps - 1) / BLOCK);
  }

  /** 换参数：增益类参数逐块过渡；第一次设参数时直接到位。 */
  setParams(params: RenderParams): void {
    const blocks = this.params ? this.rampBlocks : 0;
    while (this.states.length < params.sources.length) this.states.push(new SourceState(this.taps));
    params.sources.forEach((src, i) => {
      this.states[i].gain.set(src.gain, blocks);
      this.states[i].send.set(src.send, blocks);
    });
    params.subGains.forEach((g, i) => this.subGains[i].set(g, blocks));
    this.params = params;
  }

  /** 跳转播放位置时清空滤波器与尾巴（避免把旧位置的声音带过来），正在过渡的参数直接到位。 */
  reset(): void {
    for (const s of this.states) {
      s.reset();
      s.gain.snap();
      s.send.snap();
    }
    this.sub.reset();
    for (const g of this.subGains) g.snap();
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
        const st = this.states[i];
        const gain = st.gain.next();
        if (gain > 0) {
          st.tailBlocks = this.tailBlocks;
          this.renderSource(src, st, gain, inputs[src.channel], start, tCenter, p, outL, outR);
        } else if (st.tailBlocks > 0) {
          this.flushTail(st, start, outL, outR);
        }
        const send = st.send.next();
        if (send > 0) {
          const x = inputs[src.channel];
          const level = send * wet;
          for (let n = 0; n < BLOCK; n++) outSend[start + n] += level * x[start + n];
        }
      }
      this.renderSub(inputs, start, outL, outR);
    }
  }

  /** 静音的声源：把卷积尾巴放完，然后清空状态。 */
  private flushTail(st: SourceState, start: number, outL: Float32Array, outR: Float32Array): void {
    this.convolveBlock(this.zeros, this.hL, this.hR, st, start, outL, outR);
    st.tailBlocks--;
    if (st.tailBlocks === 0) st.reset();
  }

  private renderSource(
    src: SourceParams,
    st: SourceState,
    level: number,
    x: ArrayLike<number>,
    start: number,
    tCenter: number,
    p: RenderParams,
    outL: Float32Array,
    outR: Float32Array,
  ): void {
    const pos = trackPosition(p.motions[src.track], tCenter, src.offsetDeg, this.pos);
    const dist = Math.min(MAX_DISTANCE_M, Math.max(MIN_DISTANCE_M, pos.dist));
    const gain = level * (REF_DISTANCE_M / dist);
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

  private renderSub(inputs: ArrayLike<number>[], start: number, outL: Float32Array, outR: Float32Array): void {
    const gains = [this.subGains[0].next(), this.subGains[1].next(), this.subGains[2].next()];
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
