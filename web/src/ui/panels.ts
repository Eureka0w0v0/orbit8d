// 左侧音轨面板与右侧轨道参数面板。面板只负责显示与收集操作，场景数据的修改统一交给 App。

import { effectiveTrackGains } from "../audio/params";
import { BEATS_PER_BAR } from "../orbit/orbit";
import { TRACK_COLORS } from "../scene/orbits";
import type { Analysis, Bars, Orbit, Room, RoomName, Scene, Shape, Speed, Track, TrackName } from "../types";
import { TRACKS } from "../types";
import { Slider, fmt, h, segmented } from "./dom";
import { PARAM_LABEL, PRESET_LABEL, ROOM_LABEL, SHAPE_LABEL, TEXT, TRACK_LABEL } from "./labels";
import type { SchemaRanges } from "./schema";

const STEREO_TRACKS: ReadonlySet<TrackName> = new Set(["drums", "other"]);
const SHAPES: Shape[] = ["circle", "ellipse", "pendulum", "figure8", "spiral", "fixed"];
const BAR_CHOICES: Bars[] = [1, 2, 4, 8];
const ROOMS: RoomName[] = ["room", "hall", "church"];

const cssColor = (hex: number) => `#${hex.toString(16).padStart(6, "0")}`;

export interface PanelActions {
  select(track: TrackName): void;
  setTrack(track: TrackName, patch: Partial<Track>): void;
  setOrbit(track: TrackName, patch: Partial<Orbit>): void;
  setSpeed(track: TrackName, patch: Partial<Speed>): void;
  setRoom(patch: Partial<Room>): void;
  setRearDarken(db: number): void;
  applyPreset(name: string): void;
  resetView(): void;
}

interface TrackRow {
  row: HTMLDivElement;
  mute: HTMLButtonElement;
  solo: HTMLButtonElement;
  gain: Slider;
}

export class TracksPanel {
  readonly el: HTMLElement;
  private readonly rows = new Map<TrackName, TrackRow>();

  constructor(actions: PanelActions, ranges: SchemaRanges, presets: readonly string[]) {
    const gainRange = ranges.of("Track", "gain_db");
    const list = h("div", { class: "track-list" });
    for (const name of TRACKS) {
      const mute = h("button", { type: "button", class: "chip", title: TEXT.mute }, "M");
      const solo = h("button", { type: "button", class: "chip", title: TEXT.solo }, "S");
      const gain = new Slider({
        label: PARAM_LABEL.gain_db, ...gainRange, step: 0.5, value: 0, format: fmt.db,
        onInput: (v) => actions.setTrack(name, { gain_db: v }),
      });
      mute.addEventListener("click", (e) => {
        e.stopPropagation();
        actions.setTrack(name, { mute: !mute.classList.contains("on") });
      });
      solo.addEventListener("click", (e) => {
        e.stopPropagation();
        actions.setTrack(name, { solo: !solo.classList.contains("on") });
      });
      const row = h(
        "div",
        { class: "track-row", style: `--track:${cssColor(TRACK_COLORS[name])}` },
        h("div", { class: "track-head" }, h("span", { class: "dot" }), h("span", { class: "track-name" }, TRACK_LABEL[name]), mute, solo),
        gain.el,
      );
      row.addEventListener("click", () => actions.select(name));
      this.rows.set(name, { row, mute, solo, gain });
      list.append(row);
    }
    const presetBox = h("div", { class: "presets" });
    for (const p of presets) {
      presetBox.append(h("button", { type: "button", class: "preset", onclick: () => actions.applyPreset(p) }, PRESET_LABEL[p] ?? p));
    }
    this.el = h("aside", { class: "panel left" }, h("h2", {}, TEXT.tracks), list, h("h2", {}, TEXT.presets), presetBox);
  }

  sync(scene: Scene, selected: TrackName): void {
    const gains = effectiveTrackGains(scene);
    for (const name of TRACKS) {
      const r = this.rows.get(name)!;
      const t = scene.tracks[name];
      r.row.classList.toggle("selected", name === selected);
      r.row.classList.toggle("silent", gains[name] === 0);
      r.mute.classList.toggle("on", t.mute);
      r.solo.classList.toggle("on", t.solo);
      r.gain.set(t.gain_db);
    }
  }
}

export class OrbitPanel {
  readonly el: HTMLElement;
  private readonly body: HTMLDivElement;
  private builtKey = "";
  private sliders: Array<{ slider: Slider; read: (s: Scene) => number }> = [];

  constructor(private readonly actions: PanelActions, private readonly ranges: SchemaRanges) {
    this.body = h("div", { class: "orbit-body" });
    this.el = h("aside", { class: "panel right" }, this.body);
  }

  sync(scene: Scene, selected: TrackName, analysis: Analysis): void {
    const o = scene.tracks[selected].orbit;
    const key = `${selected}|${o.shape}|${o.speed.mode}|${o.speed.bars}|${o.direction}|${scene.room.name}`;
    if (key !== this.builtKey) {
      this.builtKey = key;
      this.build(scene, selected, analysis);
      return;
    }
    for (const { slider, read } of this.sliders) slider.set(read(scene));
  }

