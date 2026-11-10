# Orbit 8D 技术规格（真理源，v2）

> 代码与本文档必须一致。改代码时同步改这里。

## 1. 已冻结的需求契约

| 项 | 决定 |
|---|---|
| 形态 | 本地网页版：浏览器界面 + 本机 Python 后台，只监听 127.0.0.1 |
| 轨道 | 预设形状 + 在 3D 白模人头周围拖拽调参；v2 起按段落编排：自动识别段落 → 自动编排，每段的轨道、混响量可改，可加“停顿 / 飞过头顶”事件（§13）；自由画路径不做 |
| 分轨 | Demucs htdemucs_ft 四轨（vocals / drums / bass / other）；120 Hz 以下固定正前方 |
| 试听 | 边拖边听：浏览器 AudioWorklet 实时双耳渲染；导出走后台高质量渲染 |
| 格式 | 输入：ffmpeg 能解码的白名单格式；输出：m4a(AAC 256k) / mp3(320k) / flac(24-bit) / wav(24-bit) / ogg(Opus 192k) |
| 音质基线 | KU100 HRTF、背后压暗、16 方向扩散混响 + 8 个早期反射、按场景的音色补偿 EQ（经典场景与原曲长期平均谱一致）、真峰值限幅（保动态） |

说明：本机 ffmpeg 没有 Vorbis 编码器，ogg 输出采用 Ogg Opus（兼容性相同，音质更好）。

## 2. 用户流程

1. 拖入音乐 → 解码 → 分轨（约 0.3× 时长，GPU；本机没有分轨模型时先下载，约 336 MB，界面显示“正在下载分轨模型”与进度）→ 分析（测速、校准、音色锚点、段落识别）→ 就绪。同一文件再次导入按内容哈希直接复用。分析算法升级后，旧项目在服务启动时自动重新分析（不重新分轨）。导入页下方与顶栏歌名旁的 ▾ 列出最近的歌，可以打开或删除（§10.3）。
2. 打开项目默认就是“自动编排”好的场景：播放条变成时间轴（段落色块 + 事件条），3D 轨道形状与右侧面板跟着播放头所在的段切换。
3. 3D 场景：白色石膏人头 + 每条音轨一个彩色轨道环 + 一个表示声音位置的彩色小点（不发光），播放时色点沿轨道跑、声音同步移动。
4. 拖拽：3D 里轨道左侧的白色小圆环改距离，暂停时拖色点改起点；朝向用右侧面板的“朝向”快捷键和倾斜盘调整。改的都是播放头所在的那一段。
5. 预设（只有一段时整个替换；多段时只改当前段的轨道与混响量）：经典 8D / 歌手绕着你转 / 双环反向 / 上下翻滚 / 三层环绕（人声顶层 75°、乐器高度层 35° 逆时针、鼓环绕层、贝斯固定）/ 斜向环绕（人声往右前翘 45°、乐器往左前翘 45° 逆时针）/ 立体交叉（人声左右竖环、乐器前后竖环、鼓水平环）。都跟随歌曲小节。（v1.1 的“单点环绕（视频同款）”已移除。）
6. 导出：选格式与响度（响亮 = 与试听一致，默认 / 标准 = −14 LUFS）→ 后台渲染 → 下载 `<原歌名> (8D).<扩展名>`。

## 3. 架构

```
web/（Vite + TypeScript + Three.js，无 UI 框架）
  src/app.ts     总控：界面阶段、场景修改的唯一入口（applyScene）、帧循环、导入导出流程，把下面各层串起来
  src/session/   打开一首歌之后才有的过程状态：歌的上下文（song）、自动保存（autosave）、EQ 与房间混响跟上场景（eq-sync）、一次导出的等待（export-run）
  src/ui/        音轨面板、参数面板、播放条与时间轴、最近的歌、导入弹层、导出对话框；界面阶段表、快捷键映射、导入进度（纯函数）
  src/scene/     Three.js：白模人头、轨道环、把手、声源色点、拖拽交互、三层半球
  src/audio/     AudioWorklet 双耳渲染核心 + 播放控制 + 混响/EQ 卷积；9 声道布局与处理器名只写在 params.ts 一处
  src/model/     不依赖浏览器的场景规则（纯函数）：编辑（edit）、撤销 / 重做（history）、三层的划分（layers）、混音规则（mix）、音轨的固定属性（tracks）
  src/orbit/     轨道公式与时间轴（与 Python 逐点一致，共享黄金向量）
  src/i18n/      界面三语词典（中文 / 日本語 / English）与语言检测
  src/api.ts、src/types.ts   后端接口；与后端模型一一对应的类型
backend/orbit8d/（Python 3.12 + FastAPI）
  api/         HTTP 接口：app（组装、启动流程、托管前端构建产物）、guards（Host / 跨站防护、统一的错误应答）、routes_*（按领域分的路由）、state（启动后才建好的对象）
  jobs/        songs（收歌、排导出、删歌：查状态 + 改记录 + 排任务，在存储锁里一起做）、worker（单工位任务队列）、store（记录的文件存储，原子写）、states（显式状态机）、layout（磁盘布局，只算路径）、stems（分轨与试听文件的读写、WAV → FLAC 迁移）
  assets.py    外部数据的下载与校验入口（HRTF 数据集；分轨模型经 separate/model；`make assets`）；混响冲激响应缓存文件的命名、旧缓存清理
  separate/    Demucs 分轨；分轨模型文件（model：先找 HuggingFace 缓存，缺了才下载并校验 sha256）
  media/       ffmpeg 探测、解码、编码（参数列表调用，不经 shell）；音频文件的分段读写（audiofile）
  engine/      hrtf / orbit / timeline / render / reverb / master / tone / tempo / structure / choreo / scene / sources（9 路声源）/ mixdown（按场景渲染到母带）/ analysis（分析）/ chunks（分段处理）
  download.py  下载到 .part → 校验 sha256 → 原子改名（assets 与 separate/model 共用）
  config.py、errors.py、logging_setup.py   运行配置、带错误码的异常、结构化日志
shared/golden/ 跨语言一致性测试数据（Python 生成，TS 校验）；contract.json 是前后端共享的枚举、声道布局、记录字段与错误码的快照
```

依赖方向：上层只能依赖下层，模块之间没有环，也不许用函数内 import 把环藏起来。“谁可以依赖谁”的两张表写在测试里（`backend/tests/test_architecture.py`、`web/src/architecture.test.ts`：只读源码里的 import，不执行模块），具体到每个单元以表为准；新增单元或新增方向要先改表。
- 后端自上而下：`__main__`（启动）→ `api` → `assets`、`jobs` → `separate`、`media` → `engine`；`download`、`config`、`errors`、`logging_setup` 是最底下的小模块。`engine` 不依赖项目里的任何其他包；`jobs` 不依赖 `api`、`assets`、`separate`（分轨器由启动处传进来）；入口层 `api` 不拿存储锁。
- 前端自上而下：`app` → `session`、`ui`、`scene`、`audio` → `model`、`orbit` → `types`；`i18n`、`api.ts` 与 `types.ts` 同在底层。`ui`、`scene`、`audio` 互不依赖；`session` 只用 `audio`、`model`、`types`（定时器、取数、保存这些副作用由 `app.ts` 传进去，所以不用浏览器环境就能测）；正式代码不依赖测试代码。

单一真理源：HRTF 表、混响冲激响应（BRIR）、按场景的补偿 EQ、每轨校准增益、试听总增益、自动编排全部由后台生成，前端下载同一份使用。

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
- 相位（度）：单段时 `φ(t) = start + offset + dir × 360 × (t − t_ref) / T`，`dir = +1`（顺时针）/ `−1`（逆时针），`t_ref` 为分析得到的“正前方对齐点”。多段、停顿时相位由时间轴积分得到（§13.3），单段无事件时与上式完全相同。
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

