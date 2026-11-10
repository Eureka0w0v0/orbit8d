// 打开着的一首歌：项目记录、它的分析结果，以及由分析结果得出的节拍信息与小节线。
// 这几样要么都有、要么都没有，而且打开期间不变，所以合成一个只读的整体，由 openSong 一次建好。

import type { Timing } from "../audio/params";
import { barGrid, type BarGrid } from "../scene/edit";
import type { Analysis, Project, SourceInfo } from "../types";

export interface OpenSong {
  readonly project: Project;
  readonly analysis: Analysis;
  /** 编译时间轴用的节拍信息。 */
  readonly timing: Timing;
  /** 小节线：切分、拖分界、放事件时吸附用。 */
  readonly grid: BarGrid;
}

export function openSong(project: Project): OpenSong {
  const analysis = project.analysis;
  if (!analysis) throw new Error("The project has no analysis");
  return {
    project,
    analysis,
    timing: { bpmNorm: analysis.bpm_norm, tRef: analysis.t_ref, durationS: analysis.duration_s },
    grid: barGrid(analysis.bpm_norm, analysis.t_ref),
  };
}

/** 顶栏显示的歌名：有标题用“歌手 - 标题”（没有歌手就只有标题），没有标题用文件名。 */
export function titleOf(source: Pick<SourceInfo, "title" | "artist" | "filename">): string {
  if (!source.title) return source.filename;
  return `${source.artist ? `${source.artist} - ` : ""}${source.title}`;
}
