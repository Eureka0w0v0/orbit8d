import { describe, expect, it } from "vitest";
import { QUANTUM } from "./core";
import { transportAt } from "./transport";

describe("播放 / 停止按帧号生效", () => {
  it("新的播放从 startFrame 所在的量子开始，之前不动", () => {
    const pending = { startFrame: 1000, songOffset: 12 };
    expect(transportAt(1000 - QUANTUM, pending, null).start).toBe(false);
    expect(transportAt(1000 - QUANTUM + 1, pending, null).start).toBe(true);
    expect(transportAt(0, null, null)).toEqual({ start: false, stop: false });
  });

  it("停止在 stopFrame 之后的第一个量子生效：之前照常渲染淡出", () => {
    expect(transportAt(4999, null, 5000).stop).toBe(false);
    expect(transportAt(5000, null, 5000).stop).toBe(true);
  });

  it("跳转：先淡出停止（15 ms），新的播放（60 ms 后）再开始", () => {
    const now = 44100;
    const stopFrame = now + 662;
    const pending = { startFrame: now + 2646, songOffset: 30 };
    const events: string[] = [];
    for (let frame = now; frame < now + 4096; frame += QUANTUM) {
      const { start, stop } = transportAt(frame, events.includes("start") ? null : pending, events.includes("stop") ? null : stopFrame);
      if (stop) events.push("stop");
      if (start) events.push("start");
    }
    expect(events).toEqual(["stop", "start"]);
  });
});
