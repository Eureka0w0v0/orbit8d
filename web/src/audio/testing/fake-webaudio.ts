// 假的 Web Audio（只给试听引擎的探针测试用，见 ../probe.test.ts）：
// 1) 把引擎做的每一个音频图操作按顺序记下来：建节点、connect / disconnect、AudioParam 的每一次调度、
//    声源的 start / stop、发给 AudioWorklet 的消息；时间一律记成“相对当前时刻”。
// 2) 只渲染到 Worklet 为止（缓冲声源 → 淡入淡出增益 → Worklet 输入），让真正的 worklet.ts / transport.ts / core.ts
//    在假的 AudioWorkletGlobalScope 里逐量子运行，记下它什么时候出声、输出的能量与校验和。
//    卷积器、EQ、限幅、电平表不渲染，只记录对它们的操作。
// 时间由测试推进（advance），一次一个渲染量子；window.setTimeout 也挂在这只钟上，所以结果完全确定。

export const QUANTUM = 128;
const SIGNIFICANT = 6; // 浮点量保留的有效位数：重构出错远大于这个量级，不同 JS 引擎的末位差异远小于它

type Handler = (() => void) | null;
type ParamEvent =
  | { kind: "set"; time: number; value: number }
  | { kind: "ramp"; time: number; value: number }
  | { kind: "target"; time: number; value: number; tau: number };

export interface ProcessorLike {
  process(inputs: Float32Array[][], outputs: Float32Array[][]): boolean;
}
interface ProcessorPort {
  onmessage: ((event: { data: unknown }) => void) | null;
}
type ProcessorCtor = new () => ProcessorLike & { port: ProcessorPort };

/** 数字的稳定写法：保留 6 位有效数字，-0 记成 0。 */
export function num(v: number): string {
  if (!Number.isFinite(v)) return String(v);
  const rounded = Number(v.toPrecision(SIGNIFICANT));
  return String(rounded === 0 ? 0 : rounded);
}

/** 确定的伪随机权重（整数运算，不依赖 Math.random / 三角函数）。 */
export function weight(k: number): number {
  return (Math.imul(k + 1, 2654435761) >>> 0) / 2 ** 32 - 0.5;
}

// ---------- AudioWorkletGlobalScope ----------
const registry = new Map<string, ProcessorCtor>();
let nextPort: ProcessorPort | null = null;

class FakeAudioWorkletProcessor {
  readonly port: ProcessorPort;
  constructor() {
    if (!nextPort) throw new Error("AudioWorkletProcessor can only be constructed by an AudioWorkletNode");
    this.port = nextPort;
    nextPort = null;
  }
}

/** Worklet 输出的统计：从上一次 take() 到现在。 */
export class WorkletStats {
  quanta = 0;
  active = 0;
  first: number | null = null; // 第一个出声的量子相对统计起点的帧号
  last: number | null = null;
  private readonly energy = [0, 0, 0]; // 左、右、混响送出
  private check = 0;
  private frames = 0;

  add(outL: Float32Array, outR: Float32Array, send: Float32Array): void {
    let any = false;
    [outL, outR, send].forEach((ch, c) => {
      for (let i = 0; i < ch.length; i++) {
        const v = ch[i];
        if (v === 0) continue;
        any = true;
        this.energy[c] += v * v;
        this.check += v * weight((this.frames + i) * 3 + c);
      }
    });
    if (any) {
      this.active++;
      this.first ??= this.frames;
      this.last = this.frames;
    }
    this.quanta++;
    this.frames += outL.length;
  }

  summary(): string {
    if (this.quanta === 0) return "no quanta";
    if (this.active === 0) return `${this.quanta} quanta, silent`;
    const energy = this.energy.map(num).join(", ");
    return `${this.quanta} quanta, ${this.active} audible (frames +${this.first}..+${this.last}), energy L/R/send = ${energy}, checksum ${num(this.check)}`;
  }
}

