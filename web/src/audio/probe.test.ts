// 试听引擎探针：在假的 Web Audio 里（testing/fake-webaudio.ts）按固定脚本操作 AudioEngine，
// 把它做的全部音频图操作，以及真正的 worklet.ts / transport.ts / core.ts 的出声情况，和 probe.golden.json 逐项比对。
// 试听路径上“会不会爆音、切换顺不顺”没有别的自动化手段能看住，所以重构 audio/* 与 App 的播放路径时，这份快照不许变。
// 确实要改试听行为时才重新生成，并在提交说明里写明原因：ORBIT8D_UPDATE_PROBE=1 npx vitest run src/audio/probe.test.ts

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { CalibrationKey, Scene } from "../types";
import { AudioEngine, SAMPLE_RATE, STEM_NAMES } from "./engine";
import { buildRenderParams, type RenderParams } from "./params";
import { FakeAudioContext, drain, encodeAudio, installFakeWebAudio, num, registeredProcessors } from "./testing/fake-webaudio";

const shared = (name: string) => fileURLToPath(new URL(`../../../shared/golden/${name}`, import.meta.url));
const GOLDEN_FILE = fileURLToPath(new URL("./probe.golden.json", import.meta.url));
const UPDATE = process.env.ORBIT8D_UPDATE_PROBE === "1";

interface RenderCase {
  scene: Scene;
  bpm_norm: number;
  t_ref: number;
  calibration: Record<CalibrationKey, number>;
}
interface Contract {
  preview_files: Record<string, number[]>;
}
interface Step {
  do: string;
  log: string[];
  worklet: string;
  after: string;
}
type Golden = Record<string, Step[]>;

const renderCase = JSON.parse(readFileSync(shared("render_case.json"), "utf8")) as RenderCase;
const contract = JSON.parse(readFileSync(shared("contract.json"), "utf8")) as Contract;

// ---------- 固定的输入 ----------
const SONG_A = { frames: 66150, seed: 1 }; // 1.5 秒
const SONG_B = { frames: 44100, seed: 50 }; // 1 秒
const EXTRA_FRAMES: Record<string, number> = { vocals_hi: 5, other_sub: 37 }; // 各文件长度略有出入：引擎按最短的对齐

/** 确定的伪随机噪声（整数线性同余），幅度约 ±0.1。 */
function noise(seed: number, frames: number): Float32Array {
  const out = new Float32Array(frames);
  let state = (seed * 2654435761) >>> 0;
  for (let i = 0; i < frames; i++) {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    out[i] = (state / 2 ** 32 - 0.5) * 0.2;
  }
  return out;
}

function hrtf(): ArrayBuffer {
  const file = readFileSync(shared("render_grid.bin"));
  return file.buffer.slice(file.byteOffset, file.byteOffset + file.byteLength) as ArrayBuffer;
}

/** 后端给的 7 个试听文件（名字与声道数取自契约）。 */
function stems(song: { frames: number; seed: number }): Record<string, ArrayBuffer> {
  const files: Record<string, ArrayBuffer> = {};
  Object.entries(contract.preview_files).forEach(([name, channels], k) => {
    const frames = song.frames + (EXTRA_FRAMES[name] ?? 0);
    files[name] = encodeAudio(
      name,
      channels.map((_, c) => noise(song.seed + 10 * k + c, frames)),
    );
  });
  return files;
}

const original = (song: { frames: number; seed: number }) => encodeAudio("original", [noise(song.seed + 900, song.frames), noise(song.seed + 901, song.frames)]);
const brir = (room: string, seed: number) => encodeAudio(`brir-${room}`, [noise(seed, 2000), noise(seed + 1, 2000)]);
const eq = (n: number) => encodeAudio(`eq-${n}`, [noise(700 + n, 1025)]);

function params(song: { frames: number }, edit: (scene: Scene) => void = () => undefined): RenderParams {
  const scene = structuredClone(renderCase.scene);
  edit(scene);
  const timing = { bpmNorm: renderCase.bpm_norm, tRef: renderCase.t_ref, durationS: song.frames / SAMPLE_RATE };
  return buildRenderParams(scene, timing, renderCase.calibration, 1.25);
}

