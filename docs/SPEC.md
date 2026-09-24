# Orbit 8D v1 技术规格（真理源）

> 代码与本文档必须一致。改代码时同步改这里。

## 1. 已冻结的需求契约

| 项 | 决定 |
|---|---|
| 形态 | 本地网页版：浏览器界面 + 本机 Python 后台，只监听 127.0.0.1 |
| 轨道 | 预设形状 + 在 3D 白模人头周围拖拽调参；自由画路径、时间轴不在 v1 |
| 分轨 | Demucs htdemucs_ft 四轨（vocals / drums / bass / other）；120 Hz 以下固定正前方 |
| 试听 | 边拖边听：浏览器 AudioWorklet 实时双耳渲染；导出走后台高质量渲染 |
| 格式 | 输入：ffmpeg 能解码的白名单格式；输出：m4a(AAC 256k) / mp3(320k) / flac(24-bit) / wav(24-bit) / ogg(Opus 192k) |
| 音质基线 | KU100 HRTF、背后压暗、16 方向扩散混响、HRTF 平均音色补偿 EQ、真峰值限幅 |

说明：本机 ffmpeg 没有 Vorbis 编码器，ogg 输出采用 Ogg Opus（兼容性相同，音质更好）。

## 2. 用户流程

1. 拖入音乐 → 解码 → 分轨（约 0.3× 时长，GPU）→ 分析（测速、校准）→ 就绪。同一文件再次导入按内容哈希直接复用。
2. 3D 场景：白色石膏人头 + 每条音轨一个彩色轨道环 + 发光声源小球，播放时小球沿轨道跑、声音同步移动。
3. 拖拽：环上的半径把手（改半径）、倾斜把手（前后/左右倾斜）、声源小球（暂停时改起点）；右侧面板改形状与参数。
4. 预设：经典 8D / 歌手绕着你转 / 双环反向 / 上下翻滚。
5. 导出：选格式 → 后台渲染 → 下载 `<原歌名> (8D).<扩展名>`。

## 3. 架构

```
web/（Vite + TypeScript + Three.js，无 UI 框架）
  src/orbit/   轨道公式（与 Python 逐点一致，共享黄金向量）
  src/audio/   AudioWorklet 双耳渲染核心 + 播放控制 + 混响/EQ 卷积
  src/scene/   白模人头、轨道环、把手、声源小球、拖拽交互
  src/ui/      音轨面板、参数面板、播放条、导入与导出
backend/orbit8d/（Python 3.12 + FastAPI）
  engine/      hrtf / orbit / render / reverb / master / tempo / scene / pipeline
  media/       ffmpeg 探测、解码、编码（参数列表调用，不经 shell）
  separate/    Demucs 分轨
  jobs/        显式状态机、文件存储（原子写）、单工位任务队列
  api/         HTTP 接口 + 托管前端构建产物
shared/golden/ 跨语言一致性测试数据（Python 生成，TS 校验）
```

单一真理源：HRTF 表、混响冲激响应（BRIR）、补偿 EQ、每轨校准增益、试听总增益全部由后台生成，前端下载同一份使用。

## 4. 坐标与轨道模型

### 4.1 坐标约定
- 头部坐标：x 向右、y 向上、z 向前（听者面朝 +z）。
- 方位角 az：从正前方**顺时针**（俯视）计，0° 正前、90° 正右、180° 正后、270° 正左。
- 仰角 el：向上为正。
- 方向向量：`x = cos(el)·sin(az)`，`y = sin(el)`，`z = cos(el)·cos(az)`。

### 4.2 相位与周期
- 速度：`bars ∈ {1,2,4,8}`（每小节 4 拍）或 `seconds ∈ [2, 30]`。
- 小节换算用的速度 `bpm_norm`：检测到的 BPM 通过 ×2 / ÷2 归一到 [70, 140)。
- 周期 `T = bars × 4 × 60 / bpm_norm` 或 `T = seconds`。
- 相位（度）：`φ(t) = start + offset + dir × 360 × (t − t_ref) / T`，`dir = +1`（顺时针）/ `−1`（逆时针），`t_ref` 为分析得到的“正前方对齐点”。
- `offset`：立体声轨拆成的两个声源分别取 `∓width/2`；单声道声源为 0。

