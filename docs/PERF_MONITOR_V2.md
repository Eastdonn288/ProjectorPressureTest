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
            ("PIDSTAT","SLOW","fg"), ("GPU","MED","gpu"),    ("WIFI","SLOW",()),
            ("NTC","SLOW",())]
tick 后处理顺序固定:parse → up → rollover/reboot 守卫 → deltas → fg    （不得改序）
```

> **`WIFI` 的 metrics 元组是空的,这是刻意的**(v2.12.0)—— 它**不喂任何 metric**,见 §12.10。WIFI 段**追加在末尾而非插入**:命令的字节序对每一个既有段都不变,§12.1 上面那两条命令字面量因此无需重写。**`NTC` 沿用同一条纪律**(v2.13.0,§12.11):空 metrics 元组、追加在 `WIFI` 之后、又是一段条件通道。

**谁不发段:`off_sections` 是唯一的开关量(v2.13.0,见 §12.12)。**

```
off_sections = 用户本次取消的段组 ∪ 运行期降级关掉的段(GPU / WIFI / NTC)
build_tick_command(nonce, tiers_present, guard_s, off_sections)
section_due(name, secs, tiers, off_sections)
```

`Monitor.off_sections()` 算**一次**,同一个 `frozenset` 同时喂给**命令合成**与**到期判定**两个调用点 —— 这两个函数此前各收一半布尔开关(`gpu_enabled` / `wifi_enabled`),其中 wifi 那一半在 `tick()` 里**漏传过**,导致"降级后仍每拍发 `@@WIFI`"(PITFALLS.md #51)。**一个集合、算一次、两处共享**就是为了让这种漂移不可能再发生。`--selftest` 里有一条正面断言:对若干 `off_sections` 取值,两个函数对**每一段**的取舍必须完全一致。

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

**WIFI 段(v2.12.0,见 §12.10 的三条决策):**

- 命令字面量:`echo @@WIFI:<nonce>; timeout -k 1 2 cmd wifi status`。**`timeout` 包装不是可选项** —— 不包的话,一次卡死的 `cmd wifi status`(binder 调进僵住的 `system_server`)会一直拖到 PC 侧预算 `B` 耗尽 → `rc is None` → `res = "timeout"` → **这一拍所有 metric 被判 `ST_FAILED`,整拍数据全丢**;包上以后最坏只丢 wifi 这一段,该拍只是 `partial`。守卫取 **2 s**(`WIFI_GUARD_S`),因为实测耗时 98–172 ms,2 s 已是 10 倍余量。
- 实测成本(设备 `EE88885D8DEF08004CD1` / EcoPro / idle):`cmd wifi status` **98–172 ms**,输出的 **2392 B**。普通 `adb shell`(uid 2000),**不需要 `su`**。加进复合命令后实测 **+76.5 ms**(FULL 383.9 → FULL+WIFI 460.4 ms),占空比影响见 §12.1.1。
- 降级与重探**完全镜像 GPU**:连续 **3 个到期 tick** 读失败(`WIFI_DEGRADE_AFTER`)→ 置 `d`、发一次 `src_degraded`、此后不再发 `@@WIFI` 标记、每 **300 s**(`WIFI_REPROBE_INTERVAL_S`)重探一次;恢复时 `src_recovered`。**复用 `src_degraded`/`src_recovered` 而不造新 kind** —— 这两个名字是按"数据源"命名的,`detail` 已足够区分。影响面因此被钳死在**每个故障期最多 3 个 partial 拍**,且 `coverage` 不受影响(`good = ok + partial`)。
- **读取失败 ≠ 断网**,而且**断网不让这一拍变 `partial`**:两者都是"这一拍测到了什么"的问题,混为一谈会让一次 20 分钟的路由器重启表现为"监控退化";更糟的是 `ok` 掉到 0 会让 **gate 1 DATA 判 inconclusive,把一次健康的 CPU 压测报成 INCONCLUSIVE**。只有**读失败**才计入 `sec_fail`。两条语义各有纯函数钉死(`wifi_section_failed()`)并进 `--selftest`。

**NTC 段(v2.13.0,见 §12.11 的三条决策):**

- 命令字面量:`echo @@NTC:<nonce>; timeout -k 1 2 cat <LCD_PATH> <LED_PATH>`。**两个通道一次 `cat`**:一次读取就是一份不可分割的观测。`timeout` 包装的理由与 WIFI 段**逐字相同**(卡死的 sysfs 读会吃光 PC 预算 → 整拍 `ST_FAILED`)。
- 实测成本(idle):一次 `cat` 中位 **92.5 ms**,基线 `adb shell true` 61.1 ms → 净增 **+31.4 ms**(**v2.14.1 起这条命令换成 `su 0 cat`,中位变 200.1 ms、净增 +60.6 ms**,见 §12.11.6;下面这组复合命令的实测数**没有随之重测**,差值应整体上调约 +30 ms);**但并入复合命令后实测 +65 ms 上下**(交错取样 7 次落在 **+51.8 ~ +74.0 ms**,见 §12.1.1;更早的块采样 3 次是 +51.5 / +57.9 / +86.4 ms)—— 段内增量**不是**独立调用的增量,`su`/shell 启动的重叠会吃掉一部分,也会有反向的抖动,而交错取样对消的只是**块间**漂移,去不掉这个偏差。
- 降级与重探**完全镜像 GPU/WIFI**:连续 3 个到期 tick 读失败 → 置 `d`、发一次 `src_degraded`、此后不再发 `@@NTC` 标记、每 300 s 重探。**复用 `src_degraded`/`src_recovered`,不造新 kind**;只有**读失败**计入 `sec_fail`,读到的值偏了不算失败。
- **`NTC` 不做门、不做卡控、不进 `caps`** —— 与 WIFI **刻意不同**(用户 2026-09-20 明说"不作为 gate,不做卡控,纯监控")。理由:WiFi 通断有明确的"坏"语义,而温度的"坏"要先有换算公式和阈值 —— 公式虽然拿到了,但它落在**下游的离线工具**里(§12.11.2),采集链路上永远只有计数。

**burst(手动补采):** arm 时钳位 `counter[SLOW] = min(counter[SLOW], ticks_per[MED])`(禁止把 SLOW 相位跳到 MED 之前);预算 `max(1, duration_sec / 300)`,上限 12 次/小时。T ≥ 30 是 no-op;T=6 抬 +0.26 pp,T=1 最多 +0.33 pp —— **此代价必须如实写进 UI 提示**。

**参数钳位:** `interval_sec` float,default 2.0,**min 1.0**,max 60;脚本内再钳一次 `max(1.0, min(60.0, v))`(server.py 目前不校验 min/max,只缓存 schema)。

### 12.1.1 锁定的成本与占空比(**2026-09-20 重测,含 NTC;v2.13.1 改为交错取样后再重测**)

> ⚠ **这张表是快照,不是常数,先读这条再读数字。** 同一台设备的 `FULL` 在**同一天内**三次重测分别是 **318.3 / 314.4 / 428.6 ms**(间隔约两小时),两天前是 383.9 ms,再往前是 644 ms。**绝对值的漂移量级大于本次新增通道的增量**,所以**能跨版本比较的只有相邻两行的差值**,不能比较两次测量各自的总额。原因未查明(推测是设备当时的后台状态 / 温度 / 索引),`--probe-cost` 只量空载且每次都要重新锚定。

**2026-09-20 实测**(EcoPro / `EE88885D8DEF08004CD1` / idle / `--probe-cost`,5 reps 取中位,
**round-robin 取样** —— 见下"取样顺序")。**下面是其中一次完整测量**(重测三轮里的第一轮)——
**不是"最后一次",也不该被当成常数**:同样这台设备、同样空载,重测四次得到的 T=2.0 全开占空比是
**9.11 / 9.16 / 9.24 / 9.48 %**,光重复测量本身就有 **0.37 pp** 的散布。

| 变体 | 中位墙钟 | 输出字节 |
|---|---|---|
| FAST | 131.1 ms | 753 |
| FAST+MED | 246.0 ms | 2095 |
| FULL | 417.1 ms | 2536 |
| FULL+WIFI | 482.6 ms | 2601 |
| **FULL+WIFI+NTC** | **549.8 ms** | 2622 |

**净增(只有同一次测量内相邻两行可减):wifi = +65.5 ms,ntc = +67.2 ms。**

**取样顺序(v2.13.1,`perf_monitor.py` 1.3.2)**:变体**交错取样**(round-robin:一轮里每个变体各
一次,再进下一轮),不再是"一个变体连量完 reps 再换下一个"。**调用次数 / sleep 次数 / runtime 全部
不变** —— 改的只是顺序。理由:这张表的产物是**相邻两行的差值**,而差值只有在两行**同一时刻**测出来时
才是代价;整块量完再换下一个,等于把一整段背景漂移塞进了差值里。交错之后漂移变成共模项,相减时对消。
**实测重复性 —— 交错取样没有量到改善,如实记下**(同设备 idle,5 reps 取中位):

| | ntc delta(各次) | 极差 | 均值 |
|---|---|---|---|
| round-robin(新),n=7 | +51.8 / +58.6 / +58.9 / +67.2 / +68.7 / +73.5 / +74.0 | 22.2 | +64.7 |
| 块采样(旧),n=6 | +51.5 / +57.9 / +63.3 / +64.5 / +71.8 / **+86.4** | 34.9 | +65.9 |

> ⚠ **别从上面这张表读出"交错取样让重复性变好了" —— 它没有。** 上表里 round-robin 的极差小,
> 唯一的来源是块采样那组有一个 **+86.4 ms** 的旧离群点;把它拿掉,两组几乎没有区别。真正做过一次
> **受控 A/B**(同一天、两种取样**交替**各跑 3 次,让背景漂移对两组同等作用)的结果是:
>
> | A/B(交替 3 次) | ntc delta | 极差 |
> |---|---|---|
> | round-robin | +51.8 / +58.6 / +68.7 ms | **16.9** |
> | 块采样(只改循环顺序的副本) | +63.3 / +64.5 / +71.8 ms | **8.5** |
>
> 也就是**块采样那组反而更紧**。n=3 本来什么都说明不了,而它至少说明:**在这台设备的这个窗口里,
> 背景漂移小到"交错能省下的那部分"被噪声盖住了。**
>
> **在负载下又做了一遍(同样交替各 3 次、连测两组共 n=6,6 个 `timeout 400 yes` 压到 ~66% idle)** ——
> 那里漂移本该大得多,交错本该显出优势,**结果同样没有**:
>
> | 负载下(各 n=6) | delta 分布 | 极差 |
> |---|---|---|
> | round-robin,ntc | +36.9 ~ +284.5 ms | 247.6 |
> | 块采样,ntc | +18.1 ~ +243.8 ms | **225.7** |
> | round-robin,wifi | **−7.2** ~ +216.1 ms | 223.3 |
> | 块采样,wifi | **−23.0** ~ +232.3 ms | 255.3 |
>
> **负载下的逐通道 delta 在两种取样顺序下都不能用**:极差 220~255 ms,而信号本身只有 100~160 ms;
> 两种顺序各出现了一次**负的 wifi delta**(−7.2 / −23.0)—— 一个纯成本差值出现负号,说明行间散布
> 已经**大于通道本身的代价**。空载时它只是个不可靠的数,负载时它直接是错的。
>
> **那这个改动凭什么留着?** 因为它去掉的是一个**系统性**误差项,不是噪声:块采样把"两行之间那段
> 时间设备在忙别的"**整段记进差值**,而差值本身是这张表唯一的产品。代价是零(调用次数 / sleep
> 次数 / runtime 全不变),机制上它只会让估计更接近无偏,所以留着。
> **但两次受控实验(空载 3+3、负载 6+6)都没量出改善,而且两次都是块采样那组极差更小 ——
> "实测证明它更稳"这句话是假的,不许写。**
>
> **更要紧的是负载那一格暴露的东西**:负载下主导误差看来**不是**行间漂移(否则交错会赢),而是
> **单次调用自己的抖动** —— 加测一次拿到整表,同一变体的 5 次取样是:
> `FULL min 627.0 / max 924.6`(**组内极差 297.6 ms**)、`FULL+WIFI 859.3 / 1157.5`(298.2)、
> `FULL+WIFI+NTC 1049.1 / 1247.1`(198.0)。**组内极差(200~300)和组间极差(220~255)是同一个
> 量级** —— 也就是说,"两行之间的一致程度"根本不是瓶颈,瓶颈是**每一行自己就没测准**。
> **改顺序对这项误差一分都去不掉,只能靠加 reps**,而加多少 reps 未做实验。
> 记进 [TODO.md](../TODO.md) §6.20,**未验证**。
>
> 本文档里早于 2026-09-20 的 delta(§12.10 的 **+76.5 ms**、§12.11 的 **+51.5 ~ +86.4 ms**)都是块采样
> 的产物,保留为历史记录,**不再作为当前基准**。
>
> **ntc 的增量还有一个更大的坑**:它**单独一次 `cat` 只花 92.5 ms、相对 `adb shell true` 净增
> 31.4 ms**,而进复合命令后是 +65 ms 上下(实测 7 次:+51.8 ~ +74.0)。差这么多不是矛盾:**段进复合
> 命令后的增量 ≠ 独立调用的增量**(shell 与 `su` 的启动开销有重叠,也可能被别的东西挤占),而且两个
> 通道要读两个 sysfs 文件。交错取样只对消**块与块之间**的漂移,**这个偏差它一分都去不掉**。
> **取区间,不取单点。**

| T | MED | SLOW | tick 混合 | 占空比(wifi 开,ntc 关) | 占空比(全开) | |
|---|---|---|---|---|---|---|
| 1.0 s | 5 s | 30 s | 24 FAST + 5 MED + 1 FULL | 16.20% | **16.43%** | FULL 单拍已占 1 s 的 57% |
| **2.0 s** | **6 s** | **30 s** | 10 FAST + 4 MED + 1 FULL | 9.26% | **9.48%** | ← 推荐 |
| 6.0 s | 6 s | 30 s | 0 FAST + 4 MED + 1 FULL | 4.89% | 5.11% | 分层退化(MED 与 FAST 同层) |
| 30 s | 30 s | 30 s | 0 + 0 + 1 FULL | 1.61% | 1.83% | 三层塌成一层 |

> **两列占空比是刻意留的**:左列是 v2.12.0 发布时用的基准(wifi 开、ntc 还不存在),留着它才能把一次重测和文档里已有的数字对上;右列才是**默认配置**(ntc 默认开)真实的代价。NTC 走 SLOW 档,只搭 FULL 拍的车,T=2.0 的代价是 **+0.22 pp**。

**真机实跑交叉验证(v2.13.0,2026-09-20,T=2.0,duration=120 s,61 拍,同一台设备):**

| 配置 | 判定块实测 | 空载合成预测 | 差 |
|---|---|---|---|
| 全开(默认) | `cost_median=140ms **duty=10.07%**` | 9.11 ~ 9.48% | **+0.59 ~ +0.96 pp** |
| 取消「前台应用」 | `cost_median=141ms **duty=9.21%**` | — | — |

> **实测与预测的缺口比原来记的更宽,原因是预测那一列换了算法**:旧表写的是块采样预测 9.71%,
> 现在是交错取样四次重测的 **9.11 ~ 9.48%**(§12.1.1)。**缺口从 +0.36 pp 变成 +0.59 ~ +0.96 pp**
> —— 如实改,不把预测往实测上凑。
> **实跑永远比空载贵,这是既有结论不是新问题**(§10):`--probe-cost` 量的是一台什么都没干的设备,而实跑时设备正在放视频。**如实记下,不调成一致。** 两行都不是"NTC 的代价" —— 它们分别是"默认配置真实跑一次的代价"。
> **注意两列的可信度是不对称的**:右列是**推算**(合成 tick 混合 × 逐层单价),左列是**直接记账**
> (见下),所以**要引用就引用左列**。

> **左列为什么是"直接记账"而不是估计**:判定块里那两格不是推出来的,是跑批自己数的 ——
> `cost_ms` 是**每一拍真实测到的墙钟**(`perf_monitor.py` 里每拍 `cost_ms = now - start`),
> `cost_median` 是这 61 个数的 **p50**,`duty` 是 **Σ(每拍实测耗时) ÷ 总时长**。
> 没有合成、没有相减、没有从别的量外推。**它是"这次跑批里到底有多少比例的时间花在采集上"的账**,
> 所以它不受 §12.1.1 那张表的取样顺序问题影响 —— **这两套数字是两套东西,不要混着引用。**
> 更早的同型数据:真机实跑(v2.12.0,含 wifi 不含 ntc)9.43%;真机实跑(2026-09-14,不含 wifi,MT9676)9.06%。**这三个数不能相减** —— 设备不同、时长不同、后台状态不同。
> `adb over WiFi` 链路**至今完全没测**。自测 adb 往返:本机 **55 ms**,设计文档里的 110–140 ms 是另一条链路 —— 所以往返基线是**启动自测**而非硬编码常数(§12.7 第 6 条)。


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

> ⚠ **`{9}` 是写死的,`st` 契约在 v2.12.0 未变,而且这是刻意的。** 加 wifi 时最容易"顺手做完整"的一步就是把它变成第 10 个 metric —— **不要**。`METRIC_ORDER` 的 9 结构性耦合在四处:这个正则的 `{9}`、CSV 列块、分布表、以及定宽控制台行;改一处就得改全部,而收益是零 —— WiFi 链路是**测量条件**,不是被测对象(与 §12.9 第 4 行的 keepalive 同构)。它走独立的 `wifi` / `wifi_st` 列 + `evidence["wifi"]` 顶层键,与 `memory`/`cpu`/`fg` 同级,**不进 `metrics`**(那里所有消费者都按 `METRIC_ORDER` 走)。理由与完整设计见 §12.10。

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
# PPTP perf samples v2 | script=perf_monitor.py | judge_version=v1-uncalibrated
# device=<serial>
# config=interval_ms=2000,duration_sec=0,watch_pkg=(unset)
# config_monitors=mem,gpu,fg,wifi,ntc
# tiers=FAST:1,MED:3,SLOW:15
# metrics=cpu,procs,up,mem,gpu,gpu_clk,fg_pkg,fg_pid,fg_cpu
# sources=cpu=stat,mem=meminfo,gpu=mali/dvfs,fg=window+pidof,wifi=cmd_wifi_status,ntc=iio_in_voltage3_2_raw
# ntc_unit=raw_adc ntc_paths=lcd:<path>,led:<path>
# counters_t0=<iso8601>
t_sec, clock_ms, res, k, cost_ms, flush_ms, late_ms, gap_ms,
cpu, cpu_st, procs, procs_st, up, up_st, mem, mem_st,
gpu, gpu_st, gpu_clk, gpu_clk_st, fg_pkg, fg_pkg_st, fg_pid, fg_pid_st, fg_cpu, fg_cpu_st,
wifi, wifi_st, ntc_lcd, ntc_led, ntc_st,
causes, events                                       ← 33 列,顺序固定
# end_of_run=<iso8601> ticks=N ticks_ok=M run_wall_s=W status=completed|interrupted|csv_degraded
```

