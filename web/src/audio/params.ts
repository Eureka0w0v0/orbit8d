// 场景 → 渲染参数。与 backend/orbit8d/engine/pipeline.py 的 render_tracks / send_signal 逐项对应。

import { toOrbitParams, type OrbitParams } from "../orbit/orbit";
import type { CalibrationKey, Scene, TrackName } from "../types";
import { TRACKS } from "../types";

/** 试听缓冲区的 9 个声道（顺序即 AudioBuffer 的声道顺序）。 */
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

export const SUB_TRACKS = ["bass", "drums", "other"] as const;

export interface SourceParams {
  track: TrackName;
  channel: number;
  orbit: OrbitParams;
  offsetDeg: number;
  gain: number; // 线性：用户音量 × 静音/独奏 × 校准 × 输入缩放（不含距离）
  send: number; // 混响送出（线性，不含距离）
}

export interface RenderParams {
  sources: SourceParams[];
  subGains: [number, number, number]; // bass_sub / drums_sub / other_sub
  rearCut: number;
  tRef: number;
}

const SOURCE_LAYOUT: ReadonlyArray<{ track: TrackName; channel: number; side: -1 | 0 | 1; share: number }> = [
  { track: "vocals", channel: 0, side: 0, share: 1 },
  { track: "bass", channel: 1, side: 0, share: 1 },
  { track: "drums", channel: 2, side: -1, share: 0.5 },
  { track: "drums", channel: 3, side: 1, share: 0.5 },
  { track: "other", channel: 4, side: -1, share: 0.5 },
  { track: "other", channel: 5, side: 1, share: 0.5 },
];

/** 用户音量 × 静音/独奏：只要有音轨独奏，其余音轨全部静音。 */
export function effectiveTrackGains(scene: Scene): Record<TrackName, number> {
  const anySolo = TRACKS.some((n) => scene.tracks[n].solo);
  const out = {} as Record<TrackName, number>;
  for (const name of TRACKS) {
    const t = scene.tracks[name];
    const audible = !t.mute && (t.solo || !anySolo);
    out[name] = audible ? 10 ** (t.gain_db / 20) : 0;
  }
  return out;
}

export function buildRenderParams(
  scene: Scene,
  bpmNorm: number,
  tRef: number,
  calibration: Record<CalibrationKey, number>,
  inputScale = 1,
): RenderParams {
  const gains = effectiveTrackGains(scene);
  const sources = SOURCE_LAYOUT.map(({ track, channel, side, share }): SourceParams => {
    const t = scene.tracks[track];
    const level = gains[track] * calibration[track] * inputScale;
    return {
      track,
      channel,
      orbit: toOrbitParams(t.orbit, bpmNorm),
      offsetDeg: (side * t.width_deg) / 2,
      gain: level,
      send: t.reverb_send * level * share,
    };
  });
  const sub = calibration.sub * inputScale;
  return {
    sources,
    subGains: [gains.bass * sub, gains.drums * sub, gains.other * sub],
    rearCut: 1 - 10 ** (-scene.rear_darken_db / 20),
    tRef,
  };
}