场景 v2 = `{version: 2, mix: {音轨: 混音}, sections: [段], events: [事件], room: {name}, rear_darken_db}`（§13.1）。
下表前半是每一段里每条音轨的轨道参数（`sections[k].orbits[音轨]`），混音参数全曲共用（`mix[音轨]`）。

| 参数 | 范围 | 默认 |
|---|---|---|
| shape | circle / ellipse / pendulum / figure8 / spiral / fixed | circle |
| radius_m | 0.5–4 | 1.2 |
| speed | {mode: bars, bars ∈ {1,2,4,8}} 或 {mode: seconds, seconds ∈ [2,30]} | 分析给出的默认小节数 |
| direction | cw / ccw | cw |
| start_deg | −360–360 | 0 |
| height_deg | −60–90（90 = 正头顶） | 0 |
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
| 每段 wet_db | −24–0 | −12 |
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

分频：`low = sosfiltfilt(butter(2, 120 Hz))`，`high = x − low`（零相位、相加完全还原）。分离残差并入 other：`other = 原曲 − 人声 − 鼓 − 贝斯`（与 “other 分轨 + (原曲 − 四轨之和)” 数学上相同；分析只读这三条分轨）。

### 5.2 每块处理（块长 32 采样；浏览器把 128 采样的渲染量子拆成 4 块）
块长 32 时方向切换误差比 128 低约 13 dB（6 秒一圈且带仰角起伏的实测：与 16 采样块相比 −53.9 dB）。
对每个运动声源、每个块 b（中心时刻 t_b）：
1. 由时间轴求 `(az, el, d)`（§13.3）。
2. 背后压暗：`hp` 为一阶 Butterworth 高通（3 kHz，因果，状态跨块连续）；`r = max(0, −z)`，`z = cos(el)·cos(az)`；`x' = x − r·cut·hp(x)`，`cut = 1 − 10^(−rear_darken_db/20)`。
3. 距离增益：`g_d = 1 / clamp(d, 0.5, 4)`（1 m 为 0 dB）。
4. 增益：`g = g_d · 校准增益 · 10^(gain_db/20) · 静音/独奏`。
5. HRIR：在 (el, az) 网格上双线性插值（el 截断到网格范围）。
6. 分块重叠相加卷积：本块输入 × 本块 HRIR，尾巴加到后续块。

超低频：三轨按各自的音量/静音/独奏相加后，用 (0°, 0°) 的 HRIR 渲染，无压暗、无距离增益，乘 sub 校准增益。

### 5.3 混响
- 送出信号（单声道）：`Σ reverb_send × 校准增益 × 10^(gain_db/20) × 静音/独奏 × 声源信号`（不含距离增益，使远近改变直达/混响比），再逐块乘 `10^(wet_db(t)/20)`（分段混响量，段落交界处与轨道同步平滑过渡，§13.3）。
- BRIR：16 个水平方向的去相关噪声尾巴（分 5 个频段按房间 RT60 衰减，含预延迟与 8 ms 起音）分别过对应方向的 HRIR 后相加，150 Hz 高通，按每耳能量归一；再叠加 8 个早期反射（§13.7）。
- 湿声 = 送出信号 ⊛ BRIR。

| 房间 | RT60（<250 / 250–1k / 1k–4k / 4k–8k / >8k Hz，秒） | 预延迟 |
|---|---|---|
| room | 0.8 / 0.7 / 0.6 / 0.45 / 0.3 | 8 ms |
| hall | 2.2 / 2.0 / 1.6 / 1.1 / 0.6 | 18 ms |
| church | 4.0 / 3.6 / 2.8 / 1.8 / 0.9 | 30 ms |

### 5.4 母带
- 补偿 EQ：水平一圈 HRTF 平均功率谱的倒数（1/3 倍频程平滑，限幅 ±6 dB）+ 按场景的频带校正（§13.6），1025 点线性相位 FIR（导出时补偿 512 点延迟）。
- 导出：自动总增益 = min(到目标响度所需增益, 让 99% 的 30 ms 片段压缩不超过 1 dB 的增益) → 前视限幅（4× 过采样估真峰值，上限 −1 dBTP）。目标响度可选：响亮 −9 LUFS（默认，与试听一致）/ 标准 −14 LUFS（`master.LOUDNESS_TARGETS`）；波峰高的歌两个目标都可能先被峰值条件限住。
- 试听：总增益取分析时对“经典 8D”预设算出的值 + 浏览器 DynamicsCompressor 兜底。

### 5.5 校准增益
分析阶段用“经典 8D”预设整曲渲染一次，每个音轨的校准增益 = √(该音轨原始立体声能量 / 渲染后能量)，sub 同理。导出与试听都用这组固定增益，保证两边音量平衡一致。同一次渲染还给出音色锚点（§13.6）。

### 5.6 HRTF 数据
- 来源：TH Köln Neumann KU100 `HRIR_FULL2DEG.sofa`（sofacoustics.org，sha256 `d3671e68…5ddb7`），48 kHz、128 点，89 档仰角 × 180 档方位（2°）。
- 处理：SOFA 逆时针方位转为顺时针索引 → 重采样到 44.1 kHz → 补零到 128 点 → 按水平一圈平均每耳能量归一为 1。
- 二进制格式 `hrtf.bin`：`"O8DH"` + uint32 头长 + JSON 头 `{version, sample_rate, taps, az_step_deg, n_az, el_nodes, layout:"el,az,ear,tap"}` + float32 数据（小端）。

### 5.7 分段处理（`engine/chunks.py`）
整曲按 `CHUNK = 423 360` 采样（约 9.6 秒，= 32 采样块 × 13230 = 30 ms 限幅窗 1323 × 320）一段依次处理，峰值内存只与段长有关；结果与整段一次处理一致（只差浮点舍入，均有“分段 = 整段”测试，`tests/test_chunks.py`）。
- 整曲只保留 float32：分析时 = 9 路声源（36 字节/帧）+ 校准用混音（8 字节/帧）；导出时 = 未经母带的混音（8 字节/帧）。读文件一律 float32，进每段后再转 float64 计算。
- 因果的处理把状态带进下一段：HRTF 分块卷积尾巴（4 块）、背后压暗一阶高通状态、超低频与混响卷积尾巴（`StreamConvolver`）、K 计权滤波器。
- 要“看前看后”的处理给每段前后补上下文、只保留本段：零相位分频 ±0.5 秒（二阶 120 Hz 的瞬态衰减到 e⁻²⁶⁶）、线性相位 EQ ±512、真峰值 ±16（插值滤波器只用到 ±10 个采样；真峰值本身也按 65 536 采样分块算）、限幅增益 ±2048（真峰值 10 + 最小值滤波 1323 + 平滑 661）。
- 固定帧切分的统计用 `Framer` 保证帧位置与整段相同：Welch 平均谱（8192 点、半重叠，各段按帧数加权平均；一次最多 64 帧）、测速谱通量（2048 点跳 256，相邻帧差分跨段接续）、段落识别的逐帧频带（4096 跳 2048）。按区间求和的统计（响度门限块、每小节功率、0.25 秒音量起伏）用 `SegmentSums` 按区间边界累计。
- 响度：`master.LoudnessMeter` 逐段做 BS.1770（K 计权状态接续、400 ms 门限块按整曲位置累计、−70 LUFS 绝对门限 + −10 LU 相对门限），与 pyloudnorm 0.2 逐项一致（误差 < 1e-9 LU）；比一个门限块还短或全静音时为 −inf。
- 分析（`analysis.analyze`）共四遍：①原曲 + 人声 / 鼓 / 贝斯分轨 → 9 路声源与所有与速度无关的统计（各轨原始能量、谱通量、原曲 Welch 谱与响度、音量起伏、逐帧频带、声源频谱、最大绝对值）；测速；②经典场景渲染只求各轨能量 → 校准增益；③渲染校准后的混音（存 float32）→ 过 HRTF 平均补偿的 Welch 谱 → 音色锚点 → 过场景 EQ 测片段峰值与响度 → 试听总增益；④再读一遍原曲与人声分轨 → 每小节功率 → 段落。
- 导出读试听文件（`preview/*.flac` × preview_scale，与浏览器试听同一份数据）：逐段渲染 → 流式 EQ（延迟 512 点在写入整曲混音时补回）→ 测片段峰值与响度 → 总增益 → 逐段限幅、边算边写 `render.wav`。`RENDER_VERSION = 3`（导出读试听文件、分段渲染、响度可选）；`ANALYSIS_VERSION` 不变（结果等价，不触发重新分析）。
- 实测（M4 Pro）：Faded 212 秒——分析峰值 1.06 GB / 14.6 秒、导出 0.75 GB / 8.0 秒（旧实现 5.2 GB / 5.6 GB）；17.7 分钟合成长歌——分析 2.4 GB / 75 秒、导出 0.95 GB / 41 秒。与旧实现比：段落、测速、音量起伏逐位相同，校准增益相对差 ~1e-11，音色锚点差 < 1e-8 dB，导出差异 −125.8 dB（来自试听文件的 24 位量化）。

