# perf_monitor v2 — 总体架构设计

> 状态:**设计已定稿,实现中**(P0/P1 已发布为 v2.7.5 / v2.7.6)。
> 所有数字均为 2026-09-14 在 MT9676(`B0403374A2A508001F00`)上的实测值。
>
> ⚠ **权威顺序**:§3–§5 是设计与复核过程的记录(保留审计线索);**§12 是定稿规格,
> 与 §3–§5 冲突时一律以 §12 为准**。§11 记录复核发现,§12 记录结论与未决项。

---

## 0. 结论摘要

| | 现状 | v2 | 依据 |
|---|---|---|---|
| 单个采样点墙钟 | **876.5 ms** | FAST **134.0** / FAST+MED **243.1** / FULL **408.7** ms | **直接实测 v2 复合命令**(2026-09-14,7 次取中位),非分项相加 |
| 2 秒间隔下占空比 | **43.3 %** | **9.07 %** | 按 `MED=T*ceil(5/T)`、`SLOW=T*ceil(30/T)` 的 tick 混合算 |
| 加速比 | — | **4.8×** | 同上 |
| 判定 | **完全没有** | 增量证据 + 收尾判定 + 中途事件 | |
| 归档产物 | 日志 + report.json | 日志 + `samples.csv` + `report.json` + `events.csv` | |

> **口径演进(三次修正的记录,别再用旧数)**:最初 6.4%/6.7× 是**分项相加推导**,口径不成立;
> 改成直接测**旧命令形状**得 7.15%/6.1×;最后按**定稿的 v2 命令形状**(带 `@@UP` 段、`@@FGPKG` 段、
> 一次 su 读两个 GPU 文件、`@@DONE` 尾标记)重测得 **9.07%/4.8×** —— 这才是要实现的形状。
> **归根到底:总量只认"要实现的那个命令形状"的直接实测。**

> **口径说明(2026-09-14 复核修正)**:上表数字是**直接测出来的三条复合命令**,
> 不是把单指标读数相加。本文早期的 6.4% / 6.7× / 82.4 / 194.3 是**分项相加推导**的,
> 复核证明该口径不成立(分项各自含 adb 固定开销,相加会重复计数)——
> **凡是"总量"类数字只认复合命令的直接实测,禁止再由分项推导**。
> 设备 CPU 一栏已删除:原文的 255→30.9 ms/s 两个数都没有实测来源。

三句话:
1. **采样要分级** —— 不是所有指标都需要每拍读。CPU 每拍,内存/GPU 每 6 s,前台应用每 30 s。
2. **判定要分层** —— 有些证据**事后无法恢复**(null 的成因、增量基线、事件时刻),必须当场累积;其余(趋势、分位数)收尾算更准。
3. **算可以随时算,看只在末尾看** —— 判定结果只在收尾输出一份;中途只输出**事件**,不输出结论。

---

## 1. 现状定位(实测)

当前 [perf_monitor.py](../scripts/perf_monitor.py) 每拍发**一条复合 adb 命令**,实测分项:

| 命令 | 墙钟 | 备注 |
|---|---|---|
| adb 进程 + transport 固定开销 | 110–140 | **每拍固定**,与命令大小无关 |
| `cat /proc/stat` + `cat /proc/meminfo` | 170–192 | 单读 |
| `su 0 cat dvfs_utilization` + `gpu_clock`(分两次) | 251.6 | 单读 |
| `dumpsys window \| grep mFocusedApp` | 184–196 | 单读 |
| **`top -n 1 -b`** | **581.6** | **单读,最大的一项** |
| **今天整条复合命令(实测)** | **866.5** | **← 只认这个数** |

> ⚠ **上表是"孤立单读",相加得不到 866.5** —— 每条单读都各自含一遍 110–140 ms 的 adb 固定开销,
> 加性口径会重复计数(相加约 1297 ms)。**总量只认最后一行的直接实测。**

两个结论:

- **`top` 一项就占了 67% 的墙钟。** 它是最大的一刀。
- **`interval_sec` 的 `min=0.5` 是物理上达不到的谎**:单拍 863 ms > 500 ms,`remaining` 恒为负 → 永不 sleep → 背靠背满速跑,而 UI 从不告诉用户。

另外实测确认的三条硬约束(影响设计):

| 事实 | 后果 |
|---|---|
| 全系统 `dumpsys meminfo` = **7433–7546 ms** | **禁止进入采样路径** |
| `dumpsys meminfo <pid>` = 426–604 ms | 只能低频(30 s) |
| `cat /proc/<pid>/smaps_rollup` = **Permission denied**(SELinux) | **拿不到 PSS**,不要设计依赖它的指标 |

---

## 2. 架构:三层职责

```
┌─ 脚本层 (scripts/perf_monitor.py) ───────────────────────────────┐
│  采样:分级 tick,每拍一条复合 adb 命令                            │
│  证据:增量累积器(事后不可恢复的信息)                              │
│  判定:纯函数 judge(序列, 事件) → verdict dict                    │
│  输出:每拍一行 PERF|sample + 事件一行 PERF|event + 收尾判定块      │
└──────────────────────────────────────────────────────────────────┘
        │ stdout(平台逐字转发,server.py 零改动)
        ▼
┌─ 前端层 (static/app.js) ─────────────────────────────────────────┐
│  新增两个 handlePerfLine 分支:sample(定宽渲染) / event(一行事件)   │
│  不做任何判定,不新增 badge/状态条                                 │
└──────────────────────────────────────────────────────────────────┘
        │ 收尾
        ▼
┌─ 归档层 (archive/<模块>/<时间>_<脚本>_<设备>/) ──────────────────┐
│  stdout.log / logcat.log / serial.log(ECharts 图 chart.png)      │
│  samples.csv / report.json(含 verdict) / events.csv              │
└──────────────────────────────────────────────────────────────────┘
```

**铁律不变**:脚本 stdout 全 ASCII、依赖只用标准库、脚本能独立 CLI 跑、日志采集归平台。

**显示边界(2026-09-14 用户拍板)**:离线区间、采集源禁用、pid 变化这类**事实**允许作为事件行进 **stdout** ——
"stdout 没那么局促"。但**其它地方照旧禁止**:不加横幅、不加 toast、不加状态条、不加离线专用面板;
设备卡的「临时离线」合成卡仍是离线状态的 UI 表达。这是对 v2.7.2 决策 #0.3 的**窄口径覆盖**,不是撤销。

---

## 3. 采样层

### 3.1 分级排程(核心改动)

三个层级,**同一时刻到期的层合并成一条 adb 命令**(现设计已有此能力,只是今天所有层都绑在 FAST 上):

| 层 | 周期(T=2 s 时) | 内容 |
|---|---|---|
| **FAST** | 每 T(默认 2 s,新下限 **1.0 s**) | `cpu`、`procs` |
| **MED** | `MED*ceil(5/MED)`,T=2 → 6 s | `mem`、`gpu`、`gpu_clk`、**`up`** |
| **SLOW** | `MED*ceil(30/MED)`,T=2 → 30 s + **事件突发** | `fg_pkg`、`fg_pid`、`fg_cpu` |

> **周期用嵌套式 `SLOW = MED*ceil(30/MED)`**,不用 `T*ceil(30/T)`:后者在 T 为小数时不保证嵌套,
> 会出现"SLOW 到期但 MED 未到期"的畸形拍。

**两个 2026-09-14 新增的字段(复核发现判决块要它们,但原设计没有数据源)**:

- **`up` = 设备开机时长**(`cat /proc/uptime` 第一列,秒)。**为什么必须读**:它是**判断设备是否重启过的最便宜手段** ——
  相邻两拍 `up` 明显变小就是这个设备刚重启过。不读它有两个后果:① 判决块里"这次跑设备重启过 1 次"无法写出;
  ② 更要紧的是**重启会把 `/proc/stat` 和 GPU 计数器清零**,而 `cpu%`/`gpu%` 是**差分算出来的** ——
  计数器倒退时差分是负数或垃圾值。今天的代码只有一个不完整的守卫(`if st[1] > cpu_baseline[1]`,只在 idle 前进时才计算),
  **不报错也不记录**,读数是悄悄偏掉的。成本 ~2-4 ms,并入 MED 拍。
- **`fg_pid` = 前台应用的进程号**。**为什么必须有**:`fg_pkg`(前台包名)消失**不等于应用死了** ——
  导航、屏保、OSD 提示都会抢焦点,那是正常的。要区分"应用真的崩了"和"焦点被抢走了",**唯一可靠的证据是进程号消失**。
  而且这个值**几乎是免费的**:`fg_cpu` 本来就要 `cat /proc/<pid>/stat`,pid 已经在手上,只是今天没有把它记下来。

