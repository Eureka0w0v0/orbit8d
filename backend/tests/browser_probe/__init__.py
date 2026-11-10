"""浏览器探针：无头 Chrome 把构建好的前端真的走一遍，后台是真实的 create_app + 假分轨器 + 合成的歌。

    cd web && npm run build
    cd backend && uv run python -m tests.browser_probe            # 与 baseline.json 比对，不一致退出码 1
    cd backend && uv run python -m tests.browser_probe --update   # 有意改了试听 / 界面行为之后重新生成基线

不进 make test（要 Chrome，跑一遍约一分钟）。改前端或 HTTP 接口之后跑它；与基线不一致时先单独重跑一次，
分清是机器太忙造成的偶发差异还是真的回归。数据目录是临时的；HRTF 数据从 ORBIT8D_DATA_DIR（默认仓库的
data/）的 assets 只读软链接过来，分轨模型用不到。

走的流程（script.py）：导入并打开歌 → 播放 / 暂停 → 空格 → B 键切原曲 → 方向键跳转 → 拖播放头（暂停时、
播放中）→ 改参数看到“已保存” → 撤销 / 重做 → 停掉后台看到“保存失败”、再起后台恢复“已保存” → 导入第二首 →
从歌曲菜单换歌 → 导出并下载 → 删歌（不在用的、正在用的）。

每一步都断言关键界面状态，并且不许有意料之外的错误：未捕获的异常、未处理的 Promise 拒绝、console.error、
失败的网络请求。意料之中的只有两种：还没保存过场景的歌 GET /scene 返回 404（前端据此改用自动编排）；
“停掉后台”那两步里连不上后台。

基线里逐步比对的内容（recorder.js 记录）：
- dom：阶段、8D / 原曲、保存状态、是否在播放、歌名、撤销 / 重做按钮、选中的形状、各轨静音、歌曲菜单行数等。
- params：每个 AudioParam 各自的调度序列（方法 + 目标值 + 时间）。只比“每个参数自己的先后”，不比不同参数
  之间的穿插次序（那取决于动画帧与网络应答谁先到，对声音没有影响）。
- messages：发给 AudioWorklet 的消息类型序列（init / params / start / stop）。
- graph：建节点、connect / disconnect、声源 start / stop、decodeAudioData、resume。排序后比较：
  两个同时结束的声源谁先触发 ended 不确定。

归一化与不比的量：
- 节点按创建次序编号（gain1、source2…），不依赖对象身份。
- 时间：同一段同步代码里出现的第一个时间记成 @now 或 @now+0.06（相对 currentTime，按 30 ms 取整），
  其余时间记成相对它的差（精确到 0.1 ms）。Chrome 里 currentTime 在同一段同步代码中读两次可能差一个渲染量子，
  而引擎安排一次淡出要读好几次；探针让同一段同步代码里读到的 currentTime 保持不变，记录才不会抖动。
- 数值保留 3 位小数。cancelScheduledValues 之后紧跟的 setValueAtTime 的值是“当时读到的当前值”，取决于
  操作落在淡入淡出的哪一刻，记成 <current>，不比。
- 不比：声源 start 的 offset、Worklet 消息的内容（startFrame、songOffset、渲染参数）、播放位置的具体秒数、
  电平表读数（只断言播放时有信号）、动画帧数与耗时（打印出来供参考）。
"""