// ---------- 时钟与日志 ----------
interface Timer {
  id: number;
  due: number;
  fn: () => void;
}

export class FakeAudioContext {
  static current: FakeAudioContext | null = null;

  readonly sampleRate: number;
  readonly destination: FakeNode;
  readonly audioWorklet = {
    addModule: (url: string): Promise<void> => {
      if (typeof url !== "string" || url === "") throw new Error("addModule needs the worklet module URL");
      this.log("audioWorklet.addModule(<worklet module>)");
      return Promise.resolve();
    },
  };
  state: "suspended" | "running" = "suspended";
  frame = 0;
  wall = 0; // 墙上时钟（秒）：window.setTimeout 用它，暂停着的 AudioContext 不走但它照走
  stats = new WorkletStats();
  private lines: string[] = [];
  private readonly counters = new Map<string, number>();
  private readonly params: FakeParam[] = [];
  private readonly sources: FakeSource[] = [];
  private readonly worklets: FakeWorkletNode[] = [];
  private timers: Timer[] = [];
  private timerId = 0;
  private callbacksRan = false;

  constructor(options: { sampleRate: number; latencyHint?: string }) {
    this.sampleRate = options.sampleRate;
    this.destination = new FakeNode(this, "destination");
    this.log(`new AudioContext(${JSON.stringify(options)})`);
    FakeAudioContext.current = this;
  }

  get currentTime(): number {
    return this.frame / this.sampleRate;
  }

  // ---- 记录 ----
  log(line: string): void {
    this.lines.push(line);
  }

  /** 取走到目前为止的记录。 */
  take(): string[] {
    const out = this.lines;
    this.lines = [];
    return out;
  }

  takeStats(): string {
    const out = this.stats.summary();
    this.stats = new WorkletStats();
    return out;
  }

  label(kind: string): string {
    const n = (this.counters.get(kind) ?? 0) + 1;
    this.counters.set(kind, n);
    return `${kind}${n}`;
  }

  /** 相对当前时刻的写法：+0.06。 */
  rel(t: number): string {
    const d = Number((t - this.currentTime).toFixed(6));
    return `${d < 0 ? "-" : "+"}${Math.abs(d)}`;
  }

  relFrame(frame: number): string {
    const d = frame - this.frame;
    return `${d < 0 ? "-" : "+"}${Math.abs(d)}`;
  }

  registerParam(param: FakeParam): void {
    this.params.push(param);
  }

  registerSource(source: FakeSource): void {
    if (!this.sources.includes(source)) this.sources.push(source);
  }

  registerWorklet(node: FakeWorkletNode): void {
    this.worklets.push(node);
  }

  // ---- AudioContext 接口 ----
  resume(): Promise<void> {
    this.log("ctx.resume()");
    this.state = "running";
    return Promise.resolve();
  }

  createGain(): FakeGain {
    return new FakeGain(this);
  }

  createConvolver(): FakeConvolver {
    return new FakeConvolver(this);
  }

  createDynamicsCompressor(): FakeCompressor {
    return new FakeCompressor(this);
  }

  createChannelSplitter(outputs: number): FakeNode {
    const node = new FakeNode(this, this.label("splitter"));
    this.log(`ctx.createChannelSplitter(${outputs}) -> ${node.label}`);
    return node;
  }

  createAnalyser(): FakeAnalyser {
    return new FakeAnalyser(this);
  }

  createBufferSource(): FakeSource {
    return new FakeSource(this);
  }

  createBuffer(channels: number, length: number, sampleRate: number): FakeBuffer {
    const buffer = new FakeBuffer(this, channels, length, sampleRate, "");
    this.log(`ctx.createBuffer(${channels}, ${length}, ${sampleRate}) -> ${buffer.label}`);
    return buffer;
  }