**先看真正要用的数(2026-09-14 实测,每变体 5 次取中位,一次 = 一个 adb 进程 + 一条复合命令)**:

| 变体 | 中位墙钟 | min | max | 输出字节 |
|---|---|---|---|---|
| **今天(每拍)** | **866.5 ms** | 855.5 | 874.2 | 2786 |
| FAST only | **100.8 ms** | 96.4 | 111.1 | 713 |
| FAST+MED | **207.9 ms** | 195.0 | 226.6 | 2089 |
| FULL(FAST+MED+SLOW) | **305.6 ms** | 304.3 | 339.2 | 2421 |

按 `MED=T*ceil(5/T)`、`SLOW=T*ceil(30/T)` 的 tick 混合:

| T | MED | SLOW | tick 混合 | 占空比 | (今天) |
|---|---|---|---|---|---|
| 1.0 s | 5 s | 30 s | 24 FAST + 5 MED + 1 FULL | 12.54% | 86.6% |
| **2.0 s** | **6 s** | **30 s** | 10 FAST + 4 MED + 1 FULL | **7.15%** | **43.3%** |
| 6.0 s | 6 s | 30 s | 0 FAST + 4 MED + 1 FULL | 3.79% | 14.4% |

**注意 T=1.0 s 档的占空比是 12.5% 而不是 2s 档的一半** —— tick 更密但每条命令的成本不变。

下面是**分项孤立单读**,用来判断"哪一项贵",**不可相加**(每条各自含一遍 adb 固定开销):

| 指标 | 命令 | 墙钟 | 设备 CPU |
|---|---|---|---|
| cpu | `echo @@STAT; cat /proc/stat` | 79.2 | 25.5 |
| procs | `set -- /proc/[0-9]*; echo $#` | ≈0 边际 | 3.0 |
| mem | `echo @@MEM; cat /proc/meminfo` | 79.7 | 25.0 |
| gpu + gpu_clk | `echo @@GPU; timeout -k 2 5 su 0 cat .../dvfs_utilization .../gpu_clock` | **129.9** | 67.0 |
| fg | `echo @@FOCUS; dumpsys window 2>/dev/null \| grep -m1 mFocusedApp` | 104.2 | 51.0 |
| fg_cpu | `echo @@PIDSTAT; for x in $(pidof <pkg>); do cat /proc/$x/stat; done` | 125.5 | 60.5 |

**四条实测得来的关键决定:**

1. **`top` 换成 `pidof` + `/proc/<pid>/stat` 差分** —— 墙钟 4.6×、设备 CPU 5.7×,而且**语义更好**:实测同一包连发三次 `top` 得 85.0 / 26.4 / 19.6,瞬时值本身不可信;改用 utime+stime 差分得到的是**区间平均**。
2. **两条 `su 0 cat` 合并成一条**(`su 0 cat 文件A 文件B`)—— 省 **122 ms/拍**。注意 `parse_gpu_clk` 必须从 `re.search(r"(\d+)")` 改成**按行取**独立整数行,否则会读到 `busy_time` 的数字。
3. **`timeout -k 2 5` 必须保留**(toybox 实测支持 `-k`)。PC 侧 `subprocess.run(timeout=)` **只杀 adb.exe 客户端,杀不掉设备侧命令** —— 去掉它等于每拍泄漏一个持 debugfs 句柄的 root 进程,8 h 里不断叠加且平台完全看不见。
4. **MEM 从每拍挪到 6 s**:`MemAvailable` 是分钟级慢变量,6 s 分辨率不丢信息,省 ~8.4 ms/s。

### 3.2 多层同拍 → 零阶保持,不用 null

**这是一个必须避开的陷阱。** 直觉方案是"低频指标读不到就写 null",但实测前端源码([app.js](../static/app.js) `showSymbol:false` + `connectNulls:false`)意味着:

> 6 s 降频下**不存在任何相邻非 null 点对** → **整条 series 从图上消失**(不是空洞、不是虚线)。`MEM` 还是无条件 series → **黄线也整条不见**。归档的 `chart.png` 走同一套代码 → **你长期保存的证据图上有图例、有刻度、一条线都没有**。

**结论**:每拍都带值(**零阶保持**,沿用上一次读数),`null` 只留给**真正读不到**的情况。这样正常降频不出洞、掉线才出洞。代价是报告里的分位数必须在**真实读数**上算,不能在展开后的每拍序列上算(否则被复制的值会稀释百分位)—— 所以报告里每个指标要打印它自己的 `n`。

### 3.3 会话模型:每拍一个新 adb 进程

**显然的优化是错的。** 常驻 `adb shell` 会话(开一次、写 stdin、读回)看起来能省掉每拍的进程启动,但实测把它否掉了:

| 论点 | 实测 |
|---|---|
| 理论上限 | 省 56.1 ms spawn+往返 = 863 ms 的 **6.5%** |
| 公平交替 A/B | 常驻侧**中位数反而慢 8.1%**(均值持平) |
| 故障隔离 | 持久会话是单一串行上下文;**实测一次 `sleep 30` 之后,后续所有请求永久超时且不恢复** —— "丢一拍"升级成"丢一段会话" |
| 平台会杀它 | 用户点"重连"是 `adb kill-server`;设备 watchdog **每 5 s 轮询、对离线设备每 ≥20 s 自动 `adb reconnect offline`** → 会话死亡是自动且周期性的 |
| 组帧污染 | 持久会话里设备的自发输出会落进下一次采样缓冲(**实测注入消息出现在 `@@STAT` 之前**),必须做 nonce 双标记 |
| 无人回收 | 服务端两个 reaper 都匹配不到它(`_reap_orphan_scripts` 要求 cmdline 含 `--device`;`_reap_orphan_logcat` 要求 logcat + `-v threadtime` + `-T 1`) |

> **每拍多付 ~56 ms,买的是一次挂起只损失一拍、用户点重连不打断数据流、不需要组帧。隔离性比 6% 值钱。**

### 3.4 节奏与超时(负反馈的坑)

**不要**从名义 `interval` 派生超时(如 `min(interval*0.8, 10)`):`interval=2` 推出的 1.6 s 会把"设备很忙"误判成"读不到",而收紧预算杀掉的只是 adb 客户端 → **设备侧旧命令继续跑、下一拍立刻再叠一条**,形成"越慢越重叠"的正反馈 —— 正好是本来要防的东西。

**改成**:`budget = max(3.0, 3 × 最近 30 拍实测 p95)`,超时后主动跳到 `now + interval` 并记 timeout 事件。今天的 `sample_timeout = max(30, int(interval)+15)` 意味着**一拍可以吃掉 30 s**,而 `--duration` 与停止按钮只在循环顶部检查。

### 3.5 采样自记账

每拍记录 `cost_ms`(PC 侧 `perf_counter` 实测墙钟)与 `flush_ms`,分开记。**把"监控自己造成的扰动"变成报告里的一等可见数字** —— 否则没法回答"这个监控对设备的影响有多大"。

---

## 4. 判定层

### 4.1 分工:什么必须当场累积,什么收尾算更准

| 判据 | 何时算 | 为什么 |
|---|---|---|
| **每个 null 的成因**(`ok/timeout/offline/no_root/no_app/parse_fail/not_due`) | **必须增量** | 事后序列里只剩一个 null,**原理上不可恢复**。这是增量累积器最不可替代的产物 |
| **delta 基线 + 原始计数器**(total/idle、busy/idle 四个 int) | **必须增量** | `cpu%`/`gpu%` 本身就是增量产物;且不持久化原始计数器,"这台设备中途重启过"在归档里无法事后复算 |
| **事件流**(离线窗口起止、reboot/rollover、fg_change/fg_lost、read_fail、timeout、degraded、long_window) | **必须增量** | 硬杀时它是"那段数据为什么是平的"的唯一解释;计算是 O(1)、对设备零额外成本 |
| 覆盖率与每指标有效率 | 增量 | 硬杀后无法从被截断的序列精确恢复 |
| 1 分钟聚合桶 | 增量 | 让任意时长的内存与报告体量有界(24 h@1 s → 1440 桶 ≈ 200 KB,而不是 61 MB) |
| **临时判读(partial verdict)** | 增量,每 ≤120 s 快照 | **硬杀后归档里仍有一份带 `partial=true` 的判读**,而不是只有原始数据 |
| 精确分位数 p50/p90/p95 | 收尾 | 排序需要全集;流式近似换来的只是用户没要的东西 |
| 趋势/斜率(整段最小二乘) | 收尾 | 需要整个跨度才能回答"是不是在慢慢涨" |
| 瞬时尖峰 + **回落** | 收尾 | 只维护 running max 会丢掉尖峰时刻,更丢掉"回落了没有" —— 而这决定该不该报 |

