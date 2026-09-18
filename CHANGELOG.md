# 更新日志 (Changelog)

## v2.11.2 — 2026-09-18 (perf_monitor HTML 报告:链路事件带上墙钟时刻)

用户原话:

> html 的报告里面的 rollback 和 reboot 等 event 目前只有运行节拍的标记而没有 timestamp,
> 我想要能看到问题发生时的时间如 18:39:12 等,这个才是重要的元素

**先澄清一个词**:报告里的类型叫 **`rollover`**(GPU/CPU 计数器回绕),不是 `rollback` ——
`grep rollback` 在仓库里只命中 `scrollback` 的假阳性。用户看到的正是那条 `24 counter rollover(s)`。

### 改了什么

报告第五节「链路事件」新增一列 **`发生时刻`**(本地时区 `HH:MM:SS`),**排在「相对时刻」前面**;
鼠标悬停显示完整的 `YYYY-MM-DD HH:MM:SS`。两列都留:节拍回答「在这趟跑的哪个位置」,
墙钟回答「几点几分出的问题」—— 后者才是能拿去和另一台设备的日志、一张工单、或某个人"下午那会儿"对上的东西。

### 关键事实:数据早就有,只是渲染层没输出

每条事件本来就同时带 `t_sec`(运行节拍)和 `clock_ms`(epoch 毫秒),`events.csv` 的列头一直是
`t_sec, clock_ms, type, reason, detail`,`report.json` 的 `events.items` 里也一直有 `clock_ms` ——
**HTML 渲染时只取了 `t_sec`**。所以本次**只动渲染**:payload、判定、CSV、限流逻辑一律没碰,
`docs/REPORT_FORMAT.md`(共享引擎 schema)同样不动 —— 这是 perf_monitor 自己的第 4 个区块。

**降级**:缺失或非法的 `clock_ms` 渲染成 `-` 而不是抛异常 —— 同一份 HTML 在收尾时也会从
partial snapshot 写一遍,那条路径上不能因为一个时间戳把报告整个弄没。

### 验证(2026-09-18)

| 门 | 结果 |
|---|---|
| `--selftest` | **0 failure**,其中 8 条是本次新增(格式化 / 完整日期 / 坏时钟不抛 / 行内墙钟 / tooltip / 节拍列保留 / 列头 / 无时钟降级) |
| **真实归档数据重渲染** | 取归档里 30 条事件的报告,用新渲染器重出 HTML,**逐行**与 `events.csv` 的 `clock_ms` 比对:30/30 行的 `HH:MM:SS` 与本地时间一致,tooltip 的完整日期一致,`相对时刻`/`类型` 两列逐行不变(回归) |
| 真机 20s 短跑 | `report :` 仍是**最后一行**(既有硬约束),`html =` 照常落盘,0 事件时走「没有记录到任何链路事件」空分支,文档完整 |

### 版本

`perf_monitor.py` 的 `SCRIPT_VERSION` 1.1.0 → **1.1.1**;平台 2.11.1 → **2.11.2**
(`server.py` 的 `version=` + `healthz`、`static/index.html` 的 `?v=` ×3)。平台侧**零逻辑改动**。

## v2.11.1 — 2026-09-17 (修「硬停」之后那台设备被永久卡死)

用户原话:

> 修一下，硬停的这个 bug

### 现象

对一台设备「硬停」(force-stop)之后,那台设备**再也起不了新任务**:`POST /api/run` 一直返回
`409 设备 X 上有正在运行的任务`,直到重启服务或 `POST /api/tasks/force-cleanup`。同一次验证里复现两次。

### 根因:谁先醒,谁就把状态写死

`api_force_stop_by_device` 把状态写成 `interrupting`,指望 `_stream_logs` 看到子进程退出后把它推进成
`interrupted`。但那一步在 **`await _stop_captures()` 之后** —— 一 `await` 就让出事件循环,而 `_stream_logs`
此刻正卡在 `proc.wait()` 上,子进程一死它立刻拿到退出码,于是:

1. `_stream_logs` 先跑完自己的终结块:`status = "failed"`(终态)、`ended_at`、停采集、归档、广播 `end`;
2. 控制权回到硬停这边,把**已经终态**的 `"failed"` **覆盖成 `"interrupting"`**;
3. `_stream_logs` 早已 `return`,**没有任何东西会再推进它** —— 任务永远停在 `interrupting`,
   而 409 守卫恰好拦 `running` / `interrupting`。

正常「中断」(`POST /api/stop/{task_id}`)**没有**这个问题:它**同步**写 `interrupting` 再发信号,不存在交叠。