  /** 解码 encodeAudio() 造的假音频文件；和真的一样会把传入的 ArrayBuffer 转移走（之后不能再用）。 */
  decodeAudioData(data: ArrayBuffer): Promise<FakeBuffer> {
    let decoded: { tag: string; channels: Float32Array[] };
    try {
      decoded = decodeAudio(data);
    } catch (err) {
      this.log(`ctx.decodeAudioData(<${data.byteLength} bytes>) -> rejected`);
      return Promise.reject(err);
    }
    structuredClone(data, { transfer: [data] }); // 转移所有权：调用方手里的 ArrayBuffer 变成空的
    const buffer = new FakeBuffer(this, decoded.channels.length, decoded.channels[0].length, this.sampleRate, decoded.tag);
    decoded.channels.forEach((ch, c) => buffer.getChannelData(c).set(ch));
    this.log(`ctx.decodeAudioData(<${decoded.tag}>) -> ${buffer.describe()}`);
    return Promise.resolve(buffer);
  }

  // ---- window.setTimeout / clearTimeout ----
  setTimeout(fn: () => void, ms = 0): number {
    const id = ++this.timerId;
    this.timers.push({ id, due: this.wall + ms / 1000, fn });
    this.log(`window.setTimeout(${num(ms)} ms)`);
    return id;
  }

  clearTimeout(id: number): void {
    this.timers = this.timers.filter((t) => t.id !== id);
  }

  // ---- 推进时间 ----
  /** 推进 seconds 秒（取整到渲染量子）。AudioContext 在运行时逐量子渲染；到点的定时器与 ended 事件按顺序触发。 */
  async advance(seconds: number): Promise<void> {
    await drain();
    const quanta = Math.round((seconds * this.sampleRate) / QUANTUM);
    for (let q = 0; q < quanta; q++) {
      this.callbacksRan = false;
      for (const node of this.worklets) node.deliver();
      if (this.state === "running") this.renderQuantum();
      this.wall += QUANTUM / this.sampleRate;
      this.fireTimers();
      if (this.callbacksRan) await drain();
    }
    for (const node of this.worklets) node.deliver();
  }

  private renderQuantum(): void {
    for (const node of this.worklets) node.render(this.frame);
    this.frame += QUANTUM;
    for (const param of this.params) param.render(this.currentTime);
    for (const source of [...this.sources]) {
      if (!source.finishedBy(this.frame)) continue;
      this.sources.splice(this.sources.indexOf(source), 1);
      this.callbacksRan = true;
      source.fireEnded();
    }
  }

  private fireTimers(): void {
    const due = this.timers.filter((t) => t.due <= this.wall + 1e-9).sort((a, b) => a.due - b.due || a.id - b.id);
    for (const timer of due) {
      this.timers = this.timers.filter((t) => t.id !== timer.id);
      this.callbacksRan = true;
      this.log(`event: timer fired`);
      timer.fn();
    }
  }
}

