# PPTP 架构总览

> **本文档描述 PPTP 平台当前的真实架构(v2.11.2)。**
> 它是系统"**形状 / 契约 / 边界**"的唯一真源:**后端 API 面(§3)、数据流(§4)、目录结构(§7)** 只在这里定义,其它文档只写指针,不复制正文。改代码前先看这里,再看 [DECISIONS.md](DECISIONS.md)(已批准、不可回退)与 [PITFALLS.md](PITFALLS.md)(踩过的坑)。
>
> **铁律:纯本地 / 单机 / 无远程 / 无鉴权 / 单用户。**
> 报告(HTML)的格式与参数架构真源见 [REPORT_FORMAT.md](REPORT_FORMAT.md);接手路径见 [START-HERE.md](START-HERE.md)。

> 版本:v2.11.2 | 维护规则:架构变动随版本更新;CLAUDE.md / README / CHANGELOG 同步。

---

## 1. 总体架构

四层,自顶向下:**浏览器 → HTTP(WS + REST)→ server.py → 脚本子进程**。

```
[Browser]   单页 HTML + 原生 JS + ECharts 本地 vendor(无构建步骤)
  · 顶栏:服务信息 · 存档占用 · 打开存档 · 重置 · 关机
  · 设备面板(3s 轮询,含串口勾选)/ 脚本面板(启动拉取)/ 任务面板(2s 轮询)
  · 全局唯一日志面板:#log-console + #log-info + #perf-chart,绑 state.currentTaskId,
    只渲染 stdout;WS 帧按 taskId 过滤 → 切任务不残留(详见 §6)
        │ REST(GET/POST/PUT/DELETE)          │ WS /ws/logs/{task_id}?sources=
        ▼                                     ▼
[server.py]  FastAPI 单文件
  · TASKS = { task_id: {...} } 进程内(进程句柄不下发给前端)
  · NoCacheMiddleware:静态 no-store + 前端 ?v= 缓存破坏
  · 三源采集(§4.2)/ _stream_logs 落盘 + 重放 50k 行 / PERF| 行原样流转
  · 终态 _archive_task(§4.3)→ archive/<模块>/<时间>_<脚本>_<设备>/
  · 每设备唯一性 409 / 硬停(SIGKILL)/ 孤儿进程清理 / adb 重连
  · _device_watchdog(v2.7.2);串口 _serial_attempt 受监督重连
        │ subprocess.Popen(["python","-u","scripts/x.py","--device",serial,"--params",json])
        │ list-arg(shell=False)→ Windows cmd.exe 不吞 ;/|/>;另:asyncio 子进程(logcat)+ 守护线程(串口)
        ▼
[scripts/*.py]  9 个:ir_runner / wifi_onoff / wifi_reboot / wifi_switch / sensor_reboot /
  app_launch / perf_monitor / battery_inout_stress / bt_reboot_stress
  —— 契约见 §5,独立可 CLI 跑,平台侧零注册(v2.6.0 起零改动)
```

---

## 2. 技术栈与部署

| 层 | 选择 | 备注 |
|---|---|---|
| 后端 | FastAPI 单文件 `server.py` | uvicorn;`start.bat` 一键启动,`server_window.ps1` 开服务窗口 |
| 前端 | 原生 JS(无框架) | 单 `index.html` + `app.js`(IIFE) |
| 图表 | ECharts 5.5.1 本地 vendor | `static/vendor/echarts.min.js`(~1MB,pin 版本) |
| 设备交互 | adb subprocess | 不引入第三方 adb 库 |
| 数据库 | 无 | 内存 dict + 文件 + `localStorage`;`data/` 只放 `server.pid` 之类运行期小文件,任务表本身在内存 |
| 部署 | 本地单机 Windows | 纯 HTTP、`127.0.0.1:8000`;无远程、无鉴权、单用户(项目铁律) |
| 串口 | pyserial 3.5(**可选**) | 延迟 import;缺失时串口采集降级为空/`unavailable`,服务照常起 |

---

## 3. 后端 API 面(有效端点)

**本节是后端 API 面的唯一所有者** —— 路由、方法、请求与响应形状只在这里定义。

