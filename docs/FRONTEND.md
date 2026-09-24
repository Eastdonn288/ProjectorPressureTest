# PPTP — 前端交互设计沉淀

> **本文档是"前端交互模型"的唯一 owner**:界面为什么长这样、每一次交互为什么这么定、踩过什么坑、还有什么没定。
> 代码结构 / API / 数据模型在 [ARCHITECTURE.md](ARCHITECTURE.md),踩坑清单在 [PITFALLS.md](PITFALLS.md),已批准的设计决策在 [DECISIONS.md](DECISIONS.md);**凡是前端交互(卡片绑定、切换、合成卡、图表)的争议,以本文为准。**
>
> **版本**: v2.11.2 / 2026-09-18
>
> ⚠️ 本文**不写任何行号** —— 行号会漂,函数名不会。定位代码请直接 grep 标识符(行号已在本文件上出过一次事故)。

---

## §1. 已敲定的交互决策

每条格式:**决策 / 内容 / 为什么不那样做**。本节的字段名、函数名、CSS 类名均已对照当前 `static/app.js` / `static/index.html` / `static/style.css` 逐条复核。

| # | 决策 | 内容 | 为什么不那样做 |
|---|---|---|---|
| 1 | **全局单日志面板** | 整个平台只有**一个** `#log-console` + 一个 `#log-info` + 一个 `#perf-chart`,全部绑定到单一的 `state.currentTaskId`。`#perf-chart`(200px)在 DOM 里就夹在 `#log-info` 与 `.panel-body` 之间。设备卡 / 任务卡只是"把 `currentTaskId` 指到哪"的**入口**,不是数据容器。 | **不做 per-card / per-device 独立终端。** 每卡一个终端 = N 条 WS + N 份缓冲 + N 套滚动状态,而用户一次只看一个任务;多出来的 N-1 套全是维护成本,还得在切卡时同步 —— 收益为零。 |
| 2 | **卡片绑定与切换** | 设备卡点击 → `onSelectDevice(serial)`,3 分支:① 该设备有 `running` / `interrupting` 任务 → `viewTaskLogs(running.task_id)`;② 否则取该设备最近任务(`started_at` 倒序)→ `viewTaskLogs(last.task_id)`;③ 都没有 → 关残留 WS、`currentTaskId = null`、`setConsoleTarget(null, serial)`、`clearConsole()`、`syncPerfChart()`,进入"空日志面板"态(`renderLogInfo` 显示"查看设备 X 的日志(该设备暂无任务)")。任务卡点击 → `viewTaskLogs(task.task_id)`,同一个函数。`viewTaskLogs` 幂等:`state.currentTaskId === taskId` 时只重刷卡片高亮(`renderTasks` + `renderScripts`)并 return。 | 幂等是**为了不出现重连风暴** —— 重复点同一任务若走"关 WS + 重开",一次误双击就是一次完整 replay。切换时顺带把 `state.selectedDeviceSerial` 改成该任务的设备,是为了不让脚本面板的高亮残留在上一台设备上(否则用户得再点一次设备卡才看得对)。 |
| 3 | **切任务的语义** | 切任务 = 换 `currentTaskId` + `setConsoleTarget` + `clearConsole()` + `userScrolledUp = false` + `_wsReconnectAttempts = 0` + `openWs(taskId)` + `syncPerfChart()` + 重刷三类卡片。 | 不"保留旧 console 内容等新日志来覆盖":那会让用户分不清屏幕上的行属于哪个任务。清空是唯一不会误读的语义。 |
| 4 | **WS 帧过滤(总闸)** | `onmessage` 第一行 `if (state.currentTaskId !== ws._taskId) return` —— 旧连接推来的帧直接丢弃。**任何新增的 WS 帧处理都必须过这个闸。** | 不滤的话,切到空设备后旧设备的 WS 还在连,旧日志会"复活"并写进本该是空的 console。这是"切设备时旧日志仍出现"这类 bug 的根因。 |
| 5 | **WS 关闭与重连** | 关闭三条路径:**①** `openWs` 开新连接前无条件 close 旧连接;**②** `onSelectDevice` 的空任务分支显式 `close()` 并清 `currentTaskId`(对 ① 的双保险,同时释放 server 端 fd);**③** `clearConsole()` **不关 WS**,它只清 DOM。重连:`onclose` → `scheduleReconnect(taskId)`,守卫依次是"正在关服 / `currentTaskId !== taskId`(用户已切走)/ 任务已终态 / 已有重连定时器在排队";退避 `min(1000 * attempt, 5000)` = 1s/2s/3s/4s/5s,超过 5 次放弃并往 console 打一行英文提示让用户刷新(`state._wsReconnectAttempts` 计数,`ws.onopen` 与用户主动切任务时清零)。 | **不无限重试** —— server 真的挂了就是白推,还会掩盖"已经连不上"这个事实。**切走的任务不重连** —— 否则后台会攒着一堆没人看的连接。**不弹错误框** —— server 重启/抖动是常态,自动恢复比打断用户更友好。 |
| 6 | **合成卡 A:临时离线** | `renderDevices()` 对"`status` ∈ `running` / `interrupting` 且不在当前 `adb devices`"的设备合成 `_temp_offline: true` 的卡,model 文案"(临时离线 · 任务在跑)"。`interrupting` 也算 live —— 中断重启测试时脚本还在等设备回来,卡不能在这几秒里凭空消失。`isOffline = !isTempOffline && !isBattOff && d.status !== "device"`,即**临时离线不算离线**:卡可点、可换脚本、可硬停。脚本下拉框仍锁(`notSelected \|\| !!running \|\| isOffline`);样式 `.device-card-temp-offline` = 虚线琥珀边(`var(--warn)`)。硬停按钮只在"当前任务设备的设备临时离线"时启用(`renderLogInfo` 里 `isTempOffline = !liveSerials.has(t.device)`)。`refreshDevices()` 的 `keptSerials`(= runningSerials + 放电关机设备)保住这些设备的 `deviceScripts` / `deviceSequences` 不被清。 | **不把掉线画成红色"离线"、不加弹窗/横幅/toast。** 离线在压测场景里是**计划内**的正常状态;把计划内重启画成故障,会让用户对真故障脱敏(用户原话:「不能因为掉线或重启就把任务卡片设备卡片清掉」)。 |
| 7 | **合成卡 B:放电关机** | `renderDevices()` 在临时离线卡之后,对满足**四条**的设备合成 `_batt_off: true` 的卡:① 脚本是 `battery_inout_stress.py`;② 状态 ∈ `finished` / `interrupted` / `failed`;③ 设备不在 `adb devices`;④ **该 battery 任务仍是该设备最新任务**(`latestTaskOf()` 校验,防被后续任务顶掉)。model 文案按 `latest.params.mode` 分"(放电测试 · 设备已关机)" / "(电池测试 · 设备已关机)",状态文案"设备已关机",badge "放电关机"(红)。`isOffline` 同样排除 `_batt_off`;空态检查也排除 `battOff` —— 设备全关机、只剩放电关机卡时仍显示卡,而不是"未检测到 ADB 设备"。样式 `.device-card-batt-off` = 虚线红边(`var(--err)`),与临时离线的琥珀一眼区分。退出:设备重新出现在 `adb devices` → 自动回正常在线卡;删除对应任务 → 卡移除。 | **不让卡消失**:放电模式以"设备关机"作为正常终止信号,旧逻辑会让卡直接消失,看起来像"设备丢了"。**不改成后端下发**:合成卡与临时离线卡同源,都是前端基于"实时任务 + 实时 adb 列表"推导的视图态,后端保持无状态更简单。 |
| 8 | **性能图表绑定与切换** | DOM 是 `#perf-chart`,`syncPerfChart()` = dispose + 隐藏 / rebind + 重建,由 `state.currentTaskId` 驱动(与 console 同一个"当前任务"模型,不是每卡渲染)。类型按脚本名泛化:`PERF_SCRIPT_KINDS` 把 `perf_monitor.py` → `"perf"`、`battery_inout_stress.py` → `"battery"`,`perfKind(taskId)` 查 kind,`isPerfTask` = kind 非 null——**新增会打 `PERF\|` 的脚本必须在这里注册**。不残留三件套:① WS 帧过滤(见行 4);② `state.perfSamples[taskId]` 每任务独立缓冲(`PERF_SAMPLE_CAP = 86400`);③ `resetPerfBuffer(taskId)`(`ws.onopen` 调)+ `pushPerfSample` 按 `t` 去重(WS 重放会把同样的样本再喂一遍)。**图表逻辑只在离散导航点调用**(`viewTaskLogs` / `ws.onopen` / `onSelectDevice` 空任务分支 / `onDeleteTask` / `onCleanupTasks` / `onResetClick`),**绝不在 2s/3s 轮询里**。 | **不在 2s 轮询里碰图表** —— 用户切走后图表会被持续重建、闪烁。**不用 dataZoom** —— 它让用户能拖出 1h 视野,和"每帧拉回最新"互相打架。**不并排双图** —— 全局单面板 + 单 `currentTaskId` 模型下任何时刻只显示一个任务的图,按 kind 分支比维护两套图表实例便宜得多。 |
| 9 | **数据流:`PERF\|` 行到图表** | 脚本打 `PERF\|` 行 → 现有 WS 原样流转(server 零改动)→ `appendLogLine` 拦前缀 → `handlePerfLine`:`type === "meta"` 存 `state.perfMeta`(含 `kind`);`type === "sample"` 入 `perfPending` 并返回可读行;**`type === "event"`** 按 `PERF_EVENT_LEVEL` 渲染成 `[perf] t=… \| [!] kind: reason`(级别缺失时标 `?`)。**未知 type 只打一行 `(unrecognized record type: …)`,绝不回显原始 JSON** —— 机器数据可能任意长,而且每次重连都会重放一遍。渲染:`flushLogBuffer` 末尾调 `flushPerfChart`(rAF 批处理),选项由 `buildPerfOption(taskId, fullRange)` 构建,系列键统一由 `perfSeriesAndKeys()` 的 `perfSeriesKeys` 驱动(`backfillPerfOption` / `flushPerfChart` / 导出循环全部用它,与 kind 无关)。横轴 `type:"time"`,坐标 `perfX(s) = s.clock ?? perfXBase + s.t * 1000`;live 分支固定 **1h 滚动窗口**(`PERF_WINDOW_MS = 60 * 60 * 1000`,`interval: 10min` + `splitNumber: 6` 钉死 10 分钟刻度),每轮 flush 把 min/max 滚到最新样本并 `shift()` 掉窗外点。双 Y 轴:perf 左轴 0-100%,前台APP(`fg_cpu`)右轴 0-400%(`top` 多核 %CPU 可超 100);GPU 线只在 `meta.sources.gpu` 为真时存在,前台APP 只在 `sources.fg` 为真时存在。 | **分发键是 `type:"sample"`,不是"有 cpu 字段"** —— 脚本缺 `type:"sample"` 时样本会被整批丢弃(症状:不出线 + 导出只剩 txt,同源)。**窗口只管"看"不管"存"**:完整历史仍留在 `perfSamples[taskId]`,CSV / 全程 PNG 不受 1h 窗口影响。 |
| 10 | **每设备 / 每脚本独立** | `state.deviceScripts[serial]`、`state.deviceSequences[serial]`(序列**文件名**)、`state.deviceParams[serial][script]` 三层全部按设备隔离,设备 A 选什么绝不影响 B。脚本参数走**自描述 schema**:脚本用模块级 `PARAMS` 声明字段 + `--dump-params` 打印,前端 `paramInputHtml` 按 `type` 渲染 `input`(text / number / checkbox)或 `select`(枚举),保存后跑任务时合并进 `--params` 透传。 | **不做全局共享选择** —— 真实压测就是多台投影仪跑不同序列(老化 vs 工厂)。**不在前端硬编码表单** —— 每个脚本参数不同,硬编码无法扩展;前端 / 平台代码一行不改,新增脚本只需定义 `PARAMS`。枚举不做纯文本输入 —— `sensor=gsensor/tof` 这类值太容易拼错。 |
| 11 | **localStorage 持久化边界** | key 固定 `pptp.deviceState.v1`,写入防抖 200ms。**只存**:`selectedDeviceSerial`、`deviceSequences`、`deviceParams`、`deviceSerialPort`。**不存**:`deviceScripts`(会话状态)、`deviceSerialCapture` 与 `deviceLogcatCapture`(每次运行的意图)、`devices` / `scripts` / `tasks`(永远从 server 拉)。 | **`deviceScripts` 不持久化是一次决策反转**(2026-07-20):用户反馈"关机再开,设备卡脚本选择不是默认状态,而是上次选的"。语义拆开就清楚了 —— 配置(哪个设备跑哪个序列)留下,会话意图(现在想跑什么)不留。**勾选框同样不持久化**:一个陈旧的"开"会在下次会话静默占住 COM 口。**不加"重置"按钮来区分刷新 vs 重启**:同一个 localStorage 分不清 Ctrl+F5 与 server restart,而运行中的下拉框本来就置灰、非运行状态下拉框清空也无害,**用户明确接受这个副作用**。 |
| 12 | **console 渲染:rAF 批处理 + 上限** | `logBuf` 累积,`requestAnimationFrame` 一次性 `textContent +=`;DOM 上限 `LOG_DOM_CAP = 1000`(超出时在顶部插一行英文 `(truncated N lines; full log: <path>)`,路径由 `logFileName()` 取服务端下发的归档名);`state.userScrolledUp` 为真时不抢滚动条。**`#log-info` 每 2s 被 `renderLogInfo()` 整体 `innerHTML` 重写,任何需要跨轮存活的东西都不能放进去。** | 不批量的话,replay 2000+ 行时逐行 `textContent +=` 是 O(n²),肉眼可见地卡;1000 行抄 VSCode 终端默认 scrollback,取"不卡"与"够读"的平衡点。 |
| 13 | **只显示 stdout + console 全英文(两条平台铁律)** | **①** logcat(恒开)与串口(按需)照采、照归档,但**前端永久不展示**;`openWs` 只订阅 `?sources=stdout`,那些通道的帧根本不到浏览器。v2.6.0 的通道显示方案(计数 pills、`#channel-bar` 缝、`TABS_ENABLED`)已在 v2.7.0 **整体删除,不要再加回来**。**②** 凡打进 `#log-console` 的内容**必须全英文 / ASCII**,时间戳统一走 `fmtClock`(`toLocaleTimeString("zh-CN", { hour12: false })`)—— `hour12:false` 是关键,少了它 zh-CN 浏览器会吐"下午3:30"这类中文进日志。UI 文案(卡片 / 弹窗 / 图表系列名 / 导出表头)仍是中文。判据只有一条:**进不进 `#log-console`**。 | 用户原话:「既然 logcat 和串口日志太多的话,建议不考虑做他们两个的前端显示了……建议还是稳定简单为主导。」**删掉显示方案时保留了唯一的信息面**:任务结束时由服务端广播进 console 的那行 `[archive] <模块>/<目录>/ \| stdout NL \| logcat NL \| serial NL \| …` 摘要清单(它报的是**条数**,不是日志内容,所以不违反"不展示 logcat/串口")—— 删掉计数 pills 之后,串口采集失败会变成完全没有信号,而静默失效最容易被当成 bug 报。 |
| 14 | **导出与存档** | 导出按钮 `#btn-export-log` 自 v2.7.0 起 `hidden disabled`(handler `onExportLog` 保留,去掉 `hidden` 就能回来)。每次运行结束自动归档到 `archive/<模块>/<时间>_<脚本>_<设备>/`,含三份日志 + `report.json` + `summary.json`;**`chart.png` 由浏览器在任务结束时离屏渲染后回传**(`archiveChartIfAvailable`,服务端故意不引入绘图库;服务端的 artifact 上传口同时接受 `perf.csv`,但今天的自动路径只回传 png)。浏览器没开就没有图 —— 用户明确接受,`summary.json` 如实记录缺了哪些产物。任务卡上"报告"按钮只在真的存在中文 HTML 报告时才渲染(`htmlReports()` 读归档清单),"存档"按钮在资源管理器里打开该目录。**`×` 与「清空已完成的卡片」只移除列表条目,不动存档**(服务端 `_forget_task` 明确不碰磁盘)。 | **存档与"当前视图"必须解耦**:导出应当只依赖数据(`perfSamples[tid]` / 归档目录),不依赖视图状态。**离屏全程图导出**(`exportFullChartDataUrl`)就是把这条原则落到实处 —— 切走之后再导出历史任务,照样是全程图。**"×" 不删存档**:既然承诺了永久保留,一次误点就不该摧毁它。 |
| 15 | **缓存破坏 + 参数弹窗入口** | `index.html` 的 3 处资源引用(style.css / echarts vendor / app.js)都带 `?v=<版本>`,服务端 `NoCacheMiddleware` 对静态文件发 `Cache-Control: no-store, no-cache, must-revalidate`。脚本参数弹窗(`openParamsModal`)与序列弹窗(`openSeqModal`)都只有**一个**入口 —— 点脚本卡:带 `--dump-params` schema 的走参数弹窗,`ir_runner.py` 走序列 picker;序列 picker 点一行即提交(无"保存"按钮)。 | 平台经常小改前端,F5 不够硬,开发流程接受 Ctrl+F5 当正常操作。**不为两种入口做两套 modal** —— 复用同一个弹窗、只换标题与内容,省掉一份重复 UI。设备卡不再放序列选择器:那是脚本级的东西,混在设备卡上会让人以为序列是设备的属性。 |

