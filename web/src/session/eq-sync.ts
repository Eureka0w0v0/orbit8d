// 让试听用的补偿 EQ 与房间混响跟上场景：场景改完一会儿没再改才去取新 EQ，只用最后一次请求的结果；
// 房间的混响数据取过就留着，换回去不用再下载。取数与装载都由外面传入，这里不碰网络和 Web Audio。

import type { RoomName, Scene } from "../types";

export const EQ_DEBOUNCE_MS = 250;

export interface EqTarget {
  projectId: string;
  scene: Scene;
}

export interface EqSyncDeps {
  /** 现在该按哪个场景取 EQ（没有打开歌时为 null）。到点才问，所以取到的总是最新的场景。 */
  target(): EqTarget | null;
  fetchEq(target: EqTarget): Promise<ArrayBuffer>;
  applyEq(wav: ArrayBuffer): Promise<void>;
  fetchRoom(room: RoomName): Promise<ArrayBuffer>;
  applyRoom(wav: ArrayBuffer): Promise<void>;
  /** 取 EQ 或装 EQ 失败：继续用原来的那个。 */
  onError(err: unknown): void;
  setTimer(fn: () => void, ms: number): number;
  clearTimer(id: number): void;
}

export class EqSync {
  private timer = 0;
  private seq = 0;
  private readonly rooms = new Map<RoomName, ArrayBuffer>();
  private loaded: RoomName | null = null;

  constructor(private readonly deps: EqSyncDeps) {}

  /** 现在装着的房间（还没装过为 null）。 */
  get room(): RoomName | null {
    return this.loaded;
  }

  /** 场景变了：EQ_DEBOUNCE_MS 内没再变，就去取这个场景的补偿 EQ。 */
  schedule(): void {
    this.deps.clearTimer(this.timer);
    this.timer = this.deps.setTimer(() => void this.refresh(), EQ_DEBOUNCE_MS);
  }

  private async refresh(): Promise<void> {
    const target = this.deps.target();
    if (!target) return;
    const seq = ++this.seq;
    try {
      const wav = await this.deps.fetchEq(target);
      if (seq === this.seq) await this.deps.applyEq(wav); // 期间又发了新的请求：这份过时了，不装
    } catch (err) {
      this.deps.onError(err);
    }
  }

  /** 装上这个房间的混响（已经装着就什么也不做）。 */
  async ensureRoom(room: RoomName): Promise<void> {
    if (this.loaded === room) return;
    let wav = this.rooms.get(room);
    if (!wav) {
      wav = await this.deps.fetchRoom(room);
      this.rooms.set(room, wav);
    }
    await this.deps.applyRoom(wav.slice(0)); // 解码会拿走缓冲区的所有权，所以每次传副本，留着的那份才能再用
    this.loaded = room;
  }
}
