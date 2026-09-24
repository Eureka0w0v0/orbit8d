// 总控：界面阶段状态机 + 场景数据 + 3D 视图 + 试听引擎 + 导入导出流程。

import { ApiError, api, poll } from "./api";
import { AudioEngine, STEM_NAMES } from "./audio/engine";
import { buildRenderParams, effectiveTrackGains } from "./audio/params";
import { toOrbitParams, type OrbitParams } from "./orbit/orbit";
import { Handles } from "./scene/handles";
import { loadHead } from "./scene/head";
import { visualRadius } from "./scene/mapping";
import { OrbitView } from "./scene/orbits";
import { Stage } from "./scene/stage";
import type { ExportFormat, Orbit, Project, Room, RoomName, Scene, Speed, Track, TrackName } from "./types";
import { TRACKS } from "./types";
import { h } from "./ui/dom";
import { EXPORT_STATE_LABEL, PROJECT_STATE_LABEL, TEXT } from "./ui/labels";
import { OrbitPanel, TracksPanel, type PanelActions } from "./ui/panels";
import { SchemaRanges } from "./ui/schema";
import { ExportDialog, Overlay, Transport } from "./ui/widgets";

type Phase = "booting" | "empty" | "uploading" | "processing" | "loading" | "ready" | "error";

/** 界面阶段的显式状态机：只允许表里的跳转。 */
const PHASE_TRANSITIONS: Record<Phase, readonly Phase[]> = {
  booting: ["empty", "loading", "error"],
  empty: ["uploading"],
  uploading: ["processing", "loading", "error"],
  processing: ["loading", "error"],
  loading: ["ready", "error"],
  ready: ["uploading"],
  error: ["uploading", "empty"],
};

const PRESETS = ["classic", "singer", "dual", "tumble"] as const;
const DEFAULT_PRESET = "classic";
const LAST_PROJECT_KEY = "orbit8d.lastProject";
const TOAST_MS = 4000;
const STAGE_SPAN: Partial<Record<Project["state"], [number, number]>> = {
  UPLOADED: [0, 0.02],
  DECODING: [0.02, 0.06],
  SEPARATING: [0.06, 0.88],
  ANALYZING: [0.88, 1],
};
const STEREO: ReadonlySet<TrackName> = new Set(["drums", "other"]);

function describe(err: unknown): string {
  if (err instanceof ApiError) return `${err.message}（${err.code}）`;
  if (err instanceof Error) return err.message;
  return String(err);
}

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null; // 隐私模式等情况下不可用，只影响“刷新后自动打开上次的歌”
  }
}

export class App {
  private phase: Phase = "booting";
  private project: Project | null = null;
  private scene: Scene | null = null;
  private selected: TrackName = "vocals";
  private formats: ExportFormat[] = [];
  private paramsDirty = false;
  private readonly brirCache = new Map<RoomName, ArrayBuffer>();
  private loadedRoom: RoomName | null = null;
  private readonly views = new Map<TrackName, OrbitView>();
  private readonly engine = new AudioEngine();
  private stage!: Stage;
  private handles!: Handles;
  private tracksPanel!: TracksPanel;
  private orbitPanel!: OrbitPanel;
  private transport!: Transport;
  private overlay!: Overlay;
  private exportDialog!: ExportDialog;
  private title!: HTMLElement;
  private toastBox!: HTMLElement;

  async start(root: HTMLElement): Promise<void> {
    const viewport = h("div", { class: "viewport" });
    this.title = h("span", { class: "song" });
    const header = h(
      "header",
      { class: "topbar" },
      h("span", { class: "logo" }, TEXT.appName),
      this.title,
      h("button", { type: "button", class: "ghost", onclick: () => this.overlay.pickFile() }, "导入新歌"),
      h("button", { type: "button", class: "primary", onclick: () => this.openExport() }, TEXT.export),
    );
    this.toastBox = h("div", { class: "toasts" });
    this.overlay = new Overlay((file) => void this.importFile(file));
    this.exportDialog = new ExportDialog();
    this.transport = new Transport(() => void this.togglePlay(), (t) => void this.engine.seek(t));
    root.replaceChildren(viewport, header, this.transport.el, h("p", { class: "credits" }, TEXT.credits), this.overlay.el, this.exportDialog.el, this.toastBox);
    this.overlay.showProgress("正在启动", null);

    try {
      this.stage = new Stage(viewport, visualRadius);
      const [health, schema, hrtf, eq, head] = await Promise.all([api.health(), api.schema(), api.hrtf(), api.eq(), loadHead()]);
      this.formats = health.formats;
      this.stage.scene.add(head);
      await this.engine.init(hrtf, eq);
      this.engine.onEnded = () => this.transport.update(this.engine.time, this.engine.duration, false);
      const ranges = SchemaRanges.from(schema);
      const actions = this.panelActions();
      this.tracksPanel = new TracksPanel(actions, ranges, PRESETS);
      this.orbitPanel = new OrbitPanel(actions, ranges);
      root.append(this.tracksPanel.el, this.orbitPanel.el);
      for (const track of TRACKS) {
        const view = new OrbitView(track);
        this.views.set(track, view);
        this.stage.scene.add(view.group);
      }
      this.handles = new Handles(this.stage, {
        views: this.views,
        selected: () => this.selected,
        params: (t) => this.orbitParams(t),
        orbit: (t) => this.scene!.tracks[t].orbit,
        playing: () => this.engine.playing,
        songTime: () => this.engine.time,
        tRef: () => this.project?.analysis?.t_ref ?? 0,
        select: (t) => this.select(t),
        change: (t, patch) => this.mutate((s) => Object.assign(s.tracks[t].orbit, patch)),
      });
      this.stage.onFrame(() => this.tick());
      window.addEventListener("keydown", (e) => this.onKey(e));
      await this.restoreLastProject();
    } catch (err) {
      this.fail(err);
    }
  }

