# PPTP — 待办事项

> 本文件只放**已确认、但当前决定不做**的事情。已完成的进 [CHANGELOG.md](CHANGELOG.md),
> 设计决策进 [docs/DECISIONS.md](docs/DECISIONS.md),踩坑与规矩进 [docs/PITFALLS.md](docs/PITFALLS.md)。
> 用户 2026-09-11 明确:下面这些**先记录,暂不修改**。
>
> **编号是标识符,不复用也不重排。**条目被移除后留下的号是空的,不拿新内容去填 ——
> 否则任何外部转引("见 TODO §7.1")都会指向别的东西。

---

## 1. `wifi_reboot_stress.py` 会把「失败的 reboot」记成 PASS

**严重度:高** —— 这是压测平台最坏的输出形式:假绿。

**问题**:脚本从不校验 `adb reboot` 是否真的让设备重启了。

- `adb reboot` 的返回码被丢弃(脚本注释写着 `non-zero is normal`,但对 rc 不做任何判断)。
- "等待设备上线"的轮询(`sys.boot_completed`)对一个**从未重启过**的设备**同样立即成立** —— 设备一直在线,所以第一轮就通过。
- 结果:如果 `adb reboot` 因为任何原因开始失败(ADB/USB 抖动、设备瞬时 offline、固件不允许 reboot、adb server 被杀),脚本会**继续报 PASS 到底**,最后打印 `success rate: 100.0%`。

**为什么会发生**:评测链路是 "发 reboot 命令 → 等设备上线 → 查 WiFi",缺了中间那步"**确认设备确实掉线过**"。

**建议方向**(未定稿):
- 记录 `adb reboot` 前后的 `sys.boot_completed` 或开机时间(`/proc/uptime`)变化,确认确实重启过;
- 或轮询时**先等设备消失**(`adb devices` 里 offline/absent),再等它回来 —— 没观察到掉线就判该轮 FAIL;
- 至少把 rc 与"从未观察到掉线"记进报告 JSON,让假绿可被事后发现。

**同类风险**:`bt_reboot_stress.py` / `sensor_reboot_stress.py` 是同一个结构(reboot → 等上线 → 查回连),
**很可能有同样的问题**,改动时一并核对。

---

## 2. `wifi_reboot_stress.py` 中断早于第一轮时,汇总读起来像彻底失败

**严重度:低** —— 可读性,不影响数据正确性。

**问题**:在第一轮迭代完成前按「中断」,stdout 会打印:

```
========== summary ==========
  passed: 0 / 0
  success rate: 0.0%
```

外加 exit code 1。**实际上一轮都没测**,但这个记录读起来和"设备彻底失败"一模一样 ——
而这是用户唯一会在 console 里看到、也是会进归档的通道。

**建议方向**(未定稿):区分"0 轮通过"与"0 轮执行";中断且 `completed == 0` 时打印
`no iterations completed (interrupted before first check)` 且退出码不要用 FAIL 语义。

**同类风险**:其它 reboot 类脚本的中断汇总同样需要核对。

---

## 3. 串口停止时最后半行必丢(已确认,刻意不修)

**严重度:低** —— 一行。

读线程的 tail flush 只在 `stop_evt` 置位后执行,而 `_stop_captures` 在同一个同步块里
`evt.set()` 紧接 `ctask.cancel()`,消费端先收到 CancelledError 并关掉文件,那半行就进了无人读的队列。
修它需要在最取消敏感的路径里插等待,代价与收益不成比例。

**含义**:以裸提示符(无换行)结尾的设备 console,最后一行永远不进文件。
详见 [docs/PITFALLS.md](docs/PITFALLS.md) #30。

---

## 4. 前端从未在真实浏览器里被验证过

**严重度:低** —— 但这是"没测过",不是"没问题"。

v2.7.0 的服务端做了逐路径真机验证,DOM id 也做了交叉校验,但"按钮长什么样、点下去对不对"
没有人在浏览器里看过。

**下次开浏览器先 Ctrl+F5**,重点看:任务卡 `存档` 按钮、任务面板占用数字、跑完那次 `[archive]`
摘要行、图表回传是否真的落进 `archive/*/chart.png`、perf 折线图右缘是否跟随最新样本滚动,
以及放电测试后设备卡是否变成"放电关机"(虚线红边)而非消失。这些收益**完全是视觉的**,
`py_compile` / `node --check` 测不出来。