### 1.1 `state` 形状(字段 → 归属)

`state` 对象是上表的落点。标 ★ 的字段会被写进 `localStorage`(见行 11),其余只在会话内存里活。

```js
const state = {
  devices: [],                  // GET /api/devices
  scripts: [],                  // GET /api/scripts
  tasks: [],                    // GET /api/tasks
  deviceScripts: {},            // { [serial]: "ir_runner.py" }   设备→脚本(不持久化!)
  deviceSequences: {},          // ★ { [serial]: "default" }     设备→序列文件名
  deviceParams: {},             // ★ { [serial]: { [script]: {param: value} } } 设备×脚本参数
  selectedDeviceSerial: null,   // ★ 当前选中的设备(驱动脚本卡高亮 / 下拉框解锁)
  currentDeviceSerial: null,    //   日志面板绑定的设备(空面板态下用来显示"设备 X 暂无任务")
  currentTaskId: null,          //   日志面板 + 性能图表共同的绑定键
  perfMeta: null,               //   当前任务的 PERF| meta 行(含 kind: "perf" | "battery")
  perfSamples: {},              //   { [taskId]: [样本] } 每任务独立缓冲(切任务不残留的数据层)
  currentWs: null,              //   当前 WebSocket
  backendOnline: false,         //   顶栏 ● pill
  userScrolledUp: false,        //   console 不抢滚动条
  server: null,                 // GET /api/server/status
  shuttingDown: false,          //   关服中:scheduleReconnect 直接放弃
  seqContext: null,             //   ⚠ 死字段,全库无引用(真正的 context 是 _seqPickerContext)
  deviceSerialCapture: {},      //   设备卡"串口日志"勾选(每次运行的意图,不持久化)
  deviceSerialPort: {},         // ★ { [serial]: "COM9" } 设备→串口口(偏好,持久化)
  serialPorts: [],              // GET /api/serial/ports 缓存
  deviceLogcatCapture: {},      // { [serial]: false } = 显式关掉 logcat(缺省/true 都表示采)
  archiveStats: null,           // { count, bytes } 存档占用(顶栏 hover)
  chartPosted: {},              // { [taskId]: true } 已回传过 chart.png,防 2s 轮询重复上传
};
```

