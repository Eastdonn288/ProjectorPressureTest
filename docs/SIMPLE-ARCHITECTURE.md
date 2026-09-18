# PPTP 架构总览

> **本文档描述 PPTP 平台当前的真实架构(v2.11.1,2026-09-17)。**
> 代码级细节(函数行号 / 数据模型 / 设计决策 / 踩坑)以 [HANDOFF_PROMPT.md](HANDOFF_PROMPT.md) 为准;
> 本文是"系统长什么样、数据怎么流、契约是什么"的速览。改代码前先看 HANDOFF。

> 版本:v2.11.1 | 维护规则:架构变动随版本更新;README / CHANGELOG / HANDOFF 同步。
> 报告(HTML)的格式与参数架构单独沉淀在 [REPORT_FORMAT.md](REPORT_FORMAT.md)。

---

## 1. 总体架构

```
┌──────────────────────────────────────────────────────────────────────┐
│  Browser — 单页 HTML + 原生 JS + ECharts 本地 vendor(无构建步骤)        │
│  ┌ 顶栏:服务信息 · 存档占用 · 打开存档 · 重置 · 关机 ──────────────┐  │
│  ├──────────┬──────────┬──────────────────────────┬───────────────┤  │
│  │ 设备面板   │ 脚本面板  │  日志面板(#log-console +   │  任务面板      │  │
│  │  3s 轮询  │ 启动拉取  │   #log-info + #perf-chart)│  2s 轮询      │  │
│  │ 含串口勾选 │          │   ↑只渲染 stdout          │  卡片:存档入口 │  │
│  └──────────┴──────────┴──────────────────────────┴───────────────┘  │
│    全局单日志面板绑定 state.currentTaskId;WS 帧按 taskId 过滤 → 不残留   │
│    v2.7.0:console 只渲染 stdout(logcat/serial 的前端显示方案已删除)     │
│    v2.7.1:存档是全局功能 → 入口与占用放顶栏,不混在任务卡工具栏里         │
└──────────▲───────────────────▲───────────────────────────────────────┘
           │ REST (GET/POST/DELETE)   │ WebSocket /ws/logs/{task_id}?sources=
┌──────────▼───────────────────▼───────────────────────────────────────┐
│  server.py (FastAPI 单文件,2491 行)                                   │
│                                                                      │
│  · in-memory TASKS = { task_id: {...} }(进程句柄不下发给前端)          │
│  · NoCacheMiddleware:静态文件 no-store + 前端 ?v= 缓存破坏              │
│  · /api/scripts/{name}/params → 跑 `--dump-params` 读自描述参数 schema │
│  · _stream_logs 协程:读子进程 stdout → 落盘 + 重放 50k 行 + WS 逐行推送 │
│    PERF| 前缀行原样流转(性能图表零后端改动)                             │
│  · 【v2.6.0】三源采集:stdout + logcat(恒开)+ serial(任务勾选)         │
│    统一 _emit → 落盘 + 计数 + _broadcast(source)                       │
│    按源订阅过滤(WS_SUBS):无人订阅的源零成本                            │
│  · 【v2.7.0/2.7.1】任务终态 _archive_task:三份日志 move + 报告 copy    │
│    → archive/<模块>/<时间>_<脚本>_<设备>/,并写 summary.json 清单       │
│  · 【v2.7.2】_device_watchdog:每 20s 对离线设备发 adb reconnect offline │
│    串口 _serial_attempt 受监督重连(连续失败预算,收到数据即清零)        │
│  · 每设备唯一性 409 校验 / 硬停(SIGKILL)/ 孤儿进程清理 / adb 重连        │
└──────────▲────────────────────────────────────────────────────────────┘
           │ subprocess.Popen(["python","-u","scripts/x.py","--device",serial,"--params",json])
           │ list-arg 子进程(shell=False) → Windows cmd.exe 不吞 ;/|/>
           │ 另:asyncio 子进程(adb logcat)+ 守护线程(串口)
┌──────────▼────────────────────────────────────────────────────────────┐
│  scripts/*.py(9 个,平台契约,独立可 CLI 跑;**v2.6.0 零改动**)          │
│    ir_runner / wifi_onoff / wifi_reboot / wifi_switch /                │
│    sensor_reboot / app_launch / perf_monitor / battery_inout_stress /  │
│    bt_reboot_stress                                                    │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 2. 技术栈与部署

| 层 | 选择 | 备注 |
|---|---|---|
| 后端 | FastAPI 单文件 `server.py` | uvicorn;`start.bat` 一键启动 |
| 前端 | 原生 JS(无框架) | 单 `index.html` + `app.js`(IIFE,2317 行) |
| 图表 | ECharts 5.5.1 本地 vendor | `static/vendor/echarts.min.js`(~1MB,pin 版本) |
| 设备交互 | adb subprocess | 不引入第三方 adb 库 |
| 数据库 | 无 | 内存 dict + 日志文件 + localStorage |
| 部署 | 本地单机 Windows | 无远程、无鉴权、单用户(项目铁律) |
| 串口 | pyserial 3.5(**可选**) | 延迟 import;缺失时串口采集降级为空/`unavailable`,服务照常起 |

---

## 3. 后端 API 面(有效端点)

| Method | Route | 用途 |
|---|---|---|
| GET | `/healthz` | 健康检查(含 version) |
| GET/POST | `/api/server/status` · `/api/server/shutdown` | 服务状态 / 优雅关停 |
| GET | `/api/devices` | `adb devices -l` 解析 |
| GET | `/api/scripts` · `/api/scripts/{name}/params` | 脚本列表 + `--dump-params` 参数 schema |
| GET/POST/PUT/DELETE | `/api/sequences[/{name}]` | IR 序列 .ini 列表 / 读写 / 删除 / 新建模板 |
| POST | `/api/run` | 启动任务(device-uniqueness 409 + `--params` 透传) |
| POST | `/api/stop/{task_id}` | 中断任务(CTRL_BREAK → SIGBREAK → KeyboardInterrupt) |
| POST | `/api/tasks/force-stop-by-device/{serial}` | 硬停(SIGKILL,设备临时离线时用) |
| POST | `/api/tasks/force-cleanup` · `/api/adb/reconnect` | 硬清理 / adb server 重启 |
| GET/DELETE | `/api/tasks` · `/api/tasks/{id}` | 任务列表/详情 |
| GET | `/api/tasks/{id}/log?source=` | 单通道日志**尾部读**(默认 `stdout`;非法 source → 400;归档后从 `archive/` 读) |
| POST | `/api/tasks/cleanup` | 从列表移除已结束任务(**存档保留**) |
| GET | `/api/serial/ports` | 本机 COM 口列表(pyserial 缺失 → 空列表) |
| GET | `/api/tasks/{id}/report` | 内联返回该次运行归档里的 HTML 报告(任务卡「报告」按钮;**archive/ 没有静态挂载**,这是唯一入口。多份报告时开文件名第一份) |
| POST | `/api/tasks/{id}/archive/artifact` | 浏览器回传图表 PNG 进存档(白名单 + 8MB 上限) |
| POST | `/api/tasks/{id}/archive/reveal` · `/api/archive/reveal` | 资源管理器打开任务存档 / 存档根目录 |
| GET | `/api/archive/stats` | 存档总数与总占用 |
| WS | `/ws/logs/{task_id}?sources=` | 重放 + 实时推送;默认 `stdout`(前端只用默认值) |

---

## 4. 关键数据流

### 4.1 启动任务
```
[前端] POST /api/run {device, script, params}
  → [后端] 409 校验(该设备已有 running/interrupting 任务)
  → Popen(["python","-u",f"scripts/{script}","--device",serial,
           "--params",json.dumps(params)])
  → TASKS[task_id] = {device, script, status:"running", ...}
  → asyncio.create_task(_stream_logs(task_id, proc))
  → [前端] 任务卡显示 running
