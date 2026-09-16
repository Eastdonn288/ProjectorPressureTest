# PPTP — 待办事项

> 本文件只放**已确认、但当前决定不做**的事情。已完成的进 [CHANGELOG.md](CHANGELOG.md),
> 设计决策与踩坑进 [docs/HANDOFF_PROMPT.md](docs/HANDOFF_PROMPT.md)。
> 用户 2026-09-11 明确:下面这些**先记录,暂不修改**。

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
详见 [docs/HANDOFF_PROMPT.md](docs/HANDOFF_PROMPT.md) §5.5.5 #30。

---

## 4. 前端从未在真实浏览器里被验证过

**严重度:低** —— 但这是"没测过",不是"没问题"。

v2.7.0 的服务端做了逐路径真机验证,DOM id 也做了交叉校验,但"按钮长什么样、点下去对不对"
没有人在浏览器里看过。

---

## 5. ⚠ E-SafeNet 透明加密会破坏 Claude Code 的文件工具(已确认 5 个文件带密文壳)

**严重度:高(会毁文件)** —— 这不是项目代码问题,是本机环境问题,但必须记录。

**现象**:2026-09-11 v2.7.2 开发期间发现,`server.py` 在磁盘上带一层
`E-SafeNet`(亿赛通)透明加密外壳。表现:

- **Python 进程(经 Bash 启动)读到的是明文** —— 文件正常、能编译能跑。
- **Claude Code 的 Read / Grep / Edit 工具读到的是密文** —— Read 显示
  `b#e� P  E-SafeNet  LOCK ...` 之类的二进制;Grep 完全搜不到该文件(直接从结果里消失,
  连 `FastAPI` 这种必然存在的词都搜不到);Read 看到的行数是 316,而真实文件是 2448 行。

**危险点**:Edit 工具是"读-改-写"。它读到密文、在密文上做替换、再把密文写回 ——
**会直接毁掉 server.py**。

**状态(2026-09-11,同日)**:用户**已手动解密** `server.py`,Claude Code 的 Read/Grep/Edit 恢复正常
(实测 2448 行、AST 解析通过、grep 能搜到、多行 Edit 可用)。**但底层加密软件仍在**,随时可能再次
包上 —— 所以下面的规避手段保留,遇到工具读到乱码/搜不到文件时照此处理。

**当前规避**:改 `server.py` **一律用 Python 脚本**读写并加断言,例如:

```bash
"D:/Conda_Environments/dev_env/python.exe" - <<'PY'
import pathlib
p = pathlib.Path('server.py')
t = p.read_text(encoding='utf-8')
old = '''...'''
assert t.count(old) == 1, f"expected 1 match, got {t.count(old)}"
p.write_text(t.replace(old, new, 1), encoding='utf-8')
PY
"D:/Conda_Environments/dev_env/python.exe" -m py_compile server.py
```

**注意**:`read_text`/`write_text` **必须显式传 `encoding='utf-8'`**;另外 Python 文本模式写入
会把 LF 转成 CRLF,写完后要再转回 LF,否则 Edit 工具(即使读得到明文)也会因为行尾不匹配而失败:

```python
b = pathlib.Path('server.py').read_bytes()
pathlib.Path('server.py').write_bytes(b.replace(b'\r\n', b'\n'))
```