与图表 / 日志相关的**模块级单例**(不在 `state` 里,但同属"当前任务模型"):`perfChart` / `perfChartTaskId` / `perfPending` / `perfOption` / `perfSeriesKeys` / `perfXBase`(图表绑定与横轴锚点)、`logBuf` / `logFlushScheduled`(console 批处理)、`_lastKey`(三类卡片的重渲染短路键)、`_seqPickerContext` / `_paramsCtx`(两个 modal 的上下文)、以及常量 `PERF_PREFIX` / `PERF_SAMPLE_CAP` / `PERF_WINDOW_MS` / `PERF_SCRIPT_KINDS` / `PERF_EVENT_LEVEL` / `LOG_DOM_CAP` / `STORAGE_KEY`。

**注意**:`deviceSequences` 存的是**序列文件名**(不是 ini 正文)—— `onRunClick` 里直接拼成 `ir_sequences/<name>.ini`;`app.js` 里它上面那段注释写的是"ini 正文",**注释已过时,以用法为准**。

---

## §2. 采纳记录(被否掉的方案才是重点)

按时序排列,但**日期与版本号已剥离**(那是 [CHANGELOG.md](../CHANGELOG.md) 的职责)。每条保留的是:**当时没选什么、以及为什么不选**。

- **设备卡"运行中"pill 去重 + 配色区分** —— 否掉"active + 临时离线时右上再挂一个 ⚡ 运行中 pill":它在视觉上和蓝色的"✓ 已选中"冲突,同一张卡上两个角标互相抢。改为"运行中"只在 `device-actions` 里出现一次,配色换成橙色 `--warn` + 斜体加粗,与蓝色选中 pill 分成两个类别;`style.css` 里那条 `::after` 规则已删除(注释仍在)。
- **`deviceScripts` 不持久化(决策反转)** —— 否掉"刷新保留脚本选择":重启后卡片显示上次选的脚本,语义不对。改为只持久化配置类字段。也否掉"加一个重置按钮来区分刷新 vs 重启":同一个 localStorage 分不清两者,而运行中下拉框本来就置灰,非运行状态下拉框清空无害 —— 加按钮反而引入"什么时候点?"的新认知成本。
- **临时离线设备合成卡** —— 否掉"让卡直接消失"。
- **"硬停"按钮(临时离线专属)** —— 问题:设备掉线时子进程可能卡死,`stop/{task_id}` 发 CTRL_BREAK 没用;所以必须有一条只在 temp-offline 时启用的 SIGKILL 通道。
- **日志 replay 截断提示** —— 否掉"静默截断":截断后用户不知道前面还有内容,会以为日志丢了。
- **删除任务 = 删除日志文件**(**已被 v2.7.0 反转**)—— 当时否掉"留在磁盘上累积";后来改成了"存档永久保留、`×` 只移除列表条目"。反转理由见本文件末尾几条。
- **"未选脚本" muted 提示** —— 问题:设备没选脚本时卡片的 actions 区空着,用户疑惑;补一个灰色 11px 的 `未选脚本`。
- **任务卡 active 高亮** —— 问题:`state.currentTaskId` 在任务面板里原本没有任何视觉区分,用户看不出"日志面板现在在看哪个任务";给当前任务卡加 `.active`。
- **设备卡 chip 显示已选脚本** —— 问题:选了脚本后 actions 区只显示"未选脚本"或"运行中",**选的是哪个脚本不直观**;在 row2 补 `device-script-chip`(脚本名,去掉 `.py` 后缀)。
- **切设备无任务时 console 显示"该设备暂无任务"** —— 问题:切到无任务设备后,console 会残留上一设备的最后几行(被 rAF flush 延后);`clearConsole()` 之后必须再 `setConsoleTarget(null, serial)`,否则空 console 无法区分"没有任务"与"日志还没来"。
- **default.ini 5/6 字段 bug** —— parser 是 5 字段而 ini 是 6 字段,直接报 Key not found。删掉 `name` 字段,统一到 5 字段。
- **IR 脚本融合 + Long 按键真长按** —— 否掉"代码拆三个文件 + 外部 keyevent.txt 数据依赖",合并为单文件。**更关键的是否掉了"循环发短按"的假长按**:在 duration_ms 内反复 down+up,设备收到的是一堆短按;改为 `send_key_down` → `sleep(duration_ms)` → `send_key_up` 的真"按住"。
- **四个 UI bug** —— ① 运行中脚本卡不置灰(会让人以为能再跑);② 切任务卡不联动脚本面板;③ 离线设备任务切后脚本高亮残留;④ 设备卡左下渲染出字面量 `undefined`。四个都修在"渲染判定"这一层,不是加补丁。
- **脚本参数前端可配置(自描述 schema)** —— 否掉"前端硬编码表单"(无法扩展),也否掉"参数存全局"(会跨设备串味)。
- **参数类型新增 `select`** —— 否掉"枚举也用文本输入":拼错不报错、静默跑错场景。
- **性能图表实时曲线** —— 否掉"每卡渲染一个图表":全局单面板模型下任何时刻只显示一个任务。也否掉"在 2s 轮询里刷新图表":切走后会持续重建、闪烁。
- **图表横轴墙钟时间 + 双 Y 轴** —— 否掉"单 Y 轴 0-100%":前台 APP 的 `top` 多核 %CPU 实测可到 116-123%,单轴会把线顶格裁掉(原需求本就允许"Y 轴可以不同")。长时检测的诉求是**刻度数稳定**(约 10 个)而不是"永远显示全部刻度" —— 跨度越长,单格越大。
- **perf 管道按脚本类型泛化(`perfKind`)** —— 否掉"给电池复制一套图表管道"(维护两套实例),也否掉"同屏并排双图"(与全局单面板 + 单 `currentTaskId` 模型冲突)。
- **log console 全英文 + 固定 1h 滚动窗口** —— 否掉"x 轴显示全部":用户要的是"实时显示 1h",像心电图一样滑。否掉 `dataZoom`:它和逐帧拉回最新互斥。
- **放电关机卡** —— 否掉"让卡消失"(看起来像设备丢了),也否掉"后端下发视图态"(合成卡本来就是前端推导)。
- **导出图片改为全程图** —— 否掉"直接 `getDataURL` 当前图表":那只能导出当前绑定任务的 1h 窗口,切走后 `perfChart` 为 null 直接跳过 png。用户当时还说"要是很难做的话建议直接通过 csv 生成图表也可以" —— 采纳了"不依赖屏幕"的思路,但改成浏览器离屏渲染,免去手动步骤。
- **日志通道化(先只展示 stdout,但按通道就位)** —— 否掉"现在就做多缓冲 console":那就是给一个还没被批准的设计付实现成本。当时的正确交付物是**协议 + 状态 + 路由**,判据是"打开 tab 需不需要改 server.py 或 WS 协议"。
- **任务结束自动存档 + 删掉 logcat/串口的显示方案** —— 真正的问题不是"没存档"而是"找不到":日志一直在 `logs/`,但 32 位 `task_id` 打头,人眼认不出,界面也没有入口。否掉"把 logcat 也塞进 console":那既解决不了"找不到",又让 UI 更复杂。
- **存档收纳四处调整** —— 否掉"打开存档留在任务面板":用户原话「打开存档应该是全局功能,所以应该放在优先级更高的位置」;判据是"这个按钮管的是平台级的东西还是这一栏的东西"。也否掉「清空已完成」这个旧名字(行为没变但语义变了 —— 改了行为必须回头改文案)。
- **掉线不清卡片** —— 否掉"再加 banner / toast / 日志行提醒":用户原话「不需要,直接拿之前的【临时离线】就行了」。**教训**:"离线"在压测场景里是正常状态,不是错误状态。
- **参数弹窗顶部的脚本自述 `hint` 条(v2.15.0,D-70)** —— 问题:`power_cycle_stress` 有一条**跑之前就必须满足**的前提(设备上电方式必须改成 `Direct`),不满足时脚本不报错、只会一直等一个永远不来的开机。否掉的两个方案:① **前端硬编码**那句中文(违反 D-47,前端不该知道任何脚本名);② **做成一条只读参数行**(会进 `fields`、进而污染报告参数表与 `params_values`,而且它不是参数)。改为**脚本自述一个顶层 `hint` 字符串**,服务端原样转发(缺省 `""`,全部脚本统一多这一个键),前端一视同仁地渲染。三条实现约束写在这里,防止后人"顺手优化"掉:① DOM 上它**不是 `fields`** —— `saveParams` 遍历 `_paramsFields`,天然不碰它;② 它放在 **`#params-form` 之外**(表单是 `max-height` 可滚动容器,放进去会被滚走);③ 用 **`textContent` 而非 `innerHTML`**。`loadParamsSchema` 也因此改成**缓存整个响应** —— 以前只留 `r.fields`,那正是 hint 会静默消失的地方。
  > **这不是 D-09 / D-36 禁掉的横幅/toast/状态条**,也**不是** D-47 禁掉的硬编码:它静态、不随运行状态变、只在用户**主动打开**参数弹窗时出现一次。判据是"它是**运行前的前置条件**还是**运行中的状态**" —— 离线是状态(所以 D-09/D-25 那套处理),上电模式是前置条件。**删它之前先读 D-70。**
  > **已知边界**(不修):弹窗只由脚本卡片入口打开,而那个入口要求脚本声明了 `--dump-params` ⇒ **没有参数的脚本即使有 hint 也显示不出来**。