```

### 4.2 日志推送(三源 + PERF 流)

```
[子进程 stdout] ──逐行──▶ [_stream_logs 协程]
                                    │
[adb logcat]  ──asyncio 子进程──▶ [_capture_logcat]
   (恒开;重启回读 2000 行;       │
    连续失败预算 60)               ├─▶ _emit(task_id, source, line)
[串口 COMx]   ──守护线程+Queue──▶ [_capture_serial]     │
   (任务勾选,丢最旧,512MB 上限)      │                  │
                                    │                  ├─▶ 落盘 logs/<stem>.<source>
                                    │                  └─▶ _broadcast(..., source)
                                    │                        │
                                    │            WS_SUBS 源订阅过滤:无人订阅 → 跳过
                                    │                        ▼
                                    └── stdio 重放(50k 行 / 4MB 尾读)──▶ WS 逐行推给前端
                                                             ▼
                                              [前端 appendLogLine(line)]
                                                ├─ 普通行 → #log-console(RAF 批量)
                                                └─ PERF| 前缀 → handlePerfLine
                                                     (meta/sample → 性能图表,零后端改动)

[任务终态] ──▶ _stop_captures(关句柄) ──▶ _archive_task ──▶ archive/<模块>/<时间>_<脚本>_<设备>/
                │                                              ↑
                └─▶ console 一行归档摘要                 浏览器 POST chart.png