const quieter = (scene: Scene) => {
  scene.mix.drums.mute = true;
  scene.mix.vocals.gain_db = -6;
  scene.mix.other.reverb_send = 0.2;
  scene.rear_darken_db = 3;
};

// ---------- 一次会话：一个引擎 + 它的假 AudioContext ----------
class Session {
  readonly steps: Step[] = [];

  private constructor(
    readonly engine: AudioEngine,
    readonly ctx: FakeAudioContext,
  ) {}

  /** 只建引擎，建图的记录留给调用方取。 */
  static create(): Session {
    const engine = new AudioEngine();
    const ctx = FakeAudioContext.current;
    if (!ctx) throw new Error("The engine did not create an AudioContext");
    engine.onEnded = () => ctx.log("event: engine.onEnded()");
    return new Session(engine, ctx);
  }

  /** 已初始化、已载入歌 A（与 App.loadProject 同样的顺序），之前的记录丢掉。 */
  static async loaded(): Promise<Session> {
    const s = Session.create();
    await s.engine.init(hrtf());
    await s.engine.loadStems(stems(SONG_A));
    await s.engine.loadOriginal(original(SONG_A), 0.7);
    s.engine.setListen("8d");
    await s.engine.setRoom(brir("hall", 300));
    await s.engine.setEq(eq(1));
    s.engine.setParams(params(SONG_A), 0.8);
    await s.ctx.advance(0.01);
    s.ctx.take();
    s.ctx.takeStats();
    return s;
  }

  /** 做一步操作并记下它引起的全部音频图操作；操作抛错也记下来。 */
  async step(name: string, action: () => unknown): Promise<void> {
    let thrown = "";
    try {
      await action();
    } catch (err) {
      thrown = `, threw: ${err instanceof Error ? err.message : String(err)}`;
    }
    await drain();
    const e = this.engine;
    this.steps.push({
      do: name,
      log: this.ctx.take(),
      worklet: this.ctx.takeStats(),
      after: `time ${num(e.time)} / ${num(e.duration)}, playing ${e.playing}, listening ${e.listening}${thrown}`,
    });
  }

  advance(seconds: number): Promise<void> {
    return this.step(`advance ${seconds} s`, () => this.ctx.advance(seconds));
  }

  /** 发起一个要等时间推进才结束的操作（换 EQ），不等它；结束时记一条事件。 */
  start(name: string, action: () => Promise<unknown>): Promise<void> {
    return this.step(`${name} (not awaited)`, () => {
      void action().then(
        () => this.ctx.log(`event: ${name} resolved`),
        (err: unknown) => this.ctx.log(`event: ${name} rejected: ${err instanceof Error ? err.message : String(err)}`),
      );
    });
  }
}

