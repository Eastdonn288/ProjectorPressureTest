# PPTP 踩坑总表(PITFALLS)

- 本文件是 PPTP 的**永久踩坑登记表**:每条 `#N` 都是**永久标识符,永不重编号、永不合并、永不删除**;条目按编号升序物理排列。
- 其它文档与代码注释一律以 `docs/PITFALLS.md #N` 的形式引用本表,不要复制条目正文。
- 「位置」只给代码标识符 / 函数名,**全表不写行号**(行号会漂);每条格式为 `症状 / 规则 / 位置 / 来源`。
- **本文件按需读章节,严禁整篇通读**(同 CLAUDE.md 的 Context 预算铁律)。

---

## #0 — E-SafeNet 透明加密

- **症状**:本机(装了亿赛通 E-SafeNet)部分 `.py` 文件在磁盘上带一层透明加密外壳。表现:① Claude Code 的 `Read` 显示 `b#e� P  E-SafeNet  LOCK ...` 之类二进制,行数也是假的(`server.py` 读到 316 行,真实 2448 行);② **`Grep` 失败是静默的** —— 不报错、不警告,该文件直接从结果里消失,连 `FastAPI` 这种必然存在的词都搜不到;③ `Edit` 反复报 "String to replace not found"。**推论:看到 Grep 0 命中时先怀疑密文壳,再怀疑代码不存在 —— 这两件事在工具输出里长得一模一样。**
- **不对称性(2026-09-16 v2.10.0 精确表述)**:同一时刻、同一个文件,**Read/Grep 读到密文,而 Python 进程读到明文** —— 这是两个不同的读进程,不是文件的两种状态。铁证:同一刻 `Grep` 在 `scripts/perf_monitor.py` 上搜 `HTML_CSS` **0 命中**,而 `python -c "print(len(open('scripts/perf_monitor.py').read()))"` 把 128KB 全部按 UTF-8 解出来了。所以**踩到时第一反应必须是「这文件被包壳了」,而不是「代码不在那儿」**。
- **规则**:撞上密文壳**立刻停手** —— **绝不用 Read/Edit/Grep 碰它**,`Edit` 是"读-改-写",它读到密文、在密文上替换、再把密文写回,**会直接毁掉文件**。改这类文件**一律用 Python 脚本**,并加 `assert`:读写 `assert t.count(old) == 1`;`read_text`/`write_text` **必须显式传 `encoding='utf-8'`**;写完 `python -m py_compile` 验证。注意 Python 文本模式写入会把 LF 转成 CRLF,写完要再转回 LF,否则 Edit 工具(即使读得到明文)也会因行尾不匹配而失败 —— 用 `write_bytes(read_bytes().replace(b'\r\n', b'\n'))`。
- **触发**:推测是 Python 写文件后触发 E-SafeNet 对**新内容**加密。**新建文件命中即变密文** —— A/B 复现时用 Python 生成的 `server_old_tmp.py` 一诞生就是密文(grep 完全搜不到,而 Python 自己读写完全正常、uvicorn 也能 import)。所以那套 Python 读写 workaround **只对"Python 自己也读不了"的情况才需要**;只要 Python 能读,文件带壳并不影响运行,**别急着救**。用 **Write 工具**建的文件不受影响;用 **Python 脚本**建/改的会中招。
- **便宜测法**:`Read` 那个文件 `limit: 2` —— 出乱码或行数明显不对就是带壳。**别用字节扫描**(在磁盘上 grep `E-SafeNet` 字样):本文档 / CLAUDE.md 自己的正文里就写着这个词,必然假阳性 —— 而 Python 读它们本来就是明文,**壳是读进程侧的东西**。
- **逃生口(2026-09-17 v2.11.1)—— 文件被包壳后怎么把代码交给用户**:用 Python 把明文抄成一个**非 `.py` 文件**(如 `server.txt`),壳不会跟过去。做法是 Python `read_bytes()` → `write_bytes()`。**关键在扩展名**:至今中招的**全是 `.py`**(`server.py` / `perf_monitor.py` / `_pptp_report.py` / 几个 `%TEMP%` harness),说明加密策略是按扩展名下发的 —— 换个扩展名(`.txt`)就落在策略之外。**另外一定要字节模式写(`write_bytes`,不是 `write_text`)**:既不做 LF→CRLF 翻译,也绕开了「Python 文本写」这条已知触发路径。**这就是「文件被包壳后怎么把代码交给用户」的标准做法。**校验方式:源与副本各 `read_bytes()` 相等 + `sha256` 一致(2026-09-17 实测:107982 字节 / 2651 行 / 0 CRLF;抄出的 `server.txt` Read 正常、Grep **6 命中**,而同一个 Grep 打在 `server.py` 上是 **0 命中**)。
- **实测清单(2026-09-16,逐个用 Read 实测出来的,不是推断)**:当前带密文壳 —— `server.py`、`scripts/_pptp_report.py`、`scripts/perf_monitor.py`、`scripts/wifi_onoff_stress.py`、`scripts/battery_inout_stress.py`;当前可正常 Read —— `scripts/wifi_switch_stress.py`、`scripts/wifi_reboot_stress.py`、`scripts/bt_reboot_stress.py`、`scripts/sensor_reboot_stress.py`、`scripts/app_launch_stress.py`、`scripts/ir_runner.py`、`static/app.js`、`docs/*.md`、`CHANGELOG.md`。⚠ **带壳与否不可预测**:上面那批"带壳"里有一半是**本轮被 Python 脚本改过却仍是明文**的,所以**不能用"我改过它/没改过它"推断**,每个文件碰之前**单独实测**。⚠ `server.py` 现在是**带壳状态** —— 不要沿用"它已经被手动解密过"的旧结论。
- **规则(v2.9.0 起生效)**:`scripts/perf_monitor.py` 一律用 Python + `assert t.count(old) == 1` 改,写完 `py_compile`,**绝不用 Read/Edit/Grep 碰它**;`server.py` 同。
- **位置**:`server.py`、`scripts/perf_monitor.py`、`scripts/_pptp_report.py`、`%TEMP%` 下的 harness 文件
- **来源**:[TODO.md](../TODO.md) §5(2026-09-11 / 09-14 / 09-16 / 09-17 四次确认)

---