### 改法:单一终结者 —— 终态必须在任何 `await` 之前落定

- 硬停在**第一个 `await` 之前**同步**认领**终结者身份:`t["_finalized"] = "force_stop"`,并直接写**终态**
  `"interrupted"` / `exit_code = -9` / `ended_at`。被杀的脚本本来就该记 `interrupted` 而不是 `failed` ——
  是操作员要它死的。
- `_stream_logs` 在 `proc.wait()` 之后看到 `t["_finalized"]` 就**直接 return**,不再做第二次终结。
  它原本要做的五件事(状态 / `ended_at`、停采集、归档、广播 `end`、广播归档行)硬停这边一件不少地自己做,
  所以没有丢失 —— v2.11.1 只补了一句 `_broadcast_archive_line()`,因为 `_stream_logs` 不再代劳。

### 验证(真机 `B0403374A2A508001F00`,17/17 通过)

| 场景 | 结果 |
|---|---|
| A 运行中硬停 | `interrupted` / `exit=-9` / 已归档(6 文件,21528 字节) |
| B 紧接着 `POST /api/run` | **不再 409** —— 旧 bug 正卡在这一步(设备解封) |
| B' 正常「中断」回归 | `interrupted`,归档齐全 |
| C 中断途中再硬停 | 仍是终态 |
| D 连续 3 次硬停 | 全部终态 `interrupted` / `exit=-9`,无残留非终态任务 |

服务端输出里每个硬停任务**只有一行** `[archive] … (force_stop)`、**没有** `(task_end)` ——
这就是 `_stream_logs` 确实走了那条 `return` 的证据(否则会多出一行)。

### 版本

`server.py` 2.11.0 → 2.11.1(`app = FastAPI(version=…)` + `healthz`)、`static/index.html` 的 `?v=` ×3。
**逻辑改动只有上面那两处**;perf_monitor 与其余 7 个脚本未动。

## v2.11.0 — 2026-09-17 (perf_monitor 长跑按键 keepalive:写个 .ini,让设备别弹「还在看吗」)

用户原话:

> 我后续压测 YouTube | Amazon | Netflix | MediaPlayer 可能存在长时间播放时,应用弹出无操作提示,
> 所以我需要在现有的 perf monitor 的基础上,可以通过调用 ir_runner 发送按键给设备
> 我建议是在 perf monitor 的 params 增加可以选红外 ini 的选项,然后直接调用一个后台持续发这个 ini 就行了
> 避免过度设计
> 只是需要在 perf_monitor 的时候跑 ini 就行了

### 做了什么

`perf_monitor` 第 4 个参数 `key_ini`(**下拉,默认空 = 不发送**)。选中后它在一条 **daemon 线程**里反复执行那个
`.ini` —— 按键节奏**就是** ini 里的 `delay_ms`(唯一真相,不另加参数,改节奏 = 改文件)。附一份模板
`ir_sequences/idle_keepalive.ini`:`KEYCODE_MEDIA_PLAY` 短按 @30 分钟。**ini 随便选、随便改。**

### 四条已拍板决策(DO NOT REVERT)

1. **线程内 `import ir_runner`**,不起子进程。
2. 先给一个 `KEYCODE_MEDIA_PLAY` 模板,之后要能自由选 / 自由改 ini。
3. 30 分钟一次(`delay_ms` 决定)。
4. **只有 perf_monitor 需要** —— 其余 7 个脚本、平台逻辑零改动。

### 为什么是线程,不是子进程(本轮核心取舍)

平台「中断」发 `CTRL_BREAK_EVENT`,子进程确实收得到;但**「硬停」走的是 `proc.kill()`(`TerminateProcess`),
不发任何控制台事件** —— 子进程会活下来**继续按按键**,直到下次重启服务器才被 `_reap_orphan_scripts` 扫掉。
daemon 线程随进程一起消失,这个失败模式根本不存在。**真机验证:硬停 `killed:1` / `exit=-9` 之后,
又等了 16 秒(3 个以上按键周期),没有任何进程还在发按键。**

### 为什么自己驱动 `run_step`,不用 `run_loop`

`run_loop` 是 `while True`,唯一出口是它自己那个线程里的 `KeyboardInterrupt` —— 而 Windows 的控制台事件
**只投给主线程**,那个异常永远不会到。改为以「一次按键」为单位自己循环,换来三件事:

- **可中断的等待**(`stop.wait(sec)` 而非 `time.sleep`)→ 停起来是瞬时的;
- **精确的按键数** —— 一次 `run_step` 调用 = 一次按键,`key_sent` 就是真按下去的次数。原先把整个 `count`
  交给 `run_step`,中断时那一串**要么全算要么全不算**(实测出现过「日志 40 行按键、计数 30」);