| Method | Route | 用途 |
|---|---|---|
| GET | `/` | 返回 `static/index.html`(单页入口) |
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

`static/` 是唯一的静态挂载(且**故意最后挂**,不遮 API 路由);`archive/` **不做静态挂载** —— 存档只能经 `/api/tasks/{id}/report` 与 reveal 端点访问。

### 3.1 三条 API 不变量(改动时不得破坏)

- **每设备单任务 409 守卫**:`POST /api/run` 启动前校验目标设备是否已有 `running` / `interrupting` 任务,有则 409。守卫**只认这两个状态** —— 任何把任务永久卡在 `interrupting` 的 bug 都会把那台设备**永久锁死**(见 §4.4)。
- **`_CLAIMED_REPORTS`**:服务端已认领过的报告文件绝对路径集合。同一份报告只会被**一个**任务收进存档;mtime 兜底扫描时据此排除已认领文件,避免两次运行互相偷报告。
- **`_public_captures(t)`**:任务快照里**允许下发**的那部分采集状态(`active/status/restarts/detail/port`,均为慢变量)。通道行数等高频量故意不进 `/api/tasks`(见 §4.2)。

---

## 4. 关键数据流

**本节是数据流的唯一所有者**:run → 日志流 → 采集 → 归档 → 报告 → 中断,只有这里讲。

### 4.1 启动任务
```
[前端] POST /api/run {device, script, params}
  → [后端] 409 校验(该设备已有 running/interrupting 任务)
  → Popen([...]) → TASKS[task_id] = {device, script, status:"running", ...}
  → asyncio.create_task(_stream_logs(task_id, proc)) → [前端] 任务卡 running
```

### 4.2 日志推送(三源 + PERF 流)

```
[子进程 stdout] ────逐行────▶ [_stream_logs 协程]
                                   │
[adb logcat] 恒开 ─asyncio 子进程▶ [_capture_logcat]  (重启回读 2000 行;连续失败预算 60)
                                   ├─▶ _emit(task_id, source, line)
[串口 COMx] 勾选 ─守护线程+Queue▶ [_capture_serial]  (丢最旧,512MB 上限)
                                   │      ├─▶ 落盘 logs/<stem>.<source>
                                   │      └─▶ _broadcast(..., source)
                                   │            └ WS_SUBS 源订阅过滤:无人订阅 → 跳过
                                   └─ stdio 重放(50k 行 / 4MB 尾读)─▶ WS 逐行推给前端
                                                                      ▼
                                        [前端 appendLogLine(line)] ─┬ 普通行 → #log-console
                                                                    └ PERF| → handlePerfLine

[任务终态] ─▶ _stop_captures(关句柄) ─▶ _archive_task ─▶ archive/<模块>/<时间>_<脚本>_<设备>/
                                                              ↑ 浏览器 POST chart.png
```

`<模块>` 由 `ARCHIVE_MODULES` 显式映射得出(脚本名 → `wifi`/`perf`/`battery`/`sensor`/`app-launch`/`ir`/`bt`,未登记落 `other`),与 `reports/stress-test/<模块>/` 对齐。采集上限可用环境变量抬高:`PPTP_LOGCAT_MAX_MB`(默认 1024)、`PPTP_SERIAL_MAX_MB`(默认 512)。

**可调常量**(全在 `server.py` 顶部,改行为先 grep 这些名字):`DEVICE_WATCH_INTERVAL_SEC`(5s)/ `ADB_RECONNECT_MIN_INTERVAL_SEC`(20s)—— 设备看门狗的两拍,见 [PITFALLS.md](PITFALLS.md) #31;`LOGCAT_ENABLED`(总开关,置 False 即整体不采 logcat);`LOG_SUFFIX`(三种日志后缀 `.log` / `.logcat.log` / `.serial.log`);`SERIAL_OPEN_TIMEOUT`(3s)/ `SERIAL_RECONNECT_DELAY_SEC`(2s)/ `SERIAL_MAX_RESTARTS`(60);`ARCHIVE_DIR`(`archive/`)。

