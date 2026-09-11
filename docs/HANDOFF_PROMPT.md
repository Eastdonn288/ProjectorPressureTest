# PPTP — Handoff Prompt for the Next Session

> **本文件是 PPTP 项目的当前真源。**其他文档(SIMPLE-PRD.md / SIMPLE-PLAN.md / SIMPLE-ARCHITECTURE.md)已标记为历史归档,只供参考。README.md 是用户面向的精简说明,[FRONTEND_UX.md](FRONTEND_UX.md) 是前端交互设计沉淀(决策 + 改进建议 + 开放问题),本文件包含完整的 API、数据模型、设计决策、bug 历史等内部细节。

> 你是下一任接手 PPTP 项目的 Claude / 任意 LLM。这份文档是项目当前状态的自包含摘要,用来在你没有对话历史的情况下迅速上手,避免基于陈旧/不完整记忆产出不存在或已废弃的特性。
>
> **使用方式**: 把你和用户的实际任务描述放在这份 Prompt 之后,逐节阅读。动手前先 grep / Read 标注的源码行确认。

---

## 0. You are working on

**PPTP — Projector Pressure Test Platform**。一个本地单机的 Web 压测平台,用于在多台 ADB Android 投影仪设备上启动 Python 压测脚本(IR 遥控器模拟、WiFi 开关/重启/切换压力测试),实时看每台设备输出的日志。脚本可用 `PARAMS` 自描述可配置项,前端自动识别并弹窗配置。

设计原则:
- **纯本地 / 单机** — 所有数据落本地,无鉴权
- **vanilla JS, 无前端框架** — 单一 IIFE 模块
- **后端最小化** — FastAPI 单文件
- **只一个跨设备数据源:ADB** — WebSocket 推 ADB 进程 stdout

---

## 1. Repo layout (关键文件 + 行号)

```
E:\ProjectorPressureTest\
├── server.py                       (2528 行) — FastAPI 后端,所有 API + WS + 三源采集引擎 + 自动存档
├── start.bat                       — 启动 launcher(成功自动关闭,失败保留信息)
├── server_window.ps1               — PPTP-Server 窗口:显示 uvicorn 日志 + 写文件
├── stop.bat
├── docs\
│   ├── HANDOFF_PROMPT.md            ← 你正在读
│   ├── SIMPLE-PRD.md
│   ├── SIMPLE-ARCHITECTURE.md
│   └── SIMPLE-PLAN.md               — 早期需求/架构/计划
├── scripts\                         (平台调用的压测脚本)
│   ├── ir_runner.py                 — 红外遥控序列循环(自包含 IRRemote,默认无限循环;长按=down+hold+up)
│   ├── wifi_onoff_stress.py         — WiFi 开关压力(关/开循环 + wpa_cli 扫描统计;PARAMS 可配)
│   ├── wifi_reboot_stress.py        — 重启 + WiFi 重连压力测试(PARAMS 可配)
│   ├── wifi_switch_stress.py        — 多网络循环切换(预置 WIFI_NETWORKS;PARAMS 可配)
│   ├── sensor_reboot_stress.py      — 重启 + 传感器(gsensor/ToF)回连压测;PARAMS 可配(含 select 下拉框)
│   ├── app_launch_stress.py         — APP 冷/热启动耗时压测(APP_PRESETS 内置 3 个 APP 一起跑,am start -W TotalTime + logcat Displayed 交叉验证)
│   ├── perf_monitor.py              — 性能监控(CPU/GPU/内存% + 前台APP CPU%;PERF| 流 → 前端图表,PARAMS 可配)
│   ├── battery_inout_stress.py      — 电池充/放电压测(电量/温度/电压,串口开 health 轮询;PARAMS 可配,全英文 ASCII)
│   └── bt_reboot_stress.py          — 重启 + 蓝牙音箱 A2DP 回连压测(reboot → 上线 → 轮询回连;PARAMS 可配,判定 = 适配器开 + A2DP CONNECTED)
├── ir_sequences\                    (用户可编辑的 .ini 序列文件 + 按键速查)
│   ├── 1.ini                        — 当前序列(KEY_VCR + KEYCODE_HDMI)
│   └── KEY_REFERENCE.md             — 按键速查:KEY_* 24 + KEYCODE_* 厂商 23 + 原生 26
├── static\
│   ├── app.js                        (2317 行) — 前端所有逻辑
│   ├── index.html                    (155 行)  — 4 面板 + 2 模态
│   ├── style.css                     (872 行)
│   └── vendor\echarts.min.js         — ECharts 5.5.1 本地化(无构建步骤)
├── reports\
│   ├── stress-test\wifi\            — WiFi 压测报告 JSON(脚本生成,gitignore)
│   └── stress-test\sensor\          — 传感器压测报告 JSON(reboot_gsensor_*.json / reboot_tof_*.json)
├── logs\                            — **运行期临时工作区**,任务一结束内容就搬走
│   ├── server.out.log                — uvicorn stdout
│   ├── server.err.log                — uvicorn stderr
│   └── <stem>.<source><suffix>       — 运行中每任务每通道一份(见 §6 决策 #1 命名方案)
│       stem   = <task_id>_<yyyyMMdd-HHmmss>_<script-stem>_<device>
│       source = stdout | logcat | serial
└── archive\                         — **永久保留**(见 §6 决策 #0),**按模块分类**
    └── <module>\{wifi,perf,battery,sensor,app-launch,ir,bt,other}\
        └── <yyyyMMdd-HHmmss>_<script-stem>_<device>\
            ├── stdout.log / logcat.log / serial.log   — 从 logs/ 移入
            ├── report.json   — 脚本自己写的那份的副本(从 reports/ 复制)
            ├── chart.png     — 浏览器渲染回传(perf/battery 才有,且要求结束时浏览器开着)
            └── summary.json  — 清单:task_id/参数/状态/bytes+lines/report_source/notes
```

> `archive/` 目录名**不含 task_id** —— 那是给人看的。task_id 在 `summary.json` 里。
> 模块由 `ARCHIVE_MODULES` **显式映射**(脚本名 → 模块),不靠文件名猜;未登记的落 `other/`。
> 模块名与 `reports/stress-test/<模块>/` 对齐。

> **优先看的文件**: `server.py` + `static/app.js` + `static/index.html` + `static/style.css`。其余均为辅助。

---

## 2. Tech stack (单行)

**Backend**: Python 3.10+, FastAPI, uvicorn, pydantic 2.x, 纯 stdlib subprocess。

**Frontend**: Vanilla JS (ES2017+), 无任何框架/构建工具, 无 npm。
- 4 面板 CSS Grid: `240px 240px 1fr 300px` (设备 / 脚本 / 日志 / 任务)
- 单个 IIFE 包裹的 `state` 全局
- 模态用纯 HTML + CSS, 无 portal

**AD-hoc infra**: `start.bat` 启动 `uvicorn` + 打开浏览器。`stop.bat` 强杀。

---

## 3. How to run

```bash
# 启动(开发)
cd E:\ProjectorPressureTest
E:\Conda_Environments\dev_env\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8000

# 浏览器打开 http://127.0.0.1:8000

# 验证
curl http://127.0.0.1:8000/healthz
# {"ok":true,"version":"2.6.0","time":"..."}
```

Python 依赖: `pip install fastapi uvicorn pydantic` + **`pyserial`**(可选但强烈建议)。
`pyserial` **不是**给服务端自己用的 —— 平台靠它枚举 COM 口(`serial.tools.list_ports.comports()`,
读的是 Windows 注册表 `HARDWARE\DEVICEMAP\SERIALCOMM`,和「设备管理器」同源),`battery_inout_stress`
也靠它开串口。**没装的表现极具误导性:任务照跑、没有任何报错,只是设备卡上的 COM 下拉框永远是空的**
(见踩坑 #36)。装的时候务必用**启动服务端的那个 Python**(start.bat 会挑 PATH 里第一个 `python`,
不一定是你在 conda 里装过的那个 —— 服务端窗口启动时打印的 `[OK] Python: ... at <路径>` 才是真身)。

---

## 4. Backend API surface (server.py)

所有路由(`@app.<verb>`):

| Method | Route | Purpose |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/` | 静态 index.html |
| GET | `/api/server/status` | uvicorn PID + uptime + 任务计数 |
| POST | `/api/server/shutdown` | 优雅关停(级联 CTRL_BREAK + os._exit) |
| GET | `/api/devices` | adb devices -l 解析结果 |
| GET | `/api/scripts` | scripts/*.py 列表(含 `has_params` 源嗅探标志) |
| GET | `/api/scripts/{name}/params` | **读脚本声明的参数 schema**(跑 `--dump-params`,按 `(name, mtime)` 缓存) |
| GET | `/api/sequences` | **ir_sequences/*.ini 列表** |
| GET | `/api/sequences/{name}` | 读单个 .ini 内容 |
| PUT | `/api/sequences/{name}` | 写整个 .ini 内容 |
| DELETE | `/api/sequences/{name}` | 删 .ini(default 拒绝) |
| POST | `/api/sequences` | **新建** .ini(模板) |
| POST | `/api/run` | 启动任务子进程(409 冲突 / device-uniqueness) |
| POST | `/api/stop/{task_id}` | 单任务 CTRL_BREAK |
| POST | `/api/tasks/force-stop-by-device/{serial}` | SIGKILL 该设备所有任务 |
| POST | `/api/tasks/force-cleanup` | SIGKILL 全部 + 清空 TASKS |
| POST | `/api/tasks/cleanup` | 删所有终态任务 + log |
| POST | `/api/adb/reconnect` | adb kill-server + start-server |
| GET | `/api/tasks` | 全部任务(无 _proc) |
| GET | `/api/tasks/{task_id}` | 单个 |
| GET | `/api/tasks/{task_id}/log?source=` | 单通道日志尾部(默认 stdout)。`source` 白名单校验,非法 → **400**;归档后自动从 `archive/<dir>/` 读 |
| DELETE | `/api/tasks/{task_id}` | **从列表移除任务,存档保留**(`_forget_task`,见 §6 决策 #0.5) |
| GET | `/api/serial/ports` | 本机 COM 口列表(`{"ports":[...],"available":bool}`);pyserial 缺失 → 空列表,永不抛 |
| POST | `/api/tasks/{id}/archive/artifact` | 浏览器回传 `{name, data}` 写进存档;`name` 白名单(chart.png/perf.csv)+ 8MB 上限 + hex 守卫 |
| POST | `/api/tasks/{id}/archive/reveal` | 资源管理器打开该任务存档目录(`os.startfile`) |
| POST | `/api/archive/reveal` | 打开存档根目录 |
| GET | `/api/archive/stats` | `{"count","bytes","dir"}` 存档占用 |
| WS | `/ws/logs/{task_id}?sources=` | 实时推送;默认 `stdout`。**前端只订阅 stdout**,所以 logcat/serial 帧根本不上网线 |

**关键不变量**:
- 一台设备同时只能跑一个任务(`api_run` 检查并发 409)
- 临时文件 `_seq_<uuid>.ini` 创建后由 `ir_runner.py` 在 finally 块里删除
- `delete /api/sequences/default` 拒绝(400)
- **采集是服务端职责**:脚本拿不到自己的 `task_id` / 日志路径,9 个脚本零改动
- `GET /api/tasks/{id}/log` 走**尾部读**(`_read_tail`,默认 4 MB 上限),绝不整文件 `read_text()` —— logcat 上限 256 MB,整读会硬冲内存

---

## 5. Frontend state shape (static/app.js L14)

```js
const state = {
  devices: [],                 // GET /api/devices
  scripts: [],                 // GET /api/scripts
  tasks: [],                   // GET /api/tasks
  deviceScripts: {},           // { [serial]: "ir_runner.py" }  — 设备→脚本
  deviceSequences: {},        // { [serial]: "default" }     — 设备→序列文件名
  deviceParams: {},           // { [serial]: { [script]: {param: value} } } — 设备×脚本参数(前端配置)
  selectedDeviceSerial: null, // 当前选中的设备(驱动脚本卡高亮)
  currentDeviceSerial: null,  // 日志面板绑定的设备
  currentTaskId: null,        // 日志面板绑定的任务(v2.4.0:也是性能图表的绑定键;v2.4.1 起样本带 clock 墙钟字段)
  // PERF 实时图表(v2.4.1;v2.5.0 起按脚本类型泛化 perfKind):perfMeta = 当前任务的 PERF| meta 行
  // (含 kind:"perf"/"battery");perfSamples = 每任务独立样本缓冲 { [taskId]: [样本] },
  // perf 类键 {t,cpu,gpu,mem,fg_cpu,fg_pkg,gpu_clk};battery 类键 {t,level,temp,voltage_mv,status,jump,jump_type}。
  // 两个字段都随任务切换换对象、随任务删除/重置清空——这是"切卡片不残留"的数据层。
  perfMeta: null,
  perfSamples: {},
  currentWs: null,            // 当前 WebSocket
  backendOnline: false,
  userScrolledUp: false,
  server: null,                // GET /api/server/status
  shuttingDown: false,
  seqContext: null,           // modal context: {target, file, device}

  // ===== Log capture (v2.6.0 采集 / v2.7.0 显式只显示 stdout) =====
  // 采集侧(服务端)仍在跑 logcat + 串口,前端**只渲染 stdout**且是永久的,所以
  // 这里已经没有 activeSource / captureCounts / CHANNELS / TABS_ENABLED 了。
  deviceSerialCapture: {},    // { [serial]: bool }  设备卡串口抓取勾选(本次会话)
  deviceSerialPort: {},       // { [serial]: "COM9" } 设备→串口口(持久化)
  serialPorts: [],            // GET /api/serial/ports 缓存
  archiveStats: null,         // { count, bytes } 存档占用(任务面板标题栏)
  chartPosted: {},            // { [taskId]: true } 已尝试回传过图表的任务(防重复)
};
```

**localStorage 持久化**(`STORAGE_KEY = "pptp.deviceState.v1"`):
- 保存:`selectedDeviceSerial` + `deviceScripts` + `deviceSequences` + `deviceParams` + `deviceSerialPort`
- **不保存** devices/scripts/tasks(始终从 server 拉)
- **不保存** `deviceSerialCapture`(勾选是本次会话的意图,与 `deviceScripts` 刻意不持久化的取舍一致)
- 防抖 200ms

---

## 5.5 前端交互模型:卡片绑定 / 切换 / 临时离线 / 性能图表(改前端必读)

> 平台最容易踩坑的区域。所有"切过去没更新 / 有残留 / 绑定错了"类 bug 都源于没理解本节的模型。**本节 5.5.1-5.5.4 的行号是 v2.5.2 时代的,已随 v2.6.0 改动失准 —— 请以 §10 的表为准,或直接 grep 函数名。**

### 5.5.1 单日志面板模型(全局 console,不是每卡一个)

- 整个平台**只有一个** `#log-console` + 一个 `#log-info` + 一个 `#perf-chart`,全部绑定 `state.currentTaskId`(L36)。
- **没有** per-card / per-device 的独立终端。设备卡、任务卡只是"把 currentTaskId 指到哪"的**入口**,不是数据容器。
- 绑定链:`onSelectDevice(serial)` L1280 或任务卡点击 → `viewTaskLogs(taskId)` L1128 → `setConsoleTarget(taskId, device)` L540(写 currentTaskId + `renderLogInfo` L546)→ `clearConsole()` L634 → `openWs(taskId)` L1156。
- **2s/3s 轮询只重建卡片 DOM,绝不重置 currentTaskId / 关 WS / 动图表**:`refreshTasks` L1480(2s)、`refreshDevices` L1392(3s)、`refreshServerStatus` L1541(5s)、`tickClock` L1508(1s),全部挂在 `init()` L1979(轮询挂载 L1986-1989)。若把副作用塞进这些回调,切走时用户正在看的日志会被持续打断。
- `viewTaskLogs` 幂等:`if (state.currentTaskId === taskId) return`(L1132-1136)——重复点同一任务不重开 WS(避免重连风暴),只刷卡片高亮。

