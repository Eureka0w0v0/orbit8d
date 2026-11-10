// 导入一首歌时给用户看的进度与出错说明（纯函数）：后端的状态 + 阶段进度 → 标题与总进度条；异常 → 当前语言的提示。

import { ApiError } from "../api";
import { T, errorText } from "../i18n";
import type { Project, ProjectState } from "../types";

/** 各阶段在总进度条上占的区间（分轨最久）。 */
const STAGE_SPAN: Partial<Record<ProjectState, [number, number]>> = {
  UPLOADED: [0, 0.02],
  DECODING: [0.02, 0.06],
  SEPARATING: [0.06, 0.88],
  ANALYZING: [0.88, 1],
};

/** 还在处理中的状态：要等它走到 READY 或 FAILED。 */
export const BUSY: ReadonlySet<ProjectState> = new Set(["UPLOADED", "DECODING", "SEPARATING", "ANALYZING"]);

/** 分轨前第一次下载模型（后端项目记录的 step）：单独显示下载进度。 */
export const MODEL_DOWNLOAD_STEP = "model_download";

export function overallProgress(p: Pick<Project, "state" | "progress">): number {
  const [lo, hi] = STAGE_SPAN[p.state] ?? [1, 1];
  return lo + (hi - lo) * p.progress;
}

/** 处理中的歌该显示的标题与进度（0–1）。 */
export function progressOf(p: Pick<Project, "state" | "progress" | "step">): { title: string; fraction: number } {
  if (p.step === MODEL_DOWNLOAD_STEP) return { title: T.ui.modelDownload, fraction: p.progress };
  return { title: T.projectState[p.state], fraction: overallProgress(p) };
}

/** 给用户看的错误：按错误码用当前语言说明，后面带上错误码方便排查。 */
export function describe(err: unknown): string {
  if (err instanceof ApiError) return `${errorText(err.code, err.message)} (${err.code})`;
  if (err instanceof Error) return err.message;
  return String(err);
}
