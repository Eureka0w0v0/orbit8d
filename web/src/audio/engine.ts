// 试听音频引擎：9 声道缓冲 → AudioWorklet（双耳 + 已乘分段混响量的送出信号）→ BRIR 卷积（混响）
// → 按场景的补偿 EQ（两个卷积器交叉淡化切换，换 EQ 不爆音）→ 试听总增益 → 兜底限幅。
// 原曲（A/B 对比）与 8D 同时播放、采样级同步，只是其中一路音量为 0；切换时 40 ms 交叉淡化。

import workletUrl from "./worklet.ts?worker&url";
import { parseHrtf } from "./hrtf";
import { INPUT_CHANNELS, type RenderParams } from "./params";
import type { WorkletMessage } from "./worklet";

export const SAMPLE_RATE = 44100;
const PROCESSOR_NAME = "orbit8d-binaural";
const START_LATENCY_S = 0.06;
const PARAM_RAMP_S = 0.05;
const EQ_FADE_S = 0.08;
const AB_FADE_S = 0.04;

export type ListenMode = "8d" | "original";
const LIMITER = { threshold: -1, knee: 0, ratio: 20, attack: 0.001, release: 0.1 };
const METER_FFT = 2048;
const METER_FLOOR_DB = -90;

/** 试听文件名（后端 PREVIEW_NAMES）→ 在 9 声道缓冲中的起始声道。 */
const STEM_LAYOUT: ReadonlyArray<{ name: string; channel: number; channels: 1 | 2 }> = [
  { name: "vocals_hi", channel: 0, channels: 1 },
  { name: "bass_hi", channel: 1, channels: 1 },
  { name: "drums_hi", channel: 2, channels: 2 },
  { name: "other_hi", channel: 4, channels: 2 },
  { name: "bass_sub", channel: 6, channels: 1 },
  { name: "drums_sub", channel: 7, channels: 1 },
  { name: "other_sub", channel: 8, channels: 1 },
];

export const STEM_NAMES = STEM_LAYOUT.map((s) => s.name);

export class AudioEngine {
  readonly ctx: AudioContext;
  private node: AudioWorkletNode | null = null;
  private readonly wet: ConvolverNode;
  private readonly preEq: GainNode;
  private readonly eqs: [ConvolverNode, ConvolverNode];
  private readonly eqGains: [GainNode, GainNode];
  private activeEq = 0;
  private eqChain: Promise<void> = Promise.resolve();
  private hasEq = false;
  private readonly master: GainNode;
  private readonly path8d: GainNode;
  private readonly pathOriginal: GainNode;
  private original: AudioBuffer | null = null;
  private originalSource: AudioBufferSourceNode | null = null;
  private originalLevel = 1;
  private listenMode: ListenMode = "8d";
  private readonly meters: [AnalyserNode, AnalyserNode];
  private readonly meterBuf = new Float32Array(METER_FFT);
  private buffer: AudioBuffer | null = null;
  private source: AudioBufferSourceNode | null = null;
  private songOffset = 0;
  private startCtxTime = 0;
  playing = false;
  onEnded: (() => void) | null = null;

  constructor() {
    this.ctx = new AudioContext({ sampleRate: SAMPLE_RATE, latencyHint: "interactive" });
    this.wet = this.ctx.createConvolver();
    this.wet.normalize = false; // 必须在设置 buffer 之前
    this.preEq = this.ctx.createGain();
    this.eqs = [this.ctx.createConvolver(), this.ctx.createConvolver()];
    this.eqGains = [this.ctx.createGain(), this.ctx.createGain()];
    this.master = this.ctx.createGain();
    this.path8d = this.ctx.createGain();
    this.pathOriginal = this.ctx.createGain();
    this.pathOriginal.gain.value = 0;
    const limiter = this.ctx.createDynamicsCompressor();
    limiter.threshold.value = LIMITER.threshold;
    limiter.knee.value = LIMITER.knee;
    limiter.ratio.value = LIMITER.ratio;
    limiter.attack.value = LIMITER.attack;
    limiter.release.value = LIMITER.release;
    this.wet.connect(this.preEq);
    this.eqs.forEach((eq, i) => {
      eq.normalize = false;
      this.eqGains[i].gain.value = i === this.activeEq ? 1 : 0;
      this.preEq.connect(eq).connect(this.eqGains[i]).connect(this.path8d);
    });
    this.path8d.connect(this.master).connect(limiter).connect(this.ctx.destination);
    this.pathOriginal.connect(limiter);
    const split = this.ctx.createChannelSplitter(2);
    this.meters = [this.ctx.createAnalyser(), this.ctx.createAnalyser()];
    limiter.connect(split);
    this.meters.forEach((m, ch) => {
      m.fftSize = METER_FFT;
      split.connect(m, ch);
    });
  }

