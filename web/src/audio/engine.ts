// 试听音频引擎：9 声道缓冲 → AudioWorklet（双耳）+ BRIR 卷积（混响）→ 补偿 EQ → 试听总增益 → 兜底限幅。

import workletUrl from "./worklet.ts?worker&url";
import { parseHrtf } from "./hrtf";
import { INPUT_CHANNELS, type RenderParams } from "./params";
import type { WorkletMessage } from "./worklet";

export const SAMPLE_RATE = 44100;
const PROCESSOR_NAME = "orbit8d-binaural";
const START_LATENCY_S = 0.06;
const PARAM_RAMP_S = 0.05;
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
  private readonly wetGain: GainNode;
  private readonly eq: ConvolverNode;
  private readonly master: GainNode;
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
    this.wetGain = this.ctx.createGain();
    this.eq = this.ctx.createConvolver();
    this.eq.normalize = false;
    this.master = this.ctx.createGain();
    const limiter = this.ctx.createDynamicsCompressor();
    limiter.threshold.value = LIMITER.threshold;
    limiter.knee.value = LIMITER.knee;
    limiter.ratio.value = LIMITER.ratio;
    limiter.attack.value = LIMITER.attack;
    limiter.release.value = LIMITER.release;
    this.wet.connect(this.wetGain).connect(this.eq);
    this.eq.connect(this.master).connect(limiter).connect(this.ctx.destination);
    const split = this.ctx.createChannelSplitter(2);
    this.meters = [this.ctx.createAnalyser(), this.ctx.createAnalyser()];
    limiter.connect(split);
    this.meters.forEach((m, ch) => {
      m.fftSize = METER_FFT;
      split.connect(m, ch);
    });
  }

  async init(hrtf: ArrayBuffer, eqWav: ArrayBuffer): Promise<void> {
    await this.ctx.audioWorklet.addModule(workletUrl);
    this.node = new AudioWorkletNode(this.ctx, PROCESSOR_NAME, {
      numberOfInputs: 1,
      numberOfOutputs: 2,
      outputChannelCount: [2, 1],
      channelCount: INPUT_CHANNELS.length,
      channelCountMode: "explicit",
      channelInterpretation: "discrete",
    });
    this.node.connect(this.eq, 0);
    this.node.connect(this.wet, 1);
    const table = parseHrtf(hrtf);
    this.post({ type: "init", table });
    const fir = await this.ctx.decodeAudioData(eqWav);
    const stereo = this.ctx.createBuffer(2, fir.length, SAMPLE_RATE);
    stereo.copyToChannel(fir.getChannelData(0), 0);
    stereo.copyToChannel(fir.getChannelData(0), 1);
    this.eq.buffer = stereo;
  }

  private post(msg: WorkletMessage): void {
    if (!this.node) throw new Error("音频引擎还没初始化");
    this.node.port.postMessage(msg);
  }

  /** 解码 7 个试听文件并打包成一个 9 声道缓冲（保证各声源采样级同步）。 */
  async loadStems(files: Record<string, ArrayBuffer>): Promise<void> {
    this.stop();
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

  async setRoom(brirWav: ArrayBuffer): Promise<void> {
    this.wet.buffer = await this.ctx.decodeAudioData(brirWav);
  }

  setParams(params: RenderParams, wetDb: number, previewGain: number): void {
    this.post({ type: "params", params });
    const now = this.ctx.currentTime;
    this.wetGain.gain.setTargetAtTime(10 ** (wetDb / 20), now, PARAM_RAMP_S);
    this.master.gain.setTargetAtTime(previewGain, now, PARAM_RAMP_S);
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
    src.onended = () => {
      if (this.source !== src) return;
      this.playing = false;
      this.songOffset = this.duration;
      this.source = null;
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