- **teardown 期间不再按键** —— `run_step` 自己的重复循环无法被中断,一个 `count=50` 的步会一路按到结束。

派发细节(Short/Long、KEYCODE vs sendevent)仍然全部归 `run_step`,这里只传一个 `count=1` 的副本。

### 报告如实记录 4 个字段(不参与判定)

`config` 加 `key_ini` / `key_sent` / `key_failed` / `key_status`;HTML 参数表显示选的是哪个 ini。
**keepalive 永远不能改变结论** —— 设备掉线、ini 写坏、按键名不认识,全都只进 `status`,判定块照常。

### 一个真实发现:`CTRL_BREAK` 会打断「在途的那次按键」

`CTRL_BREAK` 送给**整个进程组**,`adb.exe` 子进程同样收到 —— 于是停止瞬间正在飞的那次按键会以
`ADB failed: `(stderr 为空)的形式失败返回。第一版把它计成 `key_failed=1`,报告里看起来像 keepalive 出过错,
**而那其实是用户自己按的停止**。修法:按键报错先**不计数**,等它后面那个等待走完再定性 ——
真故障后面跟着正常间隔,停止会把那个等待打断。**所以 `key_failed` 的含义是「失败过并且还在继续重试」。**
(同一处还修掉了更脆的一点:一次瞬时失败原本会让整场长跑的 keepalive **彻底死掉**,现在只是跳过这一次。)

### 验证(真机 `B0403374A2A508001F00`)

| 门 | 结果 |
|---|---|
| `py_compile` / `--selftest` | 通过,0 failure(含新增 2 条 `key_ini` 断言) |
| `--dump-params` | 4 个 field;`key_ini` 是 select:`(不发送按键)` / `1.ini` / `idle_keepalive.ini` |
| 不传 `key_ini` 跑 20s | 0 行 `[key]` 输出;`[config] key_ini = (none)`;报告行仍在最后;rc=0 |
| 5 秒节奏 ini 跑 60s | **12 次按键 = 60/5 整除**,`key_sent=12`、`key_failed=0` |
| ini 路径故意写坏 | `status=failed: FileNotFoundError...`,**跑测照常完成、判定不受影响** |
| 平台跑模板 ini(相对路径) | 立刻 1 次按键,之后 30 分钟一次;`key_sent=1`、`key_failed=0`、`stopped` |
| 中断恰好落在按键中间(300ms 密节奏) | **日志按键行数 == `key_sent`**,`key_failed=0`(即上面那个误判已修) |
| 硬停(force-stop) | `killed:1` / `exit=-9` / 16 秒内**无残留进程继续按键**;归档 6 个文件完好 |
| 确定性用例 A/B/C/D | 停止打断的报错不计;真瞬时故障计数且 keepalive 存活;计数与按键一一对应;50 连发在 stop 后 0.5s 内停下且只算真实按下的 10 次 |
| HTML 参数表 | 显示所选 ini 名,不再是 `&mdash;` |

平台侧**只 bump 版本号**(`server.py` 的 `version=`、healthz、`static/index.html` 的 `?v=` ×3 → 2.11.0),
**零逻辑改动**:前端 `paramInputHtml` 本来就消费 `f.choices`,加一个 select 参数是零改动。

### 已知残余

见 [TODO.md](TODO.md) §8。另:`docs/REPORT_FORMAT.md` **不动** —— 共享引擎的 schema 没变,这只是一个新参数实例。

---

## v2.10.0 — 2026-09-16 (每个脚本的 Overall Result 都出一份中文 HTML 报告 + 报告引擎沉淀)

