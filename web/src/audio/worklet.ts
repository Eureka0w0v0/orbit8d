// AudioWorklet：把 9 声道试听缓冲区实时渲染成双耳立体声（输出 0）和混响送出信号（输出 1）。
// 只在 AudioWorkletGlobalScope 里运行；以下三项是该作用域的全局量，只在本模块内声明，避免污染主线程类型。
// 播放 / 停止都按帧号生效：停止前主线程先把输入淡出，这里照常渲染到 stopFrame，输出跟着淡出、不硬切；
// 新一次播放在 startFrame 所在的渲染量子才清空状态，不会打断上一次的淡出。

import { BinauralCore, QUANTUM } from "./core";
import type { HrtfTable } from "./hrtf";
import { transportAt, type Start } from "./transport";
import type { RenderParams } from "./params";

declare const sampleRate: number;
declare const currentFrame: number;
declare function registerProcessor(name: string, ctor: unknown): void;
declare class AudioWorkletProcessor {
  readonly port: MessagePort;
}

export const PROCESSOR_NAME = "orbit8d-binaural";
export const INPUT_CHANNEL_COUNT = 9;

export type WorkletMessage =
  | { type: "init"; table: HrtfTable }
  | { type: "params"; params: RenderParams }
  | { type: "start"; startFrame: number; songOffset: number }
  | { type: "stop"; stopFrame: number };

class Orbit8DProcessor extends AudioWorkletProcessor {
  private core: BinauralCore | null = null;
  private pending: RenderParams | null = null;
  private playing = false;
  private startFrame = 0;
  private songOffset = 0;
  private nextStart: Start | null = null;
  private stopFrame: number | null = null;

  constructor() {
    super();
    this.port.onmessage = (event: MessageEvent<WorkletMessage>) => this.onMessage(event.data);
  }

  private onMessage(msg: WorkletMessage): void {
    switch (msg.type) {
      case "init":
        this.core = new BinauralCore(msg.table, sampleRate);
        if (this.pending) this.core.setParams(this.pending);
        break;
      case "params":
        this.pending = msg.params;
        this.core?.setParams(msg.params);
        break;
      case "start":
        this.nextStart = { startFrame: msg.startFrame, songOffset: msg.songOffset };
        break;
      case "stop":
        this.stopFrame = msg.stopFrame;
        if (this.nextStart && this.nextStart.startFrame >= msg.stopFrame) this.nextStart = null; // 还没开始就停了
        break;
    }
  }

  private advanceTransport(): void {
    const { start, stop } = transportAt(currentFrame, this.nextStart, this.stopFrame);
    if (stop) {
      this.playing = false;
      this.stopFrame = null;
    }
    if (start && this.nextStart) {
      ({ startFrame: this.startFrame, songOffset: this.songOffset } = this.nextStart);
      this.nextStart = null;
      this.playing = true;
      this.core?.reset();
    }
  }

  process(inputs: Float32Array[][], outputs: Float32Array[][]): boolean {
    const [outL, outR] = outputs[0];
    const send = outputs[1][0];
    outL.fill(0);
    outR.fill(0);
    send.fill(0);
    this.advanceTransport();
    const input = inputs[0];
    if (!this.core || !this.playing || !input || input.length < INPUT_CHANNEL_COUNT) return true;
    const songTime = this.songOffset + (currentFrame - this.startFrame) / sampleRate;
    this.core.process(input, songTime, outL, outR, send, QUANTUM);
    return true;
  }
}

registerProcessor(PROCESSOR_NAME, Orbit8DProcessor);
