// 底部的播放条：播放 / 暂停、8D / 原曲切换、时间、时间轴、电平表。

import type { ListenMode } from "../audio/engine";
import { fmt, h } from "./dom";
import { TEXT } from "./labels";

const METER_RANGE_DB = 60;

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
