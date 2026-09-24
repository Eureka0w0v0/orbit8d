// 左侧音轨面板与右侧参数面板（分【轨道】【混音】【段落】【空间】四页）。
// 轨道参数改的是“播放头所在的那一段”；混音、房间是全曲共用的。面板只负责显示与收集操作，改数据统一交给 App。

import { effectiveTrackGains } from "../audio/params";
import { T, directionName, presetName, sectionName } from "../i18n";
import { BEATS_PER_BAR } from "../orbit/orbit";
import { DIRECTIONS, matchDirection, orientationFromTilt, tiltFromOrientation } from "../orbit/orientation";
import { eventsIn, sectionEnd } from "../scene/edit";
import { LAYERS, layerOf } from "../scene/layers";
import { TRACK_COLORS } from "../scene/orbits";
import type { Analysis, Bars, EventKind, Mix, Orbit, Room, RoomName, Scene, Shape, Speed, TrackName } from "../types";
import { TRACKS } from "../types";
import type { SchemaDef, SchemaRanges } from "./schema";
import { Slider, fmt, h, segmented, setSegmented } from "./dom";
import {
  DEFAULT_SECTION_COLOR,
  EVENT_LABEL,
  PARAM_LABEL,
  ROOM_LABEL,
  SECTION_COLORS,
  SECTION_LABELS,
  SHAPE_LABEL,
  TEXT,
  TRACK_LABEL,
} from "./labels";
import { TiltPad } from "./tiltpad";

const STEREO_TRACKS: ReadonlySet<TrackName> = new Set(["drums", "other"]);
const SHAPES: Shape[] = ["circle", "ellipse", "pendulum", "figure8", "spiral", "fixed"];
const BAR_CHOICES: Bars[] = [1, 2, 4, 8];
const ROOMS: RoomName[] = ["room", "hall", "church"];
type Tab = "orbit" | "mix" | "section" | "space";
const TABS: ReadonlyArray<{ value: Tab; label: string }> = [
  { value: "orbit", label: TEXT.tabOrbit },
  { value: "mix", label: TEXT.tabMix },
  { value: "section", label: TEXT.tabSection },
  { value: "space", label: TEXT.tabSpace },
];

const cssColor = (hex: number) => `#${hex.toString(16).padStart(6, "0")}`;
const sectionColor = (label: string) => SECTION_COLORS[label] ?? DEFAULT_SECTION_COLOR;

export interface PanelActions {
  select(track: TrackName): void;
  setMix(track: TrackName, patch: Partial<Mix>): void;
  setOrbit(track: TrackName, patch: Partial<Orbit>): void;
  setSpeed(track: TrackName, patch: Partial<Speed>): void;
  setRoom(patch: Partial<Room>): void;
  setRearDarken(db: number): void;
  applyPreset(name: string): void;
  autoChoreograph(): void;
  setSectionLabel(label: string): void;
  setSectionWet(db: number): void;
  splitSection(): void;
  removeSection(): void;
  addEvent(kind: EventKind): void;
  removeEvent(index: number): void;
  resetView(): void;
  domeVisible(): boolean;
  toggleDome(visible: boolean): void;
}

/** 面板要显示的一切：场景、选中音轨、播放头所在段、分析结果、歌曲长度。 */
export interface PanelContext {
  scene: Scene;
  track: TrackName;
  section: number;
  analysis: Analysis;
  duration: number;
}

interface TrackRow {
  row: HTMLDivElement;
  mute: HTMLButtonElement;
  solo: HTMLButtonElement;
}

/** 左栏：音轨名、静音、独奏；下面是自动编排与预设。 */
export class TracksPanel {
  readonly el: HTMLElement;
  private readonly rows = new Map<TrackName, TrackRow>();
  private readonly scope: HTMLParagraphElement;