- **hint 复用 `.seq-warn` 样式,`style.css` 零改动** —— 那已经是现成的琥珀色警示条(含 `b` / `-sub` / `code` 三种内部样式),为一条文本再引入一套样式不划算。

---

## §3. 开放问题 / 待拍板

已按当前代码复核:**仍未决** / **已解决**。解决的写清是哪个版本或哪处代码确认的。

### 3.0 ⛔ 日志通道 tab 切换 —— **已关闭,不做**

- **原计划**:v2.6.0 把数据层 / WS 协议 / 渲染路由都按通道预留好,只差 tab UI(`TABS_ENABLED = false`)。
- **关闭理由(用户原话)**:「既然 logcat 和串口日志太多的话,建议不考虑做他们两个的前端显示了。前端只保留 stdout,直接将 logcat 和串口日志的前端显示方案删掉。建议还是稳定简单为主导。」
- **已执行**:`CHANNELS` / `TABS_ENABLED` / `state.activeSource` / `state.captureCounts` / 计数 pills / `#channel-bar` 节点与 CSS / `onmessage` 的 capture 分支 / 服务端 2s 计数心跳 —— **全部删除**。今日复核:`app.js` / `index.html` / `style.css` 中 `cap-counts`、`cap-item`、`cap-dot`、`channel-bar`、`channel-tab` 的命中数为 **0**(源码已无,只剩文档在讲这段历史)。logcat 与串口继续在服务端采集并归档,只是前端永不展示。
- **留下的信息面**:任务结束时 console 里一行归档摘要。这不是"半个 tab",是删掉 pills 之后**串口采集失败唯一的信号**。
- **状态**:⛔ 已关闭。**如果将来又要做通道展示,先回来重读这一条。**

