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

用户 2026-09-16 的四点要求:

> 1. 每个脚本 stdout 的 Overall Result 都需要生成 html 的报告
> 2. 参考 PerfMonitor 的生成逻辑
> 3. 当前已连接 adb 设备
> 4. 需要将 HTML 格式的 Overall Result 的架构和 Params 作为架构写入 md 文档沉淀

v2.9.0 时中文 HTML 报告是 `perf_monitor` **独占**的。这一版把它抽成共享引擎,8 个脚本接进去。

### 架构:一份数据,两个渲染器

```
脚本已有的局部变量 ──┬──► 现有 print(...)          ★ 一字不改
                     └──► rows=[row(...), ...]     ★ 值表达式直接抄旁边那一句
                                └─► build_payload(...) ─► write_html_report ─► <报告同stem>.html
```

**四个关键设计点**:

1. **stdout 不由引擎渲染。** 用户明确选择"只加 HTML,stdout 一个字不改"——三种汇总风格原样并存,
   只在末尾多一行 `html = <绝对路径>`。控制台是用户实时在看的东西,不为统一风格去动它。
   代价:`rows` 要脚本手写(3~8 行),不能从 print 语句反推 —— 换来的是两处显示的数不可能各算一遍。
2. **不碰现有 JSON 报告。** 全库无消费者解析报告 JSON,保持原样 = 零回归。
   HTML 里有「完整数据」折叠区把 JSON 整体递归渲染进去,保证 `HTML ⊇ JSON`。
3. **路径推导封在引擎里。** `write_html_report(json_path, doc)` 自己由 JSON 路径推 HTML 路径,
   **不给调用方拼串的机会** —— stem 逐字相同是归档能收到它的唯一条件。
4. **引擎吞掉自己的异常**,失败时打 `[warn]` 并返回 `""`(空串,不是 `None`),调用方写 `if html_path:`。

### 交付物

| 文件 | 改动 |
|---|---|
| **新增** `scripts/_pptp_report.py` | 共享报告引擎(588 行)。下划线开头 → 平台不把它当可跑脚本 |
| **新增** `docs/REPORT_FORMAT.md` | **用户第 4 点**:报告格式与参数架构的沉淀文档(13 节) |
| `wifi_switch_stress.py` | 接入(参考实现) |
| `wifi_onoff_stress.py` / `sensor_reboot_stress.py` / `app_launch_stress.py` / `battery_inout_stress.py` | 接入(已有 JSON) |
| `bt_reboot_stress.py` / `wifi_reboot_stress.py` | 接入 **+ 从零补出 JSON 报告**(它们此前根本不写报告) |
| `perf_monitor.py` | **保留自己的渲染器**(见下),补上从来没有的**参数表** |
| `server.py` | `REPORT_WRITING_SCRIPTS` 加两个 + 过时注释修正 + `GET /api/tasks/{id}/report` 路由 |
| `static/app.js` / `static/index.html` | 任务卡新增「报告」按钮 + `?v=` ×3 |
| `ir_runner.py` | **不动** —— 没有 PARAMS、没有 PASS/FAIL 语义,给它编一个结论是造假 |

### Params 架构(用户点名要沉淀的那块)

perf 此前把 3 个字段硬编码进一句副标题,`label` 和 `choices` 完全没用上。
新引擎补的 `render_params_table(values, schema)` 从 `PARAMS` 声明生成三列表(参数 / 本次取值 / **来源**),
**顺序遍历 schema 而非值的字典** —— 所以每个声明的参数都有行,漏传的会回退到声明默认值并注明「本次未传」。

「来源」列三态:「本次未传,取声明默认值」/「传入了与默认相同的值」/「已改 + 默认 X」。
**"用户改没改过这个参数"在报告里直接看得见**,不用翻 `summary.json`。

`choices` 在本仓库有**两种形状**(纯字符串列表 / `{value,label}` dict 列表),两种都得吃 —— `_choice_label` 都处理,
匹配不上时**原样显示该值而不是丢掉**(丢掉会让人以为参数没生效)。bool 渲染 `是`/`否`,**绝不打 `True`/`False`**。

### 归档:零改动(已核实)

`_companion_files` 收编一个文件的全部条件是「同目录 + 文件名以 `<报告stem>.` 开头 + 后缀 ∈ `(.csv, .html)`」。
所以只要 HTML 与 JSON **同 stem 同目录**,归档自动带走它 —— 服务端一行不用改。
归档时主报告被改名成 `report.json`,**伴随文件保留原文件名**。

### ⚠ 硬约束:`report :` 行必须仍是最后一行

`_sniff_report_path` 认「含 `" : "` + 含 `report` + 最后一个 `" : "` 右边以 `.json` 结尾」的**第一行**。
所以:① `html =` 行用 `=` 而不是 `:`(结构性避免被误认,不依赖后缀巧合);② `report :` 保持最后一行。
`perf_monitor --selftest` 里有两条断言守着(`exactly one sniffable report path, on the last line` /
`html line is not sniffable` for all 5 verdict kinds)。

### 中断时报告与 HTML 都写(`wifi_onoff_stress.py`,用户 2026-09-16 裁决)

`wifi_onoff_stress.py` 的报告写入原本被 `if not interrupted:` 包着(脚本自身既有设计,注释写着
"report is only saved on full completion"),而 `=== results ===` + `OVERALL` 在守卫**外**、中断时照打 ——
于是中断时 **stdout 有一个 Overall Result,却没有任何报告**,与"每个 Overall Result 都出一份 HTML"直接冲突。

**用户裁决(2026-09-16)**:"中断时报告与 HTML 都写,报告简单注明一下吧" → 守卫**整条删掉**,两类产物每次都写。
"只补 HTML"不可能:报告与 HTML 必须**同 stem 同目录**才能被归档收走(§7),孤儿 HTML 会被 `_companion_files`
静默丢弃 —— 所以要么两个都写,要么都不写。

**中断怎么被注明**:判定**一个字不改**(控制台与页面因此不可能互相打架),中断这件事走两条路进报告 ——
JSON 里新增 `interrupted: bool` + `interrupted_note: str`(两个键**恒在**,形状不随结束方式变),
同一句话进 `warn_zh` → HTML 里是横幅正下方那条黄条,打开页面第一眼就能看见。
措辞分两种:一轮都没跑起来 →「本次运行在开始任何一轮之前被中断,下面的比率与结论**不成立**」;
跑了若干轮 →「已开始 N/M 轮(其中最后开始的 1 轮未跑完),下面的比率与结论**只覆盖这些已开始的轮次**」——
因为 `actual` 数进了那轮在飞的迭代,而它没贡献任何一次检查计数,不说清楚的话半场会被读成一场差的。

**真机实测**(平台拉起 → 跑到第 2 轮时点「中断」,`B0403374A2A508001F00`):status `interrupted`、exit 1;
归档 `archive/wifi/<ts>_wifi_onoff_stress_<dev>/` 里 `report.json`(665 B)与 `<stem>.html`(7651 B)**都在**;
`report.json` 含 `"interrupted": true` 与那句 note;stdout 契约原样(`html =` 在 `report :` 之前、`report :` 仍是最后一行);
HTML 横幅仍是 `FAIL/异常`(**刻意** —— 判定不动,所以控制台与页面不可能不一致),黄条里是 note + 未达标项;
页面自包含;`GET /api/tasks/{id}/report` 200 `text/html`,7651 字节里含那句 note。

### 验证(2026-09-16,真机 `B0403374A2A508001F00`,平台端到端)

**静态**:`py_compile` server.py + 10 个脚本全过;`node --check static/app.js` 过;版本号 grep 全库一致(2.10.0)。

**单脚本(真机实跑)**:`wifi_switch_stress`(4 网络 3/4 通过 → FAIL 路径)、`app_launch_stress`(3 APP →
3 组 json+html 同 stem 成对、零孤儿)、`perf_monitor`(参数表把 `watch_pkg` 的 dict 形状 choices 解析成中文标签)、
`bt_reboot`/`wifi_reboot`(零迭代路径 → 诚实显示"不做判定",不编造 PASS/FAIL)。
`perf_monitor.py --selftest` **0 failures**。**口令泄漏验证**:三个 Wi-Fi 口令在 HTML 里全不在,SSID 仍在,`已隐去` 标记正常。

**归档门(平台拉起,端到端)**:
- `wifi_switch_stress` 短任务 → `archive/wifi/<ts>_.../` 里 `report.json` + `<stem>.html` **都在**;
  `summary.json` 的 `artifacts` 列出 html 且 `report_source` = `stdout_line`(证明是嗅探命中,不是 mtime 兜底)。
- `wifi_reboot_stress` **首次产出报告并归档成功**(此前 `summary.json` 写的是"本脚本不写报告")。
- `GET /api/tasks/{id}/report` → **200 `text/html`**,字节数与归档文件一致。
- **中断门**:`wifi_reboot_stress` 跑到第 2 轮时平台「中断」→ `[interrupted] Ctrl+C received` 后仍打印完整汇总,
  **报告 + html 照常写出并归档**,`report :` 仍是最后一行且可嗅探。

**未证**:前端「报告」按钮在真实浏览器里的点击效果(本机无真实浏览器,与前几个版本同一残余);
`wifi_onoff` / `sensor_reboot` / `battery_inout` 三个脚本只过了静态门与 `--dump-params`,**没在真机上跑过**(收尾时 `wifi_onoff` 补跑了一次**中断**路径的端到端,跑满路径仍未验证)。

### 已知残余(已记进 TODO.md)

- **~~`wifi_onoff_stress.py` 中断时不写报告~~ → 已解决(2026-09-16 用户裁决)**:守卫已删,中断时 JSON 与 HTML
  都写,报告里带 `interrupted` / `interrupted_note`,HTML 的黄条里能看到那句注。见上文「中断时报告与 HTML 都写」。
- 引擎的 `attach()` / `document_of()` 是**死 API**:定义了、有 docstring,但全库无调用方。保留作预留,docstring 已改成如实说明。
- `app_launch_stress` 一次运行多份报告 → 前端「报告」按钮**只开第一份**(文件名排序),其余从「存档」取。
- 明文 Wi-Fi 口令仍在归档的 `report.json` 里(`hide_keys` 只管 HTML 渲染)。
- 引擎 588 行,计划里估的 320 —— 多出来的是 params 表(两种 `choices` 形状)、`_detail_html` 递归隐去、区块构造器。

### 有意偏离计划的地方(如实记录)

- **`perf_monitor` 保留自己的渲染器**,没有完全交给共享引擎:它那 4 个区块(gates / 通道分布 / 每门明细 / 链路事件)
  比通用引擎能表达的要丰富,重写它们有风险(要碰 `--selftest`)而无用户可见收益。perf **只吸收了参数表**——那才是它真正缺的。
- `write_html_report` 失败返回 `""` 而非计划里写的 `None`。
- 引擎里的常量叫 `SCHEMA`,不是计划里写的 `REPORT_SCHEMA`。

## v2.9.0 — 2026-09-14 (perf_monitor 参数收敛 + 判定块可读性 + 中文 HTML 报告 + gate 9 判据修正)

用户跑完第一份真机短样本后的四项确认。**其中第 2 项是我自己在这次改动里引入的严重 bug** ——
它让前台应用通道**整轮静默失效**而所有门仍报 pass,先说这个。

### ⚠ 2. 修掉 `due_sec` 被改反(通道级静默失效)

删 `track_foreground` 开关时,`due_sec()` 原本的

    name not in ("FOCUS", "FGPKG", "PIDSTAT") or self.track_fg

被压成了 `name not in (...)` —— **逻辑取反**。后果:`FOCUS`/`FGPKG`/`PIDSTAT` 三个 section
永远不"到期",**前台应用通道整轮没有任何数据**(`fg_pkg`/`fg_pid`/`fg_cpu` 全 null,
状态字符恒为 `n`),而:

- `--selftest` 60+ 条断言**全绿**;
- 假设备 harness **全绿**(它照常合成 FOCUS section,协议层面看不出问题);
- 真机 60s 实跑 **`RESULT: OK`、9 门全 pass、覆盖率 100%**。

唯一看得出来的地方是**对着 samples.csv 逐拍看 `st`**:SLOW 拍的状态字符是 `n`(从未到期)
而不是 `b`(基线)或 `f`(新值)。

修法与加固:

- 谓词提到模块级 `section_due()`,补 **6 条 selftest 断言**(命令侧确实要求了 section、谓词侧接受、
  无 SLOW 时拒绝、section 缺失时拒绝、GPU 受 `gpu_enabled` 约束)。
