// 混音规则：哪些音轨现在听得到、各自多大声。面板、3D 视图、试听渲染都按这一条算。

import type { Scene, TrackName } from "../types";
import { TRACKS } from "../types";

/** 用户音量 × 静音/独奏：只要有音轨独奏，其余音轨全部静音。 */
export function effectiveTrackGains(scene: Scene): Record<TrackName, number> {
  const anySolo = TRACKS.some((n) => scene.mix[n].solo);
  const out = {} as Record<TrackName, number>;
  for (const name of TRACKS) {
    const m = scene.mix[name];
    const audible = !m.mute && (m.solo || !anySolo);
    out[name] = audible ? 10 ** (m.gain_db / 20) : 0;
  }
  return out;
}
