// Injected into the page before any of its scripts run (Page.addScriptToEvaluateOnNewDocument).
// Records what the app does to the real Web Audio API, without changing what the API does:
//   params   - per AudioParam, in order: value writes, setValueAtTime, linearRampToValueAtTime, setTargetAtTime, ...
//   messages - the type of every message posted to an AudioWorklet port, in order
//   graph    - nodes created, connect / disconnect, source start / stop, decodeAudioData, resume
// How times and values are normalised is described in ../browser_probe/__init__.py.
(() => {
  "use strict";
  const BASE_BUCKET_S = 0.03; // "now" vs "now + start latency": coarse on purpose, currentTime moves under our feet
  const state = { params: {}, messages: [], graph: [], inflight: 0, network: 0, frames: 0, maxFrameGapMs: 0 };
  const counters = {};
  const labels = new WeakMap();
  const workletPorts = new WeakMap();
  let context = null;
  let group = 0; // one group = one synchronous burst of calls (they share the same "now")
  let groupOpen = false;
  let groupBase = null;
  const lastCall = new WeakMap(); // AudioParam -> { group, method }

  const nameOf = (obj) => labels.get(obj) ?? "?";
  const label = (kind) => `${kind}${(counters[kind] = (counters[kind] ?? 0) + 1)}`;
  const value = (v) => String(Math.round(v * 1000) / 1000);

  function touchGroup() {
    if (groupOpen) return;
    groupOpen = true;
    groupBase = null;
    queueMicrotask(() => {
      groupOpen = false;
      group++;
    });
  }

  /** A time argument, relative to the first time seen in this burst; the first one is tagged now / now+x. */
  function when(t) {
    touchGroup();
    if (groupBase === null) {
      groupBase = t;
      const ahead = Math.round((t - (context ? context.currentTime : 0)) / BASE_BUCKET_S) * BASE_BUCKET_S;
      return ahead === 0 ? "@now" : `@now${ahead > 0 ? "+" : "-"}${Math.abs(Math.round(ahead * 1000) / 1000)}`;
    }
    const delta = Math.round((t - groupBase) * 10000) / 10000;
    return delta === 0 ? "@+0" : `@${delta > 0 ? "+" : "-"}${Math.abs(delta)}`;
  }

  function paramCall(param, text, method) {
    touchGroup();
    (state.params[nameOf(param)] ??= []).push(text);
    lastCall.set(param, { group, method });
  }

  // ---- the clock ----
  // Chrome lets currentTime tick over (by one render quantum) between two reads in the same piece of
  // synchronous code, and the app reads it several times while scheduling one fade. Under the probe every
  // read within one synchronous burst returns the first value, so the recorded times do not jitter.
  const clock = Object.getOwnPropertyDescriptor(BaseAudioContext.prototype, "currentTime");
  let frozenTime = null;
  Object.defineProperty(BaseAudioContext.prototype, "currentTime", {
    ...clock,
    get() {
      if (frozenTime === null) {
        frozenTime = clock.get.call(this);
        queueMicrotask(() => {
          frozenTime = null;
        });
      }
      return frozenTime;
    },
  });

  // ---- AudioParam ----
  const paramProto = AudioParam.prototype;
  const describe = {
    setValueAtTime(param, [v, t]) {
      const last = lastCall.get(param);
      // right after cancelScheduledValues in the same burst the app pins the value it just read: timing dependent
      const held = last && last.group === group && last.method === "cancelScheduledValues";
      return `setValueAtTime(${held ? "<current>" : value(v)}, ${when(t)})`;
    },
    linearRampToValueAtTime: (_, [v, t]) => `linearRampToValueAtTime(${value(v)}, ${when(t)})`,
    exponentialRampToValueAtTime: (_, [v, t]) => `exponentialRampToValueAtTime(${value(v)}, ${when(t)})`,
    setTargetAtTime: (_, [v, t, tau]) => `setTargetAtTime(${value(v)}, ${when(t)}, ${value(tau)})`,
    setValueCurveAtTime: (_, [curve, t, d]) => `setValueCurveAtTime(<${curve.length}>, ${when(t)}, ${value(d)})`,
    cancelScheduledValues: (_, [t]) => `cancelScheduledValues(${when(t)})`,
    cancelAndHoldAtTime: (_, [t]) => `cancelAndHoldAtTime(${when(t)})`,
  };
  for (const [method, text] of Object.entries(describe)) {
    const original = paramProto[method];
    if (typeof original !== "function") continue;
    paramProto[method] = function (...args) {
      paramCall(this, text(this, args), method);
      return original.apply(this, args);
    };
  }
  const valueProp = Object.getOwnPropertyDescriptor(paramProto, "value");
  Object.defineProperty(paramProto, "value", {
    ...valueProp,
    set(v) {
      paramCall(this, `value = ${value(v)}`, "value");
      valueProp.set.call(this, v);
    },
  });

  // ---- nodes ----
  const PARAMS = { gain: ["gain"], compressor: ["threshold", "knee", "ratio", "attack", "release"] };
  function register(node, kind) {
    const name = label(kind);
    labels.set(node, name);
    for (const p of PARAMS[kind] ?? []) labels.set(node[p], `${name}.${p}`);
    return name;
  }
  const contextProto = BaseAudioContext.prototype;
  const factories = {
    createGain: "gain",
    createConvolver: "convolver",
    createDynamicsCompressor: "compressor",
    createChannelSplitter: "splitter",
    createAnalyser: "analyser",
    createBufferSource: "source",
  };
  for (const [method, kind] of Object.entries(factories)) {
    const original = contextProto[method];
    contextProto[method] = function (...args) {
      const node = original.apply(this, args);
      state.graph.push(`${method} -> ${register(node, kind)}`);
      return node;
    };
  }
  const decode = contextProto.decodeAudioData;
  contextProto.decodeAudioData = function (...args) {
    state.graph.push("decodeAudioData");
    return decode.apply(this, args);
  };

  const RealContext = window.AudioContext;
  window.AudioContext = class extends RealContext {
    constructor(...args) {
      super(...args);
      context = this;
      labels.set(this.destination, "destination");
      state.graph.push(`new AudioContext(${this.sampleRate} Hz)`);
    }
    resume() {
      state.graph.push("resume");
      return super.resume();
    }
  };

  const nodeProto = AudioNode.prototype;
  const connect = nodeProto.connect;
  nodeProto.connect = function (...args) {
    state.graph.push(`${nameOf(this)}.connect(${[nameOf(args[0]), ...args.slice(1)].join(", ")})`);
    return connect.apply(this, args);
  };
  const disconnect = nodeProto.disconnect;
  nodeProto.disconnect = function (...args) {
    state.graph.push(`${nameOf(this)}.disconnect()`);
    return disconnect.apply(this, args);
  };

  // start(when, offset) is AudioBufferSourceNode's own method, stop(when) is inherited. The offset depends on timing.
  const start = AudioBufferSourceNode.prototype.start;
  AudioBufferSourceNode.prototype.start = function (...args) {
    state.graph.push(`${nameOf(this)}.start(${args.length ? when(args[0]) : ""})`);
    return start.apply(this, args);
  };
  const stop = AudioScheduledSourceNode.prototype.stop;
  AudioScheduledSourceNode.prototype.stop = function (...args) {
    state.graph.push(`${nameOf(this)}.stop(${args.length ? when(args[0]) : ""})`);
    return stop.apply(this, args);
  };

  // ---- AudioWorklet ----
  const RealWorkletNode = window.AudioWorkletNode;
  window.AudioWorkletNode = class extends RealWorkletNode {
    constructor(ctx, name, options) {
      super(ctx, name, options);
      const mine = register(this, "worklet");
      workletPorts.set(this.port, mine);
      state.graph.push(`new AudioWorkletNode(${name}, ${options?.channelCount} in, ${JSON.stringify(options?.outputChannelCount)} out) -> ${mine}`);
    }
  };
  const post = MessagePort.prototype.postMessage;
  MessagePort.prototype.postMessage = function (message, ...rest) {
    if (workletPorts.has(this)) state.messages.push(String(message?.type));
    return post.call(this, message, ...rest);
  };

  // ---- quiescence: requests in flight, animation frames ----
  const realFetch = window.fetch;
  window.fetch = function (...args) {
    state.inflight++;
    state.network++;
    const done = () => {
      state.inflight--;
      state.network++;
    };
    const result = realFetch.apply(this, args);
    result.then(done, done);
    return result;
  };
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (...args) {
    state.inflight++;
    state.network++;
    this.addEventListener("loadend", () => {
      state.inflight--;
      state.network++;
    });
    return send.apply(this, args);
  };
  let lastFrame = 0;
  const onFrame = (t) => {
    if (lastFrame) state.maxFrameGapMs = Math.max(state.maxFrameGapMs, t - lastFrame);
    lastFrame = t;
    state.frames++;
    requestAnimationFrame(onFrame);
  };
  requestAnimationFrame(onFrame);

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const frame = () => new Promise((resolve) => requestAnimationFrame(resolve));
  const activity = () => state.network + state.messages.length + state.graph.length + Object.values(state.params).reduce((n, list) => n + list.length, 0);

  window.__probe = {
    sleep,
    /** Resolves once nothing has happened for quietMs (page timers, so app timers due earlier have fired) and two frames ran. */
    async settle(quietMs = 400, limitMs = 60000) {
      const deadline = performance.now() + limitMs;
      for (;;) {
        const before = activity();
        await sleep(quietMs);
        await frame();
        await frame();
        if (state.inflight === 0 && activity() === before) return true;
        if (performance.now() > deadline) throw new Error("the page never settled");
      }
    },
    /** Everything recorded since the last call. */
    take() {
      const out = { params: state.params, messages: state.messages, graph: state.graph };
      state.params = {};
      state.messages = [];
      state.graph = [];
      return out;
    },
    stats: () => ({ frames: state.frames, maxFrameGapMs: Math.round(state.maxFrameGapMs), contextState: context?.state ?? "none" }),
  };
})();