- **假设备 harness 新增「通道存活」断言**:凡是存在「有 SLOW 拍**成功返回过**」的运行,
  `fg_pkg`/`fg_pid` 就必须至少产出一个值。**已用一份重新注入该 bug 的副本验证过这条断言确实会失败** ——
  不会失败的回归测试等于没有。
- **为什么原来拦不住**:所有既有断言都是**形状**断言(CSV 是矩形、报告可往返、门是 9 道),
  没有一条问过「这个通道到底出过值没有」。一个死掉的通道和一次太短的运行,在形状上一模一样。

### 1. 参数面收敛(用户 2026-09-14)

- **删掉 `track_foreground` 开关**,前台采集**恒定开启**。理由:「被压测的 app 挂了 / 掉到后台」
  是长跑里最常见的故障,而 `fg_*` 是唯一能看见它的信号;关掉只省一次 `dumpsys window` + `pidof` +
  `/proc/<pid>/stat`,不值一个开关。连带删掉 `Evidence.track_fg`、CSV 前导的 `track_foreground=`、
  报告 `config` 的同名字段。
- **`watch_pkg` 由自由文本改为下拉**(`type: "select"`)。自由文本里一个拼错的包名会**静默废掉**
  唯一依赖它的 gate 9(APP)。**清单已由用户 2026-09-14 定稿**:YouTube TV / Netflix / Prime Video /
  本地媒体播放器 + 「不关注」,共 5 项(此前那个「爱奇艺(投影版)」是我猜的占位,**这台设备上根本没装,
  已删**)。包名是用 `cmd package query-activities -c ...LEANBACK_LAUNCHER` 从真机读的 ——
  **注意 `pm list packages -3` 不够用:YouTube TV 和 Netflix 在这台机器上是系统应用,会被 `-3` 滤掉。**
- `interval_sec` / `duration_sec` 保留。**`interval_sec` 不是分级采样的替代品**:它是基准拍周期 T,
  MED/SLOW 的周期由 T **推导**(固定 5 s / 30 s 目标),分级采样不等于「不需要采样间隔」。

### 3. 收尾判定块:带解释 + 对齐

- 判定块改为从**一张共享的 row 表**渲染(`verdict_rows()`),控制台与 HTML 同源,不会各说各话。
- 块尾新增 `--- what these mean ---`,每行指标配一条**简短英文解释**:coverage / cadence 怎么算、
  `delta` 与 `slope` 为什么"打架"是设计如此、`STABLE|CHANGED|GONE` 的区别、
  `duty` 是监控自身加的负载。
- 修掉 gate 7 在短跑里把**裸 `None`** 打进自己的 detail(`slope None%/h not actionable`)。
- **stdout 仍然全 ASCII** —— 中文只进 HTML。

### 4. 中文 HTML 报告(`perf_<dev>_<ts>.html`)

- 与 `report.json` **同名同目录**,由**同一份 payload** 渲染,所以两者不可能不一致。
- 四张表:①指标判定(结论 + 说明)②通道分布(含单位与「采到 N 拍 / 应采 M 拍」)
  ③检查门明细(9 道全列,未通过的原因写在行内)④链路事件。
- 顶部四色结果横幅(通过 / 注意 / 异常 / 样本不足)+ 未标定告警条。
- **自包含**:内联 CSS、无 JS、无图表库 —— 目标是能被拷出归档目录、在任何一台没装过 PPTP 的机器上、
  五年后打开还看得懂。
- 平台侧 `_companion_files()` 的认领扩展名由 `.csv` 扩到 `.csv` + `.html`,与报告一起进 `archive/`。
- `[archive]` 摘要行(v2.7.0 起那一行)补上报告附件的清点:它原先按名字常量只列 `report.json` / `chart.png` /
  `perf.csv`,而附件的名字是**从报告 stem 派生的**(`perf_<dev>_<ts>.html`),列不进常量表 —— 于是这一行会
  **漏报它自己文件夹里的一个文件**。改为再扫一遍后缀补上。这行是「这个文件夹里有什么」的清单,漏一个就是错。

### 5. gate 9(APP)两个方向都判反了 —— 已修

起因是给用户核对他要保留的四个关注应用时,顺手把 gate 9 拉出来跑了六个场景,
结果发现**这个门在两个方向上都给了错误答案**:

| 场景 | 旧行为 | 应该是 |
|---|---|---|
| 关注的 app 全程在前台 | pass | pass ✅ |
| 关注的 app 先在前台、之后被切走 | fail | fail ✅ |
| **脚本从桌面开始跑,关注的 app 从没打开过** | **fail** | inconclusive ❌ |
| **选了设备上没装的包** | **fail** | inconclusive ❌ |
| **app 仍在前台、但进程已经没了(真·崩溃)** | **pass** | **fail** ❌ |
| 不关注 | pass | pass ✅ |

**根因**:判据是「前台包名 != `watch_pkg` 连续 2 拍」,于是

- "从没出现过"和"出现过又消失"**算不出区别** → 前者被当成丢失(假异常);
- 而真正的崩溃(窗口还在、进程没了)**根本看不见** —— 前台包名仍然等于 `watch_pkg`。

更糟的是代码注释就写着「`gone` requires the process to be missing from pidof while it is
still the focused package」—— **注释描述的是意图,代码做的是另一件事**。真正记录 pid 级证据的
`fg_pid_lost` 确实在累加,**但没有任何一道门读它**(只出现在证据与显示行里)。

**修法**:

- 引入 `watch_seen` —— 只有**先健康过**(前台且进程在)才可能"丢失"。没出现过就不算丢失。
- 新增 `watch_pid_dead` —— 前台仍是它、但 `pidof` 空 = **就地死掉**,直接证据,不需要连续计数。
- 丢失按**事件**计数(`_watch_lost` latch):一直不回来只记一次,恢复后再丢才记第二次。
- gate 9 变成三个出口:`gone`/`pid_dead` → **fail**;设了 `watch_pkg` 但从没出现过 → **inconclusive**
  (detail 写明"没装?没打开?");否则 **pass**。
- 显示侧新增 `NEVER SEEN` 结论词与 `pid_dead=` 一列;APP 那行的解释补上两个新词的中文。
- 顺带补上**一直存在的可读性洞**:控制台 `GATES` 行原来只印 `9=fail(APP)` 而**不印原因**,
  原因只在报告里。现在非通过的门把 detail 一并印出来 —— 否则这一轮加的解释等于白加。

**加固**:`--selftest` 新增 7 条断言(五个场景 + 证据里必须带 `seen`/`pid_dead` + 不关注时不得产生丢失),
全部**用真 `Evidence` + 真 `judge` 驱动**,不是查形状。**并已用一份重新注入旧判据的副本反向验证**:
旧逻辑在这 7 条里**挂 5 条**(两个假异常 + 一个假绿 + 证据缺字段),新逻辑 7 条全过。
**不会失败的回归测试等于没有测试。**

**真机验证**(`B0403374A2A508001F00`,两次 45s 实跑):

- `watch=com.google.android.youtube.tv`(当时前台就是它)→ `APP : STABLE ... pid_dead=0`,`GATES : all pass`;
- `watch=com.amazon.amazonvideo.livingroom`(没在前台)→ `RESULT : INCONCLUSIVE`、
  `APP : NEVER SEEN`、`GATES : 9=inconclusive(APP): watched package ... never reached the foreground
  during this run - not installed on this device, or never launched`。

### 硬约束与踩坑

- `report` 摘要行**必须仍是最后一行、且是唯一能被 `_sniff_report_path` 认领的行**。新增的 HTML 行
  改用 `  html   = <path>`(`=` 而非 `:`):报告写在 `reports/` 下,路径本身含 `report` 子串,
  只靠扩展名检查去拒绝它太脆。selftest 现在**逐字复刻** server 的 sniff 规则,断言
  「有且只有一行可认领,且在最后一行」。
- `scripts/perf_monitor.py` 现已被 **E-SafeNet 包上密文壳**(2026-09-14):Python 读写完全正常、
  能编译能跑,但 Read/Grep **静默失效**(Grep 直接 0 命中)。此后本文件一律用 Python + 断言修改,
  详见 [TODO.md](TODO.md) §5。

### 验证

- `--selftest` **0 failure**:新增 6 条谓词断言 + 2 条 row 完整性断言 + 18 条 HTML 断言 +
  1 条 sniff 复刻断言。
- 假设备 harness **ALL HARNESS CHECKS PASSED**,含新增通道存活断言(已反向验证)。
- 真机 75s 实跑 `B0403374A2A508001F00`:`RESULT: OK`、9 门全 pass、覆盖率 100%、
  `duty=10.45%`、`fg_cpu n=2`、`APP: STABLE pkgs=1` —— **前台通道已恢复**。
- HTML 产物检查:标签配对、声明 charset、无未渲染占位符(含新的 `NEVER SEEN` 结论与中文说明)。
- gate 9 回归:7 条新断言全过,**旧判据副本挂 5 条**(反向验证)。
- 真机两次短跑覆盖 gate 9 的 pass / inconclusive 两个出口。
- 归档认领单测:`.json` / `.samples.csv` / `.events.csv` / `.html` 被认领,`.txt` 与
  「晚 1 秒的相邻 run」被拒绝。
- **仍未验证**:HTML 在真实浏览器里的观感(见 [TODO.md](TODO.md) §4)。

---


## v2.8.0 — 2026-09-14 (perf_monitor v2:分级采样 + 证据累积 + 收尾判定)

设计全文见 [docs/PERF_MONITOR_V2.md](docs/PERF_MONITOR_V2.md)(§12 为权威)。本次是 P2–P5 的落地:
调度器 / 采样瘦身 / 最小事件路径 / 累积器 / 报告 / judge / 平台归档扩展。

### 数据面

- **分级采样**:FAST(每拍 `/proc/stat`+进程数+uptime)/ MED(每 `ceil(5/T)` 拍:meminfo+GPU)/
  SLOW(嵌套 `med*ceil(30/(med*T))` 拍:dumpsys window + pidof + `/proc/<pid>/stat`)。
  嵌套周期保证不会出现「SLOW 到期而 MED 没到期」的畸形拍。时间基准改**整数毫秒**
  (浮点秒在 T=1.1/2.2 这类值上会让 `ceil()` 病态)。
- **一拍一次 adb**:8 个 section 用 nonce 帧(`@@NAME:<6hex>`)拼进**同一条**复合命令,
  设备侧的杂音无法撕开 section 边界。T=2s 实测占空比 **9.06%**(设计预测 9.07%)。
- **零阶保持**:没到期的指标带上一拍的值(`st` 字符 `h`),不是 `null`。若用 `null`,
  加上前端 `showSymbol:false` + `connectNulls:false`,稀疏序列会**整条消失**,归档的 `chart.png` 会是空白。
- **判定与采集分离**:`judge(evidence)` 是纯函数,`--selftest` 断言
  `judge(report["evidence"]) == report["verdict"]` 对每份报告成立。
- **9 态逐指标账本**:`f` 新值 / `h` 保持 / `n` 未到期 / `b` 基线 / `x` 失败 / `a` 缺失 / `d` 禁用,
  线协议 `st = "<res>:<9 个字符>"`。

### 判定面

9 道门(DATA / COVERAGE / CADENCE / TIMEOUT / ENVELOPE / REBOOT / MEMORY / CPU / APP)+ 时长不足的
INCONCLUSIVE 地板,合并序 `INCONCLUSIVE > FAIL > WARN > OK`。中途只发**事件**(事实),判定只在收尾打印一次。

### 输出面

- 实时行新增 `| st=ok:fffhhhhhh` 列。**该列必须定宽**(见下)。
- 中途事件行进 stdout:`[perf] t=<t>s | [!] offline_start: device unreachable`,
  `!`=告警级 / `i`=信息级 / `?`=未知类型。**限流在脚本侧**(60s 窗口)。
- 前端新增 `event` 分支,并**删掉了未知类型的 raw-JSON 回显** —— 那会让新记录类型在每次 WS 重连时
  把原始 JSON 刷进 console。
- 新增归档成员:`perf_<dev>_<ts>.samples.csv`(28 列 + 注释块)、`.events.csv`。平台侧
  `_companion_files()` 按**报告 stem 前缀**配对认领,与报告一起进 `archive/`(P5)。

#### `st` 列必须右对齐结果词(实现中发现的坑)