共享报告引擎 `scripts/_pptp_report.py`(588 行,下划线开头 → 平台不当作可跑脚本)接入 8 个脚本,沉淀文档 `docs/REPORT_FORMAT.md`;新增 `GET /api/tasks/{id}/report` + 任务卡「报告」按钮;`ir_runner` 不动(无 PARAMS / 无 PASS-FAIL 语义,给它编结论是造假)。
`bt_reboot` / `wifi_reboot` **首次产出 JSON 报告**(此前根本不写报告);`perf_monitor` 保留自己的渲染器,只吸收参数表 `render_params_table`(顺序遍历 `PARAMS` 声明,「来源」列看得见改没改)。
HTML 路径由引擎自推、不给调用方拼 stem 的机会(#45)、转义按数据类型而非长相(#46)、`html =` 用 `=` 而非 `:` 保 `report :` 仍是唯一可被 `_sniff_report_path` 认领的最后一行(#43)→ [PITFALLS.md](docs/PITFALLS.md);`wifi_onoff_stress` 中断时 JSON + HTML 都写,报告带 `interrupted` / `interrupted_note` → [DECISIONS.md](docs/DECISIONS.md)。

## v2.9.0 — 2026-09-14 (perf_monitor 参数收敛 + 判定块可读性 + 中文 HTML 报告 + gate 9 判据修正)

删 `track_foreground`(前台采集恒开,`fg_*` 是唯一能看见「app 挂了」的信号);`watch_pkg` 改 `select`(5 项,包名用 `cmd package query-activities -c ...LEANBACK_LAUNCHER` 从真机读 —— `pm list packages -3` 会把 YouTube TV / Netflix 这些系统应用滤掉);`interval_sec` 保留(基准拍周期 T,MED/SLOW 由它推导);新增中文 HTML 报告 `perf_<dev>_<ts>.html`(与 `report.json` 同 payload 同 stem、自包含无 JS),`_companion_files()` 认领后缀扩到 `.csv + .html`;新加的 `html =` 行改用 `=` 而非 `:` 以免被 sniff 误认 → [PITFALLS.md](docs/PITFALLS.md) #43。
⚠ 删开关时布尔谓词被压成 `name not in (...)`,**逻辑整个取反** → FOCUS/FGPKG/PIDSTAT 整轮静默失效,而 `--selftest` / 假设备 harness / 真机 9 门全 pass;谓词提到模块级 `section_due()` + harness 新增「通道存活」断言 → [PITFALLS.md](docs/PITFALLS.md) #42。
gate 9(APP)两个方向都判反:引入 `watch_seen`(没出现过不算丢失)与 `watch_pid_dead`(前台仍是它但 `pidof` 空 = 就地崩溃),「从没出现」改判 inconclusive → #44;控制台 `GATES` 行补印失败门的 detail。另:`scripts/perf_monitor.py` 起被 E-SafeNet 包密文壳,Read/Grep 静默失效,此后一律用 Python + 断言改。

## v2.8.0 — 2026-09-14 (perf_monitor v2:分级采样 + 证据累积 + 收尾判定)

分级采样 FAST/MED/SLOW(嵌套周期,时间基准改整数毫秒);一拍一次 adb(8 个 section 用 nonce 帧 `@@NAME:<6hex>` 拼进同一条复合命令),T=2s 实测占空比 9.06%;9 道门(DATA/COVERAGE/CADENCE/TIMEOUT/ENVELOPE/REBOOT/MEMORY/CPU/APP)+ INCONCLUSIVE 地板,合并序 `INCONCLUSIVE > FAIL > WARN > OK`;`judge(evidence)` 是纯函数;设计全文 `docs/PERF_MONITOR_V2.md`。
真机抓到的坑:`_fg_delta()` 基线推进条件写反 → `fg_cpu` 整场 null(#38);`st` 列结果词必须 `padStart(7)` 右对齐,否则一次掉线把后面所有列推歪 6 格(#39);覆盖率分母用拍序号 `max(1, last_k + 1)` 而非实测耗时(#40);坏拍把所有非 `d` 指标标 `x` 而非保留上一拍值(#41)→ [PITFALLS.md](docs/PITFALLS.md)。

## v2.7.6 — 2026-09-14 (perf 实时行改定宽列,可竖读)

纯前端改动(`static/app.js` 的 `handlePerfLine` sample 分支):每单元格定宽(`pct` 6 / `secs` 8 / `MHz` 7),`null` 渲染成同宽 `-`(一排短横,不会被误读成「GPU 真的是 0.0%」,见 #14),`pkg=` 挪到行尾,去掉重复墙钟;列恒在(即使某序列被禁用)。
验证靠 node 层渲染 harness,不用 `node --check`(语法检查看不见列位置错位)→ [PITFALLS.md](docs/PITFALLS.md) #39;已知边界:`t` 超 27.7 小时会溢出一格,24h 包络内不处理。

## v2.7.5 — 2026-09-14 (logcat 人工开关)

perf_monitor 跑 8 小时级监控,而 logcat 恒开一次 run ≈ 1GB,还会在设备已打满时再压一路 `adb logcat` —— 监控任务等于自己干扰自己。
`RunRequest` 新增 `logcat_capture: bool = True`(默认 True → 老前端不发该字段行为完全一致),设备卡 `device-row5` 勾选、**不持久化**(残留的 off 会静默丢掉崩溃现场)→ [PITFALLS.md](docs/PITFALLS.md) #37;关掉时不建 `_cap_logcat_task`、logcat 日志文件根本不创建,既有守卫天然跳过。

## v2.7.4 — 2026-09-11 (脚本在同事机器上无法结束:stdout 管道两端编码不一致)

根因:`server_window.ps1` 设 `PYTHONIOENCODING=utf-8`(子进程写 UTF-8),而 `Popen(..., text=True)` 没写 `encoding=` → 中文 Windows 回退 **GBK**,子进程的 UTF-8 字节流被按 GBK 解码,`proc.stdout.readline()` 抛 `UnicodeDecodeError`(实测 `top -n 1 -b` / `dumpsys window` 那几段最易带出非法字节对)。
症状是「无法结束」而非报错:异常逃到 `_stream_logs` 底部的兜底 `except`,只广播一条错误就 `return`,**把下面的收尾块整块跳过** → 任务永远停在 `running`、`exit_code=None`、不归档,日志孤零零留在 `logs/`。
修法:管道两端钉死 `encoding="utf-8", errors="replace"` + `child_env["PYTHONIOENCODING"]="utf-8"`;读取循环自带 `try`、收尾无条件执行;`ir_runner.py` 3 处 `subprocess.run(text=True)` 与 `_adb_reconnect_offline` / `/api/adb/reconnect` 一并修 → [PITFALLS.md](docs/PITFALLS.md) #35。A/B 实证:修复前 `status=running` 永不结束、`exit_code=None`,修复后 `finished` / `exit=0` / 归档逐字节完整。

## v2.7.3 — 2026-09-11 (串口勾选高亮 + 任务面板排版 + reports/ 不再堆积)

串口勾选开启时整行高亮(`serial-on` 只在「真的会采集」时才加,关闭时 padding/border 常驻以免卡片高度跳动);任务面板删掉死节点 `#task-hint`(JS 0 引用,白占 ~70px),`.panel-header` 加 `flex-wrap: wrap` + `.panel-actions` 用 `margin-left: auto`。
**报告改为「搬」**:归档成功后删掉 `reports/` 里的原件(先 copy 再 unlink),`reports/` 变成几秒钟的暂存区 —— 修正 v2.7.0 / v2.7.1 里「只**复制**一份进存档」的旧说法;`app_launch_stress` 的多份报告由 `_find_task_reports` 整组归档为 `report.json` / `report-2.json`。
`logs/`(运行期工作区 + 服务自身日志)与 `reports/`(**脚本契约**,脚本独立 CLI 跑也写这里)两个目录本身都不能删,用户视角只需要看 `archive/` → [DECISIONS.md](docs/DECISIONS.md)。

## v2.7.2 — 2026-09-11 (掉线容忍:串口自动重连 + 周期 adb reconnect + 日志链路自愈)

`_capture_serial` 是一次性的,端口没开成读线程一死就永久 `failed`;改成受监督重试循环:连续失败预算 `SERIAL_MAX_RESTARTS = 60`、间隔 2s、**收到任何真实数据就清零**(终身计数在长跑里必被打穿,见 #27);每次重试起新线程 + 新队列并把 `cap["_stop_evt"]` 重指(#30);写可见 marker `--- serial capture reconnecting #N (原因) ---`,断过多久在存档里看得见;状态新增非终态 `"reconnecting"`。
新增后台 `_device_watchdog`:每 5s 跑 `adb devices -l`,对「有 running/interrupting 任务但不在列表或状态非 device」的 serial 每 20s 发 `adb reconnect offline` —— 绝不 `kill-server`(不能影响其它脚本)→ #31。合成「临时离线」卡的条件写死 `status === "running"` 漏了 `interrupting`,已补;`_broadcast` 发送超时后真正 `ws.close()`(close 自身也要超时 → #34);`_announce_captures()` 修掉 `captures` 快照自 v2.7.0 起冻结的问题(#32)。

## v2.7.1 — 2026-09-11 (存档按模块分类 + 存档入口提到全局 + 报告误归属修复)

存档按模块分类 `archive/<模块>/`(`ARCHIVE_MODULES` 是显式映射表,不靠文件名猜,未登记落 `other/`;旧版本已归档的任务回退扁平路径);「打开存档」按钮 + 占用徽标从任务面板移到顶栏;「清空已完成」改「清空已完成的卡片」。
修复报告误归属(上一轮 `ir_runner` 根本不写报告,存档里却出现了 `report.json`):只有 `REPORT_WRITING_SCRIPTS` 里的脚本才走 mtime 扫描,mtime 门槛从「开始-5s」收紧到 **≥ 任务开始时间**,`_CLAIMED_REPORTS` 保证一份报告只被一个任务认领。
清理 `logs/` 48 个任务日志(1.6MB)与 `reports/stress-test/` 59 个 JSON(508KB),目录本身保留。此处原写「平台运行时只**复制**一份进存档」,已被 v2.7.3 的「搬」(复制后删原件)取代 → [DECISIONS.md](docs/DECISIONS.md)。

## v2.7.0 — 2026-09-11 (任务结束自动存档 + 日志前端方案收敛)

任务一结束(任何结束方式)自动归档到 `archive/<时间>_<脚本>_<设备>/`(目录名不含 task_id):`stdout.log` / `logcat.log` / `serial.log` + `report.json` + `chart.png` + `summary.json`(`report_source` / `notes` / 每文件 bytes+lines),**永久保留不做自动删除**;`chart.png` 由浏览器 `exportFullChartDataUrl`(2560×720)渲染后 POST 回传(服务端无绘图库),浏览器没开就没有这张图;报告 path 经 `_sniff_report_path` 廉价嗅探、兜底 mtime 扫描,两条路径都强制校验落在 `REPORTS_DIR` 内。
**减法**:删掉 logcat / 串口的前端显示方案(`CHANNELS` / `TABS_ENABLED` / `state.activeSource` / `state.captureCounts` / 计数 pills / `#channel-bar` / `capture_counts` 心跳),前端**永久只显示 stdout**;保留整个采集引擎、`WS_SUBS` 源订阅过滤(承重结构)与 `ws_logs` 的 `?sources=`;唯一新增一行 `[archive] … | stdout 22L | logcat 8L | serial 0L | report.json` 归档摘要 → [DECISIONS.md](docs/DECISIONS.md)。
删任务不动存档(`_delete_task_logs` → `_forget_task`);`#btn-export-log` 加 `hidden`(代码保留)。当时 `bt_reboot` / `wifi_reboot` / `ir_runner` 都不写报告 —— **前两个在 v2.10.0 已补出 JSON 报告**,`ir_runner` 至今不写。
采集引擎四处真机隐患:logcat 重启预算改「连续」计(#27);收尾不再覆盖通道自身给出的终止诊断(#28);串口读线程开窗后死掉可检测;重启改 `-T 2000` 回填开机窗口(#29)。`LOGCAT_MAX_BYTES` 256MB → 1GB(实测一次重启 ≈ 1.3MB logcat)。删 2s 计数心跳时连它唯一在做的 `t["captures"] = _public_captures(t)` 一起删了(→ #32);串口停止时读线程最后半行必丢,刻意不修(→ #33)。另两条属于脚本、已报用户未改:`wifi_reboot_stress` 从不校验 `adb reboot` 是否真的重启(失败 reboot 被记成 PASS = 假绿),第一轮完成前中断会打印 `passed: 0 / 0`。

## v2.6.0 — 2026-09-11 (每任务三源实时日志:stdout + logcat + 串口)

服务端采集引擎(9 个脚本零改动):logcat 恒开 `adb -s <serial> logcat -v threadtime -T 1 -b main -b system -b crash '*:I'` + 自动重启循环 marker,`PPTP_LOGCAT_MAX_MB` 默认 256MB,**永不 `logcat -c`**(#25);串口按任务勾选(设备卡 checkbox + COM 下拉,裸守护线程 + `asyncio.Queue` 满则丢最旧、不用 `readline()`、不动 DTR/RTS,#26);「脚本优先」仲裁 —— 声明 `serial_port` 的 `battery_inout_stress` 拥有端口,平台标 `skipped` / `script_owns_port`;一切失败只降级不抛出(`_stop_captures` 对「从未启动的通道」的守卫 → #24)。
WS 按源订阅 `WS_SUBS`(`/ws/logs/{task_id}?sources=`),没订阅者的源零成本;`_broadcast` 新增 5s 发送超时。日志名 `logs/<task_id>_<yyyyMMdd-HHmmss>_<script-stem>_<device>.<suffix>`(task_id 刻意保留在最前,清理 glob 与 `^[0-9a-f]{32}$` 路径防护无需改);`GET /api/tasks/{id}/log?source=` 走尾部读 `_read_tail`(默认 4MB),非法 source → 400。
⚠ 本节原「重点 5」的前端通道路由 / 计数 pills / `#channel-bar` 接缝**已在 v2.7.0 整体删除**,前端只显示 stdout —— 当前实现以 v2.7.0 为准(采集侧不变,显示侧已移除)。

## v2.5.2 — 2026-08-26 (新增重启蓝牙回连压测脚本 bt_reboot_stress.py)

新增 `scripts/bt_reboot_stress.py`(仿 wifi_reboot_stress):每轮 `adb reboot` → 等设备上线 → 轮询 A2DP 音箱回连(每 5s 一次,最多 `bt_reconnect_timeout` 秒,默认 90s)→ PASS/FAIL;参数还有 `iterations` / `wait_sec` / `back_online_timeout`。
回连判定 `dumpsys bluetooth_manager`:蓝牙适配器已开启(`enabled: true`)**且**至少一个 A2DP 状态机 `mConnectionState: CONNECTED`(即音箱真正回连,而非仅「蓝牙开着」,真机实测正常回连 ~6s)。实测「刚连上就重启」→ CONNECT_TIMEOUT 判 FAIL。本设备蓝牙开关未对用户开放 → 只测重启回连。

## v2.5.1 — 2026-08-25 (前端 log console 全英文 + 折线图固定 1h 滚动窗口 + 放电关机卡片 + 全程图导出)

`#log-console` 只准英文/ASCII:删 `STATUS_ZH` / `JUMP_TYPE_ZH` 中文映射表,时间戳统一走 `fmtClock`(`toLocaleTimeString("zh-CN",{hour12:false})`)强制 24h,meta 行 / 重连重放截断提示 / 占位符全转英文(UI 文案仍中文)→ [PITFALLS.md](docs/PITFALLS.md) #20。
折线图 x 轴改定宽 1h 滚动窗口(`interval: 10*60*1000` + `splitNumber: 6`,右缘 = 最新样本墙钟),**移除 dataZoom**(与固定窗口互斥)→ #21。
电池测试结束设备关机时合成「放电关机」专用卡(虚线红边,条件:终态 battery 任务 + 设备不在 `adb devices` + `latestTaskOf()`)不再消失;导出 chart.png 改为**任务全程** + 0.5h 一个刻度(`buildPerfOption(taskId, true)` fullRange 分支)。本次只动前端,脚本零改动。

## v2.5.0 — 2026-08-25 (电池充放电压测 + 前端图表类型泛化)

新增 `battery_inout_stress.py`:电量/温度/电压/状态来自 `dumpsys battery`,但本机 `ro.config.batteryless=true`、adb 又受 SELinux 限制(`Failed transaction 2147483646`),**只能走串口 console root shell** 发 `set polling true` → [PITFALLS.md](docs/PITFALLS.md) #18;参数 `mode`(charge / discharge **分开跑**)/ `serial_port` / `interval_sec`(默认 10s)/ `temp_warn_c`(45°C)/ `full_hold_sec`(60s)。
保留电量**跳变检测**(方向感知)与温控告警;单次中止信号:电量到 100% 稳定(`stop_reason="full"`)/ 连续 2 次读失败(设备关机,`power_off`)/ 手动;PERF meta + sample 都带 `type` + `clock`;不输出 Excel。
前端新增 `perfKind(taskId)` → `"perf" | "battery" | null`,电池曲线(电量蓝左轴 0-100% + 温度红右轴),CSV 分支表头 `t_sec,t_wall,level_percent,…` 后缀 `.batt.csv`(与 perf_monitor 共用 buildPerfOption / flushPerfChart,天然同步);新 PERF 脚本必须注册 `PERF_SCRIPT_KINDS` → [PITFALLS.md](docs/PITFALLS.md) #19。

## v2.4.2 — 2026-08-25 (性能图表长时检测体验)

导出 chart.png 固定 **1280×360 归一化**(2x 后 2560×720),不再「很长」;x 轴加 `splitNumber: 6` + `hideOverlap`,刻度数稳定在 ~10 个(10min 跨度间隔 54s、2h 跨度 800s);采样上限 3600 → 86400(支持 24h@2s 全量)。
实机排查结论:视频播放时 GPU%≈0 **是硬件正常**不是读数失效(本机 MStar 显示管线 / MT5896:视频解码走 VPU、画面合成走 HWC,GPU 只画 UI)→ [PITFALLS.md](docs/PITFALLS.md) #14。

## v2.4.1 — 2026-08-25 (修复性能图表"不出线")

根因:`perf_monitor.py` 打印 sample 行时 `emit` dict **漏了 `"type": "sample"`**(docstring 写了、代码没实现)→ 前端 `handlePerfLine` 按 `obj.type === "sample"` 分发,数据全被当未知类型丢弃 → 图表空白 + 导出只剩 txt,**一个字段缺失命中三条症状** → [PITFALLS.md](docs/PITFALLS.md) #17。
修法:`emit` 补 `"type": "sample"` + `"clock"`(wall-clock epoch ms);横轴改真实墙钟 `type:"time"`(旧日志回退 `started_at + t*1000`);前台 APP 曲线加独立右轴 0-400%(实测 116-123%,单轴会顶格裁切);CSV 加 `t_wall` 列。

## v2.4.0 — 2026-08-25

新增 `scripts/perf_monitor.py`(实时 CPU / GPU / 内存 + 前台 APP 的 CPU%):CPU `/proc/stat`、mem `/proc/meminfo`、GPU `/sys/kernel/debug/mali0/dvfs_utilization`(需 root,`su 0`)、前台 APP 走 `top -n 1 -b | grep <pkg>`;参数 `interval_sec`(2.0)/ `duration_sec`(0=手动停)/ `track_foreground`;每采样一次复合 `adb shell`,打一行 `PERF|{...}`(meta + sample),服务端零改动 → [PITFALLS.md](docs/PITFALLS.md) #7 / #8 / #9 / #10 / #13。
前端:本地 vendor `static/vendor/echarts.min.js`(ECharts 5.5.1,pin 版本 → #16),`#log-info` 与日志终端之间新增 200px `#perf-chart`,数据按任务 `perfSamples[taskId]` 独立缓冲;一键导出 log.txt + perf.csv + chart.png。

## v2.3.0 — 2026-08-24

`sensor_reboot_stress.py` 从 unittest 重写为平台契约;`PARAMS` 新增 `type: "select"` + `choices`(字符串列表或 `{value,label}`),前端自动渲染 `<select>`;参数 `sensor`(gsensor / tof)/ `iterations` / `reboot_timeout`,`PASS_THRESHOLD` 98% 等为硬编码常量;su 适配链含裸 `su 0 <cmd>`(本设备 `su -c` 报 `invalid uid/gid -c`)→ [PITFALLS.md](docs/PITFALLS.md) #11。
新增 `app_launch_stress.py`:`mode` cold / hot 分开跑,主指标 `am start -W` 的 `TotalTime` + logcat `Displayed` 交叉验证;`APP_PRESETS` 硬编码 Netflix / Prime Video / YouTube TV(三个一起跑),**每个 APP 各一份**报告 `reports/stress-test/app-launch/`。
真机冒烟修 3 处:Activity 必须**全限定** `package/full.Class`(Prime Video / YouTube 的入口不在包命名空间);`Displayed` 的 `+592ms` 被 regex 误读成 592 秒;`LaunchState` 与所选模式矛盾从「打 note」升级为**该轮判 NG 不计样本**(`TotalTime: 0` 同样判 NG)。

## v2.2.0 — 2026-08-24

脚本用模块级 `PARAMS` 自描述(`name` / `label` / `type`(`int`/`float`/`bool`/`str`)/ `default` / `min` / `max`)+ `--dump-params`;后端新增 `GET /api/scripts/{name}/params`(跑 `--dump-params` 读,按 `(name, mtime)` 缓存);前端配置值按**设备 × 脚本**存 `state.deviceParams`,跨刷新保留。
新增 `wifi_onoff_stress.py` 与 `wifi_switch_stress.py`(`WIFI_NETWORKS` 硬编码在脚本内),`wifi_reboot_stress.py` 补参数;wpa_cli / `cmd wifi connect-network` 需 root,统一裸 `su 0 <cmd>`(本设备 `su -c` 报 `invalid uid/gid`),`use_su` 可关 → [PITFALLS.md](docs/PITFALLS.md) #11;真机:wifi_onoff 1 轮全 PASS,wifi_switch 3/4 PASS(第 4 个 SSID 拼写 `-` / `_` 差异已修正)。

## v2.1.0 — 2026-08-17

新增上层注入路径:`KEYCODE_*` 走 `adb shell input keyevent`,**user 版固件无需 userdebug**;厂商遥控按键映射表 23 个 + 安卓原生标准键 26 个,按名字前缀自动分发(`KEY_*` → sendevent 需 userdebug/root,`KEYCODE_*` → input keyevent,未映射的原样透传)。
已知限制:`KEYCODE_*` **长按暂无效**(设备 `input` 命令无 `keydown`/`keyup`,ini 写 `LongXXXX` 不报错但命令静默失败),前端 IR 弹窗已加提示;按键对照表 `ir_sequences/KEY_REFERENCE.md` 扩为三张表。

## 此前版本 — pre-input-keycode @ 96b5430

新增 KEY_MEMO 按键(码值 396 / 0x18c);修复 `ir_runner` 循环间延时未按 ini 的 `delay_ms` 执行(此前写死 1.0s)。
⚠ 此版本**尚未导入 `KEYCODE_*`**:仅支持 `KEY_*` sendevent 注入,**user 版固件无法使用**(需 userdebug/root);KEYCODE 导入前的主线 checkpoint 基线。
