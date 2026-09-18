# perf_monitor v2 — 总体架构设计

> 状态:**已上线,持续修订中**。实现路线 P0–P7 已全部发布,当前项目版本 **v2.11.2**,上线后的修订见 **§12.8 / §12.9**。
> 除注明日期者外,数字均为 2026-09-14 在 MT9676(`B0403374A2A508001F00`)上的实测值。
> ⚠ **§12 是定稿规格**。§3–§5、§7–§9、§11 与 §12.7-原始 已整体删除(原文见 git 历史),**编号不连续是有意的,不是缺页**;本文只保留 §0–§2、§6、§10 与 §12。

## 0. 结论摘要

| | 现状 | v2 | 依据 |
|---|---|---|---|
| 单个采样点墙钟 | **876.5 ms** | FAST **134.0** / FAST+MED **243.1** / FULL **408.7** ms | **直接实测 v2 复合命令**(2026-09-14,7 次取中位),非分项相加 |
| 2 秒间隔下占空比 | **43.3 %** | **9.07 %** | 按 `MED=T*ceil(5/T)`、`SLOW=T*ceil(30/T)` 的 tick 混合算 |
| 加速比 | — | **4.8×** | 同上 |
| 判定 | **完全没有** | 增量证据 + 收尾判定 + 中途事件 | |
| 归档产物 | 日志 + report.json | 日志 + `samples.csv` + `report.json` + `events.csv` | |

> **口径演进(别再用旧数)**:最初 6.4%/6.7× 是**分项相加推导**,口径不成立;改测**旧命令形状**得 7.15%/6.1×;最后按**定稿的 v2 命令形状**重测得 **9.07%/4.8×**。早期的 82.4 / 194.3 与"设备 CPU 255→30.9 ms/s"同样作废。**总量只认「要实现的那个命令形状」的直接实测,禁止由分项推导。**

三句话:① **采样要分级** —— CPU 每拍,内存/GPU 每 6 s,前台应用每 30 s。② **判定要分层** —— 事后无法恢复的证据(null 成因、增量基线、事件时刻)必须当场累积,其余(趋势、分位数)收尾算更准。③ **算可以随时算,看只在末尾看** —— 判定只在收尾出一份,中途只输出**事件**。

## 1. 现状定位(实测)

当前 [perf_monitor.py](../scripts/perf_monitor.py) 每拍发**一条复合 adb 命令**,实测整条 **866.5 ms**(孤立分项相加得不到它 —— 每条单读各含 110–140 ms 的 adb 固定开销,相加约 1297 ms)。两个结论:**`top -n 1 -b` 占 67% 墙钟**(581.6 ms);**`interval_sec` 的 `min=0.5` 是物理上达不到的谎**(单拍 863 ms > 500 ms,`remaining` 恒负 → 背靠背满速跑,UI 从不告诉用户)。

三条硬约束:全系统 `dumpsys meminfo` = **7433–7546 ms**(**禁止进入采样路径**)、`dumpsys meminfo <pid>` = 426–604 ms(只能 30 s)、`cat /proc/<pid>/smaps_rollup` = **Permission denied**(SELinux,**拿不到 PSS**)。

## 2. 架构:三层职责

```
┌─ 脚本层 (scripts/perf_monitor.py) ───────────────────────────────┐
│  采样:分级 tick,每拍一条复合 adb 命令                            │
│  证据:增量累积器(事后不可恢复的信息)                              │
│  判定:纯函数 judge(evidence) → verdict dict                      │
│  输出:每拍一行 PERF|sample + 事件一行 PERF|event + 收尾判定块      │
└──────────────────────────────────────────────────────────────────┘
        │ stdout(平台逐字转发,server.py 零改动)
        ▼
┌─ 前端层 (static/app.js) ─────────────────────────────────────────┐
│  handlePerfLine 新增 sample(定宽渲染)/ event(一行事件)分支        │
│  不做任何判定,不新增 badge/状态条                                 │
└──────────────────────────────────────────────────────────────────┘
        │ 收尾
        ▼
┌─ 归档层 (archive/<模块>/<时间>_<脚本>_<设备>/) ──────────────────┐
│  stdout.log / logcat.log / serial.log(ECharts 图 chart.png)      │
│  samples.csv / report.json(含 verdict)/ events.csv               │
└──────────────────────────────────────────────────────────────────┘
```

**铁律**:脚本 stdout 全 ASCII、依赖只用标准库、脚本能独立 CLI 跑、日志采集归平台。**显示边界(2026-09-14 用户拍板)**:离线区间、采集源禁用、pid 变化这类**事实**允许作为事件行进 **stdout**("stdout 没那么局促");**其它地方照旧禁止** —— 不加横幅 / toast / 状态条 / 离线专用面板,设备卡的「临时离线」合成卡仍是离线状态的唯一 UI 表达。这是对 v2.7.2 决策 #0.3 的**窄口径覆盖**,不是撤销。

> **§3 采样层 / §4 判定层 / §5 输出层 已删除** —— 内容已被 §12.1 / §12.2 / §12.3 / §12.4 取代,原文见 git 历史。

## 6. 归档产物

用户已确认:**归档不受文件类型约束**,唯一要求是"归档在一起 + 统一命名"。平台侧今天的三个实现限制:`_sniff_report_path`(server.py)的候选**必须以 `.json` 结尾**;`_siblings`(server.py)走 `REPORTS_DIR.rglob("*.json")`;`ARCHIVE_MEMBER`(server.py)只认三个日志名。

**方案(2026-09-14 定稿):扁平文件,设备名进文件名。**

```
reports/stress-test/perf/
    ├── perf_<dev_short>_<ts>.json        报告(现有形状,加 verdict 对象)
    ├── perf_<dev_short>_<ts>.html        同一份 payload 的中文渲染(v2.9.0 起)
    ├── perf_<dev_short>_<ts>.samples.csv 全分辨率原始序列
    └── perf_<dev_short>_<ts>.events.csv  事件流
```

