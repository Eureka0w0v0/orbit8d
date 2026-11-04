import { describe, expect, it } from "vitest";
import { PHASE_TRANSITIONS, canGo, type Phase } from "./phase";

describe("界面阶段状态机", () => {
  it("切歌、删掉正在用的歌、从导入页打开最近的歌", () => {
    expect(canGo("ready", "loading")).toBe(true);
    expect(canGo("ready", "empty")).toBe(true);
    expect(canGo("empty", "loading")).toBe(true);
    expect(canGo("error", "loading")).toBe(true);
  });

  it("处理中、加载中不能被打断去别的歌", () => {
    expect(canGo("processing", "empty")).toBe(false);
    expect(canGo("processing", "uploading")).toBe(false);
    expect(canGo("loading", "loading")).toBe(false);
    expect(canGo("uploading", "empty")).toBe(false);
  });

  it("每个阶段都能从启动走到，也都有出路", () => {
    const seen = new Set<Phase>(["booting"]);
    const queue: Phase[] = ["booting"];
    while (queue.length) {
      for (const next of PHASE_TRANSITIONS[queue.shift()!]) {
        if (!seen.has(next)) {
          seen.add(next);
          queue.push(next);
        }
      }
    }
    expect([...seen].sort()).toEqual(Object.keys(PHASE_TRANSITIONS).sort());
    for (const phase of Object.keys(PHASE_TRANSITIONS) as Phase[]) expect(PHASE_TRANSITIONS[phase].length).toBeGreaterThan(0);
  });
});