// ---------- 固定的操作脚本 ----------
const SCENARIOS: Record<string, () => Promise<Step[]>> = {
  "build the graph and initialise the worklet": async () => {
    const s = Session.create();
    await s.step("new AudioEngine()", () => undefined);
    await s.step("init(hrtf)", () => s.engine.init(hrtf()));
    await s.step("levels()", () => s.ctx.log(`levels() -> ${s.engine.levels().map(num).join(", ")}`));
    return s.steps;
  },

  "load a song in the order App.loadProject uses": async () => {
    const s = Session.create();
    await s.engine.init(hrtf());
    s.ctx.take();
    await s.step("loadStems(7 preview files)", () => s.engine.loadStems(stems(SONG_A)));
    await s.step("loadOriginal(original, 0.7)", () => s.engine.loadOriginal(original(SONG_A), 0.7));
    await s.step('setListen("8d")', () => s.engine.setListen("8d"));
    await s.step("setRoom(hall)", () => s.engine.setRoom(brir("hall", 300)));
    await s.step("setEq(eq-1)", () => s.engine.setEq(eq(1)));
    await s.step("setParams(scene, 0.8)", () => s.engine.setParams(params(SONG_A), 0.8));
    await s.advance(0.05);
    return s.steps;
  },

  "play, pause and resume": async () => {
    const s = await Session.loaded();
    await s.step("play()", () => s.engine.play());
    await s.advance(0.3);
    await s.step("pause()", () => s.engine.pause());
    await s.advance(0.1);
    await s.step("play() again", () => s.engine.play());
    await s.advance(0.065);
    await s.step("pause() in the middle of the fade-in", () => s.engine.pause());
    await s.advance(0.1);
    await s.step("pause() while paused", () => s.engine.pause());
    await s.step("play()", () => s.engine.play());
    await s.step("play() while playing", () => s.engine.play());
    await s.advance(0.1);
    await s.step("pause()", () => s.engine.pause());
    await s.advance(0.05);
    return s.steps;
  },

  "seek while playing and while paused": async () => {
    const s = await Session.loaded();
    await s.step("play()", () => s.engine.play());
    await s.advance(0.2);
    await s.step("seek(0.9) while playing", () => s.engine.seek(0.9));
    await s.advance(0.2);
    await s.step("pause()", () => s.engine.pause());
    await s.step("seek(0.3) while paused", () => s.engine.seek(0.3));
    await s.advance(0.05);
    await s.step("play()", () => s.engine.play());
    await s.advance(0.1);
    await s.step("seek(-1) while playing", () => s.engine.seek(-1));
    await s.advance(0.1);
    await s.step("seek(99) while playing", () => s.engine.seek(99));
    await s.advance(0.1);
    await s.step("pause()", () => s.engine.pause());
    return s.steps;
  },

  "drag the playhead (App.scrubStart / scrub / scrubEnd)": async () => {
    const s = await Session.loaded();
    await s.step("play()", () => s.engine.play());
    await s.advance(0.25);
    await s.step("scrubStart while playing: pause()", () => s.engine.pause());
    await s.advance(0.3);
    await s.step("scrubEnd: seek(1.0) then play()", async () => {
      await s.engine.seek(1.0);
      await s.engine.play();
    });
    await s.advance(0.2);
    await s.step("pause()", () => s.engine.pause());
    await s.advance(0.05);
    await s.step("scrubEnd while paused: seek(0.5)", () => s.engine.seek(0.5));
    await s.advance(0.05);
    return s.steps;
  },

  "switch between 8D and the original": async () => {
    const s = await Session.loaded();
    await s.step("play()", () => s.engine.play());
    await s.advance(0.2);
    await s.step('setListen("original")', () => s.engine.setListen("original"));
    await s.advance(0.02);
    await s.step('setListen("8d") in the middle of the crossfade', () => s.engine.setListen("8d"));
    await s.advance(0.1);
    await s.step('setListen("original")', () => s.engine.setListen("original"));
    await s.advance(0.1);
    await s.step("pause()", () => s.engine.pause());
    await s.advance(0.05);
    await s.step('setListen("8d") while paused', () => s.engine.setListen("8d"));
    await s.step("play()", () => s.engine.play());
    await s.advance(0.1);
    await s.step("pause()", () => s.engine.pause());
    const bare = Session.create();
    await bare.engine.init(hrtf());
    bare.ctx.take();
    await bare.step('setListen("original") before an original is loaded', () => bare.engine.setListen("original"));
    return [...s.steps, ...bare.steps];
  },

  "change the room while playing": async () => {
    const s = await Session.loaded();
    await s.step("play()", () => s.engine.play());
    await s.advance(0.2);
    await s.step("setRoom(room)", () => s.engine.setRoom(brir("room", 400)));
    await s.advance(0.1);
    await s.step("setRoom(hall)", () => s.engine.setRoom(brir("hall", 300)));
    await s.advance(0.05);
    await s.step("pause()", () => s.engine.pause());
    return s.steps;
  },

  "update the EQ while playing": async () => {
    const s = await Session.loaded();
    await s.step("play()", () => s.engine.play());
    await s.advance(0.2);
    await s.start("setEq(eq-2)", () => s.engine.setEq(eq(2)));
    await s.advance(0.05);
    await s.start("setEq(eq-3) while eq-2 is still fading in", () => s.engine.setEq(eq(3)));
    await s.advance(0.2);
    await s.advance(0.2);
    await s.start("setEq(not audio)", () => s.engine.setEq(new ArrayBuffer(8)));
    await s.start("setEq(eq-1) after the failed one", () => s.engine.setEq(eq(1)));
    await s.advance(0.2);
    await s.step("pause()", () => s.engine.pause());
    return s.steps;
  },

  "change scene parameters while playing": async () => {
    const s = await Session.loaded();
    await s.step("play()", () => s.engine.play());
    await s.advance(0.2);
    await s.step("setParams(drums muted, vocals -6 dB, less reverb, 0.5)", () => s.engine.setParams(params(SONG_A, quieter), 0.5));
    await s.advance(0.1);
    await s.step("setParams(first scene, 0.8)", () => s.engine.setParams(params(SONG_A), 0.8));
    await s.advance(0.1);
    await s.step("pause()", () => s.engine.pause());
    await s.step("setParams(drums muted, ...) while paused", () => s.engine.setParams(params(SONG_A, quieter), 0.5));
    await s.step("play()", () => s.engine.play());
    await s.advance(0.1);
    await s.step("pause()", () => s.engine.pause());
    return s.steps;
  },

  "play to the end, then play again": async () => {
    const s = await Session.loaded();
    await s.step("seek(1.3)", () => s.engine.seek(1.3));
    await s.step("play()", () => s.engine.play());
    await s.advance(0.4);
    await s.step("play() after the song ended", () => s.engine.play());
    await s.advance(0.1);
    await s.step("pause()", () => s.engine.pause());
    return s.steps;
  },

  "load another song while playing": async () => {
    const s = await Session.loaded();
    await s.step("play()", () => s.engine.play());
    await s.advance(0.2);
    await s.step("loadStems(song B)", () => s.engine.loadStems(stems(SONG_B)));
    await s.step("loadOriginal(song B, 0.5)", () => s.engine.loadOriginal(original(SONG_B), 0.5));
    await s.step("setParams(song B, 0.6)", () => s.engine.setParams(params(SONG_B), 0.6));
    await s.advance(0.05);
    await s.step("play()", () => s.engine.play());
    await s.advance(0.2);
    await s.step("pause()", () => s.engine.pause());
    return s.steps;
  },

  "calls before the engine is ready": async () => {
    const s = Session.create();
    s.ctx.take();
    await s.step("setParams before init", () => s.engine.setParams(params(SONG_A), 0.8));
    await s.step("play() before init", () => s.engine.play());
    await s.step("pause() before init", () => s.engine.pause());
    await s.step("init(hrtf)", () => s.engine.init(hrtf()));
    await s.step("play() without a song", () => s.engine.play());
    await s.step("seek(1) without a song", () => s.engine.seek(1));
    return s.steps;
  },
};