- **三个文件同前缀同时间戳** —— "归档在一起 + 统一命名"靠命名约定保证,不靠目录;平台侧只需把认领规则从 `*.json` 扩到这三个后缀,**mtime 窗口兜底与 `_CLAIMED_REPORTS` 原样复用**;硬停时脚本没打完声明行,兜底扫描照样能按 `dev_short` + mtime + 后缀一起收。
- **不用"一个 run 一个目录"**:它落不了地 —— `_sniff_report_path` 要求路径以 `.json` 结尾、`_siblings` 按**文件名**匹配设备名、`_archive_task` 对报告走 `shutil.copy2`(传目录抛异常)、`_safe()` 与 `_CLAIMED_REPORTS` 都只有文件版,要落地得新增三套目录级机制。**也不把 glob 放开成任意扩展名**:那会把三重保护(`_CLAIMED_REPORTS` + mtime 窗口 + 设备名前缀)变成零保护,同设备背靠背跑时前一个 run 的 CSV 会被后一个认领(v2.7.1 修过的那类 bug)。**白名单后缀 + 同前缀成组**才是安全边界。
- **`report.json` 必须移除全分辨率 `samples` 数组**:实测 336 B/条(带 `_cpu_prev`/`_gpu_prev` 且 `indent=2` 展开)→ 外推 61 MB / 序列化 2.4 s,而**收尾写盘期间被二次 force-stop 就整份丢**;换成统计 + 事件 + 判读并改 compact 序列化(全分辨率已由平台收在 `stdout.log` 里)。

> **§7 分阶段路线图 已删除** —— 六个阶段 P0–P6 已全部发布,由 §12.6 的阶段边界表取代,原文见 git 历史。

> **§8 已拍板(2026-09-14)的五条裁决已删除** —— 议题与结论已登记到 [DECISIONS.md](DECISIONS.md),原文见 git 历史。

> **§9 明确不做 的清单已删除** —— 各项(常驻 shell、全系统 meminfo、每拍 `top`、PSS、自采 logcat、FPS、pandas/numpy、自动泛化 `PERF_SCRIPT_KINDS`、判定搬去服务端、开跑前查磁盘空间)同样登记到 [DECISIONS.md](DECISIONS.md),原文见 git 历史。

## 10. 风险与未验证

**完整清单由 [TODO.md](../TODO.md) §6 拥有,本节不重复。** 三条最要紧的:① **全部数字是 USB + 设备负载下测的**(load avg ~21/4 核、399–401 任务),**adb over WiFi 完全没测**,`--probe` 必须保留现场重标定入口,不得把绝对值写成承诺;② **判定阈值未标定**(§12.7 第 8 条),在拿到一次健康长跑基线前只是**设计假设而非实测值**;③ **硬停无最终判定**,force-stop 时只有 ≤120 s 前的 `partial` 判读 —— 已知边界,不是 bug。

> **§11 复核发现 已删除** —— 其结论均已落进 §12(归档扁平化 → §6/§12.5;离线进 stdout → §2;`up`/`fg_pid` 数据源 → §12.1;coverage 循环定义与 `not_due` 互斥 → §12.2;两侧超时不自治 → §12.3;管线帽 → §12.5;P2 不自足 → §12.6),原文见 git 历史。

## 12. 定稿规格(权威)

> 原文标题曾是「与 §3–§5 冲突时以本节为准」。§3–§5 已删除,**本节是本文唯一的规格来源**。

### 12.1 调度器

**时间基一律整数毫秒,调度算术里禁止出现 float 秒。** 浮点 MED 在 T=1.1/1.9/2.2/10.7 上会产生 `ceil(5/1e-15)` 之类爆炸值;整数化后对 T ∈ [1.0, 60.0] 全扫(步长 0.1)无病态。

```
now_ms  = time.monotonic_ns() // 1_000_000
T_ms    = max(1000, min(60000, int(round(interval_sec * 1000))))   # 落整后立即丢弃浮点值
origin_ms = 第一个 tick 的 now_ms,此后永不变
nominal_ms[k] = origin_ms + k * T_ms                                # k 单调递增
```

**层周期(整数推导)与段表:**

```
ticks_per[FAST] = 1
ticks_per[MED]  = max(1, ceil(5000 / T_ms))
ticks_per[SLOW] = ticks_per[MED] * max(1, ceil(30000 / (ticks_per[MED] * T_ms)))
due(tier, k)    = (k % ticks_per[tier]) == 0

SECTIONS = [("STAT","FAST","cpu"), ("PROCS","FAST","procs"), ("UP","FAST","uptime"),
            ("MEM","MED","mem"),   ("FOCUS","SLOW","fg"),    ("FGPKG","SLOW","fg"),
            ("PIDSTAT","SLOW","fg"), ("GPU","MED","gpu")]
tick 后处理顺序固定:parse → up → rollover/reboot 守卫 → deltas → fg    （不得改序）
```

| T_ms | MED | SLOW |
|---|---|---|
| 1000 | 每 5 tick = 5.0 s | 每 30 tick = 30.0 s |
| **2000** | **每 3 tick = 6.0 s** | **每 15 tick = 30.0 s** |
| 6000 | 每 1 tick = 6.0 s | 每 5 tick = 30.0 s |
| 30000 | 每 1 tick = 30.0 s | 每 1 tick = 30.0 s |

- 周期用**嵌套式** `SLOW = MED * ceil(30/MED)`,不用 `T*ceil(30/T)` —— 后者在小数 T 上不保证嵌套,会出现"SLOW 到期但 MED 未到期"的畸形拍。T=6000 时 MED 每 tick 到期(6.0 s > 5.0 s 窗口)→ **MED 与 FAST 同层,线协议必须允许同一 tick 内两段并存**。
- **`UP` 从早期的 MED 提到 FAST**:reboot 守卫和它守卫的计数器**必须落在同一 tick**,否则重启后第一个 tick 拿旧 uptime 比新计数器,rollover 误报。`up`(`cat /proc/uptime` 第一列)是判断设备重启过的**最便宜手段**;重启会清零 `/proc/stat` 与 GPU 计数器,而 `cpu%`/`gpu%` 是差分算出来的,没有它读数会**悄悄偏掉**。**`fg_pid` 同理必须有**:`fg_pkg` 消失**不等于应用死了**(导航/屏保/OSD 都会抢焦点),唯一可靠证据是进程号消失;它几乎免费(`fg_cpu` 本来就要 `cat /proc/<pid>/stat`)。
- **每 tick 独立起一个 `adb.exe`。常驻 `adb shell` 会话这个显然的优化已实测否决**(理论上限只省 56.1 ms = 863 ms 的 **6.5%**,A/B 实测常驻侧**中位数反而慢 8.1%**,且把"丢一拍"升级成"丢一段会话";详见 [DECISIONS.md](DECISIONS.md))。**每拍多付 ~56 ms 买的是隔离性。**
- tick 前生成一次性 nonce `os.urandom(3).hex()`(6 位小写十六进制);每段前打 `@@<SEC>:<nonce>`,**命令尾必须补 `@@DONE:<nonce>`**;`split_sections` 只认「`@@` + 段名 + `:` + 本 tick nonce」的行,nonce 不匹配整段丢弃并计一次 `misparse`;命令以**参数列表**交给 `subprocess`,绝不 `shell=True`。**为什么**:`@@GPU` 标记在 `su` 读之前就已 echo 回来,su 段被截断时没有尾标记就**无法区分「少了一段」和「设备没有 GPU 节点」**;设备自发输出可能让某行以 `@@` 开头,nonce 让它无法冒充。

