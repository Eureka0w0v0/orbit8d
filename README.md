# Orbit 8D

把普通音乐做成**可自定义的 8D（双耳环绕）音乐**：自动把歌拆成人声 / 鼓 / 贝斯 / 其他乐器四轨，每条音轨在 3D 白模人头周围沿你设定的轨道运动，边拖边听，满意后导出 m4a / mp3 / flac / wav / ogg。

- 真实人头测量的 HRTF（Neumann KU100，全球面 2° 网格），不是简单的左右声像摆动
- 6 种轨道形状（圆、椭圆、钟摆、8 字、螺旋、固定），可调距离、速度（按小节或秒）、方向、起点、高度、前后 / 左右倾斜、水平转向
- 三层半球网格（环绕层 / 高度层 / 顶层），一键把轨道移到头顶或耳朵高度
- 120 Hz 以下的超低频固定在正前方，不会出现一只耳朵轰低音
- 浏览器里实时试听（AudioWorklet），与导出使用同一套算法和数据
- 纯本地运行：服务只监听 127.0.0.1，歌曲不会上传到任何地方

> 8D 效果必须**戴耳机**听，外放听不出来。

## 快速开始

需要：macOS（推荐 Apple 芯片，分轨会用 GPU）、[uv](https://docs.astral.sh/uv/)、Node.js 20+、ffmpeg（`brew install ffmpeg`）。

```bash
make setup   # 安装前后端依赖，下载 HRTF 数据（约 20 MB，校验 sha256）
make run     # 构建前端并启动服务，自动打开 http://127.0.0.1:8765
```

第一次分轨时会自动下载 Demucs 模型（几百 MB，需要联网），之后可离线使用。

## 使用

1. **导入**：把歌曲拖进窗口（或点击选择文件）。依次经过解码 → 分轨（约为歌曲时长的 0.3 倍）→ 测速与校准。同一首歌再次导入会直接打开。
2. **看与听**：空格播放 / 暂停。拖动画面旋转视角，滚轮缩放。每条彩色轨道对应一条音轨，发光小球就是声音的位置；底部电平表显示左右耳音量。
3. **改轨道**：
   - 点击轨道或小球选中音轨，右侧面板显示它的全部参数；
   - 拖动轨道左侧的**白色圆点**改距离，拖**正前方**和**右侧**的小圆环改前后 / 左右倾斜；
   - 暂停时可以直接拖小球改起点（圆、椭圆、螺旋、固定）。
4. **分层与预设**：右侧“所在层”一键切换环绕层（耳朵高度）/ 高度层 / 顶层（头顶绕圈）；左侧一键预设：经典 8D / 歌手绕着你转 / 双环反向 / 上下翻滚 / 单点环绕（整首歌一个声源，模仿常见的 8D 视频）。
5. **混音**：每条音轨可调音量、静音（M）、独奏（S）；右侧“空间”一栏可切换房间（小房间 / 大厅 / 教堂）、混响量和“背后压暗”（转到脑后时高频变暗的程度）。
6. **导出**：右上角“导出”，选择格式后在后台做高质量渲染，完成后自动下载 `<歌名> (8D).<扩展名>`，保留原曲的封面和标签。

## 开发

```bash
make dev     # 后台 127.0.0.1:8765 + Vite 开发服务器 127.0.0.1:5173（热更新）
make test    # Python 与 TypeScript 全量测试
make lint    # ruff 检查与格式校验
```

```
backend/orbit8d/
  engine/     轨道公式、HRTF、分块渲染、混响、母带、测速、场景模型、整曲管线
  media/      ffmpeg 封装（格式白名单、编码、封面）
  separate/   Demucs 分轨
  jobs/       显式状态机、记录存储、任务队列
  api/        HTTP 接口
web/src/
  orbit/      轨道公式（TS 版，与 Python 逐点一致）
  audio/      实时双耳渲染核心、AudioWorklet、试听引擎
  scene/      Three.js 舞台、白模人头、轨道视图、拖拽把手
  ui/         面板与控件
shared/golden/  跨语言一致性测试数据（Python 生成，TS 校验）
```

完整设计、算法和不变量见 [docs/SPEC.md](docs/SPEC.md)。运行数据（上传、分轨、导出、日志）都在 `data/`，可以随时删除。

## 第三方素材与许可

- 人头模型：“Infinite, 3D Head Scan” by Lee Perry-Smith，[CC BY 3.0](https://creativecommons.org/licenses/by/3.0/)（`web/public/models/LeePerrySmith_License.txt`），取自 three.js 示例。
- HRTF：TH Köln，“A Spherical Far Field HRIR/HRTF Compilation of the Neumann KU 100”（B. Bernschütz），CC BY-SA 3.0；不随仓库分发，由 `make assets` 从 sofacoustics.org 下载。
- 分轨模型：[Demucs](https://github.com/facebookresearch/demucs)（MIT）。
- 3D 渲染：[three.js](https://threejs.org/)（MIT）。