## 6. 显式状态机

项目：
| 当前 | 允许的下一个 |
|---|---|
| UPLOADED | DECODING, FAILED |
| DECODING | SEPARATING, FAILED |
| SEPARATING | ANALYZING, FAILED |
| ANALYZING | READY, FAILED |
| READY | ANALYZING（分析算法升级：服务启动时自动重新分析，沿用已有分轨） |
| FAILED | UPLOADED（同一文件重新导入时重试：接口当场把它转回“已上传”、清掉旧错误，再排队分析；工位正忙时界面看到的是排队，而不是上次的失败） |

导出：
| 当前 | 允许的下一个 |
|---|---|
| QUEUED | RENDERING, FAILED |
| RENDERING | ENCODING, FAILED |
| ENCODING | DONE, FAILED |
| DONE | — |
| FAILED | QUEUED（同参数重新提交时重试） |

非法跳转抛 `IllegalTransition`，状态不变。每次跳转写结构化日志（trace_id = 项目 / 导出 ID）。服务重启时把未完成的记录标为 FAILED：项目错误码 `INTERRUPTED`（重新导入即可）、导出错误码 `EXPORT_INTERRUPTED`（重新导出即可）。
项目记录另有 `step`：状态里的子步骤，目前只有分轨前第一次下载模型时为 `model_download`（`progress` 此时是下载进度），每次状态跳转清空。
两张表与 `check_transition` 在 `jobs/states.py`。接口当场做的跳转只有两个重试（FAILED → UPLOADED、FAILED → QUEUED），在 `jobs/songs.py` 里和“排任务”一起做；其余跳转由任务队列（`jobs/worker.py`）做：执行任务时逐步推进，服务启动时把要重新分析的歌转到 ANALYZING；上次没做完的记录由 `Store.recover_interrupted` 在启动时标为 FAILED。

### 6.1 存储、迁移与删除（`jobs/layout.py`、`jobs/stems.py`、`jobs/store.py`、`jobs/songs.py`）
- 分工：`layout` 只管每个文件叫什么、放在哪（`ProjectFiles`、`ExportFiles`，不读不写）；`stems` 管分轨与试听文件的读写和 WAV → FLAC 迁移；`store` 管记录（`ProjectRecord`、`ExportRecord`、上传文件的元数据 `SourceInfo`，每条一个 JSON、原子写）和存储锁；`songs` 是收歌、排导出、删歌这三个要在锁里一起做的用例。记录的字段清单记在 `shared/golden/contract.json`，前端的类型与它比对。
- 项目目录：`source`（上传的原文件）、`stems/*.flac`（4 条分轨，24 位 FLAC，每轨缩放到 ±1 以内，缩放系数记在记录的 `stem_scales`）、`preview/*.flac`、`scene.json`、`record.json`。解码出的 `orig.wav` 只在分析期间存在，用完即删；重新分析时从 `source` 再解码。分析结果只存在 `record.json` 里：旧版另写的那份 `analysis.json` 从来没人读，现在不再写（已有的不动，随项目一起删）。
- 读记录时忽略这个版本不认识的键（旧版留下的，如导出记录里从没用过的 `progress`；或以后的版本加的），缺的键用默认值，所以新旧版本写的记录都读得出来。
- 分轨 WAV → FLAC：写 FLAC → 读回逐段比对（误差 ≤ 4 个最低位）→ 记录写缩放系数 → 删 WAV。同一轨有 WAV 就用 WAV（缩放 1），所以中途崩溃不会出现缩放系数对不上的 FLAC；重复执行无副作用。
- 启动时：清空回收目录 `data/trash/`、删掉上次没传完的上传、删掉不再使用的旧缓存（全局 EQ `eq_*.wav`、旧版本 BRIR、临时文件）；已就绪且还有 WAV 分轨或 `orig.wav` 的旧项目排队迁移（在重新分析之前）。
- 删除：有排队或正在跑的任务（任务队列按歌计数）或状态不是 READY / FAILED 时拒绝（409 `PROJECT_BUSY`）；否则先把它的导出目录、再把项目目录改名挪进回收目录（原子），再删。“查状态 + 改记录 + 排任务”与删除都在存储锁里做（上传、导出、保存场景同样），不会删到一半又有新任务。拿锁的只有 `jobs/`（三个用例在 `jobs/songs.py`），入口层不碰锁。

## 7. API（仅 127.0.0.1，Host 头白名单防 DNS 重绑定，跨站写请求 403）