### 4.2 判定必须是纯函数

```python
verdict = judge(series, events)      # 输入序列 + 事件 → verdict dict
```

输入是纯数据,所以**报告里同时存序列与判定,结论永远可被重算验证**。`judge_version` 与阈值必须写进报告,否则老归档无法用新脚本复算,也无法解释当时为什么判 WARN。

### 4.3 硬规则

- **`INCONCLUSIVE ≠ PASS`。** `n < 5` 或 `span < 30 s`、`coverage < 0.5`、全指标无有效读数 → `INCONCLUSIVE`
- `coverage < 0.8` 或 `partial=true` → 最高只能 `WARN`,不能给 `OK`
- 分母用**实测中位周期**推算的 expected ticks,**不能用用户填的 nominal interval** —— 否则 `interval=0.5` 这种物理达不到的档位会恒定把 coverage 钉在 0.5,让一次完全健康的跑永远拿不到 OK
- **中途出的结论性告警必须带数据充分性**:第 5 分钟说"内存泄漏"是危险的,要能输出 `LEAK? (early, insufficient data)` 这种**不确定态**

### 4.4 R² 不作为门控

参考脚本用 R² ≥ 0.60/0.75 当置信门。**4000 次蒙特卡洛实测推翻了它**:

| | R² 中位数 | 越过 0.60 | 越过 0.75 |
|---|---|---|---|
| 纯随机游走 | **0.45** | **37 %** | **21 %** |
| 真泄漏(噪声量级相当) | 可低至 **0.38** | — | — |

R² 度量的是"这串数有多光滑",**不是"有没有泄漏"**。→ R² 只作诊断字段写进报告,不参与门控。要用必须先真机标定。

### 4.5 明确不做的判据

- **GPU 冻结 / 卡 0** —— 踩坑 #14:本板视频硬解时 GPU≈0 是正确的(VPU + HWC)
- **以 `fg_pkg` 消失判应用死亡** —— 导航/屏保/OSD 都会抢焦点,必须有 **pid 级证据**
- **以 `comm` 字段做进程匹配** —— 内核按 `TASK_COMM_LEN-1=15` 硬截断,实测读到 `3127 (.apps.tv.dreamx)`,永远匹配不上真实包名 → 会让 `fg_cpu` 全程 null → 被判异常 → **给一次健康的 8 h 跑打出假红**。只信 `pidof`
- **Mann-Kendall 趋势检验** —— 内存序列高度自相关,Z 值会被放大成假显著
- **Theil-Sen 直接跑原始序列** —— O(n²),且自相关与毛刺污染斜率(必须先分块取中位数)

---

## 5. 输出层

### 5.1 实时数据流(每拍一行)

定宽单元格,数值右对齐,可直接竖读:

```
[perf] t=    2.0s | cpu= 61.6% | gpu= 35.3% | mem= 67.4% | fg= 39.4% | clk= 552MHz | pkg=com.google.android.apps.tv.launcherx
[perf] t=    6.0s | cpu=     - | gpu=     - | mem=     - | fg=     - | clk=      -                                （掉线）
```

- `pkg=` 放行尾(长度不定,放中间会把后面的列推歪)
- `null` 渲染成同宽 `-` → 掉线时整行是一排横线,**不会被误读成"GPU 真的 0.0%"**
- 丢掉重复墙钟(行首 `[HH:MM:SS]` 由平台提供)

### 5.2 中途事件(允许,但不是结论)

用户已批准中途输出**事件与提示**。切分原则:

| 性质 | 内容 | 中途打? |
|---|---|---|
| 事实 | 采集源被禁用、pid 变化、设备离线区间、重启 | ✅ 立即 |
| 事实 | **采样节奏劣化**(实测间隔被拉长) | ✅ 立即 —— 否则"曲线变稀"会被误当成设备变好 |
| 结论 | 内存泄漏 / 阶跃 / 循环 | ✅ 但**必须带数据充分性** |
| 结论 | 应用死亡 | ✅ 但必须有 pid 级证据 |
| 汇总 | 最终判定 | ❌ 只在收尾 |

**三条落地纪律:**