```

`<模块>` 由 `ARCHIVE_MODULES` 显式映射得出(脚本名 → `wifi`/`perf`/`battery`/`sensor`/`app-launch`/`ir`/`bt`,未登记落 `other`),与 `reports/stress-test/<模块>/` 对齐。

- **`source = None` 是控制帧**(`state` / `end` / 归档摘要行),发给所有订阅者;`source = stdout|logcat|serial` 是数据帧,只发给订阅了该源的连接。**前端只订阅 stdout**,所以 logcat/serial **根本不上网线**
- **通道行数不进 `/api/tasks`**:公开快照只含 `active/status/restarts/detail/port`(慢变),计数会进 `summary.json` 与那行 console 摘要 —— 否则每 2s 轮询都会让前端全量重渲染(见 HANDOFF 踩坑 #23)
- `PERF|{json}` 是 perf_monitor / battery_inout_stress 的采样流(前端按 `perfKind` 区分图表类型);**必须带 `type:"sample"` + `clock`**(前端分发硬依赖,缺失则图表全丢 —— 见 HANDOFF 踩坑 #17)。它与三源采集是**正交**的两条路:PERF 走 stdout,后端零改动

### 4.3 任务结束 → 自动存档

```
[_stream_logs 尾部 / 硬停 / 强制清理 / 关服务]  ← 四个收尾调用点(同一个任务只会走到其中一个;
                                               硬停那条靠 t["_finalized"] 让 _stream_logs 让位,见 4.4)
        │
        ├─▶ _stop_captures(task_id, reason)   ← 必须在前:释放串口 + 关闭日志句柄
        │      (failed/unavailable/skipped/capped 保留自身状态,不被改写成 stopped)
        ├─▶ _archive_task(task_id, reason)    ← 幂等、同步、绝不抛
        │      ├─ 三份日志 Path.replace() 进 archive/<名字>/
        │      ├─ report.json ← stdout 嗅探到的路径(兜底 mtime 扫描),必须落在 reports/ 内
        │      ├─ <报告stem>.html ← _companion_files 按「同目录+同stem+.html」自动收走
        │      │    (v2.10.0 起每个脚本都有;主报告改名为 report.json,伴随文件保留原名)
        │      └─ summary.json ← 参数/状态/各文件 bytes+lines/report_source/notes
        ├─▶ WS end 帧(带 archive 字段)
        └─▶ console 一行 [archive] 摘要(串口采到没有就靠这一行)
```

图表由**浏览器**在任务结束时渲染后 POST 到 `/api/tasks/{id}/archive/artifact` —— 服务端**故意不引入绘图库**(ECharts 只存在于页面里)。代价:结束时浏览器没开就没有 `chart.png`,由 `summary.json` 如实反映。

### 4.5 报告生成(脚本侧,与平台解耦)

```
脚本已有的局部变量 ──┬──▶ 现有 print(...)        ★ 平台只显示 stdout,一字不改
                     └──▶ rows=[row(...), ...]   ★ 值表达式抄旁边那一句
                                └─▶ build_payload() ─▶ write_html_report(json_path, doc)
                                                              └─▶ <报告同stem>.html