- 代码：`api/app.py` 只做组装（启动流程、防护、挂路由、托管前端）；Host / 跨站防护与错误应答在 `api/guards.py`；接口按领域分在 `api/routes_projects.py`、`routes_scene.py`、`routes_exports.py`、`routes_assets.py`，路由里不写业务规则（收歌、排导出、删歌调 `jobs/songs.py`）。
- Host 头只接受 `127.0.0.1` / `localhost`。
- 非 GET/HEAD/OPTIONS 请求若带 Origin，必须是本服务或 Vite 开发服务器（5173）的源，否则 403（防 CSRF）。
- 错误响应统一为 `{code, message}`（请求体或查询参数没通过校验时也是：422 `INVALID_REQUEST`，message 里写明哪个字段、为什么）：message 是英文技术说明，界面按 code 显示当前语言的提示（`web/src/i18n` 的 error 表，不认识的 code 才显示 message）；路径里的 ID 必须是 16 位小写十六进制，否则 404。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/health | 版本、可用输出格式 |
| GET | /api/presets | 预设名列表（前端据此生成按钮） |
| GET | /api/presets/{name}?bars=N | 预设场景 |
| GET | /api/scene/schema | 场景参数表（前端滑杆范围的唯一来源） |
| POST | /api/projects | 请求体为文件原始字节（`Content-Type: application/octet-stream`），原文件名放在 `X-Filename` 头（URL 编码，仅用于显示）；边收边写临时文件，>300 MB 立即中止（413）；ffprobe 校验格式白名单与时长 ≤ 20 分钟；项目 ID = 内容 sha256 前 16 位（重复上传直接返回，失败过的会重试） |
| GET | /api/projects | 最近的歌（最近用过的在前：记录更新或保存场景，取较晚者）：`{id, state, title, artist, duration_s, size_bytes, error, last_used}` |
| GET | /api/projects/{id} | 状态、阶段进度与子步骤（`step`）、分析结果（BPM、时长、默认小节数、t_ref、校准增益、试听总增益、音色锚点、声源频谱、段落、分析版本）、`preview_scale`、`stem_scales` |
| DELETE | /api/projects/{id} | 连同它的导出删除，204；不存在 404；还有任务 409 `PROJECT_BUSY`（§6.1） |
| GET | /api/projects/{id}/stems/{vocals_hi,bass_hi,drums_hi,other_hi,bass_sub,drums_sub,other_sub,original}.flac | 试听用 24-bit FLAC（drums_hi/other_hi/original 为立体声；original = 原曲，供 A/B 对比；全部按同一个 preview_scale 缩放） |
| GET | /api/projects/{id}/scene | 这首歌上次保存的场景（经白名单校验，v1 自动升级）；没保存过或文件已不合法时 404 `NO_SAVED_SCENE` |
| PUT | /api/projects/{id}/scene | 整份替换保存（幂等，原子写到项目目录 `scene.json`）；请求体 > 256 KB → 413，不合法 → 422 `INVALID_SCENE`（不覆盖已保存的） |
| GET | /api/projects/{id}/choreography | 按识别出的段落自动编排好的场景（新歌的默认场景，§13.5） |
| POST | /api/projects/{id}/choreography | `{scene}` → 沿用这个场景的分界与段名重新编排（只换各段轨道、混响量与事件，§13.5），前端“✨ 自动编排”用 |
| POST | /api/projects/{id}/eq | `{scene}` → 这个场景的补偿 EQ（单声道 float32 WAV，1025 点），导出用同一个函数 |
| GET | /api/assets/hrtf.bin, /api/assets/brir/{room}.wav | DSP 数据（BRIR 缓存文件名带算法版本） |
| POST | /api/projects/{id}/exports | `{scene, format, loudness}`（loudness = loud / standard，默认 loud）；导出 ID = sha256(渲染版本 + 项目 ID + 规范化场景 JSON + 格式 + 响度) 前 16 位（幂等；渲染算法升级后不复用旧导出） |
| GET | /api/exports/{id} | 状态（QUEUED / RENDERING / ENCODING / DONE / FAILED，阶段本身就是进度）、失败时的错误码、完成后的下载文件名 |
| GET | /api/exports/{id}/file | 下载 |

输入格式白名单（ffprobe 的 codec_name）：mp3、aac、alac、flac、pcm_*、vorbis、opus。

## 8. 不变量（红线）
1. 120 Hz 以下永远在正前方，两耳相同。
2. 导出时长 = 原曲时长（编码器填充 ≤ 50 ms），真峰值 ≤ −1 dBTP。
3. 同一场景：试听与导出差异 < −40 dB（时间轴、分段混响量、补偿 EQ 两边同源）。
4. 同场景 + 同格式导出幂等。
5. 只监听 127.0.0.1；上传文件只按生成 ID 存放；ffmpeg 参数列表调用；上传格式白名单 + 大小/时长上限。
6. 异常一律结构化日志 + 前端可读提示，不吞异常；任务失败带错误码（`errors.CodedError`：解码 / 编码 / 下载模型 `MODEL_DOWNLOAD_FAILED` / 分轨 `SEPARATION_FAILED` 等），界面按错误码翻译。
7. 内存只随“float32 的整曲数据”增长（分析约 1.9 MB/秒、导出约 0.35 MB/秒），其余临时内存与歌长无关（§5.7）。

## 9. 测试设计