```
# T=2000 的全到期 tick(k=15),nonce=a17f3c 为例:
adb -s <serial> shell "echo @@STAT:a17f3c; cat /proc/stat; set -- /proc/[0-9]*; echo @@PROCS:a17f3c; echo $#; echo @@UP:a17f3c; cat /proc/uptime; echo @@MEM:a17f3c; cat /proc/meminfo; echo @@FOCUS:a17f3c; dumpsys window 2>/dev/null | grep mFocusedApp; echo @@FGPKG:a17f3c; pidof <last_pkg>; echo @@PIDSTAT:a17f3c; for x in $(pidof <last_pkg>); do cat /proc/$x/stat; done; echo @@GPU:a17f3c; timeout -k 2 <G> su 0 cat /sys/kernel/debug/mali0/dvfs_utilization /sys/kernel/debug/mali0/gpu_clock; echo @@DONE:a17f3c"

# T=2000 的 FAST-only tick(k=14):
adb -s <serial> shell "echo @@STAT:a17f3c; cat /proc/stat; set -- /proc/[0-9]*; echo @@PROCS:a17f3c; echo $#; echo @@UP:a17f3c; cat /proc/uptime; echo @@DONE:a17f3c"
```

> **两处易错**:① `@@PROCS` 是**进程数**(`set -- /proc/[0-9]*; echo $#`,用 shell glob 内建避免 fork;对比 `ls /proc | grep -c` 贵 20 倍),**不是** `/proc/<pid>/stat` —— 后者属于 SLOW 层的 `@@PIDSTAT`。② `@@GPU` 段**必须一次 `su 0 cat` 读两个文件**(`dvfs_utilization` **和** `gpu_clock`):只读前者会让 `gpu_clk` 永远为空,分两次读要多付一次 su 冷启动(实测 251.6 ms vs 129.9 ms);`parse_gpu_clk` 必须**按行取**独立整数行,不能用 `re.search(r"(\d+)")`(会读到 `busy_time` 的数字)。