`st` 是 `<拍结果>:<9 个状态字符>`。9 个状态字符**永远**等宽,但结果词不等宽
(`ok` / `error` / `timeout` / `offline` / `partial`)。第一版直接原样拼进去,于是
**一次掉线就把它后面每一列推歪 6 格** —— 而这恰恰是定宽列存在的唯一理由,也恰恰是
最需要竖读的时候(故障中)。

改成**结果词 `padStart(7)` 右对齐**,整个 `st` 单元恒为 17 字符,状态字符永远落在同一列:

```
[perf] t=   30.0s | cpu= 25.3% | gpu= 26.7% | mem= 64.4% | fg= 31.9% | clk= 552MHz | st=     ok:fffffffff | pkg=...
[perf] t=   32.0s | cpu=     - | gpu=     - | mem=     - | fg=     - | clk=      - | st=offline:xxxxxxxxx | pkg=...
```

**代价**:实时行前缀从 82 字符涨到 105(`st` 占 23)。按 4MB 重放帽折算,单行约 309→332 B,
`chart.png` 的完整区间从 T=2s × 7.8h 缩到 **≈ 7.0h**(见 [TODO.md](TODO.md) §6.9)。
`samples.csv` 是全分辨率,不受影响。

**这一条是 node 层的渲染 harness 抓出来的**,不是 `node --check` —— 语法检查看不见
"列位置错位"。`node --check` 全绿而列已经歪了。

### 真机验证(2026-09-14,MT9676 `B0403374A2A508001F00`)

- 40s / 75s 两次实跑:`RESULT: OK`,`GATES: all pass`,`coverage=100%`,`cadence=1.0x`,占空比 9.06%,
  自测 adb 往返 55–62ms(设计文档里的 110–140ms 是另一条链路,故改为启动时自测而非硬编码)。
- **实跑抓到一个单测两层都没抓到的 bug**:`_fg_delta()` 只在比较成功后才推进基线,而首次调用
  永远无法成功 ⇒ 基线永不建立 ⇒ `fg_cpu` **整场为 null**(见 CHANGELOG 下方"发现的缺陷")。
- `--selftest` 60 项断言 + 假设备 harness(离线/超时/错帧/重启/计数回退注入)全绿。

### 已知边界(不阻塞)

风险与未标定项集中记在 [TODO.md](TODO.md) §6。要点:所有判定阈值**尚未标定**
(`judge_version=v1-uncalibrated`,健康报告也会显示未标定,避免假信心);
图表在 T=2s 下约 7.8h 后退化成后段缩略图(`samples.csv` 不丢数据);
多进程 app 的 `pidof` 覆盖缺口未在真机对照(`com.x:remote` 不被 `pidof` 枚举,见 §6.4)。

### 发现的缺陷(本次实跑修掉)

`_fg_delta()` 的基线推进条件写反,导致 `fg_cpu` 全程为 `baseline`、值恒为 `null`。
`--selftest` 与假设备 harness 都通过了 —— 因为假设备喂的 `utime` 单调递增,而断言只看
"状态字符合法"而不看"第二个 SLOW 周期之后必须出值"。**只有真机跑出来了**。
已补一条回归断言(第一次读=baseline,第二次读必须出值)。

---

## v2.7.6 — 2026-09-14 (perf 实时行改定宽列,可竖读)

**背景**:用户原话「stdout 的打印逻辑修改得美观一点,现在文字都挤在一起,很难观察到我需要的内容」。
关键是**那行不是脚本打的,是前端渲染的**([app.js](../static/app.js) `handlePerfLine` 的 sample 分支),
所以这是一处纯前端改动。

### 改动:定宽单元格 + 语义重排

```
现在:  [perf] 14:06:54 (2.0s) cpu=61.6% gpu=35.3% mem=67.4% fg=com.google.android.apps.tv.launcherx 39.4% gpu_clk=552MHz
改成:  [perf] t=    2.0s | cpu= 61.6% | gpu= 35.3% | mem= 67.4% | fg= 39.4% | clk= 552MHz | pkg=com.google.android.apps.tv.launcherx
掉线:  [perf] t= 7200.0s | cpu=     - | gpu=     - | mem=     - | fg=     - | clk=      -
```

三条规则,每条都有实际作用:

1. **每个单元格定宽** —— 缺值也必须占满自己的列,否则后面所有列会左移,竖读就断了。
   `pct` 6 字符 / `secs` 8 字符 / `MHz` 7 字符。
2. **`null` 渲染成同宽的 `-`** —— 于是掉线那一拍是**一排短横**,**永远不会被误读成"GPU 真的是 0.0%"**。
   这条直接对应踩坑 #14(本板视频硬解时 GPU≈0 是正确的):一排零值必须是不可能被误撞出来的形状。
3. **`pkg=` 挪到行尾** —— 包名长度不定,放中间会把后面每一列推歪。

另两个决定:**去掉重复墙钟**(`flushLogBuffer` 已经在行首加了 `[HH:MM:SS]`,第二个时间只增宽不增信息);
**列恒在**,即使某序列被禁用(无 root 时没有 gpu、`track_foreground=false` 时没有 fg)也照样输出 ——
一排短横是自解释的且保持对齐,而"有时有、有时没有的列"不是。

### 验证

用 node 跑了一遍渲染逻辑(这是 `node --check` 测不出来的部分),六行覆盖:首拍(基线未建立)、正常、GPU 真 0、
4 位数秒、**整拍掉线**、无包名:

- `pkg=` 之前的前缀长度**恒为 82**,所有 `|` 分隔符位置逐列一致
- 掉线行是一排对齐的短横

**已知边界**:`t` 超过 27.7 小时(5 位数秒)会溢出一格。在 24h 设计包络内,暂不处理。

**未验证**:真实浏览器里的观感 —— 用户 2026-09-14 已同意后续实跑确认(TODO §6.8)。

### 版本与验证

版本号 2.7.5 → **2.7.6**(改的是 `static/app.js`,但按纪律 server.py `version=` + healthz + index.html `?v=` ×3 同步 bump)。

---

## v2.7.5 — 2026-09-14 (logcat 人工开关)

**背景**:perf_monitor 要跑 8 小时级别的监控,而 logcat 是**恒开**的 —— 一次 run 就是 ~1GB,
没人会去看一份 1GB 的 logcat;它还会在**压测已经把设备打满**的时候再压一路 `adb logcat` 进程。
**监控任务等于自己干扰自己**,而 perf_monitor 要的恰恰是设备在自然状态下的表现。

用户决定:**logcat 还是需要的,但增加人工开关** —— 完全照串口日志那套的形状。

### 实现(三处,镜像 `serial_capture`)

| 位置 | 改动 |
|---|---|
| `RunRequest` | 新增 `logcat_capture: bool = True` —— **默认 True**,老前端不发这个字段时行为与今天完全一致 |
| `api_run` | 计算 `logcat_status = "starting" if req.logcat_capture else "off"`;`_cap_logcat` 按它设 `active/status`;**只有 `starting` 才真的起采集协程** |
| 设备卡 | 新增 `device-row5` 一行:`☑ logcat 日志`(默认勾选);状态走 `state.deviceLogcatCapture`,**不持久化** |

**为什么默认开**:logcat 是崩溃现场的唯一证据。开关的意义是"长时间监控时能关掉",不是"默认别采"。
**为什么不持久化**:和串口勾选同样理由 —— 它是**本次意图**;而 logcat 的失败方向更危险
(残留的 off 会让下一次压力测试**静默丢掉崩溃现场**)。不持久化意味着每次打开页面都回到"采集"。

### 该改动不碰的地方

- 关掉时**不建** `_cap_logcat_task`,`_stop_captures` 的"从未启动的通道"守卫天然跳过它
- 关掉时 logcat 日志文件**根本不创建**,`_archive_task` 的 `if not srcp.exists(): continue` 自然跳过
- `LOGCAT_ENABLED`(模块级总闸)语义不变,仍然优先

### 验证(真机,串行开/关各一次)

| 用例 | 结果 |
|---|---|
| `logcat_capture: false` | `captures.logcat = {active:false, status:"off"}`;归档**只有** `stdout.log` ✅ |
| `logcat_capture: true` | `captures.logcat = {active:true, status:"stopped"}`;归档 `logcat.log` + `stdout.log` ✅ |
| **不传该字段**(老前端) | 与 true 一致 —— 默认采集,向后兼容 ✅ |

静态门:`py_compile` / `node --check` 通过;版本三处一致。测试用探针脚本与测试存档已清理。

### 版本与验证

版本号 2.7.4 → **2.7.5**(server.py `version=` + healthz + index.html `?v=` ×3)。

---

## v2.7.4 — 2026-09-11 (脚本在同事机器上无法结束:stdout 管道两端编码不一致)

**用户报的 Bug**:项目代码发给同事使用,`perf_monitor` 跑起来**无法正常结束**。日志:

```
[14:06:54] [perf] 14:06:54 (2.0s) cpu=61.6% gpu=35.3% mem=67.4% fg=... gpu_clk=552MHz
[14:06:56]
[14:06:56] [error] 'gbk' codec can't decode byte 0xaa in position 290: illegal multibyte sequence
```

### 1. 根因:两端对同一根管道的编码理解不同

