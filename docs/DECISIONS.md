# PPTP 设计决策登记册(DECISIONS)

本文件是**已批准、不得被静默推翻**的设计裁决的唯一登记处。**`D-nn` 是永久标识符,永不重编号**;被推翻的条目**划线后指向替代条目,永不删除**。其他文档引用本文件时写 `docs/DECISIONS.md D-nn`。**按分组读,别从头读** —— 每条只留裁决本身,调试叙事与"某个版本改了什么"在 → 指向的文档里。

> 指向:[CHANGELOG.md](../CHANGELOG.md)(版本沿革)· [PITFALLS.md](PITFALLS.md)(踩坑 `#N`)· [REPORT_FORMAT.md](REPORT_FORMAT.md)(报告引擎)· [PERF_MONITOR_V2.md](PERF_MONITOR_V2.md)(§12 权威)· [TODO.md](../TODO.md)(已知但不修)

---

## 一、平台铁律 (D-01 – D-09)

### D-01 采集是服务端职责,脚本零改动
「每一次的脚本执行都能实时监控 logcat 和串口日志,后续增加自动开启和自动保存」
服务端在 `api_run` 起采集协程、在任务终态收尾;脚本**拿不到自己的 `task_id` / 日志路径**,9 个脚本零改动。三件一起、分阶段:① 实时采集与推送 ② 自动开启(绑定任务生命周期)③ 自动保存(落盘)。
**理由**:`scripts/` 下脚本必须独立可 CLI 跑 —— 采集一旦进脚本,平台与脚本就互相绑死。
**禁止**:把 logcat / 串口抓取写进 `scripts/*.py`。
**反悔**:唯一例外是脚本业务本身要串口,平台自动让位(见 D-06)。

### D-02 前端只显示 stdout,永久
「直接将 logcat 和串口日志的前端显示方案删掉,建议还是稳定简单为主导」
logcat / 串口**照采、照归档,前端永不展示**。前端只订阅 `stdout`,所以 logcat / serial 帧根本不发上网线 —— `WS_SUBS: dict[int, set[str]]`(`id(ws)` → 订阅源),`_broadcast(task_id, msg, source)` 跳过没有订阅者的源,`source=None` 留给控制帧。
**已整体删除**:`CHANNELS` / `TABS_ENABLED` / `state.activeSource` / `state.captureCounts` / 计数 pills / `#channel-bar` / `_capture_counts_ticker`。
**禁止**:把 v2.6.0 的通道显示方案(计数 pills / tab 接缝)加回来。**永不复活。**

### D-03 ~~前端通道显示方案可用开关打开~~ **[已作废,见 D-02]**
~~v2.6.0 批准:前端暂时只展示 stdout,但**数据层 / WS 协议 / 渲染路由全部按通道就位** —— 开 tab 只是 UI 改动,`TABS_ENABLED = false` 是唯一的开关。~~
**作废**(用户 v2.7.0 改主意):整套方案删除,取代者为 D-02。
**仍然保留**:`#btn-export-log` 加 `hidden`(自动存档取代手动导出),但 **`onExportLog` 全部代码保留未删** —— 用户说的是"暂时",去掉 `hidden` 即可恢复;多源导出循环里的 `CHANNELS` 引用改为 **`CAPTURE_EXPORT = ["logcat","serial"]`**。这一段不要顺手清理。

### D-04 一台设备同时只能跑一个任务,冲突返回 409
背景:多设备各自跑不同脚本、各自日志独立,但同一台设备不能有两个并发任务 —— ADB 层会互相打断。
`api_run` 检查并发,第二发 `POST /api/run` **返回 409**(device-uniqueness)。v2.11.1 修的既有 bug:硬停把状态写成 `interrupting`,但这一步在 `await _stop_captures()` **之后**,读取线程可能已把任务收尾成 `failed`,于是 `interrupting` **覆盖掉终态**、再没有任何东西推进它 → **该设备从此被 409 卡死**(只能 `force-cleanup` 或重启服务解开)。
**因此规定**:硬停必须在**第一个 `await` 之前**同步认领终结者(置 `_finalized` + 直接写终态 `interrupted` / `exit_code=-9`),`_stream_logs` 看到 `_finalized` 直接 return。**[已裁决,不要再改]**
**理由**:状态机只能有一个推进者,否则终态会被覆盖成永远无人推进的中间态。
→ [PITFALLS.md](PITFALLS.md) #48

### D-05 一份报告只被一个任务认领(`_CLAIMED_REPORTS`)
背景:兜底 mtime 扫描曾把**前一个任务**的报告算给当前任务 —— 同设备背靠背跑、两个任务时间窗重叠,于是 `ir_runner`(根本不写报告)的存档里出现了 `report.json`。
**三重收紧**:① **只有 `REPORT_WRITING_SCRIPTS` 里的脚本才走扫描**(`ir_runner` / `wifi_reboot` / `bt_reboot` 从不写报告);② mtime 必须 **≥ 任务开始时间**(原来是"开始-5s",白放宽了一截);③ **`_CLAIMED_REPORTS` 保证一份报告只被一个任务认领**。
两条关联路径(嗅探 stdout 的 `  report          : <绝对路径>` + mtime 兜底)**都必须校验结果落在 `REPORTS_DIR` 内** —— 路径来自脚本 stdout,是数据不是可信路径。
**理由**:**归属错一份报告比少一份更糟** —— 它看起来完全可信,内容是别人的。
**禁止**:放宽 mtime 窗口回"开始-5s";让不写报告的脚本走扫描。

### D-06 脚本优先:声明 `serial_port` 的脚本拥有串口(`_public_captures`)
背景:串口同时只能被一方打开,而平台在任务期一律采串口。
声明了 `serial_port` 参数的脚本**拥有**该端口(目前只有 `battery_inout_stress.py`);服务端把串口采集标成 `skipped` / `script_owns_port`(任务照跑、logcat 照采),前端把控件禁用 + 给中文提示。
`_public_captures()` 是通道状态的公开快照,**刻意只含 `active/status/restarts/detail/port`,不含 `lines/bytes`** —— 否则每 2s 轮询都会判定"变了"并触发全量重渲染。
**理由**:该脚本需要串口做 5 分钟保活,被抢会让**电量曲线变平** —— 用调试台换电量曲线是坏交易。
**禁止**:平台采集抢走脚本的串口;把 `lines/bytes` 放进公开快照。
**反悔**:这个让位**必须在 UI 可见**,否则会被用户当 bug 报。

### D-07 代码文件无中文,stdout 与 `#log-console` 全 ASCII
「前端 log 打印不能存在中文」;「代码文件无中文,中文只进 md」。
`scripts/*.py` / `static/app.js` / `static/style.css` / `static/index.html` 无中文;脚本 stdout **全英文/ASCII**(Windows 控制台 + WS 编码)。凡打进 `#log-console` 的内容全英文 —— 可读行如 `[batt] HH:MM:SS (X.Xs) level=87% temp=36.2C voltage=8.4V status=charging`、meta 行 `[batt] monitor start | mode=charge | port=COM9 | source=dumpsys battery`、WS 重连/重放/截断提示与占位符。**已删 `STATUS_ZH` / `JUMP_TYPE_ZH` 中文映射表**;所有 log 时间戳走 `fmtClock`(`toLocaleTimeString("zh-CN",{hour12:false})`)强制 24h `HH:MM:SS`,杜绝 zh-CN 浏览器吐"下午3:30"。**UI 文案仍中文**(卡片 / 弹窗 / 图表系列名 / 导出表头)。
**禁止**:在 `#log-console` 里打中文;把中文映射表加回来。
→ [PITFALLS.md](PITFALLS.md) #20

### D-08 掉线容忍:三条链路自动接回;重连只用 `adb reconnect offline`
「perf_monitor 这种,当存在设备重启或者 adb 掉线时是正常的」;「设备重启完成后需要能自动连接上之前的日志,且能自动连接上 adb 和串口」;「重连就只 reconnect 吧,**不能影响其它脚本运行**」。
四处落地:① 串口从"一次性"改成受监督重连循环 —— **连续失败预算 `SERIAL_MAX_RESTARTS = 60`**、**收到任何真实数据就清零**、每次重试换新线程 / 新 `Event`、写可见 marker;② 新增 `_device_watchdog` 周期发 `adb reconnect offline`;③「临时离线」合成卡的条件补上 `interrupting`;④ `_broadcast` 发送超时后**真正 `ws.close()`** —— 否则前端 `onclose` 不触发、`scheduleReconnect` 不执行,浏览器握着死 socket 永远收不到日志(这才是"连不上之前的日志"的真身)。logcat 采集自带重启循环(**最多 5 次、间隔 2s**),每次写一行可见 marker `--- logcat capture restart #N ---` —— 这是用户唯一能察觉"中间断过"的途径。
**探查先于实现**:用户 5 条里有 2 条**已经满足**,不要重复造 —— `perf_monitor.py` 的 `adb_shell()` 从不抛异常、失败返回 `""`,主循环照常产出全 null sample 并继续;任务卡从不会消失。
**禁止**:**自动 `kill-server`** —— 它只属于「重置」按钮 `POST /api/adb/reconnect`,否则会打断同机其它设备正在跑的任务;给"临时离线"再加一层提醒(见 D-09)。
→ [PITFALLS.md](PITFALLS.md) #27 / #31 / #32 / #34

### D-09 不新增离线提醒 UI
「不需要,直接拿之前的【临时离线就行了】」
「临时离线」合成卡就是**唯一信号**。
**禁止**:横幅 / toast / 额外日志行。

---

## 二、存档与产物 (D-10 – D-17)