**GPU 降级(PITFALLS.md #14:视频播放时 GPU ≈ 0 是正确的 —— 绝不许建「GPU 卡在 0」规则):**

- 连续 **3 个到期 tick** 失败(任何原因)→ 置 `d`、发一次 `src_degraded`、此后不再发 `@@GPU` 标记、每 300 s 重探一次;启动探测失败同样每 300 s 重探,恢复时可再发一次 meta 更新。
- **`meta.sources` 只许增不许减**(否则前端重建 series,把前段好数据抹掉);**降级判定只看「读不到」,绝不看「读到的值是 0」**。

**burst(手动补采):** arm 时钳位 `counter[SLOW] = min(counter[SLOW], ticks_per[MED])`(禁止把 SLOW 相位跳到 MED 之前);预算 `max(1, duration_sec / 300)`,上限 12 次/小时。T ≥ 30 是 no-op;T=6 抬 +0.26 pp,T=1 最多 +0.33 pp —— **此代价必须如实写进 UI 提示**。

**参数钳位:** `interval_sec` float,default 2.0,**min 1.0**,max 60;脚本内再钳一次 `max(1.0, min(60.0, v))`(server.py 目前不校验 min/max,只缓存 schema)。

### 12.1.1 锁定的成本与占空比(2026-09-14 实测,7 次取中位)

**这是要实现的命令形状的直接实测**(MT9676 / `B0403374A2A508001F00` / idle):

| 变体 | 中位墙钟 | min | max | 输出字节 |
|---|---|---|---|---|
| **今天(每拍)** | **876.5 ms** | 848.9 | 914.3 | 2793 |
| FAST (v2) | **134.0 ms** | 131.0 | 141.0 | 780 |
| FAST+MED (v2) | **243.1 ms** | 234.6 | 255.6 | 2170 |
| FULL (v2) | **408.7 ms** | 388.3 | 418.5 | 2539 |

| T | MED | SLOW | tick 混合 | 占空比 | |
|---|---|---|---|---|---|
| 1.0 s | 5 s | 30 s | 24 FAST + 5 MED + 1 FULL | **16.14%** | FULL 单拍已占 1 s 的 41% |
| **2.0 s** | **6 s** | **30 s** | 10 FAST + 4 MED + 1 FULL | **9.07%** | ← 推荐 |
| 6.0 s | 6 s | 30 s | 0 FAST + 4 MED + 1 FULL | 4.60% | 分层退化(MED 与 FAST 同层) |
| 30 s | 30 s | 30 s | 0 + 0 + 1 FULL | 1.36% | 三层塌成一层 |

> 比早期旧数贵是预期的:v2 命令多了 `@@UP` 段、`@@FGPKG` 段、`@@DONE` 尾标记且 GPU 一次 su 读两个文件,FULL 从 305.6 → 408.7 ms(+34%);**T=1s 档要留意**(FULL 单拍占 1 s 的一半),长跑建议 T=2s。**首次真机实跑同步验证**:占空比 9.06%(设计预测 9.07%)、自测 adb 往返 **55–62 ms** —— 比设计文档里的 110–140 ms 低得多(那是另一条链路),所以往返基线改为**启动自测**而非硬编码常数。

### 12.2 证据模型

**每 tick 的指标级状态(9 位,顺序固定):**

```
METRIC_ORDER = [cpu, procs, up, mem, gpu, gpu_clk, fg_pkg, fg_pid, fg_cpu]
  f = fresh     本 tick 到期且解析成功          x = failed    到期但解析失败
  h = held      ZOH 保持(未到期但有历史值)     a = absent    设备没有这个节点
  n = not_due   未到期且无历史值                d = disabled  被降级策略关闭
  b = baseline  首次读,只立基线不出 delta
```

**tick 级结果:** `ok`(所有到期段 f/h/b)/ `partial`(有 x 或 a 但至少一段成功)/ `timeout`(PC 预算耗尽)/ `offline`(rc≠0 且 stderr 含 `device offline`/`no devices`)/ `error`(其它非零退出)

**线协议字段:** `st = "<res>:<9 chars>"`,正则 `^(ok|partial|timeout|offline|error):[fhnbxad]{9}$`

```
"ok:fffhhhhhh"       T=2000 普通 tick
"ok:fffffffff"       T=2000 第 15 tick(全到期)
"offline:xxxhhhhhh"
"timeout:nnnnhhnnn"
```

> **为什么是 9 字符 7 态**(否决 4 态/6 态):4 态把 `absent`/`disabled`/`failed` 混成一类且没有 `baseline`,下游分不出「设备没这个节点」和「这次读失败」—— 两者触发完全不同的告警。**ZOH 要点**:低频指标读不到时**不写 null**,而是带上一拍的值(`h`)—— 前端 `showSymbol:false` + `connectNulls:false` 会让稀疏序列**整条消失**(连无条件 series 的黄线一起),归档 `chart.png` 走同一套代码就会只留图例和刻度;代价是分位数必须在**真实读数**上算,所以报告里每个指标要打印它自己的 `n`。

**覆盖率的分母:**

```
expected = max(1, floor(run_wall_ms / T_ms))        # 分母 = 请求周期,不做任何替换
good     = 本 run 中 res ∈ {ok, partial} 的 tick 数
coverage = good / expected
cadence_ratio = p50(dt_ms) / T_ms                   # 门限 <= 1.5
interval_below_tick_cost = (T_ms < p50(cost_ms))    # cap-only 标志,不参与替换
```

> **为什么必须改**:旧规则「分母用实测中位周期」会把「采集跟不上」本身掩盖掉 —— 周期被拉长,分母跟着变大,覆盖率反而更好看,一次比请求慢 12 倍的跑仍能拿 OK。

**samples.csv**(唯一原始真相,append-only):

```
reports/stress-test/perf/perf_<dev_short>_<ts>.samples.csv
# PPTP perf samples v2 | script=perf_monitor.py | judge_version=v1
# device=<serial>
# config=interval_ms=2000,duration_sec=0
# tiers=FAST:1,MED:3,SLOW:15
# metrics=cpu,procs,up,mem,gpu,gpu_clk,fg_pkg,fg_pid,fg_cpu
# sources=cpu=stat,mem=meminfo,gpu=mali/dvfs,fg=top
# counters_t0=<iso8601>
t_sec, clock_ms, res, k, cost_ms, flush_ms, late_ms, gap_ms,
cpu, cpu_st, procs, procs_st, up, up_st, mem, mem_st,
gpu, gpu_st, gpu_clk, gpu_clk_st, fg_pkg, fg_pkg_st, fg_pid, fg_pid_st, fg_cpu, fg_cpu_st,
causes, events                                       ← 28 列,顺序固定
# end_of_run=<iso8601> ticks=N ticks_ok=M run_wall_s=W status=completed|interrupted|csv_degraded
```

- 启动时 `open(..., "a", newline="")` **持住句柄**,每 tick 写完 `flush()`,运行期不 close。
- **撕裂行判定**:末行必须同时满足「含换行符 + 列数 = 28 + `t_sec` 严格单调」,否则整行丢弃;`end_of_run` 行**必须写**,缺行即文件级 partial 标记。

**events.csv**(同目录同前缀):列 `t_sec, clock_ms, type, reason, detail`

- P2 事件集:`timeout, offline_start, offline_end, cadence, tick_gap, misparse, src_degraded, src_recovered`;P3 增补:`reboot, rollover, fg_change, fg_lost, app_gone, pkg_mismatch, budget_exhausted, leak_suspect`。
- **限流**:key = `(kind, dev_short)`,固定 60 s 窗口;**只有真正打印了才登记**(禁止「先登记再丢弃」,否则真事件会被限流吃掉)。
- **两套时间,各有各的用处(v2.11.2,2026-09-18)**:每条事件同时带 `t_sec`(运行节拍,第几秒)与 `clock_ms`(epoch 毫秒);`reports/*.html` 第五节「链路事件」**两列都渲染**,墙钟 `发生时刻` 在前、节拍 `相对时刻` 在后,悬停给出完整 `YYYY-MM-DD HH:MM:SS`。用户原话「我想要能看到问题发生时的时间如 18:39:12 等,这个才是重要的元素」—— 节拍只说"在这趟跑的哪个位置",墙钟才能和另一台设备的日志、一张工单对上。**`clock_ms` 本来就是渲染层漏掉的一个字段,不是新采的数据**(payload / CSV / 判定 / 限流全都没动);缺失或非法时钟渲染成 `-`,绝不抛。

**report.json**(只存证据,不存原始样本):

- 结构 `{"verdict": {...}, "evidence": {...}, ...}`;`judge_version` 与阈值必须写进报告,否则老归档无法用新脚本复算。
- 不变量(可 assert 且必须写进 selftest):**`judge(report["evidence"]) == report["verdict"]`**;二级可重演:`verdict ← evidence` 精确一致、`evidence ← samples.csv` 机械可推。
- **不放 samples 数组** —— 旧版塞全量 samples,与「证据 O(1) 有界」自相矛盾(实测 309 B/条)。

**9 条门(id 固定,不许改号):**

| # | 门 | 判据 |
|---|---|---|
| 1 | DATA | `samples.csv` 存在且 `ticks_ok > 0` 且 `status != csv_degraded` |
| 2 | COVERAGE | `coverage >= 0.8`(0.5–0.8 降为 WARN) |
| 3 | CADENCE | `cadence_ratio <= 1.5` |
| 4 | TIMEOUT | timeout 次数 / expected ≤ 0.05 |
| 5 | ENVELOPE | `T_ms` 与 duration 落在支持包络内 |
| 6 | REBOOT | 无 reboot 事件;有则**切段重算,不得跨 reboot 聚合** |
| 7 | MEMORY | 无显著单调上升且无 OOM 前兆 |
| 8 | CPU | 有 fg 段时 `fg_cpu` 有有效样本 |
| 9 | APP | `fg_pkg` 未失联(`app_gone`/`fg_lost` 未触发);v2.9.0 起为三出口(见 §12.8 第 3b 条) |

合成:`result = INCONCLUSIVE > FAIL > WARN > OK`,并附 `capped_by` 列表说明是哪几个门封的顶。**`INCONCLUSIVE ≠ PASS`**:`n < 5` 或 `span < 30 s`、`coverage < 0.5`、全指标无有效读数 → `INCONCLUSIVE`;`coverage < 0.8` 或 `partial=true` → 最高只能 `WARN`;中途的结论性告警必须带数据充分性(要能输出 `LEAK? (early, insufficient data)` 这种**不确定态**)。

**R² 不作为门控**(实测推翻):4000 次蒙特卡洛下纯随机游走的 R² 中位数 **0.45**,越过 0.60 的高达 **37 %**、越过 0.75 达 **21 %**,而真泄漏(噪声量级相当)可低至 **0.38** —— 它度量的是"这串数有多光滑",**不是"有没有泄漏"**,只作诊断字段;要用必须先真机标定。同理**明确不做**的判据:**GPU 冻结/卡 0**(PITFALLS.md #14);**以 `fg_pkg` 消失判应用死亡**(必须有 pid 级证据);**以 `comm` 字段做进程匹配**(内核按 `TASK_COMM_LEN-1=15` 硬截断,实测读到 `3127 (.apps.tv.dreamx)`,永远匹配不上真实包名 → `fg_cpu` 全程 null → **给一次健康的 8 h 跑打出假红**;只信 `pidof`);**Mann-Kendall 趋势检验**(内存序列高度自相关,Z 值会被放大成假显著);**Theil-Sen 直接跑原始序列**(O(n²),且自相关与毛刺污染斜率,必须先分块取中位数)。

**相对早期设计的删减**(复核认定冗余或恒等):删 `n_valid`(与 `n_fresh` 恒等);删线协议上的 per-metric cause 字典(降成 CSV 单一 `causes` 列);删线协议上的 `win_s`(可由 `t_now - t_prev_fresh` 推出);**删 1 分钟桶**(samples.csv 已是全分辨率,桶只增加一处可变真相);删「分母用实测中位周期」(它是错的,不是冗余)。

### 12.3 双侧超时

**两级结构:** PC 侧 `B` = 整个 adb 调用的墙钟预算;设备侧 `G` = 命令里 `timeout -k 2 G <cmd>` 的守卫值。

```
硬约束: B >= G + 2.0 + transit + 0.3        transit = adb 固定开销实测 110-140 ms,保守取 0.3 s
       → B >= G + 2.6
默认:   USB 连接 G=3 → B=5.5(留 0.4 s 余量;启动自测的往返 55–62 ms 也落在这个保守取值内)
```

**G 的自适应:**

```
估计窗 = 最近 30 个 tick 中 res ∈ {ok, partial} 的 cost_ms   ← 必须排除 timeout
base   = p90(窗口样本 < 30 时退化为 max)
G_need = ceil(clamp(5.0 * base_ms / 1000, 3.0, 15.0))
阻尼   : 缩小立即生效;变大每 tick 最多 +1 s,且必须连续 3 个 tick 都要求变大才动
```

> **为什么不按 p95 定**:watchdog 的尺寸应按「典型代价」定,不是按「你要抓的那个异常的 p95」定 —— 旧规则会随异常自涨(超时拍计入 p95 → 预算变大 → 下次卡死等更久),把要抓的现象吃掉。**也不从名义 interval 派生**:`max(30, int(interval)+15)` 方向是错的(T 越大预算越松,恰好在最需要快速失败的大周期给最长等待),而 `min(interval*0.8, 10)` 会把"设备很忙"误判成"读不到",收紧预算杀掉的只是 adb 客户端 → **设备侧旧命令继续跑、下一拍立刻再叠一条**,形成"越慢越重叠"的正反馈。B 只从 `cost_ms` 估计窗派生。

**有界子进程(必须修的实现缺陷):**

- 不用 `subprocess.run(timeout=)`,改**显式 `Popen` + `communicate(timeout=B)`**;`TimeoutExpired` → `proc.kill()` → **再做第二次 `communicate(timeout=1.0)`,必须有界**;第二次也超时 → 关管道、不再 wait(CPython 在 kill 之后再调 `communicate` 是**无界**的,设备进 D 态时会挂死采集线程)。
- `adb_shell` 必须返回 `(rc, stdout, stderr)` 三元组(旧缺陷:`adb_capture` 返回 `r.stdout or ""` 且 `except: return ""`,**没有 rc 没有 stderr**,TimeoutExpired 与设备离线无法区分)。
- PC 侧超时**只杀 adb.exe 客户端,杀不掉设备侧命令** —— 设备侧 `timeout -k 2 G` 因此不可省,否则每拍泄漏一个持 debugfs 句柄的 root 进程,8 h 里不断叠加且平台完全看不见(toybox 实测支持 `-k`)。

**段级守卫字面命令(G=3):**

```
timeout -k 2 3 su 0 cat /sys/kernel/debug/mali0/dvfs_utilization
timeout -k 2 3 su 0 cat /sys/kernel/debug/mali0/gpu_clock
```

(`GPU_DVFS_READ`/`GPU_CLK_READ` 旧版只有 `timeout 5`,`-k` 缺失 —— 设备侧 hang 时 5 s 后的 SIGTERM 可能被忽略。)

### 12.4 输出协议

**实时数据行**(P1 已发布 v2.7.6 的定宽格式,新增 `st` 字段后为):

```
[perf] t=    2.0s | cpu= 61.6% | gpu= 35.3% | mem= 67.4% | fg= 39.4% | clk= 552MHz | st=ok:fffhhhhhh | pkg=com.google.android.apps.tv.launcherx
```

每格定宽、数值右对齐,可直接竖读;**缺值也占满自己的列**(否则后面全部左移,竖读就断);`pkg=` 放行尾(长度不定,放中间会推歪后面的列);`null` 渲染成同宽 `-` → 掉线时整行是一排横线,**不会被误读成"GPU 真的 0.0%"**;丢掉重复墙钟(行首 `[HH:MM:SS]` 由平台提供)。

**中途事件(允许,但不是结论):** 事实类(采集源被禁用、pid 变化、设备离线区间、重启、**采样节奏劣化**)立即打;结论类(内存泄漏/阶跃/循环、应用死亡)打但**必须带数据充分性 / pid 级证据**;**最终判定只在收尾**。三条落地纪律:① **前端必须学新行类型** —— `handlePerfLine` 对未知 `type` 走 `return line`,不加分支会把**原始 JSON 直接漏进 console**,写进 `stdout.log` 后每次 WS 重连都重放一遍,**污染永久且反复**;② 限流流程必须是 **收集 → 去重 → 截断 → 只登记留下来的**;③ **事件绝不进 server 的 task 对象**(会进 `tasksRenderKey`,每次轮询整卡重渲染,PITFALLS.md #23)。

**判定块字面行(收尾一次,纯 ASCII,定宽,由 `format_result_lines(verdict)` 渲染):**

```
RESULT : <OK|WARN|FAIL|INCONCLUSIVE>
RUN    : ticks=<N> ok=<M> coverage=<pct>% cadence=<r>
DATA   : samples.csv=<filename> events=<n> judge_version=v1
DEVICE : <serial> (<dev_short>)
MEMORY : <...>
CPU    : <...>
APP    : <...>
EVENTS : timeout=<n> offline=<n> reboot=<n>
report : <path>
```

> **约束**:除最后一行外,**任何行不得出现字面量 `report`**,最后一行必须且仅是 report.json 的路径 —— `_sniff_report_path` 靠「含 ` : ` 且候选以 `.json` 结尾」认路径,多行候选会认错,而且 `_stream_logs` 只捕获**第一个** report 路径。`verdict.metrics[m] = {status, n_fresh, expected, coverage}`;`EVENTS` 一行汇总、详情在报告里(否则看到跑过 3 次告警却不知道是哪三次);v2.9.0 起块尾另有 `  html   = <path>` 一行,用 **`=` 而非 `:`**(理由见 §12.8)。

### 12.5 支持包络

```
interval_sec ∈ [1.0, 60]     duration_sec ∈ [0, 86400]  (0 = 不限)
```

| 帽 | 值 | 何时咬 |
|---|---|---|
| **interval 下界 1.0 s** | 由**物理代价**定 | FULL 复合命令实测 305.6 ms + 每个 adb.exe 的 110–140 ms 固定开销 → 0.5 s 周期在设备上采不出来。旧版 `min=0.5` 是谎言 |
| interval 上界 60 s | 由设计意图定 | T > 60 时 SLOW(30 s 目标)与 MED 塌成同一层,分层失去意义 |
| `PERF_SAMPLE_CAP=86400` | **数据完整性帽** | @1s 恰好 24h(零余量);@2s 48h |
| `REPLAY_TAIL_BYTES=4MB` | **重放帽** | ≈13981 行 v2 定宽行(309 B)→ **@2s 约 7.8h,@1s 约 3.9h**;超过后重放丢头部,`chart.png` 退化成后段缩略图 |
| `LOG_DOM_CAP=1000` | 仅影响 DOM 显示 | @1s 约 16.7 分钟,**不丢数据** |
| `MAX_REPLAY_LINES=50000` | **对 PERF 行是死代码** | 需平均 < 83.9 B/行,而 v2 行实测 309 B,永远先被 4MB 字节帽截断 |

> **一句话**:数据完整的区间是 **T=2s × 48h**,但重放与图表完整只到 **T=2s × 7.8h**(T=1s × 3.9h);**实际推荐工作区间是 T=2s × 8h 以内**。要跑满 24h@1s 需要先决定是否抬 `REPLAY_TAIL_BYTES`(见 §12.7 第 3 条)。

### 12.6 阶段边界

| 阶段 | 交付 | 依赖 |
|---|---|---|
| **P0** 平台侧采集归位 | logcat / 串口采集、离线容忍 | ✅ 已随 v2.7.4/v2.7.5 发布 |
| **P1** 控制台定宽 | `padCell` 定宽列 | ✅ 已随 v2.7.6 发布 |
| **P2** 调度器 + 采样瘦身 + **最小事件路径** + 前端适配 | 整数 `T_ms`/`ticks_per`;SECTIONS 表(`up` 提 FAST);nonce + `@@DONE`;`st` 线协议;G/B 自适应与**有界 Popen**(`adb_shell` 返回三元组);前端 `st` 渲染 + `gap_ms` 断线 | P1 |
| **P3** 累积器 / 报告 / 事件落盘 | samples.csv(28 列 + 注释块 + `end_of_run`)append-only 持句柄;events.csv + 60s 限流;report.json 改为只存 evidence + `os.replace` 原子写 | P2 |
| **P4** judge 与自证 | 纯函数 `judge(evidence)`;`--selftest` 断言 `judge(report["evidence"]) == report["verdict"]` 与 `evidence ← samples.csv` 机械可推 | P3 |
| **P5** 平台归档扩展 | `_sniff_report_path` / `_siblings` 扩到 `*.csv` 且以 `perf_<dev_short>_<ts>.` 前缀配对;`ARCHIVE_MEMBER` 增 samples/events | P4 |
| **P6** 事件呈现策略 | console / 图表 / 归档三处的呈现与文案;区分信息级(`fg_change`)与告警级(`offline_start`/`reboot`);**仍禁止 banner / toast / 状态条** | P5 |
| **P7** 文档与版本同步 | 落到本文;bump 三处版本号;同步 CHANGELOG | P6 |

> **P2 的关键修正**(复核发现):原计划让 P2 发射 timeout 事件,但**事件发射器是 P3、前端分支是 P6** —— P2 单独上线时事件行走 `handlePerfLine` 的未知类型分支 → **原始 JSON 直接进 console 且每次 WS 重连重放**。所以**「最小事件路径」(发射器 + 前端 `st`/event 渲染 + 限流)整体并入 P2**,P2 不是自足的一步。
> **可跑性归位**:脚本必须能独立 CLI 跑(`python -u scripts/perf_monitor.py --device <serial>`);平台自动发现 `scripts/*.py`,零注册;`--probe` / `--dump-params` / `--params` / `--selftest` 四个口子长期保留。

### 12.7 未决项 —— 已由实现方拍板(2026-09-14)

> 用户 2026-09-14:「问我没用,我已经有点看不懂了……你拿一下主意,风险项可以暂时加到 todo 里面」。以下 8 条由实现方按**保守、可反悔**的原则决定,全部记进 [TODO.md](../TODO.md) §6。**任何一条都可以随时推翻** —— 每条都写了"怎么改回来"。

| # | 议题 | 决定 | 理由 | 怎么反悔 |
|---|---|---|---|---|
| 1 | 多进程应用的 `pidof` 取哪个 | **`fg_cpu` = 所有 pid 之和;`fg_pid` = 最小 pid(主进程)** | 压测关心的是**这个应用总共吃多少 CPU**,不是某个子进程;主进程 pid 做存活判据(它死了才是真死) | 改 `pick_fg_pids()` 一处 |
| 2 | 要不要加 `watch_pkg` | **加,默认空** | 空 = 维持现状(看前台是谁);填了 = 只关心这个包,它不在前台/消失才算异常。**默认不变,所以零风险** | 前端参数表删一行 |
| 3 | 4MB 重放帽(>7.8h 图表退化) | **接受,不改平台帽** | 改它会影响**所有**脚本的重放内存。而且 **samples.csv 是全分辨率的,证据不丢** —— PNG 只是便利品,CSV 才是证据 | 抬 `REPLAY_TAIL_BYTES`,或按脚本类型分流 |
| 4 | samples.csv 被 Excel 锁住 | **接受 `csv_degraded`,但强制降级可见** | 锁住时判读**照样成立**(evidence 来自内存累积器,不读 CSV);丢的只是**原始逐拍转储**。降级会写进判定块的 DATA 行并把结果封顶为 WARN,**不可能被忽略** | 改成 sidecar 文件续写 |
| 5 | 三条复合命令重测 | ✅ **已完成 2026-09-14** —— 见 §12.1.1 | 实测确认了预判:新命令更贵,FULL 305.6 → **408.7 ms**,占空比 7.15% → **9.07%** | 重新跑测量脚本即可 |
| 6 | WiFi 的 adb 固定开销未知 | **不猜,改成启动时自测** | 脚本启动时跑 3 次 `adb shell true` 量自己的往返基线,`B` 从它派生 —— **USB/WiFi 的差异被这个自测吸收掉,公式里不再有硬编码常数** | 把自测换回常数 |
| 7 | su 陷 D 态的僵尸读 | **接受** | PC 侧有界 `communicate` 保证采集线程不死;设备侧那一个是**已知且必然回收不了**的(POSIX 的 D 态本就不响应信号) | 无法在用户态解决,只能重启设备 |
| 8 | 判定阈值未标定 | **写成具名常量 + `CALIBRATED = False`,判定块打印 `judge_version=v1-uncalibrated`** | 让"还没标定"这件事**印在每一份报告上**,杜绝假信心 | 一次健康 8h 基线跑后改常量 + 置 True |

> 第 8 条尤其重要:未标定的阈值写死却不标记,会让一份**完全健康**的报告看起来像有结论 —— 那正是"假绿"。
> **本节编号 12.7 与行号 8 被 `scripts/perf_monitor.py` 的代码注释直接引用,不可改动。**

> **§12.7-原始 未决项 已删除** —— 8 条已逐条裁决,结论即上表,原文见 git 历史。

### 12.8 上线后的三项修订(v2.9.0,2026-09-14,用户跑完第一份真机短样本后)

**本节推翻 §12.7 第 2 条的部分内容**(那是"加 `watch_pkg`,默认空"),其余不动。

| # | 议题 | 决定 | 理由 | 怎么反悔 |
|---|---|---|---|---|
| 1 | `track_foreground` 开关注定要删 | **删,前台采集恒开** | 用户:「采集前台 APP 感觉没必要做开关吧」。§12.7 当初把它当参数是**过度设计** —— 它省下的是一次 `dumpsys window` + `pidof` + `/proc/<pid>/stat`,而丢掉的信号是「被压测的 app 挂了 / 掉到后台」,那是长跑里最该被看见的故障。**`fg_*` 是唯一能看见它的东西。** | 在 `build_tick_command` 加回一个 flag(不建议) |
| 2 | `watch_pkg` 自由文本 → **select 下拉** | **改 `type:"select"`,预置 `WATCH_PKG_CHOICES`** | **自由文本里一个拼错的包名会静默废掉唯一依赖它的 gate 9** —— 报告看起来一切正常。下拉把"拼错"从可能性里去掉。清单**已由用户 2026-09-14 定稿**:YouTube TV / Netflix / Prime Video / 本地媒体播放器 + 不关注(**原先猜的"爱奇艺投影版"该设备上没装,已删**)。包名来自 `cmd package query-activities -c ...LEANBACK_LAUNCHER`;`pm list packages -3` 会漏掉 YouTube TV / Netflix(系统应用) | 改 `WATCH_PKG_CHOICES` 一处;前端本来就走 `f.choices`,零改动 |
| 3b | gate 9(APP)的判据 | **改成三出口:`watch_seen` + `watch_pid_dead` → fail / inconclusive / pass** | 原判据(包名 != `watch_pkg` 连续 2 拍)把"从没出现过"判成丢失(**假 fail**),又看不见"窗口还在但进程没了"(**假绿** —— 而那正是 `watch_pkg` 唯一的存在理由)。**判据无法区分两种状态时必须引入第三态,而不是调阈值** | 回到旧判据(不建议);`WATCH_LOSS_STREAK` 可调 |
| 3 | 结论怎么给人看 | **控制台块带英文解释(仍全 ASCII)+ 另出一份中文 HTML** | 用户:「最后的报告有些太抽象了」「输出一版 html 的 overall result 做成表格样式,然后带点指示描述,html 的可以带中文的」。**两者同源渲染** —— 同一张 row 表出控制台,同一份 payload 出 HTML,不可能各说各话 | 各自独立 |

**`interval_sec` 保留,并且要反复解释清楚**:用户问「采样间隔在之前的架构里面你不是说会做分级采样吗」。**答案:`interval_sec` 就是本文档里那个 T,分级采样分的是「每一拍读哪些节点」,不是「还要不要有一个采样间隔」。** MED/SLOW 的周期由 T **推导**(固定 5 s / 30 s 目标),调 T = 同时调三层。

**HTML 报告的边界(刻意如此)**:**自包含** —— 内联 CSS、无 JS、不拉图表库,目标是能把它拷出归档目录,在**一台从没装过 PPTP 的机器上**几年后打开还看得懂(引 CDN 或复用 `static/vendor/echarts.min.js` 都会**当场破坏这个性质**);**不是第二份判定** —— 由 `report.json` 的同一份 payload 渲染,写失败只打印一行 `[warn]`,没有 `else` 分支,**它永远不能改变结论**;**中文只在 HTML 里** —— stdout 仍是纯 ASCII(Windows 控制台 + WS 编码),这与"代码文件无中文"不冲突:这是**给人类看的数据**,不是代码。

**新增的硬约束**:判定块末尾的 `  html   = <path>` 用 **`=` 而非 `:`** —— PITFALLS.md #43:`_sniff_report_path()` 的认领规则是「含 ` : ` 且含 `report` 且以 `.json` 结尾,只取第一条命中」,而报告路径在 `reports/` 下**本身就含 `report` 子串**,只靠扩展名拒绝太脆;selftest 现在逐字复刻该规则做断言。

> **⚠ 这一轮引入并修掉了一个通道级静默失效** —— 删 `track_foreground` 时把 `due_sec()` 的布尔谓词一起改写,**逻辑整个取反**,前台通道整轮零数据而三层测试全绿。详见 [PITFALLS.md](PITFALLS.md) #42 与 [TODO.md](../TODO.md) §6.10。**教训:形状断言看不见死掉的通道。**

### 12.9 长跑按键 keepalive(v2.11.0,2026-09-17)

压测 YouTube / Prime Video / Netflix / 本地播放器时,**长时间播放会让 app 弹「还在看吗」**,把播放打断 —— 采到的样本里混进一段「没人看」的数据,而报告看起来一切正常。办法:加第 4 个参数 `key_ini`,后台按选中的 `.ini` 定时发按键把它压下去。**用户四条已裁决**:线程内 `import ir_runner` / 先给 MEDIA_PLAY 模板且 ini 要能自由选自由改 / 30 分钟一次 / **只有 perf_monitor 需要**,其余 7 个脚本与平台逻辑零改动。

| # | 议题 | 决定 | 理由 | 怎么反悔 |
|---|---|---|---|---|
| 1 | 怎么调 ir_runner | **线程内 `import ir_runner`**,不起子进程 | 平台「硬停」走 `proc.kill()`(`TerminateProcess`),**不发任何控制台事件** —— 子进程会活下来继续按按键,直到下次重启服务器才被 `_reap_orphan_scripts` 扫掉。daemon 线程随进程一起死 | 改 `Popen`(**不建议** —— 除非同时给硬停加进程组清理) |
| 2 | `run_loop` 还是自己驱动 | **自己按「一次按键」循环调 `run_step`** | `run_loop` 的 `while True` 唯一出口是它自己那个线程里的 `KeyboardInterrupt`,而 Windows 只把控制台事件投给**主线程** —— 那个异常永不到达 | 调 `run_loop`(失去中断能力与精确计数) |
| 3 | 再加一个发送间隔参数? | **不加**,节奏 = ini 的 `delay_ms` | 单步 ini 的 `delay_ms` 既是「重复间隔」也是「轮间隔」,它就是按键周期。再加一个参数 = 两处真相 | 加 `key_interval_sec`(不建议) |
| 4 | 报告要不要新 gate | **不要**,只记 4 个字段 | keepalive 是**测量条件**,不是被测对象。它的故障不能把一场本来有效的样本判成"不通过" | 加 gate(会污染判定语义) |
| 5 | `count>1` 的步怎么算 | **拆成 `count=1` 副本,逐次调用** | `run_step` 自己的重复循环**无法被中断**:一个 50 连发的步在停止后会一路按完;而且那一串**要么全算要么全不算**(实测出现过「日志 40 行按键、计数 30」) | 整个 `count` 交回 `run_step`(**不建议**) |

**一处真机发现**:`CTRL_BREAK` 送给**整个进程组**,`adb.exe` 也在内 —— 停止瞬间在途的那次按键会以 `ADB failed: `(stderr 为空)失败返回。第一版把它计成 `key_failed=1`,报告里看起来像 keepalive 出过错,**实际是用户自己按的停止**;定性只能放在**按键后面那个等待**上(真故障后面跟着正常间隔,停止会打断它)。**`key_failed` 的含义 = 失败过且仍在重试**。

**参数面**:`key_ini` 走 select(与 `watch_pkg` 同款待遇,见 §12.8 第 2 条),choices 在**导入时**用 `os.listdir` 扫 `ir_sequences/*.ini`(排除 `_` 前缀的临时文件),值用仓库相对路径、解析时对 `PROJECT_ROOT` 取绝对路径;前端本就消费 `f.choices`,**零改动**。`docs/REPORT_FORMAT.md` **不动** —— 共享引擎的 schema 没变,这只是新参数实例。

**v2.11.1 补记(2026-09-17,平台侧,与 perf_monitor 无关)**:验证本功能时撞见并修掉了「硬停之后那台设备被永久卡死」—— 上表第 1 条把「硬停 = `proc.kill()`,不发控制台事件」当成既定事实,而那条路径当时自己也坏了:它把 `interrupting` 写在 `await _stop_captures()` **之后**,`_stream_logs` 却在 `proc.wait()` 上**先醒**、先把任务终结成 `failed`,于是非终态覆盖了终态、没人再推进,409 守卫把那台设备锁死;硬停现在在**第一个 `await` 之前**认领终结者并直写终态 `interrupted`。**本表第 1 条的结论不变**(硬停仍是 `proc.kill()`、仍不发控制台事件),所以「线程而不是子进程」这个取舍依旧成立。详见 [ARCHITECTURE.md](ARCHITECTURE.md) §4.4 与 [PITFALLS.md](PITFALLS.md) #48。

---

> **维护约定**:本文只写 `perf_monitor` 的判定模型与采样规格;平台机制见 [ARCHITECTURE.md](ARCHITECTURE.md) 与 [START-HERE.md](START-HERE.md),报告与 HTML 的渲染契约见 [REPORT_FORMAT.md](REPORT_FORMAT.md)。改本文时**只要 §12.x 的编号与 §12.7 表中第 8 条不动**,`scripts/perf_monitor.py` 的注释引用就一直有效。