---

## 5. ⚠ E-SafeNet 透明加密会破坏 Claude Code 的文件工具

**严重度:高(会毁文件)** —— 这不是项目代码问题,是本机环境问题,但必须记录。
机制、不对称性、规则与逃生口的完整说明在 **[docs/PITFALLS.md](docs/PITFALLS.md) #0**;
本节只留**清单与实测方法**。

**已确认带密文壳(2026-09-16 收尾盘点,逐个 `Read` 实测)**:
`server.py`、`scripts/_pptp_report.py`、`scripts/perf_monitor.py`、`scripts/wifi_onoff_stress.py`、
`scripts/battery_inout_stress.py`。

**当前可正常 Read**:`scripts/wifi_switch_stress.py`、`scripts/wifi_reboot_stress.py`、
`scripts/bt_reboot_stress.py`、`scripts/sensor_reboot_stress.py`、`scripts/app_launch_stress.py`、
`scripts/ir_runner.py`、`static/app.js`、`docs/*.md`、`CHANGELOG.md`。

⚠ **带壳与否不可预测** —— 上面那批"带壳"里有一半是**本轮被 Python 脚本改过却仍是明文**的,
所以**不能用"我改过它/没改过它"推断**,每个文件碰之前**单独实测**。

**便宜的实测方法**:`Read` 那个文件 **`limit: 2`** —— 出乱码或行数明显不对就是带壳。
**别用字节扫描**(在磁盘上 grep `E-SafeNet` 字样):CLAUDE.md / TODO.md 自己的正文里就写着这个词,
必然假阳性 —— 而 Python 读它们本来就是明文,壳是**读进程侧**的东西。

**待办**:是否有办法让 Agent 工具走白名单进程(或反过来避免用非白名单进程写这些文件)。
在那之前,`server.py` 与 `scripts/perf_monitor.py` 一律按 PITFALLS #0 的方式改。

---

## 6. perf_monitor v2 的风险与未验证项(2026-09-14,实现期间不阻塞)

设计见 [docs/PERF_MONITOR_V2.md](docs/PERF_MONITOR_V2.md)。以下**已知且已接受**,不要在实现时
顺手"解决",除非用户单独提出。

> **状态(2026-09-14,v2.8.0)**:P2–P5 已落地并在真机跑通(40s/75s 两次,`RESULT: OK`、全门通过、
> 占空比 9.06%)。**未验证项没有减少多少** —— 真机跑通只证明"能采到数",不证明"判定准"。
> 下面 **6.2(阈值未标定)** 仍是最大的那一条,且**只有一次健康的长跑基线能解**。

### 6.1 归档无限增长 → 磁盘满(用户明确否决磁盘检查)

归档永久保留 + 每次 run 新增 `samples.csv` + logcat 默认 1 GB/次,磁盘**会**满。用户 2026-09-14
明确否决"开跑前查剩余空间"。后果需要知道:

- 满了之后 `_archive_task` **设计上就是静默失败**(不抛出,只写进 `summary.json` 的 notes),
  用户只会看到 `logs/` 里越堆越多;
- 更糟的是**跑在中途**:logcat / serial 写不进去会静默丢数据,而**任务照常报完成**。

### 6.2 判定阈值未标定

`delta`、`slope`、最短观测窗这些常量在拿到一次真机健康长跑基线之前,是**设计假设而非实测值**。
标定完成前它们只能靠 `--selftest` 的合成序列保证"不明显错",不能保证"不漏报不误报"。

### 6.3 adb over WiFi 完全没测

全部实测数字是 USB + 设备重载下(load avg ~21/4 核、399-401 任务)测的。WiFi 下传输基线会大幅
抬升,**相对收益排序可能反转**。`--probe` 必须保留现场重标定入口,文档不得把这些绝对值写成承诺。

### 6.4 多进程 app 的 `pidof` 语义缺口

`top | grep` 能看到 `com.x:remote` 这类子进程行,而 `pidof com.x` **不枚举它**。换数据源后
`fg_cpu` 的覆盖可能**变窄**而不是变宽。本机没有多进程前台 app 可测,落地前必须在真机
(Netflix / YouTube 这类有 `:remote`/`:sandbox` 的 app)上对照验证。

