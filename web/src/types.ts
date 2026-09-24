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

export interface Track {
  orbit: Orbit;
  gain_db: number;
  width_deg: number;
  reverb_send: number;
  mute: boolean;
  solo: boolean;
}

export interface Room {
  name: RoomName;
  wet_db: number;
}

export interface Scene {
  version: 1;
  tracks: Record<TrackName, Track>;
  room: Room;
  rear_darken_db: number;
}

export type CalibrationKey = TrackName | "sub";

export interface Analysis {
  sample_rate: number;
  duration_s: number;
  bpm: number;
  bpm_norm: number;
  default_bars: Bars;
  t_ref: number;
  calibration: Record<CalibrationKey, number>;
  preview_gain: number;
}

export type ProjectState = "UPLOADED" | "DECODING" | "SEPARATING" | "ANALYZING" | "READY" | "FAILED";
export type ExportState = "QUEUED" | "RENDERING" | "ENCODING" | "DONE" | "FAILED";
export type ExportFormat = "m4a" | "mp3" | "flac" | "wav" | "ogg";

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
  error: ApiErrorBody | null;
  source: SourceInfo;
  analysis: Analysis | null;
  preview_scale: number;
}

export interface ExportRecord {
  id: string;
  project_id: string;
  state: ExportState;
  format: ExportFormat;
  progress: number;
  error: ApiErrorBody | null;
  file_name: string;
}
