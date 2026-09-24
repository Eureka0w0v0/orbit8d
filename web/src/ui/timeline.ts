// 播放条里的时间轴：段落色块 + 原曲音量起伏 + 事件条 + 播放头。
// 点色块跳到那里，按住左右拖可以预览（松手才真正跳过去）；拖段落之间的竖线调整分界；拖事件条移动、拖事件条右边缘改长短（吸附到拍，按住 ⌥ 自由），
// 点一下事件条选中它（Delete 删除）。数值合法性（吸附、不重叠）由 App 调 scene/edit.ts 保证。

import { eventAnchor } from "../scene/edit";
import type { Scene } from "../types";
import { fmt, h } from "./dom";
import { DEFAULT_SECTION_COLOR, EVENT_LABEL, SECTION_COLORS, TEXT, TRACK_LABEL } from "./labels";

export interface TimelineActions {
  /** 在色块上按下：开始拖播放头（单击 = 按下后马上松开）。 */
  scrubStart(t: number): void;
  scrub(t: number): void;
  /** 松手（或拖动被系统打断）：跳到 t。 */
  scrubEnd(t: number): void;
  moveBoundary(k: number, t: number): void;
  selectEvent(index: number | null): void;
  /** anchor：事件条的新起点（停顿 = 完全停住的时刻）。free：不吸附。 */
  moveEvent(index: number, anchor: number, free: boolean): void;
  resizeEvent(index: number, end: number, free: boolean): void;
}

type Drag =
  | { kind: "scrub"; last: number }
  | { kind: "boundary"; k: number }
  | { kind: "move"; index: number; grab: number }
  | { kind: "resize"; index: number };

const EDGE_PX = 6; // 事件条右端这么宽的区域是“改长短”，但最多占条宽的 1/3（短事件也能拖动位置）
const ENVELOPE_RANGE_DB = 40; // 比最响处低 40 dB 以下都贴底
const ENVELOPE_FILL = "rgba(255, 255, 255, 0.10)";
const ENVELOPE_LINE = "rgba(255, 255, 255, 0.30)";

const pct = (x: number) => `${(Math.min(1, Math.max(0, x)) * 100).toFixed(3)}%`;

export class TimelineStrip {
  readonly el: HTMLDivElement;
  private readonly lane: HTMLDivElement;
  private readonly marks: HTMLDivElement;
  private readonly head: HTMLDivElement;
  private readonly envelope: HTMLCanvasElement;
  private readonly resizeObserver: ResizeObserver;
  private blocks: HTMLDivElement[] = [];
  private markEls: HTMLDivElement[] = [];
  private anchors: number[] = [];
  private duration = 0;
  private key = "";
  private drag: Drag | null = null;
  private envelopeDb: number[] = [];
  private envelopeHop = 0.25;

  constructor(private readonly actions: TimelineActions) {
    this.envelope = h("canvas", { class: "tl-envelope" });
    this.lane = h("div", { class: "tl-lane" });
    this.marks = h("div", { class: "tl-marks", title: TEXT.eventHint });
    this.head = h("div", { class: "tl-head" });
    this.el = h("div", { class: "timeline" }, this.marks, this.lane, this.head);
    for (const target of [this.lane, this.marks]) {
      target.addEventListener("pointermove", this.onMove);
      target.addEventListener("pointerup", this.onUp);
      target.addEventListener("lostpointercapture", this.onLost);
    }
    this.lane.addEventListener("pointerdown", this.onLaneDown);
    this.marks.addEventListener("pointerdown", this.onMarksDown);
    this.resizeObserver = new ResizeObserver(() => this.drawEnvelope());
    this.resizeObserver.observe(this.lane);
  }

  private timeAt(clientX: number): number {
    const r = this.lane.getBoundingClientRect();
    return r.width > 0 ? Math.min(1, Math.max(0, (clientX - r.left) / r.width)) * this.duration : 0;
  }

  private onLaneDown = (e: PointerEvent): void => {
    if (e.button !== 0 || this.duration <= 0) return;
    const grip = (e.target as HTMLElement).closest<HTMLElement>(".tl-grip");
    if (grip) {
      this.drag = { kind: "boundary", k: Number(grip.dataset.k) };
      this.lane.setPointerCapture(e.pointerId);
      return;
    }
    const t = this.timeAt(e.clientX);
    this.drag = { kind: "scrub", last: t };
    this.lane.setPointerCapture(e.pointerId);
    this.actions.scrubStart(t);
  };

  private onMarksDown = (e: PointerEvent): void => {
    if (e.button !== 0 || this.duration <= 0) return;
    const mark = (e.target as HTMLElement).closest<HTMLElement>(".tl-mark");
    if (!mark) {
      this.actions.selectEvent(null);
      return;
    }
    const index = Number(mark.dataset.index);
    this.actions.selectEvent(index);
    this.drag = this.onEdge(mark, e.clientX) ? { kind: "resize", index } : { kind: "move", index, grab: this.timeAt(e.clientX) - this.anchors[index] };
    this.marks.setPointerCapture(e.pointerId); // 容器不会被重建，拖动中事件条重画也不丢
    e.preventDefault();
  };

