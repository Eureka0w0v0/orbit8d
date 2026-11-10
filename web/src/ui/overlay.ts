// 覆盖整个窗口的弹层：拖入 / 选择文件导入（下面可带最近的歌）、处理进度、出错提示。

import type { Lang } from "../i18n";
import { h } from "./dom";
import { TEXT } from "./labels";
import { languageSelect } from "./language";

const ACCEPT = "audio/*,.mp3,.m4a,.aac,.flac,.wav,.aiff,.aif,.ogg,.opus";

/** 覆盖整个窗口的弹层：拖入导入 / 处理进度 / 错误。 */
export class Overlay {
  readonly el: HTMLDivElement;
  private readonly card: HTMLDivElement;
  private readonly picker: HTMLInputElement;

  constructor(
    onFile: (file: File) => void,
    private readonly onLanguage: (next: Lang) => void,
  ) {
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

  /** recent：最近的歌列表（没有就不显示这一块）。 */
  showDrop(recent: HTMLElement | null = null): void {
    this.el.classList.remove("hidden");
    const zone = h("button", { type: "button", class: "dropzone", onclick: () => this.pickFile() }, h("div", { class: "drop-icon" }, "♫"), h("h1", {}, TEXT.dropTitle), h("p", {}, TEXT.dropHint));
    const language = h("label", { class: "drop-lang" }, h("span", {}, `🌐 ${TEXT.language}`), languageSelect(this.onLanguage));
    this.card.replaceChildren(zone, language, ...(recent ? [h("div", { class: "recent" }, h("h2", {}, TEXT.recentSongs), recent)] : []));
  }

  showProgress(title: string, fraction: number | null, detail = ""): void {
    this.el.classList.remove("hidden");
    const bar = h("div", { class: `bar ${fraction === null ? "indeterminate" : ""}` }, h("div", { class: "fill", style: `width:${Math.round((fraction ?? 0) * 100)}%` }));
    this.card.replaceChildren(h("div", { class: "progress" }, h("h1", {}, title), bar, detail ? h("p", {}, detail) : null));
  }

  showError(message: string): void {
    this.el.classList.remove("hidden");
    this.card.replaceChildren(
      h("div", { class: "progress error" }, h("h1", {}, TEXT.errorTitle), h("p", {}, message), h("button", { type: "button", onclick: () => this.pickFile() }, TEXT.retry)),
    );
  }

  hide(): void {
    this.el.classList.add("hidden");
  }
}