### 6.5 没有温度/功耗指标

对"8 h 无人值守别把平台搞不稳",**SoC 温度可能是最相关也最缺失的信号**。本轮未探过
`/sys/class/thermal/thermal_zone*/temp` 在本板是否可读、是否要 root。

### 6.6 硬停时没有最终判定

平台 force-stop 时脚本来不及收尾,归档里只有 ≤120 s 前那份 `partial=true` 的临时判读。
这是已知边界,**不是 bug**。

### 6.7 报告形状跨版本不同构

旧 `report.json` 没有 `verdict` 与新字段。任何报告消费方都要容忍缺字段。

### 6.7.1 归档里的旧 `report.json` 旁边**没有** `samples.csv`

v2.8.0 之前跑的 run,归档里只有 `report.json`;v2.8.0 之后才带 `perf_<dev>_<ts>.samples.csv` /
`.events.csv`。P5 的配对逻辑**只在报告存在时生效**,所以:
**如果脚本被硬停(来不及写报告),同一次的 CSV 不会被认领** —— 它们会留在 `reports/` 里,
而"报告没找到"的 note 会进 `summary.json`。这是刻意的(没有报告就没有锚点去配对),
但意味着**硬停的 run 其 CSV 需要人工从 `reports/` 捡**。

### 6.8 前端改动仍需你实跑确认

P1(定宽列)与 P6(事件行)的收益**完全是视觉的**,`py_compile` / `node --check` 测不出来。
且前端至今**从未在真实浏览器验证过**(见 §4)。用户 2026-09-14 已同意后续实跑确认。

### 6.9 perf_monitor v2 的已拍板决策与其残余风险(2026-09-14)

用户原话:「问我没用,我已经有点看不懂了……你拿一下主意,风险项可以暂时加到 todo 里面」。
以下由实现方按"保守 + 可反悔"拍板,**决定与理由见 [docs/PERF_MONITOR_V2.md](docs/PERF_MONITOR_V2.md) §12.7
与 [docs/DECISIONS.md](docs/DECISIONS.md)**。这里只记**残余风险**,不重复决策,也不重复 §6.2。

- **图表在超长跑里会退化成后段缩略图** —— `REPLAY_TAIL_BYTES=4MB`。v2.8.0 加了 `st` 列后
  单行约 309→332 B,所以帽从约 13981 行降到约 **12600 行**:T=2s 时 **≈ 7.0h**(原 7.8h)、
  T=1s 时 ≈ 3.5h(原 3.9h)。超过之后归档的 `chart.png` 只画得出后一段。
  **原始数据不丢**(`samples.csv` 是全分辨率的),但"一眼看全程"的那张图会缺头。
  推荐工作区间 **T=2s × 7h 以内**,再长就要先抬这个帽(或把 `st` 从实时行里去掉 ——
  它是唯一为"给人看"而增宽、却又不是给人看的列)。
  **v2.9.0 复核**:新增的 HTML 报告行只出现在**收尾那一次**的判定块里,不进实时流,
  所以这个帽**没有变化**(仍 ≈ 7.0h @ T=2s);逐拍线宽也一字未动。
- **samples.csv 被 Excel 独占打开时会降级** —— Windows 文件锁导致 append 失败,
  脚本落 `status=csv_degraded` 继续跑。此时**判读仍然成立**(evidence 来自内存,不读 CSV),
  丢的是**原始逐拍转储**。降级会印在判定块的 DATA 行并把结果封顶为 WARN。
- **设备侧 su 陷进 D 态会残留一个僵尸读进程** —— `timeout -k 2` 的 SIGKILL 对不可中断睡眠无效。
  PC 侧有界 `communicate` 保证**采集线程不死、任务照常跑完**,但设备上会留一个卡住的 `cat`。
  这是 POSIX 语义决定的,用户态无法解决;真要清只能重启设备。
- **WiFi 链路下超时余量未验证** —— 公式里的 adb 往返开销是 USB 实测(110–140 ms)。
  已改为**脚本启动时自测往返基线**(不再硬编码常数),所以 USB/WiFi 差异被自测吸收;
  但"自测值是否足够保守"本身没在 WiFi 下验证过。