  constructor(actions: PanelActions, presets: readonly string[]) {
    const list = h("div", { class: "track-list" });
    for (const name of TRACKS) {
      const mute = h("button", { type: "button", class: "chip", title: TEXT.mute }, "M");
      const solo = h("button", { type: "button", class: "chip", title: TEXT.solo }, "S");
      mute.addEventListener("click", (e) => {
        e.stopPropagation();
        actions.setMix(name, { mute: !mute.classList.contains("on") });
      });
      solo.addEventListener("click", (e) => {
        e.stopPropagation();
        actions.setMix(name, { solo: !solo.classList.contains("on") });
      });
      const row = h(
        "div",
        { class: "track-row", style: `--track:${cssColor(TRACK_COLORS[name])}` },
        h("span", { class: "dot" }),
        h("span", { class: "track-name" }, TRACK_LABEL[name]),
        mute,
        solo,
      );
      row.addEventListener("click", () => actions.select(name));
      this.rows.set(name, { row, mute, solo });
      list.append(row);
    }
    const auto = h("button", { type: "button", class: "preset auto", title: TEXT.autoChoreoTitle, onclick: () => actions.autoChoreograph() }, TEXT.autoChoreo);
    const presetBox = h("div", { class: "presets" }, auto);
    for (const p of presets) {
      presetBox.append(h("button", { type: "button", class: "preset", onclick: () => actions.applyPreset(p) }, presetName(p)));
    }
    this.scope = h("p", { class: "hint scope" });
    this.el = h("aside", { class: "panel left" }, h("h2", {}, TEXT.tracks), list, h("h2", {}, TEXT.presets), presetBox, this.scope);
  }

  sync(ctx: PanelContext): void {
    const gains = effectiveTrackGains(ctx.scene);
    for (const name of TRACKS) {
      const r = this.rows.get(name)!;
      const m = ctx.scene.mix[name];
      r.row.classList.toggle("selected", name === ctx.track);
      r.row.classList.toggle("silent", gains[name] === 0);
      r.mute.classList.toggle("on", m.mute);
      r.solo.classList.toggle("on", m.solo);
    }
    const multi = ctx.scene.sections.length > 1;
    this.scope.classList.toggle("hidden", !multi);
    if (multi) this.scope.textContent = TEXT.presetScope(sectionName(ctx.scene.sections[ctx.section].label));
  }
}

/** 右栏：选中音轨在当前段的参数，分四页。结构变化时重建，数值变化时只同步显示（不打断拖动）。 */
export class OrbitPanel {
  readonly el: HTMLElement;
  private readonly body: HTMLDivElement;
  private tab: Tab = "orbit";
  private fineOpen = false;
  private builtKey = "";
  private sliders: Array<{ slider: Slider; read: (s: Scene) => number }> = [];
  private layerBox: HTMLDivElement | null = null;
  private directionBox: HTMLDivElement | null = null;
  private pad: TiltPad | null = null;
  private last: PanelContext | null = null;

  constructor(private readonly actions: PanelActions, private readonly ranges: SchemaRanges) {
    this.body = h("div", { class: "orbit-body" });
    this.el = h("aside", { class: "panel right" }, this.body);
  }

  sync(ctx: PanelContext): void {
    this.last = ctx;
    const { scene, track, section } = ctx;
    const sec = scene.sections[section];
    const o = sec.orbits[track];
    const key = [
      this.tab,
      track,
      section,
      scene.sections.length,
      sec.label,
      o.shape,
      o.speed.mode,
      o.speed.bars,
      o.direction,
      scene.room.name,
      this.tab === "section" ? JSON.stringify([sec.start_s, sectionEnd(scene, section, ctx.duration), eventsIn(scene, section, ctx.duration)]) : "",
    ].join("|");
    if (key !== this.builtKey) {
      this.builtKey = key;
      this.build(ctx);
    }
    for (const { slider, read } of this.sliders) slider.set(read(scene));
    if (this.layerBox) setSegmented(this.layerBox, layerOf(o.height_deg));
    if (this.directionBox) setSegmented(this.directionBox, matchDirection(o));
    this.pad?.set(tiltFromOrientation(o));
  }