| 模块 | 用例 | 断言 |
|---|---|---|
| engine.orbit | 各形状固定时刻 (az, el, d) | 与 `shared/golden/orbit_vectors.json` 误差 < 1e-9 |
| web orbit | 同一份黄金向量 | 误差 < 1e-6 |
| engine.timeline | 单段无事件 = v1 公式；跨段 / 变速 / 停顿 / 飞过头顶连续；正反方向过渡不出 NaN；分段混响量平滑 | < 1e-7°；每毫秒转动 < 0.5° |
| web timeline | `shared/golden/timeline_vectors.json`（11 个随机多段 + 事件场景） | 方向单位向量、距离、混响量误差 < 1e-9 |
| engine.structure | 合成一首已知结构的歌（前奏 / 主歌 / 副歌 / 主歌 / 副歌 / 桥段 / 副歌 / 尾声） | 分界落在真实小节线上、标签全对 |
| engine.tone | 已知倾斜的匹配；参考场景 = 锚点；模型预测的染色 vs 真实渲染；静音 / 调音量不被拉回 | 1e-9；逐位相等；中高频 ±1.5 dB；±0.75 dB |
| web model/edit | 切分 / 删除 / 拖分界吸附小节线；事件重叠；预设只改当前段；拖事件吸附到拍、碰到同类事件停住、改长短 0.5–30 秒 | 纯函数、不改入参、位置没变返回原对象 |
| web model/history | 撤销 / 重做、连续修改合并、新修改清空重做、上限 100 步 | 逐步断言 |
| web i18n | 中日英三份词典条目完全一致、无空串、英文全部译过；语言检测优先级；段落名 / 预设名 / 错误码的显示 | 逐项断言 |
| engine.analysis | 原曲 × original_gain 与经典场景 8D 试听（× preview_gain）的响度 | 相差 < 0.1 LU |
| engine.structure | 音量起伏：相对最响处、静音压到 −60、不满一格也算一格 | 逐点断言 |
| api | 场景存取：未保存 404、保存后原样取回、不合法 422 且不覆盖、超大 413、跨站 403 | 状态码与内容 |
| engine.hrtf | 网格点插值 = 原始 IR；90° 两耳时间差；左右对称 | < 1e-9；0.62–0.70 ms；镜像能量差 < 1.5 dB |
| engine.render | 固定方向与直接卷积；块 32 vs 16 | < −100 dB；< −45 dB |
| engine.mixdown | 超低频正前方；时长 | <100 Hz 两耳相关 > 0.999；样本数相等 |
| engine.master | 限幅后真峰值 | ≤ −1.0 dBTP |
| web audio core | 黄金渲染用例（合成 HRIR 网格 + 9 路 8192 采样噪声；三段 + 过渡 + 停顿 + 飞过头顶 + 分段混响量） | 与 Python 输出差异 < −90 dB |
| web scene/mapping | 方向↔世界坐标互逆、局部→世界与轨道公式一致、由位置反推相位 | < 1e-6 |
| jobs.states | 非法跳转 | 抛 IllegalTransition，状态不变 |
| api | 非音频 / 超大 / 危险文件名 / 非法 Host | 400 / 413 / 不落盘 / 400 |
| api | 同参数重复导出 | 同一导出 ID |
| e2e | 10 s 音频上传 → 就绪 → 五种格式导出 | ffprobe 可读、时长一致 |
| engine.chunks / master / tempo / structure / mixdown / analysis | 每个分段核心按随机段长（含 1 采样、不满一段、上下文比段还长）与整段 scipy / pyloudnorm 参考对比；SceneRender 按不同段长 | 卷积 / 帧 / Welch < 1e-10；响度 < 1e-9 LU；真峰值逐位相同；限幅 < 1e-10；渲染 < 1e-12 |
| engine.analysis | 20 秒与 60 秒合成歌的分析内存（tracemalloc） | 每秒增长 < 2.5 MB，60 秒峰值 < 500 MB |
| assets / separate.model | 本机假 HTTP 服务：下载校验、校验失败与 404 不留半截文件、残留 .part 覆盖、缓存优先且坏缓存重下、断网报 `MODEL_DOWNLOAD_FAILED`；分轨进度换算 | 逐项断言 |
| api（存储） | 新歌分轨是 FLAC 且没有 orig.wav；峰值 > 1 的分轨按缩放系数还原；旧项目启动迁移后可重新分析与导出；校验失败保留 WAV；列表排序与字段；删除连同导出、404 / 409 / 跨站 403、删后可重新导入；启动清理；中断的导出码；响度选项 −14 / −9 LUFS；模型下载子步骤与失败码 | 状态码、文件、数值 |
| engine.choreo | 重复段落轮换且确定；重编保留分界、段名、混音、房间；自定义段名按主歌；最响副歌按原曲音量起伏 | 逐项断言 |
| web audio core | 改音量 20 ms 逐块过渡；静音后尾巴放完严格为 0；取消静音 = 从干净状态渲染 | 相邻块电平比 > 0.85；== 0；逐样本相等 |
| web audio transport / ui phase / scene redraw | 播放、停止按帧号生效（跳转先停后起）；界面阶段新跳转；按需重画判定 | 逐项断言 |
| 契约（`tests/test_contract.py`、`web/src/contract.test.ts`） | `shared/golden/contract.json` 由后端常量生成：音轨、形状、房间、小节选项、导出格式、响度、两个状态枚举、子步骤、9 声道顺序与试听文件 → 声道表、采样率、停顿的减速时长、三层的一键高度、各参数上限、记录字段、全部错误码、场景参数表。后端断言重新生成的结果与已提交文件一致；前端断言 TS 常量、类型的字段清单、`STEM_LAYOUT`、i18n 的错误码表与它一致 | 逐项相等（有意改契约时重新生成并在提交里写明） |
| 依赖方向（`tests/test_architecture.py`、`web/src/architecture.test.ts`） | 读每个源文件的 import：各单元只依赖表里允许的单元、没有循环依赖；后端另查没有函数内的项目内 import、`api` 不拿存储锁；前端另查正式代码不依赖测试代码 | 违规清单为空（§3） |
| jobs.store | 歌名与导出文件名的来历、文件名清理；旧版写的项目记录读得出来且原样写回；导出记录里从没用过的 `progress` 读的时候丢掉；缺的键用默认值、不认识的键忽略 | 逐项断言 |
| api | 工位正忙时重新导入一首失败过的歌；请求校验失败的应答 | 返回的记录已是 UPLOADED、旧错误已清；422 且响应体是 `{code: INVALID_REQUEST, message}` |
| web session | 自动保存（防抖、只存最后一次、失败重试、重试期间又改、过期的应答不算、换歌前 flush、删歌没删成时改动还在）；EQ 跟随场景（250 ms 防抖、只用最后一次结果、房间缓存、失败保留旧 EQ）；导出的等待（轮询、幂等命中、失败、取消后不再更新界面也不下载）；歌的上下文与歌名 | 假定时器 + 假接口，逐步断言 |
| web ui/shortcuts、ui/progress | 按键 + 当时的情形 → 动作（修饰键、焦点在控件或输入框时让路）；各阶段在总进度条上的区间、模型下载子步骤、错误提示 | 逐项断言 |
| 试听引擎探针 A（`web/src/audio/probe.test.ts`，进 `make test`） | 假的 Web Audio 记录引擎在固定操作序列下的全部音频图操作：建了哪些节点、connect / disconnect、每个 AudioParam 的每次调度（方法、数值、相对时间）、声源 start / stop、发给 Worklet 的消息；Worklet 用真实渲染核心跑，记录各段是否出声。操作包括载入、播放、暂停、跳转、拖播放头、原曲与 8D 切换、换房间、EQ 更新、播放中改场景 | 与 `probe.golden.json` 逐项相同（快照由重构前的代码生成；有意改试听行为时才用 `ORBIT8D_UPDATE_PROBE=1` 重新生成） |
| 浏览器探针 B（`make probe`，`backend/tests/browser_probe/`，不进 `make test`） | 无头 Chrome 打开构建好的前端，后台是真实的 `create_app` + 假分轨器 + 合成的歌 + 临时数据目录：导入并打开、播放 / 暂停、空格、B、方向键、拖播放头、改参数看到“已保存”、撤销 / 重做、停掉后台看到“保存失败”再恢复、导入第二首、换歌、导出并下载、导出中途关对话框、删歌，共 35 步 | 每步的界面状态正确；没有未捕获的异常、未处理的 Promise 拒绝、console.error、意料之外的失败请求；真实 AudioContext 上各 AudioParam 的调度序列、Worklet 消息类型序列、音频图操作与 `baseline.json` 一致（时间归一化、不比的量见该目录的 `__init__.py`） |

## 10. 界面（web/）

### 10.1 界面阶段状态机（`src/app.ts`）
| 当前 | 允许的下一个 |
|---|---|
| booting | empty, processing, loading, error |
| empty | uploading, loading（从最近的歌里打开） |
| uploading | processing, loading, error |
| processing | loading, error |
| loading | ready, error |
| ready | uploading, loading（切歌）, empty（删掉了正在用的歌） |
| error | uploading, empty, loading |

表在 `src/ui/phase.ts`（有单元测试）。非法跳转抛错；`ready` 阶段的非致命错误（如预设加载失败）用提示条显示，不改变阶段。刷新页面时用 localStorage 里的上次项目 ID 自动恢复（存储不可用时忽略）；该项目若正在重新分析，先进 processing 等它就绪。打开一首歌时先取它保存过的场景，没有再用自动编排。

### 10.2 3D 场景
- 世界坐标与音频坐标：人头面朝世界 +z，听者右侧 = 世界 −x，`world = (−x, y, z)`（`src/scene/mapping.ts`）。
- 人头模型标定：模型坐标里两耳外缘中点 (−0.087, 1.504, −0.162)、耳距 3.62，缩放到耳距 0.18 并把中点放到原点。
- 距离可视化：`visual = 0.32 · √(d / 0.5)`（d ∈ [0.5, 4] m）；肩膀下方（y = −0.3）两圈很淡的距离参考圈对应 1 m / 2 m。不画实心地面，参考圈也不写深度，比肩膀低的轨道完整可见。
- 轨道形状显示播放头所在段的轨道；色点位置按时间轴（含段落过渡、停顿、飞过头顶）。
- 声源色点（`src/scene/orbits.ts`）：每条音轨一个实心色点（Sprite，`sizeAttenuation = false`，远近不变大小，不参与色调映射，保持音轨原色）。选中：26 像素贴图（白边 + 深色描边，实心约 9 像素），画在最上层；未选中：16 像素、不透明度 0.85；静音 0.3。贴图外圈透明部分用来扩大点击范围。立体声轨（鼓、其他乐器）也只画一个点，放在左右声道正中（偏移 0）；选中时在轨道上加亮一段 7 像素宽的弧（音轨色往白色混 55%），从 −width/2 到 +width/2，弧长即声像宽度。
- 不做辉光后处理（不再用 EffectComposer / UnrealBloom），直接渲染；两耳处不再放标记点。
- 把手：只有轨道相位 270°（左侧）的白色空心小圆环（22 像素贴图）= 距离；暂停时拖色点改起点（仅圆 / 椭圆 / 螺旋 / 固定，相位可由位置反推：`start = 目标相位 − 时间轴累积相位`，色点偏移为 0）。倾斜不再放 3D 把手，统一由倾斜盘负责。
- 固定像素大小：`scale = 2 · px · tan(fov/2) / 视口高`（`spriteScaleForPixels`），每帧按画布尺寸更新。
- 视觉层级：选中音轨的轨道粗而亮，其余细而淡（不透明度 0.16），未选中的色点更小更淡；不画头部到色点的连线。
- 按需重画（`src/scene/redraw.ts`）：逐帧回调照常跑（色点位置、电平表、时间），但只有画面真的变了才调用 WebGL 渲染——播放中、拖播放头、播放位置跳了、视角在动（`OrbitControls.update()` 返回 true，含阻尼惯性）、场景 / 选中音轨 / 网格开关 / 窗口大小变了（`Stage.invalidate()`）。静止时不占 GPU。