/** 让挂起的 Promise 回调全部跑完（用真正的定时器让出一轮事件循环；假的只装在 window 上）。 */
export function drain(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

// ---------- AudioParam ----------
export class FakeParam {
  private events: ParamEvent[] = [];
  private rendered: number;

  constructor(
    private readonly ctx: FakeAudioContext,
    readonly label: string,
    private readonly initial: number,
  ) {
    this.rendered = initial;
    ctx.registerParam(this);
  }

  /** 和浏览器一样：读到的是最近一个渲染量子算出的值，不会因为刚取消了调度就跳变。 */
  get value(): number {
    return this.rendered;
  }

  set value(v: number) {
    this.ctx.log(`${this.label}.value = ${num(v)}`);
    this.insert({ kind: "set", time: this.ctx.currentTime, value: v });
    this.rendered = v;
  }

  setValueAtTime(v: number, time: number): this {
    this.ctx.log(`${this.label}.setValueAtTime(${num(v)}, ${this.ctx.rel(time)})`);
    this.insert({ kind: "set", time, value: v });
    return this;
  }

  linearRampToValueAtTime(v: number, time: number): this {
    this.ctx.log(`${this.label}.linearRampToValueAtTime(${num(v)}, ${this.ctx.rel(time)})`);
    this.insert({ kind: "ramp", time, value: v });
    return this;
  }

  setTargetAtTime(target: number, time: number, tau: number): this {
    this.ctx.log(`${this.label}.setTargetAtTime(${num(target)}, ${this.ctx.rel(time)}, ${num(tau)})`);
    this.insert({ kind: "target", time, value: target, tau });
    return this;
  }

  cancelScheduledValues(time: number): this {
    this.ctx.log(`${this.label}.cancelScheduledValues(${this.ctx.rel(time)})`);
    this.events = this.events.filter((e) => e.time < time);
    return this;
  }

  private insert(event: ParamEvent): void {
    const at = this.events.findIndex((e) => e.time > event.time);
    if (at < 0) this.events.push(event);
    else this.events.splice(at, 0, event);
  }

  valueAt(t: number): number {
    let value = this.initial;
    let from = 0;
    for (let i = 0; i < this.events.length; i++) {
      const e = this.events[i];
      if (e.kind === "ramp") {
        if (t >= e.time || e.time <= from) {
          value = e.value;
          from = e.time;
          continue;
        }
        return value + ((e.value - value) * Math.max(0, t - from)) / (e.time - from);
      }
      if (e.time > t) break;
      if (e.kind === "set") {
        value = e.value;
        from = e.time;
        continue;
      }
      const next = this.events[i + 1];
      const until = next && next.time < t ? next.time : t;
      value = e.value + (value - e.value) * Math.exp(-(until - e.time) / e.tau);
      from = until;
    }
    return value;
  }

  render(t: number): void {
    this.rendered = this.valueAt(t);
  }
}

// ---------- 节点 ----------
export class FakeNode {
  readonly inputs = new Set<FakeNode>();
  private readonly targets = new Set<FakeNode>();

  constructor(
    readonly ctx: FakeAudioContext,
    readonly label: string,
  ) {}

  connect(dest: FakeNode, ...indexes: number[]): FakeNode {
    this.ctx.log(`${this.label}.connect(${[dest.label, ...indexes].join(", ")})`);
    this.targets.add(dest);
    dest.inputs.add(this);
    return dest;
  }

  disconnect(): void {
    this.ctx.log(`${this.label}.disconnect()`);
    for (const dest of this.targets) dest.inputs.delete(this);
    this.targets.clear();
  }

  /** 这个节点在 [frame, frame + QUANTUM) 的输出；没有在出声时返回 null。只有声源和增益会出声。 */
  output(_frame: number): Float32Array[] | null {
    return null;
  }
}

function mix(parts: Float32Array[][]): Float32Array[] {
  const channels = Math.max(...parts.map((p) => p.length));
  const out = Array.from({ length: channels }, () => new Float32Array(QUANTUM));
  for (const part of parts) part.forEach((ch, c) => ch.forEach((v, i) => (out[c][i] += v)));
  return out;
}

export class FakeGain extends FakeNode {
  readonly gain: FakeParam;

  constructor(ctx: FakeAudioContext) {
    super(ctx, ctx.label("gain"));
    ctx.log(`ctx.createGain() -> ${this.label}`);
    this.gain = new FakeParam(ctx, `${this.label}.gain`, 1);
  }

  override output(frame: number): Float32Array[] | null {
    const parts = [...this.inputs].map((n) => n.output(frame)).filter((p): p is Float32Array[] => p !== null);
    if (!parts.length) return null;
    const out = mix(parts);
    for (let i = 0; i < QUANTUM; i++) {
      const g = this.gain.valueAt((frame + i) / this.ctx.sampleRate);
      for (const ch of out) ch[i] *= g;
    }
    return out;
  }
}

export class FakeConvolver extends FakeNode {
  private flag = true;
  private data: FakeBuffer | null = null;

  constructor(ctx: FakeAudioContext) {
    super(ctx, ctx.label("convolver"));
    ctx.log(`ctx.createConvolver() -> ${this.label}`);
  }

  get normalize(): boolean {
    return this.flag;
  }

  set normalize(v: boolean) {
    this.ctx.log(`${this.label}.normalize = ${v}`);
    this.flag = v;
  }

  get buffer(): FakeBuffer | null {
    return this.data;
  }

  set buffer(b: FakeBuffer | null) {
    this.ctx.log(`${this.label}.buffer = ${b ? b.describe() : "null"}`);
    this.data = b;
  }
}

export class FakeCompressor extends FakeNode {
  readonly threshold: FakeParam;
  readonly knee: FakeParam;
  readonly ratio: FakeParam;
  readonly attack: FakeParam;
  readonly release: FakeParam;

  constructor(ctx: FakeAudioContext) {
    super(ctx, ctx.label("compressor"));
    ctx.log(`ctx.createDynamicsCompressor() -> ${this.label}`);
    this.threshold = new FakeParam(ctx, `${this.label}.threshold`, -24);
    this.knee = new FakeParam(ctx, `${this.label}.knee`, 30);
    this.ratio = new FakeParam(ctx, `${this.label}.ratio`, 12);
    this.attack = new FakeParam(ctx, `${this.label}.attack`, 0.003);
    this.release = new FakeParam(ctx, `${this.label}.release`, 0.25);
  }
}

export class FakeAnalyser extends FakeNode {
  private size = 2048;
  private readonly level: number;

  constructor(ctx: FakeAudioContext) {
    super(ctx, ctx.label("analyser"));
    ctx.log(`ctx.createAnalyser() -> ${this.label}`);
    this.level = 0.05 * Number(this.label.slice("analyser".length));
  }

  get fftSize(): number {
    return this.size;
  }

  set fftSize(v: number) {
    this.ctx.log(`${this.label}.fftSize = ${v}`);
    this.size = v;
  }

  /** 电平表不渲染：给一个固定电平的方波（两个表各不相同），只为确认引擎读的是哪个表、怎么换算。 */
  getFloatTimeDomainData(out: Float32Array): void {
    this.ctx.log(`${this.label}.getFloatTimeDomainData(<${out.length}>)`);
    for (let i = 0; i < out.length; i++) out[i] = i % 2 ? -this.level : this.level;
  }
}

export class FakeBuffer {
  readonly label: string;
  readonly duration: number;
  private readonly channels: Float32Array[];

  constructor(
    private readonly ctx: FakeAudioContext,
    readonly numberOfChannels: number,
    readonly length: number,
    readonly sampleRate: number,
    readonly tag: string,
  ) {
    this.label = ctx.label("buffer");
    this.duration = length / sampleRate;
    this.channels = Array.from({ length: numberOfChannels }, () => new Float32Array(length));
    this.channels.forEach((ch, c) => channelNames.set(ch.buffer, `${this.label}.ch${c}`));
  }

  describe(): string {
    return `${this.label}${this.tag ? `<${this.tag}>` : ""}(${this.numberOfChannels} ch, ${this.length} frames)`;
  }

  getChannelData(channel: number): Float32Array {
    return this.channels[channel];
  }

  copyToChannel(source: Float32Array, channel: number, start = 0): void {
    const origin = channelNames.get(source.buffer) ?? "<array>";
    const from = source.byteOffset / Float32Array.BYTES_PER_ELEMENT;
    this.ctx.log(`${this.label}.copyToChannel(${origin}[${from}..${from + source.length}), ${[channel, ...(start ? [start] : [])].join(", ")})`);
    this.channels[channel].set(source, start);
  }
}

const channelNames = new WeakMap<ArrayBufferLike, string>();

export class FakeSource extends FakeNode {
  private data: FakeBuffer | null = null;
  private handler: Handler = null;
  private startFrame: number | null = null;
  private offset = 0;
  private stopFrame = Number.POSITIVE_INFINITY;

  constructor(ctx: FakeAudioContext) {
    super(ctx, ctx.label("source"));
    ctx.log(`ctx.createBufferSource() -> ${this.label}`);
  }

  get buffer(): FakeBuffer | null {
    return this.data;
  }

  set buffer(b: FakeBuffer | null) {
    this.ctx.log(`${this.label}.buffer = ${b ? b.describe() : "null"}`);
    this.data = b;
  }

  get onended(): Handler {
    return this.handler;
  }

  set onended(fn: Handler) {
    this.ctx.log(`${this.label}.onended = ${fn ? "<handler>" : "null"}`);
    this.handler = fn;
  }

  start(when = 0, offset = 0): void {
    this.ctx.log(`${this.label}.start(${this.ctx.rel(when)}, ${num(offset)})`);
    if (this.startFrame !== null) throw new Error("InvalidStateError: start() was already called");
    this.startFrame = Math.round(when * this.ctx.sampleRate);
    this.offset = Math.round(offset * this.ctx.sampleRate);
    this.ctx.registerSource(this);
  }

  stop(when = 0): void {
    this.ctx.log(`${this.label}.stop(${this.ctx.rel(when)})`);
    if (this.startFrame === null) throw new Error("InvalidStateError: stop() before start()");
    this.stopFrame = Math.round(when * this.ctx.sampleRate);
  }

  private get endFrame(): number {
    if (this.startFrame === null || !this.data) return Number.POSITIVE_INFINITY;
    return Math.min(this.stopFrame, this.startFrame + Math.max(0, this.data.length - this.offset));
  }

  finishedBy(frame: number): boolean {
    return frame >= this.endFrame;
  }

  fireEnded(): void {
    this.ctx.log(`event: ${this.label} ended`);
    this.handler?.();
  }

  override output(frame: number): Float32Array[] | null {
    if (this.startFrame === null || !this.data || frame + QUANTUM <= this.startFrame || frame >= this.endFrame) return null;
    const out = Array.from({ length: this.data.numberOfChannels }, () => new Float32Array(QUANTUM));
    const first = Math.max(frame, this.startFrame);
    const last = Math.min(frame + QUANTUM, this.endFrame);
    out.forEach((ch, c) => {
      const src = this.data!.getChannelData(c);
      for (let f = first; f < last; f++) ch[f - frame] = src[f - this.startFrame! + this.offset];
    });
    return out;
  }
}

// ---------- AudioWorkletNode ----------
interface WorkletOptions {
  numberOfInputs?: number;
  numberOfOutputs?: number;
  outputChannelCount?: number[];
  channelCount?: number;
  channelCountMode?: string;
  channelInterpretation?: string;
}

export class FakeWorkletNode extends FakeNode {
  readonly port: { postMessage: (message: unknown) => void };
  private readonly processor: ProcessorLike & { port: ProcessorPort };
  private readonly queue: unknown[] = [];
  private readonly channelCount: number;
  private readonly outputChannels: number[];

  constructor(ctx: FakeAudioContext, name: string, options: WorkletOptions = {}) {
    super(ctx, ctx.label("worklet"));
    ctx.log(`new AudioWorkletNode(${JSON.stringify(name)}, ${JSON.stringify(options)}) -> ${this.label}`);
    const ctor = registry.get(name);
    if (!ctor) throw new Error(`InvalidStateError: no AudioWorkletProcessor is registered as "${name}"`);
    nextPort = { onmessage: null };
    this.processor = new ctor();
    this.channelCount = options.channelCount ?? 2;
    this.outputChannels = options.outputChannelCount ?? [2];
    this.port = {
      postMessage: (message: unknown) => {
        ctx.log(`${this.label}.port.postMessage(${describeMessage(ctx, message)})`);
        this.queue.push(structuredClone(message)); // 和真的一样做结构化克隆：消息里只能有纯数据
      },
    };
    ctx.registerWorklet(this);
  }

  /** 把排队的消息交给处理器（真实环境里消息在某个之后的渲染量子之前到达）。 */
  deliver(): void {
    for (const data of this.queue.splice(0)) this.processor.port.onmessage?.({ data });
  }

  render(frame: number): void {
    const parts = [...this.inputs].map((n) => n.output(frame)).filter((p): p is Float32Array[] => p !== null);
    // channelCountMode = "explicit" + "discrete"：有输入在出声时固定 channelCount 路，多的丢掉、少的补零；没有输入时是 0 路
    const mixed = parts.length ? mix(parts) : [];
    const input = parts.length ? Array.from({ length: this.channelCount }, (_, c) => mixed[c] ?? new Float32Array(QUANTUM)) : [];
    const outputs = this.outputChannels.map((n) => Array.from({ length: n }, () => new Float32Array(QUANTUM)));
    const scope = globalThis as unknown as { currentFrame: number; sampleRate: number };
    scope.currentFrame = frame;
    scope.sampleRate = this.ctx.sampleRate;
    this.processor.process([input], outputs);
    this.ctx.stats.add(outputs[0][0], outputs[0][1], outputs[1][0]);
  }
}

function checksum(values: ArrayLike<number>): number {
  let sum = 0;
  for (let i = 0; i < values.length; i++) sum += values[i] * weight(i);
  return sum;
}

/** 消息里所有数值（含嵌套的 TypedArray）的个数与校验和：内容变了就会变。 */
function digest(value: unknown): { count: number; sum: number } {
  const out = { count: 0, sum: 0 };
  const visit = (v: unknown): void => {
    if (typeof v === "number") {
      out.sum += v * weight(out.count);
      out.count++;
    } else if (ArrayBuffer.isView(v)) {
      const arr = v as unknown as ArrayLike<number>;
      for (let i = 0; i < arr.length; i++) visit(arr[i]);
    } else if (Array.isArray(v)) v.forEach(visit);
    else if (typeof v === "string") for (const ch of v) visit(ch.codePointAt(0));
    else if (v && typeof v === "object") for (const key of Object.keys(v).sort()) visit((v as Record<string, unknown>)[key]);
  };
  visit(value);
  return out;
}

interface SourceLike {
  track: string;
  channel: number;
  offsetDeg: number;
  gain: number;
  send: number;
}

function describeMessage(ctx: FakeAudioContext, message: unknown): string {
  const msg = message as { type: string } & Record<string, unknown>;
  if (msg.type === "start") return `start: startFrame ${ctx.relFrame(msg.startFrame as number)}, songOffset ${num(msg.songOffset as number)}`;
  if (msg.type === "stop") return `stop: stopFrame ${ctx.relFrame(msg.stopFrame as number)}`;
  if (msg.type === "init") {
    const t = msg.table as { sampleRate: number; taps: number; nAz: number; elNodes: ArrayLike<number>; data: ArrayLike<number> };
    return `init: table ${t.sampleRate} Hz, ${t.taps} taps, ${t.nAz} az x ${t.elNodes.length} el, ${t.data.length} values, checksum ${num(checksum(t.data))}`;
  }
  if (msg.type === "params") {
    const p = msg.params as { sources: SourceLike[]; subGains: number[]; rearCut: number; motions: unknown; wet: unknown };
    const sources = p.sources.map((s) => `${s.track}@${s.channel} offset ${num(s.offsetDeg)} gain ${num(s.gain)} send ${num(s.send)}`).join("; ");
    const motions = digest(p.motions);
    const wet = digest(p.wet);
    return `params: sources [${sources}], subGains [${p.subGains.map(num).join(", ")}], rearCut ${num(p.rearCut)}, motions ${motions.count} values checksum ${num(motions.sum)}, wet ${wet.count} values checksum ${num(wet.sum)}`;
  }
  return `${msg.type}: ${JSON.stringify(digest(message))}`;
}

// ---------- 假的音频文件 ----------
const MAGIC = 0x4f384441; // "O8DA"
const TAG_BYTES = 16;
const HEADER_BYTES = 12 + TAG_BYTES;

/** 造一个 decodeAudioData 认得的“文件”：头部（魔数、声道数、帧数、名字）+ 各声道 float32。 */
export function encodeAudio(tag: string, channels: Float32Array[]): ArrayBuffer {
  const frames = channels[0].length;
  const out = new ArrayBuffer(HEADER_BYTES + channels.length * frames * 4);
  const view = new DataView(out);
  view.setUint32(0, MAGIC);
  view.setUint32(4, channels.length);
  view.setUint32(8, frames);
  new Uint8Array(out, 12, TAG_BYTES).set(new TextEncoder().encode(tag).subarray(0, TAG_BYTES));
  channels.forEach((ch, c) => new Float32Array(out, HEADER_BYTES + c * frames * 4, frames).set(ch));
  return out;
}

function decodeAudio(data: ArrayBuffer): { tag: string; channels: Float32Array[] } {
  if (data.byteLength < HEADER_BYTES || new DataView(data).getUint32(0) !== MAGIC) throw new Error("EncodingError: Unable to decode audio data");
  const view = new DataView(data);
  const count = view.getUint32(4);
  const frames = view.getUint32(8);
  const tag = new TextDecoder().decode(new Uint8Array(data, 12, TAG_BYTES)).replace(/\0+$/, "");
  const channels = Array.from({ length: count }, (_, c) => new Float32Array(data.slice(HEADER_BYTES + c * frames * 4, HEADER_BYTES + (c + 1) * frames * 4)));
  return { tag, channels };
}

// ---------- 装到全局 ----------
/**
 * 把假的 AudioContext / AudioWorkletNode / window 定时器，以及 AudioWorkletGlobalScope 的全局量装到 globalThis。
 * 要在 import worklet 模块之前调用（它一加载就会 registerProcessor）。返回卸载函数。
 */
export function installFakeWebAudio(sampleRate: number): () => void {
  const scope = globalThis as Record<string, unknown>;
  const names = ["AudioContext", "AudioWorkletNode", "AudioWorkletProcessor", "registerProcessor", "sampleRate", "currentFrame", "window"];
  const saved = names.map((name) => [name, Object.getOwnPropertyDescriptor(scope, name)] as const);
  const context = (): FakeAudioContext => {
    if (!FakeAudioContext.current) throw new Error("No AudioContext has been created yet");
    return FakeAudioContext.current;
  };
  const values: Record<string, unknown> = {
    AudioContext: FakeAudioContext,
    AudioWorkletNode: FakeWorkletNode,
    AudioWorkletProcessor: FakeAudioWorkletProcessor,
    registerProcessor: (name: string, ctor: ProcessorCtor) => {
      if (registry.has(name)) throw new Error(`NotSupportedError: "${name}" is already registered`);
      registry.set(name, ctor);
    },
    sampleRate,
    currentFrame: 0,
    window: {
      setTimeout: (fn: () => void, ms?: number) => context().setTimeout(fn, ms),
      clearTimeout: (id: number) => context().clearTimeout(id),
    },
  };
  for (const name of names) Object.defineProperty(scope, name, { value: values[name], writable: true, configurable: true });
  return () => {
    for (const [name, descriptor] of saved) {
      if (descriptor) Object.defineProperty(scope, name, descriptor);
      else delete scope[name];
    }
    registry.clear();
    FakeAudioContext.current = null;
  };
}

export function registeredProcessors(): string[] {
  return [...registry.keys()];
}