  // ---------- 状态机 ----------
  private go(next: Phase): void {
    if (!PHASE_TRANSITIONS[this.phase].includes(next)) throw new Error(`界面状态非法跳转：${this.phase} → ${next}`);
    this.phase = next;
    document.body.dataset.phase = next;
  }

  private fail(err: unknown): void {
    console.error("[orbit8d]", err);
    if (PHASE_TRANSITIONS[this.phase].includes("error")) {
      this.go("error");
      this.overlay.showError(describe(err));
    } else {
      this.toast(describe(err));
    }
  }

  private toast(message: string): void {
    const el = h("div", { class: "toast" }, message);
    this.toastBox.append(el);
    window.setTimeout(() => el.remove(), TOAST_MS);
  }

  // ---------- 导入与加载 ----------
  private async restoreLastProject(): Promise<void> {
    const id = storage()?.getItem(LAST_PROJECT_KEY);
    if (id) {
      try {
        const project = await api.project(id);
        if (project.state === "READY") {
          await this.loadProject(project);
          return;
        }
      } catch (err) {
        console.warn("[orbit8d] 上次的歌已不可用", err);
        storage()?.removeItem(LAST_PROJECT_KEY);
      }
    }
    this.go("empty");
    this.overlay.showDrop();
  }

  private async importFile(file: File): Promise<void> {
    if (!PHASE_TRANSITIONS[this.phase].includes("uploading")) return;
    this.engine.pause();
    this.go("uploading");
    try {
      this.overlay.showProgress(TEXT.uploading, 0, file.name);
      let project = await api.upload(file, (f) => this.overlay.showProgress(TEXT.uploading, f, file.name));
      if (project.state !== "READY") {
        this.go("processing");
        project = await poll(
          () => api.project(project.id),
          (p) => p.state === "READY" || p.state === "FAILED",
          (p) => this.overlay.showProgress(PROJECT_STATE_LABEL[p.state], this.overallProgress(p), p.source.filename),
        );
        if (project.state === "FAILED") {
          throw new ApiError(0, project.error?.code ?? "FAILED", project.error?.message ?? PROJECT_STATE_LABEL.FAILED);
        }
      }
      await this.loadProject(project);
    } catch (err) {
      this.fail(err);
    }
  }

  private overallProgress(p: Project): number {
    const [lo, hi] = STAGE_SPAN[p.state] ?? [1, 1];
    return lo + (hi - lo) * p.progress;
  }

  private async loadProject(project: Project): Promise<void> {
    const analysis = project.analysis;
    if (!analysis) throw new Error("项目缺少分析结果");
    this.go("loading");
    this.overlay.showProgress("加载试听音频", null, project.source.filename);
    const buffers = await Promise.all(STEM_NAMES.map(async (n) => [n, await api.stem(project.id, n)] as const));
    await this.engine.loadStems(Object.fromEntries(buffers));
    const scene = await api.preset(DEFAULT_PRESET, analysis.default_bars);
    this.project = project;
    this.selected = "vocals";
    await this.ensureRoom(scene.room.name);
    this.applyScene(scene);
    this.title.textContent = project.source.title
      ? `${project.source.artist ? `${project.source.artist} - ` : ""}${project.source.title}`
      : project.source.filename;
    this.transport.setInfo(`${Math.round(analysis.bpm_norm)} BPM`);
    storage()?.setItem(LAST_PROJECT_KEY, project.id);
    this.stage.resetView();
    this.go("ready");
    this.overlay.hide();
  }

  // ---------- 场景 ----------
  private orbitParams(track: TrackName): OrbitParams {
    return toOrbitParams(this.scene!.tracks[track].orbit, this.project?.analysis?.bpm_norm ?? 120);
  }

