// 界面阶段的显式状态机（SPEC §10.1）：只允许表里的跳转，其余一律视为程序错误。

export type Phase = "booting" | "empty" | "uploading" | "processing" | "loading" | "ready" | "error";

export const PHASE_TRANSITIONS: Record<Phase, readonly Phase[]> = {
  booting: ["empty", "processing", "loading", "error"],
  empty: ["uploading", "loading"], // loading：从最近的歌里打开一首
  uploading: ["processing", "loading", "error"],
  processing: ["loading", "error"],
  loading: ["ready", "error"],
  ready: ["uploading", "loading", "empty"], // loading：切歌；empty：删掉了正在用的歌
  error: ["uploading", "empty", "loading"],
};

export function canGo(from: Phase, to: Phase): boolean {
  return PHASE_TRANSITIONS[from].includes(to);
}
