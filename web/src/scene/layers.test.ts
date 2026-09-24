import { describe, expect, it } from "vitest";
import { LAYERS, layerOf } from "./layers";

describe("三层半球的分层", () => {
  it("按高度归层：环绕层 [-15, 15]、高度层 (15, 60]、顶层 (60, 90]", () => {
    expect(layerOf(0)).toBe("surround");
    expect(layerOf(-15)).toBe("surround");
    expect(layerOf(15)).toBe("surround");
    expect(layerOf(15.5)).toBe("height");
    expect(layerOf(60)).toBe("height");
    expect(layerOf(75)).toBe("top");
    expect(layerOf(90)).toBe("top");
    expect(layerOf(-30)).toBeNull();
  });

  it("每层的一键高度落在本层内，且层与层首尾相接", () => {
    for (const layer of LAYERS) expect(layerOf(layer.center)).toBe(layer.name);
    for (let i = 1; i < LAYERS.length; i++) expect(LAYERS[i].from).toBe(LAYERS[i - 1].to);
    expect(LAYERS[LAYERS.length - 1].to).toBe(90);
  });
});