### 4.3 形状（局部坐标，φ 用度，三角函数内换算为弧度）

| 形状 | az | el | 距离 |
|---|---|---|---|
| circle 圆 | φ | height | R |
| ellipse 椭圆 | atan2(sin φ, a·cos φ) | height | R·√(sin²φ + a²cos²φ) |
| pendulum 钟摆 | swing·sin φ | height | R |
| figure8 8 字 | swing·sin φ | height + lift·sin 2φ | R |
| spiral 螺旋 | φ | height + lift·sin(φ/4) | R |
| fixed 固定 | start + offset（与时间无关） | height | R |

之后整体旋转：`v' = Ry(yaw) · Rz(roll) · Rx(pitch) · v`
- `Rx(pitch)`（绕两耳连线）：`y' = y·cos p + z·sin p`，`z' = −y·sin p + z·cos p`（pitch > 0：前方抬高）。
- `Rz(roll)`（绕正前方轴）：`x' = x·cos r − y·sin r`，`y' = x·sin r + y·cos r`（roll > 0：右侧抬高）。
- `Ry(yaw)`（绕竖直轴，顺时针为正）：`az' = az + yaw`，等价于 `x' = x·cos w + z·sin w`，`z' = −x·sin w + z·cos w`。
- 回到角度：`az = atan2(x', z')` 归一到 [0, 360)，`el = asin(clamp(y', −1, 1))`。

### 4.4 参数范围（后端白名单校验）

| 参数 | 范围 | 默认 |
|---|---|---|
| shape | circle / ellipse / pendulum / figure8 / spiral / fixed | circle |
| radius_m | 0.5–4 | 1.2 |
| speed | {mode: bars, bars ∈ {1,2,4,8}} 或 {mode: seconds, seconds ∈ [2,30]} | 分析给出的默认小节数 |
| direction | cw / ccw | cw |
| start_deg | −360–360 | 0 |
| height_deg | −60–60 | 0 |
| pitch_deg / roll_deg | −90–90 | 0 |
| yaw_deg | −180–180 | 0 |
| aspect | 0.3–1 | 0.6 |
| swing_deg | 10–180 | 120 |
| lift_deg | 0–60 | 30 |
| gain_db | −24–12 | 0 |
| width_deg | 0–90 | vocals/bass 0，drums/other 40 |
| reverb_send | 0–1 | vocals 1，other 0.7，drums 0.4，bass 0 |
| mute / solo | bool | false |
| 全局 room | room / hall / church | hall |
| 全局 wet_db | −24–0 | −12 |
| 全局 rear_darken_db | 0–12 | 6 |

## 5. 渲染算法（导出与试听共用）

### 5.1 声源
| 声源 | 来源 | 声道 | 运动 |
|---|---|---|---|
| vocals | vocals 全频（两声道取平均） | 1 | vocals 轨道 |
| bass_hi | bass 的 120 Hz 以上（平均） | 1 | bass 轨道 |
| other_L / other_R | other 的 120 Hz 以上 | 2 | other 轨道，offset ∓width/2 |
| drums_L / drums_R | drums 的 120 Hz 以上 | 2 | drums 轨道，offset ∓width/2 |
| bass_sub / drums_sub / other_sub | 各自 120 Hz 以下（平均） | 各 1 | 固定正前方；跟随本轨音量/静音/独奏，不受轨道参数影响（静音贝斯时它的超低频也一起静音） |

分频：`low = sosfiltfilt(butter(2, 120 Hz))`，`high = x − low`（零相位、相加完全还原）。分离残差 `orig − Σstems` 并入 other。

### 5.2 每块处理（块长 32 采样；浏览器把 128 采样的渲染量子拆成 4 块）
块长 32 时方向切换误差比 128 低约 13 dB（6 秒一圈且带仰角起伏的实测：与 16 采样块相比 −53.9 dB）。
对每个运动声源、每个块 b（中心时刻 t_b）：
1. 由轨道求 `(az, el, d)`。
2. 背后压暗：`hp` 为一阶 Butterworth 高通（3 kHz，因果，状态跨块连续）；`r = max(0, −z)`，`z = cos(el)·cos(az)`；`x' = x − r·cut·hp(x)`，`cut = 1 − 10^(−rear_darken_db/20)`。
3. 距离增益：`g_d = 1 / clamp(d, 0.5, 4)`（1 m 为 0 dB）。
4. 增益：`g = g_d · 校准增益 · 10^(gain_db/20) · 静音/独奏`。
5. HRIR：在 (el, az) 网格上双线性插值（el 截断到网格范围）。
6. 分块重叠相加卷积：本块输入 × 本块 HRIR，尾巴加到后续块。