  private onEdge(mark: HTMLElement, clientX: number): boolean {
    const r = mark.getBoundingClientRect();
    return clientX > r.right - Math.min(EDGE_PX, r.width / 3);
  }

  private onMove = (e: PointerEvent): void => {
    const d = this.drag;
    if (!d) {
      const mark = (e.target as HTMLElement).closest<HTMLElement>(".tl-mark");
      this.marks.style.cursor = mark ? (this.onEdge(mark, e.clientX) ? "ew-resize" : "grab") : "";
      return;
    }
    const t = this.timeAt(e.clientX);
    if (d.kind === "scrub") {
      d.last = t;
      this.actions.scrub(t);
    } else if (d.kind === "boundary") this.actions.moveBoundary(d.k, t);
    else if (d.kind === "move") this.actions.moveEvent(d.index, t - d.grab, e.altKey);
    else this.actions.resizeEvent(d.index, t, e.altKey);
  };

  private onUp = (e: PointerEvent): void => {
    const d = this.drag;
    if (!d) return;
    this.drag = null; // 先清掉，释放捕获时触发的 lostpointercapture 就不会再结束一次
    (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
    if (d.kind === "scrub") this.actions.scrubEnd(this.timeAt(e.clientX));
  };

  /** 拖动被系统打断（切走窗口等）：拖播放头时跳到最后的位置。 */
  private onLost = (): void => {
    const d = this.drag;
    this.drag = null;
    if (d?.kind === "scrub") this.actions.scrubEnd(d.last);
  };

  /** 原曲音量起伏（每首歌设一次）。 */
  setEnvelope(db: number[], hopS: number): void {
    this.envelopeDb = db;
    this.envelopeHop = hopS;
    this.drawEnvelope();
  }

  /** 段落或事件变了才重建；current = 播放头所在段，selected = 选中的事件。 */
  setScene(scene: Scene, duration: number, current: number, selected: number | null): void {
    const key = JSON.stringify([duration, scene.sections.map((s) => [s.start_s, s.label]), scene.events]);
    if (key !== this.key) {
      this.key = key;
      const durationChanged = duration !== this.duration;
      this.duration = duration;
      this.rebuild(scene);
      if (durationChanged) this.drawEnvelope();
    }
    this.blocks.forEach((b, i) => b.classList.toggle("current", i === current));
    this.markEls.forEach((m, i) => m.classList.toggle("selected", i === selected));
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
    this.lane.replaceChildren(...this.blocks, this.envelope, ...grips);
    this.anchors = scene.events.map(eventAnchor);
    this.markEls = scene.events.map((e, i) =>
      h(
        "div",
        {
          class: `tl-mark ${e.kind}`,
          "data-index": i,
          style: `left:${pct(this.anchors[i] / d)};width:${pct(e.duration_s / d)}`,
          title: `${EVENT_LABEL[e.kind]} · ${e.targets.map((t) => TRACK_LABEL[t]).join("、")} · ${fmt.clock(this.anchors[i])}`,
        },
      ),
    );
    this.marks.replaceChildren(...this.markEls);
  }

  private drawEnvelope(): void {
    const canvas = this.envelope;
    const w = this.lane.clientWidth;
    const hgt = this.lane.clientHeight;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(w * dpr));
    canvas.height = Math.max(1, Math.round(hgt * dpr));
    const ctx = canvas.getContext("2d");
    if (!ctx || this.duration <= 0 || this.envelopeDb.length === 0) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, hgt);
    const y = (db: number) => hgt * (1 - Math.min(1, Math.max(0, (db + ENVELOPE_RANGE_DB) / ENVELOPE_RANGE_DB)));
    ctx.beginPath();
    ctx.moveTo(0, hgt);
    this.envelopeDb.forEach((db, i) => ctx.lineTo((((i + 0.5) * this.envelopeHop) / this.duration) * w, y(db)));
    ctx.lineTo(w, hgt);
    ctx.closePath();
    ctx.fillStyle = ENVELOPE_FILL;
    ctx.fill();
    ctx.beginPath();
    this.envelopeDb.forEach((db, i) => {
      const x = (((i + 0.5) * this.envelopeHop) / this.duration) * w;
      if (i === 0) ctx.moveTo(x, y(db));
      else ctx.lineTo(x, y(db));
    });
    ctx.strokeStyle = ENVELOPE_LINE;
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  setTime(t: number): void {
    this.head.style.left = pct(this.duration > 0 ? t / this.duration : 0);
  }
}
