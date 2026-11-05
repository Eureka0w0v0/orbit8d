// 播放 / 停止按帧号生效（AudioWorklet 用的纯函数，便于测试）：
// 新一次播放在 startFrame 所在的渲染量子开始；停止在 stopFrame 之后的第一个量子生效。

import { QUANTUM } from "./core";

export interface Start {
  startFrame: number;
  songOffset: number;
}

export function transportAt(frame: number, pending: Start | null, stopFrame: number | null): { start: boolean; stop: boolean } {
  return {
    start: pending !== null && frame + QUANTUM > pending.startFrame,
    stop: stopFrame !== null && frame >= stopFrame,
  };
}