  async init(hrtf: ArrayBuffer): Promise<void> {
    await this.ctx.audioWorklet.addModule(workletUrl);
    this.node = new AudioWorkletNode(this.ctx, PROCESSOR_NAME, {
      numberOfInputs: 1,
      numberOfOutputs: 2,
      outputChannelCount: [2, 1],
      channelCount: INPUT_CHANNELS.length,
      channelCountMode: "explicit",
      channelInterpretation: "discrete",
    });
    this.node.connect(this.preEq, 0);
    this.node.connect(this.wet, 1);
    this.post({ type: "init", table: parseHrtf(hrtf) });
  }

  /** 换补偿 EQ：新 EQ 装进闲着的卷积器，再交叉淡化过去。多次调用按顺序排队，不会打断正在进行的淡化。 */
  setEq(eqWav: ArrayBuffer): Promise<void> {
    const job = this.eqChain.then(() => this.swapEq(eqWav));
    this.eqChain = job.catch(() => undefined); // 一次失败不影响后续切换；错误仍由调用方处理
    return job;
  }

  private async swapEq(eqWav: ArrayBuffer): Promise<void> {
    const fir = await this.ctx.decodeAudioData(eqWav);
    const stereo = this.ctx.createBuffer(2, fir.length, SAMPLE_RATE);
    stereo.copyToChannel(fir.getChannelData(0), 0);
    stereo.copyToChannel(fir.getChannelData(0), 1);
    if (!this.hasEq) {
      this.eqs[this.activeEq].buffer = stereo; // 第一次：直接装上
      this.hasEq = true;
      return;
    }
    const next = 1 - this.activeEq;
    this.eqs[next].buffer = stereo;
    const now = this.ctx.currentTime;
    for (const [i, target] of [[next, 1], [this.activeEq, 0]] as const) {
      const g = this.eqGains[i].gain;
      g.cancelScheduledValues(now);
      g.setValueAtTime(g.value, now);
      g.linearRampToValueAtTime(target, now + EQ_FADE_S);
    }
    this.activeEq = next;
    await new Promise((r) => window.setTimeout(r, EQ_FADE_S * 1000 + 20));
  }

  private post(msg: WorkletMessage): void {
    if (!this.node) throw new Error("Audio engine is not initialised");
    this.node.port.postMessage(msg);
  }

  /** 解码 7 个试听文件并打包成一个 9 声道缓冲（保证各声源采样级同步）。 */
  async loadStems(files: Record<string, ArrayBuffer>): Promise<void> {
    this.stop();
    this.original = null;
    const decoded = await Promise.all(STEM_LAYOUT.map((s) => this.ctx.decodeAudioData(files[s.name])));
    const length = Math.min(...decoded.map((b) => b.length));
    const packed = this.ctx.createBuffer(INPUT_CHANNELS.length, length, SAMPLE_RATE);
    STEM_LAYOUT.forEach((s, i) => {
      for (let c = 0; c < s.channels; c++) {
        packed.copyToChannel(decoded[i].getChannelData(c).subarray(0, length), s.channel + c);
      }
    });
    this.buffer = packed;
    this.songOffset = 0;
  }

  /** 原曲（立体声）；level = 响度对齐增益 × 试听文件缩放。 */
  async loadOriginal(wav: ArrayBuffer, level: number): Promise<void> {
    this.original = await this.ctx.decodeAudioData(wav);
    this.originalLevel = level;
    this.applyListen(0);
  }