```

**引擎不渲染 stdout,平台也不解析 HTML** —— 两边唯一的接触面是文件系统上的 stem 配对(见 4.3)。
所以报告能力**对平台是零改动的**:`_pptp_report` 是纯 stdlib、import 无副作用、下划线开头(opts out of 脚本发现)。

**接口**:stdout 里 `report :` 行仍是可嗅探的最后一行;新增 `html =` 行用 `=` 所以不会被误认。
完整契约、参数表模型、新脚本接入清单 → [REPORT_FORMAT.md](REPORT_FORMAT.md)。

### 4.4 中断 / 硬停
- **中断**:`POST /api/stop/{id}` → 子进程发 CTRL_BREAK(Windows)→ 脚本 `SIGBREAK→KeyboardInterrupt` → 打印已完成汇总 → **报告与 HTML 照常写出**(2026-09-16 起,中断不再跳过报告;报告里注明本次被中断)→ 退出 → 状态 `interrupted`。**刻意不杀采集** —— 停机/收尾日志最有价值,由 `_stream_logs` 在真正 EOF 时统一收
- **硬停**:`POST /api/tasks/force-stop-by-device/{serial}` → `proc.kill()`(SIGKILL)+ 就地归档(SIGKILL 没有 EOF 可等)。
  **v2.11.1 起,硬停是它那条路径上的唯一终结者**,且在**第一个 `await` 之前**同步认领:`t["_finalized"] = "force_stop"`
  + 直接写终态 `interrupted` / `exit_code = -9` / `ended_at`。`_stream_logs` 在 `proc.wait()` 之后看到 `_finalized`
  就 **return**,不做第二次终结(它要做的收尾硬停本来就全做了)。**为什么必须这样**:`_stream_logs` 正卡在
  `proc.wait()` 上,**子进程一死就醒**,而原作者以为它「稍后」才醒 —— 结果是它先把任务终结成 `failed`(终态),
  硬停随后的 `interrupting`(非终态)**盖在终态上**,而它已经 `return`,没人再推进 ⇒ 任务永远 `interrupting`,
  409 守卫([第 102 行](#43-任务结束--自动存档)那条 `running/interrupting` 校验)把那台设备**永久锁死**。
  见 HANDOFF 踩坑 #48 / [TODO.md](../TODO.md) §8.1。

---

## 5. 脚本契约(scripts/*.py 统一遵守)

1. **独立可跑**:`python -u scripts/x.py --device <serial> [--params <json>]` 能脱离平台 CLI 直接跑
2. **自描述参数**:可选模块级 `PARAMS` 列表 + `--dump-params` 打印 schema(`int/float/bool/str/select`);前端自动渲染参数弹窗
3. **SIGBREAK → KeyboardInterrupt**:平台"中断"按钮能优雅打断并输出已完成汇总
4. **ASCII-only stdout**:不打印非 ASCII,避免 Windows 控制台乱码 / WS 编码问题
5. **报告落盘**:`reports/stress-test/<模块>/<name>_<dev>_<ts>.json`(已 gitignore)。
   **v2.10.0 起每个脚本还要写一份同 stem 的中文 HTML** —— 走共享引擎 `_pptp_report.write_html_report(json_path, doc)`,
   路径由引擎从 JSON 路径推导(**不要自己拼串**:stem 逐字相同是归档能收到它的唯一条件)。
   stdout 里 `report :` 行必须**仍是最后一行**(`_sniff_report_path` 只取第一行命中);新增的 `html =` 行必须用 `=`。
   完整规则见 [REPORT_FORMAT.md](REPORT_FORMAT.md);`ir_runner.py` 无报告(无 PASS/FAIL 语义)。
6. **退出码**:0 = PASS / 手动结束,非 0 = FAIL
7. **日志采集不归脚本管**(v2.6.0):脚本**不需要**也不应该自己抓 logcat / 开串口 —— 平台在服务端统一采集。唯一例外是脚本**业务本身**需要串口(`battery_inout_stress.py` 的 `serial_port`),此时平台按其参数声明**自动让位**(`skipped`/`script_owns_port`),不抢口

| 脚本 | 测什么 | 参数 |
|---|---|---|
| `ir_runner.py` | 红外序列(KEY_*/KEYCODE_* 双通道注入,长按 down+hold+up) | 序列文件 |
| `wifi_onoff_stress.py` | WiFi 开关循环 + wpa_cli 扫描 | iterations/on/off/scan/use_su |
| `wifi_reboot_stress.py` | adb 重启循环 + WiFi 重连 | iterations/wait/settle/back_online |
| `wifi_switch_stress.py` | 多网络循环切换(预置 `WIFI_NETWORKS`) | cycles/use_su |
| `sensor_reboot_stress.py` | 重启 + gsensor/ToF 回连压测 | sensor/iterations/reboot_timeout |
| `app_launch_stress.py` | APP 冷/热启动耗时(APP_PRESETS 内置 3 APP 一起跑) | mode/iterations/p95_threshold |
| `perf_monitor.py` | CPU/GPU/内存% + 前台APP CPU%(PERF| 流 → 前端图表);跑完出 `report.json` + 中文 HTML(**保留自己的渲染器**,4 个区块比通用引擎丰富;参数表走共享引擎) | interval/duration/watch_pkg |
| `battery_inout_stress.py` | 电池充/放电压测(电量/温度/电压,串口开 health 轮询,100% 稳定或关机自动停) | mode/serial_port/interval/temp_warn/full_hold |
| `bt_reboot_stress.py` | 重启 + 蓝牙音箱 A2DP 回连压测(reboot → 上线 → 轮询音箱回连,判定=适配器开 + A2DP CONNECTED) | iterations/wait_sec/bt_reconnect_timeout/back_online_timeout |

---

## 6. 前端架构要点

- **全局单日志面板**:整个平台只有**一个** `#log-console` + `#log-info` + `#perf-chart`,全部绑定 `state.currentTaskId`。设备卡/任务卡只是"把 currentTaskId 指到哪"的入口
- **不残留三件套**:WS 帧按 `currentTaskId !== ws._taskId` 过滤 + 切任务 dispose/重建图表 + 每任务独立样本缓冲 `perfSamples[taskId]`
- **只显示 stdout,永久(v2.6.0 采集 / v2.7.0 定案)**:logcat 与串口在服务端照采、照归档,**前端永不展示**。v2.6.0 曾预埋的通道显示方案(`CHANNELS` / `TABS_ENABLED` / `activeSource` / 计数 pills / `#channel-bar`)已在 v2.7.0 **整体删除** —— 用户判断这两种日志体量太大、不值得做展示,要求"稳定简单为主导"。**别再把它加回来。**
- **图表回传**:任务结束时浏览器用 `exportFullChartDataUrl` 渲染 PNG 并 POST 进存档(见 4.3)。触发点两处:WS `end` 帧(正在看该任务时)+ `refreshTasks` 轮询扫描(覆盖"任务在别的视图下结束"与"跑完才打开页面")
- **串口采集是运行期 opt-in**:设备卡 `.device-row4` 的 checkbox + COM 下拉;端口持久化、勾选**不**持久化;脚本自身声明了 `serial_port` 时控件禁用并给出中文提示(平台自动让位)
- **性能图表**:`#perf-chart`(200px)挂在日志面板头部;ECharts `type:"time"` 墙钟横轴(**固定 1h 滚动窗口**,`interval:10min` + `splitNumber:6` ~6 刻度,右缘跟随最新样本)、双 Y 轴、导出三件(txt + csv + **全程图 png**)。**图表类型按脚本泛化**(`perfKind`):`perf` 类(perf_monitor)画 CPU/GPU/MEM/前台APP 四线;`battery` 类(battery_inout_stress)画电量(左 0-100%)+ 温度(右 °C)两线。单图 + 单任务绑定,切换任务 dispose/重建,不残留。**导出的 png 为任务全程图**(离屏渲染 `perfSamples` 全量,横轴 0.5h 一个刻度,不受屏幕 1h 窗口限制;v2.5.1)。**日志控制台输出全英文/ASCII**(v2.5.1,UI 文案仍中文)
- **合成设备卡**:`renderDevices()` 会对"不在 `adb devices` 但仍有意义"的设备合成专用卡 —— **临时离线卡**(有 running 任务,reboot 压测,琥珀虚线)+ **放电关机卡**(电池测试终态 + 设备关机,`_batt_off`,红色虚线 + "设备已关机"/"放电关机"徽标,v2.5.1)。二者都算"非离线"可交互;设备重新上线自动回正常卡
- **状态持久化**:`localStorage` key `pptp.deviceState.v1` 只存已选设备/每设备脚本/序列/参数/串口口;设备与任务数据总从 server 拉
- **轮询节奏**:设备 3s / 任务 2s / 服务 5s / 时钟 1s;**图表逻辑只在离散导航点调用,绝不进轮询**