### 3.1 ir_runner 无限循环 vs N 圈配置 —— **部分已解决**

- **现状**:`scripts/ir_runner.py` 现有 `--loops N` 这个 CLI 参数,`loops=None`(缺省)即无限循环。但 `ir_runner.py` 是唯一**没有** `--dump-params` schema 的脚本(它走序列 picker 那条路),所以平台参数弹窗里配不了循环数。
- **未解决的部分**:平台 UI 上没有"跑 N 次"的入口,跑循环数只能走 CLI;**引入版本未核实**(README 与 CHANGELOG 均未提及 `--loops`)。
- **状态**:⏸️ 引擎侧已可做,UI 侧仍待拍板。

### 3.2 任务历史保留时长 —— **仍未决**

- **现状**:`TASKS` 仍是内存 dict,server 重启全丢(只有 archive 落盘)。`server.py` 里有 `TASKS_FILE = DATA_DIR / "tasks.json"` 这一行,但**全库再无第二处引用** —— 存盘恢复没有实现。
- **选项**:(a) session 内永久保留(当前默认,无清理机制);(b) 保留最近 N 条;(c) 落 JSON 文件、server 启动恢复。
- **状态**:⏸️ 待用户拍板。

### 3.3 "重置"按钮的语义 —— **已解决**

