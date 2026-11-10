// 场景的自动保存：改完一会儿没再改才保存（整份替换，只存最后一次），失败隔几秒重试。
// 显示给用户的状态只有三个——已保存 / 保存中 / 保存失败——由最近发生的事决定（STATE_AFTER）。
// 保存函数与定时器由外面传入，这里不碰网络和浏览器。

import type { Scene } from "../types";

export const SAVE_DEBOUNCE_MS = 800;
export const SAVE_RETRY_MS = 5000;

export type SaveState = "saved" | "saving" | "failed";
type SaveEvent = "changed" | "stored" | "storeFailed" | "opened";

/** 状态机：发生了什么 → 之后显示什么。保存结果只有“是最新一次保存、且之后没有新改动”时才算数（见 flush）。 */
export const STATE_AFTER: Record<SaveEvent, SaveState> = {
  changed: "saving", // 有了新改动
  stored: "saved", // 最新的改动存好了
  storeFailed: "failed", // 最新一次保存失败，等着重试
  opened: "saved", // 刚打开一首歌：界面上的场景就是存着的那份
};

export interface SaveJob {
  projectId: string;
  scene: Scene;
}

export interface AutosaveDeps {
  /** 把场景存到后端；keepalive：页面正在关闭，也尽量发出去。 */
  save(job: SaveJob, keepalive: boolean): Promise<void>;
  /** 状态变了（或被重新确认），界面据此更新提示。 */
  onState(state: SaveState): void;
  /** 一次保存失败了（之后会重试）。 */
  onError(err: unknown): void;
  setTimer(fn: () => void, ms: number): number;
  clearTimer(id: number): void;
}

export class Autosave {
  private pending: SaveJob | null = null;
  private timer = 0;
  private seq = 0;
  private current: SaveState = "saved";

  constructor(private readonly deps: AutosaveDeps) {}

  get state(): SaveState {
    return this.current;
  }

  private emit(event: SaveEvent): void {
    this.current = STATE_AFTER[event];
    this.deps.onState(this.current);
  }

  /** 场景变了：记下最新的这份，SAVE_DEBOUNCE_MS 内没再变就保存。 */
  changed(job: SaveJob): void {
    this.pending = job;
    this.emit("changed");
    this.deps.clearTimer(this.timer);
    this.timer = this.deps.setTimer(() => void this.flush(), SAVE_DEBOUNCE_MS);
  }

  /** 现在就把没存的发出去（换歌、切语言、关页面之前；定时器到点也走这里）。没有待存的就什么也不做。 */
  async flush(keepalive = false): Promise<void> {
    this.deps.clearTimer(this.timer);
    const job = this.pending;
    if (!job) return;
    this.pending = null;
    const seq = ++this.seq;
    try {
      await this.deps.save(job, keepalive);
      if (seq === this.seq && !this.pending) this.emit("stored");
    } catch (err) {
      this.deps.onError(err);
      if (seq !== this.seq) return; // 已经有更新的保存在进行
      this.pending ??= job;
      this.emit("storeFailed");
      this.timer = this.deps.setTimer(() => void this.flush(), SAVE_RETRY_MS);
    }
  }

  /** 刚打开一首歌。 */
  opened(): void {
    this.emit("opened");
  }

  /**
   * 做一件会让没存的改动作废的事（删掉正在用的歌）：期间先不保存（免得往正在删的歌里存）。
   * 做成了，改动作废；没做成（后端拒绝了），把改动放回去照常保存——等结果的时候又改过的话，更新的那份已经在排队。
   */
  async discardWhile<T>(action: () => Promise<T>): Promise<T> {
    this.deps.clearTimer(this.timer);
    const held = this.pending;
    this.pending = null;
    try {
      return await action();
    } catch (err) {
      if (held && !this.pending) this.changed(held);
      throw err;
    }
  }
}