describe("试听引擎探针：音频图操作与 Worklet 出声情况", () => {
  const golden: Golden = existsSync(GOLDEN_FILE) ? (JSON.parse(readFileSync(GOLDEN_FILE, "utf8")) as Golden) : {};
  const actual: Golden = {};
  let uninstall: () => void = () => undefined;

  beforeAll(async () => {
    uninstall = installFakeWebAudio(SAMPLE_RATE);
    await import("./worklet"); // 在假的 AudioWorkletGlobalScope 里加载：它会 registerProcessor
  });

  afterAll(() => {
    uninstall();
    if (UPDATE) writeFileSync(GOLDEN_FILE, `${JSON.stringify(actual, null, 1)}\n`);
  });

  it("Worklet 模块注册了一个处理器，试听文件名与契约一致", () => {
    expect(registeredProcessors()).toHaveLength(1);
    expect([...STEM_NAMES].sort()).toEqual(Object.keys(contract.preview_files).sort());
  });

  it("快照里没有多余的脚本", () => {
    if (!UPDATE) expect(Object.keys(golden).sort()).toEqual(Object.keys(SCENARIOS).sort());
  });

  for (const [name, run] of Object.entries(SCENARIOS)) {
    it(name, async () => {
      actual[name] = await run();
      if (!UPDATE) expect(actual[name]).toEqual(golden[name]);
    });
  }
});
