// 界面文案入口：按当前语言（src/i18n）导出。逻辑代码里不写死任何显示文本。
// 段落名在存档里是后端给的中文规范名（前奏 / 主歌 / 副歌 / 桥段 / 尾声 / 全曲），显示时用 i18n.sectionName 翻译。

import { T } from "../i18n";

export const TRACK_LABEL = T.track;
export const SHAPE_LABEL = T.shape;
export const ROOM_LABEL = T.room;
export const EVENT_LABEL = T.event;
export const PROJECT_STATE_LABEL = T.projectState;
export const EXPORT_STATE_LABEL = T.exportState;
export const FORMAT_LABEL = T.format;
export const PARAM_LABEL = T.param;
export const TEXT = T.ui;

/** 段落规范名（存档里的值）与时间轴颜色。 */
export const SECTION_LABELS = ["前奏", "主歌", "副歌", "桥段", "尾声"] as const;
export const WHOLE_SONG = "全曲";
export const SECTION_COLORS: Record<string, string> = {
  前奏: "#6f7fb8",
  主歌: "#2fa597",
  副歌: "#d4588d",
  桥段: "#c99a3a",
  尾声: "#6f7fb8",
};
export const DEFAULT_SECTION_COLOR = "#5b6275";
