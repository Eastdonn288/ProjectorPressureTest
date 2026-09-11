# 更新日志 (Changelog)

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