  get listening(): ListenMode {
    return this.listenMode;
  }

  /** 在 8D 与原曲之间切换（两路一直同步在播，只交叉淡化音量）。 */
  setListen(mode: ListenMode): void {
    if (mode === "original" && !this.original) return;
    this.listenMode = mode;
    this.applyListen(AB_FADE_S);
  }

  private applyListen(fade: number): void {
    const now = this.ctx.currentTime;
    const targets: Array<[GainNode, number]> = [
      [this.path8d, this.listenMode === "8d" ? 1 : 0],
      [this.pathOriginal, this.listenMode === "original" ? this.originalLevel : 0],
    ];
    for (const [node, value] of targets) {
      node.gain.cancelScheduledValues(now);
      node.gain.setValueAtTime(node.gain.value, now);
      node.gain.linearRampToValueAtTime(value, now + Math.max(fade, 1 / SAMPLE_RATE));
    }
  }

  async setRoom(brirWav: ArrayBuffer): Promise<void> {
    this.wet.buffer = await this.ctx.decodeAudioData(brirWav);
  }

  setParams(params: RenderParams, previewGain: number): void {
    this.post({ type: "params", params });
    this.master.gain.setTargetAtTime(previewGain, this.ctx.currentTime, PARAM_RAMP_S);
  }

  /** 最终输出左右声道的 RMS 电平（dB），用于电平表。 */
  levels(): [number, number] {
    return this.meters.map((m) => {
      m.getFloatTimeDomainData(this.meterBuf);
      let sum = 0;
      for (const v of this.meterBuf) sum += v * v;
      const rms = Math.sqrt(sum / this.meterBuf.length);
      return rms > 0 ? Math.max(METER_FLOOR_DB, 20 * Math.log10(rms)) : METER_FLOOR_DB;
    }) as [number, number];
  }

  get duration(): number {
    return this.buffer?.duration ?? 0;
  }

  /** 当前歌曲时间（秒）。 */
  get time(): number {
    if (!this.playing) return this.songOffset;
    return Math.min(this.duration, this.songOffset + Math.max(0, this.ctx.currentTime - this.startCtxTime));
  }

  async play(): Promise<void> {
    if (!this.buffer || !this.node || this.playing) return;
    await this.ctx.resume();
    const src = this.ctx.createBufferSource();
    src.buffer = this.buffer;
    src.connect(this.node);
    const when = this.ctx.currentTime + START_LATENCY_S;
    const offset = this.songOffset >= this.duration ? 0 : this.songOffset;
    this.post({ type: "transport", playing: true, startFrame: Math.round(when * SAMPLE_RATE), songOffset: offset });
    src.start(when, offset);
    if (this.original) {
      const orig = this.ctx.createBufferSource(); // 同一时刻、同一位置开始：与 8D 采样级同步
      orig.buffer = this.original;
      orig.connect(this.pathOriginal);
      orig.start(when, offset);
      this.originalSource = orig;
    }
    src.onended = () => {
      if (this.source !== src) return;
      this.playing = false;
      this.songOffset = this.duration;
      this.source = null;
      this.originalSource?.disconnect(); // 原曲与 8D 等长，同时播完
      this.originalSource = null;
      this.onEnded?.();
    };
    this.source = src;
    this.songOffset = offset;
    this.startCtxTime = when;
    this.playing = true;
  }

  pause(): void {
    if (!this.playing) return;
    const t = this.time;
    this.stop();
    this.songOffset = t;
  }

  async seek(t: number): Promise<void> {
    const wasPlaying = this.playing;
    this.stop();
    this.songOffset = Math.min(Math.max(0, t), this.duration);
    if (wasPlaying) await this.play();
  }

  private stop(): void {
    if (this.originalSource) {
      const orig = this.originalSource;
      this.originalSource = null;
      orig.stop();
      orig.disconnect();
    }
    if (this.source) {
      const src = this.source;
      this.source = null;
      src.onended = null;
      src.stop();
      src.disconnect();
    }
    if (this.playing) this.post({ type: "transport", playing: false, startFrame: 0, songOffset: this.songOffset });
    this.playing = false;
  }
}