超低频：三轨按各自的音量/静音/独奏相加后，用 (0°, 0°) 的 HRIR 渲染，无压暗、无距离增益，乘 sub 校准增益。

### 5.3 混响
- 送出信号（单声道）：`Σ reverb_send × 校准增益 × 10^(gain_db/20) × 静音/独奏 × 声源信号`（不含距离增益，使远近改变直达/混响比）。
- BRIR：16 个水平方向的去相关噪声尾巴（分 5 个频段按房间 RT60 衰减，含预延迟与 8 ms 起音）分别过对应方向的 HRIR 后相加，再与 150 Hz 高通卷积，按每耳能量归一。
- 湿声 = 送出信号 ⊛ BRIR × 10^(wet_db/20)。

| 房间 | RT60（<250 / 250–1k / 1k–4k / 4k–8k / >8k Hz，秒） | 预延迟 |
|---|---|---|
| room | 0.8 / 0.7 / 0.6 / 0.45 / 0.3 | 8 ms |
| hall | 2.2 / 2.0 / 1.6 / 1.1 / 0.6 | 18 ms |
| church | 4.0 / 3.6 / 2.8 / 1.8 / 0.9 | 30 ms |

### 5.4 母带
- 补偿 EQ：水平一圈 HRTF 平均功率谱的倒数，1/3 倍频程平滑，限幅 ±6 dB，1025 点线性相位 FIR（导出时补偿 512 点延迟）。
- 导出：自动总增益 = min(到 −9 LUFS 所需增益, 让 98% 的 30 ms 片段压缩不超过 2 dB 的增益) → 前视限幅（4× 过采样估真峰值，上限 −1 dBTP）。
- 试听：总增益取分析时对“经典 8D”预设算出的值 + 浏览器 DynamicsCompressor 兜底。

### 5.5 校准增益
分析阶段用“经典 8D”预设整曲渲染一次，每个音轨的校准增益 = √(该音轨原始立体声能量 / 渲染后能量)，sub 同理。导出与试听都用这组固定增益，保证两边音量平衡一致。

### 5.6 HRTF 数据
- 来源：TH Köln Neumann KU100 `HRIR_FULL2DEG.sofa`（sofacoustics.org，sha256 `d3671e68…5ddb7`），48 kHz、128 点，89 档仰角 × 180 档方位（2°）。
- 处理：SOFA 逆时针方位转为顺时针索引 → 重采样到 44.1 kHz → 补零到 128 点 → 按水平一圈平均每耳能量归一为 1。
- 二进制格式 `hrtf.bin`：`"O8DH"` + uint32 头长 + JSON 头 `{version, sample_rate, taps, az_step_deg, n_az, el_nodes, layout:"el,az,ear,tap"}` + float32 数据（小端）。

## 6. 显式状态机

项目：
| 当前 | 允许的下一个 |
|---|---|
| UPLOADED | DECODING, FAILED |
| DECODING | SEPARATING, FAILED |
| SEPARATING | ANALYZING, FAILED |
| ANALYZING | READY, FAILED |
| READY | — |
| FAILED | DECODING（同一文件重新导入时重试） |

导出：
| 当前 | 允许的下一个 |
|---|---|
| QUEUED | RENDERING, FAILED |
| RENDERING | ENCODING, FAILED |
| ENCODING | DONE, FAILED |
| DONE | — |
| FAILED | QUEUED（同参数重新提交时重试） |

非法跳转抛 `IllegalTransition`，状态不变。每次跳转写结构化日志（trace_id = 项目 / 导出 ID）。服务重启时把未完成的记录标为 FAILED（INTERRUPTED）。

## 7. API（仅 127.0.0.1，Host 头白名单防 DNS 重绑定，跨站写请求 403）

