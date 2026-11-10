// 播放条、导入/进度弹层（含最近的歌）、顶栏的歌曲菜单、导出对话框。

import type { ListenMode } from "../audio/engine";
import { LANGS, LANG_NAMES, T, lang, type Lang } from "../i18n";
import type { ExportFormat, Loudness, ProjectSummary } from "../types";
import { fmt, h } from "./dom";
import { FORMAT_LABEL, PROJECT_STATE_LABEL, TEXT } from "./labels";

const METER_RANGE_DB = 60;
const ACCEPT = "audio/*,.mp3,.m4a,.aac,.flac,.wav,.aiff,.aif,.ogg,.opus";
const DELETE_ARM_MS = 3000; // 删除要在 3 秒内连按两次
const MB = 1024 * 1024;
const LOUDNESS: readonly Loudness[] = ["loud", "standard"];

export interface SongActions {
  open: (id: string) => void;
  remove: (song: ProjectSummary) => void;
}

export function songLabel(song: { title: string; artist: string }): string {
  return song.artist ? `${song.artist} - ${song.title}` : song.title;
}

/** 最近的歌：点歌名打开（只有已就绪的能打开），删除要连按两次才生效，不弹系统对话框。 */
export function songList(songs: readonly ProjectSummary[], currentId: string | null, actions: SongActions): HTMLUListElement {
  const list = h("ul", { class: "songs" });
  for (const song of songs) list.append(songRow(song, song.id === currentId, actions));
  return list;
}

function songRow(song: ProjectSummary, current: boolean, actions: SongActions): HTMLLIElement {
  const meta = [fmt.clock(song.duration_s), T.unit.megabytes((song.size_bytes / MB).toFixed(0))];
  if (song.state !== "READY") meta.push(PROJECT_STATE_LABEL[song.state]);
  const open = h(
    "button",
    { type: "button", class: "song-open", disabled: song.state !== "READY" || current, title: songLabel(song) },
    h("span", { class: "song-title" }, songLabel(song)),
    h("span", { class: "song-meta" }, meta.join(" · ")),
  );
  open.addEventListener("click", () => actions.open(song.id));
  const remove = h("button", { type: "button", class: "ghost song-delete", title: TEXT.deleteSongTitle }, TEXT.deleteSong);
  let timer = 0;
  remove.addEventListener("click", (e) => {
    e.stopPropagation();
    if (remove.classList.contains("armed")) {
      window.clearTimeout(timer);
      remove.disabled = true;
      actions.remove(song);
      return;
    }
    remove.classList.add("armed");
    remove.textContent = TEXT.deleteConfirm;
    timer = window.setTimeout(() => {
      remove.classList.remove("armed");
      remove.textContent = TEXT.deleteSong;
    }, DELETE_ARM_MS);
  });
  return h("li", { class: current ? "song-row current" : "song-row" }, open, remove);
}

/** 顶栏歌名旁的 ▾：展开最近的歌，切歌或删歌。点外面或按 Esc 收起。 */
export class SongMenu {
  readonly button: HTMLButtonElement;
  readonly el: HTMLDivElement;

  constructor(private readonly render: () => Promise<HTMLElement>) {
    this.button = h("button", { type: "button", class: "ghost icon", title: TEXT.songMenuTitle, "aria-haspopup": "true" }, "▾");
    this.el = h("div", { class: "song-menu hidden" });
    this.button.addEventListener("click", (e) => {
      e.stopPropagation();
      void this.toggle();
    });
    document.addEventListener("click", (e) => {
      if (!this.el.contains(e.target as Node)) this.close();
    });
    window.addEventListener("keydown", (e) => e.key === "Escape" && this.close());
  }

  get isOpen(): boolean {
    return !this.el.classList.contains("hidden");
  }

  private async toggle(): Promise<void> {
    if (this.isOpen) {
      this.close();
      return;
    }
    await this.refresh();
    this.el.classList.remove("hidden");
  }

  async refresh(): Promise<void> {
    this.el.replaceChildren(h("h2", {}, TEXT.recentSongs), await this.render());
  }

  close(): void {
    this.el.classList.add("hidden");
  }
}

/** 语言下拉框（中文 / 日本語 / English），选了就交给 onPick（记住并刷新页面）。 */
export function languageSelect(onPick: (next: Lang) => void): HTMLSelectElement {
  const select = h("select", { class: "lang-select", title: TEXT.language, "aria-label": TEXT.language });
  for (const l of LANGS) select.append(h("option", { value: l, selected: l === lang }, LANG_NAMES[l]));
  select.addEventListener("change", () => onPick(select.value as Lang));
  return select;
}

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
    this.play = h("button", { type: "button", class: "play", title: `${TEXT.play} (${TEXT.spaceKey})` }, "▶");
    this.play.addEventListener("click", onToggle);
    this.listen = h("button", { type: "button", class: "listen", title: TEXT.listenTitle }, TEXT.listen8d);
    this.listen.addEventListener("click", onListen);
    this.time = h("span", { class: "time" }, "0:00 / 0:00");
    this.info = h("span", { class: "info" });
    this.meterBars = [h("div", { class: "lvl" }), h("div", { class: "lvl" })];
    this.meter = h("div", { class: "meter", title: TEXT.meterTitle }, h("span", {}, "L"), h("div", { class: "track" }, this.meterBars[0]), h("span", {}, "R"), h("div", { class: "track" }, this.meterBars[1]));
    this.el = h("footer", { class: "transport" }, this.play, this.listen, this.time, timeline, this.info, this.meter, h("span", { class: "phones" }, `🎧 ${TEXT.headphones}`));
  }

  update(t: number, duration: number, playing: boolean): void {
    this.play.textContent = playing ? "❚❚" : "▶";
    this.play.title = `${playing ? TEXT.pause : TEXT.play} (${TEXT.spaceKey})`;
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
