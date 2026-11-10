// 与后端 pydantic 模型（backend/orbit8d/engine/scene.py）一一对应。参数范围以后端 /api/scene/schema 为准。

export type Shape = "circle" | "ellipse" | "pendulum" | "figure8" | "spiral" | "fixed";
export type TrackName = "vocals" | "drums" | "bass" | "other";
export const TRACKS: readonly TrackName[] = ["vocals", "drums", "bass", "other"];
export type RoomName = "room" | "hall" | "church";
export type Bars = 1 | 2 | 4 | 8;

export interface Speed {
  mode: "bars" | "seconds";
  bars: Bars;
  seconds: number;
}

export interface Orbit {
  shape: Shape;
  radius_m: number;
  speed: Speed;
  direction: "cw" | "ccw";
  start_deg: number;
  height_deg: number;
  pitch_deg: number;
  roll_deg: number;
  yaw_deg: number;
  aspect: number;
  swing_deg: number;
  lift_deg: number;
}

/** 全曲共用的混音设置（不随分段变化）。 */
export interface Mix {
  gain_db: number;
  width_deg: number;
  reverb_send: number;
  mute: boolean;
  solo: boolean;
}

/** 一段：从 start_s 开始到下一段开始，各音轨沿各自的轨道走；wet_db 是这一段的混响量。 */
export interface Section {
  start_s: number;
  label: string;
  orbits: Record<TrackName, Orbit>;
  wet_db: number;
}

export type EventKind = "hold" | "overhead";

/** 停顿：0.5 秒减速 → 停住 duration_s → 0.5 秒加速；飞过头顶：duration_s 内经过正上方。 */
export interface SceneEvent {
  t_s: number;
  kind: EventKind;
  duration_s: number;
  targets: TrackName[];
}

export interface Room {
  name: RoomName;
}

export interface Scene {
  version: 2;
  mix: Record<TrackName, Mix>;
  sections: Section[];
  events: SceneEvent[];
  room: Room;
  rear_darken_db: number;
}

/** 与后端 scene.HOLD_RAMP_S 相同。 */
export const HOLD_RAMP_S = 0.5;

/** 自动识别的段落（后端 structure.SectionInfo）。 */
export interface SectionInfo {
  start_s: number;
  bars: number;
  label: string;
  energy_db: number;
}

export type CalibrationKey = TrackName | "sub";

/** 试听时在听哪一路：8D 渲染，还是原曲（A/B 对比）。只有前端有这个概念。 */
export type ListenMode = "8d" | "original";

export interface Analysis {
  sample_rate: number;
  duration_s: number;
  bpm: number;
  bpm_norm: number;
  default_bars: Bars;
  t_ref: number;
  calibration: Record<CalibrationKey, number>;
  preview_gain: number;
  match_eq_db: number[];
  spectra: Record<string, number[]>;
  sections: SectionInfo[];
  original_gain: number; // 原曲试听的增益：和 8D 试听一样响
  envelope_db: number[]; // 原曲每 envelope_hop_s 秒的音量（相对最响处，dB，最低 -60）
  envelope_hop_s: number;
  version: number;
}

export type ProjectState = "UPLOADED" | "DECODING" | "SEPARATING" | "ANALYZING" | "READY" | "FAILED";
export type ExportState = "QUEUED" | "RENDERING" | "ENCODING" | "DONE" | "FAILED";
export type ExportFormat = "m4a" | "mp3" | "flac" | "wav" | "ogg";
export type Loudness = "loud" | "standard"; // 导出响度：响亮（与试听一致）/ 标准 -14 LUFS

export interface ApiErrorBody {
  code: string;
  message: string;
}

export interface SourceInfo {
  filename: string;
  size: number;
  duration_s: number;
  title: string;
  artist: string;
  album: string;
  has_cover: boolean;
}

export interface Project {
  id: string;
  state: ProjectState;
  progress: number;
  step: string; // 当前状态里的子步骤："model_download" = 第一次下载分轨模型
  error: ApiErrorBody | null;
  source: SourceInfo;
  analysis: Analysis | null;
  preview_scale: number;
}

/** 最近的歌（GET /api/projects），最近用过的在前。 */
export interface ProjectSummary {
  id: string;
  state: ProjectState;
  title: string;
  artist: string;
  duration_s: number;
  size_bytes: number;
  error: ApiErrorBody | null;
  last_used: number;
}

export interface ExportRecord {
  id: string;
  project_id: string;
  state: ExportState;
  format: ExportFormat;
  loudness: Loudness;
  error: ApiErrorBody | null;
  file_name: string;
}
