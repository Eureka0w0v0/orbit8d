// 总控：界面阶段状态机 + 场景数据（分段时间轴）+ 3D 视图 + 试听引擎 + 导入导出流程。

import { ApiError, api, poll } from "./api";
import { T, errorText, lang, switchLang, type Lang } from "./i18n";
import { AudioEngine, STEM_NAMES, type ListenMode } from "./audio/engine";
import { buildRenderParams, compileMotions, effectiveTrackGains, type Timing } from "./audio/params";
import { toOrbitParams, type OrbitParams } from "./orbit/orbit";
import { phaseAt, type TrackMotion } from "./orbit/timeline";
import { Dome } from "./scene/dome";
import {
  MAX_EVENTS,
  MAX_SECTIONS,
  addEvent,
  applyPreset,
  barGrid,
  moveBoundary,
  moveEvent,
  removeEvent,
  removeSection,
  resizeEvent,
  sectionIndexAt,
  splitAt,
  type BarGrid,
  type EditReason,
  type EditResult,
} from "./scene/edit";
import { History } from "./scene/history";
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
import { ExportDialog, Overlay, Transport, languageSelect } from "./ui/widgets";

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
const SAVE_DEBOUNCE_MS = 800;
const SAVE_RETRY_MS = 5000;
const ORIGINAL_PREVIEW = "original";
const SEEK_STEP_S = 5;
const DEFAULT_RADIUS_M = 1.2; // 还没打开歌时三层半球按默认轨道距离画（与后端 Orbit.radius_m 默认值一致）
const STAGE_SPAN: Partial<Record<Project["state"], [number, number]>> = {
  UPLOADED: [0, 0.02],
  DECODING: [0.02, 0.06],
  SEPARATING: [0.06, 0.88],
  ANALYZING: [0.88, 1],
};
const BUSY: ReadonlySet<Project["state"]> = new Set(["UPLOADED", "DECODING", "SEPARATING", "ANALYZING"]);
const STEREO: ReadonlySet<TrackName> = new Set(["drums", "other"]);
const TEXT_ENTRY = new Set(["TEXTAREA", "SELECT"]);
const TEXT_INPUT_TYPES = new Set(["text", "search", "number", "email", "password", "url"]);

type SaveState = "saved" | "saving" | "failed";

/** 可以打字的地方不抢快捷键（本应用目前没有文本框，防将来加了以后误触）。 */
function isTextEntry(el: HTMLElement): boolean {
  if (TEXT_ENTRY.has(el.tagName) || el.isContentEditable) return true;
  return el.tagName === "INPUT" && TEXT_INPUT_TYPES.has((el as HTMLInputElement).type);
}

/** 给用户看的错误：按错误码用当前语言说明，后面带上错误码方便排查。 */
function describe(err: unknown): string {
  if (err instanceof ApiError) return `${errorText(err.code, err.message)} (${err.code})`;
  if (err instanceof Error) return err.message;
  return String(err);
}