  private panelActions(): PanelActions {
    return {
      select: (t) => this.select(t),
      setTrack: (t, patch: Partial<Track>) => this.mutate((s) => Object.assign(s.tracks[t], patch)),
      setOrbit: (t, patch: Partial<Orbit>) => this.mutate((s) => Object.assign(s.tracks[t].orbit, patch)),
      setSpeed: (t, patch: Partial<Speed>) => this.mutate((s) => Object.assign(s.tracks[t].orbit.speed, patch)),
      setRoom: (patch: Partial<Room>) => this.mutate((s) => Object.assign(s.room, patch)),
      setRearDarken: (db) => this.mutate((s) => (s.rear_darken_db = db)),
      applyPreset: (name) => void this.applyPreset(name),
      resetView: () => this.stage.resetView(),
    };
  }

  private select(track: TrackName): void {
    this.selected = track;
    if (this.scene) this.applyScene(this.scene);
  }

  private mutate(fn: (draft: Scene) => void): void {
    if (!this.scene) return;
    const next = structuredClone(this.scene);
    fn(next);
    this.applyScene(next);
  }

  private async applyPreset(name: string): Promise<void> {
    const analysis = this.project?.analysis;
    if (!analysis) return;
    try {
      const scene = await api.preset(name, analysis.default_bars);
      await this.ensureRoom(scene.room.name);
      this.applyScene(scene);
    } catch (err) {
      this.fail(err);
    }
  }

  private applyScene(scene: Scene): void {
    this.scene = scene;
    const analysis = this.project?.analysis;
    if (!analysis) return;
    const gains = effectiveTrackGains(scene);
    for (const track of TRACKS) {
      this.views.get(track)?.update({
        params: this.orbitParams(track),
        widthDeg: scene.tracks[track].width_deg,
        stereo: STEREO.has(track),
        audible: gains[track] > 0,
        selected: track === this.selected,
      });
    }
    this.tracksPanel.sync(scene, this.selected);
    this.orbitPanel.sync(scene, this.selected, analysis);
    this.paramsDirty = true;
    if (scene.room.name !== this.loadedRoom) void this.ensureRoom(scene.room.name).catch((err) => this.fail(err));
  }

  private async ensureRoom(room: RoomName): Promise<void> {
    if (this.loadedRoom === room) return;
    let wav = this.brirCache.get(room);
    if (!wav) {
      wav = await api.brir(room);
      this.brirCache.set(room, wav);
    }
    await this.engine.setRoom(wav.slice(0)); // decodeAudioData 会转移所有权，传副本
    this.loadedRoom = room;
  }

  // ---------- 每帧 ----------
  private tick(): void {
    const analysis = this.project?.analysis;
    if (!this.scene || !analysis || !this.project) return;
    if (this.paramsDirty) {
      this.paramsDirty = false;
      const params = buildRenderParams(this.scene, analysis.bpm_norm, analysis.t_ref, analysis.calibration, this.project.preview_scale);
      this.engine.setParams(params, this.scene.room.wet_db, analysis.preview_gain);
    }
    const t = this.engine.time;
    for (const view of this.views.values()) view.setTime(t, analysis.t_ref);
    this.handles.update();
    this.transport.update(t, this.engine.duration, this.engine.playing);
    this.transport.setLevels(this.engine.levels());
  }

  private async togglePlay(): Promise<void> {
    if (this.phase !== "ready") return;
    try {
      if (this.engine.playing) this.engine.pause();
      else await this.engine.play();
    } catch (err) {
      this.fail(err);
    }
  }

  private onKey(e: KeyboardEvent): void {
    const target = e.target as HTMLElement;
    if (e.code !== "Space" || ["INPUT", "BUTTON", "SELECT", "TEXTAREA"].includes(target.tagName)) return;
    e.preventDefault();
    void this.togglePlay();
  }

  // ---------- 导出 ----------
  private openExport(): void {
    if (this.phase !== "ready" || !this.project || !this.scene) {
      this.toast("先导入一首歌");
      return;
    }
    this.exportDialog.open(this.formats, (fmt) => void this.runExport(fmt));
  }

  private async runExport(format: ExportFormat): Promise<void> {
    if (!this.project || !this.scene) return;
    try {
      this.exportDialog.progress(EXPORT_STATE_LABEL.QUEUED);
      const created = await api.createExport(this.project.id, this.scene, format);
      const rec = await poll(
        () => api.exportRecord(created.id),
        (r) => r.state === "DONE" || r.state === "FAILED",
        (r) => this.exportDialog.progress(EXPORT_STATE_LABEL[r.state]),
      );
      if (rec.state === "FAILED") this.exportDialog.error(`${EXPORT_STATE_LABEL.FAILED}：${rec.error?.message ?? ""}`);
      else this.exportDialog.done(api.exportUrl(rec.id), rec.file_name);
    } catch (err) {
      this.exportDialog.error(describe(err));
    }
  }
}
