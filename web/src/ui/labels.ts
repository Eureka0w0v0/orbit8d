// 界面文案（中文）。只在这里改文字，逻辑代码里不写死任何显示文本。

import type { ExportFormat, ExportState, ProjectState, RoomName, Shape, TrackName } from "../types";

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
};

export const PROJECT_STATE_LABEL: Record<ProjectState, string> = {
  UPLOADED: "已上传",
  DECODING: "解码中",
  SEPARATING: "分轨中（人声 / 鼓 / 贝斯 / 乐器）",
  ANALYZING: "测速与校准中",
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
  direction: "方向",
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
  wet_db: "混响量",
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
  bars: "按小节",
  seconds: "按秒",
  mute: "静音",
  solo: "独奏",
  dragHint: "拖动左侧白点改距离，拖正前 / 右侧圆环改倾斜；暂停时可以拖小球改起点",
  resetView: "重置视角",
  credits: "人头模型：Infinite, 3D Head Scan by Lee Perry-Smith（CC BY 3.0）· HRTF：TH Köln Neumann KU100",
} as const;