- 三层半球网格（参考杜比全景声示意图，`src/model/layers.ts`、`src/scene/dome.ts`）：环绕层 [−15°, 15°]（蓝）、高度层 (15°, 60°]（橙）、顶层 (60°, 90°]（红）；只画 −15° / 0° / 15° / 60° 四条分界线、极淡色带（1.5%）和三个层名，选中音轨所在层提亮（5%）；按选中音轨的可视半径缩放，使其轨道贴在球面上；可在“空间”页关闭（记在 localStorage）。
- “所在层”按钮：一键把选中音轨的高度设为该层中心（0° / 35° / 75°），高亮随高度变化同步，不重建面板。

### 10.3 面板与朝向
- 左栏：音轨名 + 静音 / 独奏；“✨ 自动编排”（`POST /choreography`：按时间轴上现在的分界与段名重编，混音、房间、背后压暗不变）+ 预设按钮（由 `GET /api/presets` 生成）；多段时提示“预设只改当前这一段”。
- 最近的歌（`GET /api/projects`）：导入页拖入区下方一块，顶栏歌名旁的 ▾ 展开同一个列表（点外面或 Esc 收起）。每行：歌名（只有已就绪的能点开；正在用的标 ▶）、时长 · 占用空间（· 状态）、“删除”。删除要 3 秒内连按两次（第一次变成“再点一次删除”），不弹系统对话框；删掉正在用的歌时丢弃没存的改动、回到导入页（没删成——后端回 409，歌还有任务——时改动不丢，照常保存）。
- 右栏头部：音轨名 + 当前段名（彩色标签）。分【轨道】【混音】【段落】【空间】四页：轨道页 = 形状 → 朝向快捷键（水平 / 竖·左右 / 竖·前后 / 斜↗ / 斜↖）→ 倾斜盘 + 所在层 → 距离 → 形状专属参数 → 速度与转动方向 → “精细调节”（起点、高度、三个倾斜角）；混音页 = 音量、声像宽度、混响送出（全曲）；段落页 = 起止与小节数、段落名、这一段的混响量、在此切分 / 删除本段、这一段的事件（可删）、在播放头处给当前音轨加停顿 / 飞过头顶；空间页 = 房间、背后压暗、网格开关、重置视角。
- 播放条里的时间轴（`src/ui/timeline.ts`）代替进度条：段落色块（前奏 / 尾声 蓝紫、主歌 青、副歌 粉、桥段 金）、上方事件条（停顿 黄、飞过头顶 粉）、白色播放头。点一下跳到那里；在色块上按住左右拖是“拖播放头”：拖动中播放头、时间、3D 轨道与面板都跟着预览位置走，松手才真正跳过去（播放中会先暂停、松手后从新位置接着播；拖动被系统打断时跳到最后的位置）；拖段落之间的竖线调整分界（吸附小节线，两边至少各留 1 小节）。段落色块上叠一层淡淡的原曲音量起伏（每 0.25 秒一点，−40 dB 以下贴底）。
- 事件条（高 9 像素，最少 10 像素宽）：条的起点 = 停顿“完全停住”的时刻 / 飞过头顶的开始，长度 = duration_s。拖动改位置、拖右端（最多 6 像素、不超过条宽 1/3）改长短，都吸附到拍（1/4 小节），按住 ⌥ 自由拖；只在自己的空档里移动，碰到同一音轨的同类事件就停住；数组顺序不变（拖动中下标稳定）。点一下选中（白框），Delete / Backspace 删除，Esc 取消选中。
- 撤销 / 重做（`src/model/history.ts`）：顶栏 ↩ ↪ 与 ⌘Z / ⇧⌘Z（Ctrl+Y）；最多 100 步，间隔不到 0.5 秒的连续修改算一步；换歌清空。所有场景修改都经过 `App.applyScene` 这一个入口记录。
- 自动保存（`src/session/autosave.ts`，状态表 已保存 / 保存中 / 保存失败）：改完 0.8 秒没再改就 PUT 整个场景（只存最后一次），顶栏显示“已保存 / 保存中… / 保存失败，稍后重试”（失败 5 秒后重试）；换歌前、关页面（pagehide，keepalive）时把没存的发出去。
- 导出对话框（`src/ui/export-dialog.ts`；等待过程在 `src/session/export-run.ts`）：选格式与响度 → 显示阶段（排队 / 渲染 / 编码）→ 完成后自动开始下载。界面每 0.7 秒问一次进度，同一时间只等一个导出：关掉对话框（或又开始一次导出）就不再等之前那次，不会过一会儿突然冒出下载、也不会两次导出抢同一个对话框；后台照常把它做完，同样的参数再导出一次直接拿到结果（幂等）。
- 键盘（`src/ui/shortcuts.ts`：按键 → 动作名的纯映射）：空格播放 / 暂停，← / → 跳 5 秒，B 切换 8D / 原曲，⌘Z / ⇧⌘Z 撤销 / 重做，Delete 删除选中的事件。
- 编辑规则（`src/model/edit.ts`，纯函数）：切分在最近的小节线上、新段复制原段；删第 k 段时间并给前一段（删第一段并给后一段）；事件默认停顿 1 小节、飞过头顶 2 小节，同一音轨同类事件不能重叠（停顿含前后各 0.5 秒）；最多 16 段、64 个事件。
- 倾斜盘（`src/ui/tiltpad.ts`、`src/orbit/orientation.ts`）：俯视人头，白点方向 = 轨道最高点朝向 az，离圆心距离 = 翘起角 tilt（边缘 90°）；写回时统一为 pitch = tilt、roll = 0、yaw = az（可表示任意平面朝向）；读取时由轨道平面法线反算。注意：pitch 90° = 左耳→头顶→右耳的竖环，roll 90° = 正前→头顶→脑后的竖环。