---

## 7. 目录结构

```
ProjectorPressureTest/
├── server.py              # FastAPI 单文件(2491 行)
├── start.bat / stop.bat / server_window.ps1   # 一键启停 + 服务窗口
├── README.md / CHANGELOG.md
├── docs/                  # 给 agent 的文档(见 §8);REPORT_FORMAT.md = 报告格式与参数架构真源
├── static/
│   ├── index.html         # 单页(155 行)
│   ├── app.js             # 前端逻辑 IIFE(2317 行)
│   ├── style.css          # 暗色主题(872 行)
│   └── vendor/echarts.min.js  # ECharts 5.5.1 本地化(~1MB)
├── scripts/               # 9 个压测脚本(平台契约,§5) + _pptp_report.py 报告引擎(下划线开头 → 不当可跑脚本)
├── ir_sequences/          # IR 序列 .ini + KEY_REFERENCE.md 按键速查
├── reports/stress-test/   # 脚本报告原始落点(脚本契约;平台复制一份进 archive/,gitignore)
├── archive/               # ★ 任务存档(永久保留,**按模块分类**)
│   └── <模块>/{wifi,perf,battery,sensor,app-launch,ir,bt,other}/
│        └── <时间>_<脚本>_<设备>/{stdout,logcat,serial}.log
│            + report.json + chart.png + summary.json
│            + <报告stem>.html   ← v2.10.0 起每个脚本都有(伴随文件,不改名)
└── logs/                  # 运行期临时工作区(任务结束内容搬进 archive/)
                           #   server.out/err.log + <task_id>_<时间>_<脚本>_<设备>.*
```