1. **前端必须学新行类型。** [app.js](../static/app.js) 对未知 `type` 走 `return line` —— 不加分支会把**原始 JSON 直接漏进 console**,而且写进 `stdout.log` 后每次 WS 重连都重放一遍,**污染是永久且反复的**
2. **限流是硬需求。** 参考脚本在这上面栽得很典型:告警 key 里带了 `window_len`,满窗前**每个新长度都是新 key**;而且"先登记后裁剪",被裁掉的那条**既没打印又已登记**。流程必须是 **收集 → 去重 → 截断 → 只登记留下来的**
3. **事件绝不进 server 的 task 对象** —— 会进 `tasksRenderKey`,每次轮询整卡重渲染(踩坑 #23)

### 5.3 收尾判定块

由纯函数 `format_result_lines(verdict)` 渲染,JSON 是唯一真源,stdout 块只是它的一次渲染,两者不可能漂移。

```
=== perf verdict (judge v1) ===
  RESULT  : WARN
  RUN     : ran=28800s  ticks=14400  cost=6.4% duty  interval~2.0s
  DATA    : coverage=0.94  segments=2  offline=187s  gaps=3
  DEVICE  : reboots=1  at t=4120s (uptime 8120s -> 38s)
  MEMORY  : NO LEAK   delta=+0.4%  slope=+0.06%/h  blocks=5/7  n=2390
  CPU     : avg=41.2%  p95=95.1%  max=98.6%  >=90% for 412s (load, not a fault)
  APP     : switches=5  pkgs=3  gone=0  restarts=0
  EVENTS  : 3 (src_disabled x1, pid_change x2)
  report          : <abs path>
```

- **不得含字面 `report`**(除最后那行路径)—— 服务端 `_sniff_report_path` 会扫 stdout 认领报告路径
- 保留现有 summary 行(用户可要求合并)
- `EVENTS` 一行汇总 + 详情在报告里 —— 否则你看到跑过 3 次告警却不知道是哪三次

---

## 6. 归档产物

用户已确认:**归档不受文件类型约束**,唯一要求是"归档在一起 + 统一命名"。当前平台的限制是实现细节,需要小改:

| 位置 | 现状限制 |
|---|---|
| [`_sniff_report_path`](../server.py#L1439) | 候选**必须以 `.json` 结尾** |
| [`_siblings`](../server.py#L1530) | `REPORTS_DIR.rglob("*.json")` |
| [`ARCHIVE_MEMBER`](../server.py#L100) | 只认三个日志名 |

**方案(2026-09-14 定稿):扁平文件,设备名进文件名。**

```
reports/stress-test/perf/
    ├── perf_<dev_short>_<ts>.json        报告(现有形状,加 verdict 对象)
    ├── perf_<dev_short>_<ts>.html        同一份 payload 的中文渲染(v2.9.0 起)
    ├── perf_<dev_short>_<ts>.samples.csv 全分辨率原始序列
    └── perf_<dev_short>_<ts>.events.csv  事件流
```

- **三个文件同前缀同时间戳**,所以"归档在一起 + 统一命名"靠命名约定保证,不靠目录
- 平台侧只需把认领规则从 `*.json` 扩到这三个后缀([`_siblings`](../server.py#L1530) 的 rglob + [`_sniff_report_path`](../server.py#L1442) 的后缀判定),
  **mtime 窗口兜底与 `_CLAIMED_REPORTS` 原样复用** —— 它们本来就是按"文件名含 `dev_short`"设计的,天然适配
- 硬停时脚本没打完声明行,兜底扫描照样能按 `dev_short` + mtime + 后缀把同前缀的三个文件一起收

**为什么不用"一个 run 一个目录"**:复核证明它在今天的平台代码上落不了地 —— `_sniff_report_path` 要求路径以
`.json` 结尾、`_siblings` 按**文件名**匹配设备名、`_archive_task` 对报告走 `shutil.copy2`(传目录会抛异常)、
`_safe()` 与 `_CLAIMED_REPORTS` 都只有文件版。要落地得新增目录级校验/认领/搬移三套机制,而扁平方案
**复用现有全部守卫,只加两个后缀**(2026-09-14 用户选定)。

**为什么仍然不把 glob 放开成任意扩展名**:那会把三重保护(`_CLAIMED_REPORTS` + mtime 窗口 + 设备名前缀)
变成零保护 —— 同设备背靠背跑时前一个 run 的 CSV 会被后一个 run 认领,正是 v2.7.1 修过的那类 bug。
**白名单三个后缀 + 同前缀成组** 才是安全边界。

**`report.json` 必须移除全分辨率 `samples` 数组**:实测 336 B/条(因为每条带 `_cpu_prev`/`_gpu_prev` 两个内部基线数组,且 `indent=2` 把数组展开)→ 外推 61 MB / 序列化 2.4 s,而**收尾写盘期间被二次 force-stop 就整份丢**。换成 1 分钟桶 + 统计 + 事件 + 判读,并改 compact 序列化。全分辨率本来就由平台把 `stdout.log` 收了一份。

---

## 7. 分阶段路线图

| 阶段 | 目标 | 交付 | 工作量 | 风险 |
|---|---|---|---|---|
| **P0 logcat 开关** | 长跑不再被迫背 1 GB logcat | 照 `serial_capture` 的形状加 `logcat_capture`:设备卡 checkbox + 服务端仲裁 + 默认开;版本 bump | 0.5 天 | 低 |
| **P1 可读性** | 实时数据流可竖读 | 只改前端 `handlePerfLine` 的 sample 分支为定宽单元格 + 移到行尾的 `pkg=`;版本 bump | 0.5 天 | 低 |
| **P2 采样瘦身** | 单拍 863 → 82/194/306 ms | 分级 tick、`top` 换 `/proc/<pid>/stat`、合并 su 双读、`budget` 从实测 p95 派生、`cost_ms` 自记账、`interval_sec` 下限改 1.0 | 1.5–2 天 | 中 |
| **P3 证据** | 事后可恢复 | `null` 成因状态、原始计数器落桶、事件流、1 分钟桶、≤120 s 原子快照 | 1.5–2 天 | 中 |
| **P4 判定** | 有结论 | `judge()` 纯函数、`INCONCLUSIVE` 规则、收尾判定块、`--selftest` 合成序列自检 | 2–3 天 | 中 |
| **P5 归档产物** | CSV 进存档 | 平台:目录式认领;脚本:`samples.csv`/`events.csv` | 1 天 | 中 |
| **P6 中途事件** | 事件与预警可见 | 前端 `event` 分支 + 限流 + 三处同一份 | 1 天 | 中 |

**顺序是有意的**:P1 独立可交付(纯前端,改完立刻见效);P2 是收益最大的一步且不引入判定;P3 是 P4 的前提;P6 最后做,因为它依赖 P3 的事件流。

---

## 8. 已拍板(2026-09-14)

| # | 议题 | 决定 |
|---|---|---|
| 1 | `logcat 恒开` | **保留 logcat**,但增加**人工开关** —— 完全照 `serial_capture` 的形状:设备卡 checkbox + 服务端仲裁,默认仍为开。见 P0 |
| 2 | 判定阈值做成界面参数? | **不做**。阈值是标定值,写死在代码里;界面只给 `interval_sec`/`duration_sec` 这类运行参数 |
| 3 | "完成"加浏览器眼验 | 用户**后续实跑确认**,不作为编码阶段的前置门槛 |
| 4 | 开跑前查磁盘空间 | **不做**(用户明确否决)。归档无限增长是已知且接受的代价 → 记 TODO |
| 5 | `perfSeriesAndKeys` 重构 | **同意执行**。仍只服务 `perf`/`battery` 两个 kind,改成由 `meta.sources` 驱动 |

风险项与未验证项统一进 [TODO.md](../TODO.md),不阻塞实现。

---

## 9. 明确不做

- 常驻 adb shell 会话(实测中位数慢 8.1%,且把可隔离故障升级为不可恢复故障)
- 全系统 `dumpsys meminfo`(7.5 s)、每拍 `top`(581 ms)
- PSS / `smaps_rollup`(本机 SELinux 拒绝,拿不到)
- 自采 logcat / `logcat -c` / `setprop debug.sf.fps` / `adb root`(违反平台铁律,`adb root` 还会重启 adbd 打断其它任务的采集)
- FPS(本机无可信来源:mFps 是 RK 私有补丁;SurfaceFlinger `--latency` 的 127 条环缓冲在 60 fps 下只有 ≈2.1 s 历史,与 2 s 采样结构性冲突)
- pandas / numpy / matplotlib / openpyxl(注:**这些库本机其实都装了**,拒绝的理由是设计约束与报告契约,不是环境缺库)
- 自动泛化 `PERF_SCRIPT_KINDS`
- 判定搬去服务端计算(违背"监控逻辑做进我的 perf 监控",且脚本单跑就没有结论)
- **开跑前检查磁盘空间 / 任何形式的自动清理**(用户明确否决;归档永久保留是既定策略)

---

## 10. 风险与未验证

| 项 | 说明 |
|---|---|
| **全部数字是 USB + 设备负载下测的** | load avg ~21/4 核、399–401 任务。**adb over WiFi 完全没测** —— WiFi 下传输基线大幅抬升,`--probe` 必须保留现场重标定入口,文档不得把这些绝对值写成承诺 |
| **多进程 app 语义缺口** | `top \| grep` 能看到 `com.x:remote`,而 `pidof com.x` **不枚举它** → 换数据源后 `fg_cpu` 覆盖可能变窄。本机没有多进程前台 app 可测,**落地前必须在真机上对照验证** |
| **无温度/功耗指标** | 对"8 h 无人值守别把平台搞不稳",SoC 温度可能是最相关也最缺失的信号。本轮没探过 `/sys/class/thermal/thermal_zone*/temp` 在本板是否可读、是否要 root |
| **判定阈值未标定** | delta / slope / 最短观测窗只有一次真机长跑基线才能定。在拿到基线前它们是**设计假设而非实测值**,必须靠 `--selftest` + 一次健康长跑作为 ship gate |
| **硬停无最终判定** | force-stop 时脚本来不及收尾,归档里只有 ≤120 s 前的 `partial` 判读。这是已知边界,必须写进文档否则会被当成 bug |
| **报告同构性** | 旧 `report.json` 没有 `verdict`/新字段,版本间形状不一致,消费方需容忍缺字段 |

---

## 11. 复核发现(2026-09-14,编码前必须先解决)

对本文做过一轮对抗式复核(算术自洽 / 与平台规则冲突 / 可实现性)。**§0 的成本数字已按复核意见改为直接实测**
(见上表);以下问题**尚未修正**,是动手前必须处理掉的。

### 11.1 归档方案(✅ 已解决 2026-09-14:改为扁平文件)

> **结论**:用户选定**扁平文件 + 设备名进文件名**,方案见 §6。原文的"一个 run 一个目录"作废。
> 下面保留问题分析,因为**它解释了为什么目录方案不能用** —— 同一类坑以后还会遇到。

### 11.1-原始 归档目录方案在今天的平台代码上落不了地(阻断)

§6 声称"硬停兜底不需要新机制" —— **这是错的**。事实:

- [`_sniff_report_path`](../server.py#L1439) 要求候选行同时含 `report` **且以 `.json` 结尾** → 目录路径认不了
- [`_siblings`](../server.py#L1530) 用 `REPORTS_DIR.rglob("*.json")`,且过滤条件是 **`dev_short not in f.name`(看文件名,不看目录名)**
- [`_archive_task`](../server.py#L1369) 对报告走 `shutil.copy2(src, dest/name)` → **传目录会抛异常**并被吞进 notes
- `_safe()` 与 `_CLAIMED_REPORTS` 都只有**文件版**,没有目录版

两种读法都坏:按文件路径声明 → 只有 `report.json` 被搬走,`samples.csv`/`events.csv` **永远留在 `reports/`**
(正是 v2.7.3 修掉的那个堆积);按目录声明 → sniffer 返回 `None`,**连 report.json 都认不到**。
而 force-stop 路径下脚本已被 kill,不会打出任何声明行 —— §4.1 承诺的"硬停后仍有 partial 判读"随之落空。

**必须二选一写死,不能留"小改"这种措辞**:
- **(a) 显式目录标签**(如 `artifacts : <dir>`):写明识别规则、用与 `_safe()` 相同的 `REPORTS_DIR` 包含性校验做目录合法性判定、**目录级认领集**、目录搬移原语(rename/copytree,失败记 notes,Windows 目标已存在的处理),并把硬停兜底改成"扫 `reports/` 下的**目录名** + mtime 窗口 + 设备名"
- **(b) 退回扁平文件 + 设备名进文件名**(`perf_<dev_short>_<ts>.samples.csv` 等):mtime 兜底与 `_CLAIMED_REPORTS` 原样复用,只需补 CSV 的认领规则

两条都要回答:**硬停时 partial 判读从哪来、`reports/` 会不会重新堆积。** 同时 §5.3 的 `report : <path>` 与 §6 的目录声明必须统一成一种语义。

### 11.2 离线区间进实时流(✅ 已解决 2026-09-14:用户批准进 stdout)

> **用户决定**:"可以在 stdout 里面加一下,这个影响不大,其他的地方不建议,stdout 没那么局促。"
> 即:**离线区间允许作为事件行出现在 stdout**;v2.7.2 决策 #0.3 禁止的是**其它地方**(横幅 / toast / 状态条 / 离线日志面板),
> 那一层**依然禁止**。所以 §5.2 的表格不变,但要在 §2 明确一句:不新增 badge/状态条/横幅,离线只以 stdout 事件行表达。

### 11.2-原始 「设备离线区间」进实时流,与 v2.7.2 冻结决策冲突(需你重新拍板)

§5.2 把「设备离线区间」列为**立即打印的事实事件**,但 v2.7.2 决策 #0 第 3 条是用户原话批准的:
「不新增离线提醒 UI —— 原话『不需要,直接拿之前的【临时离线】就行了』。『临时离线』合成卡就是唯一信号,
**别再加横幅/toast/日志行**」。CLAUDE.md 的规则是"与之冲突 → 先提矛盾再写代码"。

用户 2026-09-14 的新许可("中途可以增加监控预警和提示")**没有点名离线场景**。默认继承等于静默推翻一条冻结决策。
→ **要么用户明确覆盖,要么离线/在线窗口只进 `events.csv` 与收尾判定块,不进实时流**(实时侧仍由「临时离线」卡表达)。

### 11.3 判定块要的字段没有数据源(✅ 已解决 2026-09-14:已挂到 MED / SLOW 层)

> **结论**:`up`(设备开机时长)挂到 **MED** 层,`fg_pid`(前台进程号)随 **SLOW** 层既有的 `pidof`/`/proc/<pid>/stat` 免费产出。
> 两者均已写进 §3.1 的层表与成本口径。原文分析保留如下。

### 11.3-原始 判定块要的字段没有数据源

§5.3 判决块要 `DEVICE : reboots=1 at t=4120s (uptime 8120s -> 38s)`,但**整份设计从不读 uptime** ——
§3.1 采样表没有 `/proc/uptime` 或 `boot_id`,§0 的三档预算不含它,§7 P2 交付清单也没有。
同理 §4.5 要求"应用死亡必须有 **pid 级证据**",而 sample/meta 的字段定义里没有 pid。
→ 要么把它们显式挂到某一层(建议 SLOW,一行,成本可忽略)并写进命令表/成本表/P2 清单,要么删掉这两个要求。

### 11.4 其余待修(✅ 已由 §12 定稿解决)

> 下面每一条的**结论都在 §12**:coverage 循环定义 → §12.2;零阶保持与 `not_due` 互斥 → §12.2 的 9 态账本;
> `judge()` 入参 → §12.2 的 `judge(evidence)`;调度器缺失 → §12.1;两侧超时不自治 → §12.3;
> P2 不自足 → §12.6(最小事件路径并入 P2);管线 cap → §12.5。
> 原文保留作审计线索。

### 11.4-原始 其余待修(不阻断,但会被实现者踩到)

- **coverage 是循环指标**:分母用"实测中位周期"推算时,设备整体变慢会让分子分母同比缩小 → coverage 恒 ≈1。
  请求 2 s、实际每拍 25 s 的一次跑仍能拿 OK。建议:分母用**请求 interval** 推算,实测中位只作诊断字段
  `cadence`,明显劣化(如 >1.5×)时最高只能 WARN。
- **零阶保持与 `not_due` 互斥**:ZOH 生效后 `not_due` 在序列里永远不可观测;且没有任何字段区分"本拍实测值"
  与"保持值" → §4.2 的"结论永远可被重算验证"不成立。建议每拍四态账本 `fresh | held | failed | not_due`,
  只有 `fresh` 进 `judge()`,`samples.csv` 带状态列。
- **`judge(series, events)` 签名不足以产出 §5.3 的判决块**:RUN/DATA/DEVICE/APP 那些字段来自自记账、
  增量累积器、重启检测、pid 历史,都不在 series/events 里。需冻结入参与 verdict 字段级 schema。
- **§3.1 没有调度器设计**,且正文误称"现设计已有此能力" —— 今天的 `build_sample_command` 是**无状态静态拼接**,
  没有层/周期/到期谓词。需补:层的数据结构、到期与相位、片段拼装顺序、设备输出里偶发 `@@` 开头的行会把段切歪、
  SLOW 层合并后 `fg_pkg → fg_cpu` 的取值链、空层、单层解析失败、运行时降级、以及"事件突发"对占空比模型的影响。
  周期公式 `T*ceil(30/T)` 在 T 为小数时**不保证嵌套**(会出现"SLOW 但不 MED"的拍),建议改为嵌套式 `SLOW = MED*ceil(30/MED)`。
- **两侧超时不自治**:`budget = max(3.0, 3×p95)` 的下界就是 3.0 s,而设备侧写死 `timeout -k 2 5`。
  实测 MED/SLOW 在百毫秒级 → `3×p95 ≈ 0.6 s` → budget **恒为 3.0**,"从实测派生"在这台设备上永不生效;
  而 su 卡住时 PC 3 s 就放弃、设备侧那条还要跑到 5–7 s,正好制造本节声称要防的重叠。
- **P2 不是自足的一步**:§3.4 说 P2 要"记 timeout 事件",但事件发射器是 P3、前端 `event` 分支是 P6。
  P2 单独上线时事件行走 `return line` → **原始 JSON 直接进 console 且每次 WS 重连重放**。
  要么 P2 只在内部记账不发事件线,要么把最小事件发射器 + 前端分支 + 限流并进 P2。
- **管线 cap 没写进文档**:前端 `PERF_SAMPLE_CAP = 86400`(按 24h@2s 设计)与 WS 重放上限
  (`MAX_REPLAY_LINES=50000` / `REPLAY_TAIL_BYTES=4MB`)在 **24h @ 1s** 工况下都会撞上,而归档的
  `chart.png` 正是由这份被裁的缓冲渲染 → "长期保存的证据图"会静默丢掉最早一段。

---

## 12. 定稿规格(权威 —— 与 §3–§5 冲突时以本节为准)

### 12.1 调度器

**时间基一律整数毫秒,调度算术里禁止出现 float 秒。**

```
now_ms  = time.monotonic_ns() // 1_000_000
T_ms    = max(1000, min(60000, int(round(interval_sec * 1000))))   # 落整后立即丢弃浮点值
origin_ms = 第一个 tick 的 now_ms,此后永不变
nominal_ms[k] = origin_ms + k * T_ms                                # k 单调递增
```

> **为什么必须整数化**:§3.1 的 `T*ceil(5/T)` 保留浮点 MED,在 T=1.1/1.9/2.2/10.7 这类值上会因浮点取模
> 得到 `ceil(5/1e-15)` 之类爆炸值或直接崩。整数化后对 T ∈ [1.0, 60.0] 全扫(步长 0.1)无病态。

**层周期(整数推导):**

```
ticks_per[FAST] = 1
ticks_per[MED]  = max(1, ceil(5000 / T_ms))
ticks_per[SLOW] = ticks_per[MED] * max(1, ceil(30000 / (ticks_per[MED] * T_ms)))
due(tier, k)    = (k % ticks_per[tier]) == 0
```

| T_ms | MED | SLOW |
|---|---|---|
| 1000 | 每 5 tick = 5.0 s | 每 30 tick = 30.0 s |
| **2000** | **每 3 tick = 6.0 s** | **每 15 tick = 30.0 s** |
| 6000 | 每 1 tick = 6.0 s | 每 5 tick = 30.0 s |
| 30000 | 每 1 tick = 30.0 s | 每 1 tick = 30.0 s |

注意 T=6000 时 MED 每 tick 到期(6.0 s > 5.0 s 窗口)→ **MED 与 FAST 同层,线协议必须允许同一 tick 内两段并存**。

**段表(真正的调度单位,顺序即命令内出现顺序,不得重排):**

```
SECTIONS = [("STAT","FAST","cpu"), ("PROCS","FAST","procs"), ("UP","FAST","uptime"),
            ("MEM","MED","mem"),   ("FOCUS","SLOW","fg"),    ("FGPKG","SLOW","fg"),
            ("PIDSTAT","SLOW","fg"), ("GPU","MED","gpu")]
tick 后处理顺序固定:parse → up → rollover/reboot 守卫 → deltas → fg    （不得改序）
```

> **`UP` 从 §3.1 的 MED 提到 FAST** —— reboot 守卫和它守卫的计数器**必须落在同一 tick**,
> 否则重启后第一个 tick 会拿旧 uptime 去比新计数器,rollover 判定误报。

**命令构造:**

- 每 tick 独立拼命令、独立起一个 `adb.exe`(持久会话已实测否决,见 §3.3)
- tick 开始前生成一次性 nonce:`os.urandom(3).hex()`(6 位小写十六进制)
- 每段前打 `@@<SEC>:<nonce>`;**命令尾必须补 `@@DONE:<nonce>`**
- 用**尾标记**对齐解析:`split_sections` 只认「`@@` + 段名 + `:` + 本 tick nonce」的行;nonce 不匹配的行整段丢弃并计一次 `misparse`
- 命令以**参数列表**交给 `subprocess`,绝不 `shell=True`

```
# T=2000 的全到期 tick(k=15),nonce=a17f3c 为例:
adb -s <serial> shell "echo @@STAT:a17f3c; cat /proc/stat; set -- /proc/[0-9]*; echo @@PROCS:a17f3c; echo $#; echo @@UP:a17f3c; cat /proc/uptime; echo @@MEM:a17f3c; cat /proc/meminfo; echo @@FOCUS:a17f3c; dumpsys window 2>/dev/null | grep mFocusedApp; echo @@FGPKG:a17f3c; pidof <last_pkg>; echo @@PIDSTAT:a17f3c; for x in $(pidof <last_pkg>); do cat /proc/$x/stat; done; echo @@GPU:a17f3c; timeout -k 2 <G> su 0 cat /sys/kernel/debug/mali0/dvfs_utilization /sys/kernel/debug/mali0/gpu_clock; echo @@DONE:a17f3c"

# T=2000 的 FAST-only tick(k=14):
adb -s <serial> shell "echo @@STAT:a17f3c; cat /proc/stat; set -- /proc/[0-9]*; echo @@PROCS:a17f3c; echo $#; echo @@UP:a17f3c; cat /proc/uptime; echo @@DONE:a17f3c"
```

> **两处易错(实现时注意)**:
> ① `@@PROCS` 是**进程数**(`set -- /proc/[0-9]*; echo $#`,用 shell glob 内建避免 fork;对比 `ls /proc | grep -c` 贵 20 倍),
> **不是** `/proc/<pid>/stat` —— 后者属于 SLOW 层的 `@@PIDSTAT`。
> ② `@@GPU` 段**必须一次 `su 0 cat` 读两个文件**(`dvfs_utilization` **和** `gpu_clock`)——
> 只读前者会让 `gpu_clk` 指标永远为空,而且分两次读要多付一次 su 冷启动(实测 251.6 ms vs 129.9 ms)。

> **为什么要 `@@DONE`**:`@@GPU` 标记在 `su` 读之前就已经被 echo 回来了 —— 如果 su 段被截断,
> 没有尾标记就**无法区分「少了一段」和「设备没有 GPU 节点」**。

> **为什么要有 nonce**:设备的自发输出可能让某行以 `@@` 开头,旧逻辑「任何以 `@@` 开头的行都开新段」
> 会把后面所有段边界切歪。nonce 让外来行无法冒充。

**GPU 降级(踩坑 #14:视频播放时 GPU ≈ 0 是正确的 —— 绝不许建「GPU 卡在 0」规则):**

- 连续 **3 个到期 tick** 失败(任何原因)→ 置 `d`、发一次 `src_degraded` 事件、此后不再发 `@@GPU` 标记、每 300 s 重探一次
- 启动探测失败同样每 300 s 重探,恢复时可再发一次 meta 更新
- **`meta.sources` 只许增不许减** —— 否则前端会重建 series,把前段好数据抹掉
- **降级判定只看「读不到」,绝不看「读到的值是 0」**

**burst(手动补采):** arm 时钳位 `counter[SLOW] = min(counter[SLOW], ticks_per[MED])`(禁止把 SLOW 相位跳到 MED 之前);预算 `max(1, duration_sec / 300)`,上限 12 次/小时。T ≥ 30 时是 no-op;T=6 抬 +0.26 pp,T=1 最多 +0.33 pp —— **此代价必须如实写进 UI 提示**。

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

> **比 §3.1 的旧数贵是预期的**:v2 命令多了 `@@UP` 段、`@@FGPKG` 段(`pidof` 单独一次)、
> `@@DONE` 尾标记,GPU 还是一次 su 读两个文件。FULL 从 305.6 → 408.7 ms(+34%)。
> **T=1s 档要留意**:FULL 单拍 408.7 ms 已占 1 秒周期的一半,设备负载下会更紧张 —— 长跑建议用 T=2s。

### 12.2 证据模型

**每 tick 的指标级状态(9 位,顺序固定):**

```
METRIC_ORDER = [cpu, procs, up, mem, gpu, gpu_clk, fg_pkg, fg_pid, fg_cpu]
  f = fresh     本 tick 到期且解析成功
  h = held      ZOH 保持(本 tick 未到期但有历史值)
  n = not_due   本 tick 未到期且无历史值
  b = baseline  首次读,只立基线不出 delta
  x = failed    到期但解析失败
  a = absent    设备没有这个节点
  d = disabled  被降级策略关闭
```

**tick 级结果:** `ok`(所有到期段 f/h/b) / `partial`(有 x 或 a 但至少一段成功) / `timeout`(PC 预算耗尽) / `offline`(rc≠0 且 stderr 含 `device offline`/`no devices`) / `error`(其它非零退出)

**线协议字段:** `st = "<res>:<9 chars>"`,正则 `^(ok|partial|timeout|offline|error):[fhnbxad]{9}$`

```
"ok:fffhhhhhh"       T=2000 普通 tick
"ok:fffffffff"       T=2000 第 15 tick(全到期)
"offline:xxxhhhhhh"
"timeout:nnnnhhnnn"
```

> **为什么是 9 字符 7 态**(否决了 4 态/6 态版本):4 态把 `absent`/`disabled`/`failed` 混成一类且没有 `baseline`,
> 下游分不出「设备没这个节点」和「这次读失败」—— 这两者触发完全不同的告警。

**覆盖率的分母(修正 §4.3 的循环定义):**

```
expected = max(1, floor(run_wall_ms / T_ms))        # 分母 = 请求周期,不做任何替换
good     = 本 run 中 res ∈ {ok, partial} 的 tick 数
coverage = good / expected
cadence_ratio = p50(dt_ms) / T_ms                   # 门限 <= 1.5
interval_below_tick_cost = (T_ms < p50(cost_ms))    # cap-only 标志,不参与替换
```

> **为什么必须改**:旧规则「分母用实测中位周期」会把「采集跟不上」这件事本身掩盖掉 ——
> 周期被拉长,分母跟着变大,覆盖率反而更好看。一次比请求慢 12 倍的跑仍能拿 OK。

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

- 启动时 `open(..., "a", newline="")` **持住句柄**,每 tick 写完 `flush()`,运行期不 close
- **撕裂行判定**:末行必须同时满足「含换行符 + 列数 = 28 + `t_sec` 严格单调」,否则整行丢弃
- `end_of_run` 行**必须写**;缺行即文件级 partial 标记

**events.csv**(同目录同前缀):列 `t_sec, clock_ms, type, reason, detail`

- P2 事件集:`timeout, offline_start, offline_end, cadence, tick_gap, misparse, src_degraded, src_recovered`
- P3 增补:`reboot, rollover, fg_change, fg_lost, app_gone, pkg_mismatch, budget_exhausted, leak_suspect`
- **限流**:key = `(kind, dev_short)`,固定 60 s 窗口;**只有真正打印了才登记**(禁止「先登记再丢弃»,否则真事件会被限流吃掉)
- **两套时间,各有各的用处(v2.11.2,2026-09-18)**:每条事件同时带 `t_sec`(运行节拍,第几秒)与
  `clock_ms`(epoch 毫秒)。`reports/*.html` 第五节「链路事件」**两列都渲染**,墙钟 `发生时刻` 在前、
  节拍 `相对时刻` 在后,悬停给出完整 `YYYY-MM-DD HH:MM:SS`。用户原话「我想要能看到问题发生时的时间如
  18:39:12 等,这个才是重要的元素」—— 节拍只说「在这趟跑的哪个位置」,墙钟才能和另一台设备的日志、
  一张工单、或某人"下午那会儿"对上。**`clock_ms` 本来就是渲染层漏掉的一个字段,不是新采的数据** ——
  payload / CSV / 判定 / 限流全都没动。缺失或非法时钟渲染成 `-`,绝不抛(收尾的 partial snapshot 也走这条渲染)。

**report.json**(只存证据,不存原始样本):

- 结构 `{"verdict": {...}, "evidence": {...}, ...}`
- 不变量(可 assert 且必须写进 selftest):**`judge(report["evidence"]) == report["verdict"]`**
- 二级可重演:`verdict ← evidence` 精确一致;`evidence ← samples.csv` 机械可推
- **不放 samples 数组** —— 旧版塞全量 samples,与「证据 O(1) 有界」自相矛盾(实测 309 B/条)

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
| 9 | APP | `fg_pkg` 未失联(`app_gone`/`fg_lost` 未触发) |

合成:`result = INCONCLUSIVE > FAIL > WARN > OK`(取最高优先级),并附 `capped_by` 列表说明是哪几个门封的顶。

**相对 §4 的删减**(复核认定冗余或恒等):删 `n_valid`(与 `n_fresh` 恒等);删线协议上的 per-metric cause 字典(降成 CSV 单一 `causes` 列);删线协议上的 `win_s`(可由 `t_now - t_prev_fresh` 推出);**删 1 分钟桶**(samples.csv 已是全分辨率,桶只增加一处可变真相);删「分母用实测中位周期」(它是错的,不是冗余)。

### 12.3 双侧超时

**两级结构:** PC 侧 `B` = 整个 adb 调用的墙钟预算;设备侧 `G` = 命令里 `timeout -k 2 G <cmd>` 的守卫值。

```
硬约束: B >= G + 2.0 + transit + 0.3        transit = adb 固定开销实测 110-140 ms,保守取 0.3 s
       → B >= G + 2.6
默认:   USB 连接 G=3 → B=5.5(留 0.4 s 余量)
```

**G 的自适应(修正 §3.4 的 3×p95 正反馈):**

```
估计窗 = 最近 30 个 tick 中 res ∈ {ok, partial} 的 cost_ms   ← 必须排除 timeout
base   = p90(窗口样本 < 30 时退化为 max)
G_need = ceil(clamp(5.0 * base_ms / 1000, 3.0, 15.0))
阻尼   : 缩小立即生效;变大每 tick 最多 +1 s,且必须连续 3 个 tick 都要求变大才动
```

> **为什么不能按 p95 定**:watchdog 的尺寸应按「典型代价」定,而不是按「你要抓的那个异常的 p95」定。
> 旧规则会随异常自涨,把要抓的现象吃掉 —— 超时拍计入 p95 → 预算变大 → 下次卡死等更久。

**有界子进程(必须修的实现缺陷):**

- 不用 `subprocess.run(timeout=)`,改**显式 `Popen` + `communicate(timeout=B)`**
- `TimeoutExpired` → `proc.kill()` → **再做第二次 `communicate(timeout=1.0)`,必须有界**;第二次也超时 → 关管道、不再 wait
  (CPython 在 kill 之后再调 `communicate` 是**无界**的,设备进 D 态时会挂死采集线程)
- `adb_shell` 必须返回 `(rc, stdout, stderr)` 三元组
  (现状缺陷:`adb_capture` 返回 `r.stdout or ""` 且 `except: return ""`,**没有 rc 没有 stderr**,TimeoutExpired 与设备离线无法区分)

**段级守卫字面命令(G=3):**

```
timeout -k 2 3 su 0 cat /sys/kernel/debug/mali0/dvfs_utilization
timeout -k 2 3 su 0 cat /sys/kernel/debug/mali0/gpu_clock
```
(现状 `GPU_DVFS_READ`/`GPU_CLK_READ` 只有 `timeout 5`,`-k` 缺失 —— 设备侧 hang 时 5 s 后的 SIGTERM 可能被忽略)

**不要从名义 interval 派生超时** —— 旧版 `max(30, int(interval)+15)` 方向是错的:T 越大预算越松,恰好在最需要快速失败的大周期上给最长等待。B 只从 `cost_ms` 估计窗派生,与 interval 无耦合。

### 12.4 输出协议

**实时数据行**(P1 已发布 v2.7.6 的定宽格式,新增 `st` 字段后为):

```
[perf] t=    2.0s | cpu= 61.6% | gpu= 35.3% | mem= 67.4% | fg= 39.4% | clk= 552MHz | st=ok:fffhhhhhh | pkg=com.google.android.apps.tv.launcherx
```

**判定块字面行(收尾一次,纯 ASCII,定宽):**

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

> **约束**:除最后一行外,**任何行不得出现字面量 `report`**。
> 理由:`_sniff_report_path` 靠「含 ` : ` 且候选以 `.json` 结尾」认路径,多行候选会认错,
> 而且 `_stream_logs` 只捕获**第一个** report 路径。最后一行必须且仅是 report.json 的路径。
> `verdict.metrics[m] = {status, n_fresh, expected, coverage}`

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

> **一句话**:数据完整的区间是 **T=2s × 48h**,但重放与图表完整只到 **T=2s × 7.8h**(T=1s × 3.9h);
> **实际推荐工作区间是 T=2s × 8h 以内**。要跑满 24h@1s 需要先决定是否抬 `REPLAY_TAIL_BYTES`(见 §12.7 未决项)。

### 12.6 阶段边界(取代 §7 的路线图)

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

> **P2 的关键修正**(复核发现):§7 原计划让 P2 发射 timeout 事件,但**事件发射器是 P3、前端分支是 P6** ——
> P2 单独上线时事件行走 `handlePerfLine` 的未知类型分支 → **原始 JSON 直接进 console 且每次 WS 重连重放**。
> 所以**「最小事件路径」(发射器 + 前端 `st`/event 渲染 + 限流)整体并入 P2**,P2 不是自足的一步。

### 12.7 未决项 —— 已由实现方拍板(2026-09-14)

> 用户 2026-09-14:「问我没用,我已经有点看不懂了……你拿一下主意,风险项可以暂时加到 todo 里面」。
> 以下 8 条由实现方按**保守、可反悔**的原则决定,全部记进 [TODO.md](../TODO.md) §6。
> **任何一条都可以随时推翻** —— 每条都写了"怎么改回来"。

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

### 12.7-原始 未决项(需要你拍板或需要一次真机测量)

1. **三条复合命令必须用重写后的 `--probe` 重测** —— 表里的 100.8/207.9/305.6 ms 是**旧版本**的命令测的,
   新命令多了 `@@UP` 段与 `@@DONE` 尾标记,代价会变。**锁占空比表之前必须重测。**
2. **所有检测阈值都没标定过**(内存单调上升的斜率、cadence 门限、覆盖率门限)—— 需要一次健康的 8h 基线跑。
   在那之前门限只能算占位值。
3. **WiFi 链路下 adb 的固定开销(transit)没测过** —— `B >= G + 2.0 + transit + 0.3` 里的 110–140 ms 是 USB 实测,
   WiFi 下余量可能不够。
4. **多进程应用的 `pidof` 语义未定**:`pidof` 会返回多个 pid(如 `:remote` 进程),
   `PIDSTAT` 取哪个、还是取主进程,**需要你确认业务上要看哪一个**。
5. **设备侧 su 若陷进不可中断的 D 态**,`timeout -k 2` 的 SIGKILL 也回收不了那一个进程 ——
   PC 侧 B 与有界 `communicate` 能保证采集线程不死,但**设备会残留一个僵尸读**。是否接受需要你拍板。
6. **4MB 的 `REPLAY_TAIL_BYTES` 对 >7.8h @2s 的 perf 任务会造成重放丢头部、`chart.png` 退化成后段缩略图。**
   要不要给 perf 类任务单独抬高这个帽、或按脚本类型分流,**需要你决定**。
7. **是否给 perf_monitor 增加 `watch_pkg` 参数**:不加的话 APP 门(#9)在「应用崩溃回到 launcher」场景下
   **永远不会触发** —— 因为前台包名变了,而脚本并不知道这是异常。**需要你确认业务上关注哪个包。**
8. **samples.csv 被 Excel 独占打开时**(Windows 文件锁)`append` 会失败。现在的降级是落 `status=csv_degraded`
   并继续跑 —— 这个策略是否可接受(等于证据链降级)**需要你确认**。

### 12.8 上线后的三项修订(v2.9.0,2026-09-14,用户跑完第一份真机短样本后)

**本节推翻 §12.7 第 2 条的部分内容**(那是"加 `watch_pkg`,默认空"),其余不动。

| # | 议题 | 决定 | 理由 | 怎么反悔 |
|---|---|---|---|---|
| 1 | `track_foreground` 开关注定要删 | **删,前台采集恒开** | 用户:「采集前台 APP 感觉没必要做开关吧」。§12.7 当初把它当参数是**过度设计** —— 它省下的是一次 `dumpsys window` + `pidof` + `/proc/<pid>/stat`,而丢掉的信号是「被压测的 app 挂了 / 掉到后台」,那是长跑里最该被看见的故障。**`fg_*` 是唯一能看见它的东西。** | 在 `build_tick_command` 加回一个 flag(不建议) |
| 2 | `watch_pkg` 自由文本 → **select 下拉** | **改 `type:"select"`,预置 `WATCH_PKG_CHOICES`** | **自由文本里一个拼错的包名会静默废掉唯一依赖它的 gate 9** —— 报告看起来一切正常。下拉把"拼错"从可能性里去掉。清单**已由用户 2026-09-14 定稿**:YouTube TV / Netflix / Prime Video / 本地媒体播放器 + 不关注(**原先猜的"爱奇艺投影版"该设备上没装,已删**)。包名来自 `cmd package query-activities -c ...LEANBACK_LAUNCHER`;`pm list packages -3` 会漏掉 YouTube TV / Netflix(系统应用) | 改 `WATCH_PKG_CHOICES` 一处;前端本来就走 `f.choices`,零改动 |
| 3b | gate 9(APP)的判据 | **改成三出口:`watch_seen` + `watch_pid_dead` → fail / inconclusive / pass** | 原判据(包名 != `watch_pkg` 连续 2 拍)把"从没出现过"判成丢失(**假 fail**),又看不见"窗口还在但进程没了"(**假绿** —— 而那正是 `watch_pkg` 唯一的存在理由)。**判据无法区分两种状态时必须引入第三态,而不是调阈值** | 回到旧判据(不建议);`WATCH_LOSS_STREAK` 可调 |
| 3 | 结论怎么给人看 | **控制台块带英文解释(仍全 ASCII)+ 另出一份中文 HTML** | 用户:「最后的报告有些太抽象了」「输出一版 html 的 overall result 做成表格样式,然后带点指示描述,html 的可以带中文的」。**两者同源渲染** —— 同一张 row 表出控制台,同一份 payload 出 HTML,不可能各说各话 | 各自独立 |

**`interval_sec` 保留,并且要反复解释清楚**:用户问「采样间隔在之前的架构里面你不是说会做分级采样吗」。
**答案:`interval_sec` 就是本文档里那个 T,分级采样分的是「每一拍读哪些节点」,不是「还要不要有一个采样间隔」。**
MED/SLOW 的周期由 T **推导**(固定 5 s / 30 s 目标),调 T = 同时调三层。**这一点在 §3.1 里没有写得足够显眼,
README 与 HANDOFF 已补。**

**HTML 报告的边界(刻意如此)**:

- **自包含** —— 内联 CSS、无 JS、不拉 ECharts。目标是能把这个文件拷出归档目录,在**一台从没装过 PPTP 的机器上**、
  几年后打开,还看得懂。引 CDN 或复用 `static/vendor/echarts.min.js` 都会**当场破坏这个性质**。
- **不是第二份判定** —— 由 `report.json` 的同一份 payload 渲染。写失败只打印一行 `[warn]`,没有 `else` 分支 ——
  **它永远不能改变结论**。
- **中文只在 HTML 里** —— stdout 仍然是纯 ASCII(Windows 控制台 + WS 编码)。这与"代码文件无中文"不冲突:
  这是**给人类看的数据**,不是代码;`PARAMS` 的 label 从 v2.4.0 起就是这个待遇。

**新增的硬约束**:判定块末尾的 `  html   = <path>` 用 **`=` 而非 `:`**。原因见踩坑 #43 ——
`_sniff_report_path()` 的认领规则是「含 ` : ` 且含 `report` 且以 `.json` 结尾,只取第一条命中」,
而报告路径在 `reports/` 下**本身就含 `report` 子串**,只靠扩展名拒绝太脆。selftest 现在逐字复刻该规则做断言。

> **⚠ 这一轮引入并修掉了一个通道级静默失效** —— 删 `track_foreground` 时把 `due_sec()` 的布尔谓词
> 一起改写,**逻辑整个取反**,前台通道整轮零数据而三层测试全绿。详见 [HANDOFF_PROMPT.md](HANDOFF_PROMPT.md) 踩坑 #42
> 与 [TODO.md](../TODO.md) §6.10。**教训:形状断言看不见死掉的通道。**

### 12.9 长跑按键 keepalive(v2.11.0,2026-09-17)

压测 YouTube / Prime Video / Netflix / 本地播放器时,**长时间播放会让 app 弹「还在看吗」**,把播放打断 ——
采到的样本里混进一段「没人看」的数据,而报告看起来一切正常。办法:加第 4 个参数 `key_ini`,
后台按选中的 `.ini` 定时发按键把它压下去。**用户四条已裁决**:线程内 `import ir_runner` / 先给 MEDIA_PLAY 模板且
ini 要能自由选自由改 / 30 分钟一次 / **只有 perf_monitor 需要**,其余 7 个脚本与平台逻辑零改动。

| # | 议题 | 决定 | 理由 | 怎么反悔 |
|---|---|---|---|---|
| 1 | 怎么调 ir_runner | **线程内 `import ir_runner`**,不起子进程 | 平台「硬停」走 `proc.kill()`(`TerminateProcess`),**不发任何控制台事件** —— 子进程会活下来继续按按键,直到下次重启服务器才被 `_reap_orphan_scripts` 扫掉。daemon 线程随进程一起死 | 改 `Popen`(**不建议** —— 除非同时给硬停加进程组清理) |
| 2 | `run_loop` 还是自己驱动 | **自己按「一次按键」循环调 `run_step`** | `run_loop` 的 `while True` 唯一出口是它自己那个线程里的 `KeyboardInterrupt`,而 Windows 只把控制台事件投给**主线程** —— 那个异常永不到达 | 调 `run_loop`(失去中断能力与精确计数) |
| 3 | 再加一个发送间隔参数? | **不加**,节奏 = ini 的 `delay_ms` | 单步 ini 的 `delay_ms` 既是「重复间隔」也是「轮间隔」,它就是按键周期。再加一个参数 = 两处真相 | 加 `key_interval_sec`(不建议) |
| 4 | 报告要不要新 gate | **不要**,只记 4 个字段 | keepalive 是**测量条件**,不是被测对象。它的故障不能把一场本来有效的样本判成"不通过" | 加 gate(会污染判定语义) |
| 5 | `count>1` 的步怎么算 | **拆成 `count=1` 副本,逐次调用** | `run_step` 自己的重复循环**无法被中断**:一个 50 连发的步在停止后会一路按完;而且那一串**要么全算要么全不算**(实测出现过「日志 40 行按键、计数 30」) | 整个 `count` 交回 `run_step`(**不建议**) |

**一处真机发现**:`CTRL_BREAK` 送给**整个进程组**,`adb.exe` 也在内 —— 停止瞬间在途的那次按键会以
`ADB failed: `(stderr 为空)失败返回。第一版把它计成 `key_failed=1`,报告里看起来像 keepalive 出过错,
**实际是用户自己按的停止**。定性只能放在**按键后面那个等待**上:真故障后面跟着正常间隔,停止会打断它。
**`key_failed` 的含义 = 失败过且仍在重试**(一次瞬时失败不再让整场长跑失去 keepalive)。

**参数面**:`key_ini` 走 select(与 `watch_pkg` 同款待遇,见 §12.8 第 2 条),choices 在**导入时**用 `os.listdir`
扫 `ir_sequences/*.ini`(排除 `_` 前缀的临时文件),值用仓库相对路径、解析时对 `PROJECT_ROOT` 取绝对路径。
前端本就消费 `f.choices`,**零改动**。`docs/REPORT_FORMAT.md` **不动** —— 共享引擎的 schema 没变,这只是新参数实例。

**v2.11.1 补记(2026-09-17,平台侧,与 perf_monitor 无关)**:验证本功能时撞见并修掉了「硬停之后那台设备被
永久卡死」—— 上表第 1 条把「硬停 = `proc.kill()`,不发控制台事件」当成既定事实,而那条路径当时自己也坏了:
它把 `interrupting` 写在 `await _stop_captures()` **之后**,`_stream_logs` 却在 `proc.wait()` 上**先醒**、
先把任务终结成 `failed`,于是非终态覆盖了终态、没人再推进,任务永远 `interrupting`,409 守卫把那台设备锁死。
硬停现在在**第一个 `await` 之前**认领终结者并直写终态 `interrupted`。**本表第 1 条的结论不变**:
硬停仍然是 `proc.kill()`、仍然不发控制台事件 —— 所以「线程而不是子进程」这个取舍依旧成立。
详见 [SIMPLE-ARCHITECTURE.md](SIMPLE-ARCHITECTURE.md) §4.4 与 HANDOFF 踩坑 #48。