### 10.4 试听引擎
9 声道缓冲（vocals_hi, bass_hi, drums_hi L/R, other_hi L/R, bass_sub, drums_sub, other_sub）→ AudioWorklet（输出 0 = 双耳干声，输出 1 = 已逐块乘分段混响量的混响送出）→ BRIR 卷积 → 补偿 EQ 卷积 → 试听总增益 → DynamicsCompressor 兜底 → 输出；并联两路 AnalyserNode 驱动左右电平表。ConvolverNode 一律 `normalize = false`。
不爆音：每次播放给 8D 输入与原曲各建一个淡入淡出 GainNode，播放 10 ms 淡入、暂停 / 跳转 15 ms 淡出后再停；Worklet 的开始 / 停止按帧号生效（`src/audio/transport.ts`：停止在 stopFrame 之后的第一个量子生效，淡出期间照常渲染；新一次播放在 startFrame 所在的量子才清空状态，跳转时旧的淡出先结束）。渲染核心里各声源的增益、混响送出、超低频增益改参数时 20 ms 内逐块线性过渡（第一次设参数与跳转后直接到位，所以黄金用例不受影响）；声源静音后把卷积尾巴放完 4 块就清空状态，取消静音时从干净状态开始。
原曲 A/B：原曲缓冲与 9 声道缓冲同一时刻、同一位置开始播放（采样级同步），走另一路直接进兜底限幅；两路各有一个 GainNode，切换时 40 ms 交叉淡化。原曲那一路的增益 = original_gain × preview_scale（与 8D 试听一样响）。听原曲时 3D 画面变灰变暗，并显示“正在听原曲”。
场景变化后 250 ms 没再变（`src/session/eq-sync.ts`），就 `POST /eq` 取新 EQ，装进闲置的第二个 EQ 卷积器，80 ms 交叉淡化切过去（只用最后一次请求的结果，切换排队不打断淡化）。时间轴编译成纯数据（TypedArray）随参数一起 postMessage 给 Worklet。

### 10.5 界面语言（`src/i18n/`）
- 三份词典 zh / ja / en，结构以 `zh.ts` 的 `Messages` 类型为准，另外两份少一个词编译不过；测试再逐项比对条目、查空串。
- 页面启动时确定一次语言：localStorage 里记住的选择（`orbit8d.lang`）> 浏览器语言列表里第一个 zh / ja / en > 英文。`<html lang>` 与整页字体随语言设置（日文用 Hiragino Sans 等日文字体，3D 层名标签同样）。
- 切换：顶栏与导入页的语言下拉框；先把没存的改动存掉，再记住选择并刷新页面（界面文字在启动时一次性生成）。
- 存档里的段落名是后端给的中文规范名（前奏 / 主歌 / 副歌 / 桥段 / 尾声 / 全曲），界面用 `sectionName` 翻译显示，用户自定义的名字原样显示；预设名、朝向快捷键、层名、错误提示同理按 key 查词典。
- 场景编辑的不合法原因返回代码（`EditReason`），由界面翻译；开发者可见的内部错误（程序缺陷）一律英文、不翻译。
- 排版：分段按钮允许在空格处折行（英文、日文较长）；分页按钮按文字宽度分配；日文的预设与段落按钮用 `word-break: keep-all` 配合词典里的 `\u200b` 指定断行位置。

## 11. 不在范围内
自由画路径、逐参数自动化曲线、桌面 App 打包、头部追踪、导入自定义 HRTF、手机端、多个场景版本（每首歌只保存一份当前场景）。

## 12. 第三方素材
- 人头模型：“Infinite, 3D Head Scan” by Lee Perry-Smith，CC BY 3.0（`web/public/models/LeePerrySmith_License.txt`）。
- HRTF：TH Köln，“A Spherical Far Field HRIR/HRTF Compilation of the Neumann KU 100”（B. Bernschütz），CC BY-SA 3.0，不随仓库分发。
- 分轨模型：Demucs（Meta，MIT）。权重是 HuggingFace `adefossez/HTDemucs-ft` 固定版本 `d74ac89c` 的 4 个 safetensors + 组合配置（共约 336 MB，sha256 写在 `separate/model.py`），不随仓库分发：本机 HuggingFace 缓存里有就直接用，没有则 `make assets` 或第一次分轨时下载到 `data/assets/demucs/`。
- Orbit 8D 本身以 MIT 许可证发布（仓库根目录 `LICENSE`，版权人 Eureka0w0v0）。

## 13. v2：按段落编排的时间轴

### 13.1 场景 v2 数据模型（`engine/scene.py`、`web/src/types.ts`）
- `mix[音轨]`：gain_db / width_deg / reverb_send / mute / solo，全曲共用。
- `sections[]`：`{start_s, label(1–16 字), orbits{四条音轨的轨道}, wet_db}`，1–16 段；第一段必须从 0 秒开始，开始时间严格递增；每段必须有四条音轨的轨道。
- `events[]`（≤ 64）：`{t_s, kind: hold | overhead, duration_s ∈ [0.5, 30], targets: 1–4 条不重复的音轨}`；同一音轨同类事件的时间段不能重叠（停顿的时间段 = [t_s, t_s + duration_s + 1 秒]）。
- `room: {name}`、`rear_darken_db` 全局。
- v1 场景（`version: 1`，tracks 里混放轨道与混音，room 里有 wet_db）在校验前自动升级为只有一段“全曲”的 v2 场景。
- 预设都是只有一段的场景；`preset_parts` 把预设拆成（各轨轨道、全曲混音、这一段的混响量）供自动编排复用。

### 13.2 分析结果（`ANALYSIS_VERSION = 3`）
在 v1 字段基础上增加 `match_eq_db`（27 个 1/3 倍频程的音色锚点）、`spectra`（各路声源的频带功率）、`sections`（识别出的段落 `{start_s, bars, label, energy_db}`）、`version`。旧项目（没有 version 字段按 1 算）在服务启动时 READY → ANALYZING → READY 自动重新分析，沿用已有分轨，不重新跑 Demucs。
v3（原曲 A/B 与时间轴）再加：`original_gain`（原曲试听的线性增益：原曲响度对齐到经典场景的 8D 试听响度，即 `LUFS(补偿后混音) + 20·log₁₀(preview_gain)`，限幅 ±24 dB；任一边测不出响度时为 1）、`envelope_db` / `envelope_hop_s`（原曲每 0.25 秒的平均功率，相对最响处的 dB，最低 −60，保留 1 位小数），并生成原曲试听文件 `preview/original.flac`。

### 13.3 时间轴运动（`engine/timeline.py` = `web/src/orbit/timeline.ts`）
- 角速度：第 k 段 `ω_k = dir × 360 / T_k`（度/秒，固定形状为 0）；停顿系数 h(t)：`[t_s, t_s+0.5)` 从 1 线性降到 0，停住 duration_s，再 0.5 秒线性升回 1（只作用于 targets）。`ω(t) = ω_段(t) × h(t)` 在每个分段点（段落分界、停顿的四个拐点）之间是线性的，累积相位 `Φ(t) = ∫₀ᵗ ω` 用精确的二次式求，`Φ(t_ref)` 处归零。
- 每段的方向 = 这一段的轨道在相位 `start + offset + Φ(t)` 处的位置（固定形状只用 `start + offset`，不累积别的段转过的角度）。
- 段落过渡：分界 b 前后各 `h_b = min(半小节, 0.4 × 相邻两段较短者的长度)`；权重 `w = smoothstep((t − (b − h_b)) / 2h_b)`；方向做球面插值（几乎同向时归一化线性插值；正好相反时绕 `a × 上` 的轴转过去，a 正好竖直时改用 `a × 右`），距离线性插值。
- 飞过头顶：事件期间方向按 `e = sin²(π (t − t_s) / duration_s)` 朝正上方做球面插值，中点到达头顶，两端与原轨迹无缝衔接。
- 分段混响量：`wet_db(t)` 用同样的过渡权重在相邻两段之间线性插值，逐块（32 采样，块中心时刻）乘到混响送出信号上。
- 单段且没有事件时，结果与 v1 公式（§4.2）逐点相同（测试误差 < 1e-7°）。