- **现状**:`#btn-reset` 现在**绑定了** `onResetClick`(不再是空按钮):confirm → `POST /api/tasks/force-cleanup`(杀子进程 + 清任务列表)→ `POST /api/adb/reconnect` → 重置本地视图(关 WS、清 `currentTaskId` / `currentDeviceSerial` / `perfMeta` / `perfSamples`、清 console、`syncPerfChart()`)→ 重刷任务与设备。按钮期间禁用并显示"重置中…"。
- **当年否掉的选项**:清 localStorage / 只清任务——最终选的是"硬重置"语义。**引入版本未核实**(CHANGELOG 未单独记载)。
- **ⓘ 顺带发现一处文案不一致**:`onDeleteTask` 的 confirm 里写"日志文件会一起删除,无法恢复",但服务端的存档是永久保留的(`_forget_task` 不碰磁盘),任务卡上 `×` 的 tooltip 也写"从列表移除(存档保留在 archive/)"。**两处文案互相矛盾,建议统一到后者。**

### 3.4 日志 DOM 上限 1000 行 —— **仍未决**

- **现状**:`LOG_DOM_CAP = 1000` 仍是硬编码常量,未暴露为设置项;超出后在顶部插一行 `(truncated N lines; full log: …)`。
- **状态**:⏸️ 当前 1000 是默认值,不动;如需要再调。

