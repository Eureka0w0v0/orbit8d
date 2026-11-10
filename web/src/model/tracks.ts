// 四条音轨各自固定不变的属性：界面上用的颜色、是不是立体声轨。

import type { TrackName } from "../types";

/** 每条音轨的颜色（左栏、面板标题、3D 轨道与色点都用它）。 */
export const TRACK_COLORS: Record<TrackName, number> = {
  vocals: 0xff6b9a,
  drums: 0xffc24b,
  bass: 0x8f7dff,
  other: 0x35d6c3,
};

/** 立体声轨：渲染成左右一对声源，有“声像宽度”可调。 */
export const STEREO_TRACKS: ReadonlySet<TrackName> = new Set(["drums", "other"]);