- **`wifi` / `wifi_st`(v2.12.0)与 `ntc_lcd` / `ntc_led` / `ntc_st`(v2.13.0)都是普通列,不是 metric** —— 所以 `# metrics=` 那一行**不含**它们(它仍然只有 9 个),而 `# sources=` 那一行**加了** `wifi=cmd_wifi_status,ntc=iio_in_voltage3_2_raw`(`sources` 列的是数据来源,不是被测对象)。这个不对称是刻意的。
- `wifi_st` 取 `f/h/n/x/a/d`,**但 `b` 永远不会出现**(详见 §12.10);`wifi` 列是裸状态词(`ok/noip/noassoc/off/unknown`),`_st` 的意义与其它列相同:分不清"这一拍新鲜读到 ok"与"通道已死、ZOH 的 ok"。
- **`ntc_st` 只有一个,尽管有两个通道**(详见 §12.11):两个通道来自**同一条命令的一次读取**,新鲜度是**命令级**属性,不是通道级属性;拆成两个只会让人以为它们能各自新鲜。取值同样是 `f/h/n/x/a/d`,`b` **同样永不出现**(温读是绝对量,没有基线概念)。`ntc_lcd` / `ntc_led` 是**裸整数**,读不到时留空。
- **`# config_monitors=` 与 `# ntc_unit=` 两行是归档自述**(v2.13.0)。`config_monitors` 让一份几十年后的 CSV 自己说清"这一列全是空"是**被取消**还是**全程读失败** —— 两者在 CSV 里都是空 + `d`/`x`,而 `d` 的既有语义是"config 或降级",单看 CSV 分不出是哪一种。`ntc_unit=raw_adc` 是**诚实性锚点**:一个原始 ADC 计数列和一列摄氏度长得一模一样,**不声明单位,归档就有永久歧义**(§12.11)。
- 启动时 `open(..., "a", newline="")` **持住句柄**,每 tick 写完 `flush()`,运行期不 close。
- **撕裂行判定**:末行必须同时满足「含换行符 + 列数 = 33 + `t_sec` 严格单调」,否则整行丢弃;`end_of_run` 行**必须写**,缺行即文件级 partial 标记。列数在代码里**不重复写死**:写行与回读两处都是 `[... for c in CSV_COLUMNS]` 派生的,**全仓唯一一个字面量 `33` 是 `--selftest` 里那条断言** —— 它的作用正是"加了列但没同步文档/契约"时立刻变红。

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