### 3.5 WS 重连 max 5 次 —— **仍未决**

- **现状**:仍是 5 次(1+2+3+4+5 = 15s),之后打一行英文提示让用户刷新页面。
- **问题**:server 重启慢于 15s 时,自动恢复就废了。
- **状态**:⏸️ 当前 5 是合理值,不动;若用户反馈再调。

### 3.6 adb 设备列表刷新间隔 —— **仍未决**

- **现状**:`setInterval(refreshDevices, 3000)` 仍是 3s;面板另有手动"刷新"按钮。
- **状态**:⏸️ 当前 3s OK,不动。

### 3.7 删除任务是否要 confirm dialog? —— **已解决**

- **现状**:`onDeleteTask` 现在**有** confirm(`confirm(...)` 带设备 / 脚本 / 开始时间 / 警告文案)。任务列表里 `×` 只对终态任务(`finished` / `failed` / `interrupted`)渲染,运行中的任务根本没有删除按钮。
- **遗留**:见 3.3 的文案不一致一条。
- **状态**:✅ 已解决。

### 3.8 多脚本队列(device-uniqueness 是否过严) —— **仍未决**

- **现状**:`POST /api/run` 仍是 409 —— "设备 X 上有正在运行的任务(script),请先停止或等待完成",一台设备同时只能跑 1 个任务。
- **状态**:⏸️ 当前 strict 1 个,符合 MVP;若要做选"加任务队列"。

### 3.9 Long 按键阈值 —— **已解决**

- **现状**:格式已定并写进 README —— parser 正则收 `Short` 或 `LongXXXX`(`Long3000` = 按住 3000ms),**裸 `Long` 现在等同缺省 1500ms**(当年是"必须带数字")。README 有 `2-KEY_VCR-Long3000-500-1` 示例。
- **另注**:`KEYCODE_*` 一族的长按在目标设备(Android 14 / SDK 34)上静默失败,只有 `KEY_*` 一族真长按 —— 这条已写进 README,不是开放问题。
- **状态**:✅ 已解决。

### 3.10 设备名很长时的 truncate —— **已解决(未实机验证)**

- **现状**:`.device-name` 现在带 `overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 0 1 auto`;同样处理的还有 `device-row1 > .muted`、`.script-title`、`.script-sub`、`.device-row2 > .badge-mini`。**例外**:`.task-sub`(任务卡的脚本名/时间行)没有 ellipsis 兜底。
- **遗留**:没有在真机上用超长 serial 实测过(未核实)。
- **状态**:✅ 已解决。

