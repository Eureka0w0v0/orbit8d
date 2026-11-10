// 3D 按需重画（SPEC §10.2）：画面没有任何变化时不重画，静止时不占 GPU；电平表与时间是 DOM，照常每帧更新。

export interface FrameState {
  dirty: boolean; // 场景、选中音轨、半球网格、窗口大小等变过（Stage.invalidate）
  cameraMoved: boolean; // OrbitControls.update() 返回 true：拖视角、缩放、阻尼惯性
  animating: boolean; // 逐帧回调说这一帧画面在动：播放中、拖播放头、播放位置跳了
}

export function needsRedraw(s: FrameState): boolean {
  return s.dirty || s.cameraMoved || s.animating;
}
