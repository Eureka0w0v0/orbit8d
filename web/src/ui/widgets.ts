// 播放条、导入/进度弹层、导出对话框。

import type { ListenMode } from "../audio/engine";
import type { ExportFormat } from "../types";
import { fmt, h } from "./dom";
import { FORMAT_LABEL, TEXT } from "./labels";

const METER_RANGE_DB = 60;
const ACCEPT = "audio/*,.mp3,.m4a,.aac,.flac,.wav,.aiff,.aif,.ogg,.opus";

export class Transport {
  readonly el: HTMLElement;
  private readonly play: HTMLButtonElement;
  private readonly listen: HTMLButtonElement;
  private readonly time: HTMLSpanElement;
  private readonly info: HTMLSpanElement;
  private readonly meterBars: [HTMLDivElement, HTMLDivElement];
  private readonly meter: HTMLDivElement;

  /** timeline：代替进度条的时间轴（段落 + 事件 + 播放头）；onListen：切换 8D / 原曲。 */
  constructor(onToggle: () => void, timeline: HTMLElement, onListen: () => void) {
    this.play = h("button", { type: "button", class: "play", title: `${TEXT.play}（空格）` }, "▶");
    this.play.addEventListener("click", onToggle);
    this.listen = h("button", { type: "button", class: "listen", title: TEXT.listenTitle }, TEXT.listen8d);
    this.listen.addEventListener("click", onListen);
    this.time = h("span", { class: "time" }, "0:00 / 0:00");
    this.info = h("span", { class: "info" });
    this.meterBars = [h("div", { class: "lvl" }), h("div", { class: "lvl" })];
    this.meter = h("div", { class: "meter", title: "左 / 右耳电平" }, h("span", {}, "L"), h("div", { class: "track" }, this.meterBars[0]), h("span", {}, "R"), h("div", { class: "track" }, this.meterBars[1]));
    this.el = h("footer", { class: "transport" }, this.play, this.listen, this.time, timeline, this.info, this.meter, h("span", { class: "phones" }, `🎧 ${TEXT.headphones}`));
  }

  update(t: number, duration: number, playing: boolean): void {
    this.play.textContent = playing ? "❚❚" : "▶";
    this.play.title = `${playing ? TEXT.pause : TEXT.play}（空格）`;
    this.time.textContent = `${fmt.clock(t)} / ${fmt.clock(duration)}`;
  }

  /** 电平（dB）→ 条长；-60 dB 以下视为无声。 */
  setLevels(levels: [number, number]): void {
    levels.forEach((db, i) => {
      const fraction = Math.min(1, Math.max(0, (db + METER_RANGE_DB) / METER_RANGE_DB));
      this.meterBars[i].style.width = `${Math.round(fraction * 100)}%`;
      this.meterBars[i].dataset.db = db.toFixed(1);
    });
  }

  setListen(mode: ListenMode, available: boolean): void {
    this.listen.textContent = mode === "original" ? TEXT.listenOriginal : TEXT.listen8d;
    this.listen.classList.toggle("original", mode === "original");
    this.listen.disabled = !available;
  }

  setInfo(text: string): void {
    this.info.textContent = text;
  }
}

/** 覆盖整个窗口的弹层：拖入导入 / 处理进度 / 错误。 */
export class Overlay {
  readonly el: HTMLDivElement;
  private readonly card: HTMLDivElement;
  private readonly picker: HTMLInputElement;

  constructor(onFile: (file: File) => void) {
    this.picker = h("input", { type: "file", accept: ACCEPT, class: "hidden" });
    this.picker.addEventListener("change", () => {
      const file = this.picker.files?.[0];
      this.picker.value = "";
      if (file) onFile(file);
    });
    this.card = h("div", { class: "card" });
    this.el = h("div", { class: "overlay" }, this.card, this.picker);
    window.addEventListener("dragover", (e) => {
      e.preventDefault();
      document.body.classList.add("dragging");
    });
    window.addEventListener("dragleave", (e) => {
      if (e.relatedTarget === null) document.body.classList.remove("dragging");
    });
    window.addEventListener("drop", (e) => {
      e.preventDefault();
      document.body.classList.remove("dragging");
      const file = e.dataTransfer?.files?.[0];
      if (file) onFile(file);
    });
  }

  pickFile(): void {
    this.picker.click();
  }

  showDrop(): void {
    this.el.classList.remove("hidden");
    const zone = h("button", { type: "button", class: "dropzone", onclick: () => this.pickFile() }, h("div", { class: "drop-icon" }, "♫"), h("h1", {}, TEXT.dropTitle), h("p", {}, TEXT.dropHint));
    this.card.replaceChildren(zone);
  }

  showProgress(title: string, fraction: number | null, detail = ""): void {
    this.el.classList.remove("hidden");
    const bar = h("div", { class: `bar ${fraction === null ? "indeterminate" : ""}` }, h("div", { class: "fill", style: `width:${Math.round((fraction ?? 0) * 100)}%` }));
    this.card.replaceChildren(h("div", { class: "progress" }, h("h1", {}, title), bar, detail ? h("p", {}, detail) : null));
  }

  showError(message: string): void {
    this.el.classList.remove("hidden");
    this.card.replaceChildren(
      h("div", { class: "progress error" }, h("h1", {}, "出错了"), h("p", {}, message), h("button", { type: "button", onclick: () => this.pickFile() }, TEXT.retry)),
    );
  }

  hide(): void {
    this.el.classList.add("hidden");
  }
}

export class ExportDialog {
  readonly el: HTMLDivElement;
  private readonly body: HTMLDivElement;

  constructor() {
    this.body = h("div", { class: "dialog-body" });
    this.el = h("div", { class: "modal hidden" }, h("div", { class: "dialog" }, h("h2", {}, TEXT.exportTitle), this.body));
    this.el.addEventListener("click", (e) => e.target === this.el && this.close());
  }

  open(formats: ExportFormat[], start: (format: ExportFormat) => void): void {
    this.el.classList.remove("hidden");
    let chosen: ExportFormat = formats.includes("m4a") ? "m4a" : formats[0];
    const list = h("div", { class: "formats" });
    for (const f of formats) {
      const radio = h("input", { type: "radio", name: "fmt", value: f, checked: f === chosen });
      radio.addEventListener("change", () => (chosen = f));
      list.append(h("label", { class: "format" }, radio, h("span", {}, FORMAT_LABEL[f])));
    }
    this.body.replaceChildren(
      list,
      h("div", { class: "actions" }, h("button", { type: "button", class: "ghost", onclick: () => this.close() }, TEXT.close), h("button", { type: "button", class: "primary", onclick: () => start(chosen) }, TEXT.exportStart)),
    );
  }

  progress(label: string): void {
    this.body.replaceChildren(h("div", { class: "progress" }, h("p", {}, label), h("div", { class: "bar indeterminate" }, h("div", { class: "fill" }))));
  }

  done(url: string, fileName: string): void {
    const link = h("a", { class: "primary button", href: url, download: fileName }, `${TEXT.download} ${fileName}`);
    this.body.replaceChildren(link, h("div", { class: "actions" }, h("button", { type: "button", class: "ghost", onclick: () => this.close() }, TEXT.close)));
    link.click();
  }

  error(message: string): void {
    this.body.replaceChildren(h("p", { class: "error-text" }, message), h("div", { class: "actions" }, h("button", { type: "button", class: "ghost", onclick: () => this.close() }, TEXT.close)));
  }

  close(): void {
    this.el.classList.add("hidden");
  }
}