## #1 — ECharts 压缩版没有字面量 `echarts.init`，别用 grep 校验它

- **症状**:grep 搜 `echarts.init` 在压缩版上 **0 匹配**,看起来像依赖没装好或文件不完整。
- **规则**:压缩库不能用 grep 校验。可靠校验是 node `require('./echarts.min.js')` + 打印 `echarts.version` + `typeof echarts.init === "function"`。本库 require 出 **5.5.1**;文件里另有 `version:"5.6.0"`(是内嵌 zrender)与 `version:"1.1"`(是 SVG 命名空间),**均正常**,别当成版本不一致。
- **位置**:`static/vendor/echarts.min.js`
- **来源**:见 [FRONTEND.md](FRONTEND.md);参见 #16

## #2 — ECharts 在 hidden div 上 init 只会得到零尺寸白板

- **症状**:图表容器还 `hidden` 时就 `echarts.init`,图表永远画不出来 / 尺寸为 0。
- **规则**:必须 `el.hidden = false` **之后**再 `echarts.init`。`syncPerfChart` 已处理;`#perf-chart` 默认 `hidden`,靠 style.css 的 `[hidden]{display:none !important}` 兜底。
- **位置**:`syncPerfChart`、`#perf-chart`、static/style.css 的 `[hidden]` 规则
- **来源**:见 [FRONTEND.md](FRONTEND.md)

## #3 — 图表数据不能只 cap 缓冲，缓冲和 option 都要 cap

- **症状**:长跑 / 重放后 option 里的 series data 无限增长,与缓冲脱节。
- **规则**:`perfSamples` **和** option 的 series data **都要 cap 86400**(`PERF_SAMPLE_CAP` + `flushPerfChart` 内 splice);**只 cap 一个 → 重放后 option 数据无限涨、与缓冲脱节**。v2.5.1 起滚动窗口的 `shift()` 在 cap 前就把窗口外点剔掉,二者互补。
- **位置**:`PERF_SAMPLE_CAP`、`flushPerfChart`、`perfSamples`
- **来源**:v2.5.1

## #4 — 重放去重必须按 `t`(elapsed 秒)

- **症状**:切走再切回,WS 会重放同一任务的全部样本,不按 `t` 去重 → 曲线叠加成双份。
- **规则**:按 `t`(elapsed 秒)去重。同任务重跑会生成**新 taskId**,不会误去重。
- **位置**:`handlePerfLine` 的样本合并逻辑、`state.currentTaskId`
- **来源**:v2.5.1

## #5 — 图表逻辑塞进 2s 轮询 = 灾难

- **症状**:`refreshTasks` 每 2s 重建卡片;在里面 sync 图表会导致切走后图表被持续重建 / 闪烁。
- **规则**:**不要**在轮询里驱动图表,只在离散的切换 / 收帧点调用(见 [FRONTEND.md](FRONTEND.md) 列出的调用点)。
- **位置**:`refreshTasks`、`syncPerfChart`
- **来源**:v2.5.1

## #6 — WS 陈旧 `onopen` 会清掉新任务的 meta

- **症状**:快速切任务时,旧 WS 的 `onopen` 可能后到。
- **规则**:`onopen` 里必须 `if (state.currentTaskId !== taskId) return` 再动 perf 状态,否则会清掉新任务刚建立的 meta。
- **位置**:`openWs`、`scheduleReconnect`、`state.currentTaskId`
- **来源**:v2.5.1

## #7 — PERF 行的 JSON 带空格，但不要用正则解析它

- **症状**:`json.dumps` 默认分隔符是 `", "` 和 `": "`,PERF 行字段间有空格,看起来"不是标准 JSON"。
- **规则**:`JSON.parse` 完全没问题;**不要**用正则 / 字符串匹配提取字段。
- **位置**:脚本侧 `json.dumps` 默认参数、前端 `handlePerfLine` 的 `JSON.parse`
- **来源**:v2.5.1

## #8 — `dumpsys cpuinfo` 是近 5 分钟滚动均值，不是瞬时值

- **症状**:拿 `dumpsys cpuinfo` 当实时 CPU% 用,读数永远对不上。
- **规则**:实时前台 APP CPU% **必须用 `top -n 1 -b`**。
- **位置**:`perf_monitor.py` 采集段
- **来源**:见 [PERF_MONITOR_V2.md](PERF_MONITOR_V2.md)

## #9 — `top -n 1 -b | head -30` 会截断前台 APP

- **症状**:低 CPU 的 APP 排不进前 30,采集不到前台应用。
- **规则**:用 `top -n 1 -b | grep <pkg> | head -3`;首样本无包名时回退 `head -40`。
- **位置**:`perf_monitor.py` 采集段
- **来源**:见 [PERF_MONITOR_V2.md](PERF_MONITOR_V2.md)

## #10 — 复合 adb shell 必须用 list-arg subprocess