合成:`result = INCONCLUSIVE > FAIL > WARN > OK`,并附 `capped_by` 列表说明是哪几个门封的顶。

> **伪门负数 id 的既有机制**(v2.12.0 起有第 3 个成员):`caps` 里可以出现**负数 id** 的条目,它们**只降不升**、**不进 `gates`** —— 所以 `ids == [1..9]` 这条 selftest 断言永远成立。已有成员:`-1 PARTIAL`、`-2 UNCALIBRATED`、**`-3 WIFI`**(v2.12.0,见 §12.10)。
> ⚠ **既有空洞(不在本次范围,记进 [TODO.md](../TODO.md) §6):`capped_by` 算出来了却没有任何渲染层读它。** `format_result_lines` / `verdict_rows` / `format_result_html` 三处都没消费它,而 `GATE_ZH` 只在 HTML 门表里被用到、伪门又不在 `gates` 里 —— **所以 `-1`/`-2`/`-3` 从来没在报告上露过面**。`-3` 因此**不能依赖 `capped_by` 传达原因**,改为显式渲染两处:`WIFI` 行的值本身带摘要,以及 HTML `六、` 小节写明封顶。把这个洞补上是独立的一件事。**`INCONCLUSIVE ≠ PASS`**:`n < 5` 或 `span < 30 s`、`coverage < 0.5`、全指标无有效读数 → `INCONCLUSIVE`;`coverage < 0.8` 或 `partial=true` → 最高只能 `WARN`;中途的结论性告警必须带数据充分性(要能输出 `LEAK? (early, insufficient data)` 这种**不确定态**)。

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
DATA   : samples.csv=<filename> events=<n> judge_version=<v>
SCOPE  : <SCOPE all <n> monitor(s) enabled (nothing was deselected for this run) | SCOPE off=<ids> (<n> monitor(s) not collected - ...)>
DEVICE : <serial> (<dev_short>)
MEMORY : <...>
CPU    : <...>
APP    : <...>
EVENTS : timeout=<n> offline=<n> reboot=<n> other=<n>
GATES  : <每一道未通过的门 | all pass>
COST   : cost_median=<ms>ms duty=<pct>%
WIFI   : <LINK UP | LINK DOWN(<state>) | LINK RECOVERED | LINK ? no wifi observation in this run>
NTC    : <TEMP lcd=avg<a>/max<b> n=<k> led=avg<a>/max<b> n=<k> [fail=<j>] seen=<k>/<m> unit=raw_adc res=<P>s | TEMP ? no ntc observation in this run>
report : <path>
```

> **`WIFI` 行是纯事实行**(v2.12.0):`gids=()` 而非 `None`,所以它**恒为绿**、不参与任何门 —— 它是"这趟跑的网络条件是什么",不是"设备好不好"。值里带 `ssid=`/`rssi=`/`span=`/`seen=`/`res=`,`res=` 就是采样分辨率(§12.10)。
> **`NTC` 行同样恒为绿**(v2.13.0,`gids=()`)—— 用户明说它"不作为 gate,不做卡控,纯监控"(§12.11)。`n=` 是**真实读数**的条数(SLOW 档下多数拍走 ZOH,`h` 不计入),`seen=k/m` 是"采到 / 应采",`fail=` 只在有读失败时出现,`res=` 是采样分辨率(30 s)。**`unit=raw_adc` 必须印在行上** —— 这一行的抬头叫「温度」,而里面的数字**不是摄氏度**;不写单位就是把一个 ADC 计数冒充成温度(§12.11)。无观测时走 `TEMP ? ...` 分支(**必须非空**,否则"每行都有值"的既有断言会挂)。
> **`SCOPE` 行是纯事实行**(v2.13.0,`gids=()`):它是**归档自述**的关键 —— 一份几个月后的 CSV 里"这一列全是空"分不清是**被取消**还是**全程读失败**(§12.2)。取消门依赖项(`mem` / `fg`)时对应的门会被**强制判 inconclusive**,本次结论随之变 `INCONCLUSIVE`;**这一行把"为什么"说出来**,因为 `capped_by` 渲染不出来(§12.2 的空洞)。运行期被降级丢弃的通道**有意不列在这里** —— 那是另一回事,有 `src_degraded` 事件和空的 source。
> **约束**:除最后一行外,**任何行不得出现字面量 `report`**,最后一行必须且仅是 report.json 的路径 —— `_sniff_report_path` 靠「含 ` : ` 且候选以 `.json` 结尾」认路径,多行候选会认错,而且 `_stream_logs` 只捕获**第一个** report 路径。**`WIFI` / `NTC` / `SCOPE` 三行的所有分支都不得含 `report`**(selftest 逐字断言),这条约束因此在每次加行时都要重新检查一次。`verdict.metrics[m] = {status, n_fresh, expected, coverage}`;`EVENTS` 一行汇总、详情在报告里(否则看到跑过 3 次告警却不知道是哪三次);v2.9.0 起块尾另有 `  html   = <path>` 一行,用 **`=` 而非 `:`**(理由见 §12.8)。

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
| **P8** WiFi 链路监控(v2.12.0) | `@@WIFI` 段 + `wifi`/`wifi_st` 列(→ **30 列**)+ 伪门 `-3` + HTML `六、` 时间线。**不改 P1–P7 的任何契约**:`st` 仍是 9 字符、`METRIC_ORDER` 仍是 9 个、`gates` 仍是 1..9 | P7 |
| **P4** judge 与自证 | 纯函数 `judge(evidence)`;`--selftest` 断言 `judge(report["evidence"]) == report["verdict"]` 与 `evidence ← samples.csv` 机械可推 | P3 |
| **P5** 平台归档扩展 | `_sniff_report_path` / `_siblings` 扩到 `*.csv` 且以 `perf_<dev_short>_<ts>.` 前缀配对;`ARCHIVE_MEMBER` 增 samples/events | P4 |
| **P6** 事件呈现策略 | console / 图表 / 归档三处的呈现与文案;区分信息级(`fg_change`)与告警级(`offline_start`/`reboot`);**仍禁止 banner / toast / 状态条** | P5 |
| **P9** NTC 节点温度 + 单次运行监控项勾选(v2.13.0) | `@@NTC` 段(两个通道一次 `cat`)+ `ntc_lcd`/`ntc_led`/`ntc_st` 列(**→ 33 列**)+ `SCOPE` 事实行 + 前端 `multiselect` 参数类型。**不改 P1–P8 的任何契约**:`st` 仍是 9 字符、`METRIC_ORDER` 仍是 9 个、`gates` 仍是 1..9、`capped_by` 仍无人消费 | P8 |
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

> **第 4 行的"测量条件不设门"是 v2.12.0 WiFi 监控的直接先例**(§12.10)—— 但两者**有一个刻意的差别**:keepalive 完全不碰结论,而 WiFi 中断**要把结论封顶为 WARN**(用户 2026-09-20 裁决)。理由不对称是因为后果不对称:keepalive 失效只意味着"这一段可能没人看",样本本身仍然有效;而断网会让视频平台报 connection error,**这次跑的结论就不干净了** —— 封顶是"不许把一次条件不干净的跑说成满血",不是"把网络故障算作设备故障"。注意它**只降 `OK → WARN`,绝不覆盖 `FAIL`**:否则一次真正的设备故障可以被"当时网断了"洗白。

**一处真机发现**:`CTRL_BREAK` 送给**整个进程组**,`adb.exe` 也在内 —— 停止瞬间在途的那次按键会以 `ADB failed: `(stderr 为空)失败返回。第一版把它计成 `key_failed=1`,报告里看起来像 keepalive 出过错,**实际是用户自己按的停止**;定性只能放在**按键后面那个等待**上(真故障后面跟着正常间隔,停止会打断它)。**`key_failed` 的含义 = 失败过且仍在重试**。

**参数面**:`key_ini` 走 select(与 `watch_pkg` 同款待遇,见 §12.8 第 2 条),choices 在**导入时**用 `os.listdir` 扫 `ir_sequences/*.ini`(排除 `_` 前缀的临时文件),值用仓库相对路径、解析时对 `PROJECT_ROOT` 取绝对路径;前端本就消费 `f.choices`,**零改动**。`docs/REPORT_FORMAT.md` **不动** —— 共享引擎的 schema 没变,这只是新参数实例。

**v2.11.1 补记(2026-09-17,平台侧,与 perf_monitor 无关)**:验证本功能时撞见并修掉了「硬停之后那台设备被永久卡死」—— 上表第 1 条把「硬停 = `proc.kill()`,不发控制台事件」当成既定事实,而那条路径当时自己也坏了:它把 `interrupting` 写在 `await _stop_captures()` **之后**,`_stream_logs` 却在 `proc.wait()` 上**先醒**、先把任务终结成 `failed`,于是非终态覆盖了终态、没人再推进,409 守卫把那台设备锁死;硬停现在在**第一个 `await` 之前**认领终结者并直写终态 `interrupted`。**本表第 1 条的结论不变**(硬停仍是 `proc.kill()`、仍不发控制台事件),所以「线程而不是子进程」这个取舍依旧成立。详见 [ARCHITECTURE.md](ARCHITECTURE.md) §4.4 与 [PITFALLS.md](PITFALLS.md) #48。

### 12.10 WiFi 链路监控(v2.12.0,2026-09-20)

**要解决的问题**:`perf_monitor` 此前**完全没有网络通道** —— 9 个 metric 全是 CPU/内存/GPU/前台应用,10 种事件里没有一种与网络有关,9 道门里没有一道看网络。跑压测时如果 IPTV 平台报 connection error,报告里没有任何东西可以对齐,用户无法判断"是设备/应用的问题,还是当时网断了"。**目标产出**:报告里出现一条**带墙钟时刻的 WiFi 通断时间线**,拿它对着归档里的 `.logcat.log`(平台已在采)定位视频平台报错的那一刻。

用户原话(2026-09-20):「perf_monitor 新增监控项 / WiFi status / 采样的间隔和频率需要确认一下,这个的采样频率也不需要很高,只需要能保证查看报告时能确认是哪一段时间断的网,视频平台是什么时候报的 connection error 即可」

**用户四条已裁决(不得再改):**

| # | 议题 | 裁决 | 理由 / 代价 |
|---|---|---|---|
| 1 | 采样频率 | **SLOW 档 ≈ 30 s**(`WIFI_TIER`,与 FOCUS/FGPKG/PIDSTAT 同档) | 用户权衡过 ±30 s 的代价后选的;**换档只是改一行** |
| 2 | L3 探测(ping) | **不做**。只读 `cmd wifi status` | ping 实测 **1206 ms**(RTT 300–1105 ms),比整条 FULL 命令还贵;且引入外部 IP 依赖 |
| 3 | 断网是否影响判定 | **封顶为 WARN**(走伪门 `-3`,**不新增门**) | 见 §12.9 的差别说明;**只降 `OK→WARN`,不覆盖 `FAIL`** |
| 4 | connection error 的时刻 | **只出时间线,人工比对归档 logcat** | **perf_monitor 不读 logcat** —— 守住 v2.6.0 铁律「日志采集归平台,不归脚本」 |

#### 12.10.1 一个条件通道,不是第 10 个 metric

**不新增第 10 个 metric。** `METRIC_ORDER` 是 9,而 `ST_LINE_RE` 的 `{9}`、`st` 拼接、CSV 列块、分布表、定宽控制台行全部结构性耦合在这个 9 上。WiFi 链路是**测量条件**,不是被测对象 —— 与 §12.9 第 4 行 keepalive 同构。

因此:CSV 走**独立的 `wifi` / `wifi_st` 两列**(末尾,`causes`/`events` 之前),evidence 走**顶层 `"wifi"` 键**(与 `memory`/`cpu`/`fg` 同级,**不进 `metrics`** —— 那里所有消费者都按 `METRIC_ORDER` 走),事件走两个新 kind,判定走伪门。

#### 12.10.2 状态机与两条不可弄反的语义

| 状态 | 含义 |
|---|---|
| `ok` | 射频开、已关联、有可用 IPv4 |
| `noip` | **已关联但没 IP** —— 本轮实测抓到的真实瞬时态,也正是视频平台最容易报 connection error 的状态 |
| `noassoc` | 射频开但未关联 |
| `off` | 射频被关(`Wifi is enabled` 不出现) |
| `unknown` | 读取成功但无法归类 |
| `x` / `a` | **读取失败** —— 复用已有 `ST_FAILED`/`ST_ABSENT` 字汇,不造新词 |

`WIFI_STATES_DOWN = (off, noassoc, noip, unknown)` —— **`unknown` 算 DOWN** 是刻意的:一次无法归类的读取宁可多报一次 WARN(信号是"这个 build 变了"),也不许静默当成健康链路。

1. **读取失败 ≠ 断网。** 读失败意味着"不知道",绝不能记成一次中断,否则报告会凭空造出假窗口。纯函数钉死:`wifi_section_failed(state)` 只为 `ST_FAILED`/`ST_ABSENT` 返回 True。
2. **断网不能让这一拍变 `partial`。** 测量是成功的,是网络不通。混为一谈会让一次 20 分钟的路由器重启表现为"监控退化";更糟的是 `ok` 掉到 0 会让 **gate 1 DATA 判 inconclusive**(`freeze` 不发 `ticks_ok` 时 `judge` 回退到 `run.get("ok")`),**把一次健康的 CPU 压测报成 INCONCLUSIVE**。**只有读失败才计入 `sec_fail`。**

`wifi_st` 的 `b`(baseline)**永远不会出现**:`b` 只对 DELTA 型指标有意义(首次读没有 delta 可算),而链路状态从第一次读起就是绝对值。

#### 12.10.3 事件:免疫 60 s 限流器,且结构性有界

两个新 kind,镜像已有的 `offline_start`/`offline_end` 命名:`wifi_lost`(`reason` = 新状态词)、`wifi_back`(`detail` 前缀 `down=<n>s (<k> tick(s))`)。**只在状态真的改变时发,首个状态是基线不发事件**(与 `b` 基线同一纪律)。

**必须绕过 60 s 限流器。** `add_event` 按 `kind` 做 60 s 定窗去重,而 wifi 跳变是**成对**语义 —— 丢一半不是"降级"而是**损坏**(窗口会一直开到运行结束)。修法**不改 `add_event` 签名**:它已有现成的绕过通路 —— `immediate=False` 时整段限流器不执行(`rollover` 已在用)。**这是回归哨兵**:改回默认会让 `--selftest` 那条"喂 下/上/下/上 必须得到 4 个事件"的断言失败(实测会只剩 2 个)。

之所以不需要限流器兜底:状态跳变只可能在 due 拍上被观察到,SLOW 档下最快 30 s 一次 → 最坏 2 次/30 s = **4 次/分钟**,且只有变化才发。这是**结构性有界**,与 `misparse` 那种无界速率不同。**有界性写进文档,不要用"吃掉真实边界"的限流器来藏它。**

#### 12.10.4 分辨率:P = ticks_per[SLOW] × T,不确定度是 ±P 而不是 ±P/2

采样点是"**最后一次已知良好**",不是区间中点。设第一个非 ok 采样在 `t1`、其后第一个 ok 采样在 `t2`,则真实起点 ∈ `(t1−P, t1]`、真实终点 ∈ `(t2−P, t2]`,故 **真实时长 ∈ (上报−P, 上报+P)**。

| 档位 | 采样间隔 P | 保证能测到 | **可能整段漏掉** | 边界不确定度 | 额外占空比(T=2s) |
|---|---|---|---|---|---|
| FAST | 2.0 s | ≥2 s | <2 s | ±2 s | +5.0% |
| MED | 6.0 s | ≥6 s | <6 s | ±6 s | +1.7% |
| **SLOW(选定)** | **30.0 s** | **≥30 s** | **<30 s** | **±30 s** | **+0.33%** |

**两个后果,报告必须诚实印出来,代码里不许假装边界精确:**
1. 一条 20 s 的瞬断**有可能两个采样点都采到"联网中",报告里完全看不出来**。
2. **裁决 3 的 WARN 封顶会跟着一起漏触发** —— 测不到的中断自然也封不了顶。

这不是可以修的缺陷,是"SLOW 档 + 封顶 WARN"这套组合的**固有代价**;写明分辨率就是全部的缓解手段。落地:`freeze()` 带 `resolution_s`(=30.0,由 `wifi_resolution_s(t_ms, ticks_per)` **推导**而非硬编码,这样改 `WIFI_TIER` 不会留下过期数字)、`ROW_NOTES["wifi"]` 写死不确定度、HTML `六、` 小节印中英免责说明。

#### 12.10.5 报告里的三处落点(因为 `capped_by` 渲染不出来)

`-3` 伪门进 `caps` 不进 `gates`,所以 `ids == [1..9]` 的 selftest 断言不受影响。**但 `capped_by` 全仓无人消费**(见 §12.2 的空洞说明),所以封顶的**原因**必须显式渲染:

1. **`WIFI` 行的值本身带摘要**(纯 ASCII、单行、不含 `report`):正常 `LINK UP  ssid=... rssi=-64dBm span=0.0s seen=100/100 res=30.0s`;中断过但已恢复 `LINK RECOVERED last=<HH:MM:SS> n_outage=1 span=60.0s seen=4/4 res=30s`;当前仍断 `LINK DOWN(off) ...`;一次都没采到 `LINK ? no wifi observation in this run`(**这个分支必须非空**,否则"每行都有值"的既有断言会挂)。三种"断"的抬头是分开的 —— 否则一次恢复了的跑会渲染成 `LINK DOWN(ok)` 这种自相矛盾的行。
2. **HTML `六、WiFi 链路时间线`**(追加在小节五之后,**不重编号 一~五**):表头 `# / 开始时刻 / 结束时刻 / 时长(s) / 持续拍数 / 起始状态 / 恢复状态`;墙钟用 `_hms(clock_ms)`,完整日期进 `title=`(与第五节同一约定);未闭合窗口渲染 `(运行结束时仍未恢复)`,**绝不渲染裸 `None`**;小节标题带 `采样分辨率 30 秒,边界 ±30 秒`;`WIFI_ZH` 把状态词译成中文;中断时另起一行写明"本次运行因 WiFi 中断被封顶为 WARN";末尾印 `WIFI_RESOLUTION_NOTE` 免责说明。
3. **窗口列表有界**:`WIFI_MAX_WINDOWS = 200`,超出即置 `truncated` 并在 HTML 里自述("仅列出前 200 段");**但 `down_ticks` / `down_s_span` 这类汇总计数不受这个帽影响** —— 帽只作用于明细列表,不许把统计也一起截断。

**时长只有一个定义。** ASCII 行、HTML 每行、HTML 标题三处必须印同一个数:`wifi_window_span(w, last_t)` → evidence 字段 `down_s_span`。曾经出现过 `down=60.0s` 与 `≥300.0` 并存的版本(一个按 `down_ticks × 分辨率` 算,一个按窗口起止算),**读者只会当成 bug 或者各取所需** —— 已删除 `down_s_est`,只留这一个来源。未闭合窗口的时长按"到 `last_t` 为止"算,因此前缀 `≥`。

### 12.11 NTC 节点温度监控(v2.13.0,2026-09-20)

**要解决的问题**:9 个 metric 里没有一个能反映 LCD / LED 节点的热状态,而温度恰恰是长跑压测里最先出问题的物理量。**目标产出**:报告里多一行 NTC 事实行(周期平均 + 最高),CSV 里多三列供 Excel 画曲线。

用户原话(2026-09-20):

> 新增 NTC 节点温度数据监控
> `LCD_Path = /sys/bus/iio/devices/iio:device0/in_voltage3_raw`
> `LED_Path = /sys/bus/iio/devices/iio:device0/in_voltage2_raw`
> cat 出来后的数据不是摄氏度,需要换算
> NTC 温度监控不作为 gate,不做卡控,纯监控,曲线绘制建议不做到前端渲染,前端渲染的先仅支持当前项,图表渲染走 excel 绘制
> overall result 需输出简要信息,周期平均温度,最高温度,无需太过复杂,只是一个单纯的监控项

**用户四条已裁决(不得再改):**

| # | 议题 | 裁决 | 理由 / 代价 |
|---|---|---|---|
| 1 | 换算公式 | **"公式后面再给你,先按 raw 值做"** —— 全部落原始 ADC 计数,只留一个换算点 | 见 §12.11.2;代价是"温度"这个词在 v2.13.0 的报告里**不准确** |
| 2 | 采样档位 | **SLOW 档 ≈ 30 s**(`NTC_TIER`,与 WIFI / FOCUS 同档) | 与 WIFI 同档,换档只是改一行 |
| 3 | 是否参与判定 | **不做门、不做卡控、不进 `caps`** —— 与 WIFI **刻意不同** | 温度的"坏"要先有公式和阈值,而公式没有;WiFi 的"坏"语义是明确的 |
| 4 | 曲线画在哪 | **前端不画,走 Excel** | 前端 `PERF|` 采样行会带上 `ntc_lcd`/`ntc_led` 两个键,但**不加 series** —— 前端图表只按已知键建 series,新键被忽略、不报错、不 resize |

> **同日后续(2026-09-20)**:公式拿到了,并裁决**不落在采集脚本里** —— 换算搬去了独立的 `tools/ntc_convert.py`,每个项目一份参数的 profile。上表第 1 行那句"只留一个换算点"因此被修订为"换算点在**下游工具**里"(§12.11.2,决策 [D-63](DECISIONS.md))。第 2/3/4 行**不受影响**。

#### 12.11.1 一个条件通道,不是第 10 个 metric

理由与 §12.10.1 **逐字相同**:`ST_LINE_RE` 的 `{9}`、`st` 拼接、CSV 列块、分布表、定宽控制台行全部结构性耦合在 9 上;NTC 是**被测对象的观察**,不是被测对象本身。落点:CSV 三列(见 §12.2)、evidence **顶层 `"ntc"` 键**(与 `memory`/`wifi` 同级,**不进 `metrics`**)、判定块一行事实行(`gids=()` → 恒绿)。

**但它不是 WIFI 那样的状态机。** 温度没有"通/断"这种跳变语义,只有一条连续序列。所以:**不新增任何事件 kind**(不碰 `PERF_EVENT_LEVEL`,不碰 60 s 限流器),**不做封顶**。

#### 12.11.2 换算点**在下游**:采集落 raw ADC,换算归 `tools/ntc_convert.py`(D-63)

`parse_ntc()` 是**每一个 NTC 计数**必经的唯一漏斗,它返回计数,且**只**返回计数 —— 这是采集侧的**契约**,不是待办事项。**换算不写在这个脚本里,并且不会搬进来**:驱动换算的常数(分压电阻 / B / ADC 满量程 / 逐通道补偿)**按项目变**,把某一块板的常数焊进采集脚本,换一块板子就会**静默算错**,而且错得没有任何提示。

换算落在仓库根的 `tools/ntc_convert.py` + `tools/ntc_profiles/<项目>.ini`(裁决与禁止项见 [DECISIONS.md](DECISIONS.md) D-63,用法见 [tools/README.md](../tools/README.md))。它读 `samples.csv`,在**它旁边**写一份 `<原名>.temps.csv` —— **输入永不被修改、永不被移动**。

**为什么不加一列 `ntc_lcd_c`**:换算不在采集链路上,采集侧不产出摄氏度。**为什么原始列名不加 `_raw` 后缀**:归档的每一份 `samples.csv` 里这两列**永远是**计数、语义不会再变(这正是把换算推出去换来的),改名只会让归档无法跨版本对比;单位由 `# ntc_unit=` 表头行声明已经足够(§12.2)。

**⚠ 诚实边界:"温度"这个词在报告里是不准确的。** `NTC` 行的抬头与 `ROW_NOTES` 都写了"单位是 raw_adc、不是摄氏度",判定行的值里也**必须**带 `unit=raw_adc`。已归档的报告**不会被追溯修改**(与既有的"不重渲染归档 HTML"决定一致)。

**但归档的数据不用重跑就能变成摄氏度** —— 这是保留原始计数换来的第二个好处:`python tools/ntc_convert.py <归档的 samples.csv>` 直接产出逐拍摄氏曲线与逐通道统计。代价是历史**报告里的判定行**仍然是计数。

**B 值分两段,与参考实现一致(2026-09-21:D-64 推翻 D-63 的旧裁决)**:厂商的实现 `transformNTCToTemp` **通篇字面量、不读任何配置**,所以板子上跑出的温度只可能来自那些字面量。规则:算出的分压电阻 **> 3588 Ω** 时把 `B` 从 `4010` 换成 `3950`;`3588 Ω` 是一个**温度写成电阻的样子** —— 它正好是 3950 曲线上的 **50.00 °C**(同一算式在 4010 曲线上解得 3532.8 Ω),规则读作**冷于 50 °C 用 3950、50 °C 及以上用 4010**。工具的三个键 `NTC B VALUE COLD` / `NTC B VALUE COLD2` / `NTC B SWITCH R` 承载它,`# params=` 表头行**始终**印 `b_cold=` 与 `b_switch_r=`(缺席写 `none`)。实际差别:LCD 节点 **+0.05 °C**,LED 节点 **0.000 °C**。

**设备上不存在换算依据(已穷举,不是推测)。** 设备 `EE88885D8DEF08004CD1`(EcoPro / MT9676 / iio 驱动 `1c020a00.saradc`):**没有 `in_voltage_scale`**(读它返回 EINVAL);`/sys/class/ktc_projector/ktc_ntc3/` 有 `raw_adc` 与 `temperature`,但 **`temperature` 返回的就是 raw 值**,而且**只有 ntc3(LCD),没有 LED 的厂商节点**;无 hwmon、无 temp/ntc 属性、无厂商守护进程;thermal zone(`mtk-thermal 90000` / `gpu-thermal 83000`)是 **SoC 传感器**、单位毫摄氏度、**不是这两个 NTC**。两个节点文件都是 `-rw-r--r-- root root` —— **权限位是 world-readable,但 SELinux 不给 `shell` 域读;v2.14.1 起改走 `su 0`,见 §12.11.6**。`--probe` 现在会把这份结论打出来(含 `in_voltage_scale` 的**缺失**,因为"没找到"和"没找过"必须能区分),**下一个人不必重跑这一轮穷举**。

#### 12.11.3 两个通道一次 `cat`,`ntc_st` 只有一个

`NTC_CMD = timeout -k 1 2 su 0 cat <LCD_PATH> <LED_PATH>`(**v2.14.1 起加了 `su 0`**,理由见 §12.11.6;`su` 本身在这条命令上不打印任何东西,所以帧标记不受影响) —— **一次读取就是一份不可分割的观测**。因此 `ntc_st` **只有一个**:新鲜度是**命令级**属性。拆成 `ntc_lcd_st`/`ntc_led_st` 只会让人以为两个通道能各自新鲜。取值 `f/h/n/x/a/d`,`b` **永不出现**(温读是绝对量,没有基线概念,与 `wifi_st` 同一纪律)。

**已知代价(记进 [TODO.md](../TODO.md))**:段内**没有 per-channel 标记**,所以"只有一个文件读成功"时**看不出是哪一个**。在这种半失败下表现为"一列永久空 + 该通道 `n=0`",**而不是** `a` 状态。用户要求"无需太过复杂",因此不加标记机器。
**这个代价不传导到结论**:两列本身是分开的(哪一格空就是哪个节点的锅),而 `tools/ntc_convert.py` 另外输出 `ntc_lcd_st` / `ntc_led_st` 两个**逐通道**换算状态列,在派生文件里把归因补齐(§12.11.2)。

`NTC_CHANNELS = ("lcd", "led")` 的顺序 **== 两个路径被 `cat` 的顺序,不许重排** —— `parse_ntc` 按行号对位,重排会让 LCD 的数进 LED 的列,而且**看不出来**。

#### 12.11.4 报告落点:只有一行,没有 HTML 小节

用户明说图表走 Excel、只要"周期平均 + 最高",所以 **NTC 不新增 HTML 小节**;它会**自动**出现在 HTML 的逐项判读表里(两张表都从 `verdict_rows()` 渲染)。`--selftest` 逐字断言这一行**非空、单行、纯 ASCII、不含 `report` 字面量**(与 WIFI 行同一组约束,因为 `_sniff_report_path` 会被多行候选骗到)。

> **"图表走 Excel"这句在 2026-09-21 失效了一半**:判定行与 HTML 这一节**都不变**(上面整段仍然成立),但离线工具**另出了一张 PNG 曲线图**,而且跑完**自动**产出(§12.11.5)。报告里依然没有温度曲线 —— 曲线在归档目录的独立文件里。

#### 12.11.5 跑完**自动换算**并出图(v2.14.0,2026-09-21)

**要解决的问题**:[TODO.md](../TODO.md) §6.19 —— 跑完一次压测,归档里只有原始 ADC;要摄氏度和曲线得自己记得跑一条 `python tools/ntc_convert.py <那个 samples.csv>`。用户实测走了一遍这条路,直接问"换算后的在哪里"。

**落点**:`Monitor.finish()` 的**最末尾**(`_write_reports` 之后、`return` 之前)调 `Monitor._convert_ntc()`,它 subprocess 调 `tools/ntc_convert.py <本次 samples.csv> --profile <ntc_profile>`,产出 `<stem>.samples.temps.csv` 与 `<stem>.samples.temps.png`,与 `samples.csv` **同目录** —— 即 D-63 钉下的那条输出契约,**没有第二处路径推导**(路径从工具自己的 `wrote` / `chart` 行回显,不在这里拼)。

**触发条件只有一个:`Evidence.ntc_fresh > 0`。** 取消勾选时 `note_ntc` 根本不被调用,所以"没勾选"和"一次都没读成功"两条路落在**同一个**计数器上 —— 少一个判据就少一处漂移(PITFALLS #51 的形状)。

**为什么放在报告之后**:报告已经写完并落盘,这一步**无论如何都动不了本次结论**。工具缺失 / 超时 / 拒绝 profile,运行**照常结束**,只多一行 `[ntc] no conversion: <原因>`。这与 §12.11.1「NTC 不开门」同向 —— 温度从来不是判定对象。

**控制台输出**:只回显工具自己的 `profile` / `source` / `wrote` / `chart` / `[warn]` / `[error]` 行,每条一行、前缀 `[ntc] `、**全 ASCII**(D-07);工具那张统计表**不回显**。**`" : "` 必须被去掉**:工具原文 `wrote   : <路径>` 里的路径含 `reports/stress-test/perf/`,于是 `" : "` 与 `report` 会同时出现在一行里 —— 正是 `_sniff_report_path` 要找的形状。报告路径仍是 stdout **最后一行**(D-28 不变),`--selftest` 逐条断言这一点。

**`ntc_profile` 参数**:`PARAMS` 里的 `select`,选项由 `_ntc_profile_choices()` 扫 `tools/ntc_profiles/*.ini`(裸名、非递归、跳过 `_`),默认 `9660_P53_2G`。**非递归是故意的** —— `templates/` 里那份厂商转录档没有冷端 B,拿它换算这块板子会**静默**按旧规则算。工具**同时**收紧了默认档规则:目录里**恰好一份** `.ini` 时才敢用默认档,**两份及以上 `--profile` 变必填**、不填报错并列出可选项(裁决见 D-66)。

**代价(明写)**:matplotlib 冷启动让收尾多花几秒(预算 120 s),任务卡多转一会儿。**硬停(SIGKILL)那条路径不触发换算** —— `finish()` 根本没跑,归档里只有原始 ADC;手工补法与"为什么不做自动兜底"记在 [TODO.md](../TODO.md) §6.21。

**归档这一环要单独动一处**:图的文件名 `<stem>.samples.temps.png` 前缀合格,但 `server.py` 的 `_companion_files` 后缀白名单原本是 `(".csv", ".html")`,**不放宽这个图就进不了 `archive/`** —— 它会永远留在 `reports/stress-test/perf/` 里而归档看起来一切正常(安静丢失,PITFALLS #54 / D-67)。

#### 12.11.6 读不到值:v2.14.0 在真机上 NTC **恒为 off**(v2.14.1 修,2026-09-21)

**症状(用户报的)**:同一台设备上手工 `cat /sys/bus/iio/devices/iio:device0/in_voltage3_raw` **能出数**,但脚本跑起来 NTC 是关的、没有 ADC 值、**控制台上没有任何解释**。

**根因两条,缺一不可:**

1. **裸 `cat` 读不到。** 两个节点的权限位是 `-rw-r--r-- root root`(所以 §12.11.2 那句 "world-readable,不需要 `su`" 关于**权限位**的描述是对的),但本板 SELinux 是 **Enforcing**,节点标签 `u:object_r:sysfs:s0`,而 `adb shell` 落在 `u:r:shell:s0` —— **该域被拒绝**。`su 0`(域 `u:r:su:s0`)可以。
   **这条旧结论是怎么来的**:§12.11.2 说"实测不需要 `su`"时,那台设备的 `adbd` 正好是 **root** 在跑(`adb shell` 直接落 root 域),于是量出了"裸 `cat` 可用",并被推广成了设备属性。**它其实是当时的会话状态** —— 设备一重启 `adbd` 就回到 `shell`,NTC 静默失效。这正是 PITFALLS #55 的形状。
2. **失败被丢掉了。** `_probe_raw()` 只取 stdout。被 SELinux 拒绝时 `cat` 把 `Permission denied` 写进 **stderr**、**stdout 零字节、rc≠0**,于是探针打印一行空的 `(empty)` 和一个裸 FAIL,**没有任何原因可查**。

**修法**(D-68):

- `NTC_CMD` 与 GPU 那条路对齐,写死 `su 0`:`timeout -k 1 2 su 0 cat <LCD> <LED>`。**不是**因为权限位,而是因为 SELinux 域。
- **代价**:`su 0` 单次读中位数 **139.5 → 200.1 ms(`+60.6 ms/拍`)**;SLOW 档一拍一次 ⇒ T=2.0 约 **+0.10 pp** 占空比。**`NTC_GUARD_S` 不跟着设备 guard 走**(GPU 那条路跟),理由:这个 guard 只在驱动卡死时才会咬到,而 +60.6 ms 远不触及它。
- **代价二(明写,是这次接受的取舍)**:设备**不能 root 就丢 NTC**(降级关闭,和 GPU 一样)。旧行为是"免 root 但实际读不到",新行为是"要 root 但真的读得到"。
- `_probe_raw()` 在 **stdout 为空**时才去看 stderr,打印成**一行、纯 ASCII**:`[probe] ntc : (empty) - cat: ... Permission denied`;stderr 也空时打 `no output and no error - rc=N`。**只在为空时看** ⇒ 正常的通道仍只有一行摘要,不会变吵。
- **同一形状的第二处,一并修了**:`--probe` 里"记下我们找过 `in_voltage_scale`"那句诊断读(`_probe_raw(serial, "ntc scale", ...)`)原本也是裸 `cat`。改完之后它印出的**新 stderr 行**是 `Permission denied`,而**紧跟其后的结论行**是 "the ADC -> Celsius factor is NOT on this device" —— 上一句说"文件在、你没权限",下一句说"文件不存在",**互相矛盾,而且前一句是假的**(以 root 读,真实答案是 `Invalid argument`/EINVAL,与 §12.11.2 记的一致)。也改成 `su 0 cat`,两行现在说的是同一件事。**这是本条的判据 ③ 自己抓出来的第二个实例**:诊断行必须说真话,不然它会自己长出一个错误的结论。

**为什么不能只靠文档**:这条错误的代价不是"少一个指标",是**读者会以为设备不支持**。所以修法必须同时让**失败说得出原因** —— 一个能自己解释失败的探针,比一条写在 README 里的前提条件可靠。

---

#### 12.11.7 曲线被采样点抹掉:长跑**不打点**(v2.14.2 修,2026-09-22)

**症状**:归档 `archive/perf/20260921-180514_perf_monitor_435E54A1BB0D08004C10/` 的
`*.samples.temps.png` 上**没有曲线**,只有几个孤立的点、和糊在一起的一团标记;同一份
`*.samples.temps.csv` 数据完全正常。

**根因是剂量,不是数据**。那一次 **14.5 小时 / 1737 个读数**(52094 s,30 秒一个),绘图区宽 1468 px
→ **一个点 0.81 px**;而每个点带一圈 `markeredgewidth=2` pt(**4.17 px**)的底色描边
(`markeredgecolor=surface`)。那圈描边是 dataviz 规范要求的(重叠标记加 2px 底色环,压在线上也不糊),
**但它只在标记分得开的时候成立** —— 0.81 px 间距下相邻标记完全重叠,描边从"分隔"变成"橡皮擦",
把底下的**台阶线**连同邻居的点面一起涂掉。

**判据与落点**(`tools/ntc_convert.py` 1.3.1,见 D-69):阈值 `DOT_MIN_GAP_PX` = `MARKER_PT + MARKER_EDGE_PT`
= 10 pt = **20.83 px**(一个点的**外沿**直径 —— 描边骑在边界上,向内吃一半、向外占一半);
`px_per_second()` 从**数据跨度 + 版面常数**算(**不从** `ax.get_xlim()`:画点发生在任何 artist 之前,
那时坐标轴还没有数据范围,读到的是默认的 `(0,1)`);`dots_legible()` 拿该通道真实读数的**中位**间隔比它。
判不过 → 该通道**整个不打点**,副标题追写「采样点密过屏幕像素,未画点」。**逐通道**判定。

**实测(按系列色统计绘图区内的像素,LED 通道)**:

| 画法 | 轴内彩色像素 |
|---|---|
| 只有台阶线 | 11930 |
| 只有点(ms=8,底色描边 2pt) | 1845 |
| **线 + 点(改动前)** | **1974** |
| 线 + 点(去掉描边) | 39704 |
| 线 + 点(每 40 个取一个,32 px 间距) | 11729 |

**这不是"数据丢失"**:读数、状态、温度在 `*.temps.csv` 里一个不少;副标题仍然报**中位**采样周期
(`每 30.0 秒一个真实读数`)。丢的只是"哪一拍是真实读数"这个**视觉**提示,而它在 0.81 px 的密度下
本来就不可读。**如实记下的代价**:30 秒档下约 **40 分钟以上**的跑图上只有台阶线,看不到采样点;
**短跑的点一个没少**(10 分钟 / 20 个读数的图实测照旧每拍一个点)。

**既有断言为什么全绿**:坏的是**光栅化那一步** —— 数据对(`t_sec` 单调无重复、零空单元格、
`ntc_st` = `h:24310 / f:1737`)、artist 对(`get_xlim()` / `get_ydata()` 逐项核对过),所以
"断言数据 / 断言形状"的测试全都发现不了。新增的断言**读回 PNG 数像素**:一条钉住"14.5 小时的线还在"
(38388 px),一条**故意把点强行打开**(227 px,169 倍塌陷)—— 后者就是那个 bug 本身。

### 12.12 单次运行的监控项勾选(v2.13.0,2026-09-20)

**要解决的问题**:每次跑都必须把 9 个 metric + 两个条件通道全采一遍,想省开销、或某台设备根本没有某个节点时无法裁剪。

用户原话(2026-09-20):

> 后续 perf_monitor 需提供监控指标的取消和选中
> 单次运行可选择哪些需要监控,哪些不需要

**可取消集合与理由:**

```python
MONITOR_GROUPS = [("mem","内存",("MEM",)), ("gpu","GPU 占用",("GPU",)),
                  ("fg","前台应用",("FOCUS","FGPKG","PIDSTAT")),
                  ("wifi","WiFi 链路",(SEC_WIFI,)), ("ntc","NTC 节点温度",(SEC_NTC,))]
```

**FAST 三项(`cpu` / `procs` / `up`)不在这个表里,而且必须永久不在。** 理由不是"省事":`up` 既是 reboot 守卫**又是** `cpu%`/`gpu%` 差分能跨重启存活下去的唯一原因,而 STAT / PROCS 是每一拍的骨架 —— 去掉它们省不了多少(FAST 只占 FULL 拍的 ~30%),却会**悄悄毁掉所有差分**。**禁止把可取消粒度下探到 metric**:`gpu` 与 `gpu_clk` 不能被分开取消(设备侧一次 `su 0 cat` 读两个文件),`fg_pkg`/`fg_pid`/`fg_cpu` 同理(共享设备侧同一个 `$P` 变量)。**这不是实现偷懒,是设备命令的形状决定的。**

#### 12.12.1 `off_sections` 是唯一的开关量(并修掉了一处已存在的漂移)

改造前,`build_tick_command` 收 `gpu_enabled` / `wifi_enabled` 两个布尔,`section_due` 也各收一份 —— **两个调用点各拿一半参数,而且已经漂移了**:`tick()` 调 `build_tick_command` 时**漏传** `wifi_enabled`,于是 **WiFi 降级后仍然每拍发 `@@WIFI` 命令**(白花几十毫秒),只是解析时被 `due_sec` 丢弃。后果是 §12.1 那句"此后不再发 `@@WIFI` 标记"**是假的**,`--probe-cost` 量到的降级收益也不存在。详见 PITFALLS.md #51。

修法不是打补丁,是**消掉这类 bug 的成因**:把"哪几段不发"变成**一个集合** `frozenset`,`Monitor.off_sections()` 算**一次**,同一个对象同时喂给两个函数。两个调用点从此**不可能各自漂移**。`--selftest` 里那条"对若干 `off_sections` 取值,两个函数对**每一段**的取舍必须完全一致"的断言,就是这次价值最高的一条 —— 它正面挡住的是**谓词写反/参数漏传**这一类(本仓已有先例:一段留了注释的已删功能曾把 `name not in (...)` 写反,**静默关掉整条通道而每道门仍报 pass**)。

#### 12.12.2 失去证据的门**强制 inconclusive**,绝不静默 pass

**这是本节存在的意义。** 内存(`mem`)是**门 7 MEMORY** 的证据源;前台应用(`fg_*`)是**门 8 CPU / 门 9 APP** 的证据源。而取消勾选之后,现状代码会**静默判 PASS**:

- `fg_due < 2` → `gate(8, …, "pass", "only 0 foreground read(s) - none expected")` —— **这个分支分不清"没勾选"和"跑太短"**;
- 未设 `watch_pkg` 且前台被取消 → 门 9 以 `switches=0 pkgs=0` **静默判 PASS**。

即:报告会声称一个**从未被采集**的通道是健康的。修法(唯一动 `judge` 的地方,约 6 行):

```python
MON_GATE_OF = {"mem": (7,), "fg": (8, 9)}
```

在**九道门全部建好之后、`worst` 计算之前**按 id 覆盖:`status` 改 `"inconclusive"`,`detail` 写明原因(「monitor 'fg' was switched off for this run - no evidence was collected, so this gate cannot be judged」)。`INCONCLUSIVE` 在 `worst` 里优先级最高 → **本次结论变 INCONCLUSIVE**。这是**故意的**:取消了一个门依赖项,报告就不该假装它通过。

**门 id 仍是 1..9**,所以每一条 `range(1, 10)` 断言与整个门表契约不受影响。**不新增伪门**:`capped_by` 全仓无人渲染(§12.2 的空洞),再加一个没人看的伪门只是自欺 —— 范围事实由 `SCOPE` 行 + 被覆盖门的 `detail` 两处承载。

> ⚠ **`freeze()` 里 `monitors` / `monitors_off` 必须写在 `run` 字典**内部,因为 `judge()` 和 `_display()` 只从 `run` 读。本功能开发中出现过一次**真 bug**:这两个键被放在了 evidence 的**顶层** —— 报告照样渲染、`SCOPE` 行照样在、所有不喂数据给它的断言照样通过,而**取消勾选在真实运行里从未生效、`SCOPE` 永远写"全开"**。`--selftest` 现在钉的是这个键的**层级**,不只是它的存在。

#### 12.12.3 `SCOPE` 事实行与可逆性

- 全开:`SCOPE all <n> monitor(s) enabled (nothing was deselected for this run)`
- 有取消:`SCOPE off=<ids> (<n> monitor(s) not collected - their columns are empty by CONFIGURATION, not by failure)`

**纯 ASCII,永不为空**(`gids=()` → 恒绿)。它是**归档自述的关键**:一份几十年后的 samples.csv 里"这一列全是空"分不清是**被取消**还是**全程读失败** —— 两者在 CSV 里都是空 + `d`/`x`,而 `d` 的既有语义是"config 或降级",单看 CSV 分不出是哪一种。`# config_monitors=` 表头行(§12.2)是同一个自述的第二处。

**参数解析容忍两种形状**:`--params '{"monitors":"gpu,ntc"}'`(逗号串)与 `["gpu","ntc"]`(数组)都收;未知 id **被丢弃、绝不致命**(手写的 `--params` 不该能中断一次运行);输出顺序**强制为 `MONITOR_GROUPS` 顺序**,否则报告里"本次取值 == 默认?"的比较会被 DOM 顺序打败。空列表是**合法值**(只跑 FAST 三项)。

**已实测的可逆性**(2026-09-20,设备 `EE88885D8DEF08004CD1`,T=2.0,120 s):取消「前台应用」→ 命令里**没有** `@@FOCUS/@@FGPKG/@@PIDSTAT`,CSV 三个 `fg_*` 列 61/61 全空且状态全是 `d`,门 8/9 判 `inconclusive`,`RESULT=INCONCLUSIVE`,`SCOPE` 写 `off=fg`;**再全选重跑 → 门 8/9 回到 `pass`、`SCOPE` 写全开、三个 `fg_*` 列全部恢复有值**,无残留状态。

---

本文只写 `perf_monitor` 的判定模型与采样规格;平台机制见 [ARCHITECTURE.md](ARCHITECTURE.md) 与 [START-HERE.md](START-HERE.md),报告与 HTML 的渲染契约见 [REPORT_FORMAT.md](REPORT_FORMAT.md)。改本文时**只要 §12.x 的编号与 §12.7 表中第 8 条不动**,`scripts/perf_monitor.py` 的注释引用就一直有效。
