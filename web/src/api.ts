// 后端 HTTP 接口（docs/SPEC.md §7）。所有失败都抛 ApiError，调用方负责给用户看得懂的提示。

import type { ApiErrorBody, Bars, ExportFormat, ExportRecord, Project, RoomName, Scene } from "./types";

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function check(resp: Response): Promise<Response> {
  if (resp.ok) return resp;
  let body: Partial<ApiErrorBody> = {};
  try {
    body = (await resp.json()) as ApiErrorBody;
  } catch {
    body = { code: `HTTP_${resp.status}`, message: resp.statusText };
  }
  throw new ApiError(resp.status, body.code ?? `HTTP_${resp.status}`, body.message ?? resp.statusText);
}

async function getJson<T>(url: string): Promise<T> {
  return (await (await check(await fetch(url))).json()) as T;
}

async function getBytes(url: string): Promise<ArrayBuffer> {
  return (await check(await fetch(url))).arrayBuffer();
}

export const api = {
  health: () => getJson<{ ok: boolean; version: string; formats: ExportFormat[] }>("/api/health"),
  schema: () => getJson<Record<string, unknown>>("/api/scene/schema"),
  presets: () => getJson<string[]>("/api/presets"),
  preset: (name: string, bars: Bars) => getJson<Scene>(`/api/presets/${name}?bars=${bars}`),
  project: (id: string) => getJson<Project>(`/api/projects/${id}`),
  stem: (id: string, name: string) => getBytes(`/api/projects/${id}/stems/${name}.flac`),
  hrtf: () => getBytes("/api/assets/hrtf.bin"),
  brir: (room: RoomName) => getBytes(`/api/assets/brir/${room}.wav`),
  choreography: (id: string) => getJson<Scene>(`/api/projects/${id}/choreography`),

  /** 这首歌上次保存的场景；没保存过返回 null。 */
  async savedScene(id: string): Promise<Scene | null> {
    const resp = await fetch(`/api/projects/${id}/scene`);
    if (resp.status === 404) return null;
    return (await (await check(resp)).json()) as Scene;
  },

  /** 整份替换保存。keepalive：页面关闭时也尽量发出去（请求体 ≤ 64 KB）。 */
  async saveScene(id: string, scene: Scene, keepalive = false): Promise<void> {
    const resp = await fetch(`/api/projects/${id}/scene`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(scene),
      keepalive,
    });
    await check(resp);
  },

  /** 这个场景的补偿 EQ（单声道 WAV 冲激响应），导出用的是同一个。 */
  async sceneEq(projectId: string, scene: Scene): Promise<ArrayBuffer> {
    const resp = await fetch(`/api/projects/${projectId}/eq`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scene }),
    });
    return (await check(resp)).arrayBuffer();
  },
  exportRecord: (id: string) => getJson<ExportRecord>(`/api/exports/${id}`),
  exportUrl: (id: string) => `/api/exports/${id}/file`,

  async createExport(projectId: string, scene: Scene, format: ExportFormat): Promise<ExportRecord> {
    const resp = await fetch(`/api/projects/${projectId}/exports`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scene, format }),
    });
    return (await (await check(resp)).json()) as ExportRecord;
  },

  /** 用 XHR 上传以便显示进度（fetch 没有上传进度事件）。 */
  upload(file: File, onProgress: (fraction: number) => void): Promise<Project> {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/projects");
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
      xhr.onerror = () => reject(new ApiError(0, "NETWORK", "连不上本地服务，请确认 Orbit 8D 后台在运行"));
      xhr.onload = () => {
        let body: unknown = null;
        try {
          body = JSON.parse(xhr.responseText);
        } catch {
          body = null;
        }
        if (xhr.status >= 200 && xhr.status < 300) resolve(body as Project);
        else {
          const err = (body ?? {}) as Partial<ApiErrorBody>;
          reject(new ApiError(xhr.status, err.code ?? `HTTP_${xhr.status}`, err.message ?? "上传失败"));
        }
      };
      xhr.send(file);
    });
  },
};

/** 按固定间隔轮询，直到 done 返回 true。 */
export async function poll<T>(fetcher: () => Promise<T>, done: (v: T) => boolean, onUpdate: (v: T) => void, intervalMs = 700): Promise<T> {
  for (;;) {
    const value = await fetcher();
    onUpdate(value);
    if (done(value)) return value;
    await new Promise((r) => window.setTimeout(r, intervalMs));
  }
}