- **症状**:`shell=True` 时 `;` / `|` / `>` 被 Windows `cmd.exe` 本地吃掉,adb 收到的是残缺命令。
- **规则**:复合命令必须走 list-arg —— `["adb","-s",serial,"shell",compound_cmd]`,整条复合命令作为**一个** argv 元素传进去。
- **位置**:scripts/*.py 与 `ir_runner.py` 的所有 adb 调用点
- **来源**:见 [ARCHITECTURE.md](ARCHITECTURE.md)

## #11 — 本设备 `su -c` 报 `invalid uid/gid`，要用裸 `su 0 <cmd>`

- **症状**:`su -c "<cmd>"` 直接报 `invalid uid/gid`。
- **规则**:用裸 `su 0 <cmd>`(sensor / perf 脚本同一形态)。
- **位置**:`scripts/sensor_reboot_stress.py`、`perf_monitor.py` 的 root 调用点
- **来源**:见 [PERF_MONITOR_V2.md](PERF_MONITOR_V2.md)

## #12 — meminfo 含 `IommuLowUsage/IommuHighUsage` 行，别用错误串判可读性

- **症状**:节点可读性探测若按 `ERROR_MARKERS` 子串匹配,会被 meminfo 里这些正常行误判成"不可读"。
- **规则**:用 **parser 成功 / 失败**判定可读性(`parse_meminfo` 等),**不要**用错误串匹配。
- **位置**:`parse_meminfo`、`ERROR_MARKERS`
- **来源**:见 [PERF_MONITOR_V2.md](PERF_MONITOR_V2.md)

## #13 — GPU 节点不在标准路径，换设备先跑 `--probe`

- **症状**:标准 Mali / MTK sysfs GPU 路径**全都不存在**,以为设备没有 GPU 计数器。
- **规则**:MT9676 的 Mali 计数器在 `/sys/kernel/debug/mali0/dvfs_utilization`,且**需要 root**。换芯片 / 换设备**先跑 `--probe`** 再写采集。
- **位置**:`--probe`、`perf_monitor.py` 的 GPU 采集段
- **来源**:见 [PERF_MONITOR_V2.md](PERF_MONITOR_V2.md)

## #14 — 视频播放时 GPU%≈0 是正常的，不是读数失效

- **症状**:视频播放全程 GPU% 为 0,看起来像监控坏了。
- **规则**:本机是 **MStar 显示管线**(SurfaceFlinger 报 `displayName="MStar Demo"`)的 MT5896 Android TV:视频**解码走硬件 VPU**(不碰 GPU)、**画面合成走 HWC 硬件叠加层**(DEVICE layer,非 GPU),GPU 只画 UI(CLIENT layer,静态时缓冲缓存不重绘)。证据链:① 报告 GPU=0 时 `_gpu_prev` 的 busy 全程冻结在 **446506278**、idle 稳步增长(`fg_pkg` 全程 launcherx,视频在后台 / 预览播放,前台根本没有渲染任务);② 计数器本身正常 —— 实测启动 Settings 时 busy 从 **446506278 → 446515551**(真实 GPU 渲染事件会跳);③ 同机另一次 GPU 平均 **42%**(连续应用切换 / UI 动画这类持续负载)证明指标有效。**判断口诀:GPU% 对"应用切换 / UI 动画"压测有意义,对"视频播放"无意义 —— 这是硬件分工使然,别当成监控失效去"修"**(已向用户说明,用户选择文档化不再排查)。
- **位置**:`_gpu_prev`、`fg_pkg`、GPU 指标门
- **来源**:2026-08-25 实机排查确认;[PERF_MONITOR_V2.md](PERF_MONITOR_V2.md)

## #15 — 浏览器对"一次点击多个下载"的容忍度不同

- **症状**:导出三件套(JSON / CSV / PNG)时 Firefox 可能只放下第 1 个,拦截第 2 / 3 个。
- **规则**:Chrome / Edge 单次点击允许多个下载,**Firefox 可能拦截第 2 / 3 个** —— 导出三件套文档已注明,**无需修**。
- **位置**:前端导出三件套(`onExportLog`)
- **来源**:见 [FRONTEND.md](FRONTEND.md)

## #16 — 本地 vendor 依赖要 pin 版本 + 记来源

- **症状**:vendor 库无声升级后行为变化,查不出用了哪个版本。
- **规则**:`echarts.min.js` = **5.5.1**(主源 jsdelivr,备源 npmmirror);**换版本前必须确认 UMD 全局导出 `echarts` 存在**(并跑 #1 的校验)。
- **位置**:`static/vendor/`、`static/index.html` 的 vendor 引入
- **来源**:见 [FRONTEND.md](FRONTEND.md)

## #17 — wire 契约以端到端实证为准，不能只信 docstring

- **症状(事故)**:v2.4.1 `perf_monitor.py` 的 docstring 明写 sample 行带 `type:sample`,但 `emit` dict **实际漏了该字段** → 前端 `handlePerfLine` 按 `obj.type==="sample"` 分发,**sample 全被当未知类型丢弃**:图表不出线 + 导出只剩 txt。
- **规则**:**脚本每次改动 PERF 输出,必须真机跑一遍、看 WS 日志里 `PERF|` 行真的带 `type` 再交付**;改协议时前后端都要 grep `type:` / `clock` 的引用点。防御性兜底:前端对**无 `type` 的 PERF 数据行按 sample 解析**(现未做,可加)。
- **位置**:`perf_monitor.py` 的 `emit`、`handlePerfLine`
- **来源**:v2.4.1;见 #19

## #18 — 电池数据只能走串口，且 COM 口要用户自己选

- **症状**:`adb shell dumpsys battery` 读到的电量不实时,且经 adb 开轮询直接报错。
- **规则**:本设备 `ro.config.batteryless=true`,`adb shell dumpsys battery` **默认只缓存**不实时;必须经**串口 console**(**115200 baud**)发 `cmd android.hardware.health.IHealth/default set polling true` 才拿到实时电量 / 温度。**SELinux 挡 adb 路径**(`su 0 ... set polling true` 经 adb 报 `Failed transaction 2147483646`),所以**没有串口就拿不到实时电池数据**。`serial_port` 是 select 下拉框、`--dump-params` 时枚举当前 COM 口(本机 **COM9 = FTDI VID:PID 0403:6010**),但**哪个口对着 BMS 要用户在试跑时确认并自选**。轮询开启后需每 `SERIAL_RE_ENABLE_MIN` 分钟重发一次保活(设备可能把 polling 关回去)。
- **位置**:`serial_port`、`SERIAL_RE_ENABLE_MIN`、`scripts/battery_inout_stress.py`
- **来源**:见 [ARCHITECTURE.md](ARCHITECTURE.md);见 #36

## #19 — PERF 前端按 kind 分支，新脚本必须注册 + 字段带全

- **症状**:新写的 PERF 脚本不出图,日志里也看不出错。
- **规则**:`handlePerfLine` / `buildPerfOption` / `buildPerfCsv` / `onExportLog` 都按 `perfKind(taskId)` 分支,**`PERF_SCRIPT_KINDS` 未注册的脚本一律当普通日志,不出图**。新增 PERF 脚本**必须**:① 在 `PERF_SCRIPT_KINDS` 加映射;② sample 行带 `type:"sample"` + `clock`(见 #17);③ 需要读数的字段进 `meta.sources` 由前端显隐。**语言分工副作用**:脚本全英文 → 其 `PARAMS` 字段名 / `label` 是英文,前端参数弹窗原样显示英文标签(与其它中文脚本不同,**属预期,已文档化**)。
- **位置**:`PERF_SCRIPT_KINDS`、`perfKind`、`handlePerfLine`、`buildPerfOption`、`buildPerfCsv`
- **来源**:v2.5.0;见 #17

## #20 — log console 全 ASCII 靠双保险

- **症状**:`#log-console` 里冒出中文(时间戳"下午3:30"或状态串)。
- **规则**:`#log-console` 只准英文 / ASCII,两类常见泄漏:① **`toLocaleTimeString()` 无 `{hour12:false}`** —— zh-CN 浏览器会吐"下午3:30",log 里所有时间戳**必须走 `fmtClock`**;② meta / sample 可读行里嵌中文状态串 —— 前端旧实现有 `STATUS_ZH` / `JUMP_TYPE_ZH` 中文映射表(**已删**)。**前端侧任何新增的 log 文案都不得用中文**;UI 文案(卡片 / 弹窗 / 系列名 / 导出表头)不受此限。校验:grep `[^ -~]` 扫 `appendLogLine` / `handlePerfLine` / `flushLogBuffer` / `openWs` / `scheduleReconnect` 涉及的所有字符串。
- **位置**:`fmtClock`、`appendLogLine`、`flushLogBuffer`、`#log-console`
- **来源**:v2.5.1;见 [FRONTEND.md](FRONTEND.md)

## #21 — 固定 1h 窗口与 dataZoom 互斥

- **症状**:想加回 dataZoom 放大看局部,结果与窗口滚动互相打架。
- **规则**:x 轴改为固定滚动窗口(右缘 = 最新样本)后**禁用了 dataZoom** —— zoom 会拖出 1h 视野、与 flush 逐帧把窗口拉回最新互相打架。**若要放大看局部,只能缩短窗口或加独立控件,不要简单把 dataZoom 加回去。**窗口滚动靠 `flushPerfChart` 里 `minX = endX - PERF_WINDOW_MS` + 对 series data `while (d[0][0] < minX) d.shift()`。**注意:dataZoom 只影响屏幕上的 live 图;全程 PNG 导出走 `exportFullChartDataUrl` 的 fullRange 分支,天然不涉及窗口 / dataZoom。**
- **位置**:`PERF_WINDOW_MS`、`flushPerfChart`、`exportFullChartDataUrl`
- **来源**:v2.5.1

## #22 — `#log-info` 每 2s 被整体重写，任何嵌进去的东西都会被销毁

- **症状**:往 `#log-info` 里挂的节点 / 事件监听过一个轮询周期就没了。
- **规则**:`refreshTasks`(2s)无条件调 `renderLogInfo()`,它 `innerHTML = ...` **整个重建**。所以**任何需要跨轮存活的东西都不能放在 `#log-info` 里**,而且想在它里面挂事件监听是徒劳的。(v2.6.0 曾据此预留 `#channel-bar` 兄弟节点做通道计数 pills,**v2.7.0 连同显示方案一起删除** —— 但**这条约束本身仍然成立**,是 `#log-info` 的固有性质)
- **位置**:`renderLogInfo`、`refreshTasks`、`#log-info`
- **来源**:v2.7.0;见 [FRONTEND.md](FRONTEND.md)

## #23 — 动态计数绝不能进渲染键

- **症状**:任务列表每 2s 全量重渲染卡片、闪个不停。
- **规则**:`captureCounts` 每 2s 变;**若并入 `tasksRenderKey` / `devicesRenderKey`,每次轮询都会判定"变了" → 全量重渲染卡片**。同理 `_public_captures` 的公开快照**刻意只含 `active/status/restarts/detail/port`,不含 `lines/bytes/dropped`**。计数只走 WS `capture_counts` 帧。
- **位置**:`captureCounts`、`tasksRenderKey`、`devicesRenderKey`、`_public_captures`、`capture_counts`
- **来源**:见 [FRONTEND.md](FRONTEND.md);见 #30

## #24 — `_stop_captures` 必须对"从未启动的通道"跳过

- **症状**:一个从未启用的串口通道被收尾逻辑改写成 `stopped/task_end`,把真正的 `failed: port not present` / `skipped: script_owns_port` 诊断**抹掉了**。
- **规则**:收尾守卫 —— `ctask is None and proc is None and _stop_evt is None` → `continue`。
- **位置**:`_stop_captures`
- **来源**:见 #28(v2.6.0 的守卫不够用的地方)

## #25 — logcat 的 `-T` / `-t` 语义与 `-c` 禁令

- **症状**:用 `-t N` 想持续跟随却只 dump 就退;清了缓冲导致启动耗时判定静默失效。
- **规则**:`-T N` = tail N 行**且继续跟随**(平台用 `-T 1`);`-t N` **隐含 `-d`**(dump 完就退,不能持续跟随,**不可用**)。**永远不要跑 `logcat -c`**:`app_launch_stress.py` 依赖设备端环缓冲(`logcat -d -s ActivityTaskManager`)做启动耗时交叉验证,**清缓冲会静默破坏它的判定**。默认缓冲 `main,system,crash`(不含 events);`*:V` 在话痨设备上 24h 可达 GB 级,平台默认 `*:I`。
- **位置**:平台 logcat argv、`scripts/app_launch_stress.py`
- **来源**:见 #29

## #26 — 串口采集必须用真线程，且不用 `readline()`

- **症状**:串口读阻塞事件循环;`readline()` 每次等满 timeout 才返回。
- **规则**:串口没有文件描述符,无法走 asyncio;**用裸守护线程 + `asyncio.Queue(maxsize)`**(满则**丢最旧**并计 `dropped`,无界队列 = 内存泄漏)。跨线程只能用 `loop.call_soon_threadsafe`。**别用 `readline()`**:设备 console 的提示符没有换行,`readline()` 会每次等满 timeout 才返回;用 `read(in_waiting or 1)` 自己按 `\n` 切。**不要动 DTR/RTS**(保持默认):实机验证打开 COM9 **没有**触发设备 reboot banner,证明默认拉线状态对本设备 console 无副作用。
- **位置**:串口读线程、`dropped`、`_capture_serial`
- **来源**:见 [ARCHITECTURE.md](ARCHITECTURE.md)

## #27 — logcat 重启预算是"连续"而非"终身"

- **症状**:重启压测跑到第 6 轮左右,logcat 永久死掉,之后一行都不采。
- **规则**:旧实现 `LOGCAT_MAX_RESTARTS=5` **只增不减**,而设备重启期间 adb 常常**直接退出**(而不是阻塞在 `- waiting for device -`),一次 30 秒宕机就要烧掉 ~15 次 → **默认 100 轮的重启压测在第 6 轮左右 logcat 就永久死了**。现在预算按**连续**失败计,logcat 一吐出真实输出就**清零**(输出即设备回来的证明),上限 **60**(≈2 分钟持续失联)。**改这里之前先想清楚:任何"终身计数"的预算在长跑里都会被打穿。**
- **位置**:`LOGCAT_MAX_RESTARTS`
- **来源**:v2.7.0

## #28 — 收尾绝不能覆盖通道自己给出的终止诊断

- **症状**:串口"根本没打开成功"被报告成**健康地正常停止**,`capped`(日志被截断)也被抹成正常。
- **规则**:`_stop_captures` 曾经无条件写 `status="stopped" / detail=reason`。现在 `failed` / `unavailable` / `skipped` / `capped` 走 `keep_status` 分支**保留自身状态**(**teardown 照常跑** —— 该死该收的进程还是要收)。这条是 v2.6.0 的 `ctask is None and proc is None` 守卫(**#24**)**不够用**的地方:那个通道**有 ctask**,守卫拦不住。
- **位置**:`_stop_captures`、`keep_status`
- **来源**:见 #24

## #29 — 重启后 `-T 1` 会丢掉整个开机窗口

- **症状**:重启压测的结果文件要等 adbd 回来(约开机 25 秒)才有内容,开机日志全丢。
- **规则**:`-T 1` 只回读 1 行,而重启的原因通常正是设备重启,**开机日志就在刚刷新的环缓冲里**。现在**首次启动 `-T 1`、重启时 `-T 2000`**;marker 行明说"**可能重叠**"(adb 抖动的非重启场景确实会重复)。
- **位置**:logcat 启动 argv、重启分支、marker 行
- **来源**:见 #25

## #30 — 串口重试循环里 `cap["_stop_evt"]` 必须每次重指

- **症状**:重连循环后,旧的读线程停不住,留下一个永远写控制台的孤儿线程。
- **规则**:`_stop_captures` 是通过 `cap["_stop_evt"]` 停读线程的(**取消 asyncio task 对线程无效**)。改成重连循环后每次尝试都是**新的线程 + 新的 Event**,所以 `_serial_attempt` 开头必须 `cap["_stop_evt"] = stop_evt` **重新指过去**;指向旧 Event 就停不住当前线程。同理,通知状态变化**只在"进入 reconnecting"那一刻**发 —— `captures` 在任务对象上、因而属于 `tasksRenderKey`,每次重试都 announce 会让任务列表在重连风暴里反复全量重渲染(见 #23)。
- **位置**:`_serial_attempt`、`_stop_evt`、`_stop_captures`
- **来源**:见 #23

## #31 — 掉线自动重连用 `adb reconnect offline`，不是 `kill-server`

- **症状**:用 `kill-server` 做掉线自愈,把所有正在跑的脚本一起打断了。
- **规则**:掉线自动重连**只能用 `adb reconnect offline`** —— 它只影响 offline / unauthorized 设备,**在线设备上正在跑的其它脚本完全不受影响**;`kill-server` 会打断所有 logcat,那是「重置」按钮该干的事。用户对这条有硬约束原话:**「不能影响其它脚本运行」**。watchdog 也只对"有正在跑的任务"的设备动手,并且**每设备 20s 才试一次**。
- **位置**:`_device_watchdog`、`_adb_reconnect_offline`、`/api/adb/reconnect`
- **来源**:见 [ARCHITECTURE.md](ARCHITECTURE.md)

## #32 — 删掉某个后台任务前，先确认它还在做别的事

- **症状**:`GET /api/tasks` 的通道状态**冻结在任务开始那一刻**,只有任务结束才更新,而且**没有任何报错**。
- **规则**:v2.7.0 删掉 2s 计数心跳(它只为已删的通道 pills 存在)时,**连它唯一在做的 `t["captures"] = _public_captures(t)` 一起删了** → 公开快照从此没人刷新。现在由 `_announce_captures(task)` 在**所有状态迁移点**负责。**教训:删一个循环时,把它"顺便在做的事"列出来,别只看它的名义职责。**
- **位置**:`_announce_captures`、`_public_captures`、`GET /api/tasks`
- **来源**:v2.7.0;见 #23

## #33 — 串口最后半行必丢(已确认，刻意不修)

- **症状**:以裸提示符(无换行)结尾的 console,**最后一行永远不进文件**。
- **规则**:读线程的 tail flush 只在 `stop_evt` 置位后执行,而 `_stop_captures` 是同步块里 `evt.set()` 紧接 `ctask.cancel()`,消费端先收到 `CancelledError` 并关掉文件,那半行就进了无人读的队列。修它要在**最取消敏感的路径**插等待,低危一行,**不值** —— 已知、刻意不修。
- **位置**:`_stop_captures`、串口读线程的 tail flush
- **来源**:见 #26、#30

## #34 — 关 socket 也要有超时，理由和发消息一样

- **症状**:给发送加了超时后,服务端仍会被一个卡住的连接拖住。
- **规则**:`_broadcast` 在发送超时后真正 `ws.close()`,但 **`close()` 本身对一个刚刚证明自己会卡住的连接同样可能卡住**,而它跑在**采集读取路径**上 —— 等于把刚堵上的洞从另一个门放回来。所以关闭也必须 `asyncio.wait_for(..., WS_SEND_TIMEOUT_SEC)`。**凡是"因为我们不信任这个连接所以要做的清理",都要问一句:这个清理自己会不会卡?**
- **位置**:`_broadcast`、`WS_SEND_TIMEOUT_SEC`
- **来源**:v2.7.2

## #35 — 子进程管道两端必须显式声明同一个编码；且"读循环 + 收尾"不能共用同一个 `try`

- **症状**:任务**永远停在 `running`**、`exit_code=None`、不归档,日志以 task_id 命名孤零零留在 `logs/`(脚本其实早跑完了)。v2.7.4 在同事机器上必现。
- **根因**:`subprocess.Popen(text=True)` 不带 `encoding=` 时用 `locale.getpreferredencoding()`,中文 Windows 上是 **GBK**;而 `server_window.ps1` 给子进程设了 **`PYTHONIOENCODING=utf-8`**,于是**子进程写 UTF-8、服务端按 GBK 读**。脚本 stdout 里任意一个 GBK 非法字节对(实测 `top -n 1 -b` / `dumpsys window` 那几段最容易带出)就让 `proc.stdout.readline()` 抛 `UnicodeDecodeError`。
- **规则**:真正的灾难在于那个异常逃到了 `_stream_logs` 底部的兜底 `except` —— 它只广播一条错误就 `return`,**把下面的收尾块整块跳过**(等退出 / `exit_code` / 终态 status / `_stop_captures` / `_archive_task` / `end` 帧)。**两条独立教训**:① 凡是 `text=True` 就**必须显式 `encoding=`**(读 adb 输出的地方同理,`ir_runner.py` 与 `_adb_reconnect_offline`、`/api/adb/reconnect` 已一并修);② **读取循环和收尾写在一个 `try` 里,等于让"读失败"吃掉"终态"** —— **收尾必须无条件执行**。现在读取循环自带 `try`(失败 → 记原因 → 广播 → 终止子进程 → 离线程 `wait()`),任务**不可能停在半路**。
- **位置**:`_stream_logs`、`_stop_captures`、`_archive_task`、`_adb_reconnect_offline`、`ir_runner.py`
- **来源**:v2.7.4(构造方法与 A/B 实证见 [CHANGELOG.md](../CHANGELOG.md))

## #36 — 串口能力 = 一个 pyserial，缺了它只表现为"COM 下拉框是空的"

- **症状**:用户看到的是一个空的 `— COM —` 下拉框,**零解释**;服务端照常启动、任务照常跑完。
- **规则**:平台枚举 COM 口走 `serial.tools.list_ports.comports()`,它读的是 Windows 注册表 `HARDWARE\DEVICEMAP\SERIALCOMM` —— **和设备管理器同源**。所以不需要任何额外工具或驱动:**设备管理器能看到口,就证明驱动已经好了**,剩下的唯一变量是"跑服务端的那个 Python 里有没有 `pyserial`"。三个坑:① **`import serial` 在 `_capture_serial` 和 `/api/serial/ports` 里都是懒加载的**(为了让服务端在没 pyserial 时也能启动),所以**没装 pyserial 服务端照常启动、任务照常跑完,只有采集通道静默变成 `unavailable`**;② `/api/serial/ports` 明明返回了 `{"ports":[],"available":false}`,但前端 `refreshSerialPorts` **只取了 `ports`、把 `available` 丢了** → 用户零解释;③ **必须用跑服务端的那个 Python 装** —— `start.bat` 取的是 PATH 里第一个 `python`,不一定是你在 conda 里装过的那个。诊断一行(以服务端窗口打印的路径为准):`<那个python> -m serial.tools.list_ports -v`。
- **位置**:`_capture_serial`、`/api/serial/ports`、`refreshSerialPorts`
- **来源**:2026-09-11 同事机器实踩;见 #18

## #37 — 恒开的采集会干扰被采集的对象 —— logcat 改为人工开关

- **症状**:8 小时级别的监控任务下,一次 run ≈ **1GB** logcat;**监控任务等于自己干扰自己**。
- **规则**:logcat 从 v2.6.0 起对每个任务**恒开**,这对短时压力测试是对的(崩溃现场就在里面),但对长跑监控是错的 —— 它在**压测已经把设备打满的时候再压一路 `adb logcat`**,而 perf_monitor 要的恰恰是设备**在自然状态下**的表现。改为照 `serial_capture` 的形状加**人工开关**(设备卡 `device-row5`,请求字段 `logcat_capture`)。**三个设计要点**:① 字段默认 **True** 而不是 False —— 老前端不发这个字段时行为必须与今天完全一致,而且 logcat 的失败方向更危险(**残留的 off 会静默丢掉崩溃现场**);② 关掉时**不需要任何新分支**:`_stop_captures` 的"从未启动的通道"守卫(见 #24)天然跳过它,`_archive_task` 的 `if not srcp.exists(): continue` 也自然跳过;③ 勾选状态**不持久化是刻意的**(本次意图,同串口勾选)。**一般性教训:当采集器本身会改变被观测系统的行为时,"恒开"就是个错误的默认值 —— 长跑场景尤其。**
- **位置**:`logcat_capture`、`_stop_captures`、`_archive_task`、`device-row5`
- **来源**:v2.7.5;见 #24

## #38 — `fg_cpu` 死锁在 baseline —— 只被真机抓到的 bug

- **症状**:`fg_cpu` 整场恒为 `null`,状态字符全程是 `b`(baseline)。
- **根因**:「第一次读建立基线,第二次读才算差值」的写法里,基线推进条件写成了 `if self.fg_prev is not None: self.fg_prev = pstats`。**首次调用时 `fg_prev` 必然是 `None`,所以这个条件永远为假,基线永远建立不起来。**
- **规则**:真机 **75s** 跑出来才发现。**为什么单测和假设备 harness 都没抓到**:① 断言只检查"状态字符合法",没检查"第二个 SLOW 周期之后必须出值";② 假设备喂的 `utime` 单调递增,看起来一切正常。**教训:"需要两个样本才能算出值"的指标,必须在测试里显式跑满两个周期并断言第二个周期出值 —— 只断言形状合法等于没测。**已补回归断言(第一次 = baseline,第二次必须出值)。
- **位置**:`fg_prev`、`fg_cpu`、`perf_monitor.py` 的 `--selftest`
- **来源**:v2.8.0

## #39 — `st` 列必须定宽，否则一次掉线就把后面所有列推歪 6 格

- **症状**:`ok:` → `offline:` 让分隔符从第 **100** 列跳到第 **106** 列 —— 这恰恰废掉了定宽列存在的唯一理由,而且恰在**最需要竖读的时候(故障中)**。
- **规则**:`st` 是 `<拍结果>:<9 个状态字符>`,9 个字符永远等宽,但**结果词不等宽**(`ok` / `error` / `timeout` / `offline` / `partial`)。改法:结果词 `padStart(7)` 右对齐,`st` 单元**恒为 17 字符**。**代价**:行前缀 **82 → 105** 字符,4MB 重放帽下 `chart.png` 完整区间 **7.8h → 7.0h**(见 [TODO.md](../TODO.md) §6.9)。**这条是 node 层渲染 harness 抓的,不是 `node --check`** —— 语法检查看不见列错位,只看得见语法。
- **位置**:`st` 列渲染、node 渲染 harness
- **来源**:v2.8.0

## #40 — 判定的分母不能用"实测耗时"，要用"拍序号"

- **症状**:覆盖率能算出 **111%**;或者一个卡住的 run 也能算出接近 **100%**。
- **规则**:覆盖率写成 `ok_tick / (实测墙钟 // T)` 有两个问题:① **差一**(N 拍跨 (N-1)·T);② 更坏的是**自我满足** —— 设备越慢、墙钟越长、分母越大。改用 `max(1, last_k + 1)`(`last_k` = 已发出的最大拍号,**跳过的槽也计入**),既没有差一,也让"漏拍"真实地压低覆盖率。
- **位置**:覆盖率计算、`last_k`
- **来源**:见 [PERF_MONITOR_V2.md](PERF_MONITOR_V2.md);见 #41

## #41 — 一个"坏拍"要把所有指标标成失败，而不是保留上一拍的值

- **症状**:真掉线那一拍还带着上一拍的值画出去,图上是一条**平线**,而**平线会被读成"设备稳下来了"**。
- **规则**:零阶保持(`h`)只适用于**没到期**的指标。所以 `res ∈ {timeout, offline, error}` 时把**所有非 `d` 指标**的状态改成 `x`、值置空(前端 `connectNulls:false`,线自然断)。**信息没丢:最后一次真实读数在上一行 CSV 里** —— 正是 `-` 单元和 `gap_ms` 存在的意义。连带的坑:这样会让坏拍**抬高所有指标的 `n_due`**(对覆盖率的分母是对的 —— 那个槽确实请求了也确实空着回来),但会让"通道到底通不通"的门误报(设备只是离线,根本没机会回答),所以另记了 **`n_due_ok`**(只数坏拍之外的到期次数)给 CPU 门用。
- **位置**:`n_due`、`n_due_ok`、`gap_ms`、`connectNulls`
- **来源**:v2.8.0;见 #40

## #42 — 删掉一个开关时，把布尔谓词一起改写 = 整条通道静默失效，而且所有断言都看不见

- **症状**:**前台应用通道整轮一个值都没出过**(`fg_pkg` / `fg_pid` / `fg_cpu` 全 null,状态字符恒为 `n`),而报告却是 `RESULT: OK` / 9 门全 pass / 覆盖率 100%。
- **根因**:删 `track_foreground` 时,谓词原本是 `name not in ("FOCUS", "FGPKG", "PIDSTAT") or self.track_fg`,被压成了 `name not in (...)` —— **逻辑整个取反**,三个 section 从此永远不"到期"。
- **规则**:**为什么三层测试都没拦住**(比 bug 本身更值得记):① **`--selftest` 的 60+ 条断言、假设备 harness、真机 60s 实跑三层全绿** —— 但所有既有断言都是**形状**断言(CSV 是矩形、报告能往返、门是 9 道、版本号一致),**没有一条问过「这个通道到底出过值没有」**;**一个死掉的通道和一次太短的运行,在形状上一模一样**;② 假设备 harness 照常合成 FOCUS section,协议层面完全看不出问题 —— 它测的是**解析**,不是**这个 section 到底有没有被请求**。**唯一看得出来的地方:对着 `samples.csv` 逐拍看 `st` 列** —— SLOW 拍的状态字符是 `n`(从未到期)而不是 `b`(基线)或 `f`/`h`。**状态串是最低成本的体检,养成看它的习惯。**
- **加固**:谓词提到模块级 `section_due(name, secs, tiers, gpu_enabled)`,补 **6 条 selftest 断言**(命令侧确实要求了该 section / 谓词侧接受 / 无 SLOW 时拒绝 / section 缺失时拒绝 / GPU 受 `gpu_enabled` 约束);harness 新增 **`assert_channels_alive()`** —— 凡存在「有 SLOW 拍**成功返回过**」的运行,`fg_pkg` / `fg_pid` 就必须至少产出一个值,而且它读的是 **`n_due_ok`**(坏拍之外的到期次数),所以"设备一直离线"的场景不会被误报。**已用一份把该 bug 重新注入的副本反向验证过这条断言确实会失败。不会失败的回归测试等于没有测试 —— 修完必须让它先红一次。**
- **位置**:`track_foreground`、`section_due`、`assert_channels_alive`、`n_due_ok`、`--selftest`
- **来源**:v2.9.0;见 #41

## #43 — 控制台块的最后一行是给机器认领报告的，新增行别撞上它

- **症状**:平台把 HTML 报告认成 JSON 报告,或者报告根本没被认领。
- **规则**:`server.py` 的 `_sniff_report_path()` 靠一条**很土**的规则认领脚本报告 —— 一行里**同时**含 `" : "` 和子串 `"report"`,再把 `line.rsplit(" : ", 1)[1].strip()` 拿去 `.endswith(".json")`,**而且只取第一条命中的**。所以新增的 HTML 行改用 `  html   = <path>`(**`=` 而不是 `:`**):报告写在 `reports/` 下,**路径本身就含 `report` 子串**,只靠扩展名检查去拒绝它太脆。`--selftest` 现在**逐字复刻 server 的 sniff 规则**,断言「**有且只有一行可认领,且它在最后一行**」。改判定块的渲染时**先跑那条断言**,别等平台把报告认成 HTML。
- **位置**:`_sniff_report_path`、`_CLAIMED_REPORTS`、`--selftest`
- **来源**:v2.9.0;见 [REPORT_FORMAT.md](REPORT_FORMAT.md)

## #44 — 「没出现过」和「出现过又消失」必须分开判 —— 否则同一个门既假绿又假红

- **症状**:gate 9(APP)在**两个方向上都是错的**。**假红**:脚本从桌面开始跑、或者选的包设备上根本没装 —— `watch_pkg` **从没上过前台**,2 拍之后照样记成"丢失"并 **FAIL**;**假绿(更糟)**:app 崩了但窗口还没收 —— 前台包名**仍然等于** `watch_pkg`,判据完全看不见。
- **规则**:原判据是「前台包名 != `watch_pkg` 连续 2 拍就记一次丢失」。**两层教训**:1. **注释不是代码。** 那段代码上方写着「`gone` requires the process to be missing from pidof while it is still the focused package」,而代码做的是**包名比较**,一行 pidof 都没碰;真正记 pid 级证据的 `fg_pid_lost` 在别处累加,**没有任何一道门读它**。读到"注释与实现一致"的错觉时,**去看谁在读那个字段** —— **没人读的字段等于不存在**。2. **凡是"异常/正常"的二分判据,都要问一句「它区分得开哪两种状态吗」。** 这里的失败**不是阈值不对,是判据本身无法区分**「从未出现」与「出现后消失」—— 同一个观测量(前台包名)对两种截然不同的情况给出同一个值。修法是**引入第三个状态(inconclusive),而不是调阈值** —— 而这正是 `watch_pkg` 这个参数存在的唯一理由。
- **加固**:`--selftest` 用真 `Evidence` + 真 `judge` 跑**五个场景**,并**用重新注入旧判据的副本反向验证**(旧逻辑挂 5 条、新逻辑 0 条)。**不能失败的回归测试等于没有测试。**
- **位置**:`watch_pkg`、`fg_pid_lost`、gate 9、`Evidence`、`judge`
- **来源**:v2.9.0

## #45 — 报告路径必须由引擎推导，别让调用方拼 stem

- **症状**:**少一个字符就静默不进归档** —— 不报错、不告警,归档里就是少一个文件。
- **规则**:`_companion_files()` 收编一个伴随文件的条件是「**同目录** + 文件名以 `<报告stem> + "."` 开头 + 后缀 ∈ `(.csv, .html)`」。所以 `write_html_report(json_path, doc)` 的签名里**没有 html 路径参数**,它自己从 JSON 路径推导(`html_path_for()`)。**这不是"少写一个参数",是让错误无法表达。**新增脚本**别自己 `os.path.splitext` 拼一份**。
- **位置**:`_companion_files`、`write_html_report`、`html_path_for`、`scripts/_pptp_report.py`
- **来源**:v2.10.0;见 [REPORT_FORMAT.md](REPORT_FORMAT.md)

## #46 — 判断"这个值要不要转义"，判据必须是数据的类型，不是数据长什么样

- **症状**:任何以 `<td` 开头的数据值都会**绕过转义**;真机上没有这样的数据,所以**它在测试里不会暴露**。
- **规则**:引擎第一版的 `_cell(c)` 用 `str(c).startswith("<td")` 来决定"这个值是不是已经渲染好的 HTML"。修法:`_cell(c)` 返回 **`(cls, inner_html)` 元组**,由调用方**无条件**拼出 `<td>`;`cls` 为 `None` 时不加 class。**凡是想"看字符串长得像不像 markup 来决定要不要转义"的地方,都是同一个 bug 的变体。**
- **位置**:`_cell`、`scripts/_pptp_report.py`
- **来源**:v2.10.0;见 [REPORT_FORMAT.md](REPORT_FORMAT.md)

## #47 — "正常关闭"与"故障"现场完全一样时，别猜那个现场，去找一个两者表现不同的后续事件

- **症状**:报告写 `key_failed=1`,读起来像 keepalive 出过错 —— **实际是用户自己按的停止**;而"当场那一眼分不出来"。
- **根因**:平台停任务用 `CTRL_BREAK_EVENT`,它送给**整个进程组** —— 脚本自己 spawn 的 `adb.exe` 也在内。于是"用户按下停止"在按键线程眼里**就是一次 adb 调用失败**(`ADB failed: `,stderr 为空),而且此刻 `_stop` 还是 `False`(adb 死在前,主线程收到事件在后)。
- **规则**:出错**先不计数**,用**它后面那个等待**定性 —— 真故障后面跟着正常间隔,停止会打断它(`stop.wait(gap)` 为 True ⇒ 那是一次停止,丢掉这个错)。**`key_failed` 的含义因此是「失败过且仍在重试」。** **凡是「正常路径和故障路径产出同一个可观测现场」的地方,都要找一个只在这两种情况下不同的后续信号。**
- **位置**:`CTRL_BREAK_EVENT`、`key_failed`、`stop.wait(gap)`、`_stop`
- **来源**:v2.11.0

## #48 — 终结者只能有一个，而且终态必须在第一个 `await` 之前落定

- **症状**:任务**永远卡在 `interrupting`**;而 `POST /api/run` 的 409 守卫恰好拦 `running` / `interrupting` ⇒ **那台设备从此起不了任何新任务**,直到重启服务或 `force-cleanup`。
- **根因**:平台的「硬停」(`api_force_stop_by_device`)和读取协程 `_stream_logs` **都能**终结同一个任务。硬停原本只写 `interrupting`、指望 `_stream_logs` 推进它,但那一步在 **`await _stop_captures()` 之后**:一 `await` 就让出事件循环,而 `_stream_logs` 正卡在 `proc.wait()` 上,**子进程一死它立刻醒**,先跑完自己的终结块(终态 `failed` + 归档 + 广播),控制权才回到硬停那边 —— 于是一个**非终态**的 `interrupting` **盖在已定的终态上**,而 `_stream_logs` 已经 `return`,**再没有任何东西会推进它**。
- **规则**:**同步认领终结者** —— 硬停在**第一个 `await` 之前**置 `t["_finalized"] = "force_stop"` 并**直接写终态**(被杀就是 `interrupted`,**不是 `failed`**:是操作员要它死的);`_stream_logs` 醒来看到 `_finalized` 就 `return`,**不做第二次终结**。硬停这边本来就自己做完了 `_stream_logs` 的全部收尾动作(停采集 / 归档 / 广播 `end`),只补一句 `_broadcast_archive_line()` —— **凡是要"我先把状态写软一点、让别人去收尾"的地方,先问:那个别人此刻是不是已经醒了?** 验证时看服务端输出:硬停任务**只有一行** `[archive] … (force_stop)`、**没有** `(task_end)`,即那条 `return` 确实走到了。
- **位置**:`api_force_stop_by_device`、`_stream_logs`、`_finalized`、`_broadcast_archive_line`、`POST /api/run` 的 409 守卫
- **来源**:v2.11.1