- **`watch_pkg` 默认「不关注」** —— 不选时"应用崩溃回到 launcher"**不会被判成异常**(脚本不知道你关心哪个包)。
  默认行为与今天一致,是刻意选择;要抓这个场景就得选上。
  **v2.9.0 起它是下拉而非自由文本**,清单已由用户定稿(YouTube TV / Netflix / Prime Video /
  本地媒体播放器 + 不关注),改 `WATCH_PKG_CHOICES` 一处即可,前端零改动
  (`paramInputHtml` 早就支持 `select`,见 v2.2.0/v2.3.0 决策)。
  ⚠ **仍未验证**:本地媒体播放器(`com.mediatek.wwtv.mediaplayer`)的**进程名是否等于包名** ——
  `pidof <pkg>` 取不到 pid 的话 `fg_cpu` 对这个 app 恒为空。另三个已核对过(YouTube TV 3099 /
  Netflix 3314,都能取到)。跑它的时候顺手看一眼 `adb shell pidof com.mediatek.wwtv.mediaplayer`
  有没有输出。

### 6.10 gate 8(CPU)看不见「通道从来没被请求过」,而证据里没有能拆穿它的数据

**症状(v2.9.0 修掉的那个 bug 暴露出来的)**:前台通道整轮零数据时,gate 8 读的是
`fg_cpu["n_due_ok"]`,它当时是 **0**,于是门自己得出结论「没有预期中的采样」并**报 pass**。

**为什么 `n_due_ok == 0` 是歧义的**:它既可能是
(a) 运行太短,一次 SLOW 拍都没轮到(这时 pass 是对的),也可能是
(b) 代码把整条通道关掉了(这时该 FAIL)。
**证据里没有任何字段能区分这两者** —— `n_due`/`n_due_ok` 只在「到期」时累加,
而「到期」正是被写坏的那个东西。

**当前的兜底(不是修复)**:

- 假设备 harness 的 `assert_channels_alive()` 会在**测试**里抓住它(条件是「有 SLOW 拍成功返回过」),
  但那只覆盖 CI 路径,**真机跑不经过它**。
- 所以生产环境仍然只有**人肉看状态串**这一道 —— `samples.csv` 里 SLOW 拍恒为 `n` 而从不出现
  `b`/`f`/`h` 就是信号。判定块本身**不会**告警。

**要彻底关掉,需要往证据里加一个和「到期」无关的量**,比如:记录 SLOW 层的**理论开启次数**
(由总拍数与 T 直接算出,不依赖任何谓词),gate 8 在「理论应当开启过 N 次,而实际到期 0 次」时 FAIL。
这个量纯粹由 tick 计数推导,**bug 改不动它**,才算真正独立。**未做**(会动证据 schema 与判定版本号)。

> 同类形状的教训已写进 [docs/PITFALLS.md](docs/PITFALLS.md) #42:
> **形状断言看不见死掉的通道**。这一条是它在判定侧的残留。

**同族问题已修(2026-09-14,v2.9.0)**:gate 9(APP)原来也是"判据分不开两种状态" ——
把"关注的 app 从没出现过"判成丢失(假 fail),又看不见"窗口还在但进程没了"(假绿),
而后者正是该参数唯一的存在理由。现在拆成三出口(fail / inconclusive / pass),详见PITFALLS.md #44。
**本节的 gate 8 盲区仍然存在** —— 它和 gate 9 的病根相同(**判据可以被要检测的那个 bug 自己满足**),
只是 gate 9 能靠"先健康过没有"这个独立事实拆开,gate 8 目前没有等价物。

---

## 7. v2.10.0 报告引擎的已知残余(2026-09-16)

> 都是**已确认、当前决定不做**的事。设计决策与实测结论在
> [docs/DECISIONS.md](docs/DECISIONS.md) 的 v2.10.0 条;格式真源在
> [docs/REPORT_FORMAT.md](docs/REPORT_FORMAT.md)(该文 §12 有同一份清单的简述)。
> **7.1(中断不写报告,当天即解决)与 7.6(引擎行数估算)已移除** —— 号保留,不复用。

### 7.2 `wifi_onoff` / `sensor_reboot` / `battery_inout` 的报告没在真机上跑过