- **`source = None` 是控制帧**(`state` / `end` / 归档摘要行),发给所有订阅者;`source = stdout|logcat|serial` 是数据帧,只发给订阅了该源的连接。**前端只订阅 stdout**,所以 logcat/serial **根本不上网线**
- **通道行数不进 `/api/tasks`**:公开快照只含 `active/status/restarts/detail/port`(慢变),计数会进 `summary.json` 与那行 console 摘要 —— 否则每 2s 轮询都会让前端全量重渲染(见 [PITFALLS.md](PITFALLS.md) #23)
- `PERF|{json}` 是 perf_monitor / battery_inout_stress 的采样流(前端按 `perfKind` 区分图表类型);**必须带 `type:"sample"` + `clock`**(前端分发硬依赖,缺失则图表全丢 —— 见 [PITFALLS.md](PITFALLS.md) #17)。它与三源采集是**正交**的两条路:PERF 走 stdout,后端零改动

### 4.3 任务结束 → 自动存档

**本节拥有「何时归档、为何归档」**,以及 `_sniff_report_path` 与 `_CLAIMED_REPORTS` 的语义;报告文件内部的格式契约不在这里(见 §4.5)。

```
[_stream_logs 尾部 / 硬停 / 强制清理 / 关服务] ← 四个收尾调用点(同一任务只走其中一个;
        硬停那条靠 t["_finalized"] 让 _stream_logs 让位,见 §4.4)
        ├─▶ _stop_captures(task_id, reason)  ← 必须在前:释放串口 + 关闭日志句柄
        │      (failed/unavailable/skipped/capped 保留自身状态,不被改写成 stopped)
        ├─▶ _archive_task(task_id, reason)   ← 幂等、同步、绝不抛
        │      ├─ 三份日志 Path.replace() 进 archive/<名字>/
        │      ├─ report.json ← _sniff_report_path 从 stdout 嗅探(只取第一行命中),
        │      │   兜底 mtime 扫描并跳过 _CLAIMED_REPORTS 已认领的;必须落在 reports/ 内
        │      ├─ <报告stem>.* ← _companion_files 按「同目录 + 同 stem + 后缀∈白名单」收走
        │      │   白名单 = .csv / .html / .png (v2.14.0 起含 .png —— perf 的温度曲线图)
        │      │   (v2.10.0 起每个脚本都有 .html;主报告改名 report.json,伴随文件保留原名)
        │      └─ summary.json ← 参数/状态/各文件 bytes+lines/report_source/notes
        ├─▶ WS end 帧(带 archive 字段)
        └─▶ console 一行 [archive] 摘要(串口采到没有就靠这一行)
```

**何时归档**:任务一进终态就归档,没有"手动归档"这一步 —— 四个收尾调用点各自触发,靠 `_archive_task` 的幂等性与 `t["_finalized"]` 保证只发生一次。
**为何这样归档**:`logs/` 是运行期工作区(会被清),`archive/` 是给人看的永久产物 —— 所以结束时把三份日志 `move` 走、把报告 `copy` 留下一份。`archive/` 目录名**不含 task_id**(那是给人看的),task_id 在 `summary.json` 里。

**`_sniff_report_path` 的判定**(规则在这里,实现才在代码里):stdout 里**含 `" : "` 且含 `report`** 的行才是候选 —— 取 ` : ` **最后一次**出现的右侧,并要求它以 `.json` 结尾;命中后还要落在 `reports/` 内、跳过 `_CLAIMED_REPORTS` 已认领的。所以报告行必须**恰好一行、且在最后**(见 [REPORT_FORMAT.md](REPORT_FORMAT.md) §8)。**反过来说:任何新打印的路径行都必须避开这个形状** —— perf 的 `[ntc] wrote: <路径>` 刻意不带 `" : "`,因为那条路径里含 `reports/stress-test/perf/`,两者凑齐就成了报告行的候选形状(D-65)。

**`_companion_files` 的判定**(同样在这里):**不是目录 glob**,而是三条**同时**成立 —— 同目录 + 文件名以 `<报告 stem>.` 开头 + **后缀在白名单元组里**(`.csv` / `.html` / `.png`)。所以**新增一种产物类型要动两个地方**:名字要合格,后缀也要进白名单;只做一件就是**安静丢失**(那份文件永远留在 `reports/` 里,而归档看起来一切正常)。见 [PITFALLS.md](PITFALLS.md) #54 与 D-67。

> 归档命名与 `<stem>` 配对规则见 [REPORT_FORMAT.md](REPORT_FORMAT.md) §7。

图表由**浏览器**在任务结束时渲染后 POST 到 `/api/tasks/{id}/archive/artifact` —— 服务端**故意不引入绘图库**(ECharts 只存在于页面里)。代价:结束时浏览器没开就没有 `chart.png`,由 `summary.json` 如实反映。
**这条"服务端无绘图库"从 v2.14.0 起指的是 server.py**:perf_monitor 收尾时会调 `tools/ntc_convert.py`,而那个**独立工具**用 matplotlib 画温度曲线(缺 matplotlib 时只出 CSV 并打一行 `[warn]`,绝不静默跳过)。图经归档白名单进 `archive/`,与浏览器那张 `chart.png` 是两回事、两条路径。

### 4.4 中断 / 硬停
- **中断**:`POST /api/stop/{id}` → 子进程发 CTRL_BREAK(Windows)→ 脚本 `SIGBREAK→KeyboardInterrupt` → 打印已完成汇总 → **报告与 HTML 照常写出**(2026-09-16 起中断不再跳过报告,报告里注明本次被中断)→ 退出 → 状态 `interrupted`。**刻意不杀采集** —— 停机/收尾日志最有价值,由 `_stream_logs` 在真正 EOF 时统一收。
- **硬停**:`POST /api/tasks/force-stop-by-device/{serial}` → `proc.kill()`(SIGKILL)+ 就地归档(SIGKILL 没有 EOF 可等)。**v2.11.1 起它是那条路径上唯一的终结者**:在**第一个 `await` 之前**同步认领 `t["_finalized"] = "force_stop"`,并直接写终态 `interrupted` / `exit_code = -9` / `ended_at`;`_stream_logs` 在 `proc.wait()` 之后看到 `_finalized` 就 **return**,不做第二次终结。**为什么必须这样**:`_stream_logs` 正卡在 `proc.wait()` 上、子进程一死就醒,它会先把任务终结成 `failed`(终态),硬停随后的 `interrupting`(非终态)盖上去,而它已经 `return`,没人再推进 ⇒ 任务永远 `interrupting`,§3.1 的 409 守卫把那台设备**永久锁死**。见 [PITFALLS.md](PITFALLS.md) #48。

### 4.5 报告生成(脚本侧,与平台解耦)

```
脚本已有的局部变量 ──┬──▶ 现有 print(...)        ★ 平台只显示 stdout,一字不改
                     └──▶ rows=[row(...), ...]   ★ 值表达式抄旁边那一句
                                └─▶ build_payload() ─▶ write_html_report(json_path, doc)
                                                              └─▶ <报告同stem>.html
```

**引擎不渲染 stdout,平台也不解析 HTML** —— 两边唯一的接触面是文件系统上的 stem 配对(见 §4.3)。所以报告能力**对平台是零改动的**:`_pptp_report` 是纯 stdlib、import 无副作用、下划线开头(自动 opts out 于脚本发现)。stdout 里 `report :` 行仍是可嗅探的最后一行;新增的 `html =` 行用 `=` 所以不会被误认。完整契约、参数表模型、新脚本接入清单见 [REPORT_FORMAT.md](REPORT_FORMAT.md)。

---

## 5. 脚本契约(scripts/*.py 统一遵守)

1. **独立可跑**:`python -u scripts/x.py --device <serial> [--params <json>]` 能脱离平台 CLI 直接跑
2. **自描述参数**:可选模块级 `PARAMS` 列表 + `--dump-params` 打印 schema(`int/float/bool/str/select`);前端自动渲染参数弹窗
3. **SIGBREAK → KeyboardInterrupt**:平台"中断"按钮能优雅打断并输出已完成汇总
4. **ASCII-only stdout**:不打印非 ASCII,避免 Windows 控制台乱码 / WS 编码问题
5. **报告落盘**:`reports/stress-test/<模块>/<name>_<dev>_<ts>.json`(已 gitignore)。**v2.10.0 起每个脚本还要写一份同 stem 的中文 HTML** —— 走共享引擎 `_pptp_report.write_html_report(json_path, doc)`,路径由引擎从 JSON 路径推导(**不要自己拼串**:stem 逐字相同是归档能收到它的唯一条件)。stdout 里 `report :` 行必须**仍是最后一行**(`_sniff_report_path` 只取第一行命中);新增的 `html =` 行必须用 `=`。完整规则见 [REPORT_FORMAT.md](REPORT_FORMAT.md);`ir_runner.py` 无报告(无 PASS/FAIL 语义)。
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

- **全局单日志面板**:整个平台只有**一个** `#log-console` + `#log-info` + `#perf-chart`,全部绑 `state.currentTaskId`;设备卡/任务卡只是"把 currentTaskId 指到哪"的入口。**不残留**:WS 帧按 `currentTaskId !== ws._taskId` 过滤 + 切任务 dispose/重建图表 + 每任务独立缓冲 `perfSamples[taskId]`
- **只显示 stdout,永久(v2.6.0 采集 / v2.7.0 定案)**:logcat 与串口在服务端照采、照归档,**前端永不展示**。v2.6.0 曾预埋的通道显示方案(`CHANNELS` / `TABS_ENABLED` / `activeSource` / 计数 pills / `#channel-bar`)已在 v2.7.0 **整体删除** —— 用户判断这两种日志体量太大、不值得展示,要求"稳定简单为主导"。**别再把它加回来。**
- **图表回传**:任务结束时浏览器用 `exportFullChartDataUrl` 渲染 PNG 并 POST 进存档(见 §4.3)。触发点两处:WS `end` 帧(正在看该任务时)+ `refreshTasks` 轮询扫描(覆盖"任务在别的视图下结束"与"跑完才打开页面")
- **串口采集是运行期 opt-in**:设备卡 `.device-row4` 的 checkbox + COM 下拉;端口持久化、勾选**不**持久化;脚本自己声明了 `serial_port` 时控件禁用并给中文提示(平台让位)
- **性能图表**:`#perf-chart`(200px)挂日志面板头部;ECharts `type:"time"` 墙钟横轴(**固定 1h 滚动窗口**,`interval:10min` + `splitNumber:6`,右缘跟随最新样本)、双 Y 轴、导出三件(txt + csv + **全程图 png**)。类型按脚本泛化(`perfKind`):`perf` 类画 CPU/GPU/MEM/前台APP 四线,`battery` 类画电量(左 0-100%)+ 温度(右 °C)。导出的 png 是**全程图**(离屏渲染 `perfSamples` 全量,0.5h 一刻度,不受屏幕窗口限制;v2.5.1)。**控制台输出全英文/ASCII**(v2.5.1,UI 文案仍中文)
- **合成设备卡**:`renderDevices()` 为"不在 `adb devices` 但仍有意义"的设备合成卡 —— **临时离线卡**(有 running 任务 / reboot 压测,琥珀虚线)+ **放电关机卡**(电池测试终态 + 设备关机,`_batt_off`,红色虚线 + "设备已关机"徽标,v2.5.1)。二者都算"非离线"可交互;设备重新上线自动回正常卡
- **状态持久化**:`localStorage` key `pptp.deviceState.v1` 只存已选设备 / 每设备脚本 / 序列 / 参数 / 串口口;设备与任务数据总从 server 拉
- **轮询节奏**:设备 3s / 任务 2s / 服务 5s / 时钟 1s;**图表逻辑只在离散导航点调用,绝不进轮询**

---

## 7. 目录结构

> 下面是**描述性**的骨架,不是穷尽清单(新增文件不必回头补)。`archive/`、`logs/`、`reports/`、`temp_template/` 都是 **gitignore 的工作区**,不进版本库。

```
E:\ProjectorPressureTest\
├── server.py                       # FastAPI 后端:所有 API + WS + 三源采集引擎 + 自动存档(单文件)
├── start.bat                       # 启动 launcher(成功自动关闭,失败保留信息)
├── server_window.ps1               # PPTP-Server 窗口:显示 uvicorn 日志并写文件
├── stop.bat                        # 停止服务
├── CLAUDE.md / README.md / CHANGELOG.md / TODO.md
├── docs\                           # 给 agent 的文档(分工见 §8)
├── scripts\                        # 平台调用的压测脚本(契约见 §5,全部 PARAMS 可配)
│   ├── _pptp_report.py             # ★ v2.10.0 共享报告引擎。下划线开头 → 平台不当可跑脚本;
│   │                               #   纯 stdlib、import 无副作用(`--dump-params` 短路不受影响)
│   ├── ir_runner.py                # 红外序列循环(自包含 IRRemote,默认无限循环;长按 = down+hold+up)
│   ├── wifi_onoff_stress.py        # WiFi 开关压力(关/开循环 + wpa_cli 扫描统计)
│   ├── wifi_reboot_stress.py       # 重启 + WiFi 重连压力
│   ├── wifi_switch_stress.py       # 多网络循环切换(预置 WIFI_NETWORKS)
│   ├── sensor_reboot_stress.py     # 重启 + 传感器(gsensor/ToF)回连压测(含 select 下拉框)
│   ├── app_launch_stress.py        # APP 冷/热启动耗时(APP_PRESETS 内置 3 个一起跑;
│   │                               #   am start -W TotalTime + logcat Displayed 交叉验证)
│   ├── perf_monitor.py             # 性能监控 v2(三层分级采样 + nonce 帧 + 证据累积 + 收尾判定块);
│   │                               #   PERF| 流 → 前端图表;归档 report.json + samples.csv + events.csv
│   ├── battery_inout_stress.py     # 电池充/放电压测(电量/温度/电压,串口开 health 轮询;全英文 ASCII)
│   └── bt_reboot_stress.py         # 重启 + 蓝牙音箱 A2DP 回连压测(reboot → 上线 → 轮询回连;
│                                   #   判定 = 适配器开 + A2DP CONNECTED)
├── ir_sequences\                   # 用户可编辑的 .ini 序列文件 + 按键速查
│   ├── *.ini                       # 序列文件(如 1.ini / idle_keepalive.ini)
│   └── KEY_REFERENCE.md            # 按键速查:KEY_* / KEYCODE_* 名 ↔ 键码
├── static\
│   ├── app.js                      # 前端所有逻辑(IIFE)
│   ├── index.html                  # 4 面板 + 2 模态的单页
│   ├── style.css                   # 暗色主题
│   └── vendor\echarts.min.js       # ECharts 5.5.1 本地化(无构建步骤)
├── reports\
│   └── stress-test\<模块>\         # 脚本报告原始落点(脚本契约;平台复制一份进 archive/)。gitignore
├── logs\                           # 运行期临时工作区,任务一结束内容就搬走。gitignore
│   ├── server.out.log              # uvicorn stdout
│   ├── server.err.log              # uvicorn stderr
│   └── <stem>.<source><suffix>     # 运行中每任务每通道一份
│       stem   = <task_id>_<yyyyMMdd-HHmmss>_<script-stem>_<device>
│       source = stdout | logcat | serial
├── archive\                        # ★ 永久保留,按模块分类。gitignore
│   └── <模块>\{wifi,perf,battery,sensor,app-launch,ir,bt,other}\
│       └── <yyyyMMdd-HHmmss>_<script-stem>_<device>\
│           ├── stdout.log / logcat.log / serial.log   — 从 logs/ 移入
│           ├── report.json         — 脚本自己写的那份的副本(主报告改名,多份时 report-2.json…)
│           ├── <报告stem>.html     — ★ v2.10.0 起每个脚本都有(伴随文件,**不改名**;ir_runner 无)
│           ├── chart.png           — 浏览器渲染回传(perf/battery 才有,且要求结束时浏览器开着)
│           └── summary.json        — 清单:task_id/参数/状态/bytes+lines/report_source/notes
├── data\                           # 运行期小状态(server.pid;TASKS_FILE 路径已声明但当前未写入)
└── temp_template\                  # 第三方参考代码(同事的监控脚本),只作本地参考,不入库。gitignore
```

> `archive/` 目录名**不含 task_id** —— 那是给人看的。task_id 在 `summary.json` 里。
> 模块由 `ARCHIVE_MODULES` **显式映射**(脚本名 → 模块),不靠文件名猜;未登记的落 `other/`;模块名与 `reports/stress-test/<模块>/` 对齐。

> **优先看的文件**:`server.py` + `static/app.js` + `static/index.html` + `static/style.css`。其余均为辅助。

---

## 8. 相关文档(docs/ 分工)

| 文档 | 回答什么问题 |
|---|---|
| [README.md](../README.md) | 用户向:怎么跑、每个脚本干什么、坏了怎么办 |
| [CLAUDE.md](../CLAUDE.md) | 会话规则(自动载入,<100 行) |
| [CHANGELOG.md](../CHANGELOG.md) | 版本编年 |
| [TODO.md](../TODO.md) | 已知未决问题(编号冻结) |
| [START-HERE.md](START-HERE.md) | 我刚接手、先读哪、SOP |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 本文 —— 系统的形状、契约、不存在的东西 |
| [FRONTEND.md](FRONTEND.md) | UI 交互模型 |
| [PITFALLS.md](PITFALLS.md) | 踩过的坑与规矩 |
| [DECISIONS.md](DECISIONS.md) | 已批准、不可回退的设计决策 |
| [REPORT_FORMAT.md](REPORT_FORMAT.md) | 报告契约(名与 §8 冻结) |
| [PERF_MONITOR_V2.md](PERF_MONITOR_V2.md) | perf_monitor 的采样/判定模型(名与 §12.7 冻结) |
| [ir_sequences/KEY_REFERENCE.md](../ir_sequences/KEY_REFERENCE.md) | 按键名 ↔ 键码 |

---

## 9. 不存在的东西(防幻觉清单)

**读到这里请先默认这些都不存在** —— 如果用户或你自己的推理提到它们,先 `grep` + `Read` 求证,再决定要不要动手:

- ❌ **数据库**(SQLite / PostgreSQL) —— 全是内存 + 文件,没有 ORM、没有迁移
- ❌ **用户认证 / 登录 / 会话** —— 无鉴权
- ❌ **多用户 / 多租户** —— 单用户单机
- ❌ **远程控制 / 远程访问** —— 全本地,`127.0.0.1`
- ❌ **HTTPS** —— 纯 HTTP
- ❌ **Docker / 容器化**
- ❌ **WebSocket 自动重连框架** —— 唯一的重连是 log WS 里手写的指数退避(1s…5s,最多 5 次),只在用户仍看着该任务时生效;别处没有
- ❌ **任务调度 / 定时任务(cron)** —— 任务只由用户手动触发
- ❌ **报告 / 统计 / 历史趋势分析** —— 报告是脚本自己写出的 HTML,平台不解析、不聚合、不画趋势
- ❌ **邮件 / 通知 / 告警推送**
- ❌ **服务端图表渲染** —— ECharts 只存在于浏览器页面里,`chart.png` 是浏览器渲染后 POST 回传的(见 §4.3)
- ❌ **插件 API / 脚本注册表** —— 平台自动发现 `scripts/*.py`,零注册,没有插件机制
- ❌ **任何 JavaScript 框架(React / Vue / …)或构建工具**,也**没有 TypeScript**
- ❌ **任何 npm 依赖 / `package.json`** —— 前端就是几个静态文件
- ❌ **Tailwind 或任何 UI 框架** —— 手写 `style.css`
- ❌ **任何 `/api/ir/keys` 端点** —— 原 ir_runner 的 keys 接口已被移除
- ❌ **序列模态里的 step 编辑**(code / kind / delay / count 行)—— 之前做过,已废弃
- ❌ **"在设备上运行"式的多设备同时启动 UI 流程** —— 每台设备各自手动跑
- ❌ **"上次已保存的序列"自动 fallback 选择**
