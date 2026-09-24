import { describe, expect, it } from "vitest";
import { History } from "./history";

describe("撤销 / 重做", () => {
  it("一步一步撤销、再重做回来", () => {
    const h = new History<string>(100, 500);
    h.record("A", 0); // A → B
    h.record("B", 1000); // B → C
    expect(h.undo("C")).toBe("B");
    expect(h.undo("B")).toBe("A");
    expect(h.undo("A")).toBeNull();
    expect(h.redo("A")).toBe("B");
    expect(h.redo("B")).toBe("C");
    expect(h.redo("C")).toBeNull();
  });

  it("连续拖动（间隔 < 500 ms）合并成一步", () => {
    const h = new History<number>(100, 500);
    h.record(0, 0);
    h.record(1, 100);
    h.record(2, 450);
    h.record(3, 900); // 与上一次相隔 450 ms，仍算同一次拖动
    expect(h.undo(4)).toBe(0);
    expect(h.canUndo).toBe(false);
  });

  it("新的修改清空重做记录；撤销后的下一次修改单独成一步", () => {
    const h = new History<string>(100, 500);
    h.record("A", 0);
    h.record("B", 1000);
    expect(h.undo("C")).toBe("B");
    h.record("B", 1100); // 撤销后马上改：B → D
    expect(h.canRedo).toBe(false);
    expect(h.undo("D")).toBe("B");
    expect(h.undo("B")).toBe("A");
  });

  it("最多记 limit 步，最早的丢掉；clear 清空", () => {
    const h = new History<number>(3, 0);
    for (let i = 0; i < 5; i++) h.record(i, i);
    expect([h.undo(5), h.undo(4), h.undo(3), h.undo(2)]).toEqual([4, 3, 2, null]);
    h.record(9, 100);
    h.clear();
    expect(h.canUndo || h.canRedo).toBe(false);
  });
});