**为什么会触发**:推测是 Python 写文件后触发了 E-SafeNet 对**新内容**加密。
本次会话之前 `server.py` 一直能被 Read 正常读取,是在我用 Python 脚本改写它之后才变成密文的。
**其它文件(scripts/*.py、static/app.js、docs/*.md)目前都正常**,只有 server.py 中招。

**2026-09-11 v2.7.4 补充确认:新建文件命中即变密文。** 为了做 A/B 复现,我用 Python 脚本
生成了一份 `server_old_tmp.py` 副本,结果它**一诞生就是密文**:`grep` 完全搜不到(不报错、
只是从结果里消失),而 Python 自己读写完全正常、uvicorn 也能正常 import。所以

- 上面那套 Python 读写 workaround **只对"Python 自己也读不了"的情况才需要**;只要 Python 能读,
  文件带壳并不影响运行,别急着救。
- **反过来更要小心**:`Read`/`Grep` 对这类文件**静默失效**(不是报错),很容易误判成"这段代码不存在"。
  判断依据:Python `open().read()` 能读到内容、而 Grep 搜同一个字符串 0 命中 ⇒ 就是密文壳。
- 用 **Write 工具**(Claude Code 自己)建的文件不受影响;用 **Python 脚本**建/改的会中招。

**2026-09-14 v2.9.0 补充确认:第二批文件中招,且这次是我自己的写入触发的。**
受影响:`scripts/perf_monitor.py`(现在 Read/Grep **静默失效**,Grep 搜 `track_fg` 直接 0 命中,
而 Python 读得到、`py_compile` 通过、真机跑得动),以及 `%TEMP%` 下的几个 harness 文件
(`pptp_perf_v2_harness.py` 的 Read 只返回 25 行)。**触发点同样是 Python `write_text`。**

后果很具体:当时我正在改 `perf_monitor.py`,Edit 反复报 "String to replace not found"、
Grep 报 "No matches found" —— 两次都差点让我得出「这段代码不存在 / 已经删干净了」的错误结论。
**所以踩到时的第一反应必须是「这文件被包壳了」,而不是「代码不在那儿」。**

**规则(v2.9.0 起生效)**:`scripts/perf_monitor.py` 一律用 Python + `assert t.count(old) == 1` 改,
写完 `py_compile`,**绝不用 Read/Edit/Grep 碰它**。

**2026-09-16 v2.10.0 再次确认:不对称性可以精确表述了。** 同一时刻、同一个文件,**Read/Grep 读到密文,而 Python 进程读到明文** —— 这是两个不同的读进程,不是文件的两种状态。
本次的直接证据:`Grep` 在 `scripts/perf_monitor.py` 上搜必然存在的 `HTML_CSS` **0 命中**,同时 `python -c "print(len(open('scripts/perf_monitor.py').read()))"` 把 128KB 全部按 UTF-8 解出来了。
**推论**:看到 Grep 0 命中时**先怀疑密文壳**,再怀疑代码不存在 —— 这两件事在工具输出里长得一模一样。
另:本次由 **Write 工具**新建的 `scripts/_pptp_report.py` 与 `docs/REPORT_FORMAT.md` 之后都能正常 Read,
与上面「Write 工具建的文件不受影响」一致;但 `scripts/_pptp_report.py` 随后被 Python 脚本改过一次,**下次会话碰它之前先确认 Read/Grep 还能看到内容**。

**2026-09-16 v2.10.0 收尾盘点:已确认的受影响文件清单(逐个用 Read 实测出来的,不是推断)。**

- **当前带密文壳**:`server.py`、`scripts/_pptp_report.py`、`scripts/perf_monitor.py`、
  `scripts/wifi_onoff_stress.py`、`scripts/battery_inout_stress.py`。
- **当前可正常 Read**:`scripts/wifi_switch_stress.py`、`scripts/wifi_reboot_stress.py`、
  `scripts/bt_reboot_stress.py`、`scripts/sensor_reboot_stress.py`、`scripts/app_launch_stress.py`、
  `scripts/ir_runner.py`、`static/app.js`、`docs/*.md`、`CHANGELOG.md`。

⚠ **带壳与否不可预测** —— 上面那批"带壳"里有一半是**本轮被 Python 脚本改过却仍是明文**的,
所以**不能用"我改过它/没改过它"推断**,每个文件碰之前**单独实测**。
便宜测法:`Read` 那个文件 `limit: 2` —— 出乱码或行数明显不对就是带壳。
**别用字节扫描**(在磁盘上 grep `E-SafeNet` 字样):CLAUDE.md / TODO.md 自己的正文里就写着这个词,
必然假阳性 —— 而 Python 读它们本来就是明文,壳是**读进程侧**的东西。

**待办**:确认哪些文件受影响、是否有办法让 Agent 工具走白名单进程(或反过来避免用非白名单
进程写这些文件)。在那之前,`server.py` 与 `scripts/perf_monitor.py` 一律按上面的方式改。

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

### 6.10 gate 8(CPU)看不见「通道从来没被请求过」,而证据里没有能拆穿它的数据

**症状(v2.9.0 修掉的那个 bug 暴露出来的)**:前台通道整轮零数据时,gate 8 读的是
`fg_cpu["n_due_ok"]`,它当时是 **0**,于是门自己得出结论「没有预期中的采样」并**报 pass**。

**为什么 `n_due_ok == 0` 是歧义的**:它既可能是
(a) 运行太短,一次 SLOW 拍都没轮到(这时 pass 是对的),也可能是
(b) 代码把整条通道关掉了(这时该 FAIL)。
**证据里没有任何字段能区分这两者** —— `n_due`/`n_due_ok` 只在「到期」时累加,
而「到期」正是被写坏的那个东西。这跟 §6.9 里其它条目不同:那些是**阈值没标定**,
这一条是**判据本身可被同一个 bug 满足**。

**当前的兜底(不是修复)**:

- 假设备 harness 的 `assert_channels_alive()` 会在**测试**里抓住它(条件是「有 SLOW 拍成功返回过」),
  但那只覆盖 CI 路径,**真机跑不经过它**。
- 所以生产环境仍然只有**人肉看状态串**这一道 —— `samples.csv` 里 SLOW 拍恒为 `n` 而从不出现
  `b`/`f`/`h` 就是信号。判定块本身**不会**告警。

**要彻底关掉,需要往证据里加一个和「到期」无关的量**,比如:记录 SLOW 层的**理论开启次数**
(由总拍数与 T 直接算出,不依赖任何谓词),gate 8 在「理论应当开启过 N 次,而实际到期 0 次」时 FAIL。
这个量纯粹由 tick 计数推导,**bug 改不动它**,才算真正独立。**未做**(会动证据 schema 与判定版本号)。

> 同类形状的教训已写进 [docs/HANDOFF_PROMPT.md](docs/HANDOFF_PROMPT.md) 踩坑 #42:
> **形状断言看不见死掉的通道**。这一条是它在判定侧的残留。

**同族问题已修(2026-09-14,v2.9.0)**:gate 9(APP)原来也是"判据分不开两种状态" ——
把"关注的 app 从没出现过"判成丢失(假 fail),又看不见"窗口还在但进程没了"(假绿),
而后者正是该参数唯一的存在理由。现在拆成三出口(fail / inconclusive / pass),详见踩坑 #44。
**本节的 gate 8 盲区仍然存在** —— 它和 gate 9 的病根相同(**判据可以被要检测的那个 bug 自己满足**),
只是 gate 9 能靠"先健康过没有"这个独立事实拆开,gate 8 目前没有等价物。

### 6.9 perf_monitor v2 的已拍板决策与其残余风险(2026-09-14)

用户原话:「问我没用,我已经有点看不懂了……你拿一下主意,风险项可以暂时加到 todo 里面」。
以下由实现方按"保守 + 可反悔"拍板,**决定与理由见 [docs/PERF_MONITOR_V2.md](docs/PERF_MONITOR_V2.md) §12.7**。
这里只记**残余风险**,不重复决策。

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
- **所有判定阈值都还没标定** —— 内存斜率、cadence、覆盖率门限目前是占位值。
  实现会带 `CALIBRATED = False` 并在判定块打印 `judge_version=v1-uncalibrated`,
  **一份完全健康的报告也会显示未标定**,避免假信心。标定需要**一次健康的 8 小时基线跑**。
- **`watch_pkg` 默认「不关注」** —— 不选时"应用崩溃回到 launcher"**不会被判成异常**(脚本不知道你关心哪个包)。
  默认行为与今天一致,是刻意选择;要抓这个场景就得选上。
  **v2.9.0 起它是下拉而非自由文本**,清单已由用户定稿(YouTube TV / Netflix / Prime Video /
  本地媒体播放器 + 不关注),改 `WATCH_PKG_CHOICES` 一处即可,前端零改动
  (`paramInputHtml` 早就支持 `select`,见 v2.2.0/v2.3.0 决策)。
  ⚠ **仍未验证**:本地媒体播放器(`com.mediatek.wwtv.mediaplayer`)的**进程名是否等于包名** ——
  `pidof <pkg>` 取不到 pid 的话 `fg_cpu` 对这个 app 恒为空。另三个已核对过(YouTube TV 3099 /
  Netflix 3314,都能取到)。跑它的时候顺手看一眼 `adb shell pidof com.mediatek.wwtv.mediaplayer`
  有没有输出。

---

## 7. v2.10.0 报告引擎的已知残余(2026-09-16)

> 都是**已确认、当前决定不做**的事。设计决策与实测结论在
> [docs/HANDOFF_PROMPT.md](docs/HANDOFF_PROMPT.md) §6 的 v2.10.0 条;格式真源在
> [docs/REPORT_FORMAT.md](docs/REPORT_FORMAT.md)(该文 §12 有同一份清单的简述)。
> **7.1 当天即已解决**(用户 2026-09-16 裁决),留在这里是为了记住那个缺口的形状。

### 7.1 ✅ `wifi_onoff_stress.py` 中断时不写报告 —— **已解决(2026-09-16 用户裁决)**

用户原话:「中断时报告与 HTML 都写,报告简单注明一下吧」。

- **原先的缺口**:报告写入被 `if not interrupted:` 包着(**脚本自身既有设计**,顶部注释写着
  「report is only saved on full completion」),但 `=== results ===` + `OVERALL` 在守卫**外**、中断时照打 ——
  于是中断时 **stdout 有一个 Overall Result,却没有任何报告**,与本轮要求
  「每个脚本 stdout 的 Overall Result 都需要生成 html 的报告」直接冲突。
- **为什么只能整条删守卫**:"只在中断时补 HTML"是不可能的 —— 报告与 HTML 必须**同 stem 同目录**
  才能被归档收走(见 [REPORT_FORMAT.md](docs/REPORT_FORMAT.md) §7),否则那是一个**孤儿 HTML**,
  被 `_companion_files` 静默丢弃。所以要补就得**同时**写 JSON 报告,只能整条删。
- **现状**:两类产物每次都写。判定**一个字不改**(控制台与页面因此不可能互相打架),
  中断这件事进 `interrupted: bool` + `interrupted_note: str`(JSON 里两个键**恒在**),
  同一句话进 `warn_zh` → HTML 横幅正下方那条黄条。
- **措辞分两种**:一轮都没跑起来 → 比率与结论**不成立**;跑了若干轮 → 明说
  「已开始 N/M 轮(其中最后开始的 1 轮未跑完),下面的比率与结论**只覆盖这些已开始的轮次**」——
  `actual` 数进了在飞的那轮,而它没贡献任何一次检查计数,不写清楚的话半场会被读成一场差的。
- **真机实测过**:平台拉起 → 跑到第 2 轮点「中断」→ 归档里 `report.json` 与 `<stem>.html` 都在、
  `report.json` 带 `interrupted: true`、页面黄条可见、`report :` 仍是最后一行。详见
  [CHANGELOG.md](CHANGELOG.md) v2.10.0「中断时报告与 HTML 都写」。

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

### 7.6 引擎 588 行,计划里估的是 320

多出来的部分是 params 表(要同时吃两种 `choices` 形状)、`_detail_html` 的递归按键名隐去、
以及三种区块构造器(`section_table` / `section_kv` / `section_list`)。不是失控,是估算偏低。

### 7.7 `reports/` 里留着本轮验证的产物(**已定:暂不删**)

2026-09-16 的验证跑在 `reports/stress-test/{wifi,app-launch,perf}/` 留下 6 组 json+html,
另外 `reports/_demo_report.html` 是我做引擎冒烟测试时的手工件。**它们全部被 `.gitignore` 覆盖**
(整个 `reports/` 目录都不入库),所以不进版本库、也不会被任何将来的任务认领
(mtime 兜底扫描要求 mtime ≥ 任务开始时间)。**用户 2026-09-16 已定:暂不删** —— 留作引擎的手写样例与这轮的验证痕迹(整个 `reports/` 都不入库,不构成负担);哪天真要清就整目录删掉。

