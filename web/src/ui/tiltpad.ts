// 倾斜盘：俯视人头（上 = 正前）。白点所在方向 = 轨道最高点朝向，离圆心越远翘得越高（边缘 = 90° 竖直）。
// 双击回到水平。只负责交互与显示，换算见 orbit/orientation.ts。

import type { Tilt } from "../orbit/orientation";

const SVG_NS = "http://www.w3.org/2000/svg";
const SIZE = 132;
const R = 54; // 90° 对应的半径（viewBox 单位）
const DOT_R = 6;
const SNAP_FLAT_DEG = 4; // 离圆心很近时吸附到水平
const GUIDE_DEGS = [30, 60];

function el<K extends keyof SVGElementTagNameMap>(tag: K, attrs: Record<string, string | number>): SVGElementTagNameMap[K] {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

export class TiltPad {
  readonly el: SVGSVGElement;
  private readonly dot: SVGCircleElement;
  private readonly stem: SVGLineElement;
  private dragging = false;

  constructor(private readonly onChange: (t: Tilt) => void) {
    const half = SIZE / 2;
    this.el = el("svg", { viewBox: `${-half} ${-half} ${SIZE} ${SIZE}`, class: "tiltpad", role: "slider", "aria-label": "轨道倾斜" });
    this.el.append(el("circle", { cx: 0, cy: 0, r: R, class: "pad-ring" }));
    for (const deg of GUIDE_DEGS) this.el.append(el("circle", { cx: 0, cy: 0, r: (R * deg) / 90, class: "pad-guide" }));
    this.el.append(el("line", { x1: -R, y1: 0, x2: R, y2: 0, class: "pad-guide" }), el("line", { x1: 0, y1: -R, x2: 0, y2: R, class: "pad-guide" }));
    for (const [text, x, y] of [["前", 0, -R - 5], ["后", 0, R + 9], ["左", -R - 8, 3], ["右", R + 8, 3]] as const) {
      const label = el("text", { x, y, class: "pad-label", "text-anchor": "middle" });
      label.textContent = text;
      this.el.append(label);
    }
    this.el.append(el("circle", { cx: 0, cy: 0, r: 8, class: "pad-head" }), el("path", { d: "M -3 -7 L 0 -12 L 3 -7 Z", class: "pad-head" }));
    this.stem = el("line", { x1: 0, y1: 0, x2: 0, y2: 0, class: "pad-stem" });
    this.dot = el("circle", { cx: 0, cy: 0, r: DOT_R, class: "pad-dot" });
    this.el.append(this.stem, this.dot);

    this.el.addEventListener("pointerdown", (e) => {
      this.dragging = true;
      this.el.setPointerCapture(e.pointerId);
      this.emit(e);
    });
    this.el.addEventListener("pointermove", (e) => this.dragging && this.emit(e));
    this.el.addEventListener("pointerup", () => (this.dragging = false));
    this.el.addEventListener("pointercancel", () => (this.dragging = false));
    this.el.addEventListener("dblclick", () => onChange({ azDeg: 0, tiltDeg: 0 }));
  }

  private emit(e: PointerEvent): void {
    const rect = this.el.getBoundingClientRect();
    const x = ((e.clientX - rect.left) / rect.width - 0.5) * SIZE;
    const y = ((e.clientY - rect.top) / rect.height - 0.5) * SIZE;
    let tilt = Math.min(1, Math.hypot(x, y) / R) * 90;
    if (tilt < SNAP_FLAT_DEG) tilt = 0;
    const az = Math.atan2(x, -y) * (180 / Math.PI);
    const t = { azDeg: Math.round(az), tiltDeg: Math.round(tilt) };
    this.place(t);
    this.onChange(t);
  }

  private place(t: Tilt): void {
    const r = (R * t.tiltDeg) / 90;
    const a = (t.azDeg * Math.PI) / 180;
    const cx = r * Math.sin(a);
    const cy = -r * Math.cos(a);
    this.dot.setAttribute("cx", cx.toFixed(2));
    this.dot.setAttribute("cy", cy.toFixed(2));
    this.stem.setAttribute("x2", cx.toFixed(2));
    this.stem.setAttribute("y2", cy.toFixed(2));
  }

  /** 外部同步显示（拖动中不覆盖，避免和手指打架）。 */
  set(t: Tilt): void {
    if (!this.dragging) this.place(t);
  }
}
