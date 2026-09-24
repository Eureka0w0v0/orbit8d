// 总控：界面阶段状态机 + 场景数据（分段时间轴）+ 3D 视图 + 试听引擎 + 导入导出流程。

import { ApiError, api, poll } from "./api";
import { AudioEngine, STEM_NAMES } from "./audio/engine";
import { buildRenderParams, compileMotions, effectiveTrackGains, type Timing } from "./audio/params";
import { toOrbitParams, type OrbitParams } from "./orbit/orbit";
import { phaseAt, type TrackMotion } from "./orbit/timeline";
import { Dome } from "./scene/dome";
import {
  addEvent,
  applyPreset,
  barGrid,
  moveBoundary,
  removeEvent,
  removeSection,
  sectionIndexAt,
  splitAt,
  type BarGrid,
  type EditResult,
} from "./scene/edit";
import { Handles } from "./scene/handles";
import { layerOf } from "./scene/layers";
import { loadHead } from "./scene/head";
import { visualRadius } from "./scene/mapping";
import { OrbitView } from "./scene/orbits";
import { Stage } from "./scene/stage";
import type { EventKind, ExportFormat, Mix, Orbit, Project, Room, RoomName, Scene, Speed, TrackName } from "./types";
import { TRACKS } from "./types";
import { h } from "./ui/dom";
import { EXPORT_STATE_LABEL, PROJECT_STATE_LABEL, TEXT } from "./ui/labels";
import { OrbitPanel, TracksPanel, type PanelActions, type PanelContext } from "./ui/panels";
import { SchemaRanges } from "./ui/schema";
import { TimelineStrip } from "./ui/timeline";
import { ExportDialog, Overlay, Transport } from "./ui/widgets";

type Phase = "booting" | "empty" | "uploading" | "processing" | "loading" | "ready" | "error";

/** 界面阶段的显式状态机：只允许表里的跳转。 */
const PHASE_TRANSITIONS: Record<Phase, readonly Phase[]> = {
  booting: ["empty", "processing", "loading", "error"],
  empty: ["uploading"],
  uploading: ["processing", "loading", "error"],
  processing: ["loading", "error"],
  loading: ["ready", "error"],
  ready: ["uploading"],
  error: ["uploading", "empty"],
};