### D-10 任务结束自动存档到 `archive/<模块>/<时间>_<脚本>_<设备>/`
「我没找到 log 存放在哪里,这些 log 需要做成结束后自动存档,包括 perf monitor 的图表也算,相当于 log 和 report 都需要结束就实时存档;所以导出的按钮可以暂时先隐藏掉了」
**注意:日志一直都在 `logs/` —— 用户找不到是产品缺陷,不是用户的问题**(文件名 32 位 task_id 打头 + 界面无任何入口 + 产物散在三处)。**任何"东西在哪"的设计都要把人能不能找到算进成本。**
每任务一个文件夹;目录名**不含 `task_id`**(`make_archive_name()` 只出 `<时间>_<脚本>_<设备>`,task_id 存进 `summary.json`)。`logs/` 降级为运行期临时工作区,只留 `server.out.out` / `server.err.log`。**迁移而非复制**:`_archive_task` 用 `Path.replace()` 把日志**搬**出 `logs/`(复制会让永久保留策略下磁盘占用翻倍)。
模块由 **`ARCHIVE_MODULES` 显式映射**得出(`ir_runner.py→ir` / `wifi_onoff|wifi_reboot|wifi_switch→wifi` / `sensor_reboot_stress→sensor` / `app_launch_stress→app-launch` / `perf_monitor→perf` / `battery_inout_stress→battery` / `bt_reboot_stress→bt`),未登记落 `ARCHIVE_MODULE_FALLBACK = "other"`,**存档根目录下永远只有模块文件夹**,模块名与 `reports/stress-test/<模块>/` 对齐。
**禁止**:目录名带 `task_id`;**靠文件名猜模块** —— `wifi_onoff` / `wifi_reboot` / `wifi_switch` 同属 `wifi` 而 `bt_reboot` 不是。

### D-11 永久保留:不做自动清理、不查磁盘空间、删任务不动存档
**[已裁决,不要再改]** —— 存档无限增长是**已知且接受的代价**。
`GET /api/archive/stats` 只做 `{"count","bytes","dir"}` 占用显示。`×` / 「清空已完成」只做内存移除(`_forget_task`);`api_delete_task` / `api_cleanup_tasks` **不再碰磁盘**,两个按钮的 `title` 改成「从列表移除(存档保留在 archive/)」。
**理由**:既然承诺"永久保留",一次误点就不该摧毁它。
**禁止**:任何形式的自动清理;**开跑前查磁盘空间**(用户明确否决);把删除语义"修"回连带删除(`_delete_task_logs` → `_forget_task` 是**有意的语义变更**)。

### D-12 归档必须排在 `_stop_captures` 之后
背景:Windows 拒绝移动仍被打开的文件 —— `_stream_logs` 的 `with open(...)` 在调归档前已退出,`_stop_captures` 关掉了 logcat / serial 句柄。
**归档有四个调用点,不存在单一终点**:`_stream_logs` 尾部(正常 / 中断 / 失败都汇聚于此)、`api_force_stop_by_device`(SIGKILL 无 EOF)、`api_force_cleanup`(必须在 `TASKS.clear()` **之前**)、`api_server_shutdown`(`os._exit` 绕过一切)。`_archive_task` 靠 `task["_archived"]` **幂等**。
**禁止**:让归档抢在 `_stop_captures` 前面;**让移动失败抛出** —— 必须记进 `summary.json` 的 `notes`。

### D-13 报告是"搬"不是"复制";一个任务可有多份报告
背景:`reports/` 只是几秒钟的暂存区,不该累积、不该出现两处副本不一致。
归档成功后**先 copy 再 unlink** 删掉 `reports/` 里的原件(两步走 —— 删失败也不丢唯一副本)。**一个任务可能有多份报告**(`app_launch_stress` 每个 APP 一份),`_find_task_reports()` 返回**整组**,归档为 `report.json` / `report-2.json` / …。
**禁止**:只归档 stdout 打印的那份 —— 会把其余的**永远留在 `reports/`**。

### D-14 归档主报告名固定 `report.json`,伴随文件保留原名,路径推导封在引擎里
主报告改名 `report.json`,**伴随文件保留原名**。`_companion_files()` 只认「同目录 + 同 stem 前缀 + `.csv` / `.html`」,所以同 stem 同目录就自动带走。**路径推导封在引擎里,不接受调用方拼串。**
**归档不限制文件类型** —— 原话「归档的文件类型不局限在那几样,唯一约束是需要归档在一起且统一命名」。所以**新增产物后缀是允许的**:新脚本多出一种伴生文件时,把它加进 `_companion_files()` 的认领集合即可,**不要**为了"只归档这几种"而把文件丢掉。
**禁止**:调用方自己拼 stem 或归档路径。
→ [PITFALLS.md](PITFALLS.md) #45

### D-15 日志文件名由服务端下发,客户端从不重建路径;`_log_path` 是唯一路径权威
命名 `logs/<task_id>_<yyyyMMdd-HHmmss>_<script-stem>_<device>.<source>`(`.log` / `.logcat.log` / `.serial.log`)。**保留 `task_id` 在最前面是有意的** —— 删除 glob `f"{task_id}*"` 与 `^[0-9a-f]{32}$` 路径防护无需修改即继续工作。服务器把**确切文件名放在 `t.log_files` 里下发**,`logFileName()` 只对 v2.6.0 之前的老任务回退约定名。
`_log_path` 归档后解析到 `archive/<dir>/<member>`,未归档回退 `logs/` —— `api_task_log`、`ws_logs` 重放、导出**全部自动跟随,没有一处需要各自打补丁**。
`GET /api/tasks/{id}/log?source=` 一律走 `_read_tail`(**默认 4 MB** 上限),**绝不整文件 `read_text()`**(logcat 上限 1GB,整读会硬冲内存);`source` 白名单校验,非法 → **400**;`truncated` 必须写进导出文件头,不能静默丢行。
**理由**:没有选"每次运行建子目录",因为 Windows 在捕获文件句柄打开时拒绝 `rmtree`。
**禁止**:客户端拼日志路径 / 每次运行建子目录;整文件 `read_text()`;新增消费者各自打路径补丁。

### D-16 `LOGCAT_MAX_BYTES` 256MB → 1GB,超限不轮转
背景:真机 `wifi_reboot_stress` 实测**一次重启(关机 + 开机风暴)≈ 1.3MB logcat**(重启前只有 63 行),而三个重启脚本 `iterations` 默认 **100** → 单次默认跑 ~128MB,旧上限约 197 轮触顶并**静默截断**。文档里那句"~0.82 MB/小时"是**空闲设备**量,差约 **95 倍**,已改写。
超限写尾行说明 + `status="capped"` 并退出重启循环,**不轮转**;`PPTP_LOGCAT_MAX_MB` 可调。
**理由**:开头几分钟最有价值,截断重开比停更糟。

### D-17 存档入口:全局功能放全局位置
背景:v2.7.1 用户反馈 —— 存档是整个平台的事,不该和任务卡片的工具栏混在一起。
顶栏全局「打开存档」按钮(打开 `archive` 根)+ 任务卡「存档」按钮(打开那一次运行)。
**禁止**:把「打开存档」塞进任务卡工具栏。

---

## 三、前端 (D-18 – D-26)

### D-18 图表 = 浏览器渲染后回传,服务端**不引入 matplotlib**
离屏 ECharts 渲染 → `POST /api/tasks/{id}/archive/artifact`(`name` 白名单 `chart.png` / `perf.csv` + **8MB** 上限 + hex 守卫)。两处触发点:WS `end` 帧(正在看该任务时)+ `refreshTasks` 轮询扫描(任务在别的视图下结束 / 跑完才打开浏览器时靠 WS 重放重建);`state.chartPosted` 防重复。
导出 PNG 固定 **1280×360** 归一化(resize→getDataURL→resize 回原尺寸,2x 后 2560×720),长时检测图片也不"长";**全程图**用 `exportFullChartDataUrl(tid)` 离屏渲染、`xInterval = 30*60*1000`(**0.5h 一刻度**)—— **live 窗口只作用于屏幕,导出与屏幕解耦**。
**代价**:任务结束时浏览器没开就没有 `chart.png`(用户已知悉并接受),`summary.json` **必须如实反映缺图**。
**禁止**:服务端引入 matplotlib —— 为一个图养两套渲染不划算。

### D-19 前端 log console 全 ASCII
细则见 D-07 —— 可读行 / meta 行 / 跳变行 `[!] jump[type] prev->87%` / WS 重连·重放·截断提示 / 占位符全部英文。脚本 stdout 本就 ASCII,所以这部分**脚本零改动**。
**禁止**:console 里出现中文(包括"下午3:30"式的本地化时间)。

### D-20 折线图固定 1h 滚动窗口
用户 3 点要求逐字:「① 前端 log 打印不能存在中文;② 前端折线图的显示范围需要从刻度 2min 拉至 10min,且整体包含约 6 个刻度,也就是实时显示 1h 的曲线;③ 同步一起修改 perf_monitor 的」。
`x 轴 type:"time"` + **`PERF_WINDOW_MS = 60*60*1000`** + `interval:10*60*1000` + `splitNumber:6`;右缘 = 最新样本墙钟(`perfWindowEnd`,无样本回退 `perfXBase`),`flushPerfChart` 每次滚窗口 + `shift()` 掉窗口外点。perf / battery **共用同一套** `buildPerfOption` / `flushPerfChart`,天然同步生效。
**禁止**:把 `dataZoom` 加回来(与固定窗口互斥)。
→ [PITFALLS.md](PITFALLS.md) #21

