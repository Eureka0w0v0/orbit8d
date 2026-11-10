// 总控：界面阶段状态机 + 场景数据（分段时间轴）+ 3D 视图 + 试听引擎 + 导入导出流程。

import { ApiError, api, poll } from "./api";
import { T, errorText, lang, switchLang, type Lang } from "./i18n";
import { AudioEngine } from "./audio/engine";
import { STEM_NAMES, buildRenderParams, compileMotions } from "./audio/params";
import { toOrbitParams, type OrbitParams } from "./orbit/orbit";
import { phaseAt, type TrackMotion } from "./orbit/timeline";
import { Dome } from "./scene/dome";
import {
  MAX_EVENTS,
  MAX_SECTIONS,
  addEvent,
  applyPreset,
  moveBoundary,
  moveEvent,
  removeEvent,
  removeSection,
  resizeEvent,
  sectionIndexAt,
  splitAt,
  type EditReason,
  type EditResult,
} from "./model/edit";
import { History } from "./model/history";
import { layerOf } from "./model/layers";
import { effectiveTrackGains } from "./model/mix";
import { STEREO_TRACKS } from "./model/tracks";
import { Handles } from "./scene/handles";
import { loadHead } from "./scene/head";
import { visualRadius } from "./scene/mapping";
import { OrbitView } from "./scene/orbits";
import { Stage } from "./scene/stage";
import { Autosave, type SaveState } from "./session/autosave";
import { EqSync } from "./session/eq-sync";
import { ExportRun } from "./session/export-run";
import { openSong, titleOf, type OpenSong } from "./session/song";
import type { EventKind, ExportFormat, ListenMode, Loudness, Mix, Orbit, Project, ProjectSummary, Room, Scene, Speed, TrackName } from "./types";
import { TRACKS } from "./types";
import { h } from "./ui/dom";
import { ExportDialog } from "./ui/export-dialog";
import { EXPORT_STATE_LABEL, PROJECT_STATE_LABEL, TEXT } from "./ui/labels";
import { languageSelect } from "./ui/language";
import { Overlay } from "./ui/overlay";
import { OrbitPanel, TracksPanel, type PanelActions, type PanelContext } from "./ui/panels";
import { SchemaRanges } from "./ui/schema";
import { shortcutFor, type Shortcut } from "./ui/shortcuts";
import { TimelineStrip } from "./ui/timeline";
import { canGo, type Phase } from "./ui/phase";
import { BUSY, describe, progressOf } from "./ui/progress";
import { SongMenu, songLabel, songList, type SongActions } from "./ui/songs";
import { Transport } from "./ui/transport";

