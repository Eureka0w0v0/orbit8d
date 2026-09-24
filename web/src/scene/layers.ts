// 三层半球（参考杜比全景声示意图）：按仰角把空间分成环绕层 / 高度层 / 顶层。
// 只影响显示与“一键换层”，渲染仍按连续的 height_deg 计算。

export type LayerName = "surround" | "height" | "top";

export interface Layer {
  name: LayerName;
  from: number; // 仰角下界（度）
  to: number; // 仰角上界（度）
  center: number; // 一键换层时设置的高度
  color: number;
}

export const LAYERS: readonly Layer[] = [
  { name: "surround", from: -15, to: 15, center: 0, color: 0x4aa8ff },
  { name: "height", from: 15, to: 60, center: 35, color: 0xffa23a },
  { name: "top", from: 60, to: 90, center: 75, color: 0xff5468 },
];

/** 环绕层含两端；其余层含上界不含下界。低于 -15° 不属于任何层。 */
export function layerOf(heightDeg: number): LayerName | null {
  if (heightDeg >= LAYERS[0].from && heightDeg <= LAYERS[0].to) return LAYERS[0].name;
  for (const layer of LAYERS.slice(1)) {
    if (heightDeg > layer.from && heightDeg <= layer.to) return layer.name;
  }
  return null;
}