---

## 8. 文档导航(docs/ 分工)

| 文档 | 定位 | 状态 |
|---|---|---|
| [HANDOFF_PROMPT.md](HANDOFF_PROMPT.md) | **真源**:API / 数据模型 / 前端交互模型 / 踩坑清单 / 设计决策 | 活跃(逐版本维护) |
| [SIMPLE-ARCHITECTURE.md](SIMPLE-ARCHITECTURE.md) | 本文:架构总览 / 数据流 / 脚本契约 | 活跃 |
| [REPORT_FORMAT.md](REPORT_FORMAT.md) | **报告格式真源**:HTML 架构 / row 模型 / payload schema / **Params 表** / 归档配对 / stdout 契约 / 接入清单 | 活跃(v2.10.0 新增) |
| [PERF_MONITOR_V2.md](PERF_MONITOR_V2.md) | perf_monitor v2 的判定模型(judge / gates / ROW_SPEC) | 活跃 |
| [FRONTEND_UX.md](FRONTEND_UX.md) | 前端"为什么这样设计" + 交互决策时间线 | 活跃 |
| [SIMPLE-PRD.md](SIMPLE-PRD.md) | v2.0 初版 PRD(历史) | 已归档 |
| [SIMPLE-PLAN.md](SIMPLE-PLAN.md) | v2.0 初版实施计划(历史) | 已归档 |

> 版本沿革见 [CHANGELOG.md](../CHANGELOG.md);用户视角用法见 [README.md](../README.md)。