function editMessage(reason: EditReason): string {
  if (reason === "maxSections") return T.edit.maxSections(MAX_SECTIONS);
  if (reason === "maxEvents") return T.edit.maxEvents(MAX_EVENTS);
  return T.edit[reason];
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
  private selectedEvent: number | null = null;
  private scrubTime: number | null = null; // 正在拖播放头时的预览位置
  private resumeAfterScrub = false;
  private readonly history = new History<Scene>();
  private saveTimer = 0;
  private saveSeq = 0;
  private pendingSave: { projectId: string; scene: Scene } | null = null;
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
  private undoButton!: HTMLButtonElement;
  private redoButton!: HTMLButtonElement;
  private saveStatus!: HTMLElement;

  async start(root: HTMLElement): Promise<void> {
    const viewport = h("div", { class: "viewport" });
    this.title = h("span", { class: "song" });
    this.undoButton = h("button", { type: "button", class: "ghost icon", title: TEXT.undo, disabled: true, onclick: () => this.undo() }, "↩\uFE0E");
    this.redoButton = h("button", { type: "button", class: "ghost icon", title: TEXT.redo, disabled: true, onclick: () => this.redo() }, "↪\uFE0E");
    this.saveStatus = h("span", { class: "save-status" });
    const header = h(
      "header",
      { class: "topbar" },
      h("span", { class: "logo" }, TEXT.appName),
      this.title,
      this.undoButton,
      this.redoButton,
      this.saveStatus,
      languageSelect((next) => void this.changeLanguage(next)),
      h("button", { type: "button", class: "ghost", onclick: () => this.overlay.pickFile() }, TEXT.importSong),
      h("button", { type: "button", class: "primary", onclick: () => this.openExport() }, TEXT.export),
    );
    this.toastBox = h("div", { class: "toasts" });
    this.overlay = new Overlay(
      (file) => void this.importFile(file),
      (next) => void this.changeLanguage(next),
    );
    this.exportDialog = new ExportDialog();
    this.timeline = new TimelineStrip({
      scrubStart: (t) => this.scrubStart(t),
      scrub: (t) => this.scrub(t),
      scrubEnd: (t) => void this.scrubEnd(t),
      moveBoundary: (k, t) => this.editScene((s) => moveBoundary(s, k, t, this.grid!, this.duration)),
      selectEvent: (index) => this.selectEvent(index),
      moveEvent: (index, anchor, free) => this.editScene((s) => moveEvent(s, index, anchor, this.grid!, this.duration, free)),
      resizeEvent: (index, end, free) => this.editScene((s) => resizeEvent(s, index, end, this.grid!, this.duration, free)),
    });
    this.transport = new Transport(() => void this.togglePlay(), this.timeline.el, () => this.toggleListen());
    const badge = h("div", { class: "listen-badge" }, TEXT.listeningOriginal);
    root.replaceChildren(viewport, badge, header, this.transport.el, h("p", { class: "credits" }, TEXT.credits), this.overlay.el, this.exportDialog.el, this.toastBox);
    window.addEventListener("pagehide", () => void this.flushSave(true)); // 关页面前把没存的改动发出去
    this.overlay.showProgress(TEXT.starting, null);

    try {
      this.stage = new Stage(viewport, visualRadius);
      const [health, schema, presets, hrtf, head] = await Promise.all([api.health(), api.schema(), api.presets(), api.hrtf(), loadHead()]);
      this.formats = health.formats;
      this.stage.scene.add(head, this.dome.group);
      this.dome.setRadius(visualRadius(DEFAULT_RADIUS_M));
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
    if (!PHASE_TRANSITIONS[this.phase].includes(next)) throw new Error(`Illegal UI phase transition: ${this.phase} -> ${next}`);
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
        console.warn("[orbit8d] the last project is no longer available", err);
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

  /** 优先用这首歌上次保存的场景；没保存过用自动编排；万一取不到就退回经典预设。 */
  private async initialScene(project: Project): Promise<Scene> {
    try {
      return (await api.savedScene(project.id)) ?? (await api.choreography(project.id));
    } catch (err) {
      console.warn("[orbit8d] auto choreography unavailable, using the classic preset", err);
      return api.preset(FALLBACK_PRESET, project.analysis!.default_bars);
    }
  }

  private async loadProject(project: Project): Promise<void> {
    const analysis = project.analysis;
    if (!analysis) throw new Error("The project has no analysis");
    this.go("loading");
    this.overlay.showProgress(TEXT.loadingAudio, null, project.source.filename);
    await this.flushSave(); // 换歌前先把上一首没存的改动存掉
    const [buffers, original] = await Promise.all([
      Promise.all(STEM_NAMES.map(async (n) => [n, await api.stem(project.id, n)] as const)),
      api.stem(project.id, ORIGINAL_PREVIEW),
    ]);
    await this.engine.loadStems(Object.fromEntries(buffers));
    await this.engine.loadOriginal(original, analysis.original_gain * project.preview_scale);
    this.setListen("8d");
    const scene = await this.initialScene(project);
    this.project = project;
    this.timing = { bpmNorm: analysis.bpm_norm, tRef: analysis.t_ref, durationS: analysis.duration_s };
    this.grid = barGrid(analysis.bpm_norm, analysis.t_ref);
    this.selected = "vocals";
    this.section = 0;
    this.selectedEvent = null;
    this.history.clear();
    this.timeline.setEnvelope(analysis.envelope_db, analysis.envelope_hop_s);
    await this.ensureRoom(scene.room.name);
    await this.engine.setEq(await api.sceneEq(project.id, scene));
    this.applyScene(scene, { record: false, save: false, refreshEq: false });
    this.setSaveState("saved");
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
      removeEvent: (index) => {
        this.selectedEvent = null;
        this.editScene((s) => removeEvent(s, index));
      },
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
    else this.toast(editMessage(r.reason));
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

  /**
   * 换上新场景。record：进撤销记录（撤销 / 重做本身和刚打开时不记）；save：自动保存；refreshEq：重取补偿 EQ。
   */
  private applyScene(scene: Scene, opts: { record?: boolean; save?: boolean; refreshEq?: boolean } = {}): void {
    const { record = true, save = true, refreshEq = true } = opts;
    if (!this.timing) return;
    if (record && this.scene && scene !== this.scene) this.history.record(this.scene, performance.now());
    if (this.selectedEvent !== null && this.selectedEvent >= scene.events.length) this.selectedEvent = null;
    this.scene = scene;
    this.motions = compileMotions(scene, this.timing);
    this.section = Math.min(sectionIndexAt(scene, this.engine.time), scene.sections.length - 1);
    this.syncViews();
    this.paramsDirty = true;
    if (refreshEq) this.scheduleEq();
    if (save) this.scheduleSave();
    this.updateUndoButtons();
    if (scene.room.name !== this.loadedRoom) void this.ensureRoom(scene.room.name).catch((err) => this.fail(err));
  }

  // ---------- 语言：先把没存的改动存掉，再记住选择并刷新页面 ----------
  private async changeLanguage(next: Lang): Promise<void> {
    if (next === lang) return;
    await this.flushSave();
    switchLang(next);
  }

  // ---------- 撤销 / 重做 ----------
  private undo(): void {
    if (!this.scene || this.phase !== "ready") return;
    const previous = this.history.undo(this.scene);
    if (!previous) return;
    this.selectedEvent = null; // 事件下标可能变了
    this.applyScene(previous, { record: false });
  }

  private redo(): void {
    if (!this.scene || this.phase !== "ready") return;
    const next = this.history.redo(this.scene);
    if (!next) return;
    this.selectedEvent = null;
    this.applyScene(next, { record: false });
  }

  private updateUndoButtons(): void {
    this.undoButton.disabled = !this.history.canUndo;
    this.redoButton.disabled = !this.history.canRedo;
  }

  // ---------- 自动保存（整份替换，只保存最后一次；失败隔几秒重试） ----------
  private scheduleSave(): void {
    if (!this.project || !this.scene) return;
    this.pendingSave = { projectId: this.project.id, scene: this.scene };
    this.setSaveState("saving");
    window.clearTimeout(this.saveTimer);
    this.saveTimer = window.setTimeout(() => void this.flushSave(), SAVE_DEBOUNCE_MS);
  }

  private async flushSave(keepalive = false): Promise<void> {
    window.clearTimeout(this.saveTimer);
    const job = this.pendingSave;
    if (!job) return;
    this.pendingSave = null;
    const seq = ++this.saveSeq;
    try {
      await api.saveScene(job.projectId, job.scene, keepalive);
      if (seq === this.saveSeq && !this.pendingSave) this.setSaveState("saved");
    } catch (err) {
      console.warn("[orbit8d] saving the scene failed, retrying later", err);
      if (seq !== this.saveSeq) return; // 已经有更新的保存在进行
      this.pendingSave ??= job;
      this.setSaveState("failed");
      this.saveTimer = window.setTimeout(() => void this.flushSave(), SAVE_RETRY_MS);
    }
  }

  private setSaveState(state: SaveState): void {
    this.saveStatus.textContent = state === "saved" ? TEXT.saved : state === "saving" ? TEXT.saving : TEXT.saveFailed;
    this.saveStatus.dataset.state = state;
  }

  // ---------- 原曲 / 8D 对比 ----------
  private toggleListen(): void {
    if (this.phase !== "ready") return;
    this.setListen(this.engine.listening === "8d" ? "original" : "8d");
  }

  private setListen(mode: ListenMode): void {
    this.engine.setListen(mode);
    document.body.dataset.listen = this.engine.listening;
    this.transport.setListen(this.engine.listening, true);
  }

  // ---------- 拖播放头：拖动中只预览（3D、面板、时间都跟着走），松手才跳；播放中先暂停、松手接着播 ----------
  private scrubStart(t: number): void {
    if (this.phase !== "ready") return;
    this.resumeAfterScrub = this.engine.playing;
    if (this.resumeAfterScrub) this.engine.pause();
    this.scrubTime = t;
  }

  private scrub(t: number): void {
    if (this.scrubTime !== null) this.scrubTime = t;
  }

  private async scrubEnd(t: number): Promise<void> {
    if (this.scrubTime === null) return;
    this.scrubTime = null;
    const resume = this.resumeAfterScrub;
    this.resumeAfterScrub = false;
    try {
      await this.engine.seek(t);
      if (resume) await this.engine.play();
    } catch (err) {
      this.fail(err);
    }
  }

  // ---------- 事件选择 ----------
  private selectEvent(index: number | null): void {
    this.selectedEvent = index;
    if (this.scene) this.timeline.setScene(this.scene, this.duration, this.section, this.selectedEvent);
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
    this.timeline.setScene(scene, this.duration, this.section, this.selectedEvent);
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
      console.warn("[orbit8d] updating the compensation EQ failed, keeping the previous one", err);
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
    const t = this.scrubTime ?? this.engine.time;
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
    if (isTextEntry(target)) return;
    if ((e.metaKey || e.ctrlKey) && (e.code === "KeyZ" || e.code === "KeyY")) {
      e.preventDefault();
      if (e.code === "KeyY" || e.shiftKey) this.redo();
      else this.undo();
      return;
    }
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.code === "KeyB") {
      e.preventDefault();
      this.toggleListen();
      return;
    }
    if ((e.code === "Delete" || e.code === "Backspace") && this.selectedEvent !== null && this.phase === "ready") {
      e.preventDefault();
      const index = this.selectedEvent;
      this.selectedEvent = null;
      this.editScene((s) => removeEvent(s, index));
      return;
    }
    if (e.code === "Escape" && this.selectedEvent !== null) {
      this.selectEvent(null);
      return;
    }
    if (["INPUT", "BUTTON", "SELECT"].includes(target.tagName)) return; // 空格、方向键留给聚焦的控件
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
      this.toast(TEXT.importFirst);
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