const FALLBACK_PRESET = "classic";
const LAST_PROJECT_KEY = "orbit8d.lastProject";
const DOME_KEY = "orbit8d.showDome";
const TOAST_MS = 4000;
const ORIGINAL_PREVIEW = "original";
const SEEK_STEP_S = 5;
const DEFAULT_RADIUS_M = 1.2; // 还没打开歌时三层半球按默认轨道距离画（与后端 Orbit.radius_m 默认值一致）

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
  private song: OpenSong | null = null; // 打开着的歌；scene / motions 与它同时有值
  private scene: Scene | null = null;
  private motions: Record<TrackName, TrackMotion> | null = null;
  private selected: TrackName = "vocals";
  private section = 0;
  private formats: ExportFormat[] = [];
  private paramsDirty = false;
  private selectedEvent: number | null = null;
  private scrubTime: number | null = null; // 正在拖播放头时的预览位置
  private drawnTime = Number.NaN; // 3D 上一次画的是哪个时刻（按需重画用）
  private resumeAfterScrub = false;
  private readonly history = new History<Scene>();
  private readonly autosave = new Autosave({
    save: (job, keepalive) => api.saveScene(job.projectId, job.scene, keepalive),
    onState: (state) => this.showSaveState(state),
    onError: (err) => console.warn("[orbit8d] saving the scene failed, retrying later", err),
    setTimer: (fn, ms) => window.setTimeout(fn, ms),
    clearTimer: (id) => window.clearTimeout(id),
  });
  private readonly views = new Map<TrackName, OrbitView>();
  private readonly dome = new Dome();
  private readonly engine = new AudioEngine();
  private readonly eqSync = new EqSync({
    target: () => (this.song && this.scene ? { projectId: this.song.project.id, scene: this.scene } : null),
    fetchEq: (target) => api.sceneEq(target.projectId, target.scene),
    applyEq: (wav) => this.engine.setEq(wav),
    fetchRoom: (room) => api.brir(room),
    applyRoom: (wav) => this.engine.setRoom(wav),
    onError: (err) => console.warn("[orbit8d] updating the compensation EQ failed, keeping the previous one", err),
    setTimer: (fn, ms) => window.setTimeout(fn, ms),
    clearTimer: (id) => window.clearTimeout(id),
  });
  private readonly exporter = new ExportRun({
    create: (job) => api.createExport(job.projectId, job.scene, job.format, job.loudness),
    fetch: (id) => api.exportRecord(id),
    sleep: (ms) => new Promise((resolve) => window.setTimeout(resolve, ms)),
    onProgress: (state) => this.exportDialog.progress(EXPORT_STATE_LABEL[state]),
    onDone: (rec) => this.exportDialog.done(api.exportUrl(rec.id), rec.file_name),
    onFailed: (rec) => this.exportDialog.error(TEXT.exportFailed(errorText(rec.error?.code ?? "INTERNAL", rec.error?.message ?? ""))),
    onError: (err) => this.exportDialog.error(describe(err)),
  });
  private stage!: Stage;
  private handles!: Handles;
  private tracksPanel!: TracksPanel;
  private orbitPanel!: OrbitPanel;
  private timeline!: TimelineStrip;
  private transport!: Transport;
  private overlay!: Overlay;
  private exportDialog!: ExportDialog;
  private songMenu!: SongMenu;
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
    this.songMenu = new SongMenu(async () => songList(await api.projects(), this.song?.project.id ?? null, this.songActions()));
    const header = h(
      "header",
      { class: "topbar" },
      h("span", { class: "logo" }, TEXT.appName),
      this.title,
      this.songMenu.button,
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
    this.exportDialog = new ExportDialog(() => this.exporter.cancel()); // 关掉对话框就不再等这次导出
    this.timeline = new TimelineStrip({
      scrubStart: (t) => this.scrubStart(t),
      scrub: (t) => this.scrub(t),
      scrubEnd: (t) => void this.scrubEnd(t),
      moveBoundary: (k, t) => this.editScene((s, song) => moveBoundary(s, k, t, song.grid, song.timing.durationS)),
      selectEvent: (index) => this.selectEvent(index),
      moveEvent: (index, anchor, free) => this.editScene((s, song) => moveEvent(s, index, anchor, song.grid, song.timing.durationS, free)),
      resizeEvent: (index, end, free) => this.editScene((s, song) => resizeEvent(s, index, end, song.grid, song.timing.durationS, free)),
    });
    this.transport = new Transport(() => void this.togglePlay(), this.timeline.el, () => this.toggleListen());
    const badge = h("div", { class: "listen-badge" }, TEXT.listeningOriginal);
    root.replaceChildren(viewport, badge, header, this.songMenu.el, this.transport.el, h("p", { class: "credits" }, TEXT.credits), this.overlay.el, this.exportDialog.el, this.toastBox);
    window.addEventListener("pagehide", () => void this.autosave.flush(true)); // 关页面前把没存的改动发出去
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
        orbit: (t) => this.open.scene.sections[this.section].orbits[t],
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
    return this.song?.timing.durationS ?? 0;
  }

  /** 打开着的歌与它现在的场景。只在已经有歌的情况下才会被用到（3D 把手、面板）；没有歌时调用是程序错误。 */
  private get open(): { song: OpenSong; scene: Scene } {
    if (!this.song || !this.scene) throw new Error("No song is open");
    return { song: this.song, scene: this.scene };
  }

  // ---------- 状态机 ----------
  private go(next: Phase): void {
    if (!canGo(this.phase, next)) throw new Error(`Illegal UI phase transition: ${this.phase} -> ${next}`);
    this.phase = next;
    document.body.dataset.phase = next;
  }

  private fail(err: unknown): void {
    console.error("[orbit8d]", err);
    if (canGo(this.phase, "error")) {
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
    void this.showDropPage();
  }

  /** 导入页：拖入区 + 最近的歌（列表取不到时只显示拖入区）。 */
  private async showDropPage(): Promise<void> {
    this.overlay.showDrop();
    try {
      const songs = await api.projects();
      if (songs.length && this.phase === "empty") this.overlay.showDrop(songList(songs, null, this.songActions()));
    } catch (err) {
      console.warn("[orbit8d] the recent songs list is unavailable", err);
    }
  }

  private songActions(): SongActions {
    return { open: (id) => void this.openSong(id), remove: (song) => void this.deleteSong(song) };
  }

  /** 从最近的歌里打开一首（导入页或顶栏 ▾ 菜单）。 */
  private async openSong(id: string): Promise<void> {
    if (!canGo(this.phase, "loading")) return;
    this.songMenu.close();
    this.engine.pause();
    try {
      const project = await api.project(id);
      if (project.state !== "READY") throw new ApiError(409, "PROJECT_NOT_READY", PROJECT_STATE_LABEL[project.state]);
      await this.loadProject(project);
    } catch (err) {
      this.fail(err);
    }
  }

  /** 删歌（连同导出）。删的是正在用的歌：没存的改动直接丢掉，回到导入页。 */
  private async deleteSong(song: ProjectSummary): Promise<void> {
    const current = this.song?.project.id === song.id;
    const remove = () => api.deleteProject(song.id);
    try {
      await (current ? this.autosave.discardWhile(remove) : remove());
    } catch (err) {
      this.toast(describe(err));
      if (this.songMenu.isOpen) await this.songMenu.refresh();
      return;
    }
    if (storage()?.getItem(LAST_PROJECT_KEY) === song.id) storage()?.removeItem(LAST_PROJECT_KEY);
    this.toast(TEXT.songDeleted(songLabel(song)), "info");
    if (current) this.closeProject();
    else if (this.phase === "empty") void this.showDropPage();
    else if (this.songMenu.isOpen) await this.songMenu.refresh();
  }

  private closeProject(): void {
    this.engine.pause();
    this.songMenu.close();
    this.exportDialog.close();
    this.song = null;
    this.scene = null;
    this.motions = null;
    this.history.clear();
    this.updateUndoButtons();
    this.title.textContent = "";
    this.transport.setInfo("");
    for (const view of this.views.values()) view.group.visible = false;
    this.stage.invalidate();
    this.go("empty");
    void this.showDropPage();
  }

  private async waitReady(project: Project): Promise<Project> {
    const done = await poll(
      () => api.project(project.id),
      (p) => p.state === "READY" || p.state === "FAILED",
      (p) => {
        const { title, fraction } = progressOf(p);
        this.overlay.showProgress(title, fraction, p.source.filename);
      },
    );
    if (done.state === "FAILED") {
      throw new ApiError(0, done.error?.code ?? "FAILED", done.error?.message ?? PROJECT_STATE_LABEL.FAILED);
    }
    return done;
  }

  private async importFile(file: File): Promise<void> {
    if (!canGo(this.phase, "uploading")) return;
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

  /** 优先用这首歌上次保存的场景；没保存过用自动编排；万一取不到就退回经典预设。 */
  private async initialScene(song: OpenSong): Promise<Scene> {
    try {
      return (await api.savedScene(song.project.id)) ?? (await api.choreography(song.project.id));
    } catch (err) {
      console.warn("[orbit8d] auto choreography unavailable, using the classic preset", err);
      return api.preset(FALLBACK_PRESET, song.analysis.default_bars);
    }
  }

  private async loadProject(project: Project): Promise<void> {
    const next = openSong(project);
    const { analysis } = next;
    this.go("loading");
    this.overlay.showProgress(TEXT.loadingAudio, null, project.source.filename);
    await this.autosave.flush(); // 换歌前先把上一首没存的改动存掉
    const [buffers, original] = await Promise.all([
      Promise.all(STEM_NAMES.map(async (n) => [n, await api.stem(project.id, n)] as const)),
      api.stem(project.id, ORIGINAL_PREVIEW),
    ]);
    await this.engine.loadStems(Object.fromEntries(buffers));
    await this.engine.loadOriginal(original, analysis.original_gain * project.preview_scale);
    this.setListen("8d");
    const scene = await this.initialScene(next);
    this.song = next;
    this.selected = "vocals";
    this.section = 0;
    this.selectedEvent = null;
    this.history.clear();
    this.timeline.setEnvelope(analysis.envelope_db, analysis.envelope_hop_s);
    await this.eqSync.ensureRoom(scene.room.name);
    await this.engine.setEq(await api.sceneEq(project.id, scene));
    this.applyScene(scene, { record: false, save: false, refreshEq: false });
    for (const view of this.views.values()) view.group.visible = true;
    this.stage.invalidate();
    this.autosave.opened();
    this.title.textContent = titleOf(project.source);
    this.transport.setInfo(`${Math.round(analysis.bpm_norm)} BPM`);
    storage()?.setItem(LAST_PROJECT_KEY, project.id);
    this.stage.resetView();
    this.go("ready");
    this.overlay.hide();
  }

  // ---------- 场景 ----------
  private orbitParams(track: TrackName): OrbitParams {
    const { song, scene } = this.open;
    return toOrbitParams(scene.sections[this.section].orbits[track], song.timing.bpmNorm);
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
      splitSection: () => this.tryEdit((s, song) => splitAt(s, this.engine.time, song.grid, song.timing.durationS)),
      removeSection: () => this.tryEdit((s) => removeSection(s, this.section)),
      addEvent: (kind: EventKind) => this.tryEdit((s, song) => addEvent(s, kind, this.engine.time, this.selected, song.grid)),
      removeEvent: (index) => {
        this.selectedEvent = null;
        this.editScene((s) => removeEvent(s, index));
      },
      resetView: () => this.stage.resetView(),
      domeVisible: () => this.dome.visible,
      toggleDome: (visible) => {
        this.dome.visible = visible;
        this.stage.invalidate();
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
  private editScene(fn: (s: Scene, song: OpenSong) => Scene): void {
    if (!this.scene || !this.song) return;
    const next = fn(this.scene, this.song);
    if (next !== this.scene) this.applyScene(next);
  }

  private tryEdit(fn: (s: Scene, song: OpenSong) => EditResult): void {
    if (!this.scene || !this.song) return;
    const r = fn(this.scene, this.song);
    if (r.ok) this.applyScene(r.scene);
    else this.toast(editMessage(r.reason));
  }

  private async applyPresetToSection(name: string): Promise<void> {
    if (!this.song || !this.scene) return;
    try {
      const preset = await api.preset(name, this.song.analysis.default_bars);
      const next = applyPreset(this.scene, preset, this.section);
      await this.eqSync.ensureRoom(next.room.name);
      this.applyScene(next);
    } catch (err) {
      this.fail(err);
    }
  }

  /** ✨ 按时间轴上现在的分界与段名重新编排（混音、房间、背后压暗不变）。 */
  private async autoChoreograph(): Promise<void> {
    if (!this.song || !this.scene) return;
    try {
      const scene = await api.rechoreograph(this.song.project.id, this.scene);
      await this.eqSync.ensureRoom(scene.room.name);
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
    if (!this.song) return;
    if (record && this.scene && scene !== this.scene) this.history.record(this.scene, performance.now());
    if (this.selectedEvent !== null && this.selectedEvent >= scene.events.length) this.selectedEvent = null;
    this.scene = scene;
    this.motions = compileMotions(scene, this.song.timing);
    this.section = Math.min(sectionIndexAt(scene, this.engine.time), scene.sections.length - 1);
    this.syncViews();
    this.paramsDirty = true;
    if (refreshEq) this.eqSync.schedule();
    if (save) this.scheduleSave();
    this.updateUndoButtons();
    if (scene.room.name !== this.eqSync.room) void this.eqSync.ensureRoom(scene.room.name).catch((err) => this.fail(err));
  }

  // ---------- 语言：先把没存的改动存掉，再记住选择并刷新页面 ----------
  private async changeLanguage(next: Lang): Promise<void> {
    if (next === lang) return;
    await this.autosave.flush();
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

  // ---------- 自动保存（规则在 session/autosave.ts；这里只交出最新的场景、显示状态） ----------
  private scheduleSave(): void {
    if (!this.song || !this.scene) return;
    this.autosave.changed({ projectId: this.song.project.id, scene: this.scene });
  }

  private showSaveState(state: SaveState): void {
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
    const song = this.song;
    if (!scene || !song) return;
    const gains = effectiveTrackGains(scene);
    for (const track of TRACKS) {
      this.views.get(track)?.update({
        params: this.orbitParams(track),
        widthDeg: scene.mix[track].width_deg,
        stereo: STEREO_TRACKS.has(track),
        audible: gains[track] > 0,
        selected: track === this.selected,
      });
    }
    const selectedOrbit = scene.sections[this.section].orbits[this.selected];
    this.dome.setRadius(visualRadius(selectedOrbit.radius_m));
    this.dome.setActiveLayer(layerOf(selectedOrbit.height_deg));
    const ctx: PanelContext = { scene, track: this.selected, section: this.section, analysis: song.analysis, duration: this.duration };
    this.tracksPanel.sync(ctx);
    this.orbitPanel.sync(ctx);
    this.timeline.setScene(scene, this.duration, this.section, this.selectedEvent);
    this.stage.invalidate();
  }

  // ---------- 每帧：返回 true 表示 3D 画面在动（播放中、拖播放头、播放位置跳了），要重画 ----------
  private tick(): boolean {
    const song = this.song;
    if (!this.scene || !song || !this.motions) return false;
    if (this.paramsDirty) {
      this.paramsDirty = false;
      const params = buildRenderParams(this.scene, song.timing, song.analysis.calibration, song.project.preview_scale);
      this.engine.setParams(params, song.analysis.preview_gain);
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
    const moved = t !== this.drawnTime;
    this.drawnTime = t;
    return moved;
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
    const context = { eventSelected: this.selectedEvent !== null, ready: this.phase === "ready" };
    const hit = shortcutFor(e, e.target as HTMLElement, context);
    if (!hit) return;
    if (hit.preventDefault) e.preventDefault();
    this.runShortcut(hit.action);
  }

  private runShortcut(action: Shortcut): void {
    switch (action) {
      case "undo":
        return this.undo();
      case "redo":
        return this.redo();
      case "toggleListen":
        return this.toggleListen();
      case "deleteEvent": {
        const index = this.selectedEvent;
        this.selectedEvent = null;
        if (index !== null) this.editScene((s) => removeEvent(s, index));
        return;
      }
      case "deselectEvent":
        return this.selectEvent(null);
      case "togglePlay":
        return void this.togglePlay();
      case "seekBack":
        return void this.seek(this.engine.time - SEEK_STEP_S);
      case "seekForward":
        return void this.seek(this.engine.time + SEEK_STEP_S);
    }
  }

  // ---------- 导出 ----------
  private openExport(): void {
    if (this.phase !== "ready" || !this.song || !this.scene) {
      this.toast(TEXT.importFirst);
      return;
    }
    this.exportDialog.open(this.formats, (format, loudness) => this.runExport(format, loudness));
  }

  private runExport(format: ExportFormat, loudness: Loudness): void {
    if (!this.song || !this.scene) return;
    void this.exporter.start({ projectId: this.song.project.id, scene: this.scene, format, loudness });
  }
}