const FALLBACK_PRESET = "classic";
const LAST_PROJECT_KEY = "orbit8d.lastProject";
const DOME_KEY = "orbit8d.showDome";
const TOAST_MS = 4000;
const EQ_DEBOUNCE_MS = 250;
const SEEK_STEP_S = 5;
const STAGE_SPAN: Partial<Record<Project["state"], [number, number]>> = {
  UPLOADED: [0, 0.02],
  DECODING: [0.02, 0.06],
  SEPARATING: [0.06, 0.88],
  ANALYZING: [0.88, 1],
};
const BUSY: ReadonlySet<Project["state"]> = new Set(["UPLOADED", "DECODING", "SEPARATING", "ANALYZING"]);
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
  private motions: Record<TrackName, TrackMotion> | null = null;
  private timing: Timing | null = null;
  private grid: BarGrid | null = null;
  private selected: TrackName = "vocals";
  private section = 0;
  private formats: ExportFormat[] = [];
  private paramsDirty = false;
  private eqTimer = 0;
  private eqSeq = 0;
  private readonly brirCache = new Map<RoomName, ArrayBuffer>();
  private loadedRoom: RoomName | null = null;
  private readonly views = new Map<TrackName, OrbitView>();
  private readonly dome = new Dome();
  private readonly engine = new AudioEngine();
  private stage!: Stage;
  private handles!: Handles;
  private tracksPanel!: TracksPanel;
  private orbitPanel!: OrbitPanel;
  private timeline!: TimelineStrip;
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
    this.timeline = new TimelineStrip({
      seek: (t) => void this.seek(t),
      moveBoundary: (k, t) => this.editScene((s) => moveBoundary(s, k, t, this.grid!, this.duration)),
    });
    this.transport = new Transport(() => void this.togglePlay(), this.timeline.el);
    root.replaceChildren(viewport, header, this.transport.el, h("p", { class: "credits" }, TEXT.credits), this.overlay.el, this.exportDialog.el, this.toastBox);
    this.overlay.showProgress("正在启动", null);

    try {
      this.stage = new Stage(viewport, visualRadius);
      const [health, schema, presets, hrtf, head] = await Promise.all([api.health(), api.schema(), api.presets(), api.hrtf(), loadHead()]);
      this.formats = health.formats;
      this.stage.scene.add(head, this.dome.group);
      this.dome.visible = storage()?.getItem(DOME_KEY) !== "0";
      await this.engine.init(hrtf);
      this.engine.onEnded = () => this.transport.update(this.engine.time, this.engine.duration, false);
      const ranges = SchemaRanges.from(schema);
      const actions = this.panelActions();
      this.tracksPanel = new TracksPanel(actions, presets);
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
        orbit: (t) => this.scene!.sections[this.section].orbits[t],
        playing: () => this.engine.playing,
        phase: (t) => (this.motions ? phaseAt(this.motions[t], this.engine.time) : 0),
        select: (t) => this.select(t),
        change: (t, patch) => this.mutate((s) => Object.assign(s.sections[this.section].orbits[t], patch)),
      });
      this.stage.onFrame(() => this.tick());
      window.addEventListener("keydown", (e) => this.onKey(e));
      await this.restoreLastProject();
    } catch (err) {
      this.fail(err);
    }
  }

  private get duration(): number {
    return this.timing?.durationS ?? 0;
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

  private toast(message: string, kind: "error" | "info" = "error"): void {
    const el = h("div", { class: `toast ${kind}` }, message);
    this.toastBox.append(el);
    window.setTimeout(() => el.remove(), TOAST_MS);
  }

  // ---------- 导入与加载 ----------
  private async restoreLastProject(): Promise<void> {
    const id = storage()?.getItem(LAST_PROJECT_KEY);
    if (id) {
      try {
        let project = await api.project(id);
        if (BUSY.has(project.state)) {
          this.go("processing"); // 例如分析算法升级后，后台正在重新分析这首歌
          project = await this.waitReady(project);
        }
        if (project.state === "READY") {
          await this.loadProject(project);
          return;
        }
      } catch (err) {
        console.warn("[orbit8d] 上次的歌已不可用", err);
        storage()?.removeItem(LAST_PROJECT_KEY);
        if (this.phase !== "booting") {
          this.fail(err);
          return;
        }
      }
    }
    this.go("empty");
    this.overlay.showDrop();
  }

  private async waitReady(project: Project): Promise<Project> {
    const done = await poll(
      () => api.project(project.id),
      (p) => p.state === "READY" || p.state === "FAILED",
      (p) => this.overlay.showProgress(PROJECT_STATE_LABEL[p.state], this.overallProgress(p), p.source.filename),
    );
    if (done.state === "FAILED") {
      throw new ApiError(0, done.error?.code ?? "FAILED", done.error?.message ?? PROJECT_STATE_LABEL.FAILED);
    }
    return done;
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
        project = await this.waitReady(project);
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

  /** 新项目默认用自动编排；万一取不到就退回经典预设。 */
  private async initialScene(project: Project): Promise<Scene> {
    try {
      return await api.choreography(project.id);
    } catch (err) {
      console.warn("[orbit8d] 自动编排不可用，改用经典预设", err);
      return api.preset(FALLBACK_PRESET, project.analysis!.default_bars);
    }
  }

  private async loadProject(project: Project): Promise<void> {
    const analysis = project.analysis;
    if (!analysis) throw new Error("项目缺少分析结果");
    this.go("loading");
    this.overlay.showProgress("加载试听音频", null, project.source.filename);
    const buffers = await Promise.all(STEM_NAMES.map(async (n) => [n, await api.stem(project.id, n)] as const));
    await this.engine.loadStems(Object.fromEntries(buffers));
    const scene = await this.initialScene(project);
    this.project = project;
    this.timing = { bpmNorm: analysis.bpm_norm, tRef: analysis.t_ref, durationS: analysis.duration_s };
    this.grid = barGrid(analysis.bpm_norm, analysis.t_ref);
    this.selected = "vocals";
    this.section = 0;
    await this.ensureRoom(scene.room.name);
    await this.engine.setEq(await api.sceneEq(project.id, scene));
    this.applyScene(scene, false);
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
    return toOrbitParams(this.scene!.sections[this.section].orbits[track], this.timing?.bpmNorm ?? 120);
  }

  private panelActions(): PanelActions {
    const inSection = (fn: (s: Scene, k: number) => void) => this.mutate((s) => fn(s, this.section));
    return {
      select: (t) => this.select(t),
      setMix: (t, patch: Partial<Mix>) => this.mutate((s) => Object.assign(s.mix[t], patch)),
      setOrbit: (t, patch: Partial<Orbit>) => inSection((s, k) => Object.assign(s.sections[k].orbits[t], patch)),
      setSpeed: (t, patch: Partial<Speed>) => inSection((s, k) => Object.assign(s.sections[k].orbits[t].speed, patch)),
      setRoom: (patch: Partial<Room>) => this.mutate((s) => Object.assign(s.room, patch)),
      setRearDarken: (db) => this.mutate((s) => (s.rear_darken_db = db)),
      applyPreset: (name) => void this.applyPresetToSection(name),
      autoChoreograph: () => void this.autoChoreograph(),
      setSectionLabel: (label) => inSection((s, k) => (s.sections[k].label = label)),
      setSectionWet: (db) => inSection((s, k) => (s.sections[k].wet_db = db)),
      splitSection: () => this.tryEdit((s) => splitAt(s, this.engine.time, this.grid!, this.duration)),
      removeSection: () => this.tryEdit((s) => removeSection(s, this.section)),
      addEvent: (kind: EventKind) => this.tryEdit((s) => addEvent(s, kind, this.engine.time, this.selected, this.grid!)),
      removeEvent: (index) => this.editScene((s) => removeEvent(s, index)),
      resetView: () => this.stage.resetView(),
      domeVisible: () => this.dome.visible,
      toggleDome: (visible) => {
        this.dome.visible = visible;
        storage()?.setItem(DOME_KEY, visible ? "1" : "0");
      },
    };
  }

  private select(track: TrackName): void {
    this.selected = track;
    if (this.scene) this.syncViews();
  }

  private mutate(fn: (draft: Scene) => void): void {
    if (!this.scene) return;
    const next = structuredClone(this.scene);
    fn(next);
    this.applyScene(next);
  }

  /** 纯函数编辑：返回同一个对象表示没变。 */
  private editScene(fn: (s: Scene) => Scene): void {
    if (!this.scene || !this.grid) return;
    const next = fn(this.scene);
    if (next !== this.scene) this.applyScene(next);
  }

  private tryEdit(fn: (s: Scene) => EditResult): void {
    if (!this.scene || !this.grid) return;
    const r = fn(this.scene);
    if (r.ok) this.applyScene(r.scene);
    else this.toast(r.reason);
  }

  private async applyPresetToSection(name: string): Promise<void> {
    const analysis = this.project?.analysis;
    if (!analysis || !this.scene) return;
    try {
      const preset = await api.preset(name, analysis.default_bars);
      const next = applyPreset(this.scene, preset, this.section);
      await this.ensureRoom(next.room.name);
      this.applyScene(next);
    } catch (err) {
      this.fail(err);
    }
  }

  private async autoChoreograph(): Promise<void> {
    if (!this.project) return;
    try {
      const scene = await api.choreography(this.project.id);
      await this.ensureRoom(scene.room.name);
      this.applyScene(scene);
      this.toast(TEXT.autoChoreoDone, "info");
    } catch (err) {
      this.fail(err);
    }
  }

  private applyScene(scene: Scene, refreshEq = true): void {
    if (!this.timing) return;
    this.scene = scene;
    this.motions = compileMotions(scene, this.timing);
    this.section = Math.min(sectionIndexAt(scene, this.engine.time), scene.sections.length - 1);
    this.syncViews();
    this.paramsDirty = true;
    if (refreshEq) this.scheduleEq();
    if (scene.room.name !== this.loadedRoom) void this.ensureRoom(scene.room.name).catch((err) => this.fail(err));
  }

  /** 3D 轨道形状、面板、时间轴都显示播放头所在的那一段。 */
  private syncViews(): void {
    const scene = this.scene;
    const analysis = this.project?.analysis;
    if (!scene || !analysis) return;
    const gains = effectiveTrackGains(scene);
    for (const track of TRACKS) {
      this.views.get(track)?.update({
        params: this.orbitParams(track),
        widthDeg: scene.mix[track].width_deg,
        stereo: STEREO.has(track),
        audible: gains[track] > 0,
        selected: track === this.selected,
      });
    }
    const selectedOrbit = scene.sections[this.section].orbits[this.selected];
    this.dome.setRadius(visualRadius(selectedOrbit.radius_m));
    this.dome.setActiveLayer(layerOf(selectedOrbit.height_deg));
    const ctx: PanelContext = { scene, track: this.selected, section: this.section, analysis, duration: this.duration };
    this.tracksPanel.sync(ctx);
    this.orbitPanel.sync(ctx);
    this.timeline.setScene(scene, this.duration, this.section);
  }

  /** 场景改完 250 ms 没再改，就向后端要这个场景的补偿 EQ；只用最后一次请求的结果。 */
  private scheduleEq(): void {
    window.clearTimeout(this.eqTimer);
    this.eqTimer = window.setTimeout(() => void this.refreshEq(), EQ_DEBOUNCE_MS);
  }

  private async refreshEq(): Promise<void> {
    const project = this.project;
    const scene = this.scene;
    if (!project || !scene) return;
    const seq = ++this.eqSeq;
    try {
      const wav = await api.sceneEq(project.id, scene);
      if (seq === this.eqSeq) await this.engine.setEq(wav);
    } catch (err) {
      console.warn("[orbit8d] 更新补偿 EQ 失败，继续用上一个", err);
    }
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
    if (!this.scene || !analysis || !this.project || !this.timing || !this.motions) return;
    if (this.paramsDirty) {
      this.paramsDirty = false;
      const params = buildRenderParams(this.scene, this.timing, analysis.calibration, this.project.preview_scale);
      this.engine.setParams(params, analysis.preview_gain);
    }
    const t = this.engine.time;
    const k = sectionIndexAt(this.scene, t);
    if (k !== this.section) {
      this.section = k; // 播放头进入新的一段：轨道形状与面板跟着切换
      this.syncViews();
    }
    const frame = this.stage.frameInfo();
    for (const [track, view] of this.views) view.setPositions(this.motions[track], t, frame);
    this.handles.update();
    this.timeline.setTime(t);
    this.transport.update(t, this.engine.duration, this.engine.playing);
    this.transport.setLevels(this.engine.levels());
  }

  private async seek(t: number): Promise<void> {
    if (this.phase !== "ready") return;
    try {
      await this.engine.seek(t);
    } catch (err) {
      this.fail(err);
    }
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
    if (["INPUT", "BUTTON", "SELECT", "TEXTAREA"].includes(target.tagName)) return;
    if (e.code === "Space") {
      e.preventDefault();
      void this.togglePlay();
    } else if (e.code === "ArrowLeft" || e.code === "ArrowRight") {
      e.preventDefault();
      void this.seek(this.engine.time + (e.code === "ArrowLeft" ? -SEEK_STEP_S : SEEK_STEP_S));
    }
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
