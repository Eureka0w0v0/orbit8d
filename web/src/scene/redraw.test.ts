import { describe, expect, it } from "vitest";
import { needsRedraw } from "./redraw";

describe("3D 按需重画", () => {
  it("什么都没变就不画", () => {
    expect(needsRedraw({ dirty: false, cameraMoved: false, animating: false })).toBe(false);
  });

  it("场景变了、视角在动（含惯性）、播放或拖播放头时都要画", () => {
    expect(needsRedraw({ dirty: true, cameraMoved: false, animating: false })).toBe(true);
    expect(needsRedraw({ dirty: false, cameraMoved: true, animating: false })).toBe(true);
    expect(needsRedraw({ dirty: false, cameraMoved: false, animating: true })).toBe(true);
  });
});
