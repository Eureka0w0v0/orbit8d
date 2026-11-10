import { describe, expect, it } from "vitest";
import type { RoomName, Scene } from "../types";
import { EQ_DEBOUNCE_MS, EqSync, type EqTarget } from "./eq-sync";

interface Pending<T> {
  resolve: (value: T) => void;
  reject: (err: unknown) => void;
}

/** 假的定时器、接口与引擎：时间由测试推进，每个请求什么时候回来由测试决定。 */
function harness() {
  let now = 0;
  let nextId = 0;
  let timers: Array<{ id: number; due: number; fn: () => void }> = [];
  const state = { target: { projectId: "song-a", scene: scene("1") } as EqTarget | null };
  const eqRequests: Array<Pending<ArrayBuffer> & { target: EqTarget }> = [];
  const roomRequests: Array<Pending<ArrayBuffer> & { room: RoomName }> = [];
  const appliedEq: ArrayBuffer[] = [];
  const appliedRooms: ArrayBuffer[] = [];
  const errors: unknown[] = [];
  let failNextEqApply = false;
  const sync = new EqSync({
    target: () => state.target,
    fetchEq: (target) => new Promise((resolve, reject) => eqRequests.push({ target, resolve, reject })),
    applyEq: async (wav) => {
      if (failNextEqApply) {
        failNextEqApply = false;
        throw new Error("decode failed");
      }
      appliedEq.push(wav);
    },
    fetchRoom: (room) => new Promise((resolve, reject) => roomRequests.push({ room, resolve, reject })),
    applyRoom: async (wav) => {
      appliedRooms.push(wav);
      structuredClone(wav, { transfer: [wav] }); // 和 decodeAudioData 一样：拿走缓冲区，调用方手里的变成空的
    },
    onError: (err) => errors.push(err),
    setTimer: (fn, ms) => {
      timers.push({ id: ++nextId, due: now + ms, fn });
      return nextId;
    },
    clearTimer: (id) => {
      timers = timers.filter((t) => t.id !== id);
    },
  });
  const settle = () => new Promise<void>((resolve) => setTimeout(resolve, 0));
  const advance = async (ms: number) => {
    now += ms;
    for (const timer of timers.filter((t) => t.due <= now).sort((a, b) => a.due - b.due)) {
      timers = timers.filter((t) => t.id !== timer.id);
      timer.fn();
    }
    await settle();
  };
  return { sync, state, eqRequests, roomRequests, appliedEq, appliedRooms, errors, advance, settle, failEqApply: () => (failNextEqApply = true) };
}

function scene(marker: string): Scene {
  return { marker } as unknown as Scene;
}
const wav = (byte: number, length = 4) => new Uint8Array(length).fill(byte).buffer;
const markerOf = (target: EqTarget) => (target.scene as unknown as { marker: string }).marker;

describe("补偿 EQ 跟着场景更新", () => {
  it("场景改完 250 ms 没再改才去取；连着改只取一次，取的是当时最新的场景", async () => {
    const h = harness();
    h.sync.schedule();
    await h.advance(EQ_DEBOUNCE_MS - 1);
    h.state.target = { projectId: "song-a", scene: scene("2") };
    h.sync.schedule();
    await h.advance(EQ_DEBOUNCE_MS - 1);
    expect(h.eqRequests).toHaveLength(0);
    await h.advance(1);
    expect(h.eqRequests.map((r) => markerOf(r.target))).toEqual(["2"]);
    const eq = wav(1);
    h.eqRequests[0].resolve(eq);
    await h.settle();
    expect(h.appliedEq).toEqual([eq]);
  });

  it("只用最后一次请求的结果：先发的后回来也不装", async () => {
    const h = harness();
    h.sync.schedule();
    await h.advance(EQ_DEBOUNCE_MS);
    h.state.target = { projectId: "song-a", scene: scene("2") };
    h.sync.schedule();
    await h.advance(EQ_DEBOUNCE_MS);
    expect(h.eqRequests).toHaveLength(2);
    const newer = wav(2);
    h.eqRequests[1].resolve(newer);
    await h.settle();
    h.eqRequests[0].resolve(wav(1));
    await h.settle();
    expect(h.appliedEq).toEqual([newer]);
  });

  it("没有打开歌时到点什么也不取", async () => {
    const h = harness();
    h.state.target = null;
    h.sync.schedule();
    await h.advance(EQ_DEBOUNCE_MS);
    expect(h.eqRequests).toHaveLength(0);
  });

  it("取 EQ 失败或装不上：报告一下，原来的 EQ 留着，下次照常更新", async () => {
    const h = harness();
    h.sync.schedule();
    await h.advance(EQ_DEBOUNCE_MS);
    h.eqRequests[0].reject(new Error("offline"));
    await h.settle();
    h.sync.schedule();
    await h.advance(EQ_DEBOUNCE_MS);
    h.failEqApply();
    h.eqRequests[1].resolve(wav(2));
    await h.settle();
    expect(h.errors).toHaveLength(2);
    expect(h.appliedEq).toEqual([]);
    h.sync.schedule();
    await h.advance(EQ_DEBOUNCE_MS);
    const eq = wav(3);
    h.eqRequests[2].resolve(eq);
    await h.settle();
    expect(h.appliedEq).toEqual([eq]);
  });
});

describe("房间混响", () => {
  it("第一次要某个房间：下载、装上；已经装着就什么也不做", async () => {
    const h = harness();
    expect(h.sync.room).toBeNull();
    const first = h.sync.ensureRoom("hall");
    expect(h.roomRequests.map((r) => r.room)).toEqual(["hall"]);
    h.roomRequests[0].resolve(wav(7));
    await first;
    expect(h.sync.room).toBe("hall");
    await h.sync.ensureRoom("hall");
    expect(h.roomRequests).toHaveLength(1);
    expect(h.appliedRooms).toHaveLength(1);
  });

  it("换回用过的房间不再下载；每次交给引擎的都是副本（引擎解码会把缓冲区拿走）", async () => {
    const h = harness();
    const hall = h.sync.ensureRoom("hall");
    h.roomRequests[0].resolve(wav(7, 8));
    await hall;
    const room = h.sync.ensureRoom("room");
    h.roomRequests[1].resolve(wav(9, 8));
    await room;
    await h.sync.ensureRoom("hall");
    expect(h.roomRequests.map((r) => r.room)).toEqual(["hall", "room"]);
    expect(h.sync.room).toBe("hall");
    expect(h.appliedRooms).toHaveLength(3);
    expect(h.appliedRooms.every((buffer) => buffer.byteLength === 0)).toBe(true); // 都被“解码”拿走了，而第三次仍然装得上
  });

  it("装的过程中出错：房间不算换成，错误交给调用方", async () => {
    const h = harness();
    const attempt = h.sync.ensureRoom("church");
    h.roomRequests[0].reject(new Error("offline"));
    await expect(attempt).rejects.toThrow("offline");
    expect(h.sync.room).toBeNull();
  });
});