### 5.5.2 卡片绑定与切换

- **设备卡点击 → `onSelectDevice(serial)` L1280**(3 分支):
  1. 该设备有 running/interrupting 任务 → `viewTaskLogs(running.task_id)`(L1288,绑定到在跑的任务)
  2. 否则找该设备最近任务(`started_at` 倒序)→ `viewTaskLogs(last.task_id)`(L1295)
  3. 否则(设备无任何任务)→ 关残留 WS、`currentTaskId = null`、`clearConsole()`、`syncPerfChart()`(L1300-1307,隐藏图表)——"空日志面板"态
- **任务卡点击 → `viewTaskLogs(task.task_id)`**(同一个函数)。
- **切换语义**:切任务 = 换 currentTaskId + clearConsole + 重开 WS。切走后旧 WS 的 `onclose` 调 `scheduleReconnect(taskId)` L1214,但内部 `if (state.currentTaskId !== taskId) return` L1217 → **切走的任务不重连**。
- **WS 帧过滤(不残留的总闸)**:`onmessage` 第一行 `if (state.currentTaskId !== ws._taskId) return`(L1182)——旧设备的日志帧直接丢弃,这是"切设备时旧日志不出现"的根因。**任何新增 WS 帧处理都必须过这个闸**。
- **clearConsole 不关 WS**:切到空设备只清 DOM,旧 WS 由 `onSelectDevice` 的 else 分支显式 close(L1300-1303)。

### 5.5.3 合成卡片:临时离线 + 放电关机

**临时离线卡(reboot 压测)**

- **来源**:`renderDevices()` L322 对"有 running 任务但不在当前 `adb devices` 列表"的设备合成 `_temp_offline: true` 的卡(L340),状态文案"(临时离线 · 任务在跑)"(L394)。
- **为什么存在**:reboot 压测等场景设备按脚本意图离线几分钟;没这卡用户会以为设备丢了、任务没了。
- **交互规则**(都依赖 `_temp_offline` 标志):
  - `isOffline = !isTempOffline && !isBattOff && d.status !== "device"`(L402)——**临时离线不算离线**,卡可交互(可点、可硬停)
  - 脚本下拉框 disabled 条件 `notSelected || !!running || isOffline`(L411)——临时离线卡仍锁(任务在跑本来就不能换脚本)
  - 硬停按钮:`renderLogInfo()` 里 `isTempOffline = !liveSerials.has(t.device)`(L578)+ `$("btn-hard-stop").disabled = !(isLive && isTempOffline)`(L579)——只有"当前任务设备的设备临时离线"才启用;正常设备用"中断"走 CTRL_BREAK
  - 卡片样式 class `device-card-temp-offline`(CSS L276 起)
- **切换/刷新逻辑**:`refreshDevices()` L1392 清理"真断连"设备时,`keptSerials` 集合(L1415,= runningSerials L1407 + 放电关机设备)保证**有 running 任务 / 放电关机状态的设备不丢已记脚本/序列**(L1426/L1432);只有"既不在 adb 列表又没有 running 任务、也不在放电关机态"才 `delete deviceScripts/deviceSequences`。
- **注意**:临时离线卡只在任务 running 期间存在;任务结束后下次轮询卡消失(设备若真没了则回到"无设备"态)。

**放电关机卡(电池测试,v2.5.1)**

- **背景**:电池充/放电测试以"设备关机"作为放电模式的中止信号(脚本 `stop_reason:"power_off"` 返回 0)。任务结束后设备不在 `adb devices`,若按旧逻辑卡片会直接消失,看起来像"设备丢了"。
- **来源**:`renderDevices()` L322 在合成临时离线卡之后,对**终态 battery 任务**(`status ∈ finished/interrupted/failed`,L357)+ 设备不在 `adb devices` + **该 battery 任务仍是设备最新任务**(`latestTaskOf()` L315 校验,防被后续任务顶掉)的设备合成 `_batt_off: true` 的卡(L370)。model 文案按 `latest.params.mode` 区分放电/充电(L363/L367)。
- **交互规则**(同临时离线):
  - `isOffline` 判定同样排除 `_batt_off`(L402)——放电关机卡可交互
  - 状态文案 `stText = "设备已关机"`(L393);badge `放电关机`(红,`device-actions` 里 `isBattOff` 分支 L390);卡片虚线红边 class `device-card-batt-off`(CSS L290 起)
  - **空态检查排除 battOff**(L375 `...&& !battOff.length`)——设备全部关机、只剩放电关机卡时仍显示卡,而不是"未检测到 ADB 设备"
- **注意**:设备重新出现在 `adb devices` 时卡片自动回到正常态;删除对应任务后卡消失。

### 5.5.4 性能图表(#perf-chart)绑定与切换

- 图表 DOM 是 `#perf-chart`(200px,挂在 `#log-info` 与 `.panel-body` 之间),**不是每卡渲染**——和 console 一样全局绑定 currentTaskId。
- **不残留三件套**:
  1. `syncPerfChart()` L742 = dispose+隐藏 / rebind+重建
  2. WS 帧过滤(L1182)+ `state.perfSamples[taskId]` 每任务独立缓冲
  3. `resetPerfBuffer()` L733(ws.onopen 调)+ 按 `t` 去重(`pushPerfSample` L723,防重放叠加双曲线)
