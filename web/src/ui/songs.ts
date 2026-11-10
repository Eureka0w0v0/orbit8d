// 最近的歌：列表（导入页与顶栏菜单共用）和顶栏歌名旁的 ▾ 菜单。打开只对已就绪的歌开放；删除要连按两次。

import { T } from "../i18n";
import type { ProjectSummary } from "../types";
import { fmt, h } from "./dom";
import { PROJECT_STATE_LABEL, TEXT } from "./labels";

const DELETE_ARM_MS = 3000; // 删除要在 3 秒内连按两次
const MB = 1024 * 1024;

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
