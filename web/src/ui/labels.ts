// 界面文案（中文）。只在这里改文字，逻辑代码里不写死任何显示文本。

import type { EventKind, ExportFormat, ExportState, ProjectState, RoomName, Shape, TrackName } from "../types";

export const TRACK_LABEL: Record<TrackName, string> = { vocals: "人声", drums: "鼓", bass: "贝斯", other: "其他乐器" };

export const SHAPE_LABEL: Record<Shape, string> = {
  circle: "圆",
  ellipse: "椭圆",
  pendulum: "钟摆",
  figure8: "8 字",
  spiral: "螺旋",
  fixed: "固定",
};

export const ROOM_LABEL: Record<RoomName, string> = { room: "小房间", hall: "大厅", church: "教堂" };

export const PRESET_LABEL: Record<string, string> = {
  classic: "经典 8D",
  singer: "歌手绕着你转",
  dual: "双环反向",
  tumble: "上下翻滚",
  single: "单点环绕（视频同款）",
  layers: "三层环绕",
  diagonal: "斜向环绕",
  cross: "立体交叉",
};

/** 段落名（与后端 structure.py 的标签一致）与时间轴上的颜色。 */
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

export const EVENT_LABEL: Record<EventKind, string> = { hold: "停顿", overhead: "飞过头顶" };

export const PROJECT_STATE_LABEL: Record<ProjectState, string> = {
  UPLOADED: "已上传",
  DECODING: "解码中",
  SEPARATING: "分轨中（人声 / 鼓 / 贝斯 / 乐器）",
  ANALYZING: "测速、校准与段落识别中",
  READY: "就绪",
  FAILED: "处理失败",
};

export const EXPORT_STATE_LABEL: Record<ExportState, string> = {
  QUEUED: "排队中",
  RENDERING: "高质量渲染中",
  ENCODING: "编码中",
  DONE: "完成",
  FAILED: "导出失败",
};

export const FORMAT_LABEL: Record<ExportFormat, string> = {
  m4a: "M4A · AAC 256k（苹果设备推荐）",
  mp3: "MP3 · 320k（兼容性最好）",
  flac: "FLAC · 无损 24-bit",
  wav: "WAV · 无损 24-bit",
  ogg: "OGG · Opus 192k",
};

export const PARAM_LABEL = {
  radius_m: "距离",
  speed: "速度",
  direction: "转动方向",
  start_deg: "起点",
  height_deg: "高度",
  pitch_deg: "前后倾斜",
  roll_deg: "左右倾斜",
  yaw_deg: "水平转向",
  aspect: "椭圆扁度",
  swing_deg: "摆幅",
  lift_deg: "上下起伏",
  gain_db: "音量",
  width_deg: "声像宽度",
  reverb_send: "混响送出",
  wet_db: "这一段的混响量",
  rear_darken_db: "背后压暗",
} as const;

export const TEXT = {
  appName: "Orbit 8D",
  dropTitle: "把一首歌拖到这里",
  dropHint: "支持 mp3 / m4a / flac / wav / ogg 等常见格式，也可以点击选择文件",
  uploading: "上传中",
  headphones: "请戴耳机试听，外放听不出 8D 效果",
  play: "播放",
  pause: "暂停",
  export: "导出",
  exportTitle: "导出 8D 音乐",
  exportStart: "开始导出",
  download: "下载",
  close: "关闭",
  retry: "重新选择文件",
  presets: "一键预设",
  tracks: "音轨",
  orbit: "轨道",
  global: "空间",
  room: "房间",
  cw: "顺时针",
  ccw: "逆时针",
  byBars: "按小节",
  seconds: "按秒",
  mute: "静音",
  solo: "独奏",
  tabOrbit: "轨道",
  tabMix: "混音",
  tabSpace: "空间",
  tabSection: "段落",
  autoChoreo: "✨ 自动编排",
  autoChoreoTitle: "按识别出的段落重新编排：前奏抬高放慢、副歌加速斜绕、桥段立体交叉，副歌开头飞过头顶，主歌里停顿一下",
  autoChoreoDone: "已按段落重新自动编排",
  presetScope: "预设只改当前这一段",
  sectionName: "段落名",
  split: "✂ 在此切分",
  splitTitle: "在播放头处（对齐到最近的小节线）把这一段切成两段",
  removeSection: "删除本段",
  removeSectionTitle: "删掉这一段，时间并给前一段（删第一段时并给后一段）",
  sectionEvents: "这一段的事件",
  noEvents: "这一段还没有事件",
  addAtPlayhead: "在播放头处给当前音轨加：",
  boundaryHint: "拖时间轴上段落之间的竖线可以调整分界（自动对齐小节）；点时间轴跳到那里",
  bars: "小节",
  shape: "形状",
  orientation: "朝向",
  fineTune: "精细调节",
  padHint: "往哪边拖就往哪边翘，越靠边越竖直，双击回水平",
  resetView: "重置视角",
  layer: "所在层",
  showDome: "显示三层半球网格",
  credits: "人头模型：Infinite, 3D Head Scan by Lee Perry-Smith（CC BY 3.0）· HRTF：TH Köln Neumann KU100",
} as const;