- **图表类型按脚本泛化(perfKind)**:`PERF_SCRIPT_KINDS` L647 映射 `perf_monitor.py`→`perf`、`battery_inout_stress.py`→`battery`;`perfKind(taskId)` L651 查当前任务 kind,`isPerfTask` L657 = kind 非 null。**新增 PERF 脚本必须在这里注册**(见 5.5.5 #19)。
- **显隐规则**:`isPerfTask(taskId)` 为真才显示;非 perf 任务 / 无任务 / echarts 未加载 → dispose + `el.hidden = true`。
- **调用点(全部离散,绝不在 2s 轮询)**:
  - `viewTaskLogs` L1148 · ws.onopen L1173(带 `state.currentTaskId !== taskId` 陈旧防护 L1169,见 5.5.5 #6)
  - `onSelectDevice` 空任务分支 L1307 · `onDeleteTask` L1356 · `onCleanupTasks` L1383 · `onResetClick` L1921
- **数据流**:脚本打 `PERF|` 行 → 现有 WS 原样流转(服务端零改动)→ `appendLogLine` L593 拦截前缀 → `handlePerfLine` L681(meta 存 `state.perfMeta`{含 kind};**sample 按 `obj.type === "sample"` 分发** → 入 `perfPending` + 转可读行;perf 类带墙钟 `[perf] 11:44:48 (0.0s) ...`,battery 类带 `[batt] HH:MM:SS (X.Xs) level=87% temp=36.2C voltage=8.4V status=charging`+ 跳变时 ` [!] jump[...] prev->87%`)→ `flushLogBuffer` L605 末尾调 `flushPerfChart` L930(rAF 批处理,长程重放不卡)→ 追加 `[perfX(s), val]` 进 option 并 `setOption`(`perfX(s) = s.clock ?? perfXBase + t*1000`)。**分发键是 `type:"sample"`,脚本缺它样本全被丢弃(见 5.5.5 #17)**。
- **x 轴窗口(v2.5.1)**:**固定 1h 滚动窗口**,实时显示最近 1 小时的曲线(`PERF_WINDOW_MS = 60*60*1000` L62)。`buildPerfOption(taskId, fullRange)` L828 的 live 分支 xAxis `min: perfWindowEnd(taskId) - PERF_WINDOW_MS` / `max: perfWindowEnd(taskId)`(右缘 = 最新样本的墙钟,无样本时回退 `perfXBase`;`perfWindowEnd` L783),`interval: 10*60*1000` + `splitNumber: 6` 钉死 10 分钟步长(~6 个刻度)。`flushPerfChart` L930 每次 flush 后把窗口滚到最新样本、并把 `x < minX` 的点从 series data `shift()` 掉(窗口外的点不占内存)。**已移除 dataZoom**:它会让用户拖出 1h 视野,和 flush 滚动互斥。完整历史仍留在 `perfSamples[taskId]` 缓冲(`PERF_SAMPLE_CAP=86400`)供 CSV 导出与全程 PNG 导出
- **log 控制台 ASCII 规则(v2.5.1)**:凡打进 `#log-console` 的内容**必须全英文/ASCII**,包括 readableBattLine、meta 行(`[batt] monitor start | mode=...` / `[perf] monitor start | CPU=...`)、WS 重连/重放/截断提示、占位符。所有时间戳用 `fmtClock`(`toLocaleTimeString("zh-CN",{hour12:false})` L72)—— 强制 24 小时制 `HH:MM:SS`,避免 zh-CN 浏览器吐出"下午3:30"这类中文。**UI 文案(卡片/弹窗/图表系列名/导出表头)仍是中文**,只有 log console 输出是英文(见 5.5.5 #20)
- **系列与轴**:perf 类 = GPU 绿线 / 前台APP 红线由 `meta.sources.gpu` / `meta.sources.fg` 真值决定(`buildPerfOption` L828),CPU 蓝 / MEM 黄恒有;battery 类 = 电量(蓝,左轴 0-100% `{value}%`)+ 温度(红,右轴 auto `{value}°C`)两线(`perfSeriesKeys=["level","temp"]`)。**series 键顺序统一由 `perfSeriesKeys` 驱动**(`mk()` 组装 L800;`backfillPerfOption` L916 / `flushPerfChart` L930 的循环都用它,kind 无关)。
- **导出**:`onExportLog` L1047 下载 txt 后 `sleep(300)` 依次触发 csv + png;battery 类 csv 后缀 `.batt.csv`(表头 `t_sec,t_wall,level_percent,temp_c,voltage_mv,status,jump_delta,jump_type`,见 `buildPerfCsv` L1004)。**PNG 是全程图(v2.5.1)**:`exportFullChartDataUrl(tid)` L968 从 `perfSamples[tid]` 全量渲染,不再依赖当前视图/当前图表绑定(切走后再导出也是全程,`perfChart` 为 null 也不跳过)。实现:临时把 `perfXBase` 锚到任务 `started_at`(保证 `t`-fallback 样本时间正确)→ `buildPerfOption(tid, true)` 的 **fullRange 分支**(L831-841:min/max 取全量样本,`xInterval = 30*60*1000` = **0.5h 一个刻度**;live 分支的 10min/6 刻度只在屏幕上用)→ 建离屏 1280×360 div → `echarts.init` + `setOption` + `getDataURL({pixelRatio:2, backgroundColor:"#0a0d12"})` → dispose + 移除 div + 还原 `perfXBase`。全程时长由样本跨度决定,不受 1h 窗口限制。

### 5.5.5 踩坑清单(按"踩过的坑"排序)

1. **ECharts 压缩版没有字面量 `echarts.init`** —— grep 校验会误判 0 匹配。可靠校验:node `require('./echarts.min.js')` + `echarts.version` + `typeof echarts.init === "function"`(本库 require 出 version 5.5.1;文件里的 `version:"5.6.0"` 是内嵌 zrender,`version:"1.1"` 是 SVG 命名空间,均正常)
2. **ECharts init 在 hidden div 上是零尺寸白板** —— 必须 `el.hidden = false` **之后**再 `echarts.init`(`syncPerfChart` 已处理;index.html 里 `#perf-chart` 默认 `hidden`,style.css 的 `[hidden]{display:none !important}` 兜底)
3. **图表数据不能只 cap 缓冲** —— `perfSamples` **和** option 的 series data 都要 cap 86400(`PERF_SAMPLE_CAP` L61 + `flushPerfChart` L945 内 splice);只 cap 一个 → 重放后 option 数据无限涨、与缓冲脱节。v2.5.1 起滚动窗口的 `shift()`(L957)在 cap 前就把窗口外点剔掉,二者互补
4. **重放去重必须按 `t`(elapsed 秒)** —— 切走再切回,WS 会重放同一任务全部样本;不按 `t` 去重 → 曲线叠加成双份。同任务重跑生成新 taskId,不会误去重
5. **图表逻辑塞进 2s 轮询 = 灾难** —— `refreshTasks` 每 2s 重建卡片;在里面 sync 图表会导致切走后图表被持续重建/闪烁。只在 5.5.4 列的离散点调用
6. **WS 陈旧 onopen** —— 快速切任务时旧 WS 的 onopen 可能后到;必须 `if (state.currentTaskId !== taskId) return` 再动 perf 状态(L1169),否则会清掉新任务刚建立的 meta
7. **PERF 行 JSON 带空格**(`json.dumps` 默认分隔符是 `", "` 和 `": "`)→ `JSON.parse` 没问题;**不要**用正则/字符串匹配提取字段
8. **`dumpsys cpuinfo` 是近 5 分钟滚动均值**,不是瞬时值 —— 实时前台 APP CPU% 必须用 `top -n 1 -b`
9. **`top -n 1 -b | head -30` 会截断前台 APP**(低 CPU 的 APP 排不进前 30)→ 用 `top -n 1 -b | grep <pkg> | head -3`(首样本无包名时回退 `head -40`)
10. **复合 adb shell 必须 list-arg subprocess** —— `shell=True` 会让 `;`/`|`/`>` 被 Windows cmd.exe 本地吃掉;用 `["adb","-s",serial,"shell",compound_cmd]`
11. **本设备 `su -c` 报 `invalid uid/gid`** —— 用裸 `su 0 <cmd>`(sensor/perf 脚本同一形态)
12. **meminfo 含 `IommuLowUsage/IommuHighUsage` 行** —— 节点可读性探测若按 ERROR_MARKERS 子串匹配会误判"不可读";用 parser 成功/失败判定(`parse_meminfo` 等)
13. **GPU 节点不在标准路径** —— MT9676 的 Mali 计数器在 `/sys/kernel/debug/mali0/dvfs_utilization`(标准 Mali/MTK sysfs 路径全不存在),且需 root;换芯片/换设备先跑 `--probe`
14. **视频播放时 GPU%≈0 是正常的,不是读数失效**(2026-08-25 实机排查确认) —— 本机是 **MStar 显示管线**(SurfaceFlinger 报 `displayName="MStar Demo"`)的 MT5896 Android TV:视频**解码走硬件 VPU**(不碰 GPU)、**画面合成走 HWC 硬件叠加层**(DEVICE layer,非 GPU),GPU 只画 UI(CLIENT layer,静态时缓冲缓存不重绘)。证据链:① 14:11 报告 GPU=0 时 `_gpu_prev` 的 busy 全程冻结在 446506278、idle 稳步增长(`fg_pkg` 全程 launcherx,视频在后台/预览播放,前台根本没有渲染任务);② 计数器本身正常 —— 实测启动 Settings 时 busy 从 446506278→446515551(真实 GPU 渲染事件会跳);③ 11:56 同机 GPU 平均 42%(连续应用切换/UI 动画这类持续负载)证明指标有效。**判断口诀:GPU% 对"应用切换/UI 动画"压测有意义,对"视频播放"无意义 —— 这是硬件分工使然,别当成监控失效去"修"(已向用户说明,用户选择文档化不再排查)**
15. **Chrome/Edge 单次点击允许多个下载;Firefox 可能拦截第 2/3 个** —— 导出三件套文档已注明,无需修
16. **本地 vendor 依赖要 pin 版本 + 记来源** —— echarts.min.js = 5.5.1(主源 jsdelivr,备源 npmmirror);换版本前确认 UMD 全局导出 `echarts` 存在
17. **wire 契约以端到端实证为准,不能只信 docstring** —— v2.4.1 那次事故:perf_monitor.py 的 docstring 明写 sample 行带 `type:sample`,但 `emit` dict 实际**漏了该字段** → 前端 `handlePerfLine` 按 `obj.type==="sample"` 分发,sample 全被当未知类型丢弃,图表不出线 + 导出只剩 txt。**脚本每次改动 PERF 输出,必须真机跑一遍、看 WS 日志里 `PERF|` 行真的带 `type` 再交付**;改协议时前后端都 grep `type:`/`clock` 引用点。防御性兜底:前端对无 `type` 的 PERF 数据行按 sample 解析(现未做,可加)
18. **电池数据只能走串口,且 COM 口要用户选** —— 本设备 `ro.config.batteryless=true`,`adb shell dumpsys battery` **默认只缓存**不实时;必须经**串口 console**(115200 baud)发 `cmd android.hardware.health.IHealth/default set polling true` 才拿到实时电量/温度。**SELinux 挡 adb 路径**(`su 0 ... set polling true` 经 adb 报 `Failed transaction 2147483646`),所以**没有串口就拿不到实时电池数据**。`serial_port` 是 select 下拉框、`--dump-params` 时枚举当前 COM 口(本机 COM9 = FTDI VID:PID 0403:6010),但**哪个口对着 BMS 要用户在试跑时确认并自选**。轮询开启后需每 `SERIAL_RE_ENABLE_MIN` 分钟重发一次保活(设备可能把 polling 关回去)
19. **PERF 前端按 kind 分支,新脚本必须注册 + 字段带全** —— v2.5.0 起 `handlePerfLine`/`buildPerfOption`/`buildPerfCsv`/`onExportLog` 都按 `perfKind(taskId)` 分支(`PERF_SCRIPT_KINDS` 未注册的脚本一律当普通日志,不出图);**新增 PERF 脚本必须** ① 在 `PERF_SCRIPT_KINDS` 加映射,② sample 行带 `type:"sample"` + `clock`(见 #17),③ 需要读数的字段进 meta.sources 由前端显隐。**语言分工副作用**:脚本全英文 → 其 `PARAMS` 字段名/`label` 是英文,前端参数弹窗原样显示英文标签(与其它中文脚本不同,属预期,已文档化)
20. **log console 全 ASCII 靠双保险** —— v2.5.1 起 `#log-console` 只准英文/ASCII。两类常见泄漏:① **`toLocaleTimeString()` 无 `{hour12:false}`**:zh-CN 浏览器会吐"下午3:30"——log 里所有时间戳必须走 `fmtClock`(L72);② **meta/sample 可读行里嵌中文状态串**:脚本全英文则 status/jump_type 天然 ASCII,但前端旧实现有 `STATUS_ZH`/`JUMP_TYPE_ZH` 中文映射表(已删)——**前端侧任何新增的 log 文案都不得用中文**。UI 文案(卡片/弹窗/系列名/导出表头)不受此限。校验:grep `[^ -~]` 扫 `appendLogLine`/`handlePerfLine`/`flushLogBuffer`/`openWs`/`scheduleReconnect` 涉及的所有字符串
21. **固定 1h 窗口与 dataZoom 互斥** —— v2.5.1 把 x 轴改为固定滚动窗口(右缘=最新样本)后**禁用了 dataZoom**:zoom 会拖出 1h 视野、与 flush 逐帧把窗口拉回最新互相打架。若要放大看局部,只能缩短窗口或加独立控件,不要简单把 dataZoom 加回去。窗口滚动靠 `flushPerfChart` 里 `minX=endX-PERF_WINDOW_MS` + 对 series data `while (d[0][0] < minX) d.shift()`(L957)。**注意:dataZoom 只影响屏幕上的 live 图;全程 PNG 导出走 `exportFullChartDataUrl` 的 fullRange 分支(L831-841),天然不涉及窗口/dataZoom**
22. **`#log-info` 每 2s 被整体重写,任何嵌进去的东西都会被销毁** —— `refreshTasks`(2s)无条件调 `renderLogInfo()`,它 `innerHTML = ...` 整个重建。所以**任何需要跨轮存活的东西都不能放在 `#log-info` 里**,而且想在它里面挂事件监听是徒劳的。(v2.6.0 曾据此预留 `#channel-bar` 兄弟节点做通道计数 pills,v2.7.0 连同显示方案一起删除 —— 但这条约束本身仍然成立,是 `#log-info` 的固有性质)
23. **动态计数绝不能进渲染键** —— `captureCounts` 每 2s 变;若并入 `tasksRenderKey`/`devicesRenderKey`,每次轮询都会判定"变了"→ 全量重渲染卡片。同理 `_public_captures` 的公开快照**刻意只含 `active/status/restarts/detail/port`**,不含 `lines/bytes/dropped`。计数只走 WS `capture_counts` 帧
24. **`_stop_captures` 必须对"从未启动的通道"跳过** —— 实机踩到:一个从未启用的串口通道被收尾逻辑改写成 `stopped/task_end`,把真正的 `failed: port not present` / `skipped: script_owns_port` 诊断抹掉了。守卫:`ctask is None and proc is None and _stop_evt is None` → `continue`
25. **logcat 的 `-T` / `-t` 语义与 `-c` 禁令** —— `-T N` = tail N 行**且继续跟随**(平台用 `-T 1`);`-t N` **隐含 `-d`**(dump 完就退,不能持续跟随,不可用)。**永远不要跑 `logcat -c`**:`app_launch_stress.py` L152/L223 依赖设备端环缓冲(`logcat -d -s ActivityTaskManager`)做启动耗时交叉验证,清缓冲会**静默**破坏它的判定。默认缓冲 `main,system,crash`(不含 events);`*:V` 在话痨设备上 24h 可达 GB 级,平台默认 `*:I`
26. **串口采集必须用真线程,且不用 `readline()`** —— 串口没有文件描述符,无法走 asyncio;用裸守护线程 + `asyncio.Queue(maxsize)`(满则**丢最旧**并计 `dropped`,无界队列 = 内存泄漏)。跨线程只能用 `loop.call_soon_threadsafe`。**别用 `readline()`**:设备 console 的提示符没有换行,`readline()` 会每次等满 timeout 才返回;用 `read(in_waiting or 1)` 自己按 `\n` 切。**不要动 DTR/RTS**(保持默认):实机验证打开 COM9 **没有**触发设备 reboot banner,证明默认拉线状态对本设备 console 无副作用
27. **logcat 重启预算是"连续"而非"终身"** —— v2.7.0 前 `LOGCAT_MAX_RESTARTS=5` 只增不减,而设备重启期间 adb 常常**直接退出**(而不是阻塞在 `- waiting for device -`),一次 30 秒宕机就要烧掉 ~15 次 → **默认 100 轮的重启压测在第 6 轮左右 logcat 就永久死了**,之后一行都不采。现在预算按**连续**失败计,logcat 一吐出真实输出就清零(输出即设备回来的证明),上限 60(≈2 分钟持续失联)。**改这里之前先想清楚:任何"终身计数"的预算在长跑里都会被打穿。**
28. **收尾绝不能覆盖通道自己给出的终止诊断** —— `_stop_captures` 曾经无条件写 `status="stopped" / detail=reason`,于是"串口根本没打开成功"会被报告成**健康地正常停止**,`capped`(日志被截断)也被抹成正常。现在 `failed`/`unavailable`/`skipped`/`capped` 走 `keep_status` 分支保留自身状态(**teardown 照常跑** —— 该死该收的进程还是要收)。这条是 v2.6.0 的 `ctask is None and proc is None` 守卫**不够用**的地方:那个通道有 ctask,守卫拦不住。
29. **重启后 `-T 1` 会丢掉整个开机窗口** —— `-T 1` 只回读 1 行,而重启的原因通常正是设备重启,开机日志就在刚刷新的环缓冲里,结果文件要等 adbd 回来(约开机 25 秒)才有内容。现在首次启动 `-T 1`、**重启时 `-T 2000`**;marker 行明说"可能重叠"(adb 抖动的非重启场景确实会重复)。
30. **串口重试循环里 `cap["_stop_evt"]` 必须每次重指** —— `_stop_captures` 是通过 `cap["_stop_evt"]` 停读线程的(取消 asyncio task 对线程无效)。改成重连循环后每次尝试都是**新的线程 + 新的 Event**,所以 `_serial_attempt` 开头必须 `cap["_stop_evt"] = stop_evt` 重新指过去;指向旧 Event 就停不住当前线程,会留下一个永远写控制台的孤儿。同理,通知状态变化**只在"进入 reconnecting"那一刻**发 —— `captures` 在任务对象上、因而属于 `tasksRenderKey`,每次重试都 announce 会让任务列表在重连风暴里反复全量重渲染(踩坑 #23)。
31. **`adb reconnect offline` 而不是 `kill-server`** —— 掉线自动重连只能用前者:它只影响 offline/unauthorized 设备,**在线设备上正在跑的其它脚本完全不受影响**;`kill-server` 会打断所有 logcat,那是「重置」按钮该干的事。用户对这条有硬约束原话:「不能影响其它脚本运行」。watchdog 也只对"有正在跑的任务"的设备动手,并且每设备 20s 才试一次。
32. **删掉某个后台任务前,先确认它还在做别的事** —— v2.7.0 删掉 2s 计数心跳(它只为已删的通道 pills 存在),**连它唯一在做的 `t["captures"] = _public_captures(t)` 一起删了**,于是 `GET /api/tasks` 的通道状态冻结在任务开始那一刻,只有任务结束才更新,而且没有任何报错。现在由 `_announce_captures(task)` 在所有状态迁移点负责。**教训:删一个循环时,把它"顺便在做的事"列出来,别只看它的名义职责。**
34. **关 socket 也要有超时,理由和发消息一样** —— v2.7.2 让 `_broadcast` 在发送超时后真正 `ws.close()`,但 `close()` 本身对一个刚刚证明自己会卡住的连接同样可能卡住,而它跑在采集读取路径上 —— 等于把刚堵上的洞从另一个门放回来。所以关闭也必须 `asyncio.wait_for(..., WS_SEND_TIMEOUT_SEC)`。**凡是"因为我们不信任这个连接所以要做的清理",都要问一句:这个清理自己会不会卡?**
33. **串口最后半行必丢(已确认,刻意不修)** —— 读线程的 tail flush 只在 `stop_evt` 置位后执行,而 `_stop_captures` 是同步块里 `evt.set()` 紧接 `ctask.cancel()`,消费端先收到 CancelledError 并关掉文件,那半行就进了无人读的队列。修它要在最取消敏感的路径插等待,低危一行,不值。**意味着:以裸提示符(无换行)结尾的 console,最后一行永远不进文件。**
35. **子进程管道两端必须显式声明同一个编码 —— 且"读循环 + 收尾"不能共用同一个 `try`(v2.7.4,同事机器上必现)** —— `subprocess.Popen(text=True)` 不带 `encoding=` 时用 `locale.getpreferredencoding()`,中文 Windows 上是 **GBK**;而 `server_window.ps1` 给子进程设了 `PYTHONIOENCODING=utf-8`,于是**子进程写 UTF-8、服务端按 GBK 读**。脚本 stdout 里任意一个 GBK 非法字节对(实测 `top -n 1 -b` / `dumpsys window` 那几段最容易带出)就让 `proc.stdout.readline()` 抛 `UnicodeDecodeError`。**真正的灾难在于那个异常逃到了 `_stream_logs` 底部的兜底 `except`** —— 它只广播一条错误就 `return`,**把下面的收尾块整块跳过**(等退出 / `exit_code` / 终态 status / `_stop_captures` / `_archive_task` / `end` 帧)。症状因而极具误导性:脚本其实早跑完了,任务却**永远停在 `running`**、`exit_code=None`、不归档,日志以 task_id 命名孤零零留在 `logs/`。**两条独立教训:**① 凡是 `text=True` 就必须显式 `encoding=`(读 adb 输出的地方同理,`ir_runner.py` 3 处与 `_adb_reconnect_offline`、`/api/adb/reconnect` 已一并修);② **读取循环和收尾写在一个 `try` 里,等于让"读失败"吃掉"终态"** —— 收尾必须无条件执行。现在读取循环自带 `try`(失败 → 记原因 → 广播 → 终止子进程 → 离线程 `wait()`),任务**不可能停在半路**。A/B 实证与构造方法见 CHANGELOG v2.7.4。
36. **串口能力 = 一个 pyserial,缺了它只表现为"COM 下拉框是空的"** —— 平台枚举 COM 口走 `serial.tools.list_ports.comports()`,它读的是 Windows 注册表 `HARDWARE\DEVICEMAP\SERIALCOMM` —— **和设备管理器同源**。所以不需要任何额外工具或驱动:**设备管理器能看到口,就证明驱动已经好了**,剩下的唯一变量是"跑服务端的那个 Python 里有没有 `pyserial`"。三个坑:① `import serial` 在 `_capture_serial` 和 `/api/serial/ports` 里都是**懒加载**的(为了让服务端在没 pyserial 时也能启动),所以**没装 pyserial 服务端照常启动、任务照常跑完,只有采集通道静默变成 `unavailable`**;② `/api/serial/ports` 明明返回了 `{"ports":[],"available":false}`,但前端 `refreshSerialPorts` **只取了 `ports`、把 `available` 丢了** → 用户看到的就是一个空的 `— COM —` 下拉框,**零解释**(2026-09-11 同事机器上实踩);③ 必须用**跑服务端的那个 Python** 装 —— `start.bat` 取的是 PATH 里第一个 `python`,不一定是你在 conda 里装过的那个。诊断一行(以服务端窗口打印的路径为准):`<那个python> -m serial.tools.list_ports -v`。

---

## 6. Recent design decisions (DO NOT REVERT)

按时间倒序,这些是用户已经"批准"的设计选择:

0. **掉线容忍:三条链路自动接回 (v2.7.2)**
   - **背景**:用户明确"perf_monitor 这种,当存在设备重启或者 adb 掉线时是正常的",并要求「设备重启完成后需要能自动连接上之前的日志,且能自动连接上 adb 和串口」。
   - **用户批准的三条硬约束(DO NOT REVERT)**:
     1. **adb 重连只用 `adb reconnect offline`,绝不自动 `kill-server`** —— 原话「重连就只 reconnect 吧,**不能影响其它脚本运行**」。`kill-server` 只属于「重置」按钮。
     2. **不做 monitor 脚本泛化** —— 原话「不需要自动拥有图表,后面我一个一个让你改代码就行了」。继续维护 `PERF_SCRIPT_KINDS`,**不要**去实现"脚本自描述 series"那套。这一条是对"过度设计"的明确否决。
     3. **不新增离线提醒 UI** —— 原话「不需要,直接拿之前的【临时离线就行了】」。「临时离线」合成卡就是唯一信号,别再加横幅/toast/日志行。
   - **探查先于实现**:用户 5 条里有 2 条**已经满足**,不要重复造 —— `perf_monitor.py` 的 `adb_shell()` 从不抛异常、失败返回 `""`,主循环照常产出全 null sample 并继续(这就是用户说的"按周期发请求");任务卡从不会消失。
   - **真正要修的四处**:① 串口从"一次性"改成受监督重连循环(连续失败预算 60、收到数据清零、每次重试换新线程/新 Event、写可见 marker);② 新增 `_device_watchdog` 周期 `adb reconnect offline`;③ "临时离线"合成卡的条件补上 `interrupting`;④ `_broadcast` 发送超时后**真正 `ws.close()`** —— 否则前端 `onclose` 不触发、`scheduleReconnect` 不执行,浏览器握着死 socket 永远收不到日志(这才是"连不上之前的日志"的真身)。
   - **顺带修复**:v2.7.0 删 2s 心跳时连带删掉了唯一刷新 `t["captures"]` 的语句,导致通道状态冻结在任务开始(见踩坑 #32)。
   - **实机已验证**:串口重连循环 + 中断时干净停止 + COM9 句柄不泄漏 + watchdog 判定五情形 + `adb reconnect offline` 不打扰在线设备。**未证**:串口重连的**成功路径**(需真拔插 USB 线)、watchdog 的**真触发**(需设备真掉线)—— 都要动硬件。

1. **任务结束自动存档 (v2.7.0)**
   - **背景(用户原话)**:"我没找到 log 存放在哪里,这些 log 需要做成结束后自动存档,包括 perf monitor 的图表也算,相当于 log 和 report 都需要结束就实时存档;所以导出的按钮可以暂时先隐藏掉了"。**注意:日志一直都在 `logs/` —— 用户找不到是产品缺陷,不是用户的问题**(文件名 32 位 task_id 打头 + 界面无任何入口 + 产物散在三处)。任何"东西在哪"的设计都要把人能不能找到算进成本。
   - **用户批准的六项决策(DO NOT REVERT)**:
     1. **图表 = 浏览器渲染后回传**,服务端**不引入 matplotlib** —— 为一个图养两套渲染不划算。代价是任务结束时浏览器没开就没有 `chart.png`(用户已知悉并接受);`summary.json` 必须如实反映缺图。
     2. **每任务一个文件夹** `archive/<时间>_<脚本>_<设备>/`;`logs/` 降级为运行期临时工作区。
     3. **永久保留,只显示占用** —— 不做任何自动删除。
     4. **UI 入口**:顶栏全局「打开存档」按钮(打开 archive 根)+ 任务卡「存档」按钮(打开那一次运行)。**全局功能放全局位置** —— 存档是整个平台的事,不该和任务卡片的工具栏混在一起(v2.7.1 用户反馈)。
     5. **删任务不动存档** —— `×` / 「清空已完成」只做内存移除(`_forget_task`)。既然承诺"永久保留",一次误点就不该摧毁它。**这是有意的语义变更,不要"修"回连带删除。**
     6. **前端只显示 stdout,永久**(见下方 v2.7.0 第二条)。
   - **目录名不含 task_id** —— `make_archive_name()` 只出 `<时间>_<脚本>_<设备>`,task_id 存进 `summary.json`。这是可读性修复本身。
   - **迁移而非复制**:`_archive_task` 用 `Path.replace()` 把三份日志**搬**出 `logs/`(复制会让永久保留策略下磁盘占用翻倍)。`logs/` 里只留 `server.out.out`/`server.err.log`。
   - **归档必须排在 `_stop_captures` 之后** —— Windows 拒绝移动仍被打开的文件。`_stream_logs` 的 `with open(...)` 在调归档前已退出,`_stop_captures` 关掉了 logcat/serial 句柄,所以顺序是硬约束。移动失败**不得抛出**,记进 `summary.json` 的 `notes`。
   - **归档有四个调用点,不存在单一终点**:`_stream_logs` 尾部(正常/中断/失败都汇聚于此)、`api_force_stop_by_device`(SIGKILL 无 EOF)、`api_force_cleanup`(必须在 `TASKS.clear()` **之前**)、`api_server_shutdown`(`os._exit` 绕过一切)。`_archive_task` 靠 `task["_archived"]` 幂等。
   - **存档按模块分类(v2.7.1)**:`archive/<模块>/<时间>_<脚本>_<设备>/`。模块由 `ARCHIVE_MODULES` **显式映射**得出(不靠文件名猜 —— `wifi_onoff`/`wifi_reboot`/`wifi_switch` 同属 `wifi` 而 `bt_reboot` 不是),未登记的落 `other/`,**存档根目录下永远只有模块文件夹**。模块名与 `reports/stress-test/<模块>/` 对齐。
   - **报告是"搬"不是"复制"(v2.7.3)**:归档成功后删掉 `reports/` 里的原件(先 copy 再 unlink,两步走 —— 删失败也不丢唯一副本),所以 `reports/` 只是几秒钟的暂存区,不会累积、不会出现两处副本不一致。**一个任务可能有多份报告**(`app_launch_stress` 每个 APP 一份),`_find_task_reports()` 返回整组,归档为 `report.json` / `report-2.json` / … —— 只归档 stdout 打印的那份会把其余的永远留在 `reports/`。
   - **报告关联双路径**:优先嗅探 stdout 里那行 `  report          : <绝对路径>`(6 个脚本措辞一致,仅对齐空格不同);硬停时可能没打出来 → 兜底扫 `reports/stress-test/**` 中 mtime 落在任务窗口内且文件名含设备的 JSON。**两条路径都必须校验结果落在 `REPORTS_DIR` 内** —— 路径来自脚本 stdout,是数据不是可信路径。
   - **报告归属必须唯一(v2.7.1,真机踩到)**:兜底 mtime 扫描曾把**前一个任务**的报告算给当前任务 —— 同一台设备背靠背跑,两个任务的时间窗重叠,于是 `ir_runner`(根本不写报告)的存档里出现了 `report.json`。**归属错一份报告比少一份更糟**:它看起来完全可信但内容是别人的。三重收紧:① **只有 `REPORT_WRITING_SCRIPTS` 里的脚本才走扫描**;② mtime 必须 **≥ 任务开始时间**(原来是 `开始-5s`,白放宽了一截);③ `_CLAIMED_REPORTS` 保证**一份报告只被一个任务认领**。
   - **图表回传两处触发点**:WS `end` 帧(正在看该任务时)+ `refreshTasks` 轮询扫描(任务在别的视图下结束时 WS 帧被 `currentTaskId` 过滤掉;以及跑完才打开浏览器时 `perfSamples` 靠 WS 重放重建)。`state.chartPosted` 防重复。
   - **`_log_path` 是唯一的路径权威**:归档后解析到 `archive/<dir>/<member>`,未归档(含 v2.6.0 之前的老任务)回退 `logs/`。所以 `api_task_log`、`ws_logs` 重放、导出全部自动跟随,**没有一处需要各自打补丁**。
   - **删除语义连带变更**:`_delete_task_logs` → `_forget_task`,`api_delete_task` / `api_cleanup_tasks` 不再碰磁盘,两个按钮的 `title` 也改成「从列表移除(存档保留在 archive/)」。
   - **实测修正:`LOGCAT_MAX_BYTES` 256MB → 1GB**。真机 `wifi_reboot_stress` 实测**一次重启(关机+开机风暴)≈1.3MB logcat**(重启前只有 63 行),而三个重启脚本 `iterations` 默认 100 → 单次默认跑 ~128MB,旧上限约 197 轮触顶并静默截断。文档里那句"~0.82 MB/小时"是**空闲设备**量的,差约 95 倍,已改写。
   - **实机已验证**:正常/串口/中断(报告仍归档到)/无报告脚本(`notes` 如实说明)/图表回传/路径安全(非法 artifact 名与 base64 → 400,非法 task_id → 404)/`?source=` 读回/删任务不动存档/未归档回退。**未证**:前端实际渲染效果(无真实浏览器)。

1. **每任务三源实时日志采集:stdout + logcat + 串口 (v2.6.0) —— 显示侧已被 v2.7.0 覆盖**
   - **背景**:用户要求"每一次的脚本执行都能实时监控 logcat 和串口日志,后续增加自动开启和自动保存"。
   - **架构铁律:采集是服务端职责**。脚本拿不到自己的 `task_id` / 日志路径,所以 9 个脚本**零改动**;服务端在 `api_run` 起采集协程,在任务终态收尾。
   - **用户批准的四项决策(DO NOT REVERT)**:
     1. ~~**前端暂时只展示 stdout**,但数据层 / WS 协议 / 渲染路由**全部按通道就位** —— 开 tab 只是 UI 改动(`TABS_ENABLED = false` 是唯一的开关)。~~ **【v2.7.0 已作废】用户改主意了:"直接将 logcat 和串口日志的前端显示方案删掉,建议还是稳定简单为主导"。`CHANNELS` / `TABS_ENABLED` / `state.activeSource` / `state.captureCounts` / 计数 pills / `#channel-bar` / `_capture_counts_ticker` 全部删除。前端只显示 stdout,永久。**
     2. **logcat 恒开**(每个任务都采),**串口仅在任务显式勾选时采**(设备卡 checkbox)。勾选状态**不持久化**(本次会话意图),端口持久化。
     3. **三件一起、分阶段实现**:① 实时采集与推送 ② 自动开启(绑定任务生命周期)③ 自动保存(落盘)。
   - **logcat 采集**:`asyncio.create_subprocess_exec(["adb","-s",S,"logcat","-v","threadtime","-T","1","-b","main","-b","system","-b","crash","*:I"])`。事件循环是 **ProactorEventLoop**(uvicorn 0.51 `use_subprocess=False`),所以子进程不占 executor 线程。**自动重启循环**(最多 5 次、间隔 2s)统一处理 `adb kill-server`(`/api/adb/reconnect`)与设备重启 —— 每次重启写一行可见 marker `--- logcat capture restart #N ---`,这是用户唯一能察觉"中间断过"的途径。`kill()` 后**必须** `await proc.wait()`,否则关 loop 时刷 `__unclosed transport` 噪音。
   - **串口采集**:裸守护线程 + `asyncio.Queue`(见 §5.5.5 #26)。体量上限 `LOGCAT_MAX_BYTES`(默认 256 MB,`PPTP_LOGCAT_MAX_MB` 可调);超限写尾行说明 + `status="capped"` 并退出重启循环,**不轮转**(开头几分钟最有价值,截断重开比停更糟)。
   - **订阅过滤是性能关键**:`WS_SUBS: dict[int, set[str]]`(`id(ws)` → 订阅源),`_broadcast(task_id, msg, source)` 跳过没有订阅者的源 —— 没人看的 logcat **只花一次集合推导,零成本**。`source=None` 保留给控制帧(发给所有人)。**行为变更**:`_broadcast` 新增 **5s 发送超时**(原来无界)—— 卡死的 TCP 连接不再能永久阻塞读端。
   - **串口争用仲裁 —— "脚本优先"**:声明了 `serial_port` 参数的脚本**拥有**该端口(目前只有 `battery_inout_stress.py`)。服务端把它标成 `skipped`/`script_owns_port`(任务照跑、logcat 照采),前端把控件禁用 + 给中文提示。理由:该脚本需要串口做 5 分钟保活,被抢会让**电量曲线变平**——用调试台换电量曲线是坏交易。**这个让位必须在 UI 可见**,否则会被当 bug 报。
   - **日志命名方案(用户要求"含设备/时间/测试项")**:`logs/<task_id>_<yyyyMMdd-HHmmss>_<script-stem>_<device>.<source>`(`.log` / `.logcat.log` / `.serial.log`)。**保留 `task_id` 在最前面**是有意的 —— 删除 glob `f"{task_id}*"` 与 `^[0-9a-f]{32}$` 路径防护无需修改即继续工作。**服务器把确切的文件名放在 `t.log_files` 里下发给前端**,客户端**从不重建路径**(`logFileName()` 只对 v2.6.0 之前的老任务回退到约定名)。没有选"每次运行建子目录",因为 Windows 在捕获文件句柄打开时拒绝 `rmtree`。
   - **`GET /api/tasks/{id}/log?source=` 一律走尾部读**(`_read_tail`,默认 4 MB)—— logcat 上限 256 MB,整文件 `read_text()` 会硬冲内存。`truncated` 必须写进导出文件头,不能静默丢行。
   - **`api_stop`(中断)刻意不杀采集**:停机/收尾日志正是最有价值的部分,由 `_stream_logs` 在真正 EOF 时统一收。代价:忽略 CTRL_BREAK 的脚本会让 logcat 一直跑到硬停。
   - **实机已验证**(B0403374A2A508001F00):可读文件名三源落盘、订阅过滤(仅 stdout 的客户端收到 14 帧 stdout / **0 帧 logcat**,而服务端实采 28 行)、任务终态自动停、中断路径采集活过 `interrupting`、`script_owns_port` 仲裁、删除 glob 覆盖三源、非法 `source` → 400、无孤儿 `adb.exe`。**串口数据通路已于同日的真实 `wifi_reboot_stress` 跑中被证实**:重启时采到 64728 字节 / 854 行真实 kernel console 输出,完整覆盖 `sysrq` 关机 dump → `reboot: Restarting system with command 'shell'` → U-Boot 2019.04 → AVB `Boot state is: Green` → 内核起到 uptime 31.5s。**"空闲时读到 0 行"从来不是故障 —— console 空闲时本来就一个字不吐**。
   - **日志文件命名坑(实测暴露,v2.7.0 已修)**:`<task_id>_<时间>_<脚本>_<设备>` 里 32 位 task_id 打头,人眼无法识别,用户因此**找不到自己的日志**。v2.7.0 的存档目录名去掉了 task_id(见决策 #0)。
   - **`serial_mode: "hold"|"duty"`** 仍是"占空比采样"的预留(未做)。~~tab 切换接缝~~ 已随 v2.7.0 的前端显示方案删除而作废。

1. **重启蓝牙音箱回连压测脚本 bt_reboot_stress (v2.5.2)**
   - **背景**:用户将测试设备连上蓝牙音箱(BOGASING-G4,F4:4E:FD:F1:60:11,DUAL/A2DP;另有一台 RC-50 遥控器是 LE/HID)后,要求仿 wifi_reboot_stress.py 新增"重启蓝牙回连"压测脚本。
   - **脚本** `scripts/bt_reboot_stress.py`(纯 ASCII,仿 wifi_reboot 结构):每轮 `adb reboot` → `wait_sec` 后轮询 `sys.boot_completed` 等设备上线 → **轮询 A2DP 回连**(每 5s 查一次,最多 `bt_reconnect_timeout`,默认 90s)→ PASS/FAIL → 汇总通过率 + 每轮 P/F。PARAMS:`iterations` / `wait_sec` / `bt_reconnect_timeout` / `back_online_timeout`。
   - **回连判定** `check_bt_connected`:`dumpsys bluetooth_manager` 必须**同时满足** ① 适配器已开启(`enabled: true`);② 至少一个 A2DP 状态机处于 `mConnectionState: CONNECTED`。光"蓝牙开着"不算回连(蓝牙开 ≠ 音箱连上);A2dpStateMachine 只在 A2DP 音频设备存在,故该标记 = 音箱真正回连。真机实测正常回连 ~6s。
   - **只测重启回连,不测开关回连(用户明确)**:本设备**蓝牙开关未对用户开放**,用户无法手动关蓝牙 → 不做"disable→enable"回连场景。
   - **真机验证发现的边界行为(重要)**:音箱**刚连上后立即重启** → 回连失败(A2DP CONNECT → 30s CONNECT_TIMEOUT → DISCONNECTED,甚至手动 toggle 蓝牙也连不回,适配器 `ConnectionState: STATE_DISCONNECTED`);用户在蓝牙设置里**重新手动连一次**后重启 → ~6s 自动回连,脚本 PASS。疑似音箱休眠/连接未稳定。**结论:正式压测前先手动确认音箱能连上**(已写入 README 注意事项)。
   - 平台接入:`scripts/*.py` 自动发现,前端零改动;版本号 2.5.1 → 2.5.2(server.py L88 + healthz L341;index.html `?v=` ×3)。

2. **前端 log console 全英文/ASCII + 折线图固定 1h 滚动窗口 (v2.5.1)**
   - **背景(用户 3 点要求,逐字)**:① 前端 log 打印不能存在中文;② 前端折线图的显示范围需要从刻度 2min 拉至 10min,且整体包含约 6 个刻度,也就是实时显示 1h 的曲线;③ 同步一起修改 perf_monitor 的。
   - **log 控制台 ASCII(需求 ①)**:凡打进 `#log-console` 的内容**全英文/ASCII** —— 可读行 `readableBattLine`(`[batt] HH:MM:SS (X.Xs) level=87% temp=36.2C voltage=8.4V status=charging` + 跳变 `[!] jump[type] prev->87%`)、meta 行(`[batt] monitor start | mode=charge | port=COM9 | source=dumpsys battery` / `[perf] monitor start | CPU=...`)、WS 重连/重放/截断提示、占位符(index.html 也换英文)。**删除了 `STATUS_ZH`/`JUMP_TYPE_ZH` 中文映射表**;所有 log 时间戳走 `fmtClock`(`toLocaleTimeString("zh-CN",{hour12:false})`)强制 24h `HH:MM:SS`,杜绝 zh-CN 浏览器吐"下午3:30"。**UI 文案仍中文**(卡片/弹窗/图表系列名/导出表头),只有 console 输出英文(见 5.5.5 #20)
   - **固定 1h 滚动窗口(需求 ②③,perf/battery 共用)**:x 轴 `type:"time"` + `PERF_WINDOW_MS=60*60*1000`(L62)+ `interval:10*60*1000` + `splitNumber:6`,右缘 = 最新样本墙钟(`perfWindowEnd` L783,无样本回退 `perfXBase`),`flushPerfChart` L930 每次 flush 滚窗口 + `shift()` 掉窗口外点;**移除 dataZoom**(与固定窗口互斥,见 5.5.5 #21)。perf_monitor 与 battery 共用同一套 buildPerfOption/flushPerfChart,天然同步生效
   - **全程图 PNG 导出 + 0.5h 刻度(用户追加)**:用户要求"导出的图片需要是全程的,不能只是 1h"(不是实时窗口那 1h,而是从任务开始到结束的**全部样本**);若难做则建议"用 csv 生成图表",采纳 csv 思路但直接在浏览器用离屏 ECharts 渲染,免去再打开 csv 的手动步骤。实现 `exportFullChartDataUrl(tid)` L968:`buildPerfOption(tid, true)` fullRange 分支 min/max 取全量 `perfSamples`,`xInterval=30*60*1000`(**0.5h 一个刻度**,用户追加要求),离屏 1280×360 + pixelRatio 2 → 不依赖当前图表是否在绑定(切走后照样能导出全程)。live 窗口(1h/10min)只作用于屏幕,**导出与屏幕解耦**
   - **放电测试卡片持久化(前端追加)**:放电模式以"设备关机"结束(`stop_reason:"power_off"`),任务结束后设备不在 `adb devices`,旧逻辑会让设备卡直接消失 → 仿"临时离线"卡,在 `renderDevices()` L322 对"终态 battery 任务 + 设备不在 adb + 该任务仍是设备最新任务"合成 `_batt_off` 卡(虚线红边、"设备已关机"状态 + "放电关机"徽标,CSS `device-card-batt-off` L290);设备重新上线自动回正常卡,删任务即删卡(见 5.5.3)
   - **范围界定**:本次只动前端 app.js + style.css + index.html 占位符 + 版本号;脚本(perf_monitor.py / battery_inout_stress.py)**零改动**(其 stdout 本就 ASCII)
   - 版本号 2.5.0 → 2.5.1(server.py L88 + healthz L341;index.html `?v=` ×3)

3. **电池充放电压测脚本 battery_inout_stress + 前端电量/温度曲线 (v2.5.0)**
   - **背景**:用户新增电池充/放电压测需求,原脚本是独立形态(中文、matplotlib、Excel、`input()` 交互),不符合平台契约 → 重写为平台脚本。
   - **语言分工(用户确认)**:脚本 **100% 英文/ASCII(代码文件无中文,中文只进 md)**;前端新增文案用中文。副作用:脚本的 `PARAMS` 字段名/`label` 是英文 → 前端参数弹窗显示英文标签,属预期(见 5.5.5 #19)
   - **`PARAMS`**:`mode`(select `charge`/`discharge`,**单次只跑充电或放电**——充放电做不到自动插拔,由用户选)、`serial_port`(select,`--dump-params` 时用 `serial.tools.list_ports` 枚举当前 COM 口;**必须由用户在试跑时确认并自选**)、`interval_sec`(默认10)、`temp_warn_c`(默认45,温控告警阈值)、`full_hold_sec`(默认60,满电稳定窗口)
   - **数据源(真机 2026-08-25 探明)**:外部 BMS MCU;`adb shell dumpsys battery` **默认只缓存**,必须经**串口 console**(115200 baud)发 `cmd android.hardware.health.IHealth/default set polling true` 才实时;SELinux 挡 adb 路径(`su 0` 报 `Failed transaction 2147483646`),**串口是唯一实时数据通道**;轮询需每 `SERIAL_RE_ENABLE_MIN` 分钟重发保活。COM9 = FTDI(VID:PID 0403:6010)
   - **PERF 线**(复用 perf_monitor 契约,`ensure_ascii=True`):meta `{"type":"meta","sources":{level/temp/voltage_mv/status/com_port/mode},"device":...}`;sample `{"type":"sample","clock":<ms>,"t":...,"level":...,"temp":...,"voltage_mv":...,"status":...,"jump":...,"jump_type":...}`;**必须带 `type:"sample"` + `clock`**(见 5.5.5 #17)
   - **单次中止信号(用户指定)**:① charge 模式 `level>=100` 且 `status=="full"` 或连续满 `full_hold_sec` → `stop_reason="full"`;② 任意模式连续 2 次电池读取失败(设备关机/adb 失联)→ `stop_reason="power_off"`;③ 手动 SIGBREAK
   - **错误检测保留(原脚本逻辑)**:电量跳变检测(方向感知 `abnormal_rise`/`abnormal_drop`/`abnormal_jump`,`JUMP_THRESHOLD=2`)+ 温控告警(`temp >= temp_warn_c`);**不输出 Excel**(前端三件套导出代替)
   - **前端(perfKind 泛化)**:`PERF_SCRIPT_KINDS` 注册 `battery_inout_stress.py`→`battery`;battery 类曲线 = **电量(蓝,左轴 0-100%)+ 温度(红,右轴 °C)两线**;可读行 v2.5.0 先做中文 `[batt] ... 电量=87% 温度=36.2°C 电压=8.4V 状态=充电中`,**v2.5.1 按 ASCII 规则改成英文**(`level=87% temp=36.2C voltage=8.4V status=charging`,见 §6 决策 #2);CSV 表头 `t_sec,t_wall,level_percent,temp_c,voltage_mv,status,jump_delta,jump_type`,导出后缀 `.batt.csv`;导出三件套与 perf 一致(txt+csv+png)
   - 报告落盘 `reports/stress-test/battery/battery_<dev_short>_<ts>.json`(已 gitignore);`--probe` 探测:设备可达 + dumpsys battery 解析 + 串口枚举

4. **性能监控脚本 perf_monitor + 前端实时图表 (v2.4.0 → v2.4.1 修复 → v2.4.2 长时检测)**
   - **⚠ v2.4.1 根因修复(用户 3 症状齐发)**:图表实时不出线 + 横轴无时间点 + 导出只剩 txt —— 全部源自脚本 sample 行的 `emit` dict **漏了 `"type":"sample"` 字段**(docstring 写了 `type:sample`,代码没实现)。前端 `handlePerfLine` 以 `obj.type === "sample"` 分发,sample 被当未知类型丢弃 → `perfPending` 永远空 → 图表无数据 → `perfSamples[taskId]` 空 → 导出只剩 txt。**修复 = emit 补 `type:"sample"` + 新增 `clock`(wall-clock epoch ms);前端分发逻辑一行没改**。教训见 5.5.5 #17
   - **脚本** `scripts/perf_monitor.py`:平台契约全套;`PARAMS` = `interval_sec`(默认2.0,min0.5)/ `duration_sec`(0=手动停止)/ `track_foreground`(默认 true);`--probe` 独立探测节点(不入 PARAMS)
   - **PERF 采样协议(v2.4.1 起)**:脚本每采样打一行 `PERF|{"type":"sample","clock":<epoch_ms>,"t":...,"cpu":...,"gpu":...,"mem":...,"fg_cpu":...,"fg_pkg":...,"gpu_clk":...}`,启动打 `PERF|{"type":"meta","sources":{...}}`(`json.dumps ensure_ascii=True`)。stdout 经现有 WS `/ws/logs/{task_id}` 原样流转,**服务端流式零改动**;前端在 `appendLogLine` 拦截 `PERF|` 前缀。**`type` 与 `clock` 是前端分发的硬依赖字段,缺一即断链**
   - **MT9676 数据源(真机 2026-08-25 探明,注意:用户以为 MT9660,实为 MT9676 —— `ro.soc.model=MT9676 / ro.hardware=mt5896 / egl=mali.mt5873`)**:
     - CPU% `/proc/stat` 差值(免 root);mem% `/proc/meminfo` MemAvailable/MemTotal(系统整体,免 root,本机仅 ~1.75GB)
     - GPU% `/sys/kernel/debug/mali0/dvfs_utilization` `busy_time/idle_time` 差值(**需 root,`su 0 <cmd>`**;标准 Mali/MTK 路径本机全不存在)
     - 前台 APP CPU%: `dumpsys window|grep mFocusedApp` 取包名 → `top -n 1 -b | grep <pkg> | head -3` 解析列索引 8 = %CPU(**不用 `dumpsys cpuinfo`**,那是近 5 分钟滚动均值)
     - 每采样**一次复合 `adb shell`**(`@@STAT/@@MEM/@@GPU/@@CLK/@@FOCUS/@@TOP` 分段标记);**必须 list-arg subprocess**(shell=True 会让 `;`/`|`/`>` 被 Windows cmd.exe 吃掉)
     - **视频播放时 GPU%≈0 是正常的**(MStar 显示管线:解码走 VPU、合成走 HWC 硬件叠加,GPU 只画 UI)—— 实测证据链见 5.5.5 #14
   - **前端图表**:ECharts **5.5.1 本地 vendor**(`static/vendor/echarts.min.js`,~1MB);`#perf-chart` 挂在 `#log-info`(日志面板头部)与 `.panel-body` 之间,200px 深色底;绑定 `state.currentTaskId`
   - **不残留核心机制**:`syncPerfChart()`(dispose+隐藏/重建)+ WS 帧过滤(`state.currentTaskId !== ws._taskId` 丢帧)+ 每任务独立缓冲 `state.perfSamples[taskId]`(cap 86400,v2.4.2 从 3600 上调,24h@2s 全量保留;按 `t` 去重防重放重复)+ WS onopen 重放重建。**图表逻辑只在离散导航点调用(task view / ws.onopen / delete / cleanup / reset),绝不在 2s renderTasks 轮询里**
   - **series 显隐按 meta.sources**:`src.gpu` 真值才画 GPU 绿线;`src.fg` 真值才画"前台APP"红线(脚本 meta 不带 track_fg 字段,前端用 sources.fg 判断);CPU 蓝 / MEM 黄恒有;**X 轴 `type:"time"`(v2.4.1 起)**:数据点用样本 `clock`(epoch ms),标签 HH:MM:SS(local time)+ `hideOverlap` 稀疏;**v2.4.2 加 `splitNumber:6`**、**v2.5.1 起改为固定 1h 滚动窗口**(`interval:10*60*1000`,~6 刻度,右缘=最新样本;详见 §6 决策 #2)——旧日志无 `clock` 回退 `perfXBase = started_at + t*1000`;工具提示显示 `HH:MM:SS · t=X.Xs`;**双 Y 轴(v2.4.1 起)**:主轴 0-100% 给 CPU/GPU/MEM,右侧第二轴 0-400% 给前台APP(`yAxisIndex:1`,`top` 多核 %CPU 可超 100,实测 116-123%);lttb 降采样(v2.5.1 起**移除 dataZoom**,见 5.5.5 #21)
   - **一键导出三件**(无 ZIP,用户指定):log.txt(原始 PERF 行)+ perf.csv(表头 `t_sec,t_wall,cpu_percent,gpu_percent,mem_percent,fg_cpu_percent,fg_pkg,gpu_clk_mhz`,v2.4.1 加 `t_wall` 墙钟列 HH:MM:SS)+ chart.png(深色 2x),`onExportLog` 里 txt 下载后 `sleep(300)` 依次触发;**Chrome/Edge 允许单次点击多下载,Firefox 可能拦截后两个**(文档注明);无 perf 数据只出 txt。**v2.4.2 起 PNG 固定 1280×360 归一化导出**(resize→getDataURL→resize 回原尺寸,同步无闪烁;2x 后 2560×720),长时检测图片也不"长"
   - 报告落盘 `reports/stress-test/perf/perf_<dev_short>_<ts>.json`(已 gitignore);GPU 节点不可读 → meta 标 N/A,图表自动隐藏 GPU 系列

5. **APP 冷热启动压测脚本 app_launch_stress (v2.3.0)**
   - 测量目标 APP 冷/热启动耗时;`mode` 下拉框 `cold`/`hot` **分开跑**;冷启动每轮先 `am force-stop` + `pidof` 确认后台进程已死再测;热启动开始前确保进程存活(死了先冷拉起一次不计样本),每轮 HOME 退后台再拉起
   - **双指标交叉验证**:主指标 = `am start -W` 的 `TotalTime`(ms);清空抓 logcat `Displayed` 比对(冷启动必有,热启动通常缺失→缺失时以 `LaunchState` 语义校验为准);`LaunchState`(Android 12+)与所选模式一致性校验,冷启动拿到 HOT 标异常轮
   - **被测 APP 硬编码脚本顶部 `APP_PRESETS` 列表**(按用户决策不做成前端参数):**"要跑就三个一起跑"**,内置 Netflix / Prime Video / YouTube TV 三个(包名 + Activity 来自用户真机 `dumpsys window` 抓取),按顺序每个各跑 `iterations` 轮,每个 APP 一份报告 + 各自 p95 判定,**整体全过才 PASS**;中途某 APP 无法解析启动 Activity 也判 FAIL
   - **真机验证踩过的坑(改脚本务必牢记)**:
     - `am start -n 包名/.Activity` 的 `.` 简写只在 Activity 属于该包名命名空间时成立。Prime Video 主 Activity 是 `com.amazon.ignition.IgnitionActivity`(别家包名),YouTube 的 `MainActivity` **未导出**、可启动入口是 **`ShellActivity`**——所以 `APP_PRESETS.activity` 一律存**全限定类名**(不带前导点),脚本拼 `pkg/full.Class`
     - logcat `Displayed` 有 `+592ms` 和 `+1s291ms` 两种格式,解析要分态换算(曾把 `+592ms` 误读成 592 秒)
     - `LaunchState` 语义校验为**硬校验**:与模式矛盾该轮判 NG 不计样本;`TotalTime: 0`(intent 投给已置顶实例)也判 NG;系统不报状态(`UNKNOWN`/空)时回退进程级校验(force-stop / pidof)
   - 可配参数:`mode`(select)、`iterations`(**每 APP** 循环次数)、`settle_sec`、`gap_sec`、`launch_timeout`、`p95_threshold_ms`(0=不判定);报告落盘 `reports/stress-test/app-launch/app_launch_<mode>_<label>_<dev>_<ts>.json`(已 gitignore)

6. **参数 type 新增 select 下拉框 + 传感器压测脚本接入 (v2.3.0)**
   - `PARAMS` 字段 `type` 新增 `select`:配 `choices` 列表(字符串或 `{value,label}` 对象),前端渲染 `<select>` 下拉框;`saveParams` 用 `#modal-params [name=...]` 选择器同时匹配 input/select
   - sensor_reboot_stress.py 从 unittest 重写为平台契约(单文件 `main()`):`--device` + `--params` + `--dump-params` + SIGBREAK→KeyboardInterrupt + ASCII stdout
   - 可配参数仅 `sensor`(select 下拉框选 gsensor/tof)、`iterations`、`reboot_timeout`(上线超时);`SENSOR_WINDOW` / `POLL_COUNT` / `POLL_INTERVAL` / `PASS_THRESHOLD` 为**硬编码常量**(按用户要求,改脚本顶部)
   - 传感器读取保留原 su 适配链:本设备"裸 su"(`su -c` 报 `invalid uid/gid -c`),依次尝试直读 / su -c / su 0 / **stdin 交互式 su** / PTY 兜底;运行日志打印实际生效方式
   - 报告落盘 `reports/stress-test/sensor/reboot_<sensor>_<dev>_<ts>.json`(已 gitignore)

7. **脚本参数:自描述 schema,前端零硬编码 (v2.2.0)**
   - 脚本定义模块级 `PARAMS` 列表 + 支持 `--dump-params`(打印 `{"fields":[...]}`,ASCII 安全)
   - 后端 `GET /api/scripts/{name}/params` 跑 `python -u script.py --dump-params`,按 `(name, mtime)` 缓存(`_PARAMS_CACHE`)
   - `_list_scripts()` 源文件嗅探 `--dump-params` 标 `has_params`(纯文件读取,不启子进程)
   - 前端看到 `has_params` 就在脚本卡上支持点击弹窗,按 `type`(`int`/`float`/`bool`/`str`/`select`)渲染表单
   - 新增脚本只需定义 `PARAMS`,平台/后端/前端全都不用改 —— 与 ir_runner 序列选择的"自描述"思路一致

8. **脚本参数按 设备×脚本 独立持久化 (v2.2.0)**
   - `state.deviceParams: { [serial]: { [script]: {param: value} } }`,严格独立、互不干扰(用户明确要求"不能存在记忆或干扰")
   - 存入 localStorage(`pptp.deviceState.v1`),跨刷新保留
   - 跑任务时后端把配置值合并进 `--params` JSON 传给脚本
   - 配置入口做成和 ir_runner 一样的"点击卡片 → 弹窗"(用户明确要求)

9. **WIFI_NETWORKS 硬编码、不做成 param (v2.2.0)**
   - wifi_switch_stress 的预置网络列表是脚本文件里的常量,按用户要求**不做成前端参数**
   - 要换网络直接改脚本顶部 `WIFI_NETWORKS`
   - `use_su` 保持为参数(部分设备免 root,可关掉走 `cmd wifi` 直接调)

10. **Per-device sequence binding (each device independent)**
   - `state.deviceSequences: { [serial]: filename }` — A 选 default, B 选 aging 互不影响
   - 切设备时脚本卡实时刷新
   - 选中设备的 ir_runner 卡显示该设备的序列名
   - 禁止全局 "pinned script" 这种共享模式

11. **Sequence files instead of in-memory editor**
   - 用户改 ini 文件直接编辑
   - modal 只做选择 / 创建空模板,**不做 step 编辑**
   - 之前做过 step-by-step 编辑(代码/kind/delay/count 行),已废弃

12. **Run button on script card (not device card)**
   - ir_runner 脚本卡右侧"▶ 跑"按钮
   - 设备卡只显示状态(chip/运行中/离线)
   - 按钮在脚本卡因为 runnability 取决于 "设备 + 脚本 + 序列" 三态

13. **Script dropdown gating**
   - 设备未选中 → dropdown 禁用 + 提示"先点击选中"
   - 跑任务中 → 禁用(脚本锁定)
   - 离线 → 禁用
   - 选中 + 已选脚本 + 有序列 → 启用

14. **Visual badges**
   - 设备卡 active: 蓝色边框 + 右上 "✓ 已选中" 蓝色 pill(角标,完全在卡内)
   - 设备卡 active+running: 右上 "⚡ 运行中" 绿色 pill
   - 脚本卡 active: 置顶 + 边框 + 左上 "▸ 当前选中" 蓝色 pill(完全在卡内)
   - "跑 X" 旧 badge 已删除(避免重复)

15. **localStorage 刷新保留状态**
    - 已选设备 / 已选脚本 / 已选序列 / 已选参数跨刷新保留
    - 服务器数据(任务/devices)不持久化,总是从 server 拉

16. **RAF 批量推送日志(避免"刷")**
    - 客户端批量 buffer,RAF 一次性 textContent 更新
    - DOM 1000 行上限(类似 VSCode 终端 scrollback)
    - 10000 行上限的服务端 replay(防止异常巨大 log)

17. **modal 标题动态化**
    - 文件模式:`${seqFilename}.ini`
    - 设备模式:`设备 ${serial} 的序列`

18. **Cache busting + NoCache middleware**
    - `index.html` script/css link 带 `?v=2.5.1`
    - server 加 `NoCacheMiddleware`,静态文件 `Cache-Control: no-store`

> 完整前端交互设计沉淀(为什么这样做、踩过什么坑、还有什么没定)见 [FRONTEND_UX.md](FRONTEND_UX.md)。

---

## 7. What does NOT exist (防幻觉清单)

**绝对不要** 假定这些存在 — 如果用户提到这些,先 grep + Read 再决定:

- ❌ 数据库(SQLite/PostgreSQL) — 全是内存 + 文件
- ❌ 用户认证 / 登录
- ❌ 多用户/多租户
- ❌ WebSocket 自动重连(除 ir_runner 任务外的纯客户端流;log WS 有手动重连)
- ❌ 任务调度(只手动触发,无 cron)
- ❌ 报告/统计/历史趋势分析
- ❌ 邮件/通知
- ❌ 远程控制(全本地)
- ❌ 任何 JavaScript 框架(React/Vue/...)或构建工具
- ❌ 任何 npm 依赖
- ❌ Tailwind / 任何 UI 框架
- ❌ Docker / 容器化
- ❌ HTTPS(纯 HTTP)
- ❌ 任何 /api/ir/keys 端点(原 ir_runner keys 已被移除)
- ❌ modal 内的 step 编辑(code/kind/delay/count 行)— 之前做过, 已废弃
- ❌ 任何 "在设备上运行" 多设备同时启动 UI 流程
- ❌ "上次已保存的序列"自动 fallback 选择

---

## 8. If you're given a task — SOP

1. **不要直接写代码**。先:
   - Read 标注的源文件 + grep 相关函数名
   - 如果涉及 IR 序列,看 `scripts/ir_runner.py`(自包含 IRRemote,无外部依赖)+ `ir_sequences/1.ini`
   - 如果涉及 WiFi 脚本,看 `scripts/wifi_*.py`(共用契约:`--device` + `--params` + `--dump-params` + `su 0` 调 adb)
   - 如果涉及传感器脚本,看 `scripts/sensor_reboot_stress.py`(PARAMS 只留 `sensor`/`iterations`/`reboot_timeout`;读取走"裸 su"交互式适配链)
   - 如果涉及 APP 冷热启动脚本,看 `scripts/app_launch_stress.py`(`APP_PRESETS` 列表硬编码顶部,内置 Netflix/Prime Video/YouTube TV 三个一起跑;PARAMS 含 `mode` select 下拉框 + p95 阈值;冷启动 force-stop 校验 + 热启动 HOME 流程;`am start -W` TotalTime 主指标 + logcat Displayed 交叉验证;每 APP 一份报告,整体全过才 PASS)
   - 如果涉及性能监控脚本,看 `scripts/perf_monitor.py`(PARAMS: interval_sec/duration_sec/track_foreground;`PERF|` 采样协议 meta+sample;数据源见 §6 设计决策第 4 条;`--probe` 探测节点) + **§5.5.4/§5.5.5**(前端图表绑定与踩坑)
   - 如果涉及电池充放电压测,看 `scripts/battery_inout_stress.py`(全英文 ASCII;PARAMS: mode/serial_port/interval_sec/temp_warn_c/full_hold_sec;串口开 health 轮询 + 每 5 分钟保活;dumpsys battery 解析;单次中止 = 100% 稳定 或 关机;跳变/温控检测;数据源见 §6 设计决策第 3 条 + 踩坑 #18)
2. **列影响面**:
   - 后端 → server.py 哪个 route / 函数
   - 前端 → app.js 哪些函数 / state 哪些字段
   - 状态持久化 → 是否需要更新 localStorage
3. **不破坏的设计决策** (见 §6)。如果用户的请求跟它们冲突,先提出矛盾再写代码。
4. **写代码 + 验证**:
   - 后端改完 → `taskkill /F /PID <uvicorn-pid>` 强杀,再 `python -m uvicorn server:app --host 127.0.0.1 --port 8000` 重启
   - 前端改完 → 浏览器 **Ctrl+F5** 硬刷(必须 F5 不够,因为有 ?v=)
   - 端到端 curl 测试:`curl http://127.0.0.1:8000/api/...`
5. **完成后**:
   - 报改了哪些文件 + 行号
   - 列验证步骤结果
   - 如有 UI 改动,告诉用户"硬刷 Ctrl+F5"

---

## 9. Verify before done

最低验证清单(任何功能改动):

- [ ] 后端:`/healthz` 200; 修改的 route 用 curl 测过
- [ ] 前端:浏览器 Ctrl+F5 后,无 JS 报错(F12 console)
- [ ] state 持久化(如有):改 → F5 → 应保持
- [ ] 没有"上一版的旧实现"残留在文件中(grep 一下"// TODO"、"// 废弃"等注释)
- [ ] 没有改到 §7 列的"不存在的特性"

---

## 10. Quick reference — 关键函数名 + 行号

> 行号以 **v2.6.0(2026-09-11)** 为准;每次编辑源码后本表即失准 —— **先 grep 函数名再跳**。

**Backend (server.py,2528 行)**:
- `NoCacheMiddleware` L41 — `Cache-Control: no-store` for static
- `ARCHIVE_DIR` L65 / `ARCHIVE_MEMBER` L100 / `ARCHIVE_MODULES` L110(脚本→模块显式表)/ `REPORT_WRITING_SCRIPTS` L130 / `_CLAIMED_REPORTS` L143
- 常量:`LOGCAT_*` L150-163 / `DEVICE_WATCH_INTERVAL_SEC` + `ADB_RECONNECT_MIN_INTERVAL_SEC` L176-178 / `SERIAL_OPEN_TIMEOUT`+`SERIAL_MAX_RESTARTS`+`SERIAL_RECONNECT_DELAY_SEC` L180-185
- `make_log_stem()` L213 / **`script_module()` L229** / `make_archive_name()` L234
- **`_log_path()` L245** — 唯一路径权威,归档感知;`_archive_dir()` L260 — 含**扁平布局回退**
- `_reap_orphan_scripts()` L312 / `_reap_orphan_logcat()` L413 附近 — startup 孤儿回收
- **设备看门狗(v2.7.2)**:`_serial_is_offline()` L466 / `_adb_reconnect_offline()` L474 / `_start_device_watchdog()` L491 / **`_device_watchdog()` L495**
- `_public_captures()` L628(公开快照,**刻意不含 lines/bytes**)
- `_ws_sources()` L675 / **`_announce_captures()` L679**(状态迁移时刷新快照+广播,**只在进入 reconnecting 那一刻发**)/ `_broadcast()` L696(发送超时后**有界地** close socket,见下)+ 5s 超时
- `_emit()` L737 — 落盘 + 计数 + 广播
- `_stream_logs()` L756 — 主协程;读 stdout + 推 WS + 嗅探报告路径 + 终态收尾(停采集 → 归档 → `end` 帧 → 归档摘要行);**读取循环自带 `try`** —— 读失败只记原因并终止子进程,收尾块**无条件执行**(v2.7.4,见踩坑 #35)
- `_capture_logcat()` L814 — logcat 采集 + 自动重启循环(首次 `-T 1` / 重启 `-T 2000`;**连续**失败预算,收到行即清零)
- **`_capture_serial()` L961** — 串口**受监督重连循环**(连续失败预算 60、收到数据清零、每次换新线程/新 Event);**`_serial_attempt()` L1079** — 单次开-读周期
- `_stop_captures(task_id, reason)` L1198 — **幂等**收尾(四处调用);`keep_status` 分支 = 不覆盖自己已终止的通道状态
- `_archive_task(task_id, reason)` L1278 — **归档引擎**,幂等,同步,绝不抛
- `_find_task_report()` L1447 — 三重收紧(只扫登记脚本 / mtime ≥ 开始 / 认领唯一 + REPORTS_DIR 包含性校验)
- `api_run()` L1822 — 409 校验 + `--params` 透传 + 启动三源采集 + 串口仲裁
- `api_stop()` L1936(刻意不停采集)
- `_archive_dir_for()` L2103 / `api_archive_artifact()` L2124 / `api_archive_reveal()` L2175 / `api_archive_reveal_root()` L2198 / `api_archive_stats()` L2214(含分模块统计)
- `_forget_task()` L2261 — **只移除内存条目,刻意不动磁盘**
- `ws_logs()` L2367 — 订阅 + 分源重放(**前端只订阅 stdout**)
- ⚠ **行号会漂**,以搜索为准。另:本机有 E-SafeNet 透明加密,曾把 server.py 包成密文,导致 Read/Grep 读到乱码(Edit 若照写会毁文件)。用户 2026-09-11 已解密,但若**再遇到某文件搜不到/读出来是乱码**,立刻改用 Python 脚本读写+断言,别 Edit。详见 [TODO.md](../TODO.md) 第 5 节。

**Frontend (static/app.js,2317 行)**:
- `state` L14(含 `perfMeta`/`perfSamples`/`deviceSerialCapture`/`deviceSerialPort`/`serialPorts`/**`archiveStats`/`chartPosted`**)。**没有** `activeSource`/`captureCounts`/`CHANNELS`/`TABS_ENABLED`(v2.7.0 删)
- `CAPTURE_EXPORT`(L90 附近)= `["logcat","serial"]` — 只被隐藏的导出路径用;`PERF_PREFIX` / `PERF_SAMPLE_CAP` / `PERF_WINDOW_MS`;`PERF_SCRIPT_KINDS`(perf/battery)
- `devicesRenderKey()`(**含 `serialCapture`/`serialPort`/`serialPorts`**)/ `tasksRenderKey()`(纳入整个 tasks 数组,**所以 `t.archive` 进键是安全的** —— 它只在任务结束时写一次,是离散迁移;但任何每 2s 变的值都不能进)
- `renderScripts()` L182 — 4 状态(active/disabled),ir_runner 卡有 Run 按钮,has_params 卡可点开参数弹窗
- `renderDevices()` L348 — 设备卡 active 角标 / 脚本 chip / `.device-row4` 串口勾选 + COM 下拉;合成"临时离线"卡 + "放电关机"卡
- `renderTasks()` L580 — **任务卡:`存档` 按钮(仅 `t.archive` 存在时)+ `×`**;`setConsoleTarget()` L635
- `renderLogInfo()` L641 — 每 2s 整体重写,现在只有设备/脚本/状态/耗时/行数(计数 pills 已删)
- `appendLogLine(line)` L691(**单参数,只渲染 stdout**)→ `flushLogBuffer()` L703 → `logFileName()` L737(归档感知)/ `clearConsole()` L745
- **PERF 图表**:`exportFullChartDataUrl()` L1079(全程 PNG,也被图表回传复用)/ `buildPerfCsv()` L1115 / `downloadBlob()` L1146
- `onExportLog()` L1158(**已隐藏,代码保留**;多源循环走 `CAPTURE_EXPORT`)/ `exportBaseName()` L1267 / `capturesSummary()` L1274
- **存档(v2.7.0)**:`archiveChartIfAvailable(tid)` L1297(渲染 + POST,`chartPosted` 抢先占位防重复)/ `onRevealArchive()` L1325 / `onRevealArchiveRoot()` L1334 / `refreshArchiveStats()` L1344 / `fmtBytes()` L1358
- `viewTaskLogs()` L1367(幂等保护);`openWs(taskId)` L1398(**只订阅 stdout**,onopen 陈旧防护);`scheduleReconnect()` L1478 附近
- `onRunClick()` L1497(发送 `serial_capture`/`serial_port`);`onDeleteTask()` L1602;`onCleanupTasks()` L1628
- `refreshSerialPorts()` L1662 / `refreshDevices()` L1675 / `refreshScripts()` L1725 / `refreshTasks()` L1763(**末尾有图表回传扫描**)/ `tickClock()` L1804 / `updateLiveDurations()` L1811 / `refreshServerStatus()` L1837(顺带刷存档占用)
- `bindEvents()` L1907(含 `btn-open-archive`);`onForceStopCurrent()` L2180;`onResetClick()` L2194
- `init()` L2285;`STORAGE_KEY` 在其上方
- 参数模态:`loadParamsSchema()` L1801 / `paramInputHtml()` L1812(select 下拉框在这)/ `openParamsModal()` L1836 / `saveParams()` L1856
- 序列模态:`openSeqModal()` L1689 / `renderSeqList()` L1720 / `selectSequence()` L1776

**Frontend (static/index.html)**:
- 4 个 `.panel`(`#panel-devices/scripts/logs/tasks`)
- `#log-info` L58、**`#perf-chart` L61(200px 性能图表,默认 hidden)**、`.panel-body > #log-console` L63
- `<head>` 内 `<script src="/static/vendor/echarts.min.js?v=2.5.1"></script>` L8(在 style.css 后)
- `#modal-seq` (line 91) sequence picker;`#modal-params` (line 124) 脚本参数配置
- 顶部 `.topbar` 含 "重置" 和 "关机" 按钮 + 服务信息
- `<script src="/static/app.js?v=2.6.0">` L86 — 版本号防缓存(全站 `?v=` 共 3 处:L7 / L8 / L86)
- 顶栏 `.status` 里有 **(v2.7.1)`:存档占用徽标 `#archive-hint` + 全局「打开存档」`#btn-open-archive`**,与 重置 / 关机 并列

**CSS 章节** (static/style.css,834 行) — 行号以 v2.6.0 为准:
- 顶部 1-105 行:基础元素(panel/btn/list)
- L112-203: `.script-card` 4 状态
- L224-300: `.device-card` 各状态(含 `device-card-temp-offline` L276 琥珀虚线 + `device-card-batt-off` L290 红色虚线)
- **L322-366: `.device-row4` + `.serial-opt` + `.serial-port`(v2.6.0 串口勾选行)**
- L368-406: `.script-select` 自定义箭头(与 `.serial-port` 同款 chevron 内联 SVG)
- L475-518: `.task-card` + `×` 删除按钮
- L521-543: `.log-info` 条;**L546-561: `.cap-counts` / `.cap-item` / `.cap-dot.cap-ok|cap-err|cap-off`(v2.6.0 计数 pills)**;**L565-587: `.channel-bar` / `.channel-tab`(tab 预留)**
- L601-608: `.console` 字体 + 自适应
- L611-800 附近: `.modal` + `.seq-list` + `.seq-target`
- L828: **`.perf-chart`(200px 深色底)**;L834: `[hidden]{display:none !important}`(全局兜底)

---

## 11. Common user requests 分类

按用户曾经提过的问题模式(下次遇到类似的可快速定位):

- **"X 不显示/位置不对"** → CSS 章节 → 检查 overflow/z-index/position
- **"切换设备 Y 不更新"** → `state.selectedDeviceSerial` + `renderScripts()` + `onSelectDevice()`
- **"刷新页面 Z 丢了"** → localStorage 持久化(检查 `STORAGE_KEY` 和 save/load 点)
- **"ir_runner 行为不对"** → `scripts/ir_runner.py` 的 `argparse` 和 infinite loop 逻辑
- **"新增设备类型/操作类型"** → 后端 `api_run` 校验 / 前端 dropdown 配置
- **"样式丑了"** → CSS 章节找对应 class(`.device-card / .script-card / .btn / .modal`)
- **"加个新功能"** → 找最近的相似功能(如 modal、新端点、按钮)作模板

---

## 12. Notes on user's style preferences

- **极简主义** — 用户反复删多余 UI 元素(如"见 ir_runner 卡片"提示、跑 X 重复 badge)
- **不预设复杂架构** — 删过 plugin 系统、Vue 框架、SQLite 集成等
- **现代简洁的样式** — 喜欢渐变 + 圆角 + 微动画 + 视觉层次
- **喜欢 Ctrl+F5 测试** — 接受"硬刷"作为正常流程
- **不需要"上一版兼容"** — 删旧代码毫不犹豫(常配合 `git log` 找回)

---

## 13. Critical anti-patterns to avoid

- ❌ **不要** 修改 `state` 字段名(下游所有 `state.X` 引用都会断)
- ❌ **不要** 添加 console.log 调试(用户看不到,但有 `console.warn` 用于关键提示)
- ❌ **不要** 在 modal 里加 step 编辑 UI(已废弃)
- ❌ **不要** 在前端用 ES modules / 框架(保持 vanilla JS 单文件)
- ❌ **不要** 用 SQLite 替代内存(全平台靠内存 + 文件 + localStorage)
- ❌ **不要** 创建 README 大改 / 文档大改除非用户要求
- ❌ **不要** 假设 Python 虚拟环境(用 system conda env `D:\Conda_Environments\dev_env\python.exe`)
- ❌ **不要** 在 dev 时关掉 `STORAGE_KEY` 持久化

**前端交互模型禁区(改前先读 §5.5)**:
- ❌ **不要** 把副作用塞进 2s/3s 轮询(`refreshTasks`/`refreshDevices`)—— 那些回调只该重建卡片 DOM;日志/图表/WS 都只在离散导航点操作
- ❌ **不要** 绕过 WS 帧过滤新增 onmessage 逻辑(必须 `state.currentTaskId !== ws._taskId` 先 return)—— 这是"切设备旧日志不出现"的总闸
- ❌ **不要** 假设有 per-card / per-device 独立终端 —— 平台只有**一个**全局 `#log-console` / `#perf-chart`,绑定 `currentTaskId`
- ❌ **不要** 手动改 `el.innerHTML` 塞 `#perf-chart` 进卡片 —— 图表 div 是 index.html 静态的,只随 currentTaskId 显隐
- ❌ **不要** 在 hidden 元素上 `echarts.init`(零尺寸白板);也不要只 cap `perfSamples` 不 cap option series data(内存泄漏)
- ❌ **不要** 用 `dumpsys cpuinfo`(5 分钟滚动均值)或 `top | head`(截断低 CPU 前台 APP)做实时数据
- ❌ **不要** 用 `shell=True` 跑复合 adb 命令(Windows cmd.exe 吃掉 `;`/`|`/`>`);本设备 su 用 `su 0 <cmd>` 而非 `su -c`

---

## 14. Useful one-liners

```bash
# 查 API 路由
grep -nE "^@app\.(get|post|put|delete|websocket)" E:\ProjectorPressureTest\server.py

# 查前端 state 字段被哪里用
grep -nE "state\." E:\ProjectorPressureTest\static\app.js | head -50

# 查某个 CSS class
grep -nE "^\.device-card " E:\ProjectorPressureTest\static\style.css

# 重启服务(开发用)
netstat -ano | grep ":8000.*LISTENING" | awk '{print $NF}' | head -1 | while read pid; do taskkill /F /PID $pid; done
D:\Conda_Environments\dev_env\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8000

# 端到端测试
curl -X POST http://127.0.0.1:8000/api/sequences -H "Content-Type: application/json" -d '{"name":"test_seq"}'
curl http://127.0.0.1:8000/api/sequences
```

---

## 15. 怎么用这份文档

新会话开场建议结构:

```
[粘贴本文件内容,然后加你的任务]
---

## 你的当前任务

<用户给的具体需求>

## 期望产出

<具体的预期 / 验收标准>
```

下一任会:
1. Read §0-2 知道是什么
2. Read §6 设计决策(避免回退)
3. Read §7 "不存在"清单(避免幻觉)
4. Read §8 SOP
5. **若改前端,必读 §5.5(卡片绑定/切换/临时离线/性能图表 + 踩坑清单)**
6. 然后开始 grep 源码,写代码

如果下一任在 5 分钟内没动手写代码,说明它没读完这份文档。

---

**最后更新**: 2026-09-11(v2.7.4:**修复"脚本在同事机器上无法结束"(stdout 管道两端编码不一致)** —— 同事机器上 `perf_monitor` 跑起来永远不结束,日志先是一个空行再 `[error] 'gbk' codec can't decode byte 0xaa`。**根因**:`server_window.ps1` 给子进程设了 `PYTHONIOENCODING=utf-8`(写 UTF-8),而 server.py 的 `Popen(text=True)` **没写 `encoding=`**(读 GBK)—— 脚本 stdout 里任意一个 GBK 非法字节对就让 `readline()` 抛 `UnicodeDecodeError`。**症状之所以是"无法结束"而不是"报错"**:那个异常逃到了 `_stream_logs` 底部的兜底 handler(只广播一条错误就 `return`),**把下面的收尾块整块跳过**(等退出 / `exit_code` / 终态 / `_stop_captures` / 归档 / `end` 帧)。三层修复:① 管道两端都钉死 UTF-8 + `errors="replace"`(含 `child_env["PYTHONIOENCODING"]`,这样不经 `server_window.ps1` 直接启服务也一致);② **读取循环自带 `try`**,失败则记原因 + 广播 + 终止子进程 + 离线程 `wait()`,收尾**无条件**执行 → 任务不可能停在半路;③ 同类隐患一并修(`ir_runner.py` 3 处、`_adb_reconnect_offline`、`/api/adb/reconnect`)。**真做了 A/B**(不是推断):回退这两处造出 pre-fix 副本,同一根探针脚本 —— 修复前 `status=running`/`exit_code=None`/**永不结束且不归档**、日志孤儿留在 `logs/`;修复后 `finished`/`0`/已归档且 `stdout.log` 逐字节完整。**注意:`locale.getpreferredencoding()` 本机同样是 `cp936`,不是"同事环境特殊",只是触发取决于脚本输出里有没有 GBK 非法字节。** 踩坑新增 #35。承 v2.7.3:**串口勾选高亮 + 任务面板排版 + reports/ 不再堆积** —— 串口行开启时整行高亮(淡蓝底+蓝边框+主题色加粗;padding/border 常驻以免勾选时卡片跳高);删掉任务面板里那个**死节点** `task-hint`(HTML 写死、JS 零引用,白占 70px 把 300px 宽的面板挤爆),`.panel-header` 加 gap + wrap、按钮组 `margin-left:auto` 保证换行后仍右对齐;**报告改为"搬"不是"复制"**(先 copy 再 unlink,删失败不丢唯一副本),`reports/` 变成几秒钟的暂存区、不再累积,并且一个任务的多份报告(`app_launch` 每个 APP 一份)全部归档为 `report.json`/`report-2.json`,不再把其余的永远留在 `reports/`。承 v2.7.2:**掉线容忍** —— 串口从"一次性"改成受监督重连循环(`_serial_attempt`,连续失败预算 60、收到数据清零、每次重试换新线程/新 Event、写可见 marker);新增 `_device_watchdog` 每 20s 发一次 **`adb reconnect offline`**(只影响离线设备,**绝不 `kill-server`**,不影响在线设备上的其它脚本);"临时离线"合成卡补上 `interrupting`;`_broadcast` 发送超时后**真正 `ws.close()`**(否则前端 `onclose` 不触发 → 浏览器握着死 socket 永远收不到日志)。顺带修复 v2.7.0 删 2s 心跳时连带删掉的 `t["captures"]` 刷新(加 `_announce_captures`,**只在状态迁移时发**以免churn 渲染键)。**探查先于实现**:用户 5 条需求里 2 条其实已满足(monitor 脚本的 `adb_shell` 从不抛异常;任务卡从不会消失),没有重复造。**明确不做**:monitor 脚本泛化、新增离线提醒 UI、自动 `kill-server`。踩坑新增 #30-#32。**未证**:串口重连成功路径与 watchdog 真触发(都需要动硬件)。承 v2.7.1:用户试用反馈的四点 —— ① **存档按模块分类** `archive/<模块>/<时间>_<脚本>_<设备>/`(`ARCHIVE_MODULES` 显式映射,与 `reports/stress-test/<模块>/` 对齐,根目录下只有模块文件夹);② **存档入口提到顶栏**(全局功能放全局位置)+ 占用徽标 hover 看分模块占用;③「清空已完成」改名「**清空已完成的卡片**」;④ **修复报告误归属**:兜底 mtime 扫描曾把前一个任务的报告算给当前任务(同设备背靠背跑时),三重收紧 —— 只扫登记脚本 / mtime ≥ 开始时间 / `_CLAIMED_REPORTS` 唯一认领。另 `_archive_dir()` 对旧扁平布局回退(升级期间老任务仍可读)。已清理 `logs/` 48 个历史日志 + `reports/` 59 个报告 + 测试存档(**两个目录本身不能删**:`logs/` 是运行期工作区 + 服务日志,`reports/` 是脚本契约,用户只需看 `archive/`)。**reboot 脚本的假 PASS 问题已记录进 [TODO.md](../TODO.md),本轮不改。** 承 v2.7.0:**任务结束自动存档 + 日志前端方案收敛** —— 每个任务终态把三份日志 `move` + 脚本报告 `copy` 进 `archive/<时间>_<脚本>_<设备>/` 并写 `summary.json` 清单;`chart.png` 由浏览器渲染后 POST 回传(服务端**不引入绘图库**),浏览器没开则如实缺图;`logs/` 降级为运行期临时区;**`×`/「清空已完成」只移除列表条目,存档永久保留**(`_forget_task`);任务卡新增「存档」按钮 + 任务面板显示总占用;导出按钮隐藏(代码保留)。同轮**删掉** v2.6.0 预埋的 logcat/串口前端显示方案(`CHANNELS`/`TABS_ENABLED`/`activeSource`/`captureCounts`/计数 pills/`#channel-bar`/服务端 2s 计数心跳)—— 用户要求"稳定简单为主导",前端只保留 stdout,logcat/串口仅服务端采集归档;保留 console 里**一行** `[archive]` 摘要作为串口失败的唯一信号。另修复对抗审查发现的四条采集隐患:logcat 重启预算改为**连续**计数(旧的终身 5 次会让 100 轮重启压测在第 6 轮永久停采)、收尾不再覆盖通道自己的 `failed/unavailable/capped` 诊断、串口读线程死后可见、串口加 512MB 上限;重启回读 2000 行以回填开机日志。踩坑新增 #27-#30。**未证:前端实际渲染效果(无真实浏览器)。** 承 v2.6.0:**每任务三源实时日志采集** —— logcat 恒开 + 串口按任务勾选,服务端采集(9 个脚本零改动),WS 按源订阅过滤(`WS_SUBS`,无人订阅的源零成本)+ 5s 发送超时;logcat 自动重启循环(adb kill-server / 设备重启后 ≤2s 恢复,写可见 marker)、256MB 上限(`PPTP_LOGCAT_MAX_MB`)、**永不 `logcat -c`**;串口走守护线程 + 丢最旧队列,`script_owns_port` 仲裁让位 battery 脚本;日志文件名改为 **`<task_id>_<时间>_<脚本>_<设备>.<源>`**(服务器下发确切名,前端不重建路径);`GET /api/tasks/{id}/log?source=` 一律尾部读 + 非法源 400;新增 `GET /api/serial/ports`;导出改为多源下载 + 导出名含设备/脚本/时间;**前端仅展示 stdout**(`TABS_ENABLED=false`,数据层/WS/渲染路由已按通道就位,`#channel-bar` 预留节点在 `#log-info` 外)。踩坑新增 #22-#26。**未证:串口数据通路**(端口开合正常、无副作用,但"读到字节"未被观测到 —— 开机后空闲 console 无输出属合理表现)。承 v2.5.2:新增重启蓝牙音箱回连压测脚本 `bt_reboot_stress.py` —— 仿 wifi_reboot_stress(reboot → 上线 → 轮询 A2DP 回连),回连判定 = 适配器开 + A2DP `mConnectionState: CONNECTED`,只测重启回连不测开关回连(蓝牙开关未开放给用户);真机验证边界行为:刚连上立即重启回连失败、手动重连一次后 ~6s 自动回连。承 v2.5.1:前端 log console 全英文/ASCII + 折线图 x 轴从 2min 刻度改为固定 1h 滚动窗口 6×10min 刻度,perf/battery 同步;删除 STATUS_ZH/JUMP_TYPE_ZH 中文映射、时间戳走 fmtClock 强制 24h、移除 dataZoom;放电/电池测试结束设备关机后卡片不再消失,合成"放电关机"专用卡(仿临时离线);**导出的 chart.png 改为全程图**(`exportFullChartDataUrl` 从 perfSamples 全量渲染,不受 1h 实时窗口限制),**横轴 0.5h 一个刻度**。承 v2.5.0:新增电池充放电压测脚本 battery_inout_stress —— 串口开 health 轮询拿实时电量/温度、mode 充/放电单次只跑一个、100% 稳定或关机自动停、跳变/温控检测保留、前端 perfKind 泛化出电量+温度双线曲线与三件套导出、脚本全英文/前端中文。踩坑新增 #18 电池只能走串口/#19 PERF kind 注册/#20 log ASCII/#21 1h 窗口与 dataZoom 互斥)
**会话状态**: 完整运行中,所有改动已通过端到端验证
**未完成需求**:
- **v2.7.0 前端从未在真实浏览器里跑过** —— 服务端全部逐路径验证通过,DOM id 也做了交叉校验,但"按钮长什么样、点下去对不对"没人看过。**下次开浏览器先 Ctrl+F5**,重点看:任务卡 `存档` 按钮、任务面板占用数字、跑完那次 `[archive]` 摘要行、以及图表回传是否真的落进 `archive/*/chart.png`。
- **`wifi_reboot_stress.py` 会把失败的 reboot 记成 PASS(已报用户,未改)**:`adb reboot` 的 `rc` 被丢弃,而"设备已上线"的轮询对一个**从未重启**的设备同样成立 → 若 `adb reboot` 中途开始失败(ADB/USB 抖动、瞬时 offline、固件不允许),100 轮仍会全部 PASS 并打印 100% 通过率。**对压测平台来说假绿是最坏的输出**,建议先向用户确认再改。
- 同上脚本:第一轮迭代完成前中断会打印 `passed: 0 / 0` + `success rate: 0.0%` + exit 1,**读起来像彻底失败**,而实际上什么都没测。
- **串口停止时最后半行必丢**(见 §5.5.5 #30)—— 已确认,刻意不修。
- sensor_reboot_stress 待连真机验证读取方式与 PASS 判定(脚本已就绪)
- app_launch_stress **冷启动已真机验证通过**(三个 APP 各 1 轮,TotalTime 正常、LaunchState=COLD、Displayed 交叉验证 1/1 命中);**热启动(hot)模式还没真机验证**——等跑一轮确认 HOME 退后台 → 拉起流程与 WARM/HOT 语义校验
- app_launch_stress 冷启动曾因 component 简写(`.` 前导)打不开 Prime Video / YouTube,已修复为全限定 + YouTube 改用 ShellActivity(详见 §6 决策 #5 踩坑记录)
- battery_inout_stress **脚本逻辑 + 独立 CLI 已真机验证**(COM9 串口开 health 轮询成功、实时样本、跳变/温控、报告落盘、`--probe`/`--dump-params`);**前端曲线/导出已随 v2.5.0 平台跑通**;**用户试跑待验证**(charge 模式到 100% 稳定停;discharge 模式设备关机自动停)
- v2.5.1 **前端改动待真机复核**:重启 uvicorn + Ctrl+F5 后跑一个 perf 任务 + 一个 battery 任务,确认 log console 全英文、折线图 x 轴为 6×10min 刻度且右缘跟随最新样本滚动;导出 chart.png 应为**全程图**(时间跨度 = 任务全程,横轴 0.5h 一个刻度,不是 1h 实时窗口);再跑一次放电测试,设备关机后卡片应变为"放电关机"状态(虚线红边)而非消失,重新开机后卡片恢复在线
- bt_reboot_stress **真机已验证两个方向**(刚连上立即重启 → 回连失败 A2DP CONNECT_TIMEOUT→DISCONNECTED,连手动 toggle 蓝牙也连不回;用户在蓝牙设置里手动重连一次后 → 重启后 ~6s 自动回连 PASS);**正式长时压测待用户跑** —— 跑之前先手动确认音箱已连上(刚配对完立即重启会触发 fresh-bond flaky,详见 README 注意事项)
**已知小问题**:
- WS 在 server restart 时 log console 会"卡"在最后一行(没手动 rejoin)
- 任务被删除时 log 文件可能短暂残留(orphan cleanup 只清 startup 时的)
- IR event 设备路径(默认 `/dev/input/event1`)是 hardcode 的,如果换设备要改 `ir_runner.py` 顶部 `DEFAULT_EVENT_PATH`(详见 [README.md 故障排查](../README.md))
- WiFi 脚本在部分设备上需要 root:本设备已验证 `su 0 wpa_cli ...` 和 `su 0 cmd wifi ...`(`su -c` 会报错);`use_su` 参数可关
- wifi_switch 的 `WIFI_NETWORKS` 是预置列表,SSID 若在真机上连不上,先 `wpa_cli scan_results` 核对实际 SSID(遇到过 `-`/`_` 拼写差异)
- sensor_reboot_stress 读传感器节点需 root;本设备 su 是"裸 su"(`su -c` 报 `invalid uid/gid -c`),脚本会依次尝试直读 / su -c / su 0 / stdin 交互式 su / PTY,运行日志打印实际生效方式。PARAMS 只留 `sensor`/`iterations`/`reboot_timeout`,采集窗口/检测次数/通过阈值是硬编码常量
- battery_inout_stress 的 `serial_port` 必须由用户在试跑时确认哪个 COM 对着 BMS(本机 COM9 = FTDI 0403:6010);选错串口 → 拿不到实时电量/温度(踩坑 #18);放电模式靠"设备关机"自动停,测试中若外部供电仍在,电池读不到 0% 前不会自己停,属正常
