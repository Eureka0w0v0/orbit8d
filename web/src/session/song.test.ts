import { describe, expect, it } from "vitest";
import { barGrid } from "../scene/edit";
import type { Analysis, Project } from "../types";
import { openSong, titleOf } from "./song";

const analysis = { bpm_norm: 96, t_ref: 0.4, duration_s: 183.5, default_bars: 4 } as Analysis;
const project = (extra: Partial<Project> = {}): Project => ({ id: "0123456789abcdef", state: "READY", analysis, ...extra }) as Project;

describe("打开着的歌", () => {
  it("节拍信息与小节线都来自分析结果", () => {
    const song = openSong(project());
    expect(song.analysis).toBe(analysis);
    expect(song.timing).toEqual({ bpmNorm: 96, tRef: 0.4, durationS: 183.5 });
    expect(song.grid).toEqual(barGrid(96, 0.4));
    expect(song.grid.bar).toBeCloseTo(2.5, 12);
  });

  it("还没有分析结果的项目打不开", () => {
    expect(() => openSong(project({ analysis: null }))).toThrow("The project has no analysis");
  });

  it("歌名：歌手 - 标题；没有歌手只有标题；没有标题用文件名", () => {
    expect(titleOf({ title: "Demo Song", artist: "Somebody", filename: "demo.mp3" })).toBe("Somebody - Demo Song");
    expect(titleOf({ title: "Demo Song", artist: "", filename: "demo.mp3" })).toBe("Demo Song");
    expect(titleOf({ title: "", artist: "Somebody", filename: "demo.mp3" })).toBe("demo.mp3");
  });
});
