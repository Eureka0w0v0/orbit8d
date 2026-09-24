// 播放条里的时间轴：段落色块（段落名）+ 事件条 + 播放头。
// 点一下跳到那里；拖段落之间的竖线调整分界（由 App 对齐小节并校验）。

import type { Scene } from "../types";
import { fmt, h } from "./dom";
import { DEFAULT_SECTION_COLOR, EVENT_LABEL, SECTION_COLORS, TRACK_LABEL } from "./labels";

export interface TimelineActions {
  seek(t: number): void;
  moveBoundary(k: number, t: number): void;
}

const pct = (x: number) => `${(Math.min(1, Math.max(0, x)) * 100).toFixed(3)}%`;

export class TimelineStrip {
  readonly el: HTMLDivElement;
  private readonly lane: HTMLDivElement;
  private readonly marks: HTMLDivElement;
  private readonly head: HTMLDivElement;
  private blocks: HTMLDivElement[] = [];
  private duration = 0;
  private key = "";
  private dragging: number | null = null;

  constructor(private readonly actions: TimelineActions) {
    this.lane = h("div", { class: "tl-lane" });
    this.marks = h("div", { class: "tl-marks" });
    this.head = h("div", { class: "tl-head" });
    this.el = h("div", { class: "timeline" }, this.marks, this.lane, this.head);
    this.lane.addEventListener("pointerdown", this.onDown);
    this.lane.addEventListener("pointermove", this.onMove);
    this.lane.addEventListener("pointerup", this.onUp);
    this.lane.addEventListener("lostpointercapture", () => (this.dragging = null));
  }

  private timeAt(clientX: number): number {
    const r = this.lane.getBoundingClientRect();
    return r.width > 0 ? Math.min(1, Math.max(0, (clientX - r.left) / r.width)) * this.duration : 0;
  }

  private onDown = (e: PointerEvent): void => {
    if (e.button !== 0 || this.duration <= 0) return;
    const grip = (e.target as HTMLElement).closest<HTMLElement>(".tl-grip");
    if (grip) {
      this.dragging = Number(grip.dataset.k);
      this.lane.setPointerCapture(e.pointerId);
      return;
    }
    this.actions.seek(this.timeAt(e.clientX));
  };

  private onMove = (e: PointerEvent): void => {
    if (this.dragging !== null) this.actions.moveBoundary(this.dragging, this.timeAt(e.clientX));
  };

  private onUp = (e: PointerEvent): void => {
    if (this.dragging === null) return;
    this.dragging = null;
    this.lane.releasePointerCapture(e.pointerId);
  };

  /** 段落或事件变了才重建；current 是播放头所在的段。 */
  setScene(scene: Scene, duration: number, current: number): void {
    const key = JSON.stringify([duration, scene.sections.map((s) => [s.start_s, s.label]), scene.events]);
    if (key !== this.key) {
      this.key = key;
      this.duration = duration;
      this.rebuild(scene);
    }
    this.blocks.forEach((b, i) => b.classList.toggle("current", i === current));
  }

  private rebuild(scene: Scene): void {
    const d = this.duration || 1;
    const n = scene.sections.length;
    this.blocks = scene.sections.map((s, k) => {
      const end = k + 1 < n ? scene.sections[k + 1].start_s : d;
      return h(
        "div",
        {
          class: "tl-block",
          style: `left:${pct(s.start_s / d)};width:${pct((end - s.start_s) / d)};--c:${SECTION_COLORS[s.label] ?? DEFAULT_SECTION_COLOR}`,
          title: `${s.label} · ${fmt.clock(s.start_s)}–${fmt.clock(end)}`,
        },
        s.label,
      );
    });
    const grips = scene.sections.slice(1).map((s, i) => h("div", { class: "tl-grip", "data-k": i + 1, style: `left:${pct(s.start_s / d)}` }));
    this.lane.replaceChildren(...this.blocks, ...grips);
    this.marks.replaceChildren(
      ...scene.events.map((e) =>
        h("div", {
          class: `tl-mark ${e.kind}`,
          style: `left:${pct(e.t_s / d)};width:${pct(e.duration_s / d)}`,
          title: `${EVENT_LABEL[e.kind]} · ${e.targets.map((t) => TRACK_LABEL[t]).join("、")} · ${fmt.clock(e.t_s)}`,
        }),
      ),
    );
  }

  setTime(t: number): void {
    this.head.style.left = pct(this.duration > 0 ? t / this.duration : 0);
  }
}