这三个脚本只过了静态门(`py_compile` + `--dump-params` 字节一致),**没在真机上执行过**。
`wifi_switch` / `app_launch` / `perf_monitor` / `bt_reboot` / `wifi_reboot` 五个都真机跑过。
下次连上设备时优先补这三个。

### 7.3 引擎的 `attach()` / `document_of()` 是死 API

定义了、有 docstring,但**全库无调用方**(`document_of` 的 docstring 原本谎称"被 `--selftest` 用于验证往返",
本轮已改成如实说明它是预留)。保留理由:它表达的能力(只拿 JSON 也能重建页面)是合理的未来需求,
代价只有约 10 行。**觉得碍眼就删掉这两个函数**,没有调用方,删了不会破任何东西。

### 7.4 `app_launch_stress` 一次运行多份报告,前端按钮只开第一份

它每个 APP 写一份 json + html,所以一次运行可能有 3 份。`GET /api/tasks/{id}/report` 按文件名排序
**只返回第一份**(按钮的 tooltip 里写了共几份,其余从「存档」进去取)。**要不要为它加多选 UI 未定。**

### 7.5 明文 Wi-Fi 口令仍在归档的 `report.json` 里

`doc["hide_keys"] = ["password"]` **只管 HTML 渲染**。`archive/wifi/.../report.json` 仍含
`WIFI_NETWORKS` 的三条明文口令。这是既有行为(平台从不解析报告 JSON,也不该开始解析),
但**分享那份 JSON 前必须自己删**。HTML 已验证不含任何口令。

> 用户长期指令:**Wi-Fi 口令不算敏感,不要再提**。本节只作事实归档。

### 7.7 `reports/` 里留着本轮验证的产物(**已定:暂不删**)

2026-09-16 的验证跑在 `reports/stress-test/{wifi,app-launch,perf}/` 留下 6 组 json+html,
另外 `reports/_demo_report.html` 是做引擎冒烟测试时的手工件。**它们全部被 `.gitignore` 覆盖**
(整个 `reports/` 目录都不入库),所以不进版本库、也不会被任何将来的任务认领
(mtime 兜底扫描要求 mtime ≥ 任务开始时间)。**用户 2026-09-16 已定:暂不删** —— 留作引擎的手写样例与这轮的验证痕迹;哪天真要清就整目录删掉。

### 7.8 `app_launch_stress` 的热启动(hot)模式还没真机验证

**冷启动已真机验证通过**(三个 APP 各 1 轮,`TotalTime` 正常、`LaunchState=COLD`、
logcat `Displayed` 交叉验证 1/1 命中)。**热启动模式没跑过** —— 下次跑一轮确认
HOME 退后台 → 拉起流程与 WARM/HOT 语义校验是否符合预期。

---

## 8. v2.11.0 按键 keepalive 的已知残余(2026-09-17)

> **8.1(硬停后设备永久卡死)已修于 v2.11.1** —— 号保留,不复用;逐字记录见
> [CHANGELOG.md](CHANGELOG.md) v2.11.1 与 [docs/DECISIONS.md](docs/DECISIONS.md) 的「单一终结者」条。

### 8.2 往 `ir_sequences/` **新加** ini 后,下拉框要等重启服务才出现

平台按 `(脚本名, 文件 mtime)` 缓存 `--dump-params`,
而 `KEY_INI_CHOICES` 是**导入时**扫目录生成的 → **改已有 ini 的内容随时生效**(节奏立刻变),
**新增一个 ini 文件**则要等服务重启或 `perf_monitor.py` 本身变动。**不为它加缓存失效机制**(收益极小)。

### 8.3 keepalive 的 adb 调用会与每拍采样争用同一台设备

一次按键 ≈ 一次 `adb shell input keyevent`,与当拍的复合采样命令抢同一个 adb server。
`cost_ms` 变大**只会抬高**预算 —— PC 侧 `B` 由最近 30 个 tick 中 `res ∈ {ok, partial}` 的 `cost_ms`
估计窗派生(必须排除 timeout),设备侧 `G` 随之放宽(`timeout -k 2 G <cmd>`)—— **不会误杀**;
代价是报告里的 `COST`/`duty` 读数会**略微上浮**。判定模型见 [docs/PERF_MONITOR_V2.md](docs/PERF_MONITOR_V2.md) §12.3。30 分钟一次的节奏下影响可忽略,但归因时要知道参数表里选了哪个 ini(报告 `config` 里有 `key_ini`)。