### 13.4 段落识别（`engine/structure.py`）
1. 小节网格：`bar = 4 × 60 / bpm_norm`，小节线与 t_ref 同相位（第一条在 `t_ref mod bar`），只用完整小节。不足 8 小节的歌只有一段“全曲”。
2. 特征（每小节）：原曲单声道 4096 点帧（跳 2048）在 60 Hz–12 kHz 的 24 个对数频带的平均功率（取对数、逐维标准化）+ 人声分轨的小节能量（标准化后 ×3）。
3. 自相似矩阵（余弦相似度）上用前后各 4 小节、高斯渐变的棋盘核求每条小节线的新颖度。
4. 分界：在 [4, n−4] 小节线里取局部峰，从强到弱挑，每段 ≥ 4 小节，最多 8 段，低于最强峰 15% 的不要。
5. 标签：首 / 尾段比各段中位能量低 3 dB 以上、或人声比人声最响的段低 12 dB 以上 → 前奏 / 尾声（至少 3 段时）；其余段能量 ≥ 最低 + 50% 极差 → 副歌（能量差不到 1.5 dB 时只把最响的一段当副歌）；两次副歌之后、紧接着又回到副歌的第一段非副歌 → 桥段；其余 → 主歌。
启发式，只求大致对；标签只决定自动编排用哪种轨道，用户可以改名、拖分界。

### 13.5 自动编排（`engine/choreo.py`）
同类段落按出现次序轮换花样（第 1、2、3… 次出现依次取下表的第 1、2、3… 种，用完从头再来；规则确定，同样输入同样输出）：
| 段落 | 轨道（按出现次序轮换） | 混响量 |
|---|---|---|
| 前奏 | 经典预设换成螺旋、高度 35°（起伏 ±30°）、速度放慢一倍（最多 8 小节一圈），顺时针 | −12 + 4 dB |
| 尾声 | 同前奏，逆时针 | −12 + 4 dB |
| 主歌（用户自己起的段名也按主歌） | 经典 8D → 经典逆时针 | −12 dB |
| 副歌 | 斜向环绕（人声往右前翘 45°、其他乐器往左前翘 45° 逆时针、贝斯固定）→ 镜像斜环（倾斜朝向左右翻过来、转动方向全反）→ 三层环绕；都快一倍（最少 1 小节一圈） | −12 dB |
| 桥段 | 立体交叉（人声左右竖环、其他乐器前后竖环）→ 上下翻滚 | −12 + 2 dB |
| 全曲（整首只有一段） | 经典 8D | −12 dB |
事件：能量最高的副歌一开始，人声 + 其他乐器飞过头顶 2 小节（不超过该段长度）；每个 ≥ 8 小节的主歌（含自定义段名）在第 round(0.75 × 小节数) 条小节线处让人声完全停 1 小节（停顿从这条小节线前 0.5 秒开始减速）。混音用经典预设的默认值。
重新编排（`rechoreograph`，前端“✨”）：沿用传入场景的分界与段名，只换各段轨道、混响量与事件，全曲混音、房间、背后压暗保留；每段小节数 = round(段长 / 小节长)，段的能量用原曲音量起伏 `envelope_db` 在段内的平均功率（用来找最响的副歌）。

### 13.6 按场景的音色补偿（`engine/tone.py`）
- 锚点（实测）：分析时经典场景的完整渲染（干声 + 混响，只过 HRTF 平均补偿 EQ）与原曲比 27 个 1/3 倍频程（39 Hz–16 kHz）的长期平均谱（Welch，两声道相加）→ 差值减去中位数，±6 dB 限幅；原曲里比最响频带低 70 dB 以上的频带不校正。
- 模型（预测）：每路声源沿自己的轨道每秒取 4 个点，取 HRTF 两耳功率（1024 点 FFT 的频带平均，按 (el, az) 双线性插值）× 背后压暗的频响 `|1 − r·cut·H_hp(f)|²` × 距离增益²，按这路声源的频带功率 × (音量 × 校准)² 加权求和；超低频按正前方；混响路 = 送出功率 × 平均 `10^(wet_db/10)` × BRIR 频带功率。染色 C = 空间化后功率 / 同一组音量下不做空间化的功率。
- 场景 EQ（dB）= 锚点 + (10·log₁₀(C_经典 / C_当前) − 其中位数)，±9 dB 限幅；当前场景等于经典预设时正好等于锚点。最终 FIR = HRTF 平均补偿曲线 + 场景 EQ（按对数频率插值到细频点）→ 1025 点线性相位。
- 为什么不直接用锚点：实测锚点对抬高 / 竖直的轨道会过亮（8 kHz +3～5 dB），因为抬高方向的 HRTF 本来就更亮；按场景预测能把所有预设和自动编排都拉回 ±1.6 dB 内（§13.8）。用户自己调的音量 / 静音 / 独奏不会被拉回。

### 13.7 早期反射与母带
- BRIR = 扩散尾巴（每耳能量 1，与 v1 相同）+ 8 个早期反射：方向 (30°, 0°)、(−40°, 5°)、(100°, 20°)、(−95°, −10°)、(170°, 10°)、(−150°, 35°)、(60°, 65°)、(−15°, −45°)，到达 5.3 / 7.1 / 9.4 / 11.2 / 13.9 / 16.8 / 20.3 / 24.1 ms，每晚 1 ms 弱 0.3 dB，150 Hz 高通，总能量为尾巴的 10^(3/10) 倍。作用：让声音“出了脑袋”、增加空间感（中频两耳相关下降）。BRIR 缓存文件名带算法版本（`brir_v2_*`）。
- 限幅更保守：只允许 1% 的 30 ms 片段被压超过 1 dB（v1 是 2% / 2 dB），代价是整体小约 1 dB。

### 13.8 评测（`bench_eval.py` 同一套指标；两首歌：Faded、Die For You；参考 = 抖音 8D 视频）
注：评测脚本 `bench_eval.py` 没有提交进仓库；下表是当时用它量出的结果，不随代码自动更新。

| 指标 | v1.2 经典 | v2 经典 | v2 自动编排 | 原曲 | 抖音参考 |
|---|---|---|---|---|---|
| 音色偏差 RMS | 1.4 / 1.2 dB | 0.2 / 0.2 dB | 0.5 / 0.5 dB | — | — |
| 最大音色偏差 | −3.9 / −3.4 dB @ 8 kHz | −0.4 / −0.4 dB | −1.3 dB @ 13k / −1.6 dB @ 5k | — | — |
| 两耳相关（全频段） | 0.83 / 0.88 | 0.80 / 0.85 | 0.78 / 0.84 | 0.60 / 0.66 | 0.75 |
| 中频（200 Hz–2 kHz）两耳相关 | 0.78 / 0.79 | 0.74 / 0.75 | — | 0.50 / 0.46 | — |
| 动态 LRA（原曲 7.1 / 5.5） | 5.5 / 4.9 | 6.6 / 5.7 | 6.2 / 5.7 | — | 7.9 |
| 响度 | −11.0 / −11.3 LUFS | −12.2 / −12.5 LUFS | −11.7 / −12.1 LUFS | −8.0 / −8.3 | −16.0 |
| 两耳时间差 / 低频左右一致 | 0.68–0.70 ms / 0.998 | 不变 | 0.57–0.63 ms / 0.999 | — | 0.68 ms / 0.965 |
其余预设（歌手绕着你转 / 上下翻滚 / 三层环绕 / 斜向环绕 / 立体交叉，外加当时还在的单点环绕）在 v2 的最大音色偏差都在 ±1.4 dB 内。
全频段两耳相关到不了 0.75：120 Hz 以下固定正前方、两耳相同（红线 §8.1，为了低音不晕、不怪），200 Hz 以下的相关恒为 1；把中频降到接近参考需要更多混响或更宽的声像，会削弱“声音在绕着转”的清晰度，所以只加了早期反射。