  private slider(
    label: string,
    def: "Orbit" | "Track" | "Room" | "Speed" | null,
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

  private build(scene: Scene, track: TrackName, analysis: Analysis): void {
    this.sliders = [];
    const a = this.actions;
    const o = scene.tracks[track].orbit;
    const orbit = (s: Scene) => s.tracks[track].orbit;
    const setO = (patch: Partial<Orbit>) => a.setOrbit(track, patch);
    const barSeconds = (bars: number) => (bars * BEATS_PER_BAR * 60) / analysis.bpm_norm;
    const moving = o.shape !== "fixed";

    const title = h(
      "h2",
      { style: `--track:${cssColor(TRACK_COLORS[track])}` },
      h("span", { class: "dot" }),
      `${TRACK_LABEL[track]} · ${TEXT.orbit}`,
    );
    const shape = segmented(SHAPES.map((s) => ({ value: s, label: SHAPE_LABEL[s] })), o.shape, (s) => setO({ shape: s }), "wrap");

    const geometry: HTMLElement[] = [
      this.slider(PARAM_LABEL.radius_m, "Orbit", "radius_m", 0.05, fmt.meters, (s) => orbit(s).radius_m, scene, (v) => setO({ radius_m: v })),
      this.slider(PARAM_LABEL.start_deg, "Orbit", "start_deg", 1, fmt.deg, (s) => orbit(s).start_deg, scene, (v) => setO({ start_deg: v })),
      this.slider(PARAM_LABEL.height_deg, "Orbit", "height_deg", 1, fmt.deg, (s) => orbit(s).height_deg, scene, (v) => setO({ height_deg: v })),
    ];
    if (moving) {
      geometry.push(
        this.slider(PARAM_LABEL.pitch_deg, "Orbit", "pitch_deg", 1, fmt.deg, (s) => orbit(s).pitch_deg, scene, (v) => setO({ pitch_deg: v })),
        this.slider(PARAM_LABEL.roll_deg, "Orbit", "roll_deg", 1, fmt.deg, (s) => orbit(s).roll_deg, scene, (v) => setO({ roll_deg: v })),
        this.slider(PARAM_LABEL.yaw_deg, "Orbit", "yaw_deg", 1, fmt.deg, (s) => orbit(s).yaw_deg, scene, (v) => setO({ yaw_deg: v })),
      );
    }
    if (o.shape === "ellipse") {
      geometry.push(this.slider(PARAM_LABEL.aspect, "Orbit", "aspect", 0.01, fmt.ratio, (s) => orbit(s).aspect, scene, (v) => setO({ aspect: v })));
    }
    if (o.shape === "pendulum" || o.shape === "figure8") {
      geometry.push(this.slider(PARAM_LABEL.swing_deg, "Orbit", "swing_deg", 1, fmt.deg, (s) => orbit(s).swing_deg, scene, (v) => setO({ swing_deg: v })));
    }
    if (o.shape === "figure8" || o.shape === "spiral") {
      geometry.push(this.slider(PARAM_LABEL.lift_deg, "Orbit", "lift_deg", 1, fmt.deg, (s) => orbit(s).lift_deg, scene, (v) => setO({ lift_deg: v })));
    }

    const motion: HTMLElement[] = [];
    if (moving) {
      motion.push(
        h("div", { class: "field-label" }, PARAM_LABEL.speed),
        segmented(
          [{ value: "bars", label: TEXT.bars }, { value: "seconds", label: TEXT.seconds }] as const,
          o.speed.mode,
          (m) => a.setSpeed(track, { mode: m }),
        ),
        o.speed.mode === "bars"
          ? segmented(
              BAR_CHOICES.map((b) => ({ value: b, label: `${b} 小节 · ${barSeconds(b).toFixed(1)}s` })),
              o.speed.bars,
              (b) => a.setSpeed(track, { bars: b }),
              "wrap",
            )
          : this.slider(PARAM_LABEL.speed, "Speed", "seconds", 0.5, fmt.seconds, (s) => orbit(s).speed.seconds, scene, (v) => a.setSpeed(track, { seconds: v })),
        h("div", { class: "field-label" }, PARAM_LABEL.direction),
        segmented([{ value: "cw", label: TEXT.cw }, { value: "ccw", label: TEXT.ccw }] as const, o.direction, (d) => setO({ direction: d })),
      );
    }

    const mix: HTMLElement[] = [];
    if (STEREO_TRACKS.has(track)) {
      mix.push(this.slider(PARAM_LABEL.width_deg, "Track", "width_deg", 1, fmt.deg, (s) => s.tracks[track].width_deg, scene, (v) => a.setTrack(track, { width_deg: v })));
    }
    mix.push(this.slider(PARAM_LABEL.reverb_send, "Track", "reverb_send", 0.01, fmt.percent, (s) => s.tracks[track].reverb_send, scene, (v) => a.setTrack(track, { reverb_send: v })));

    const space = [
      h("div", { class: "field-label" }, TEXT.room),
      segmented(ROOMS.map((r) => ({ value: r, label: ROOM_LABEL[r] })), scene.room.name, (r) => a.setRoom({ name: r })),
      this.slider(PARAM_LABEL.wet_db, "Room", "wet_db", 0.5, fmt.db, (s) => s.room.wet_db, scene, (v) => a.setRoom({ wet_db: v })),
      this.slider(PARAM_LABEL.rear_darken_db, null, "rear_darken_db", 0.5, fmt.db, (s) => s.rear_darken_db, scene, (v) => a.setRearDarken(v)),
    ];

    const sections: HTMLElement[] = [title, shape, h("div", { class: "group" }, ...geometry)];
    if (moving) sections.push(h("div", { class: "group" }, ...motion));
    this.body.replaceChildren(
      ...sections,
      h("div", { class: "group" }, ...mix),
      h("h2", {}, TEXT.global),
      h("div", { class: "group" }, ...space),
      h("p", { class: "hint" }, TEXT.dragHint),
      h("button", { type: "button", class: "ghost", onclick: () => a.resetView() }, TEXT.resetView),
    );
  }
}