  private switchTab(tab: Tab): void {
    this.tab = tab;
    if (this.last) this.sync(this.last);
  }

  private slider(
    label: string,
    def: SchemaDef | null,
    prop: string,
    step: number,
    format: (v: number) => string,
    read: (s: Scene) => number,
    scene: Scene,
    onInput: (v: number) => void,
  ): HTMLElement {
    const slider = new Slider({ label, ...this.ranges.of(def, prop), step, value: read(scene), format, onInput });
    this.sliders.push({ slider, read });
    return slider.el;
  }

  private build(ctx: PanelContext): void {
    this.sliders = [];
    this.layerBox = null;
    this.directionBox = null;
    this.pad = null;
    const label = ctx.scene.sections[ctx.section].label;
    const head = h(
      "div",
      { class: "panel-head", style: `--track:${cssColor(TRACK_COLORS[ctx.track])}` },
      h("span", { class: "dot" }),
      h("span", { class: "panel-title" }, TRACK_LABEL[ctx.track]),
      h("span", { class: "section-chip", style: `--c:${sectionColor(label)}` }, sectionName(label)),
    );
    const tabs = segmented(TABS, this.tab, (t) => this.switchTab(t), "tabs");
    const content =
      this.tab === "orbit"
        ? this.orbitTab(ctx)
        : this.tab === "mix"
          ? this.mixTab(ctx)
          : this.tab === "section"
            ? this.sectionTab(ctx)
            : this.spaceTab(ctx.scene);
    this.body.replaceChildren(head, tabs, ...content);
  }