### 8.4 中断恰好落在 `Long` 长按的「按下」与「抬起」之间 → 设备可能停在按下态

`run_step` 的 Long 分支里没有 `finally` 抬起。用 `Short` 步(本仓库附的模板就是)不涉及。
**不在 keepalive 里补 `finally`** —— 那要改 `ir_runner.run_step`,而它是三个脚本共用的执行器。

### 8.5 两个线程同时 `print` 时,极低概率吃掉前端一个采样点

CPython 的 `print` 是**两次 write**(先写内容、再写 `\n`),所以两个线程的 print 可以**在同一行交错**
(本轮实测到一次:开跑瞬间 `[key]` 横幅与第一次按键的 `  -> KEYCODE_MEDIA_PLAY` 粘成一行 ——
已通过「横幅在起线程之前打印」消掉那一处)。运行中剩余的风险:按键行与主线程的 `PERF|{...}` 采样行互撞,
会让 `static/app.js` 解析失败 ⇒ **图表少一个点**。窗口是「主线程两次 write 之间」的微秒级,
每秒只有 1 次采样机会,而按键 30 分钟才 1 次 ⇒ 8 小时长跑里的概率约 `1e-5` 量级。
**不为它加进程级 stdout 锁**(要改几十处 print,属于过度设计);如实记在这里。

### 8.6 `key_sent` 的口径:一次 `run_step` 调用 = 一次按键

所以 `count=0` 的步不计数(与 `run_step` 行为一致),而**被打断的那一次按键不计**
(它可能已经按下去了,但 `run_step` 没返回)。**这是保守方向**:宁可少报,不虚报。

---

## 9. 平台的会话与清理残留(2026-09-18 归档)

两条都是**已知、当前不做**的小问题,不影响数据正确性,但知道形状能省掉一次排查。

### 9.1 服务重启后,页面的日志面板「卡」在最后一行

WS 在 server restart 时会断,而前端**没有自动 rejoin**(log WS 只有手动重连路径)。
表现:日志 console 停在上一次的最后一行不再增长,但任务其实还在跑。
**绕法**:重新点一次任务(`viewTaskLogs`)或刷新页面。**不做自动重连** ——
`ir_runner` 任务之外都是纯客户端流,加自动重连要在 `openWs`/`scheduleReconnect` 上再叠一层陈旧防护,
收益不抵风险。

### 9.2 任务被删除后,它的 log 文件没有任何自动回收

**实测**(2026-09-18 逐处读 `server.py`):删任务只走 `_forget_task()`,其 docstring 逐字写着
**"Deliberately touches NOTHING on disk"**、处置说明 "left for the user to clear by hand"。
全库对 `LOGS_DIR` 只有**写**(L265 / L1361 / L1989)与**建目录**(L73),无任何 `unlink` / `glob`;
仅有的删除是 `ir_sequences/_seq_*.ini`(startup,L408–410)、`_archive_task` 搬走后的 `reports/`
原件(L1392 / L1421)、以及用户点名删的 `.ini`(L1908)。
⚠ `_reap_orphan_scripts()` / `_reap_orphan_logcat()` 清的是**崩溃残留的进程**(前者 `python.exe`、
后者 `adb logcat` 子进程),**不是文件** —— 别再说"startup 会把 log 文件清掉"。
**刻意不做自动回收**:`archive/` 是耐用副本,`logs/` 只是运行期工作区。

### 9.3 删除任务的确认框在说谎:它说日志会被一起删掉

**严重度:低(不改数据,只误导用户)** —— 但方向很坏:它让用户以为删任务会销毁产物。

`static/app.js` 里两处确认文案都写着「日志文件会一起删除,无法恢复」(删单个任务、批量清空),
而**同一个界面**上任务卡 `×` 按钮的 tooltip 写的是「从列表移除(存档保留在 archive/)」。
**后者才是事实**:`_forget_task()` 刻意不碰磁盘,归档永久保留;`logs/` 里的运行期文件确实不会被删,
但那是"残留待清"(§9.2),不是"被删掉"。

**改法**(未做):两处确认文案改成与 `×` tooltip 一致的措辞 ——
「从列表移除;归档保留在 `archive/`,`logs/` 里的临时文件需要手工清理」。