### D-21 RAF 批量推送日志 + 双层行上限
客户端批量 buffer,**RAF 一次性 `textContent` 更新**;DOM **1000 行**上限(类 VSCode 终端 scrollback);服务端 replay 上限 **10000 行**(防异常巨大 log)。
**理由**:避免频繁 DOM 操作造成"刷"。

### D-22 UI 状态与 **设备 × 脚本** 参数持久化(`localStorage`)
已选设备 / 已选脚本 / 已选序列 / 已选参数跨刷新保留(`pptp.deviceState.v1`);**服务器数据(任务 / devices)不持久化,总是从 server 拉**。
参数按 `state.deviceParams: { [serial]: { [script]: {param: value} } }` 严格独立、互不干扰 —— 用户明确要求「不能存在记忆或干扰」;跑任务时后端把配置值**合并进 `--params` JSON** 传给脚本。配置入口做成和 ir_runner 一样的"点击卡片 → 弹窗"。
**禁止**:引入跨设备共享的参数记忆。

### D-23 ir_runner 序列:按设备独立绑定 + 文件驱动 + 不做 step 编辑器
`state.deviceSequences: { [serial]: filename }` —— A 选 `default`、B 选 `aging` 互不影响;切设备时脚本卡实时刷新;选中设备的 ir_runner 卡显示该设备的序列名。序列即 `ir_sequences/*.ini` 文件,**用户直接编辑**;modal 只做**选择 / 创建空模板**,不做 step 编辑(step-by-step 的 code / kind / delay / count 行编辑器**已废弃**)。modal 标题动态化:文件模式 `${seqFilename}.ini`,设备模式 `设备 ${serial} 的序列`。
**禁止**:全局 "pinned script" 这种共享模式;把 step 编辑器加回来。

### D-24 ir_runner 卡片:Run 在脚本卡 + 三态门控 + 视觉徽标
ir_runner 脚本卡右侧「▶ 跑」按钮;设备卡只显示状态(chip / 运行中 / 离线)。**理由**:runnability 取决于"设备 + 脚本 + 序列"三态。
门控四态:设备未选中 → dropdown 禁用 + 提示"先点击选中";跑任务中 → 禁用(脚本锁定);离线 → 禁用;选中 + 已选脚本 + 有序列 → 启用。
徽标(全部**完全在卡内**):设备卡 active = 蓝色边框 + 右上「✓ 已选中」蓝色 pill;active + running = 右上「⚡ 运行中」绿色 pill;脚本卡 active = 置顶 + 边框 + 左上「▸ 当前选中」蓝色 pill。
**禁止**:"跑 X" 旧 badge(已删除,避免重复)。

### D-25 放电测试"设备已关机"合成卡
背景:放电模式以"设备关机"结束(`stop_reason:"power_off"`),任务结束后设备不在 `adb devices`,旧逻辑会让设备卡直接消失。
在 `renderDevices()` 对"终态 battery 任务 + 设备不在 adb + 该任务仍是设备最新任务"合成 `_batt_off` 卡(虚线红边、"设备已关机"状态 + "放电关机"徽标,`.device-card-batt-off`);设备重新上线自动回正常卡,删任务即删卡。与「临时离线」合成卡是同一手法。

### D-26 静态资源:cache busting + `NoCacheMiddleware`
`index.html` 的 script / css link 带 `?v=x.y.z`(**×3**);server 加 `NoCacheMiddleware`,静态文件 `Cache-Control: no-store`。
**理由**:无构建步骤的纯静态前端,版本号是唯一的缓存失效手段。

---

## 四、报告引擎 (D-27 – D-33)

### D-27 每个脚本的 Overall Result 都出一份中文 HTML 报告(自包含)
用户四点要求:「1. 每个脚本 stdout 的 Overall Result 都需要生成 html 的报告 / 2. 参考 PerfMonitor 的生成逻辑 / 3. 当前已连接 adb 设备 / 4. 需要将 HTML 格式的 Overall Result 的架构和 Params 作为架构写入 md 文档沉淀」。
链路:脚本局部变量 → `rows`(与 print 块**并排构建**)→ `build_payload()` → `write_html_report(json_path, doc)` → `<报告同 stem>.html`。**引擎不渲染 stdout,平台也不解析 HTML** —— 两边唯一的接触面是**文件系统上的 stem 配对**。**不碰现有 JSON 报告**:全库无消费者解析报告 JSON,保持原样 = 零回归;HTML 里有「完整数据」折叠区把 JSON 整体递归渲染进去,保证 **`HTML ⊇ JSON`**。
**自包含**:内联 CSS、无 JS、无图表库 —— 目标是能拷出归档目录、在没装过 PPTP 的机器上、几年后打开还看得懂(四张表 + 四色横幅 + 未标定告警条)。
**有意保留的例外**:`perf_monitor` **保留自己的渲染器**(它那 4 个区块 gates / 通道分布 / 每门明细 / 链路事件比通用引擎能表达的丰富,重写有风险要碰 `--selftest` 而无用户可见收益),**只吸收参数表** —— 那才是它真正缺的。
→ [REPORT_FORMAT.md](REPORT_FORMAT.md)