### 3.11 🆕 死代码:`state.seqContext` / `.device-seq-pick` —— **仍未决(纯清理)**

- `state.seqContext` 在 state 里定义,但**全库再无引用** —— 序列弹窗真正的 context 是模块级 `_seqPickerContext`(另有 `_paramsCtx`)。
- CSS `.device-seq-pick` 在 `style.css` 里有 3 条规则,`app.js` / `index.html` 里**零引用**(设备卡的序列选择器早已搬到 `ir_runner` 脚本卡)。
- `openSeqModal(opts)` 还留着字符串兼容分支(旧式 `openSeqModal("default")` 进"文件模式"),但今天 UI 上只有"设备模式"一个入口 —— 文件模式的标题分支实际不可达。
- **状态**:⏸️ 无行为影响,建议随下次前端清理一并删除。

---

## §4. 已知 UX 边界 case(已被发现但未修)

| # | 场景 | 当前行为 | 备注 |
|---|---|---|---|
| 1 | WS 在 server restart 时 log console 卡最后一行 | 没手动 rejoin | 用户得手动点 task card 或重启浏览器 |
| 2 | 任务被删除后它的 log 文件不会被清 | **刻意不修** —— `_forget_task()` 不碰磁盘,`logs/` 里的文件留给用户手工清 | **没有任何**自动回收;孤儿回收清的是进程不是文件;见 [../TODO.md](../TODO.md) §9.2 |
| 3 | modal 在小屏幕(< 1024px)的响应式 | 没做 | 桌面端 OK,平板可能溢出 |
| 4 | dark mode 切换 | 只有一种暗色主题 | 用户没要求过亮色 |
| 5 | 设备卡拖拽排序 | 没做 | 设备列表按 adb 返回顺序 |
| 6 | 服务器时区 | 用 `datetime.now()` 本地时间 | 跨时区会乱;用户单机 OK |
| 7 | 长 serial 截断 | 详见 §3.10(`.device-name` 一带已加 ellipsis;`.task-sub` 除外) | 真机未实测 |
| 8 | ir_runner 长按超长(Long99999) | parser 不限 | 实际按键不会这么久 |
| 9 | 多浏览器标签页同时打开 | 共享 localStorage 但各开各的 WS | 没冲突,但 UI 状态可能不同步 |
| 10 | Ctrl+F5 后 localStorage 保留(应该) | 保留 | 已验证 |

---

## §5. 视觉规范摘要

### 5.1 颜色 token

- `--bg`: 深灰背景
- `--panel`: 卡片背景
- `--accent`: 蓝色(选中 / 高亮)
- `--ok`: 绿色(运行中)
- `--err`: 红色(离线 / 失败)
- `--warn`: 橙色(中断中)

### 5.2 卡片 4 状态

| 状态 | 边框 | 背景 | 角标 |
|---|---|---|---|
| default | 无 | panel | 无 |
| active | accent 1px | panel 加亮 | ✓ 已选中 |

> 「运行中」**不是设备卡上的角标** —— 它只在 `device-actions` 里出现一次(橙色 `--warn` + 斜体加粗的
> `.badge-running`),与蓝色的选中 pill 分成两个类别。早期那个 `⚡ 运行中` 的 `::after` 规则已删除(见 §2)。
| disabled | 无 | panel 60% 不透明 | 灰色 cursor not-allowed |
| error | err 1px | panel | 离线 |

### 5.3 字体

- 主字体: 系统 sans-serif(Win = Segoe UI / Mac = -apple-system)
- console: monospace(Win = Consolas)
- 标题: 14-16px
- 正文: 12-13px
- chip / 角标: 10-11px

### 5.4 间距 / 圆角

- panel gap: 8px
- card padding: 12-16px
- card radius: 8px
- chip radius: 4px

---

## §6. 文件清单

| 文档 | 它回答什么问题 |
|---|---|
| [README.md](../README.md) | 用户向:这平台是什么、怎么启动、每个脚本干什么 |
| [CLAUDE.md](../CLAUDE.md) | 会话启动指令:项目铁律、代码约束、文档纪律 |
| [docs/START-HERE.md](START-HERE.md) | 新会话从哪读起、按什么顺序读 |
| [docs/ARCHITECTURE.md](ARCHITECTURE.md) | `server.py` + 静态前端 + 脚本引擎是怎么拼起来的 |
| [docs/DECISIONS.md](DECISIONS.md) | 已批准、不可回退的设计决策清单 |
| [docs/PITFALLS.md](PITFALLS.md) | 踩过的坑:症状 → 根因 → 修法 |
| [docs/REPORT_FORMAT.md](REPORT_FORMAT.md) | 脚本产出与报告的契约格式 |
| [docs/PERF_MONITOR_V2.md](PERF_MONITOR_V2.md) | perf_monitor 采什么、字段什么意思、怎么判读 |

---

**维护规则**:新的交互决策进 §1(表里加一行);新的"否掉的方案"进 §2;**§1 与 §2 中任何条目都与源码冲突时,先改代码或先改这里,不要让两份说法并存**。新开放问题进 §3,并标注"仍未决 / 已解决"。§4 / §5 原样保留,§6 只维护"这个文档回答什么"。
