// 导出对话框：选格式与响度 → 进度 → 下载链接（完成时自动开始下载）或出错提示。

import type { ExportFormat, Loudness } from "../types";
import { h } from "./dom";
import { FORMAT_LABEL, TEXT } from "./labels";

const LOUDNESS: readonly Loudness[] = ["loud", "standard"];

export class ExportDialog {
  readonly el: HTMLDivElement;
  private readonly body: HTMLDivElement;

  /** onClose：对话框关掉了（点了关闭、点了外面，或者歌被关掉）。 */
  constructor(private readonly onClose: () => void) {
    this.body = h("div", { class: "dialog-body" });
    this.el = h("div", { class: "modal hidden" }, h("div", { class: "dialog" }, h("h2", {}, TEXT.exportTitle), this.body));
    this.el.addEventListener("click", (e) => e.target === this.el && this.close());
  }

  open(formats: ExportFormat[], start: (format: ExportFormat, loudness: Loudness) => void): void {
    this.el.classList.remove("hidden");
    let chosen: ExportFormat = formats.includes("m4a") ? "m4a" : formats[0];
    let loudness: Loudness = "loud";
    const list = h("div", { class: "formats" });
    for (const f of formats) {
      const radio = h("input", { type: "radio", name: "fmt", value: f, checked: f === chosen });
      radio.addEventListener("change", () => (chosen = f));
      list.append(h("label", { class: "format" }, radio, h("span", {}, FORMAT_LABEL[f])));
    }
    const levels = h("div", { class: "formats loudness" }, h("div", { class: "dialog-label" }, TEXT.loudness));
    const labels: Record<Loudness, string> = { loud: TEXT.loudnessLoud, standard: TEXT.loudnessStandard };
    for (const level of LOUDNESS) {
      const radio = h("input", { type: "radio", name: "loudness", value: level, checked: level === loudness });
      radio.addEventListener("change", () => (loudness = level));
      levels.append(h("label", { class: "format" }, radio, h("span", {}, labels[level])));
    }
    this.body.replaceChildren(
      list,
      levels,
      h("div", { class: "actions" }, h("button", { type: "button", class: "ghost", onclick: () => this.close() }, TEXT.close), h("button", { type: "button", class: "primary", onclick: () => start(chosen, loudness) }, TEXT.exportStart)),
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
    this.onClose();
  }
}
