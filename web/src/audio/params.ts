// 场景 → 渲染参数。与 backend/orbit8d/engine/pipeline.py 的 render_tracks / send_signal 逐项对应。
// 轨道运动（含分段过渡、停顿、飞过头顶）与分段混响量都预先编译成纯数据，整包 postMessage 给 AudioWorklet。

import { effectiveTrackGains } from "../model/mix";
import { compileTrack, compileWet, type TrackMotion, type WetCurve } from "../orbit/timeline";
import type { CalibrationKey, Scene, TrackName } from "../types";
import { TRACKS } from "../types";

/** AudioWorklet 处理器的注册名：主线程建节点、Worklet 里注册，两边都从这里取（不能互相 import：加载 worklet 模块就会注册）。 */
export const PROCESSOR_NAME = "orbit8d-binaural";

/** 试听缓冲区的 9 个声道（顺序即 AudioBuffer 的声道顺序，与后端 SOURCE_CHANNELS 一致）。 */
export const INPUT_CHANNELS = [
  "vocals_hi",
  "bass_hi",
  "drums_hi_L",
  "drums_hi_R",
  "other_hi_L",
  "other_hi_R",
  "bass_sub",
  "drums_sub",
  "other_sub",
] as const;

/** 三路超低频（bass_sub、drums_sub、other_sub）在 9 声道里连着放，这是第一路的位置。 */
export const SUB_CHANNEL0 = INPUT_CHANNELS.indexOf("bass_sub");

/** 试听文件（后端 PREVIEW_FILES）→ 在 9 声道缓冲中的起始声道与声道数。 */
export const STEM_LAYOUT: ReadonlyArray<{ name: string; channel: number; channels: 1 | 2 }> = [
  { name: "vocals_hi", channel: 0, channels: 1 },
  { name: "bass_hi", channel: 1, channels: 1 },
  { name: "drums_hi", channel: 2, channels: 2 },
  { name: "other_hi", channel: 4, channels: 2 },
  { name: "bass_sub", channel: 6, channels: 1 },
  { name: "drums_sub", channel: 7, channels: 1 },
  { name: "other_sub", channel: 8, channels: 1 },
];

export const STEM_NAMES = STEM_LAYOUT.map((s) => s.name);

export interface SourceParams {
  track: TrackName;
  channel: number;
  offsetDeg: number;
  gain: number; // 线性：用户音量 × 静音/独奏 × 校准 × 输入缩放（不含距离）
  send: number; // 混响送出（线性，不含距离、不含分段混响量）
}

export interface RenderParams {
  sources: SourceParams[];
  motions: Record<TrackName, TrackMotion>;
  wet: WetCurve; // 分段混响量（dB，随时间平滑过渡），逐块乘在送出信号上
  subGains: [number, number, number]; // bass_sub / drums_sub / other_sub
  rearCut: number;
}

/** 歌曲的节拍信息：编译时间轴要用。 */
export interface Timing {
  bpmNorm: number;
  tRef: number;
  durationS: number;
}

const SOURCE_LAYOUT: ReadonlyArray<{ track: TrackName; channel: number; side: -1 | 0 | 1; share: number }> = [
  { track: "vocals", channel: 0, side: 0, share: 1 },
  { track: "bass", channel: 1, side: 0, share: 1 },
  { track: "drums", channel: 2, side: -1, share: 0.5 },
  { track: "drums", channel: 3, side: 1, share: 0.5 },
  { track: "other", channel: 4, side: -1, share: 0.5 },
  { track: "other", channel: 5, side: 1, share: 0.5 },
];

export function compileMotions(scene: Scene, timing: Timing): Record<TrackName, TrackMotion> {
  const out = {} as Record<TrackName, TrackMotion>;
  for (const track of TRACKS) out[track] = compileTrack(scene, track, timing.bpmNorm, timing.tRef, timing.durationS);
  return out;
}

export function buildRenderParams(
  scene: Scene,
  timing: Timing,
  calibration: Record<CalibrationKey, number>,
  inputScale = 1,
): RenderParams {
  const gains = effectiveTrackGains(scene);
  const sources = SOURCE_LAYOUT.map(({ track, channel, side, share }): SourceParams => {
    const m = scene.mix[track];
    const level = gains[track] * calibration[track] * inputScale;
    return { track, channel, offsetDeg: (side * m.width_deg) / 2, gain: level, send: m.reverb_send * level * share };
  });
  const sub = calibration.sub * inputScale;
  return {
    sources,
    motions: compileMotions(scene, timing),
    wet: compileWet(scene, timing.bpmNorm, timing.durationS),
    subGains: [gains.bass * sub, gains.drums * sub, gains.other * sub],
    rearCut: 1 - 10 ** (-scene.rear_darken_db / 20),
  };
}