  private orbitTab({ scene, track, section, analysis }: PanelContext): HTMLElement[] {
    const a = this.actions;
    const o = scene.sections[section].orbits[track];
    const orbit = (s: Scene) => s.sections[section].orbits[track];
    const setO = (patch: Partial<Orbit>) => a.setOrbit(track, patch);
    const barSeconds = (bars: number) => (bars * BEATS_PER_BAR * 60) / analysis.bpm_norm;
    const moving = o.shape !== "fixed";
    const out: HTMLElement[] = [
      h("div", { class: "field-label" }, TEXT.shape),
      segmented(SHAPES.map((s) => ({ value: s, label: SHAPE_LABEL[s] })), o.shape, (s) => setO({ shape: s }), "grid3"),
    ];

    this.layerBox = segmented(
      [...LAYERS].reverse().map((l) => ({ value: l.name, label: T.layer[l.name] })),
      layerOf(o.height_deg) ?? "",
      (name) => setO({ height_deg: LAYERS.find((l) => l.name === name)!.center }),
      "stack",
    );
    if (moving) {
      this.directionBox = segmented(
        DIRECTIONS.map((d) => ({ value: d.key, label: directionName(d.key) })),
        matchDirection(o) ?? "",
        (key) => setO(orientationFromTilt(DIRECTIONS.find((d) => d.key === key)!.tilt)),
        "grid3",
      );
      this.pad = new TiltPad((t) => setO(orientationFromTilt(t)));
      this.pad.set(tiltFromOrientation(o));
      out.push(
        h("div", { class: "field-label" }, TEXT.orientation),
        this.directionBox,
        h("div", { class: "pad-row" }, this.pad.el, h("div", { class: "pad-side" }, h("div", { class: "field-label" }, TEXT.layer), this.layerBox)),
        h("p", { class: "hint" }, TEXT.padHint),
      );
    } else {
      out.push(h("div", { class: "field-label" }, TEXT.layer), this.layerBox);
    }

    out.push(this.slider(PARAM_LABEL.radius_m, "Orbit", "radius_m", 0.05, fmt.meters, (s) => orbit(s).radius_m, scene, (v) => setO({ radius_m: v })));
    if (!moving) {
      out.push(this.slider(PARAM_LABEL.start_deg, "Orbit", "start_deg", 1, fmt.deg, (s) => orbit(s).start_deg, scene, (v) => setO({ start_deg: v })));
    }
    if (o.shape === "ellipse") {
      out.push(this.slider(PARAM_LABEL.aspect, "Orbit", "aspect", 0.01, fmt.ratio, (s) => orbit(s).aspect, scene, (v) => setO({ aspect: v })));
    }
    if (o.shape === "pendulum" || o.shape === "figure8") {
      out.push(this.slider(PARAM_LABEL.swing_deg, "Orbit", "swing_deg", 1, fmt.deg, (s) => orbit(s).swing_deg, scene, (v) => setO({ swing_deg: v })));
    }
    if (o.shape === "figure8" || o.shape === "spiral") {
      out.push(this.slider(PARAM_LABEL.lift_deg, "Orbit", "lift_deg", 1, fmt.deg, (s) => orbit(s).lift_deg, scene, (v) => setO({ lift_deg: v })));
    }
    if (moving) {
      out.push(
        h("div", { class: "field-label" }, PARAM_LABEL.speed),
        segmented(
          [{ value: "bars", label: TEXT.byBars }, { value: "seconds", label: TEXT.seconds }] as const,
          o.speed.mode,
          (m) => a.setSpeed(track, { mode: m }),
        ),
        o.speed.mode === "bars"
          ? segmented(
              BAR_CHOICES.map((b) => ({ value: b, label: `${T.unit.bars(b)} · ${barSeconds(b).toFixed(1)}s` })),
              o.speed.bars,
              (b) => a.setSpeed(track, { bars: b }),
              "grid2",
            )
          : this.slider(PARAM_LABEL.speed, "Speed", "seconds", 0.5, fmt.seconds, (s) => orbit(s).speed.seconds, scene, (v) => a.setSpeed(track, { seconds: v })),
        h("div", { class: "field-label" }, PARAM_LABEL.direction),
        segmented([{ value: "cw", label: TEXT.cw }, { value: "ccw", label: TEXT.ccw }] as const, o.direction, (d) => setO({ direction: d })),
      );
      const fine = h(
        "details",
        { class: "fine", open: this.fineOpen },
        h("summary", {}, TEXT.fineTune),
        this.slider(PARAM_LABEL.start_deg, "Orbit", "start_deg", 1, fmt.deg, (s) => orbit(s).start_deg, scene, (v) => setO({ start_deg: v })),
        this.slider(PARAM_LABEL.height_deg, "Orbit", "height_deg", 1, fmt.deg, (s) => orbit(s).height_deg, scene, (v) => setO({ height_deg: v })),
        this.slider(PARAM_LABEL.pitch_deg, "Orbit", "pitch_deg", 1, fmt.deg, (s) => orbit(s).pitch_deg, scene, (v) => setO({ pitch_deg: v })),
        this.slider(PARAM_LABEL.roll_deg, "Orbit", "roll_deg", 1, fmt.deg, (s) => orbit(s).roll_deg, scene, (v) => setO({ roll_deg: v })),
        this.slider(PARAM_LABEL.yaw_deg, "Orbit", "yaw_deg", 1, fmt.deg, (s) => orbit(s).yaw_deg, scene, (v) => setO({ yaw_deg: v })),
      );
      fine.addEventListener("toggle", () => (this.fineOpen = fine.open));
      out.push(fine);
    } else {
      out.push(this.slider(PARAM_LABEL.height_deg, "Orbit", "height_deg", 1, fmt.deg, (s) => orbit(s).height_deg, scene, (v) => setO({ height_deg: v })));
    }
    return out;
  }

  private mixTab({ scene, track }: PanelContext): HTMLElement[] {
    const a = this.actions;
    const out = [this.slider(PARAM_LABEL.gain_db, "Mix", "gain_db", 0.5, fmt.db, (s) => s.mix[track].gain_db, scene, (v) => a.setMix(track, { gain_db: v }))];
    if (STEREO_TRACKS.has(track)) {
      out.push(this.slider(PARAM_LABEL.width_deg, "Mix", "width_deg", 1, fmt.deg, (s) => s.mix[track].width_deg, scene, (v) => a.setMix(track, { width_deg: v })));
    }
    out.push(this.slider(PARAM_LABEL.reverb_send, "Mix", "reverb_send", 0.01, fmt.percent, (s) => s.mix[track].reverb_send, scene, (v) => a.setMix(track, { reverb_send: v })));
    return out;
  }