- Host 头只接受 `127.0.0.1` / `localhost`。
- 非 GET/HEAD/OPTIONS 请求若带 Origin，必须是本服务或 Vite 开发服务器（5173）的源，否则 403（防 CSRF）。
- 错误响应统一为 `{code, message}`；路径里的 ID 必须是 16 位小写十六进制，否则 404。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/health | 版本、可用输出格式 |
| POST | /api/projects | 请求体为文件原始字节（`Content-Type: application/octet-stream`），原文件名放在 `X-Filename` 头（URL 编码，仅用于显示）；边收边写临时文件，>300 MB 立即中止（413）；ffprobe 校验格式白名单与时长 ≤ 20 分钟；项目 ID = 内容 sha256 前 16 位（重复上传直接返回，失败过的会重试） |
| GET | /api/projects/{id} | 状态、阶段进度、BPM、时长、默认小节数、t_ref、校准增益、试听总增益 |
| GET | /api/projects/{id}/stems/{vocals_hi,bass_hi,drums_hi,other_hi,bass_sub,drums_sub,other_sub}.flac | 试听用 24-bit FLAC（drums_hi/other_hi 为立体声） |
| GET | /api/assets/hrtf.bin, /api/assets/eq.wav, /api/assets/brir/{room}.wav | DSP 数据 |
| POST | /api/projects/{id}/exports | `{scene, format}`；导出 ID = sha256(项目 ID + 规范化场景 JSON + 格式) 前 16 位（幂等） |
| GET | /api/exports/{id} | 状态、进度 |
| GET | /api/exports/{id}/file | 下载 |

输入格式白名单（ffprobe 的 codec_name）：mp3、aac、alac、flac、pcm_*、vorbis、opus。

## 8. 不变量（红线）
1. 120 Hz 以下永远在正前方，两耳相同。
2. 导出时长 = 原曲时长（编码器填充 ≤ 50 ms），真峰值 ≤ −1 dBTP。
3. 同一场景：试听与导出差异 < −40 dB。
4. 同场景 + 同格式导出幂等。
5. 只监听 127.0.0.1；上传文件只按生成 ID 存放；ffmpeg 参数列表调用；上传格式白名单 + 大小/时长上限。
6. 异常一律结构化日志 + 前端可读提示，不吞异常。

## 9. 测试设计

| 模块 | 用例 | 断言 |
|---|---|---|
| engine.orbit | 各形状固定时刻 (az, el, d) | 与 `shared/golden/orbit_vectors.json` 误差 < 1e-9 |
| web orbit | 同一份黄金向量 | 误差 < 1e-6 |
| engine.hrtf | 网格点插值 = 原始 IR；90° 两耳时间差；左右对称 | < 1e-9；0.62–0.70 ms；镜像能量差 < 1.5 dB |
| engine.render | 固定方向与直接卷积；块 32 vs 16 | < −100 dB；< −45 dB |
| engine.pipeline | 超低频正前方；时长 | <100 Hz 两耳相关 > 0.999；样本数相等 |
| engine.master | 限幅后真峰值 | ≤ −1.0 dBTP |
| web audio core | 黄金渲染用例（合成 HRIR 网格 + 0.5 s 噪声） | 与 Python 输出差异 < −90 dB |
| jobs.states | 非法跳转 | 抛 IllegalTransition，状态不变 |
| api | 非音频 / 超大 / 危险文件名 / 非法 Host | 400 / 413 / 不落盘 / 400 |
| api | 同参数重复导出 | 同一导出 ID |
| e2e | 10 s 音频上传 → 就绪 → 五种格式导出 | ffprobe 可读、时长一致 |

## 10. 不在 v1 范围
自由画路径与时间轴自动化、桌面 App 打包、头部追踪、导入自定义 HRTF、手机端。

## 11. 第三方素材
- 人头模型：“Infinite, 3D Head Scan” by Lee Perry-Smith，CC BY 3.0（`web/public/models/LeePerrySmith_License.txt`）。
- HRTF：TH Köln，“A Spherical Far Field HRIR/HRTF Compilation of the Neumann KU 100”（B. Bernschütz）。
- 分轨模型：Demucs（Meta，MIT）。