### D-28 stdout 一个字不改,只加一行 `  html   = <绝对路径>`
「只加 HTML,stdout 一个字不改」—— 三种汇总家族原样并存。**注意用 `=` 而不是 `:`**。**`report :` 行必须仍是最后一行**(见 [PITFALLS.md](PITFALLS.md) #43);perf 的 `--selftest` 有断言守着,**其他脚本没有** —— 改 stdout 要人工过 [REPORT_FORMAT.md](REPORT_FORMAT.md) 的自查清单。
**理由**:控制台是用户实时在看的东西。
**代价**:`rows` 要脚本手写(3~8 行),引擎不能从 print 语句反推 —— 换来的是两处显示的数不可能各算一遍。
**禁止**:改 stdout 现有输出的任何一个字。

### D-29 `ir_runner.py` 跳过,不给它编一个不存在的结论
背景:它没有 `PARAMS`、没有 PASS/FAIL 语义,是 `.ini` 驱动的交互式序列执行器。
**禁止**:给 `ir_runner.py` 强加 HTML 报告。

### D-30 中断时报告与 HTML 都写
「中断时报告与 HTML 都写,报告简单注明一下吧」。**[已裁决,不要再改]**
`wifi_onoff_stress.py` 原先被 `if not interrupted:` 包着的**守卫整条删除** —— 只补 HTML 不可能:同 stem 才能被归档收走,**孤儿 HTML 会被 `_companion_files` 静默丢弃**。判定**一个字不改**(控制台与页面因此不可能互相打架),中断进 `interrupted` / `interrupted_note`(恒在),渲染成页面横幅下那条黄条。
**禁止**:用"中断时不写"当简化手段 —— 那会让 stdout 有 Overall Result 而 HTML 没有。

### D-31 Params 表:顺序遍历 `SCHEMA`;`choices` 两种形状都吃
`render_params_table(values, schema)` 从 `PARAMS` 声明生成三列表(参数 / 本次取值 / **来源**),**顺序遍历 schema 而非值的字典** —— 漏传的参数不会消失(回退声明默认值并注明「本次未传」)。「来源」三态 = 本次未传 / 传入了与默认相同 / **已改(默认 X)** —— **"用户改没改过这个参数"在报告里直接看得见**,不用翻 `summary.json`。
`choices` 有**两种形状**:纯字符串列表 / `{value,label}` dict 列表(后者见 perf 的 `WATCH_PKG_CHOICES`),`_choice_label` 两种都吃,**匹配不上时原样显示该值而不是丢掉**(丢掉会让人以为参数没生效)。bool 渲染「是」/「否」,**绝不打 `True`/`False`**。
常量名是 **`SCHEMA` 不是 `REPORT_SCHEMA`**;`write_html_report` 失败返回 **`""` 不是 `None`**。
**禁止**:按值的字典遍历参数表;匹配不上就跳过该选项。

### D-32 `hide_keys` 在任意深度按键名隐去(**仅 HTML**)
`hide_keys`(如 Wi-Fi 的 `["password"]`)在**任意深度**按键名隐去,并在页面上留「已隐去」标记。
**范围严格限定在 HTML 渲染**:归档的 `report.json` 不做隐去。这条只管渲染层,不要按"泄漏"处理,也不要据此改动归档内容。
→ [REPORT_FORMAT.md](REPORT_FORMAT.md)

### D-33 HTML 报告的链路事件:墙钟在前,节拍第二列
「html 的报告里面的 rollback 和 reboot 等 event 目前只有运行节拍的标记而没有 timestamp,我想要能看到问题发生时的时间如 18:39:12 等,**这个才是重要的元素**」。
**先纠一个词**:用户说的 `rollback` 其实是报告里的 **`rollover`**(计数器回绕)—— `grep rollback` 全仓库只命中 `scrollback` 的假阳性,别按字面去找。
**改动只在渲染层**:每条事件一直同时带 `t_sec` 与 `clock_ms`(`events.csv` 列头早就是 `t_sec, clock_ms, type, reason, detail`,`report.json` 的 `events.items` 里也有),只是 `format_result_html` 第五节当时只取了 `t_sec` —— payload / 判定 / CSV / 限流**一行没动**,`REPORT_FORMAT.md`(共享引擎 schema)也不动。
**两列都留、墙钟在前**:节拍答"在这趟跑的哪个位置",墙钟答"几点几分出的问题" —— 后者才能和另一台设备的日志、一张工单、某人"下午那会儿"对上;悬停给完整 `YYYY-MM-DD HH:MM:SS`(跨零点的一趟跑否则就有歧义)。**降级**:缺失 / 非法时钟渲染 `-`、**绝不抛** —— 同一份 HTML 在收尾时也会从 partial snapshot 渲染一遍。
→ [CHANGELOG.md](../CHANGELOG.md) v2.11.2

---

## 五、perf_monitor (D-34 – D-46)

### D-34 判定在收尾异步打印一次;监控逻辑做进 perf_monitor 本身
「我没有定收尾离线算一次 / 我指的是报告输出在后面输出一份」;「反正要把监控逻辑做进我的 perf 监控,目前我的 perf 仅仅只做了数据打印的活」。
实时流保持是流,**结论只出现一次**。实现:`judge(evidence)` **纯函数**,`--selftest` 断言 `judge(report["evidence"]) == report["verdict"]`。
**禁止**:在平台侧另起一套判定;**把判定搬去服务端计算** —— 违背"监控逻辑做进我的 perf 监控",且脚本单跑就没有结论。

### D-35 采样要省机器:三层分级 + 一拍一次 adb(占空比 **9.06%**)
「采样逻辑看怎么设计对机器消耗更小」。
**禁止**:常驻 adb shell 会话(实测中位数慢 **8.1%**,且把可隔离故障升级为不可恢复故障);全系统 `dumpsys meminfo`(**7.5 s**);每拍 `top`(**581 ms**);PSS / `smaps_rollup`(本机 SELinux 拒绝,拿不到)。

### D-36 事件与判定分开,允许中途预警;禁止 banner / toast / 状态条
「采集通道的『事件』和『判定』应该分开 … 可以,中途可以增加监控预警和提示,你全权负责即可」。→ 事件走 stdout(**限流 60s**),判定只在收尾。
**禁止**:banner / toast / 状态条(沿用 v2.7.2 的否决)。

### D-37 `interval_sec` / `duration_sec` 保留,阈值**不做成 params**
**[已裁决,不要再改]** 背景:阈值被视为**标定值**,写死在代码里;界面只给 `interval_sec` / `duration_sec` 这类运行参数。
用户问「采样间隔在之前的架构里面你不是说会做分级采样吗」—— **答案:它是基准拍周期 T,MED/SLOW 的周期由 T *推导*(目标固定 5s / 30s),分级采样不等于"不需要采样间隔"。**
`PARAMS` **v2.9.0 起收敛为三项**,`key_ini` 于 v2.11.0 加入:`interval_sec`(默认 **2.0**,min 1.0)/ `duration_sec`(**0 = 手动停止**)/ `watch_pkg` / `key_ini`。
**禁止**:把判定阈值放进 `PARAMS`(沿用 v2.8.0 的否决)。

### D-38 `watch_pkg` 由自由文本改 `type:"select"`,预设 `WATCH_PKG_CHOICES` 五项
「关注包名这个我建议是预留几个选项就行了,后面我把常用的那几个压测的提供给你」。**清单已由用户 2026-09-14 定稿,不再是占位**;要增删改 `WATCH_PKG_CHOICES` **一处**,前端零改动(前端本就走 `f.choices`):
`""`(不关注 / 只看前台是哪个应用)/ `com.google.android.youtube.tv`(YouTube TV)/ `com.netflix.ninja`(Netflix)/ `com.amazon.amazonvideo.livingroom`(Prime Video)/ `com.mediatek.wwtv.mediaplayer`(本地媒体播放器)。
**理由**:自由文本里一个拼错的包名会**静默废掉**唯一依赖它的 gate 9(APP),报告看起来一切正常;下拉把"拼错"从可能性里去掉。
**禁止**:退回自由文本;用 `pm list packages -3` 取包名(**YouTube TV / Netflix 在这台设备上是系统应用,`-3` 会把它们滤掉**)—— 要用 `adb shell cmd package query-activities -a android.intent.action.MAIN -c android.intent.category.LEANBACK_LAUNCHER`。
**注意**:**加条目之前先确认设备上真有这个包** —— `watch_pkg` 设了而该包从没上过前台,gate 9 报的是 **LOSS(FAIL)** 不是 inconclusive,等于自己造一个假异常。

### D-39 前台采集恒开,删除 `track_foreground` 开关
「采集前台 APP 感觉没必要做开关吧」。删除 `track_foreground` 开关、`Evidence.track_fg`、CSV 前导的 `track_foreground=`、报告 `config` 的同名字段。
**理由**:「被压测的 app 挂了 / 掉到后台」是长跑里**最该被看见**的故障,`fg_*` 是唯一能看见它的信号;关掉只省一次 `dumpsys window` + `pidof` + `/proc/<pid>/stat`,不值一个开关。
**禁止**:把它加回来。
→ [PITFALLS.md](PITFALLS.md) #42

### D-40 logcat 恒开 + 人工开关(完全照 `serial_capture` 的形状)
**[已裁决,不要再改]** 背景:v2.9.0 考虑过取消 logcat 恒开(恒开的采集会干扰被采集的对象),最终裁决是 **保留 logcat、但增加人工开关** —— 完全照 `serial_capture` 的形状:**设备卡 checkbox + 服务端仲裁,默认仍为开**。
**禁止**:取消 logcat 采集;把开关做成全局设置而不是设备卡 checkbox。
→ [PITFALLS.md](PITFALLS.md) #37

### D-41 控制台渲染:判定块自带解释;未知 PERF 类型绝不回显原始 JSON
「我希望在释出这些指标的时候能带简单解释,排版也可以做好一点」→ 块尾 `--- what these mean ---`,每行一条英文解释;`verdict_rows()` **一张共享 row 表**(控制台与 HTML 同源渲染,两者不可能各说各话)。**stdout 仍然全 ASCII。**
前端 `handlePerfLine` 的兜底分支**只打一行 `(unrecognized record type: X)`**,绝不回显原始 JSON —— console 每次 WS 重连都会重放 stdout,回显等于把原始 JSON 反复刷屏;而且一个新记录类型刚上线时就会触发。

### D-42 判定阈值全部未标定,`judge_version=v1-uncalibrated`
`CALIBRATED = False`。**健康报告也会显示未标定,这是刻意的**:避免假信心。标定需要一次健康的 **8 小时**基线跑。
`judge_version` 与阈值必须写进报告,否则老归档无法用新脚本复算,也无法解释当时为什么判 WARN。残余风险清单见 [TODO.md](../TODO.md) §6。

### D-43 长跑按键 keepalive:线程内 `import ir_runner`,不起子进程、不加 gate
「我后续压测 YouTube | Amazon | Netflix | MediaPlayer 可能存在长时间播放时,应用弹出无操作提示,所以我需要在现有的 perf monitor 的基础上,可以通过调用 ir_runner 发送按键给设备」「我建议是在 perf monitor 的 params 增加可以选红外 ini 的选项,然后直接调用一个后台持续发这个 ini 就行了」「避免过度设计」「只是需要在 perf_monitor 的时候跑 ini 就行了」。
**问题**:长跑中 app 的空闲提示会打断播放,于是样本里混进一段"没人看"的数据,而报告看起来一切正常。
**四条已批准(DO NOT REVERT)**:① **线程内 `import ir_runner`**,不起子进程;② 先给一个 `KEYCODE_MEDIA_PLAY` 模板,**之后要能随便选 ini、随便改**(`key_ini` select 扫 `ir_sequences/*.ini`,跳过 `_` 开头的临时候选);③ **30 分钟**一次(由 ini 的 `delay_ms` 决定);④ **只有 perf_monitor 需要** —— 其余 7 个脚本与平台逻辑**零改动**。
**理由**:平台的"硬停"走 `proc.kill()`(`TerminateProcess`),**不发任何控制台事件** —— 子进程会**活下来继续按按键**,直到下次重启服务器才被 `_reap_orphan_scripts` 扫掉;daemon 线程随进程一起消失。真机验证:硬停后 `killed:1` / `exit=-9`,再等 16 秒(3 个以上按键周期)**无任何进程还在按键**。
**禁止**:子进程方式;**`run_loop`** —— 它 `while True`,唯一出口是自己那个线程里的 `KeyboardInterrupt`,而 Windows **只把控制台事件投给主线程**,那个异常永远不来;再加第二个节奏参数(ini 的 `delay_ms` 就是节奏,单一真相);**给 keepalive 加 gate** —— 它是**测量条件**不是被测对象,故障只进报告 `config`(`key_ini` / `key_sent` / `key_failed` / `key_status`),**永远不改变判定**,ini 写坏 / 按键名不认识 / 设备掉线全都只进 `status`。
**实现要点**:以"**一次按键**"为单位调 `run_step`(可中断的等待 `stop.wait` 而非 `sleep`、精确的 `key_sent`;teardown 期间不再按键 —— `count>1` 的步**拆成 `count=1` 副本逐次调用**,`run_step` 自己的重复循环不可中断,一个 50 连发的步在停止后会一路按完)。**派发细节(Short/Long、`KEYCODE` vs `sendevent`)仍全部归 `run_step`。**
→ [CHANGELOG.md](../CHANGELOG.md) v2.11.0、[PITFALLS.md](PITFALLS.md) #47

### D-44 perf / battery 的图表系列由 `meta.sources` 驱动
`src.gpu` 真值才画 GPU 绿线;`src.fg` 真值才画"前台 APP"红线;CPU 蓝 / MEM 黄恒有。`perfSeriesAndKeys` 重构**同意执行**,但**仍只服务 `perf` / `battery` 两个 kind**,改为由 `meta.sources` 驱动。
**[已裁决,不要再改]**
**禁止**:**自动泛化 `PERF_SCRIPT_KINDS`**;实现"脚本自描述 series"那套 —— 「不需要自动拥有图表,后面我一个一个让你改代码就行了」,这是对"过度设计"的明确否决。
→ [PITFALLS.md](PITFALLS.md) #19

### D-45 perf_monitor 明确不做清单
以下条目**逐条已否决**,不要再提、不要再实现(注:pandas / numpy / matplotlib / openpyxl **本机其实都装了**,拒绝的理由是**设计约束与报告契约**,不是环境缺库 —— 所以"装了就能用"不构成理由):
- 常驻 adb shell 会话 —— 实测中位数慢 **8.1%**,且把可隔离故障升级为不可恢复故障
- 全系统 `dumpsys meminfo`(**7.5 s**)、每拍 `top`(**581 ms**)
- PSS / `smaps_rollup` —— 本机 SELinux 拒绝,拿不到
- 自采 logcat / `logcat -c` / `setprop debug.sf.fps` / `adb root` —— 违反平台铁律(D-01),`adb root` 还会重启 adbd 打断其它任务的采集
- FPS —— 本机无可信来源:mFps 是 RK 私有补丁;SurfaceFlinger `--latency` 的 **127** 条环缓冲在 60 fps 下只有 **≈2.1 s** 历史,与 **2 s** 采样结构性冲突
- pandas / numpy / matplotlib / openpyxl
- 自动泛化 `PERF_SCRIPT_KINDS`(见 D-44)
- 判定搬去服务端计算(见 D-34)
- 开跑前检查磁盘空间 / 任何形式的自动清理(用户明确否决,见 D-11)

### D-46 "完成"的浏览器眼验由用户后续实跑确认,不作为编码阶段的前置门槛
**[已裁决,不要再改]** 编码阶段的"完成" = `python -m py_compile` 通过 + 相关脚本 `--dump-params` / `--probe` 短跑验证 + 版本号全库一致。
**理由**:真实浏览器里的渲染 / 点击效果**未证**是已知残余,如实记录即可,不要把它变成卡住编码的门槛,也不要把"能跑"说成"验证过了"。

---

## 六、脚本契约 (D-47 – D-52)

### D-47 脚本自描述参数 schema,前端零硬编码
脚本定义模块级 `PARAMS` 列表 + 支持 `--dump-params`(打印 `{"fields":[...]}`,ASCII 安全)。后端 `GET /api/scripts/{name}/params` 跑 `python -u script.py --dump-params`,按 `(name, mtime)` 缓存(`_PARAMS_CACHE`);`_list_scripts()` **源文件嗅探** `--dump-params` 标 `has_params`(纯文件读取,不启子进程)。前端看到 `has_params` 就在脚本卡上支持点击弹窗,按 `type`(`int` / `float` / `bool` / `str` / `select`)渲染表单;`select` 配 `choices` 列表(**字符串或 `{value,label}` 对象**),渲染 `<select>`,`saveParams` 用 `#modal-params [name=...]` 选择器同时匹配 input / select。
**理由**:新增脚本只需定义 `PARAMS`,平台 / 后端 / 前端全都不用改 —— 与 ir_runner 序列选择的"自描述"思路一致。
**禁止**:在前端为某个脚本硬编码参数表单。

### D-48 脚本参数:硬编码常量 vs 前端可配,由用户逐条裁决
- `wifi_switch_stress` 的 **`WIFI_NETWORKS` 硬编码不做成 param** —— 要换网络直接改脚本顶部;但 **`use_su` 保持为参数**(部分设备免 root,可关掉走 `cmd wifi` 直接调)。
- `app_launch_stress` 的被测 APP **硬编码脚本顶部 `APP_PRESETS`**:**「要跑就三个一起跑」**,内置 Netflix / Prime Video / YouTube TV(包名 + Activity 来自用户真机 `dumpsys window` 抓取),按顺序每个各跑 `iterations` 轮,**每个 APP 一份报告 + 各自 p95 判定,整体全过才 PASS**;中途某 APP 无法解析启动 Activity 也判 FAIL。
- `sensor_reboot_stress` 的 `SENSOR_WINDOW` / `POLL_COUNT` / `POLL_INTERVAL` / `PASS_THRESHOLD` 同为**硬编码常量**(按用户要求,改脚本顶部)。
**禁止**:把上述常量挪进 `PARAMS`。

### D-49 `app_launch_stress` 的真机坑(改脚本务必牢记)
- `am start -n 包名/.Activity` 的 `.` 简写**只在 Activity 属于该包名命名空间时成立**。Prime Video 主 Activity 是 `com.amazon.ignition.IgnitionActivity`(别家包名),YouTube 的 `MainActivity` **未导出**、可启动入口是 **`ShellActivity`** —— 所以 `APP_PRESETS.activity` 一律存**全限定类名**(不带前导点),脚本拼 `pkg/full.Class`。
- logcat `Displayed` 有 `+592ms` 和 `+1s291ms` 两种格式,解析要分态换算(曾把 `+592ms` 误读成 592 秒)。
- `LaunchState` 语义校验为**硬校验**:与模式矛盾该轮判 NG 不计样本;`TotalTime: 0`(intent 投给已置顶实例)也判 NG;系统不报状态(`UNKNOWN` / 空)时回退进程级校验(force-stop / pidof)。
- **双指标交叉验证**:主指标 = `am start -W` 的 `TotalTime`(ms);清空抓 logcat `Displayed` 比对(冷启动必有,热启动通常缺失 → 缺失时以 `LaunchState` 语义校验为准)。
- `mode` 下拉 `cold` / `hot` **分开跑**:冷启动每轮先 `am force-stop` + `pidof` 确认后台进程已死再测;热启动开始前确保进程存活(死了先冷拉起一次不计样本),每轮 HOME 退后台再拉起。

### D-50 `bt_reboot_stress` 回连判定:**蓝牙开着 ≠ 音箱连上**
`check_bt_connected`:`dumpsys bluetooth_manager` 必须**同时满足** ① 适配器已开启(`enabled: true`);② 至少一个 A2DP 状态机处于 `mConnectionState: CONNECTED`。A2dpStateMachine 只在 A2DP 音频设备存在,故该标记 = 音箱真正回连(真机实测正常回连 ~6s)。
**只测重启回连,不测开关回连(用户明确)**:本设备**蓝牙开关未对用户开放**,用户无法手动关蓝牙 → 不做 "disable→enable" 场景。
**真机边界行为(重要)**:音箱**刚连上后立即重启** → 回连失败(A2DP CONNECT → 30s CONNECT_TIMEOUT → DISCONNECTED,甚至手动 toggle 蓝牙也连不回);用户在蓝牙设置里**重新手动连一次**后重启 → ~6s 自动回连,脚本 PASS。疑似音箱休眠 / 连接未稳定。**正式压测前先手动确认音箱能连上**(已写入 README 注意事项)。

### D-51 `battery_inout_stress` 的 `PARAMS` 与单次中止信号
`PARAMS`:`mode`(select `charge` / `discharge`,**单次只跑充电或放电** —— 充放电做不到自动插拔,由用户选)、`serial_port`(select,`--dump-params` 时用 `serial.tools.list_ports` 枚举当前 COM 口;**必须由用户在试跑时确认并自选**)、`interval_sec`(默认 **10**)、`temp_warn_c`(默认 **45**)、`full_hold_sec`(默认 **60**)。
**单次中止信号**:① charge 模式 `level>=100` 且 `status=="full"` 或连续满 `full_hold_sec` → `stop_reason="full"`;② 任意模式连续 **2** 次电池读取失败(设备关机 / adb 失联)→ `stop_reason="power_off"`;③ 手动 SIGBREAK。
**数据源(真机探明)**:`adb shell dumpsys battery` **默认只缓存**,必须经**串口 console**(115200 baud)发 `cmd android.hardware.health.IHealth/default set polling true` 才实时;SELinux 挡 adb 路径,**串口是唯一实时数据通道**;轮询需每 `SERIAL_RE_ENABLE_MIN` 分钟重发保活。COM9 = FTDI(VID:PID `0403:6010`)。
**错误检测保留(原脚本逻辑)**:电量跳变检测(方向感知 `abnormal_rise` / `abnormal_drop` / `abnormal_jump`,`JUMP_THRESHOLD = 2`)+ 温控告警(`temp >= temp_warn_c`)。**不输出 Excel**(前端三件套导出代替)。CSV 表头 `t_sec,t_wall,level_percent,temp_c,voltage_mv,status,jump_delta,jump_type`,导出后缀 `.batt.csv`。

### D-52 脚本必备的 PERF 契约字段:`type` 与 `clock` 缺一即断链
每采样打一行 `PERF|{"type":"sample","clock":<epoch_ms>,"t":...,...}`,启动打 `PERF|{"type":"meta","sources":{...}}`(`json.dumps ensure_ascii=True`)。前端在 `appendLogLine` 拦截 `PERF|` 前缀,以 `obj.type === "sample"` 分发。
**`type` 与 `clock` 是前端分发的硬依赖字段,缺一即断链** —— v2.4.1 三个症状齐发(图表不出线 / 横轴无时间点 / 导出只剩 txt)的全部根因就是 sample 行漏了 `"type":"sample"`,脚本 docstring 写了、代码没实现。
**禁止**:省略 `type` / `clock`;靠 docstring 而不是端到端实证确认 wire 契约。
→ [PITFALLS.md](PITFALLS.md) #17

## 七、perf_monitor WiFi 链路监控 (D-53 – D-56)

> v2.12.0,2026-09-20。用户原话:「perf_monitor 新增监控项 / WiFi status / 采样的间隔和频率需要确认一下,这个的采样频率也不需要很高,只需要能保证查看报告时能确认是哪一段时间断的网,视频平台是什么时候报的 connection error 即可」。完整规格见 [PERF_MONITOR_V2.md](PERF_MONITOR_V2.md) §12.10。

### D-53 WiFi 链路是**条件通道**,不是第 10 个 metric
`SECTIONS` 里 `("WIFI", SLOW, ())` —— **metrics 元组留空**。CSV 走追加的 `wifi` / `wifi_st` 两列(28 → **30 列**),evidence 走**顶层 `"wifi"` 键**(与 `memory`/`cpu`/`fg` 同级,**不进 `metrics`**),判定走伪门。
**理由**:`METRIC_ORDER` 的 9 结构性耦合在 `ST_LINE_RE` 的 `{9}`、CSV 列块、分布表、定宽控制台行四处;
**禁止**:把 wifi 变成第 10 个 metric,或改动 `st = "<res>:<9 chars>"` 的契约。WiFi 链路是**测量条件**,不是被测对象 —— 与 D-43(keepalive 不设门)同构。

### D-54 WiFi 采样档位 = **SLOW(≈30 s)**,这是**用户裁决**,不是可顺手调的旋钮
`WIFI_TIER = TIER_SLOW`(换档只改这一行)。分辨率由 `wifi_resolution_s()` **推导**,不是硬编码 —— 改档位不会留下过期数字。
**必须如实印出的代价**:采样间隔 P = 15 × T = 30 s,而不确定度是 **±P 而非 ±P/2**(采样点是"最后一次已知良好",不是区间中点);**短于 30 s 的瞬断可能两个采样点都采到"联网中",报告里完全看不出来** —— 连带 D-55 的 WARN 封顶也一起漏触发。
**禁止**:把边界说成精确的;把 `resolution_s` 从 evidence 里拿掉;在 `ROW_NOTES` / HTML 免责说明里省掉"<30 s 可能整段漏掉"这句话。`WIFI_RESOLUTION_NOTE` 是这三处的**唯一来源**。

### D-55 WiFi 中断把结论**封顶为 WARN**,走伪门通路,**不新增 gate**
`judge()` 里 `caps.append({"id": -3, "name": "WIFI", "status": "warn", ...})`,且**仅当 `result == "OK"` 时**改成 `"WARN"`。`caps` **永不进 `gates`** ⇒ `ids == [1..9]` 的 selftest 断言不受影响。
**两条刻意的不对称**:① `caps.append` **无条件执行**(即使已经被别的东西封过顶)⇒ 原因永远不会丢;② **只降 `OK → WARN`,绝不覆盖 `FAIL`** ⇒ 一次真正的设备故障不可以被"当时网断了"洗白。
**理由**:keepalive(D-43)完全不碰结论,而断网会让视频平台报 connection error ⇒ **这次跑的结论就不干净了**。封顶是"不许把条件不干净的跑说成满血",**不是**"把网络故障算作设备故障"。
**已知空洞(不在本次范围,[TODO.md](../TODO.md) §6)**:`capped_by` 算出来了但**全仓没有任何渲染层读它**,所以 `-1`/`-2`/`-3` 从来没在报告上露过面。⇒ 原因必须**显式渲染**:`WIFI` 行的值自带摘要 + HTML `六、` 小节写明封顶。

### D-56 WiFi 采集的四条**禁止项**
① **不加第 10 个 metric**(D-53)。
② **不用 `dumpsys wifi` / `/sys/class/net/wlan0/operstate` 兜底** —— 单一真源 `cmd wifi status`;两个来源 = 多一种失败模式。`operstate` 只看链路层,看不见"关联了但没 IP",而那正是视频平台最容易报 connection error 的状态。
③ **不 ping** —— 实测 **1206 ms**,比整条 FULL 复合命令还贵,且引入外部 IP 依赖(用户裁决)。
④ **不读 logcat** —— 守住 v2.6.0 铁律「**日志采集归平台,不归脚本**」。本功能只出**时间线**,与归档的 `.logcat.log` **人工比对**(用户裁决)。
**另**:读取失败**不等于**断网(读失败是"不知道",绝不记成一次中断);断网**不能让这一拍变 `partial`**(会让 `ok` 掉到 0 ⇒ gate 1 DATA 判 inconclusive,把一次健康的 CPU 压测报成 INCONCLUSIVE)。两条各有纯函数钉死并进 `--selftest`。

## 八、perf_monitor NTC 节点温度与监控项勾选 (D-57 – D-63)

> v2.13.0,2026-09-20。用户原话:「新增 NTC 节点温度数据监控 / `LCD_Path = /sys/bus/iio/devices/iio:device0/in_voltage3_raw` / `LED_Path = .../in_voltage2_raw` / cat 出来后的数据不是摄氏度,需要换算 / NTC 温度监控不作为 gate,不做卡控,纯监控,曲线绘制建议不做到前端渲染,前端渲染的先仅支持当前项,图表渲染走 excel 绘制 / overall result 需输出简要信息,周期平均温度,最高温度,无需太过复杂,只是一个单纯的监控项 / 后续 perf_monitor 需提供监控指标的取消和选中 / 单次运行可选择哪些需要监控,哪些不需要」。完整规格见 [PERF_MONITOR_V2.md](PERF_MONITOR_V2.md) §12.11 与 §12.12。

### D-57 NTC 是**条件通道**,不是第 10 个 metric
`SECTIONS` 追加 `("NTC", SLOW, ())`(在 `WIFI` 之后)—— **metrics 元组留空**。CSV 走追加的 `ntc_lcd` / `ntc_led` / `ntc_st` 三列(30 → **33 列**),evidence 走**顶层 `"ntc"` 键**(与 `memory`/`wifi` 同级,**不进 `metrics`**),判定走 `gids=()` 的**纯事实行**。
**理由**:与 D-53 **逐字相同**(`METRIC_ORDER` 的 9 结构性耦合在四处;温度是被测对象的**观察**,不是被测对象本身)。
**禁止**:加第 10 个 metric;改 `st = "<res>:<9 chars>"` 的契约;给 NTC 加事件 kind(它不是状态机,只有一条连续序列)。

### D-58 NTC 采样档位 = **SLOW(≈30 s)**,这是**用户裁决**
`NTC_TIER = TIER_SLOW`,与 WIFI / FOCUS 同档。分辨率由推导而来(evidence 带 `resolution_s`,报告行印 `res=`),不硬编码。
**必须如实印出的代价**:周期平均与最高值只覆盖**真实读数**(`n=` 给出条数,SLOW 档下多数拍走 ZOH 且不计入),而不是全部采样周期。

### D-59 NTC **不做 gate、不做卡控、前端不画曲线**(用户裁决)
不新增门、不进 `caps`、不封顶,与 WIFI(D-55)**刻意不同**:WiFi 的"坏"语义明确,而温度的"坏"要先有换算公式与阈值 —— 公式虽然拿到了,但它落在**下游的离线工具**里(D-63),不在采集链路上,采集侧永远只有计数。
**前端**:`PERF|` 采样行会带上 `ntc_lcd` / `ntc_led` 两个键,但**不加 series**。**曲线归 Excel**(X 轴 `t_sec`,Y 轴 `ntc_lcd` / `ntc_led`),README 里写明用法。
**禁止**:给 NTC 加门或封顶;在前端为它建 series;为它新增 HTML 小节(它会自动出现在逐项判读表里)。

### D-60 采集侧**落原始 ADC**,单位四处同时声明 · ~~「公式到位后只改这一处」已被 **D-63** 修订~~
`parse_ntc()` 是每个 NTC **计数**必经的唯一漏斗,它返回计数,且**只**返回计数。当前输出是**原始 ADC 计数**,由 `# ntc_unit=raw_adc` 表头行、CSV 的 `ntc_st` 列、报告行的 `unit=raw_adc`、`ROW_NOTES` 四处**同时声明**。
**为什么原始列名不加 `_raw` 后缀**:归档里这两列**永远**是计数(公式虽然已经拿到,但它落在下游工具里 —— D-63),改名只会让归档无法跨版本对比。**为什么不加 `ntc_lcd_c` 列**:换算不在采集链路上,采集侧**不产出**摄氏度 —— 这不是"还早",是分工。
**禁止**:在报告或文档里把这串数说成温度而不带单位声明;把换算实现放在 `parse_ntc()` 之外。
**已知代价**:两个通道一次 `cat`、**段内没有 per-channel 标记**,所以"只读出一个"时看不出是哪个文件失败(表现为那一列永久空 + `n=0`,**而不是** `a` 状态)。这是"无需太过复杂"的代价,记进 [TODO.md](../TODO.md)。

### D-61 取消门口的依赖项 ⇒ 该门**强制 `inconclusive`**,绝不静默 pass
`MON_GATE_OF = {"mem": (7,), "fg": (8, 9)}`;九道门建好之后、`worst` 之前按 id 覆盖成 `inconclusive` 并写明原因,本次结论随之变 `INCONCLUSIVE`。**门 id 仍是 1..9**,门表契约与所有 `range(1, 10)` 断言不受影响。
**理由**:`fg_due < 2` 这类分支**分不清"没勾选"和"跑太短"**;不覆盖的话,报告会声称一个**从未被采集**的通道是健康的 —— 那正是本次功能要堵掉的东西。`INCONCLUSIVE` 优先于 `FAIL`/`WARN`,所以取消勾选**不可能**让一次跑看起来更好。
**禁止**:让取消勾选后的门报 `pass`;用新增伪门来传达范围(伪门 `capped_by` 全仓无人渲染);把 `monitors` / `monitors_off` 写到 evidence 的**顶层**(必须在 `run` 字典内 —— 写错层级时报告照样渲染、`SCOPE` 照样有值,而覆盖**从未生效**,这个 bug 真实发生过)。

### D-62 监控项勾选的四条**禁止项**
① **FAST 三项(`cpu` / `procs` / `up`)永久不可取消** —— `up` 既是 reboot 守卫又是 `cpu%`/`gpu%` 差分跨重启存活的前提,STAT/PROCS 是每拍骨架;去掉省不了多少(FAST ≈ FULL 的 30%)却会**悄悄毁掉所有差分**。
② **取消粒度是段组,不许下探到 metric** —— `gpu`/`gpu_clk` 由设备侧一次 `su 0 cat` 读两个文件,`fg_pkg`/`fg_pid`/`fg_cpu` 共享设备侧同一个 `$P` 变量。这是设备命令的形状,不是实现偷懒。
③ **开关量只能是一个集合、算一次、两个调用点共享** —— `off_sections` 取代 `gpu_enabled`/`wifi_enabled` 两个形参。此前 `tick()` 调 `build_tick_command` 时**漏传**了 `wifi_enabled`,WiFi 降级后仍每拍发 `@@WIFI`(见 [PITFALLS.md](PITFALLS.md) #51)。**禁止**再引入第二个开关来源。
④ **不许让取消勾选改变 CSV 的列或门的编号** —— 取消只改"采不采",不改"长什么样";`SCOPE` 行与 `# config_monitors=` 表头行负责自述,取消**不动存档结构**。

### D-63 换算**不进 perf_monitor** —— 它是独立的离线工具 `tools/ntc_convert.py`
> **本条修订 D-60 的「公式到位后只改 `parse_ntc()` 一处」。** 用户 2026-09-20 裁决:「我建议 adc 转摄氏度先不在 perf 里面做修改,可以 perf 只输出 adc 的 excel,然后调用另外一个工具脚本来换算,然后这个工具脚本的目录存一下每个项目不同的一些参数,目前看起来只是一些参数值可能不一样,但是公式是一样的」。用户 2026-09-20 补充:「暂时先这样,后面反正 parse 是单独的脚本,改起来也方便」。

**落点**:仓库根 `tools/ntc_convert.py` + `tools/ntc_profiles/<项目>.ini`(默认档 `default.ini`,取自 EcoPro / MT9676 的厂商配置)。三个待定项均已用户裁决:**profile 位置** = 仓库根新建 `tools/`;**输出** = 转换后的 CSV **与输入同目录**(`<原名>.temps.csv`);**B 值** = ~~暂时以配置为准~~ → **2026-09-21 由 D-64 推翻:以实际运行的代码为准**。
**为什么必须分开**:驱动换算的常数(分压电阻 / B / ADC 满量程 / 逐通道补偿)**按项目变**。把某一块板的常数焊进采集脚本,换一块板子就会**静默算错**;分开还有第二个好处 —— 归档的 `samples.csv` 永远是原始计数,跨项目、跨公式版本都能对比。
**输出契约**:`<原名>.temps.csv` 写在输入旁边,**输入永不被修改、永不被移动**(归档属于产生它的那次运行)。列 = `t_sec, ntc_lcd_raw, ntc_led_raw, ntc_lcd_c, ntc_led_c, ntc_st, ntc_lcd_st, ntc_led_st` —— 原始列**原样保留**(换算结果永远可复核);`ntc_st` 是源文件的状态字符;后两列是**本工具的逐通道换算状态**(`ok` / `oor` / `bad` / `-`)。统计**只对 `ntc_st == 'f'` 的真实读数**算,与报告行 `n=` 同口径。
**超量程只标记、不钳位**:超范围要么是真故障,要么是 profile 常数填错,两种都必须让人看见;钳掉就把后一种变成一个**看着合理的数**。
**B 值分歧记录在案(不许悄悄抹平)**:厂商参考实现在算出的分压电阻 > 3588 Ω 时会换成 `B = 3950`,而配置里只有单一 `B = 4010`。裁决是**以配置为准**(用户原话:「现在暂时是按配置里面的」)。在这块板子实际读到的区间内两者相差 < 0.05 °C,但**这不是等价** —— 工具输出表头的 `b=` 会把它印出来,真到了有差别的那天必须重议,而不是就地改常数。
> **⚠ 2026-09-21:上一段的裁决已被 D-64 推翻。原文保留,不抹决策史。** 触发条件正是上一段自己写下的那条 —— "真到了有差别的那天必须重议";那天到了。
**顺带补掉 D-60 的已知代价**:源文件两个节点**共用一个 `ntc_st`**,半失败无法归因(只能靠哪一格是空的来区分)。这个工具输出**逐通道状态列**,在派生文件里把归因补上了 —— 采集侧的代价仍然存在,只是不再传导到结论里。
**禁止**:把换算搬进 `perf_monitor`(D-59 / D-60 的结论不变);让工具改写或删除输入的 `samples.csv`;对已换算的文件再换算一次(靠表头 `ntc_unit=` 守卫,缺声明即拒绝);超量程钳位;把某块板的常数写进 `perf_monitor` 或写进工具的默认档以外的地方。

> **同日追加(2026-09-20):图也归这个工具。** 用户:「需要生成图表而不是单纯做转换,根据转换后的摄氏度做成图表」。所以同一次运行还产出 `<原名>.temps.png`,**落点规则与 CSV 完全一致**(输入旁边;`--out` 同时改两者;`--force` 同时覆盖两者 —— 新 CSV 配旧图比没有图更糟),**这不是对上面任何一条的修订,是同一个工具的第二件产物**。
> 两条画法被钉死,因为它们各自是一条关于**分辨率**的断言:①**阶梯 + 真实读数打点**,不连斜线 —— 30 秒一格、采集侧保持读数,零阶保持本来就是分段常数,连斜线等于宣称中间那些**没测过**的时刻的温度;缺读处**逐通道**断线。②**读数密度写在副标题里**,周期从数据算(真实读数的**中位**间隔,不是均值 —— 一次读失败留下的双倍间隔会被均值摊到所有间隔上),只有一个读数时**不猜**。
> 配色不是口味问题:两组色值都过了分类色板校验(明度带 / 彩度下限 / 色盲可分辨度 / 常视可分辨度 / 对比度),**自检逐字钉住这六个色值**,换色必须重跑校验。默认浅色(要贴进文档的那张),`--theme dark` 是**另一套独立色值**,不是浅色反相。**单轴** —— 两个节点都是摄氏度就共用一根 Y 轴,**绝不做双 Y 轴**。
> **matplotlib 是可选依赖**:没有它时工具**照常出 CSV** 并打一行 `[warn]`,**绝不静默跳过**(「CSV 好好的、图不见了」正是让人白找一通文件的状态);自检的渲染层断言在这种情况下打 `SKIP`,**不算 PASS**。

### D-64 B 值分两段 —— **以实际运行的代码为准**,配置降为参考(2026-09-21)
> **本条推翻 D-63 的「B 值 = 暂时以配置为准」那一句。** D-63 写于 2026-09-20,当时手上**只有厂商的配置文件**;2026-09-21 用户提供了参考实现 `transformNTCToTemp(int adcVal)` 的原文,前提变了。D-63 的原文**保留**,加了推翻标记 —— 不抹决策史。

**事实**:那段 Java **通篇字面量**(`4700F` / `1023` / `1000` / `273.15` / `25` / `10*1000` / `4010` / `3588F` / `3950` / 返回 `-1`),**一个配置都不读**。所以板子上跑出任何一个温度,用的都是那些字面量,不是配置文件。
> **有一件不确定的事,不替谁推断**:配置里 `NTC OBTAIN TEMP WAY = 1` 说明**某处**读过它来选算法,所以不能说"这份配置整体是死的";能确定的只是"**实现公式的那个方法不吃配置**"。厂商原本是否打算让这些常数由 ini 驱动 —— 这正是用户后续要找开发确认的问题。

**用户裁决(原话)**:「按 Java 实际函数做最终的,配置可以留作 temp_template,后续我找开发确认下」。

**落点**:
- `tools/ntc_profiles/default.ini` 的数值**取自参考实现**;厂商配置**原样存档**到 `tools/ntc_profiles/templates/temp_template.ini`(含中文注释与转写损伤,给"找开发确认固件到底该读哪些键"那场对话用)。放在**子目录**里,而 `--list-profiles` 的 glob **不递归** → **看不见也选不着** —— 故意的:它没有冷端 B,拿它当 profile 会**静默**按旧规则换算这块板子。
- 三个新键承载第二段:`NTC B VALUE COLD` / `NTC B VALUE COLD2`(= 3950)+ `NTC B SWITCH R`(= 3588,**欧姆**)。**要么都给、要么都不给**;只给一半 → **报错**,不半套用。都不给 = 单 B 换算,对"实现里没有这段切换"的板子仍然正确。
- 工具的 `# params=` 表头行**始终**打印 `b_cold=` 与 `b_switch_r=`,缺席时写 `none` —— 归档的 `temps.csv` 必须能自述它是按哪种规则算的。

**`3588 Ω` 是什么**:它是一个**温度写成电阻的样子** —— 用 `B=3950, R0=10 kΩ, T0=298.15 K` 反解,`3588 Ω` 正好是 **50.00 °C**(同一算式在 `B=4010` 上解得 `3532.8 Ω`,所以 3588 只可能来自 3950 一侧)。规则读作:**冷于 50 °C 用 3950,50 °C 及以上用 4010**。判据写在**电阻**上、用的是**严格大于**,因为参考实现在算出温度之前只拿得到电阻;工具保持同一顺序、同一算符。

**为什么这条比看上去重要**:工具原来的 `REFERENCES` 真值回归表**从来没有锁住厂商的行为** —— 7 个锚点逐一对过,**全部**等于单 B 列,没有一个对得上厂商的两段规则。旧表测的是工具自己,不是固件。新表两列都留(单 B / 两段),断言打在**两段**列上。

**实际差别(量化,不是印象)**:LCD 节点实读区间(adc 661–666)**+0.045 ~ +0.053 °C**;LED 节点(367–376)**0.000 °C**(其阻值 2674 Ω 到不了切换点);冷端 adc=800 **−0.163 °C**。归档的 `samples.csv` **不受影响**;已归档的 `*.temps.csv` **不追溯修改**,但重新换算会给出略不同的值,两者可由表头 `b_cold=` 区分。

**禁止**:把配置抬回成真源(除非开发确认固件确实读它 —— 那时**重议本条**);只给一半的分段键还继续跑;把 `templates/` 里的档当 profile 用;因为"只差 0.05 °C"就把分段逻辑删掉。

> **2026-09-21 更名附录(仅记名,不改上文):** 本条与 D-63 原文里写的 `tools/ntc_profiles/default.ini` 已更名为
> `tools/ntc_profiles/9660_P53_2G.ini`(用户 2026-09-21:「将当前的这个 ini 命名为 9660_P53_2G,这是该项目的名称」)。
> 上面那些句子里的 `default.ini` **保持原样不重写** —— 那是当时的决策原文。**但** D-63 里"默认档"这个概念本身
> 在 2026-09-21 被 D-66 收紧了:工具的默认档不再是"名叫 default 的那一份",而是"目录里恰好只有一份时才敢用"。

### D-65 跑完**自动换算** —— 挂在 `finish()` 末尾,只调用不内联,失败绝不影响结论(2026-09-21)
> **本条结掉 [TODO.md](../TODO.md) §6.19。** 那条记录的原始风险(「换项目时忘了换 profile,就会悄悄出一份用错参数的摄氏度」)由 **D-66** 正面回答,不是靠事后核对表头。

**落点**:`Monitor.finish()` 的**最末尾**(`_write_reports` 之后、`return` 之前)调一次 `Monitor._convert_ntc()`;它 subprocess 调 `tools/ntc_convert.py <本次的 samples.csv> --profile <ntc_profile>`,产出 `<stem>.samples.temps.csv` 与 `<stem>.samples.temps.png`,与 `samples.csv` **同目录** —— 也就是 D-63 钉下的那条输出契约,**没有第二处路径推导**。

**触发条件只有一个:`Evidence.ntc_fresh > 0`。** 取消勾选时 `note_ntc` 根本不被调用,所以"没勾选"和"一次都没读成功"两条路落在**同一个**计数器上 —— 少一个判据就少一处漂移,这正是 [PITFALLS.md](PITFALLS.md) #51 的教训,也是 D-62 ③ 的同一个形状。

**为什么放在报告之后**:报告已经写完并落盘,这一步**无论如何都动不了本次结论**。它是派生装饰:工具缺失 / 超时 / 拒绝 profile,运行**照常结束**,只多一行 `[ntc] no conversion: <原因>`。这与 D-59「NTC 不开门」同向 —— 温度从来不是判定对象。

**为什么只调用不内联**:D-63 的结论不变。进 `perf_monitor` 的**没有一个是换算常数**;唯一新增的项目相关物是参数名 `ntc_profile` 的**默认值**,而那是**名字不是常数**,且由 D-66 裁决其边界。

**输出纪律**:只回显工具自己的 `profile` / `source` / `wrote` / `chart` / `[warn]` / `[error]` 行,每条一行、前缀 `[ntc] `、**全 ASCII**(D-07);工具那张统计表**不回显**(控制台是设备带载时人在读的流,不是一张表)。**不自己推算输出路径** —— 路径推导只有工具一个真源。**分隔符 `" : "` 必须被去掉**:工具原文是 `wrote   : <路径>`,而那条路径含 `reports/stress-test/perf/`,于是 `" : "` 与 `report` 会同时出现在一行里,正是 `server._sniff_report_path` 要找的形状;去掉分隔符后**再也没有**被误认成报告行的可能(且报告路径仍是 stdout **最后一行**,D-28 不变)。

**代价(明写,不藏)**:matplotlib 冷启动让收尾多花几秒(预算 120 s),任务卡会多转一会儿。**硬停(SIGKILL)那条路径不触发换算** —— `finish()` 根本没跑,归档里只有原始 ADC;**不加"归档时补算"的第二个触发点**(两个触发点各持一半规则 = PITFALLS #51 的形状),记进 [TODO.md](../TODO.md) 让人手工补。

**禁止**:把换算常数搬进 `perf_monitor`;在 `perf_monitor` 里推算 `temps.csv` / `png` 的路径;让这一步的失败影响退出码或报告;给换算加第二处触发点。

### D-66 profile 变成**前端可配参数**,工具的默认档规则**收紧为"多于一份必须显式指定"**(2026-09-21)
> **本条回答 [TODO.md](../TODO.md) §6.19 原文里那条自己写下的担忧。** 原文:「换项目时忘了换 profile,就会悄悄出一份**用错参数**的摄氏度。工具的 `# params=` 表头行印了实际用的常数,但那是事后核对,不是事前防错。」

**用户 2026-09-21 的两条裁决**(原话):profile 来源 = 「加前端可配参数」;默认档规则 = 「收紧:多份时必须显式指定」。

**落点(采集侧)**:`PARAMS` 新增 `ntc_profile`,`type:"select"`,`choices` 来自 `_ntc_profile_choices()` 扫 `tools/ntc_profiles/*.ini`(裸名、**非递归**、跳过 `_` 开头),默认 `9660_P53_2G`。**逐字对照 `_key_ini_choices()` 的既有先例** —— 同一理由:定值列表优于自由文本,因为名字打错会**静默什么都不发生**;而这里更糟,是**静默算出一个看着合理的数**。目录不存在或为空时回落成"只有默认项一项",绝不给前端一个空下拉框。

**落点(工具侧)**:`--profile` 的默认值从死常量改为 `resolve_profile_name()` 的判定,规则是 **`ntc_profiles/` 下恰好一份 `.ini` 时才敢用默认档;两份及以上,`--profile` 变必填**,不填就报错退出并**列出可选项**。今天只有一份,行为一个字不变;第二个项目进来那天,工具**拒绝猜**。
`SHIPPED_PROFILE_NAME`(本仓随附那份的名字)**永远不能变成隐式回落** —— 一旦它可以,上面这条守卫在第二份 profile 出现的瞬间就被架空。这就是解析逻辑写成函数、而不是 `args.profile or SHIPPED_PROFILE_NAME` 的原因。

**边界说明(说清楚,不让它看起来像一次偷偷的例外)**:D-63 说的是"不许把某块板的**常数**写进采集脚本"。进 `perf_monitor` 的是 `NTC_PROFILE_DEFAULT = "9660_P53_2G"` 这个**名字** —— 没有分压电阻、没有 B 值、没有任何一个参与运算的数。它默认谁,决定的是**派生文件**用哪套常数,而派生文件不影响判定(D-65)。

**这份档只留转换真正读到的键**(用户 2026-09-21:「非公式的可以直接删掉,建议激进一点……然后做紧凑一点,ini 里面注释可以写中文的」):删掉 `NTC COUNT`(可选,缺省 2)、`NTC ERR DETECT`(从不读)、`NTC CPU PATH`(从不读)、`NTC CPU TEMP COMP`(从不读)、裸键 `NTC TEMP COMP`(NTC1/NTC2 存在时是死键;厂商原件给的是 20,留着会让人误以为补了 20 °C)。**厂商原件逐字留在 `templates/temp_template.ini`**,所以"厂商写过什么"没有丢失,只是不再和"本档读什么"混在一张纸上。删减前后 `Profile.describe()` 与两个真值锚点**逐位相同**,是数值上的空操作。

**禁止**:在 `perf_monitor` 里放任何换算常数;让 `SHIPPED_PROFILE_NAME` 成为隐式回落;把 `templates/` 里的档做成可选项(它没有冷端 B,拿它换算这块板子会**静默**按旧规则);因为"用不到"就把厂商原件从 `templates/` 删掉。

### D-67 归档伴随文件的规则是「**stem 前缀 + 后缀白名单**」,新增一种产物类型必须**同时**放宽白名单(2026-09-21)
`server.py` 的 `_companion_files` **不是目录 glob**。它的判据是三条**同时**成立:同目录 + 文件名以 `<报告 stem>.` 开头 + 后缀在白名单里。原白名单 `(".csv", ".html")`。

**本次放宽为 `(".csv", ".html", ".png")`** —— D-65 那张温度曲线图叫 `<stem>.samples.temps.png`,**前缀合格、只有后缀被挡**。不放宽,图会永远留在 `reports/stress-test/perf/` 里,而 `archive/` 看起来一切正常 —— 这正是 `scripts/_pptp_report.py` 自己叫 **"a silent loss"** 的结局,而用户要的恰恰是"图在归档目录里"。

**为什么放宽是安全的**:当前全仓**没有任何脚本往报告目录写 `.png`**(唯一的 PNG 生产者是浏览器,它直接写进归档目录、绕过 `_companion_files`),所以放宽后**不会多收任何既有文件**。这条结论**只对今天的仓库成立** —— 将来若有脚本写同名前缀的 `.png`,它会被一并收进归档,而**那本来就是正确行为**。

**判据**:判断一个新产物能不能进归档,不要看"它在不在报告目录",要看**它的名字以谁开头、后缀在不在白名单里**。名字不合格 → 改文件名;后缀不在白名单 → 放宽白名单。**这是两件事**,只做一件就是安静丢失。详见 [PITFALLS.md](PITFALLS.md)。

**禁止**:假设归档是"搬走报告目录里的所有东西";加了 `.png` 就以为名字前缀那条规则也跟着松了;因为"这条路径下现在没有别的 png"就删掉元组旁那段注释(注释记的是**为什么这个元组是活的**)。