  private sectionTab({ scene, track, section, analysis, duration }: PanelContext): HTMLElement[] {
    const a = this.actions;
    const sec = scene.sections[section];
    const end = sectionEnd(scene, section, duration);
    const bars = Math.round((end - sec.start_s) / ((BEATS_PER_BAR * 60) / analysis.bpm_norm));
    const labels: string[] = [...SECTION_LABELS];
    if (!labels.includes(sec.label)) labels.push(sec.label);
    const events = eventsIn(scene, section, duration);
    const list = events.length
      ? h(
          "div",
          { class: "event-list" },
          ...events.map(({ index, event }) =>
            h(
              "div",
              { class: `event-row ${event.kind}` },
              h("span", { class: "event-kind" }, EVENT_LABEL[event.kind]),
              h("span", { class: "event-meta" }, `${fmt.clock(event.t_s)} · ${event.targets.map((t) => TRACK_LABEL[t]).join(TEXT.listSep)}`),
              h("button", { type: "button", class: "chip", title: TEXT.deleteEvent, onclick: () => a.removeEvent(index) }, "×"),
            ),
          ),
        )
      : h("p", { class: "hint" }, TEXT.noEvents);
    return [
      h("div", { class: "section-summary" }, `${fmt.clock(sec.start_s)} – ${fmt.clock(end)} · ${T.unit.bars(bars)}`),
      h("div", { class: "field-label" }, TEXT.sectionName),
      segmented(labels.map((l) => ({ value: l, label: sectionName(l) })), sec.label, (l) => a.setSectionLabel(l), "grid3"),
      this.slider(PARAM_LABEL.wet_db, "Section", "wet_db", 0.5, fmt.db, (s) => s.sections[section].wet_db, scene, (v) => a.setSectionWet(v)),
      h(
        "div",
        { class: "button-row" },
        h("button", { type: "button", title: TEXT.splitTitle, onclick: () => a.splitSection() }, TEXT.split),
        h("button", { type: "button", title: TEXT.removeSectionTitle, disabled: scene.sections.length <= 1, onclick: () => a.removeSection() }, TEXT.removeSection),
      ),
      h("div", { class: "field-label" }, TEXT.sectionEvents),
      list,
      h("div", { class: "field-label" }, `${TEXT.addAtPlayhead}${TRACK_LABEL[track]}`),
      h(
        "div",
        { class: "button-row" },
        h("button", { type: "button", onclick: () => a.addEvent("hold") }, `＋ ${EVENT_LABEL.hold}`),
        h("button", { type: "button", onclick: () => a.addEvent("overhead") }, `＋ ${EVENT_LABEL.overhead}`),
      ),
      h("p", { class: "hint" }, TEXT.boundaryHint),
    ];
  }

  private spaceTab(scene: Scene): HTMLElement[] {
    const a = this.actions;
    const toggle = h("input", { type: "checkbox", checked: a.domeVisible() });
    toggle.addEventListener("change", () => a.toggleDome(toggle.checked));
    return [
      h("div", { class: "field-label" }, TEXT.room),
      segmented(ROOMS.map((r) => ({ value: r, label: ROOM_LABEL[r] })), scene.room.name, (r) => a.setRoom({ name: r })),
      this.slider(PARAM_LABEL.rear_darken_db, null, "rear_darken_db", 0.5, fmt.db, (s) => s.rear_darken_db, scene, (v) => a.setRearDarken(v)),
      h("label", { class: "toggle" }, toggle, h("span", {}, TEXT.showDome)),
      h("button", { type: "button", class: "ghost", onclick: () => a.resetView() }, TEXT.resetView),
    ];
  }
}
