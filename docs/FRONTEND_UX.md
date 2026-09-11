# PPTP — 前端交互设计沉淀

> **本文档聚焦"前端怎么用、为什么这样设计"。**它跟 [HANDOFF_PROMPT.md](HANDOFF_PROMPT.md) 互补:HANDOFF 写的是"代码长啥样 + 改了啥",本文写的是"为什么这样做、踩过什么坑、还有什么没定"。
>
> 维护人:Claude 协助用户在 2026-07 系列会话中沉淀,后续若有新决策请追加到对应小节,不要覆盖。

> ⚠️ **行号提醒(2026-08-25,v2.4.2)**:本文 §1/§2 的 `static/app.js#Lxxx` 行号来自 2026-07-20,代码已大量演进,**行号全部漂移,不可再当作定位依据**。本文保留的是**设计理由**(为什么这样做)——需要当前准确行号/代码位置请以 [HANDOFF_PROMPT.md §5.5](HANDOFF_PROMPT.md)(前端交互模型,逐版本维护行号)为准。

---

## §1. 已敲定的交互决策(用户已确认)

每条决策格式:**做什么 / 为什么 / 涉及代码位置**。

### 1.1 Per-device sequence binding(每设备独立)

- **做什么**: `state.deviceSequences: { [serial]: filename }`,设备 A 选 default、设备 B 选 aging,互不影响。
- **为什么**: 真实压测场景下多台投影仪跑不同序列(老化 vs 工厂),不允许全局共享。
- **涉及**: [app.js:30-32](static/app.js#L30-L32) state 字段;[onSelectDevice L696-731](static/app.js#L696-L731)。

### 1.2 序列文件 > 内存编辑

- **做什么**: 用户改 ini 直接编辑文件;modal 只做 picker + 创建空模板,不做 step 编辑。
- **为什么**: 编辑器复杂度远高于实际收益(用户已会写 ini)。第一版做过 step 编辑器,被废弃。
- **涉及**: [openSeqModal L1086](static/app.js#L1086);`/api/sequences/{name}` PUT 端点直写文件。

### 1.3 Run 按钮在脚本卡,不在设备卡

- **做什么**: ir_runner 脚本卡右侧 "▶ 跑" 按钮;设备卡只显示状态。
- **为什么**: runnability 取决于「设备 + 脚本 + 序列」三态。把按钮放脚本卡可以让所有 gating 状态集中显示在脚本卡(active / disabled / 已选序列)。
- **涉及**: [renderScripts L122](static/app.js#L122);[onRunClick L664](static/app.js#L664)。

### 1.4 设备卡 dropdown gating

- **做什么**: 设备未选中 / 跑任务中 / 离线时,dropdown 禁用 + tooltip 提示"先点击选中"。
- **为什么**: 防止用户给一台"还在跑"的设备改脚本(脚本锁定是后端 409 的语义前端化)。
- **涉及**: [renderDevices L289-307](static/app.js#L289-L307)。

### 1.5 视觉徽章(active 角标,完全在卡内)

- **做什么**: 用 chip 角标表示状态,**全部在卡片内部不溢出**:
  - 设备卡 active: 蓝色边框 + 右上 "✓ 已选中" 蓝色 pill
  - 设备卡 active+running: 右上 "⚡ 运行中" 绿色 pill(替换 ✓)
  - 脚本卡 active: 置顶 + 边框 + 左上 "▸ 当前选中" 蓝色 pill
- **为什么**: 用户反复要求"角标不要溢出卡片";删除过 "跑 X" 重复 badge(避免冗余信息)。
- **涉及**: [renderDevices L286-340](static/app.js#L286-L340);[renderScripts L122-204](static/app.js#L122-L204)。

### 1.6 localStorage 持久化

- **做什么**: key `pptp.deviceState.v1`,**只保存** `selectedDeviceSerial` + `deviceSequences`(防抖 200ms)。
- **不保存**: `deviceScripts`(会话状态,2026-07-20 反转 — 详见 §2.0b)、devices / scripts / tasks(总从 server 拉)。
- **为什么**: 配置(哪个设备、跑哪个序列)跨刷新保留;会话状态(现在想跑什么)不持久化 — 否则重启/刷新后卡片显示上次的脚本选择,语义不对。
- **副作用权衡**: 同一个 localStorage 无法区分 Ctrl+F5(刷新)和 server restart,所以"重启干净"等价于"刷新也清"。**用户接受这个权衡**(2026-07-20):运行状态下拉框本来就置灰、改不动;非运行状态下拉框"无脑清空"也无害,反正随时能改。不加 "重置按钮" 区分这两类场景(避免过度设计,见 §3.3)。
- **涉及**: [STORAGE_KEY L1201](static/app.js#L1201);[loadPersistedState / savePersistedState](static/app.js#L1216-L1252)。

### 1.7 RAF 批量推送日志

- **做什么**: log buffer 累积,requestAnimationFrame 一次性 textContent 更新;DOM 上限 1000 行;服务端 replay 上限 10000 行。
- **为什么**: 防止"刷"感;replay 2000+ 行时单行 textContent += 是 O(n²),会明显卡顿。
- **涉及**: [appendLogLine L473](static/app.js#L473);[flushLogBuffer L480](static/app.js#L480);[LOG_DOM_CAP L471](static/app.js#L471)。

### 1.8 Modal 标题动态化

- **做什么**: 同一 modal 根据 context 切标题:
  - 文件模式: `${seqFilename}.ini`(从顶部序列卡点)
  - 设备模式: `设备 ${serial} 的序列`(从设备卡点)
- **为什么**: 一个 modal 复用两种入口,避免重复 UI。
- **涉及**: [openSeqModal L1086](static/app.js#L1086);`state.seqContext` 字段。

### 1.9 缓存破坏 + NoCache

- **做什么**: index.html script/css link 带 `?v=2.0.1`;FastAPI `NoCacheMiddleware` 给静态文件 `Cache-Control: no-store`。
- **为什么**: 平台经常小改前端,F5 不够硬;开发流程接受 "Ctrl+F5" 作为正常操作。
- **涉及**: server.py `NoCacheMiddleware`;[index.html script src](static/index.html) query string。

### 1.10 WS 重连指数退避

- **做什么**: WS onclose 后 1s/2s/3s/4s/5s 重试,5 次后放弃并提示用户刷新页面。
- **为什么**: server 重启 / 临时网络抖动,自动恢复比弹错误更友好;但无限重试会浪费资源。
- **涉及**: [scheduleReconnect L634](static/app.js#L634);`state._wsReconnectAttempts` 计数器。

### 1.11 WS taskId 过滤(切设备时不污染 console)

- **做什么**: WS onmessage 收到帧时,如果 `state.currentTaskId !== ws._taskId` 直接丢弃。
- **为什么**: 用户切到无任务的设备时,旧设备的 WS 还在连;不滤的话旧日志会"复活"覆盖空 console。
- **涉及**: [openWs onmessage L600-624](static/app.js#L600-L624) filter 行。

### 1.12 onSelectDevice 无任务时关 WS

- **做什么**: 切换到的设备没有任何任务时,显式关闭旧 WS + 清 currentTaskId。
- **为什么**: 双保险 — 即使 onmessage filter 漏掉,关掉连接就根本不会推帧;同时也释放 server 端 fd。
- **涉及**: [onSelectDevice L717-728](static/app.js#L717-L728)。

---

## §2. 我提的改进建议 + 已采纳(从对话累积)

按时间排序,标记 ✅ 已采纳 / ⏸️ 待定 / 🔄 决策反转。

### 2.0 🔄 设备卡 "运行中" pill 去重 + 配色区分(2026-07-20)

- **背景**: 设备卡右下"运行中"和右上 ⚡ 重复;用户提出删右上 + 左下换色。
- **改动**:
  - [style.css L253-257 删掉](static/style.css#L253-L257)`.device-card.active.device-card-temp-offline::after` 的"⚡ 运行中" pseudo-element
  - [style.css L383](static/style.css#L383) `.badge-running` 配色改为橙色 `--warn` + italic + 加粗,与蓝色"✓ 已选中"做视觉区分
- **理由**: 左下 badge 作为 run 状态的唯一指示器;橙色 + 斜体让"在跑"和"已选中"一眼能区分。
- **决策反转**: 之前的"active + temp-offline 右上 pill"在视觉上跟已选中冲突。

### 2.0b 🔄 localStorage deviceScripts 决策反转(2026-07-20)

- **背景**: 用户反馈"关机后再开,设备卡脚本选择不是默认状态,而是上次选择的"。
- **原决策** (§1.6): 刷新保留 `deviceScripts`、跨刷新不丢配置。
- **新决策** (§1.6 修订): **`deviceScripts` 不再持久化**;`selectedDeviceSerial` + `deviceSequences` 仍保留。
- **改动**: [app.js loadPersistedState](static/app.js#L1216-L1235) 跳过 deviceScripts;[savePersistedState](static/app.js#L1237-L1252) 也不写。
- **语义**:
  - deviceScripts = "会话状态"(现在想跑什么)→ 不持久化
  - deviceSequences = "配置"(哪个设备跑哪个序列)→ 持久化
  - selectedDeviceSerial = "用户偏好"(看着哪台)→ 持久化
- **副作用**: Ctrl+F5 也会清空脚本选择(同一个 localStorage,无法区分刷新 vs 重启)。**用户接受这个副作用,不加"重置"按钮解**(2026-07-20 同日用户反馈:"运行状态置灰了,非运行状态改脚本也无所谓")。原因:非运行状态下拉框无副作用,不需要精细的"重置 vs 不重置"语义。

### 2.1 ✅ 临时离线设备的合成卡片

- **背景**: wifi_reboot 压测中设备会临时掉线,但任务还在跑;用户看不到设备卡。
- **建议**: 后端用 adb devices 解析 + 任务列表合成的"临时离线"卡片,标注"(临时离线 · 任务在跑)"。
- **采纳**: [renderDevices L249-269](static/app.js#L249-L269)。
- **副作用**: dropdown 仍可用(交互锁在 running),`btn-hard-stop` 只在这种状态下启用。

### 2.2 ✅ "硬停"按钮(temp-offline 专属)

- **背景**: 设备掉线时子进程可能卡死,`stop/{task_id}` 发 CTRL_BREAK 没用。
- **建议**: 加 "硬停" 按钮,只在当前任务在 temp-offline 状态时启用,调 `/api/tasks/force-stop-by-device/{serial}`(SIGKILL)。
- **采纳**: [renderLogInfo L457-459](static/app.js#L457-L459);[index.html btn-hard-stop](static/index.html)。

### 2.3 ✅ 日志 replay 截断提示

- **背景**: 服务端 10000 行上限截断后,前端只显示最近 N 行,用户不知道。
- **建议**: 收到 `replay_meta` 消息时,在 console 顶部插一行 "── 共 X 行 · 本次显示最近 Y 行 · 完整日志见 logs/... ──"。
- **采纳**: [openWs replay_meta handler L614-622](static/app.js#L614-L622)。

### 2.4 ✅ 删除任务 = 删除日志文件

- **背景**: 任务结束后的 log 文件残留在 `logs/` 目录,长期累积。
- **建议**: `DELETE /api/tasks/{task_id}` 后端同时删 log 文件。
- **采纳**: server.py `api_delete_task()` 已加。

### 2.5 ✅ "未选脚本" muted 提示

- **背景**: 设备没选脚本时,设备卡 actions 区域空着,用户疑惑。
- **建议**: 显示 `<span class="muted">未选脚本</span>` 灰色 11px 提示。
- **采纳**: [renderDevices L317-318](static/app.js#L317-L318)。

### 2.6 ✅ 任务卡片 active 高亮

- **背景**: 当前看的任务在 task panel 里没视觉区分。
- **建议**: `state.currentTaskId === t.task_id` 时加 `.active` class。
- **采纳**: [renderTasks L387](static/app.js#L387)。

### 2.7 ✅ 设备卡 chip 显示已选脚本

- **背景**: 用户选了脚本后,设备卡 actions 区显示 "未选脚本" 或 "运行中",但选的是什么脚本不直观。
- **建议**: row2 显示 `device-script-chip`(已选脚本名,无 .py 后缀)。
- **采纳**: [renderDevices L337](static/app.js#L337)。

### 2.8 ✅ 切设备无任务时 console 显示 "暂无任务"

- **背景**: 切到无任务设备后,console 残留上一设备的最后几行(被 RAF flush 延后)。
- **建议**: clearConsole 之后 setConsoleTarget(null, serial) → renderLogInfo 显示 "查看设备 X 的日志(该设备暂无任务)"。
- **采纳**: [renderLogInfo L431-435](static/app.js#L431-L435)。

### 2.9 ✅ 文档统一:default.ini 5/6 字段 bug 修复

- **背景**: ir_runner.py parser 是 5 字段,但 default.ini 旧版是 6 字段(带 name),跑起来报 "Key not found"。
- **建议**: 删 default.ini name 字段 + 同步 README 备注 + 改 default.ini 头部注释。
- **采纳**: 2026-07-20 完成,default.ini 现在纯 5 字段。

### 2.10 ✅ 文档统一:HANDOFF 为真源 + SIMPLE-* 归档

- **背景**: README / SIMPLE-PRD / SIMPLE-PLAN / SIMPLE-ARCHITECTURE 互相打架。
- **建议**: HANDOFF 加 "本文件是真源" 注 + SIMPLE-* 加归档 banner + README 全量更新(脚本名、API 表、目录树、IR 序列章节、localStorage 章节)。
- **采纳**: 2026-07-18 完成。

### 2.11 ✅ IR 脚本融合 + Long 按键 bug 修复(2026-07-20)

- **背景**: 旧平台 IR 相关代码拆在 3 个文件里:[scripts/ir_runner.py](scripts/ir_runner.py) + [tools/ir/ir_remote.py](tools/ir/ir_remote.py) + [tools/ir/keyevent.txt](tools/ir/keyevent.txt)。更严重的是,Long 按键的实现是"循环发短按"(在 duration_ms 内反复 down+up),跟"按住 duration_ms 再松开"完全不同 — 设备收到的是一堆短按,不是长按。
- **建议**:
  1. 把 IRRemote + SequenceConfig + runner 全合到 [scripts/ir_runner.py](scripts/ir_runner.py)(一个 py)
  2. 删除 [tools/ir/](tools/ir/) 整个文件夹(去掉外部数据文件依赖)
  3. IR event 路径 hardcode 默认 `/dev/input/event1`(从旧 keyevent.txt 推断),支持 3 种覆盖(CLI `--device-event-path` / env `IR_EVENT_PATH` / 改顶部常量)
  4. 重写 long_press:`send_key_down` → sleep(duration_ms) → `send_key_up`,真正的"按住"
  5. 保留 [scripts/wifi_reboot_stress.py](scripts/wifi_reboot_stress.py)(与 IR 无关,合并不合理)
- **采纳**: 2026-07-20 完成。最终目录:`scripts/` 2 个 py(IR + WiFi)、`ir_sequences/` 1 个 ini + 1 个 md,`tools/` 整个删。

### 2.12 ✅ PPTP 平台 4 个 UI bug 修复(2026-07-20)

- **Bug1**: 运行中脚本卡片未置灰 — [renderScripts](static/app.js#L122) 加 `isLocked` 判定 + 新增 `.script-card-running` CSS 类(opacity 0.55 + not-allowed + dashed border,保留 "▸ 当前选中" ribbon 让用户知道是 active)
- **Bug2**: 切任务卡不联动脚本面板 — [viewTaskLogs](static/app.js#L571) 同步设 `state.selectedDeviceSerial` + 调 `renderDevices/renderScripts`
- **Bug3**: 离线设备任务切后脚本高亮残留 — [renderScripts](static/app.js#L151) 加 `selectedDeviceReachable` 判定(设备断开且非运行中 → 不高亮)
- **额外**: 设备卡左下【undefined】— `btnHtml` 默认 `""` 而非 undefined
- **采纳**: 2026-07-20 完成。

### 2.13 ✅ 脚本参数前端可配置(自描述 schema)(2026-08-24,v2.2.0)

- **背景**: 每个压测脚本参数不同,前端硬编码表单没法扩展。
- **决策**: 脚本用模块级 `PARAMS` 列表自描述(字段 `name/label/type/default/min/max`)+ `--dump-params` 打印 schema;**后端 / 前端 / 平台代码一行不用改**,新增脚本只需定义 `PARAMS`(与 ir_runner 序列选择"自描述"一脉相承)。
- **参数按 设备×脚本 独立持久化**:`state.deviceParams: { [serial]: { [script]: {...} } }`,存入 localStorage(`pptp.deviceState.v1`)。A 设备的配置不干扰 B 设备、不同脚本互不串(用户明确要求"不能存在记忆或干扰")。
- **入口交互**:点击带参数配置的脚本卡 → 弹窗按 schema 渲染表单(`input`/`select`);保存后跑任务时合并进 `--params` 透传。

### 2.14 ✅ 参数类型新增 select 下拉框(2026-08-24,v2.3.0)

- **背景**: 传感器脚本的 `sensor`(gsensor/tof)、APP 启动脚本的 `mode`(cold/hot)这类枚举值,文本输入容易拼错。
- **决策**: `PARAMS` 的 `type` 新增 `"select"` + `choices` 列表(字符串或 `{value,label}`),前端渲染 `<select>`;`saveParams` 用 `#modal-params [name=...]` 选择器同时匹配 input/select。

### 2.15 ✅ 性能图表实时曲线(2026-08-25,v2.4.0)

- **背景**: 手工测试/播放时想实时看设备 CPU/GPU/内存% + 前台 APP CPU%。
- **决策**:
  - **图表挂日志面板,全局绑定 `state.currentTaskId`**:`#perf-chart`(200px)插在 `#log-info` 与 `.panel-body` 之间 —— 与 console 同一个"当前任务"模型,不是每卡渲染
  - **不残留三件套**(与 console 同构):WS 帧按 `currentTaskId !== ws._taskId` 过滤 + 切任务 dispose/重建图表 + 每任务独立样本缓冲 `perfSamples[taskId]`(WS 重放重建,刷新后恢复)
  - **图表逻辑只在离散导航点调用**(task view / ws.onopen / delete / cleanup / reset),**绝不在 2s 轮询里** —— 否则切走后图表被持续重建/闪烁
  - **ECharts 5.5.1 本地 vendor**:零外部依赖、pin 版本、约 1MB;压缩版 grep 校验会误判,需 `echarts.version` + `typeof echarts.init` 验证
  - **一个导出按钮 = 三件套**(无 ZIP,用户指定):log.txt + perf.csv + chart.png;Chrome/Edge 单次点击多下载,Firefox 可能拦截后两个

### 2.16 ✅ 性能图表横轴与导出(2026-08-25,v2.4.1 / v2.4.2)

- **墙钟时间横轴 + 自动压缩**(v2.4.1 起):x 轴 `type:"time"`,标签 HH:MM:SS;数据点用脚本 `clock`(epoch ms),旧日志回退 `started_at + t*1000`
- **双 Y 轴**(v2.4.1):CPU/GPU/MEM 左轴 0-100%,前台APP 右轴 0-400% —— `top` 多核 %CPU 可超 100(实测 116-123%),单轴顶格裁切;原需求允许"Y 轴可以不同"
- **长时检测友好**(v2.4.2):刻度数稳定 ~10 个(`splitNumber:6` + `hideOverlap`),跨度越长单格越大;采样缓冲上限 86400(24h@2s);导出 chart.png 固定 1280×360(2x=2560×720),跑多久图片都不"长"。⚠️ **v2.5.1 起导出改为全程图**(离屏渲染,0.5h 刻度),此条"固定尺寸不拉长"已不再适用,见 §2.20
- **wire 契约**(v2.4.1 教训):`PERF|` sample 行**必须带 `type:"sample"` + `clock`**,缺了前端 `handlePerfLine` 会把样本当未知类型丢弃 → 图表不出线 + 导出只剩 txt(三条症状同源的根因,见 HANDOFF 踩坑 #17)
- **GPU 线视频场景读 0 是正常**(v2.4.2 实机排查):本机 MStar 显示管线(解码走 VPU、合成走 HWC 硬件叠加),GPU 只画 UI;全屏视频/静态画面下 GPU 真实闲置,GPU 绿线平 0 属预期,不是图表 bug —— 判断参考:应用切换/UI 动画压测时该线应明显波动(11:56 同机实测平均 42%),见 HANDOFF 踩坑 #14

### 2.17 ✅ 电池充放电曲线:perf 管道按脚本类型泛化(2026-08-25,v2.5.0)

- **背景**:电池充放电脚本(battery_inout_stress)也需要实时曲线,但指标不同(电量/温度/电压 vs CPU/GPU/内存)。不想复制一套图表管道。
- **决策**:**复用同一套 perf 图表管道,在构建点按"脚本类型"分支** —— 新增 `perfKind(taskId)` → `"perf" | "battery" | null`(按脚本名匹配),`isPerfMonitorTask` 泛化为 `isPerfTask`。只有 4 个点按 kind 分支:
  - **`handlePerfLine`**:battery 可读行 v2.5.0 先做中文 `[batt] HH:MM:SS (10.0s) 电量=57% 温度=40.8°C 电压=11.6V 状态=充电中` + `⚠ 跳变[...]`;**v2.5.1 按 ASCII 规则改英文**(§2.18);meta 行 v2.5.0 中文 `模式=充电/放电 · 串口=COM9` → v2.5.1 英文 `monitor start | mode=charge | port=COM9`(分隔符用 `|` 而非 `·`,保证纯 ASCII)
  - **`buildPerfOption`**:battery = 电量(蓝,左轴 0-100%)+ 温度(红,右轴 auto °C),tooltip 按系列名取 `unitOf` 显示 `%`/`°C`;perf = 原四系列
  - **`buildPerfCsv`**:battery 表头 `t_sec,t_wall,level_percent,temp_c,voltage_mv,status,jump_delta,jump_type`
  - **`onExportLog`**:battery 的 csv 后缀用 `.batt.csv`
- **为什么不并排双图**:全局单日志面板 + 单 `currentTaskId` 模型(§2.15),任何时刻只显示一个任务的图;kind 分支比维护两套图表实例便宜得多,且天然复用切任务不残留 / 重放重建 / 三件套导出
- **单图前提**:`perfSeriesKeys` / `perfXBase` 等单例按任务重建,同屏不会有两个不同 kind 的图
- **语言分工**:脚本 100% 英文/ASCII(项目规则:代码无中文);前端新增文案用中文。battery 脚本的 PARAMS label 因此是英文(参数弹窗),这是规则的直接结果

### 2.18 ✅ 日志控制台全英文 + 折线图固定 1h 滚动窗口(2026-08-25,v2.5.1)

- **背景(用户 3 点要求)**:① 前端 log 打印不能存在中文;② 折线图显示范围从 2min 刻度拉到 10min、约 6 个刻度 = 实时显示 1h 曲线;③ perf_monitor 同步。
- **log console ASCII(需求 ①)**:**凡打进 `#log-console` 的内容全英文/ASCII** —— 删掉 `STATUS_ZH`/`JUMP_TYPE_ZH` 中文映射表,`readableBattLine` 重写为 `[batt] HH:MM:SS (X.Xs) level=87% temp=36.2C voltage=8.4V status=charging`(跳变 `[!] jump[type] prev->87%`);meta 行、WS 重连/重放/截断提示、占位符全英文。**时间戳统一 `fmtClock`**(`toLocaleTimeString("zh-CN",{hour12:false})`)强制 24h `HH:MM:SS` —— 这是关键细节:无 `hour12:false` 时 zh-CN 浏览器会吐"下午3:30"这类中文进日志
- **UI 与 console 边界**:卡片 / 弹窗 / 图表系列名 / 导出表头仍中文;只有日志面板的 console 输出是英文。判据:进不进 `#log-console`
- **固定 1h 滚动窗口(需求 ②③)**:**为什么是滚动窗口而不是"显示全部"** —— 用户说"实时显示 1h 的曲线",即 x 轴恒定覆盖最近 1 小时、右侧跟随最新样本,像心电图一样滑动;而 perf_monitor 与 battery 要完全一致(③"同步修改"),所以做成 shared 的 `buildPerfOption`/`flushPerfChart` 而非 per-kind
- **实现要点**:`PERF_WINDOW_MS=60*60*1000`;xAxis `interval:10*60*1000` + `splitNumber:6` 钉死 10min 步长(只用 splitNumber 会让 ECharts 自选 5/15/30 刻度);`min: perfWindowEnd(taskId)-1h` / `max: perfWindowEnd(taskId)`(无样本回退 `perfXBase`);`flushPerfChart` 每次 flush 后把 min/max 滚到最新样本、`while (d[0][0] < minX) d.shift()` 剔窗外观测点;**移除 dataZoom** —— 它会让用户拖出 1h 视野,和 flush 逐帧拉回最新互相打架(见 HANDOFF 踩坑 #21)
- **取舍**:完整历史仍在 `perfSamples[taskId]`(86400 上限),CSV 导出不受 1h 窗口影响 —— 窗口只管"看",不管"存"

### 2.19 ✅ 放电测试卡片持久化:放电关机专用状态(2026-08-25,v2.5.1)

- **背景**:放电模式以"设备关机"自动停(`stop_reason:"power_off"`,脚本返回 0 → 任务 `finished`)。此时设备不在 `adb devices`,旧逻辑卡片直接消失 —— 看起来像"设备丢了"。用户要求:测完不能算消失,做一个专门的状态,**像重启测试的【临时离线】卡一样**。
- **决策**:**在 `renderDevices()` 合成一张 `_batt_off` 卡**(仿 `_temp_offline`),条件:① 存在 battery_inout_stress 任务且终态(`finished`/`interrupted`/`failed`);② 设备不在当前 `adb devices`;③ **该 battery 任务仍是设备最新任务**(`latestTaskOf()` 校验,防被后续任务顶掉)。
- **视觉**:虚线**红**边(`var(--err)`,区别于临时离线的琥珀 `var(--warn)`)+ 状态文案"设备已关机" + badge"放电关机"(红);model 文案按 `latest.params.mode` 区分"放电测试 · 设备已关机" / "电池测试 · 设备已关机"。
- **交互**:同临时离线卡,**不算离线** —— `isOffline` 判定排除 `_batt_off`,卡可点、可换脚本、可跑新任务;空态检查也排除 `battOff`(设备全关机只剩放电关机卡时仍显示卡,不显示"未检测到 ADB 设备")。
- **退出路径**:设备重新出现在 `adb devices` → 自动回正常在线卡;删除对应任务 → 卡移除。
- **为什么不用后端返回**:合成卡与临时离线卡同源,都是前端 `renderDevices()` 基于"实时任务 + 实时 adb 列表"推导的视图态,后端无状态更简单。

### 2.20 ✅ 导出图片改为全程图 + 横轴 0.5h 刻度(2026-08-25,v2.5.1)

- **背景(用户两点要求,逐字)**:① "导出的图片需要是全程的,不能只是 1h"(此前 PNG 是实时 1h 滚动窗口那部分);② "导出的图片的时间轴坐标需要做成 0.5h 为一个刻度"。
- **为什么不再用"当前图表直接 getDataURL"**:旧实现 `perfChart.getDataURL({pixelRatio:2})` 只能导出**当前绑定任务、当前 1h 窗口**;切走后 `perfChart = null` 直接跳过 png。用户要的是全程,而屏幕固定 1h 窗口(见 2.17)天然满足不了。
- **决策:离屏渲染一张"临时全程图"** —— `exportFullChartDataUrl(tid)`:`buildPerfOption(tid, true)` 的 **fullRange 分支**直接把 x 轴 min/max 取全量 `perfSamples[tid]`(跨度为任务全程),`xInterval=30*60*1000`(0.5h 刻度,②);建离屏 1280×360 div → `echarts.init` → `setOption` → `getDataURL({pixelRatio:2, backgroundColor:"#0a0d12"})` → dispose + 移除 div。**全程 PNG 与屏幕 live 图完全解耦**:live 仍 1h/10min 滚动,导出走全量;切走后照样能导出任意历史任务的全程图。
- **为什么离屏图而不是改 live 图 / 用 csv 出图**:① 用户原话"要是很难做的话建议直接通过 csv 生成图表也可以" —— 采纳"csv 也能出图"的思路,但直接在浏览器渲染免去手动步骤;② 全程图会让屏幕 live 图失去 1h 窗口的意义,且长跑时屏幕挤不下;③ 离屏 div(固定定位 -9999px)对用户不可见,无闪烁、不依赖 `perfChartTaskId === tid`。
- **`perfXBase` 重锚**:全程图里 `t`-fallback 样本(`clock` 缺失的旧日志)依赖 `perfXBase = started_at`;导出函数先把 `perfXBase` 锚到该任务 `started_at` 再还原,保证时间轴对得上。
- **教训**:导出与"当前视图"耦合是脆弱的 —— 导出应当只依赖**数据**(`perfSamples[tid]`),不依赖**视图状态**(谁在绑定)。

### 2.21 ✅ 日志通道化:本轮只展示 stdout,但按通道就位(2026-09-11,v2.6.0)

- **背景(用户原话)**:① "接下来的任务,我需要每一次的脚本执行都能实时监控 logcat 和串口日志,后续需要增加自动开启日志和自动保存的功能";② "stdout | logcat | serial 的 log 都需要将命名做的更好一点,包含设备信息,时间信息,测试项信息也得包含"。
- **用户拍板的三条(不可违背)**:① 日志展示方式 = **"前端暂时只展示 stdout,但后续可能会做选项卡切换,需预留"**;② 串口采集策略 = **"仅在任务显式勾选时采"**;③ 范围 = **"三件一起,分阶段实现"**。
- **为什么"暂不展示"反而是最省的做法**:用户明确要 tab 是**后续**的事。若现在就把 console 做成多缓冲(`logBuf` 按源拆分 + 每源独立时间戳 + 每源独立 `LOG_DOM_CAP`),就是给一个还没被批准的设计付实现成本。**最省的正确答案是:让 `appendLogLine(line, source)` 对非 `activeSource` 直接 return,`flushLogBuffer` 的单缓冲假设原封不动**(它永远只见 stdout)。开 tab = 只改 UI,数据层/WS/渲染路由已经就位。
- **预留接缝(四处,都不是 UI)**:① `state.activeSource`(console 渲染哪个源);② `openWs(taskId, sources)` 的 `?sources=` 订阅参数;③ `CHANNELS` 元数据 + `TABS_ENABLED = false`;④ `#channel-bar` 空节点。**第五处**是 `ws_logs` 里那个 `while True: await ws.receive_text()` —— 它现在忽略客户端消息,正是将来发 `{"type":"subscribe"}` 的地方(免去重连抖动)。
- **`#channel-bar` 为什么必须是 `#log-info` 的兄弟而不是子节点**:`renderLogInfo()` 每 2s 把 `#log-info` 整体 `innerHTML` 重写一次(tab 会被一秒销毁两次)。同理**通道计数必须每轮从 `state.captureCounts` 重放**,不能增量 patch。这是 v2.6.0 最容易踩的坑,已写进 HANDOFF §5.5.5 #22。
- **计数为什么不放 `/api/tasks`**:它每 2s 变,一旦并入 `tasksRenderKey` 就会让每轮轮询都判定"变了"→ 全量重渲染卡片。改为服务端独立的 `capture_counts` 心跳帧 + 前端**就地 patch** `#log-info .cap-counts`(比 4s 的双重轮询还快一拍)。
- **串口控件为什么放设备卡而不是日志栏**:它是**每次运行**的意图(勾选不持久化),而端口是**设备**的属性(持久化)。放设备卡能让"这台机器下次跑任务要不要抓串口"在一处看清;放日志栏则要等任务起来才发现没勾。
- **"脚本优先"仲裁必须在 UI 可见**:`battery_inout_stress` 自带 `serial_port` 参数,平台让位。若只是静默不采,用户会以为是 bug。故前端把它渲染成 **disabled + 中文提示**("该脚本自身占用串口,平台串口抓取已自动让位"),`title` 里写明后果(抢口会导致电量曲线变平)。
- **导出文件名的取舍**:用户要"含设备/时间/测试项"。文件名选 `pptp_<脚本>_<设备>_<时间>.<源>.log`,**服务端把确切文件名下发**(`t.log_files`),前端**从不重建路径** —— 命名规则只在 server.py 一处定义,前端重构不会漂移。**老任务**没有 `log_files` 时回退到约定名。
- **教训**:当用户说"先不做 UI,但要预留"时,正确的交付物是**协议 + 状态 + 路由**,而不是"半个 UI"。判断标准:**打开 tab 是否需要改 server.py 或 WS 协议?** 不需要,就说明预留到位了。

### 2.22 ✅ 任务结束自动存档 + 删掉 logcat/串口的显示方案(2026-09-11,v2.7.0)

- **背景(用户原话)**: 「我没找到 log 存放在哪里,这些 log 需要做成结束后自动存档,包括 perf monitor 的图表也算……所以导出的按钮可以暂时先隐藏掉了」。
- **真正的问题不是"没存档",是"找不到"**: 日志一直在 `logs/` 下,但文件名 32 位 `task_id` 打头(`d4fa6d50c2fc..._perf_monitor_B0403374....log`),人眼认不出;而且**界面上没有任何入口**能走到那个目录。**这是设计取舍的账没算全** —— 当初把 task_id 顶在最前是为了"删除 glob 和路径防护不用改",只算了机器可读,没算人可读。
- **交付**: 每任务一个 `archive/<时间>_<脚本>_<设备>/`,含三份日志 + `report.json` + `chart.png` + `summary.json`;任务卡加 `存档` 按钮(资源管理器打开);任务面板显示总占用。**`×` 与「清空已完成」只移除列表条目,不动存档** —— 既然承诺永久保留,一次误点就不该摧毁它。
- **为什么"删掉显示方案"和"加存档"是同一件事**: 用户在同一个决定里说了两句话 —— 日志找不到 → 要自动存档;logcat/串口太多 → 前端不显示。二者合起来才是完整的答案:**前端只负责"看正在跑的这一个",长期留存交给文件系统**。把 logcat 也塞进 console 既解决不了"找不到",又让 UI 更复杂。
- **图表的诚实降级**: 服务端**故意不引入绘图库**(ECharts 只活在页面里,为一张图养两套渲染不划算),所以 `chart.png` 由浏览器在任务结束时渲染后回传。**代价是任务结束时浏览器没开就没有图** —— 用户明确接受;`summary.json` 如实记录缺了哪些产物。事后打开页面看该任务会自动补传(WS 重放会把 PERF 行重新喂进 `perfSamples`)。
- **保留了唯一的例外**: console 里那一行 `[archive]` 摘要。删掉 pills 之后,串口采集失败会变成**完全没有信号**,而静默失效正是最容易被当成 bug 报的东西。这一行用既有通道、零新 UI、不轮询,行数本身就是"串口采到没有"的答案。
### 2.24 ✅ 掉线不应清掉卡片,离线是正常状态(v2.7.2)

- **背景(用户原话)**:「perf_monitor 这种,当存在设备重启或者 adb 掉线时是正常的」「不能因为掉线或重启就把任务卡片设备卡片清掉」「设备重启完成后需要能自动连接上之前的日志,且能自动连接上 adb 和串口」。
- **一处真 bug**:"临时离线"合成卡的判定写死 `t.status === "running"`,**漏了 `interrupting`**。场景:重启脚本正卡在等待里、设备还没回来,用户按了「中断」→ 任务变 `interrupting` → **设备卡凭空消失**。加上谓词即可。
- **任务卡与绑定关系本来就没事**:`renderTasks()` 无条件渲染整个 `state.tasks`;`refreshDevices()` 的 `keptSerials` **已经**包含 `interrupting`。**改之前先确认到底哪个才是洞** —— 探查发现 5 条需求里 2 条已满足,避免了重复造。
- **用户明确不要新提醒**:原话「不需要,直接拿之前的【临时离线就行了】」。「临时离线」合成卡(虚线琥珀边 + 「临时离线 · 任务在跑」状态)就是唯一信号,**不要**再加横幅/toast/日志行。
- **教训**: **"离线"在压测场景里是正常状态,不是错误状态。** 界面语言要跟上 —— 卡片用"临时离线"而不是红色的"离线"、不用弹窗、不打断操作。把计划内的设备重启画成故障,会让用户对真故障脱敏。

### 2.23 ✅ 存档收纳四处:按模块分类 + 入口提到全局(v2.7.1,用户试用反馈)

用户用了一天之后提了四条,都是"位置放错了"而不是"功能不对":

1. **「打开存档」从任务面板标题栏移到顶栏** —— 用户原话:「打开存档应该是全局功能,所以应该放在优先级更高的位置,而不是和任务卡片栏的那些放在一起」。**判据:这个按钮管的是平台级的东西还是这一栏的东西?** 存档是每一次运行最终都汇进去的地方,属于前者。与 重置 / 关机 并列,占用徽标也一起搬过去(hover 出分模块占用)。
2. **「清空已完成」→「清空已完成的卡片」** —— 用户原话说明了理由:「因为现在的存档都是永久保留了」。**文案没跟上语义变更**才是问题:按钮行为没变,但"清空已完成"在"存档永久保留"之后会让人以为要删东西。
3. **`archive/` 按模块分类** —— 用户原话:「不同模块的不能放在一起,像现在的 reports 文件夹下面一样做分类是最好的」。改成 `archive/<模块>/<时间>_<脚本>_<设备>/`,模块名与 `reports/stress-test/<模块>/` 对齐。
4. **老 `logs/` / `reports/` 清空** —— 见 CHANGELOG。

**教训**: **功能的"位置"和功能的"名字"都是设计的一部分,而且是最容易被实现者忽略的那部分。** 我把「打开存档」放在任务面板,是因为我实现它的时候正在改任务卡;用户一眼就看出那是**实现顺序**留下的痕迹,不是信息架构。同样,「清空已完成」这个名字在语义变更后就成了误导 —— **改了行为一定要回头改文案**。

- **教训一**: **"东西在哪"要算进设计成本。** 一个技术上完全正确、但用户找不到的目录布局,等于没有。给机器看的标识符(task_id)和给人看的标识符(时间/脚本/设备)可以并存 —— 放在不同的地方就行(目录名给人,id 进 `summary.json`)。
- **教训二**: **删功能也是交付。** 用户说"稳定简单为主导"时,正确响应是把 v2.6.0 预埋的接缝**拆掉**,而不是留着"反正以后可能用得上"。留着的接缝是有维护成本的状态,而且会误导下一个会话以为要接着做。

---

## §3. 开放问题 / 待用户拍板(下一会话可以问)

按重要性排序。

### 3.0 ⛔ 日志通道 tab 切换(已关闭,不做)

- **原计划**: v2.6.0 把数据层 / WS 协议 / 渲染路由都按通道预留好,只差 tab UI(`TABS_ENABLED = false`)。
- **关闭理由(2026-09-11,用户原话)**: 「既然 logcat 和串口日志太多的话,建议不考虑做他们两个的前端显示了。前端只保留 stdout,直接将 logcat 和串口日志的前端显示方案删掉。建议还是稳定简单为主导。」
- **v2.7.0 已执行**: `CHANNELS` / `TABS_ENABLED` / `state.activeSource` / `state.captureCounts` / 计数 pills / `#channel-bar` 节点与 CSS / `onmessage` 的两个 capture 分支 / 服务端 2s 计数心跳 —— **全部删除**。logcat 与串口继续在服务端采集并归档,只是前端永不展示。
- **留下的信息面**: 任务结束时 console 里**一行**归档摘要(`[archive] <目录>/ | stdout 22L | logcat 8L | serial 0L | report.json`)。这不是"半个 tab",是删掉 pills 之后**串口采集失败唯一的信号** —— 静默失效最容易被当成 bug 报。
- **状态**: ⛔ 已关闭。**如果将来又要做通道展示,先回来重读这一条** —— 用户明确要的是简单,不是"先埋好再做"。

### 3.1 ir_runner 无限循环 vs N 圈配置

- **现状**: 默认无限循环,只能外部 stop。
- **问题**: 用户压测一天后想跑"正好 100 圈对比",怎么办?
- **建议**: ir_runner 加 `--loops N` 参数,backend 接受后透传;UI 上跑按钮加个"跑 N 次"变体。
- **状态**: ⏸️ 待用户拍板。

### 3.2 任务历史保留时长

- **现状**: 内存 dict,server 重启全丢(只有 log 文件残留)。
- **问题**: 跑完的任务历史应该保留多久?
- **选项**: (a) session 内永久保留;(b) 保留最近 N 条;(c) 落 JSON 文件 server 启动恢复。
- **状态**: ⏸️ 待用户拍板。**当前默认 a**,无清理机制。

### 3.3 ⛔ "重置"按钮的语义(已关闭,不做)

- **现状**: topbar 有 "重置" 按钮,但**没绑事件**。
- **原候选**:
  - 清空 localStorage(已选设备 / 已选脚本 / 已选序列)
  - 后端清空所有任务 + log 文件
  - 二者都做
- **关闭理由(2026-07-20)**: 用户接受"刷新也清空 deviceScripts"的副作用(因为非运行状态下拉框改脚本无害,运行中又被置灰),不需要做精细的"刷新保留 vs 重启清空"区分。加重置按钮反而引入新的认知成本(什么时候点?清什么?)。遵循用户"极简、不预设复杂架构"的偏好(见 [HANDOFF_PROMPT §12](HANDOFF_PROMPT.md))。
- **状态**: ⛔ 已关闭,暂不实施。如未来真的有"批量清任务/日志"的需求,再单独评估,不与本条混淆。

### 3.4 日志 DOM 上限 1000 行

- **现状**: [LOG_DOM_CAP L471](static/app.js#L471) 硬编码 1000(模仿 VSCode 终端)。
- **问题**: 用户查早 10 分钟的日志会被截,要看完整只能 export。
- **建议**: 暴露为设置项(暂存 settings 没做)。
- **状态**: ⏸️ 当前 1000 是默认值,不动;如需要再调。

### 3.5 WS 重连 max 5 次

- **现状**: 5 次后弹 "请刷新页面"。
- **问题**: server 重启慢于 5+5=15s 时,自动恢复就废了。
- **建议**: max 提到 10 次,或直接无限(代价:server 挂了白推)。
- **状态**: ⏸️ 当前 5 是合理值,不动;若用户反馈再调。

### 3.6 adb 设备列表刷新间隔

- **现状**: 3s 轮询。
- **问题**: 快设备列表刷得快(浪费) / 慢设备列表刷得慢(不实时)。
- **建议**: 改为 5s + 手动刷新按钮;或保留 3s(当前)。
- **状态**: ⏸️ 当前 3s OK,不动。

### 3.7 删除任务时是否要 confirm dialog?

- **现状**: `×` 按钮直接删除(无 confirm)。
- **问题**: 误点会丢日志。
- **建议**: 加 confirm(`confirm("删除任务 + 日志?")`)。
- **状态**: ⏸️ 当前无 confirm,用户没反馈过;若误删过再加。

### 3.8 多脚本队列(device-uniqueness 是否过严)

- **现状**: 后端 409,一台设备同时只能跑 1 个任务。
- **问题**: 想跑 "先跑 wifi_reboot 30 次,再跑 ir_runner" 这种串行编排怎么办?
- **选项**: (a) 保留 strict 1 个;(b) 加任务队列(`/api/run` 支持排队);(c) orchestrator(超出平台范围)。
- **状态**: ⏸️ 当前 strict 1 个,符合 MVP;如要做选 b。

### 3.9 Long 按键阈值

- **现状**: `ir_runner.py` parser 把 kind=`LongXXXX` 解析为长按 XXXX ms(`startswith("long")`)。
- **问题**: 用户写 `Long3000` 还是 `LongPress3000` 还是 `Long-3000`?
- **建议**: 统一格式 + README / KEY_REFERENCE 加示例。
- **状态**: ⏸️ 当前是 `Long3000`,需要 README 加一行示例。

### 3.10 设备名很长时的 truncate

- **现状**: `.device-name` title 属性给完整 serial,正文 ellipsis(但没测过)。
- **建议**: 加 CSS `text-overflow: ellipsis; overflow: hidden; white-space: nowrap;` 兜底。
- **状态**: ⏸️ 当前纯靠 flex 收缩,没测过长 serial。

---

## §4. 已知 UX 边界 case(已被发现但未修)

| # | 场景 | 当前行为 | 备注 |
|---|---|---|---|
| 1 | WS 在 server restart 时 log console 卡最后一行 | 没手动 rejoin | 用户得手动点 task card 或重启浏览器 |
| 2 | 任务被删除时 log 文件可能短暂残留 | 已修(后端同步删) | 偶发 race(删除 + 写同时) |
| 3 | modal 在小屏幕(< 1024px)的响应式 | 没做 | 桌面端 OK,平板可能溢出 |
| 4 | dark mode 切换 | 只有一种暗色主题 | 用户没要求过亮色 |
| 5 | 设备卡拖拽排序 | 没做 | 设备列表按 adb 返回顺序 |
| 6 | 服务器时区 | 用 `datetime.now()` 本地时间 | 跨时区会乱;用户单机 OK |
| 7 | 长 serial 截断 | 靠 flex 收缩 | 真没测过 |
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
| active | accent 1px | panel 加亮 | ✓ 已选中 / ⚡ 运行中 |
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

## §6. 文件清单与引用

| 文档 | 用途 | 状态 |
|---|---|---|
| [HANDOFF_PROMPT.md](HANDOFF_PROMPT.md) | 真源(代码 / API / 数据模型 / 前端交互模型 / 踩坑清单) | 活跃 |
| [FRONTEND_UX.md](FRONTEND_UX.md) ← 你正在读 | 前端交互设计理由 + 决策时间线 | 活跃(行号已漂移,看 HANDOFF §5.5) |
| [SIMPLE-ARCHITECTURE.md](SIMPLE-ARCHITECTURE.md) | 架构总览 / 数据流 / 脚本契约 | 活跃(2026-08-25 重写) |
| [SIMPLE-PRD.md](SIMPLE-PRD.md) | v2.0 初版 PRD | **历史归档** |
| [SIMPLE-PLAN.md](SIMPLE-PLAN.md) | v2.0 初版实施计划 | **历史归档** |
| [CHANGELOG.md](../CHANGELOG.md) | 版本沿革(v2.2.0 起逐版记录) | 活跃 |
| [README.md](../README.md) | 用户面向精简说明 | 活跃 |
| [KEY_REFERENCE.md](../ir_sequences/KEY_REFERENCE.md) | KEY_* 24 + KEYCODE_* 厂商 23 + 原生 26 按键速查 | 活跃(手动维护) |

---

**最后更新**: 2026-08-25(v2.5.1)
**会话来源**: 累计多轮 IR 序列、modal picker、device card dropdown gating、localStorage 持久化、UI 徽章、WS 重连、WS taskId filter、onSelectDevice 关 WS、文档统一清理、default.ini 5/6 字段修复、脚本参数前端可配置(v2.2.0)、select 下拉框参数(v2.3.0)、性能实时图表(v2.4.0)、图表墙钟横轴/双Y轴/长时检测(v2.4.1/v2.4.2)、电池曲线泛化(v2.5.0)、log console 全英文 + 1h 滚动窗口 + 放电关机卡 + 全程图导出 0.5h 刻度(v2.5.1)等话题。
**维护规则**: 新交互决策追加到 §1;新改进建议追加到 §2;新开放问题追加到 §3。**不要覆盖已有条目**(保留时间线)。行号一律指回 HANDOFF §5.5(本文行号已漂移)。