- [server_window.ps1:28](server_window.ps1#L28) 设 `$env:PYTHONIOENCODING = "utf-8"` → **脚本子进程写 UTF-8**
- server.py 的 `subprocess.Popen(..., text=True)` **没写 `encoding=`** → Python 回退到 `locale.getpreferredencoding()`,中文 Windows 上是 **GBK**

于是子进程的 UTF-8 字节流被按 GBK 解码。脚本 stdout 里只要出现任意一个 GBK 非法字节对(实测 `top -n 1 -b` / `dumpsys window` 那几段最容易带出来),`proc.stdout.readline()` 就抛 `UnicodeDecodeError`。

### 2. 为什么症状是"无法结束"而不是"报错退出"

那个异常逃到了 `_stream_logs` 底部的 `except Exception` —— 它只广播一条 `{"type":"error"}` 就 **return** 了,而**整个收尾块(等进程退出 → exit_code → 终态 status → `_stop_captures` → `_archive_task` → 广播 `end`)在它的下面**。于是:

- 任务**永远停在 `running`**,前端卡片不结束、没有 `end` 事件
- **不归档**,日志文件孤零零留在 `logs/`(任务 ID 命名,用户根本找不到)
- 脚本其实早就跑完了,`exit_code` 永远是 `None`

**为什么本机不必然复现**:这不是"同事环境特殊",而是**触发取决于脚本输出里有没有 GBK 非法字节**。本机 `locale.getpreferredencoding()` 同样是 `cp936`,同一段输出一样会炸。

### 3. 三层修复

1. **管道两端都钉死 UTF-8**(根治):`Popen(..., text=True, encoding="utf-8", errors="replace", env=child_env)`,其中 `child_env["PYTHONIOENCODING"]="utf-8"` —— 脚本端也显式声明,这样**即使服务端不是由 `server_window.ps1` 启动的**(例如直接 `python -m uvicorn`)两端也一致。`errors="replace"` 保证入站字节**任何情况下都不会再抛**。
2. **收尾块加了护栏**(结构性,防同类复发):读取循环有自己的 `try`,读到一半失败时**不再逃逸**,只记下原因 → 广播 → **终止子进程**(既然已经看不见它了,就不能留一个没人管的压测在跑)→ 用 `run_in_executor` 等待(避免阻塞事件循环)→ 照常走完终态与归档。**任务从此不可能停在半路。**
3. **同类隐患一并修**:`_adb_reconnect_offline`、`/api/adb/reconnect` 的 adb 调用,以及 `ir_runner.py` 里 3 处 `subprocess.run(text=True)` —— 都是同一个"读 adb 输出用本地编码"的写法。

### 4. 验证(真的做了 A/B,不是推断)

构造一行 UTF-8 合法、GBK 非法的输出(`chr(0x7EC8)+chr(0xAA)` → 字节 `e7 bb 88 c2 aa`,`(aa 0d)` 是非法 GBK 对),造出**只回退这两处修复**的 `server.py` 副本,同一根探针脚本各跑一遍:

| | 结果 |
|---|---|
| 修复前 | `status=running`,`exit_code=None`,**永不结束**,无归档;`logs/` 里留下两个孤儿日志 |
| 修复后 | `status=finished`,`exit_code=0`,已归档;`archive/.../stdout.log` **逐字节完整**(含那一行"有毒"原文) |

机制层复现的报错与用户贴的一字不差:`'gbk' codec can't decode byte 0xaa in position 6: illegal multibyte sequence`。
护栏路径(第 2 条)也单独验过:读取失败时任务**照样走到终态并归档**(`failed` / `exit_code=1`),而不是挂住。

### 版本与验证

版本号 2.7.3 → **2.7.4**(server.py `version=` + healthz + index.html `?v=` ×3)。

- 静态门:`py_compile server.py scripts/ir_runner.py` / `node --check static/app.js` 通过
- 真机:见上表 A/B;测试用的探针脚本与临时副本、测试存档已全部清理

---

## v2.7.3 — 2026-09-11 (串口勾选高亮 + 任务面板排版 + reports/ 不再堆积)

用户两条 UI 反馈 + 一次追问:「串口日志开的情况下需要以高亮颜色显示在设备卡片,现在的颜色太淡了」「任务卡片栏的几个功能按钮和文字描述排版不好看」;以及第二次追问「logs 和 reports 是不是可以删了,就保留个文件夹存 server 端 log 和运行日志就行了」。

### 1. 串口勾选在设备卡上高亮

之前整个串口行是 `color: var(--muted)` 的灰字,勾上了也跟没勾一样淡。现在**开启时整行高亮**:淡蓝底 + 蓝边框 + `串口日志` 文字变主题色加粗,COM 下拉的边框和底色也跟着变。

细节:padding/border **常驻**(关闭时透明),所以勾选/取消不会让卡片高度跳动;`serial-on` 只在"真的会采集"时才加(`wantSerial && !serialDisabled`)—— 设备没选中、任务在跑、离线、或脚本自己占着串口时,勾着的框不代表会采,不该高亮。

### 2. 任务面板标题栏排版

根因是**一个死的提示文案**:`<span id="task-hint">点击查看日志</span>` 在 HTML 里写死、**JS 从来没引用过**(grep 0 次),白占约 70px。而这个面板只有 300px 宽,加上「清空已完成的卡片」+「刷新」两个按钮之后必然挤在一起。

- 删掉这个死节点(引导信息已经在每张卡的 `title="点击查看日志"` 和控制台占位符里)
- `.panel-header` 统一:`gap: 6px 10px` + `flex-wrap: wrap`,按钮组 `.panel-actions` 用 `margin-left: auto`(换行后依然右对齐)+ `flex-shrink: 0`
- h2 内的 `.muted` 去掉全局 `margin-left`(改由 h2 的 gap 控制),并允许溢出省略

顺带:`device-hint` 同样是**死节点**(JS 0 引用),但它是空的、不占位,留着没动。

### 3. `reports/` 不再堆积(第二次追问的实质答复)

用户两次问"logs 和 reports 能不能删"。**两个目录都不能删**(理由见下),但**堆积问题是真的,已修**:

- **报告改为"搬"而不是"复制"**:归档成功后删掉 `reports/` 里的原件(先 copy 再 unlink,分两步 —— 删失败也不会丢唯一副本)。于是 `reports/` 变成一个**几秒钟的暂存区**,任务一结束就自己清空,**永远只有一份,不会出现两处副本逐渐不一致**。
- **一个任务的多份报告一起归档**:`app_launch_stress` 每跑一个 APP 写一份报告,之前只归档 stdout 里打印的那份(最后一份),**其余的永远留在 reports/**。现在 `_find_task_report` → `_find_task_reports` 返回整组,归档为 `report.json` / `report-2.json` / …,并把原件全删掉 —— 既不丢数据也不留垃圾。
- 已清理 6 个历史孤立报告(20KB)与测试存档。

**为什么两个目录本身留着**:
- `logs/` 正是用户说的那个"存 server 端 log 和运行日志"的目录 —— `server.out.log` / `server.err.log` + 运行期暂存(任务结束全部搬进 `archive/`)
- `reports/` 是**脚本契约**:脚本脱离平台用命令行单独跑时也写这里(平台运行时才把它搬进存档)。删掉它,脚本下次自己会 `os.makedirs` 建回来

**用户视角:只需要看 `archive/`。** 另两个目录正常情况下是空的或只有服务日志。

### 版本与验证

版本号 2.7.2 → **2.7.3**。

- 静态门:`py_compile` / `node --check` 通过;DOM id 交叉校验无缺失/无重复
- 真机:跑一次 `perf_monitor`,`reports/` 里**没有新增文件**(报告已搬进 `archive/perf/<run>/report.json`),`summary.json` 的 `report_source: stdout_line`;`reports/` 清空后不再累积

---

## v2.7.2 — 2026-09-11 (掉线容忍:串口自动重连 + 周期 adb reconnect + 日志链路自愈)

**用户原话**:
> 1. perf_monitor 这种,当存在设备重启或者 adb 掉线时是正常的
> 2. 不能因为掉线或重启就把任务卡片设备卡片清掉
> 3. 不过还是要简单提醒一下用户当前设备一掉线
> 4. 设备重启完成后需要能自动连接上之前的日志,且能自动连接上 adb 和串口

用户在追问里把范围收得很紧,三条明确约束:**只 `adb reconnect`,绝不 `kill-server`**(「不能影响其它脚本运行」)、**不做 monitor 脚本泛化**(「后面我一个一个让你改代码就行了」)、**不新增离线提醒 UI**(「直接拿之前的【临时离线就行了】」)。

### 探查先于实现:其中两条需求其实已经满足

- **monitor 脚本已经天然容忍离线,零改动**。`perf_monitor.py` 的 `adb_shell()` **从不抛异常**(失败返回 `""`),主循环照常每个周期产出一个全 null 的 sample 并继续 —— 正是用户说的"按周期发请求"。
- **任务卡从不会消失**,设备卡也已有"临时离线"合成卡;绑定关系的清理逻辑**已经**覆盖 `interrupting`。

所以这一版真正要修的只有两个洞 + 一处谓词。

### 1. 串口自动重连(主要改动)

**问题**:`_capture_serial` 是**一次性**的 —— 整个任务只尝试打开端口一次,读线程一死就永久 `failed`,没有任何重开路径。设备重启导致 USB 转串口重新枚举、或者碰一下线,就会让**之后几小时的 console 静默丢失**,而且看起来像个"正常停止"。

**改法**:改成受监督的重试循环(形状对齐已经过真机验证的 logcat 重启循环):

- 连续失败预算 `SERIAL_MAX_RESTARTS = 60`、间隔 2s;**收到任何真实数据就清零** —— 和 logcat 同一条规则(终身计数在长跑里必被打穿,见踩坑 #27)。
- 每次重试起**新的读线程 + 新队列**,并把 `cap["_stop_evt"]` 指向**当前这一次**的 Event,`_stop_captures` 才能在任何时刻停住它。
- 每次重试往 `serial.log` 写一行可见 marker `--- serial capture reconnecting #N (原因) ---`,**断过多久在存档里看得见**。
- 状态新增 `"reconnecting"`(非终止态,所以被停时正常变 `stopped/task_end`)。
- 全程**永不抛出** —— 采集失败只降级,不中断任务。

### 2. 周期 adb reconnect(新后台任务 `_device_watchdog`)

每 5s 跑一次 `adb devices -l`;对"**有 running/interrupting 任务、但设备不在列表里或状态不是 device**"的 serial,每 20s 发一次 **`adb reconnect offline`**。

**为什么是 `reconnect offline` 而不是 `kill-server`**:它只影响 offline/unauthorized 设备,**在线设备上正在跑的其它脚本完全不受打扰** —— 这是用户的硬约束。`kill-server` 会打断所有 logcat,那是「重置」按钮该干的事,不是自动逻辑该干的。

尝试只 `print()` 到服务端控制台,**不往 UI 写任何东西**(用户明确不需要提醒;设备卡上的「临时离线」就是唯一信号)。

### 3. 设备卡在 `interrupting` 时不再消失

合成"临时离线"卡的条件写死 `t.status === "running"`,漏了 `interrupting`。场景:重启脚本正卡在等待里,设备还没回来,用户按了「中断」→ 任务变 `interrupting` → **设备卡直接消失**。一行谓词修掉。

### 4. WS 发送超时后真正关掉 socket(需求 4 的关键一环)

`_broadcast` 送出超时后只把连接从内存集合里摘掉,**从不 `ws.close()`**。而前端的自动重连(`scheduleReconnect`)**只由 `onclose` 触发** —— 于是浏览器会**握着一个已经死了的 socket 永远收不到日志**,界面看起来还挂在任务上。这正是"连不上之前的日志"。

### 5. 顺带修复:`captures` 快照自 v2.7.0 起不再刷新

v2.7.0 删掉 2s 计数心跳时(它只为已删掉的通道 pills 存在),**连它唯一在做的 `t["captures"] = _public_captures(t)` 也一起删了** —— 于是 `GET /api/tasks` 里通道状态会**冻结在任务开始那一刻**,只有任务结束才更新真相。新加 `_announce_captures(task)`,在所有状态迁移点刷新快照 + 广播。

**刻意只在"进入 reconnecting"那一刻 announce,而不是每次重试都 announce**:`captures` 在任务对象上,因而属于 `tasksRenderKey` —— 每 2s announce 一次会让整个任务列表在重连风暴期间反复全量重渲染,正是踩坑 #23 记录的反模式。逐次记录交给 `serial.log` 的 marker 行,最终计数落在 `summary.json`。

### 版本与验证

版本号 2.7.1 → **2.7.2**(server.py `version=` + healthz + index.html `?v=` ×3)。

**真机验证(B0403374A2A508001F00)**:
- **正常回归**:勾选 COM9 跑 `perf_monitor`,行为与改动前逐项一致(无多余 marker、`serial.log` 0 字节、`stopped/task_end`)。
- **重连循环**:用未枚举的 `COM99` 跑,`serial.log` 出现 `reconnecting #1..#10` marker,`/api/tasks` 的通道状态变为 `reconnecting`(修复前冻结在 `starting`),中断后干净变 `stopped/task_end` 而非卡在 `reconnecting`。
- **句柄不泄漏**:COM99 任务结束后 `serial.Serial('COM9')` 仍能立即打开。
- **watchdog 判定**:`_serial_is_offline` 五种情形(列表空 / offline / unauthorized / device / 别的设备在线)全部正确;`_adb_reconnect_offline()` 调用后**在线设备依旧在线**,证明不会打扰其它脚本。
- **未证**:串口重连的**成功路径**需要真的拔插 USB 转串口线,以及 watchdog 的真触发需要设备真的掉线 —— 这两个都要动硬件,由用户下一次重启压测自然覆盖。

详细决策与踩坑见 [docs/HANDOFF_PROMPT.md](docs/HANDOFF_PROMPT.md)。

---

## v2.7.1 — 2026-09-11 (存档按模块分类 + 存档入口提到全局 + 报告误归属修复)

用户在 v2.7.0 试用反馈后的一轮跟进(用户原话:「打开存档应该是全局功能,所以应该放在优先级更高的位置」「【清空已完成】应该改成【清空已完成的卡片】」「archive 还是要做区分,不同模块的不能放在一起,像现在的 reports 文件夹下面一样做分类是最好的」)。

### 存档按模块分类

```
archive/
├── wifi/      20260911-124233_wifi_reboot_stress_B0403374A2A508001F00/
├── perf/      20260911-131305_perf_monitor_B0403374A2A508001F00/
├── battery/  sensor/  app-launch/  ir/  bt/  other/
```

- `ARCHIVE_MODULES` 是**显式映射表**(脚本名 → 模块),不靠文件名猜:`wifi_onoff`/`wifi_reboot`/`wifi_switch` 同属 `wifi`,`bt_reboot` 不是;未登记的脚本落进 `other/`,**存档根目录下永远只有模块文件夹**
- 模块名与 `reports/stress-test/<模块>/` 对齐,两棵树浏览方式一致
- `GET /api/archive/stats` 新增 `modules` 分模块统计;顶栏占用徽标 hover 可看各模块占用
- **向后兼容**:模块文件夹是 v2.7.1 才有的,`_archive_dir()` 会为**在旧版本里已归档的任务**回退到扁平路径(反正它找不到就试扁平),否则升级期间正在运行的服务会看到"日志是空的"而没有任何解释

### 存档入口提到顶栏(全局)

`打开存档` 按钮 + 占用徽标从**任务面板标题栏**移到**顶栏**(和 重置 / 关机 并列)。理由:存档是整个平台的事(每次运行的文件最终都在那里),不属于"任务卡片这一栏的操作"。

### 「清空已完成」→「清空已完成的卡片」

tooltip 改为「只把已结束的卡片从列表移除;存档目录永久保留在 archive/」。存档永久保留之后,原文案会让人以为会删掉东西。

### 修复:报告可能被归属到错误的任务(真机发现)

上一轮真机测试里 `ir_runner`(根本不写报告)的存档里出现了 `report.json` —— 兜底的 mtime 扫描把**前一个任务**的报告算给了它(同一台设备背靠背跑,两个任务的时间窗重叠)。

**归属错一份报告比少一份更糟**:它看起来完全可信,但内容是别人的。三重收紧:

1. **只有登记在 `REPORT_WRITING_SCRIPTS` 里的脚本才会走 mtime 扫描** —— `ir_runner` / `wifi_reboot` / `bt_reboot` 从不写报告,之前它们会捞到邻居刚产出的文件;
2. mtime 必须 **≥ 任务开始时间**(报告是脚本的最后一步,不可能早于运行开始);原来是 `开始-5s`,白白放宽了一截;
3. **一份报告只能被一个任务认领**(`_CLAIMED_REPORTS`),杜绝交叉归属。

实测:perf → ir_runner 背靠背跑,ir_runner 现在正确地只拿到 `['logcat.log','stdout.log']` 且 `notes: ['this script writes no report']`;连续两次 perf 跑各自拿到**内容不同**的报告。

### 清理

- `logs/` 下 48 个历史任务日志(1.6MB,8-17 至 9-11)已删,只留 `server.out.log` / `server.err.log`
- `reports/stress-test/` 下 59 个报告 JSON(508KB)已删,**模块文件夹保留**(脚本会自行 makedirs)
- 测试期间产生的 14 个存档已删;用户自己的 `wifi_reboot` 那一跑保留
- **`logs/` 和 `reports/` 两个目录本身都不能删**:`logs/` 是运行期工作区 + 服务自身日志的落点;`reports/` 是**脚本契约**(脚本独立 CLI 跑时也写这里,而平台运行时只**复制**一份进存档)。用户视角只需要看 `archive/`,另外两个是实现细节

### 版本

版本号 2.7.0 → **2.7.1**。**注意**:用户机器上当时跑着的是 v2.7.0(不含本轮改动)—— 两个不同构建都自称 2.7.0 会让人分不清有没有重启成功,这正是 bump 的原因。

---

## v2.7.0 — 2026-09-11 (任务结束自动存档 + 日志前端方案收敛)

用户两点要求:①「我没找到 log 存放在哪里,这些 log 需要做成结束后自动存档,包括 perf monitor 的图表也算,相当于 log 和 report 都需要结束就实时存档;所以导出的按钮可以暂时先隐藏掉了」;②「既然 logcat 和串口日志太多的话,建议不考虑做他们两个的前端显示了。前端只保留 stdout,直接将 logcat 和串口日志的前端显示方案删掉。建议还是稳定简单为主导。」

### ⭐ 重点 1:任务一结束,全部产物自动归拢到一个可读目录

**问题不是"没有存档",是"找不到"**:日志一直在 `logs/` 下,但文件名把 32 位 `task_id` 顶在最前面(`d4fa6d50c2fc..._perf_monitor_B0403374....log`),人眼认不出来;而且界面上没有任何入口能走到那个目录;产物还散在三处(日志在 `logs/`、脚本报告在 `reports/stress-test/**`(server.py 原本完全不知道它存在)、图表只活在浏览器内存里)。

现在每个任务结束(任何结束方式)自动生成:

```
archive/20260911-123312_perf_monitor_B0403374A2A508001F00/
├── stdout.log      ← 从 logs/ 移入
├── logcat.log      ← 移入
├── serial.log      ← 移入
├── report.json     ← 从 reports/stress-test/** 复制(脚本自己写的那份)
├── chart.png       ← 浏览器渲染回传
└── summary.json    ← 服务端写的清单
```

- **目录名不含 task_id**:`<时间>_<脚本>_<设备>`,一眼能认;task_id 存在 `summary.json` 里
- **迁移而非复制**:`logs/` 降级为运行期临时工作区,任务结束文件就搬走(复制会让永久保留策略下磁盘占用翻倍)
- **`summary.json`** 记录 task_id / 设备 / 脚本 / 参数 / 状态 / 退出码 / 起止时间 / 各通道状态 / 每个文件的 bytes+lines / `report_source` / `notes`(如"该脚本不写报告")
- **永久保留**:不做任何自动删除。任务面板标题栏显示 `存档 1.2 GB · 37 个` + 一个「打开存档」按钮

**报告 JSON 的关联**:6 个脚本保存后都会打印 `  report : <绝对路径>`,服务端在日志流动时廉价嗅探该行(精确);硬停时该行可能没打出来,兜底扫描 `reports/stress-test/**` 中 mtime 落在任务窗口内且文件名含该设备的 JSON。**两条路径都强制校验结果必须落在 `REPORTS_DIR` 内** —— 路径来自脚本 stdout,是数据不是可信路径。3 个脚本(`bt_reboot_stress` / `wifi_reboot_stress` / `ir_runner`)不写报告,`notes` 里如实写明,不留空文件。

### ⭐ 重点 2:图表由浏览器渲染后回传

服务端**没有**引入 matplotlib —— ECharts 只存在于页面里,为一个图养两套渲染不划算。任务结束时浏览器用现有的 `exportFullChartDataUrl`(与手动导出同一份代码,2560×720)渲染 PNG,POST 给 `POST /api/tasks/{id}/archive/artifact` 落盘。

- 触发点两处互补:WS `end` 帧(正在看该任务时立刻传)+ `refreshTasks` 轮询扫描(任务在**别的设备视图下**结束时 WS 帧会被 `currentTaskId` 过滤掉;以及**跑完才打开浏览器**时,`perfSamples` 靠 WS 重放重建)。用 `Set` 防重复。
- **诚实的降级**:任务结束时浏览器没开 → 归档里就没有 `chart.png`,但三份日志 + `report.json` 都在,`summary.json` 如实反映。这是这个方案的固有代价,用户已知悉并选择。

### ⭐ 重点 3:删掉 logcat / 串口的前端显示方案(减法)

用户判断这两种日志体量太大、不值得做前端展示,明确要求删除并"以稳定简单为主导"。**删掉**:`CHANNELS` 元数据、`TABS_ENABLED` 接缝、`state.activeSource`、`state.captureCounts`、`capturePillsHtml()` / `captureHint()` / `renderCaptureCounts()`、`#log-info` 里的通道计数 pills、`#channel-bar` 预留节点及其 CSS、`onmessage` 的 `capture_counts` / `capture_status` 分支、服务端只为 pills 存在的 `_capture_counts_ticker` 与该心跳帧、`_stop_captures` 末尾的一对广播、`_counts_snapshot()`、`appendLogLine` 的 source 参数、`openWs` 的 sources 参数。

**保留**:整个服务端采集引擎(采集、自动重启、上限、孤儿回收)、**`WS_SUBS` 源订阅过滤**(它不是显示功能而是承重结构 —— 前端只订阅 stdout,logcat 帧因此根本不上网线)、`ws_logs` 的 `?sources=` 参数(默认 stdout)。

**唯一新增的一行信息**:任务结束时服务端往 console 推一行归档摘要 ——

```
[archive] 20260911-123312_perf_monitor_B0403374A2A508001F00/ | stdout 22L | logcat 8L | serial 0L | report.json
```

理由:删掉 pills 后,**串口采集失败将没有任何信号**,而静默失效最容易被当成 bug 报。这一行用既有通道、零新 UI、不轮询,且行数本身就是"串口采到没有"的答案。若某通道异常,这里会带上状态与原因(`serial 0L (failed: port not present)`);触顶截断会显示 `capped`。

### ⭐ 重点 4:导出按钮隐藏

`#btn-export-log` 加 `hidden`(自动存档取代了手动导出)。**`onExportLog` 全部代码保留未删** —— 用户说的是"暂时",去掉 `hidden` 即可恢复。多源导出逻辑里的 `CHANNELS` 引用改为 `CAPTURE_EXPORT = ["logcat","serial"]`。

### ⭐ 重点 5:删除语义变更 —— 删任务不动存档

`×` 与「清空已完成」现在只从列表移除任务条目,**`archive/` 目录原封不动**(`_delete_task_logs` 改为 `_forget_task`,只做内存移除)。理由:既然承诺"永久保留",一次误点就不该摧毁它;两个按钮的 `title` 也已改成「从列表移除(存档保留在 archive/)」。

### ⭐ 重点 6:采集引擎四处真机隐患(对抗审查发现并修复)

对上一次真实 `wifi_reboot_stress` 跑做了一轮多路对抗审查(23 个 agent,10 条结论存活验证)。其中四条直接损害"日志永久保留"这个承诺 —— 永久保留的日志如果**悄悄少了一大截**,比不保留更糟:

1. **`LOGCAT_MAX_RESTARTS = 5` 会把 logcat 永久杀死在第 6 次重启**(high)。旧实现 `restarts` 只增不减,是**终身预算**;而设备重启期间 adb 可能直接退出(而非阻塞 `- waiting for device -`),一次 30 秒宕机就要烧掉 ~15 次重启 → 默认 100 轮的 `wifi_reboot_stress` 在第 6 轮左右就**彻底停止采集**,之后 94% 的运行一行都没有。**修复**:预算改为**连续**失败计数,logcat 一旦真的吐出输出就清零(输出本身就证明设备回来了);上限 5 → 60(≈2 分钟持续失联才放弃)。
2. **收尾会把通道自己给出的终止诊断改写掉**(high)。`_stop_captures` 无条件把 `cap["status"]` 写成 `stopped` / `detail="task_end"` —— 一个串口从未打开成功(`failed: port not present`)的通道,任务结束时会被报告成**健康地正常停止**;`capped`(日志被截断)同样被抹成正常。**修复**:新增 `keep_status` 分支,`failed`/`unavailable`/`skipped`/`capped` 一律保留自身状态;teardown 照常执行(该死该收的进程仍然要收)。实测:COM99 跑完仍是 `{"status":"failed","detail":"port not present: COM99"}`。
3. **串口读线程在开端口 3 秒之后死掉是完全不可见的**(medium)。`err` 只在 3 秒开窗里被读一次;开窗之后线程若因 USB 转串口被重新枚举/线缆故障而死,消费端会永远阻塞在空队列上,状态**冻结在 `running`**。**修复**:消费循环改为 `asyncio.wait_for(q.get(), timeout=1.0)`,超时时检查 `thread.is_alive()`,死了就置 `failed` 并带上原因。
4. **重启后 `-T 1` 会丢掉整个开机窗口**(medium)。`-T 1` 只回读 1 行 —— 而重启的原因通常正是设备重启了,开机日志**就在刚被刷新的环缓冲里**,结果文件要等 adbd 回来(约开机 25 秒后)才有内容。**修复**:首次启动仍 `-T 1`(不把旧历史灌进新任务),**重启时改为 `-T 2000`** 回填开机日志;marker 行明确写出"可能与此前输出重叠"(adb 抖动导致的非重启场景确实会重复)。

另有**一条已确认但刻意不修**:串口停止时,读线程最后那半行(以及队列里残余)必然丢失 —— 修它需要在 `_stop_captures` 最敏感的取消路径里插入等待,代价与收益不成比例(低危,一行)。**已写入 HANDOFF 踩坑清单**。

以及**两条属于脚本而非采集引擎**的问题(已报给用户,未改):
- `wifi_reboot_stress.py` **从不校验 `adb reboot` 是否真的重启了** —— `rc` 被丢弃,"设备已上线"的轮询对一个**从未重启**的设备同样成立,于是失败的 reboot 会被记成 PASS。对一个压测平台来说,"假绿"是最坏的输出。
- 在第一轮迭代完成前中断,stdout 会打印 `passed: 0 / 0` + `success rate: 0.0%` + exit 1,**读起来像彻底失败**,而实际上什么都没测。

### ⭐ 重点 7:256MB logcat 上限对重启类脚本定小了(实测修正)

上一次真机 `wifi_reboot_stress` 实测暴露:**62 秒产生 10245 行 / 1.34 MB logcat,几乎全部来自那一次重启的开机风暴**(重启前只有 63 行),即**一次重启 ≈ 1.3 MB**。三个重启类脚本的 `iterations` 默认都是 100 → 单次默认跑就是 **~128 MB**,而旧上限 256MB 会在约 **197 轮**触顶并静默截断。

- `LOGCAT_MAX_BYTES` 默认 **256MB → 1GB**
- v2.6.0 文档里那句「~0.82 MB/小时」是**空闲设备**上量的,对最需要它的长跑脚本差了约 95 倍,已按实测口径改写并注明量级差异

### 版本与验证

- 版本号 2.6.0 → **2.7.0**(server.py `version=` + healthz + index.html `?v=` ×3)
- **静态门**:`python -m py_compile server.py`、`node --check static/app.js` 通过;已删符号全库 grep 无残留;JS 引用的 DOM id 与 index.html 交叉校验无缺失
- **真机逐路径验证(B0403374A2A508001F00)全部通过**:
  - 正常结束:`archive/<时间>_perf_monitor_<设备>/` 三份日志 + `report.json`(`report_source: stdout_line`)+ `summary.json`;`logs/` 无残留;`end` 帧带 `archive`;console 出现归档摘要行
  - 串口勾选:`serial.log` 一并归档,摘要行显示 `serial 0L`
  - **中断**:`status: interrupted`,采集活过 `interrupting`,`report.json` **仍然归档到**(perf_monitor 捕获 Ctrl+C 后照常写报告)
  - 无报告脚本(`ir_runner`):`notes: ["this script writes no report"]`,`report_source: null`,不留假文件
  - 图表回传:`chart.png` 落盘 75 字节 + `summary.json` 的 artifacts 同步更新
  - **路径安全**:非法 artifact 名(`../../evil.txt`)→ 400;非法 base64 → 400;非法 task_id → 404;`?source=../../etc/passwd` → 400
  - 归档后按 `?source=` 读回三源正常(走 `archive/<dir>/<member>`)
  - **删任务不动存档**:任务条目消失,`archive/<dir>/` 六个文件原封不动
  - 未归档老任务仍从 `logs/` 读取(单测 `_log_path` 回退分支)
  - `GET /api/archive/stats` 与实际占用一致
- **未证(需浏览器)**:前端实际渲染效果与新按钮交互 —— 服务端与 DOM 契约已交叉校验,但没有真实浏览器跑过

---

## v2.6.0 — 2026-09-11 (每任务三源实时日志:stdout + logcat + 串口)

> **⚠ 后续修正(v2.7.0)**:本节"重点 5"描述的**前端通道路由 / 计数 pills / `#channel-bar` tab 接缝已被 v2.7.0 整体删除** —— 用户决定前端只显示 stdout,不做 logcat/串口的显示。本节保留为历史记录;当前实现以 v2.7.0 一节为准(采集侧不变,显示侧已移除)。

用户要求:**每一次脚本执行都能实时监控 logcat 和串口日志**,后续增加自动开启与自动保存;并追加"三种日志的文件命名要包含设备/时间/测试项信息"。

### ⭐ 重点 1:服务端采集引擎(9 个脚本零改动)

**采集是服务端职责** —— 脚本拿不到自己的 `task_id`/日志路径,因此 `scripts/*.py` 一行未改,平台在 `api_run` 起采集协程、在任务终态统一收尾。

- **logcat 恒开**(每个任务都采):`adb -s <serial> logcat -v threadtime -T 1 -b main -b system -b crash '*:I'`,走 `asyncio.create_subprocess_exec`(事件循环是 ProactorEventLoop,不占 executor 线程)。`-v threadtime` 是唯一能与 stdout 时间线对齐的格式;`*:I` 而非 `*:V`(后者 24h 可达 GB 级)
- **自动重启循环**(最多 5 次、间隔 2s):统一处理 `POST /api/adb/reconnect` 的 `kill-server` 与设备重启(reboot 类脚本跑数小时)。每次重启写一行可见 marker `--- logcat capture restart #N ---` —— 这是用户唯一能察觉"中间断过"的途径;`restarts` 计数进卡片
- **体量上限**:`PPTP_LOGCAT_MAX_MB`(默认 256MB),超限写尾行说明 + `status="capped"` 并退出重启循环,**保留文件不轮转**(开头几分钟最有价值)。实测本机 logcat 约 1.6 行/秒(~0.82MB/h),上限只在突发时触发
- **永不 `logcat -c`**:`app_launch_stress.py` 依赖设备端环缓冲做启动耗时交叉验证,清缓冲会**静默**破坏它的判定
- **串口按任务勾选**:设备卡 checkbox + COM 下拉 → 服务端裸守护线程读串口 + `asyncio.Queue`(满则**丢最旧**并计 `dropped`)。不用 `readline()`(console 提示符无换行会每次等满超时);**不动 DTR/RTS**(实测打开 COM9 未触发设备 boot banner,证明对 console 无副作用)
- **串口争用仲裁 —— "脚本优先"**:声明了 `serial_port` 的脚本拥有该端口(`battery_inout_stress.py`)。此时平台不抢口,标 `skipped`/`script_owns_port`(任务照跑、logcat 照采),前端禁用控件 + 中文提示。理由:抢口会让保活失败、**电量曲线变平**
- **永不致命**:pyserial 缺失 / 端口未枚举 / 名非法 / 被占 → 一律降级为 `failed|unavailable|skipped`,绝不从采集抛出、绝不令 `/api/run` 失败

### ⭐ 重点 2:WS 按源订阅(性能关键)

- `WS_SUBS: dict[int, set[str]]`(`id(ws)` → 订阅源);`/ws/logs/{task_id}?sources=` 接受逗号分隔多源(未知 token 忽略而非断流,默认 `stdout`)
- `_broadcast(task_id, msg, source)` 跳过**没有订阅者**的源 —— 没人看的 logcat **只花一次集合推导,零成本**
- **行为变更**:`_broadcast` 新增 **5s 发送超时**(原无超时)—— 卡死的 TCP 连接不再能永久阻塞读端
- `state` 帧带 `captures`,重连即可得知有哪些通道及状态;重放按订阅源、固定顺序 stdout→logcat→serial,各自发自己的 `replay_meta`
- **实测订阅过滤生效**:仅订阅 stdout 的客户端收到 14 帧 stdout、**0 帧 logcat**,而服务端实采 28 行 logcat

### ⭐ 重点 3:日志命名含设备/脚本/时间(用户追加需求)

- 文件名方案:`logs/<task_id>_<yyyyMMdd-HHmmss>_<script-stem>_<device>.<suffix>`
  - `..._perf_monitor_B0403374A2A508001F00.log`(stdout)
  - `..._perf_monitor_B0403374A2A508001F00.logcat.log`
  - `..._perf_monitor_B0403374A2A508001F00.serial.log`
- **`task_id` 刻意保留在最前面**:删除 glob `f"{task_id}*"` 与 `^[0-9a-f]{32}$` 路径防护无需修改即继续工作,未来第 4 个通道零成本覆盖
- **服务器下发确切文件名**(`t.log_files`),前端**从不重建路径**;老任务无 `log_files` 时回退到约定名
- 前端导出文件名同步改为 `pptp_<脚本>_<设备>_<时间>.<源>.log`,导出**多源文件**(stdout + 每个有内容的通道 + csv + png),文件头含各通道状态与 `truncated` 标记
- 未采用"每次运行建子目录":Windows 在捕获文件句柄打开时拒绝 `rmtree`

### ⭐ 重点 4:自动保存 + 取回 API + 清理

- 三源各自落盘(`"a"` 追加、逐行 flush),写入在广播**之前**,慢客户端不会丢盘上数据
- **`GET /api/tasks/{id}/log?source=`** 一律走**尾部读**(`_read_tail`,默认 4MB)—— logcat 上限 256MB,整文件 `read_text()` 会硬冲内存。**顺带修掉 stdout 重放路径的同一隐患**;响应带 `truncated` 标记
- `source` 白名单校验,非法值 → **400**(实测 `?source=../../etc/passwd` → 400)
- 新增 `GET /api/serial/ports` → `{"ports":[...],"available":bool}`;pyserial 缺失降级为空列表,永不抛
- `/api/scripts` 返回项新增 `owns_serial`(源嗅探,不跑子进程)
- 清理:删除 glob 从 `f"{task_id}.*"` 改为 `f"{task_id}*"`(覆盖可读名);两个清理循环与 `api_force_cleanup` 补 `WS_SUBS.pop`
- 孤儿回收:`_reap_orphan_logcat()` 按 `-v threadtime` + `-T 1` **双重匹配**(不误杀开发者手敲的 `adb logcat`)

### ⭐ 重点 5:前端通道路由(本轮只展示 stdout,但按通道就位)

- **按用户明确要求:前端暂时只展示 stdout**,后续做选项卡切换。故 `appendLogLine(line, source)` 对非 `activeSource` 直接 return,`flushLogBuffer` 的单缓冲假设**完全不动** —— 开 tab 只是 UI 改动
- `state` 新增 `activeSource` / `captureCounts` / `deviceSerialCapture` / `deviceSerialPort` / `serialPorts`;`CHANNELS` 元数据 + `TABS_ENABLED = false` 作为接缝
- `renderLogInfo`(每 2s 整体重写)结尾追加**通道计数 pills**(logcat / 串口的行数 + 状态点);计数由 2s `capture_counts` 心跳帧更新并**就地 patch**(不被 4s 双重轮询拖慢)
- **`captureCounts` 刻意不进 `tasksRenderKey`**,且 `_public_captures` 公开快照**只含 active/status/restarts/detail/port、不含 lines/bytes** —— 否则每 2s 轮询都会判定"变了"并触发全量重渲染
- `#channel-bar` 作为 `#log-info` 的**兄弟节点**预留(`#log-info` 每 2s 会被整体重写,嵌进去的东西都会被销毁)
- 设备卡新增 `.device-row4` 串口勾选行;`owns_serial` 时控件禁用 + 提示"该脚本自身占用串口,平台串口抓取已自动让位"
- 持久化:`deviceSerialPort` 持久化、`deviceSerialCapture` **不**持久化(与 `deviceScripts` 既有取舍一致)

### 版本与验证

- 版本号 2.5.2 → **2.6.0**(server.py `version=` + healthz + index.html `?v=` ×3)
- **静态门**:`python -m py_compile server.py` 通过;`node --check static/app.js` 通过
- **真机端到端验证(B0403374A2A508001F00 / EcoPro)**:
  - `/healthz` → 2.6.0;`/api/serial/ports` → `["COM9"]`;`owns_serial` 仅 `battery_inout_stress.py` 为 True
  - 可读文件名的三源文件同时落盘;`?source=` 三源往返正常;非法源 → 400
  - 订阅过滤:仅 stdout 的客户端 14 帧 / 0 帧 logcat(服务端实采 28 行)
  - 任务终态自动停:`{"logcat":{"status":"stopped","detail":"task_end"}}`
  - 中断路径:采集**活过** `interrupting`,在真正 EOF 时才停(要的就是停机/收尾日志)
  - 串口仲裁:`{"serial":{"active":false,"status":"skipped","detail":"script_owns_port"}}`,且该状态**未**被收尾逻辑改写成 `stopped`
  - 删除/清理:三个可读名文件全被 `glob(f"{task_id}*")` 清掉;任务结束后无孤儿 `adb.exe`
- **未证**:串口**数据通路**(COM9 开合正常、无副作用,但 14s 内读到 0 行 —— 空闲 console 无输出属合理表现,"读到字节"尚未被观测到)。证明办法见 HANDOFF「未完成需求」

---

## v2.5.2 — 2026-08-26 (新增重启蓝牙回连压测脚本 bt_reboot_stress.py)

用户已将测试设备连上蓝牙音箱(BOGASING-G4),新增**重启 + 蓝牙音箱回连**压测脚本,仿 wifi_reboot_stress.py:

- **脚本** `scripts/bt_reboot_stress.py`:每轮 `adb reboot` → 等待设备上线 → **轮询 A2DP 音箱回连**(最多 `bt_reconnect_timeout` 秒,每 5s 查一次)→ PASS/FAIL;结束打印通过率 + 每轮 P/F。
- **回连判定**:`dumpsys bluetooth_manager` —— 要求蓝牙适配器已开启(`enabled: true`)**且**至少一个 A2DP 状态机处于 `mConnectionState: CONNECTED`(即音箱真正回连,而非仅"蓝牙开着")。真机实测:正常回连 ~6s。
- **可配参数**:`iterations` / `wait_sec`(重启后等待)/ `bt_reconnect_timeout`(回连超时,默认 90s)/ `back_online_timeout`(设备上线超时)。脚本 100% ASCII(代码文件无中文,stdout 纯英文)。
- **平台接入**:`scripts/*.py` 自动发现,前端脚本卡无需改动;版本号 2.5.1 → 2.5.2(server.py / healthz / 前端 `?v=`)。
- **真机验证(2026-08-26)**:① 音箱刚连上后立即重启 → 回连失败(CONNECT_TIMEOUT,检测正确报 FAIL)—— 疑似音箱进入休眠/连接未稳定;② 用户在蓝牙设置里手动重连音箱后再次重启 → **~6s 自动回连,脚本报 PASS**。结论:正常绑定状态下自动回连工作正常,`bt_reconnect_timeout` 默认 90s 足够。
- **用户约束(实测确认)**:本设备**蓝牙开关未对用户开放**,用户无法手动关蓝牙 → 不做"关蓝牙→开蓝牙"回连场景,只测重启回连。

---

## v2.5.1 — 2026-08-25 (前端 log console 全英文 + 折线图固定 1h 滚动窗口 + 放电关机卡片 + 全程图导出)

用户 3 点要求:① 前端 log 打印不能存在中文;② 折线图显示范围从 2min 刻度拉到 10min、整体约 6 个刻度,即实时显示 1h 曲线;③ perf_monitor 同步。追加:放电测试结束后设备关机,卡片不能消失,做专门状态(仿临时离线卡)。

- **log console 全英文/ASCII**(需求 ①):凡打进 `#log-console` 的内容全部转英文 —— 电池可读行 `[batt] HH:MM:SS (X.Xs) level=87% temp=36.2C voltage=8.4V status=charging`(+ 跳变 `[!] jump[type] prev->87%`)、meta 行(`[batt] monitor start | mode=charge | port=COM9` / `[perf] monitor start | CPU=...`)、WS 重连/重放/截断提示、控制台占位符。**删除 `STATUS_ZH`/`JUMP_TYPE_ZH` 中文映射表**;时间戳统一走 `fmtClock`(`toLocaleTimeString("zh-CN",{hour12:false})`)强制 24h `HH:MM:SS`,杜绝 zh-CN 浏览器吐"下午3:30"。**UI 文案(卡片/弹窗/图表系列名/导出表头)仍为中文**,仅 log 输出英文
- **折线图固定 1h 滚动窗口**(需求 ②③):x 轴 `interval: 10*60*1000` + `splitNumber: 6`,右缘 = 最新样本墙钟,窗口整体 1 小时(~6 个 10min 刻度);`flushPerfChart` 每次 flush 把窗口滚到最新样本、并剔除窗口外的点;**移除 dataZoom**(与固定窗口互斥)。perf_monitor 与 battery_inout_stress 共用同一套 buildPerfOption/flushPerfChart,天然同步生效
- **放电测试卡片持久化(追加)**:电池充/放电测试结束后设备若已关机,设备卡不再消失 —— `renderDevices()` 对"终态 battery 任务 + 设备不在 `adb devices` + 该任务仍是设备最新任务"的设备合成 **"放电关机"专用卡**(虚线红边 + "设备已关机"状态 + "放电关机"徽标,仿临时离线卡;`latestTaskOf()` 取设备最新任务);设备重新上线自动恢复在线卡,删除任务即删除该卡
- **导出 chart.png 改为全程图 + 0.5h 刻度(追加)**:导出的图片从"实时窗口那 1 小时"改为**任务全程**(从 `perfSamples` 全量渲染,不受屏幕 1h 滚动窗口限制,切走后再导出同样得到全程图);横轴 **0.5h 一个刻度**(`buildPerfOption(taskId, true)` fullRange 分支,`xInterval=30*60*1000`)。实现:离屏 1280×360 div + `echarts.init` → `getDataURL({pixelRatio:2})`,无需手动用 csv 二次出图
- **范围界定**:本次只动前端 app.js + style.css + index.html 占位符 + 版本号;**脚本零改动**(其 stdout 本就 ASCII)
- 版本号统一至 2.5.1(server.py / healthz / 前端 `?v=`)
- 验证:node --check 通过;log 路径 grep 无中文;待真机跑 perf + battery 任务复核 log 全英文 + 1h 窗口滚动 + 导出全程图(0.5h 刻度)

---

## v2.5.0 — 2026-08-25 (电池充放电压测 + 前端图表类型泛化)

### ⭐ 重点 1:电池充放电压测脚本 battery_inout_stress.py(实时电量/温度曲线)

用户新增的电池充放电脚本重写为平台契约(原独立脚本用 matplotlib 出图 + Excel 报告,已废弃):

- **数据源**:电量/温度/电压/状态来自 `dumpsys battery`。本机 `ro.config.batteryless=true`,电池由外部 BMS MCU 管理,health-service 默认不轮询;adb 又因 SELinux 限制无法开启轮询(`Failed transaction 2147483646`),**只能走串口 console root shell** 发 `set polling true`。因此 `serial_port` 做成参数,前端下拉框运行时枚举 PC 串口(试跑时用户自选 COM)
- **可配参数**:`mode`(下拉框 `charge` / `discharge`,**充放电分开跑**)、`serial_port`(COM 口下拉)、`interval_sec`(默认 10s)、`temp_warn_c`(温控告警阈值,默认 45°C)、`full_hold_sec`(100% 稳定窗口,默认 60s)
- **保留原脚本的错误检测**:电量**跳变检测**(方向感知:充电/已充满时骤降→异常降低、充电骤升→异常升高;放电时相反;状态未知按幅度),打印 `[batt] warning: level jump [...]` 并计入报告 `summary.jump_counts`;**温控告警**(≥ `temp_warn_c` 告警一次,降温后重新武装)
- **单次中止信号**(用户指定):**电量到 100%(稳定)** —— level≥100 且 status=full,或连续满 100% 达 `full_hold_sec` → `stop_reason="full"`;**设备关机** —— 连续 2 次读电池失败(adb 失联)→ `stop_reason="power_off"`;手动中断 → `manual`
- **PERF 线**:meta `{sources:{level,temp,voltage_mv,status,com_port,mode}}` + sample `{level,temp,voltage_mv,status,jump,jump_type}`,都带 `type` + `clock`(遵守 v2.4.1 契约)
- **不输出 Excel**:前端出图 + 三件套导出(log.txt / csv / png),与 perf_monitor 一致;报告 JSON 落 `reports/stress-test/battery/battery_<dev>_<ts>.json`(已 gitignore)

### ⭐ 重点 2:前端 perf 管道按脚本类型泛化(perfKind),电池曲线零新增组件

- 新增 `perfKind(taskId)` → `"perf" | "battery" | null`(按脚本名),`isPerfMonitorTask` 改为通用 `isPerfTask`
- **电池曲线**:`电量`(蓝,左轴 0-100%)+ `温度`(红,右轴 auto °C),tooltip 按系列显示 `%` / `°C`;可读行 `[batt] 15:20:55 (10.0s) 电量=57% 温度=40.8°C 电压=11.6V 状态=充电中`,跳变时追加 `⚠ 跳变[异常降低] 57→55%`
- **CSV 按类型分支**:battery 表头 `t_sec,t_wall,level_percent,temp_c,voltage_mv,status,jump_delta,jump_type`,导出后缀 `.batt.csv`
- **语言分工**:脚本 100% 英文/ASCII;前端新增文案用中文(与现有 UI 一致)。注意电池脚本的**参数弹窗标签是英文**(PARAMS label 在代码文件里),这是"代码文件无中文"规则的直接结果
- 其余(`syncPerfChart`/`flushPerfChart`/`backfillPerfOption`/三件套导出)kind 无关,复用现有管道

### 其他

- 版本号统一至 2.5.0(server.py / healthz / 前端 `?v=`)
- 真机验证:独立跑 5s 间隔 2 样本通过(meta + sample 格式正确;串口 COM9 实际开启 Polling=1,真实数据);`--probe` 通过(设备可达 + 电池读取 OK + COM9 枚举);`--dump-params` 返回 5 个字段
- 文档:README 电池章节 + CHANGELOG + HANDOFF(设计决策 + 踩坑 + app.js 行号重映射)+ SIMPLE-ARCHITECTURE + FRONTEND_UX

---

## v2.4.2 — 2026-08-25 (性能图表长时检测体验)

针对"检测时间比较久"的三个问题:

- **导出图固定尺寸**:chart.png 统一按 **1280×360 归一化导出**(2x 后 2560×720)。不管跑 10 分钟还是 24 小时,图片都保持紧凑可读的固定比例,不会再"很长";实时图本来就是固定宽度 + 时间轴自动压缩,这个改动把导出也钉死
- **横轴随跨度自动压缩 + 比例尺自动拉大**:x 轴加 `splitNumber: 6`,刻度数稳定在 ~10 个 —— 时间跨度越长,单格覆盖的时间越大。headless 实测:10 分钟跨度刻度间隔 54s,2 小时跨度 800s(间隔随跨度放大 ~15 倍),刻度数不随跨度变密。`hideOverlap` 兜底防标签重叠
- **采样上限 3600 → 86400**:支持 24h@2s 长时检测全量保留,不再丢弃早期历史
- headless Edge(CDP)端到端验证通过:2h 合成数据刻度间隔 800s + 导出 PNG 精确 2560×720
- 版本号统一至 2.4.2(server.py / healthz / 前端 `?v=`)

### 补充说明(2026-08-25 实机排查):播放视频时 GPU 曲线为 0 是硬件正常,不是故障

用户反馈"播放视频时 GPU 数据读不到",实机排查结论:

- **计数器本身正常**:启动 Settings 时 mali0 `busy_time` 实测从 446506278 → 446515551(真实 GPU 渲染事件会跳);同机 11:56 报告 GPU 平均 42%
- **本机是 MStar 显示管线**(SurfaceFlinger 报 `displayName="MStar Demo"`)的 MT5896 Android TV:视频**解码走硬件 VPU**、**画面合成走 HWC 硬件叠加层**,GPU 只画 UI。静态画面 / 全屏视频下 GPU 真实闲置,读数 0% 属正常硬件行为
- **14:11 报告佐证**:GPU=0 时 `_gpu_prev` 的 busy 全程冻结、idle 正常增长,`fg_pkg` 全程是 launcherx(前台为桌面,视频在后台/预览播放)
- **结论**:GPU% 指标在"应用切换 / UI 动画"压测场景有效(11:56 的 42%),视频播放场景预期为 0 —— 无需修代码,已文档化(见 HANDOFF 5.5.5 #14)

---

## v2.4.1 — 2026-08-25 (修复性能图表"不出线")

### ⭐ 根因修复:PERF sample 行缺 `type` 字段,前端全部丢弃

- **症状**(用户实测 3 项):① 图表实时不出折线,到手动结束都是空白;② 横轴没有时间点;③ 点导出只下载了 txt,没出 csv/png
- **根因**:脚本 `perf_monitor.py` 打印 sample 行时 `emit` dict **没有 `"type": "sample"` 字段**(docstring 写了 `type:sample`,代码没实现——契约与实现脱节)。前端 `handlePerfLine` 以 `obj.type === "sample"` 分发,sample 被当成"未知 PERF 类型"原样回显,`perfPending` 永远为空 → 图表无数据 → `perfSamples[taskId]` 空 → 导出只剩 txt。**一个字段缺失,三条症状全部命中**
- **修复**:`emit` 补 `"type": "sample"` + 新增 `"clock"`(wall-clock epoch ms,供时间横轴用)。前端无需改分发逻辑
- **教训**:wire 契约必须以"端到端日志实证"为准,不能只信 docstring;前端对数据型消息应做防御(无 `type` 时按 sample 兜底解析)

### 改进

- **横轴改为真实墙钟时间轴**:x 轴 `type:"time"`,标签 HH:MM:SS(local time),ECharts 自动稀疏(不密);数据点用脚本 `clock` 字段,旧日志(无 clock)回退用 `started_at + t*1000`
- **前台 APP 曲线独立右轴(0-400%)**:`top` 的多核 %CPU 可超 100(实测 116-123%),单轴会顶格裁切;原需求本就允许"Y 轴可以不同",故加第二根右侧 y 轴
- **控制台可读行带墙钟**:`[perf] 11:44:48 (0.0s) cpu=... gpu=... mem=... fg=... 3.0% gpu_clk=552MHz`
- **CSV 增加 `t_wall` 墙钟列**(HH:MM:SS,便于 Excel 直接看时间)
- **导出三件现在都正常**:perfSamples 入缓冲后 csv/png 随 txt 一起下载

### 其他

- 版本号统一至 2.4.1(server.py / healthz / 前端 `?v=`)
- 真机 E2E 验证通过:脚本样本带 `type:sample`+`clock`;headless Edge 驱动真实页面(点设备卡→选 perf_monitor→跑)确认图表可见+canvas 渲染+无控制台异常
- 文档:README / HANDOFF 更新(wire 契约实证、墙钟横轴、FG 右轴、CSV 列)

---

## v2.4.0 — 2026-08-25

### ⭐ 重点 1:性能监控脚本 perf_monitor.py(实时图表)

手工测试/播放时实时监控设备 CPU / GPU / 内存使用率 + 前台 APP 的 CPU%:

- **脚本**:`scripts/perf_monitor.py`,平台契约全套(`--device` / `--params` / `--dump-params` / SIGBREAK 中断 / ASCII stdout / 报告落 `reports/stress-test/perf/`)
- **可配参数**:`interval_sec`(采样间隔,默认 2.0s,min 0.5)、`duration_sec`(默认 0=手动停止)、`track_foreground`(是否跟踪前台 APP)
- **数据源(MT9676 真机探明,2026-08-25)**:
  - CPU%:`/proc/stat` 差值(免 root);mem%:`/proc/meminfo`(免 root)
  - GPU%:`/sys/kernel/debug/mali0/dvfs_utilization` busy_time/idle_time 差值(**需 root**,`su 0 <cmd>`)
  - GPU 频率:`mali0/gpu_clock`(附带字段);前台 APP CPU%:`dumpsys window` 取包名 + `top` 按包 grep 解析
  - 每采样**一次**复合 `adb shell`(list-arg 子进程,`;`/`|` 直达设备端),内部分段解析
- **采样协议 `PERF|{...}`**:脚本每采样打一行 JSON(meta + sample 两型),stdout 经现有 WS 原样流转,**服务端零改动**

### ⭐ 重点 2:前端实时图表(ECharts 本地 vendor)

- **ECharts 5.5.1 本地化**:`static/vendor/echarts.min.js`(pin 版本、~1MB),零外部依赖
- **图表挂在日志面板**:`#log-info` 与日志终端之间新增 200px `#perf-chart`,绑定 `state.currentTaskId`
- **不残留**:切换任务时 `syncPerfChart()` dispose + 隐藏 / 重建,图表数据按任务独立缓冲(`perfSamples[taskId]`),WS 重放重建,刷新后从重放恢复
- **一条曲线 per 指标**:CPU 蓝 / GPU 绿 / MEM 黄 / 前台APP 红(可按 meta.sources 自动隐藏 GPU / 前台系列);单 Y 轴 0-100%(t 为秒),dataZoom 滚轮缩放,长程重放 lttb 降采样
- **一键导出三件**:导出按钮同时下载 **log.txt**(含原始 PERF 行)+ **perf.csv**(可进 Excel)+ **chart.png**(深色底 2x);Chrome/Edge 单次点击多下载,Firefox 可能拦截后两个(文档注明)

### 其他

- 版本号统一至 2.4.0(server.py / healthz / 前端 `?v=` + echarts tag)
- 文档:README 新增"性能监控脚本"章节、CHANGELOG、HANDOFF 补设计决策
- 真机验证:脚本独立跑已通过(6 样本/12s,报告落盘);平台 E2E(图表实时/切任务不残留/三件导出)待跑

---

## v2.3.0 — 2026-08-24

### ⭐ 重点 1:传感器压测脚本接入框架 + 参数新增 select 下拉框类型

- **sensor_reboot_stress.py 接入 PPTP 框架**:原 unittest 脚本重写为平台契约(单文件 `main()`),从 TestCase 风格迁到 scripts/,支持:
  - `--device <serial>` + `--params <json>` + `--dump-params`(自描述参数,前端自动弹窗)
  - SIGBREAK → KeyboardInterrupt(平台"中断"按钮可正常打断并输出已完成汇总)
  - ASCII-only stdout;报告落盘 `reports/stress-test/sensor/reboot_<sensor>_<dev>_<ts>.json`(已 gitignore)
  - 保留原 su 适配链:本设备"裸 su"(`su -c` 报 `invalid uid/gid -c`),读取依次尝试直读 / su -c / su 0 / **stdin 交互式 su** / PTY 兜底
- **params 新增 `select` 类型(下拉框)**:`type: "select"` + `choices` 列表(字符串或 `{value,label}`),前端自动渲染 `<select>`,saveParams 选择器兼容 input+select
- **可配参数**:`sensor`(下拉框选 gsensor / tof)、`iterations`、`reboot_timeout`(设备上线超时);`sensor_window` / `poll_count` / `poll_interval` / `pass_threshold` 为**硬编码常量**(按用户要求不做成前端参数,改脚本顶部)
- 判定:成功率 ≥ PASS_THRESHOLD(硬编码 98%)判 PASS;中断时打印汇总不做判定;退出码 0 = PASS / 手动结束,非 0 = FAIL

### ⭐ 重点 2:APP 冷热启动压测脚本(app_launch_stress)

- **新增 `app_launch_stress.py`**:测量目标 APP 冷/热启动耗时,`mode` 下拉框 `cold` / `hot` **分开跑**:
  - **冷启动**:每轮先 `am force-stop` + `pidof` 确认后台进程已死,再 `am start -W` 测冷启动时间
  - **热启动**:开始前确保 APP 进程存活(若死了先冷拉起一次不计入样本),每轮按 HOME 退后台后再拉起,测回到前台时间
- **双指标交叉验证**:主指标 = `am start -W` 的 `TotalTime`(ms);另清空抓 logcat `Displayed` 做比对(冷启动必有;热启动窗口不重建通常缺失,缺失时以 `LaunchState` 语义校验为准);`LaunchState`(Android 12+)与所选模式一致性校验,冷启动拿到 HOT(进程没杀干净)会标异常轮
- **被测 APP 硬编码在脚本顶部 `APP_PRESETS` 列表**(按项目决策不做成前端参数):**"要跑就三个一起跑"**,内置 Netflix / Prime Video / YouTube TV 三个(包名 + 启动 Activity 来自用户真机 `dumpsys window` 抓取),按顺序每个 APP 各跑 `iterations` 轮
- **可配参数**:`mode`(cold/hot 下拉框)、`iterations`(**每 APP** 循环次数)、`settle_sec`(启动后停留)、`gap_sec`(启动前等待)、`launch_timeout`、`p95_threshold_ms`(判定阈值,0=不判定)
- 判定:每个 APP 各自 p95 ≤ `p95_threshold_ms` 判该 APP PASS,**整体全过才 PASS**;APP 无法解析启动 Activity 也判 FAIL;中断打印已完成汇总不做判定;退出码 0 = PASS / 手动结束,非 0 = FAIL
- 报告落盘 `reports/stress-test/app-launch/app_launch_<mode>_<label>_<dev>_<ts>.json`(**每个 APP 一份**,已 gitignore)
- **真机冒烟修复 3 处**:
  - **component 全限定**:`am start -n 包名/.Activity` 的 `.` 简写仅当 Activity 在包名命名空间内才正确;Prime Video(`com.amazon.ignition.IgnitionActivity`)和 YouTube(启动入口是 **ShellActivity**,`MainActivity` 未导出)都不在,导致"Activity class does not exist / SecurityException"打不开。改为脚本内拼 `package/full.Class` 全限定,`APP_PRESETS.activity` 存**全限定类名**(不带前导点)
  - **Displayed 解析**:`+592ms`(纯毫秒)被 regex 误读为 592 秒 → 计成 592000ms,交叉验证数字失真;已修复为 `+Ns+MSms` 与 `+MSms` 两态分别换算
  - **语义校验升级为硬校验**:`LaunchState` 与所选模式矛盾(冷启动拿 HOT / 热启动拿 COLD)从"打 note"升级为**该轮判 NG 不计样本**;`TotalTime: 0`(intent 投给已置顶实例,非真实启动)也判 NG;系统不报状态时回退进程级校验

### 其他

- 版本号统一至 2.3.0(server.py / healthz / 前端 `?v=`)
- 文档:README(新增"传感器压测脚本"+"APP 冷热启动压测脚本"章节 + select 类型说明)、HANDOFF 更新
- 真机验证:待跑(脚本已就绪,需连真机确认读取方式与 PASS 判定;app-launch 三个 APP 包名/Activity 已从真机抓取填入)

---

## v2.2.0 — 2026-08-24

### ⭐ 重点 1:脚本参数前端可配置

脚本用模块级 `PARAMS` 列表**自描述**可配置项,前端自动识别并在脚本卡上弹出配置窗口,后端 / 前端 / 平台代码一行不用改:

- **脚本契约扩展**:可选定义 `PARAMS`(字段:`name` / `label` / `type`(`int`/`float`/`bool`/`str`)/ `default` / `min` / `max`)+ 支持 `--dump-params` 打印 schema
- **后端**:新增 `GET /api/scripts/{name}/params`,跑 `python -u 脚本.py --dump-params` 读取,按 `(name, mtime)` 缓存;`_list_scripts()` 源嗅探 `has_params` 标志
- **前端**:点击带参数配置的脚本卡 → 按 schema 渲染表单弹窗;配置值按 **设备 × 脚本** 独立存 localStorage(`state.deviceParams`),互不干扰、跨刷新保留;跑任务时合并进 `--params` 透传
- 入口交互与 ir_runner 序列选择一致(点击卡片配置);ir_runner 本身未改造,维持默认无限

### ⭐ 重点 2:WiFi 压测脚本(三个独立脚本)

| 脚本 | 测什么 | 可配参数 |
|---|---|---|
| `wifi_onoff_stress.py`(新增) | WiFi 开关循环 + wpa_cli 扫描统计 | `iterations` `off_sec` `on_sec` `scan_sec` `count_threshold` `use_su` |
| `wifi_reboot_stress.py`(补参数) | adb 重启循环 + WiFi 重连验证 | `iterations` `wait_sec` `wifi_settle_sec` `back_online_timeout` |
| `wifi_switch_stress.py`(新增) | 多网络循环切换(预置列表) | `cycles` `connect_wait_sec` `switch_gap_sec` `use_su` |

- **su 适配**:本设备上 wpa_cli 扫描和 `cmd wifi connect-network` 都需 root,统一用 AOSP 风格 `su 0 <cmd>`(`su -c` 在本设备报 `invalid uid/gid '-c'`);`use_su` 可关
- **预置列表非 param**:wifi_switch 的 `WIFI_NETWORKS` 硬编码在脚本内(按需求不做成前端参数)
- 报告 JSON 落盘 `reports/stress-test/wifi/`(已 gitignore);退出码 0 = PASS
- 真机验证:wifi_onoff 1 轮全 PASS;wifi_switch 1 轮 3/4 PASS(第 4 个 SSID 拼写 `-`/`_` 差异已修正)

### 其他

- 版本号统一至 2.2.0(server.py / healthz / 前端 `?v=`)
- 修复/清理:前端参数表单避免 `const` 重复赋值;`reports/` 加入 .gitignore
- 文档:README / HANDOFF 更新(params 功能 + WiFi 脚本 + 行号)

---

## v2.1.0 — 2026-08-17

> 分支 `feat/input-key-injection` · 验证通过后合入主线

### ⭐ 重点:导入 KEYCODE 上层注入按键

- **新增上层注入路径**:`KEYCODE_*` 按键走 `adb shell input keyevent`,**user 版固件无需 userdebug** 即可使用(此前 sendevent 在 user 版无权限)
- **厂商遥控按键映射表**(23 个):`KEYCODE_BI` / `KEYCODE_IP` / `KEYCODE_HDMI` 等 → 对应 Android keycode 名,设备端解析
- **安卓原生标准键**(26 个):`KEYCODE_POWER`(电源)、`KEYCODE_HOME`、`KEYCODE_DPAD_*`、音量、媒体播放控制 等
- 新旧两族按键按名字前缀**自动分发**:`KEY_*` → sendevent(需 userdebug/root),`KEYCODE_*` → input keyevent(user 版可用);未映射的 `KEYCODE_*` 原样透传
- 按键对照表 `ir_sequences/KEY_REFERENCE.md` 扩充为**三张表**(sendevent / 厂商上层注入 / 安卓原生)

### 已知限制

- **KEYCODE_* 长按暂无效**:ini 写 `LongXXXX` 不报错,但命令静默失败、不会真正注入按键(设备 `input` 命令无 `keydown`/`keyup` 子命令;Android 12+ 的 `--longpress` 暂未采用)。仅短按有效
- 前端 IR 序列弹窗已加【KEYCODE 长按暂无实际作用】提示

---

## 此前版本 — pre-input-keycode @ 96b5430

> ⚠️ **此版本尚未导入 KEYCODE_***:仅支持 `KEY_*` sendevent 注入,**user 版固件无法使用**(需 userdebug/root)。

- 新增 KEY_MEMO 按键(码值 396 / 0x18c)
- 修复 ir_runner 循环间延时未按 ini 的 `delay_ms` 执行(此前写死 1.0s)
- KEYCODE 导入前的主线 checkpoint 基线
