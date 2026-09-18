# PPTP — Projector Pressure Test Platform

本地化、轻量级的 ADB 投影仪压测平台。
通过浏览器控制多台投影仪设备,实时运行压测脚本并查看日志。

> 维护笔记:本文档描述当前已实现的能力。架构、数据模型、最近设计决策、什么不存在等更详细的信息,见 [docs/HANDOFF_PROMPT.md](docs/HANDOFF_PROMPT.md)。其他 SIMPLE-* 文档是初版设计记录,已归档为历史参考。

---

## 这是什么

一个能让你在浏览器里点几下,就在 N 台投影仪上跑压测脚本的小工具。

- 设备列表:自动显示当前 ADB 设备,3 秒刷新
- 脚本管理:把 .py 丢进 scripts/,前端自动出现
- 实时日志:每台设备的 stdout 实时推送到浏览器
- 任务控制:运行 / 中断 / 硬停 / 重置,所见即所得(硬停立即把任务记为终态 `interrupted` 并归档,**那台设备马上可以起新任务**)
- **IR 序列**:ir_sequences/ 下的 .ini 文件定义按键顺序,前端 modal 选取并绑定到设备
- **双通道按键注入**:`KEY_*` 走 sendevent 直写 event 设备(需 userdebug/root);`KEYCODE_*` 走上层 `adb shell input keyevent`(**user 版固件可用**,含 23 个厂商键 + 26 个安卓原生键 + 透传)
- **脚本参数前端可配**:脚本用 `PARAMS` 自描述可配置项(循环次数、等待时长、传感器序列等),前端自动识别并弹出配置窗口,支持 `select` 下拉框类型,按 **设备 × 脚本** 独立保存到 localStorage
- **WiFi 压测脚本**:wifi_onoff / wifi_reboot / wifi_switch 三个独立脚本,覆盖开关、重启重连、多网络循环切换
- **蓝牙回连压测脚本**:bt_reboot_stress — 重启投影仪后检测**蓝牙音箱 A2DP 是否自动回连**(连好音箱后跑),判定 = 蓝牙适配器已开启 + 音箱真正连上;仿 wifi_reboot,可配循环次数/回连超时
- **传感器压测脚本**:sensor_reboot_stress — 重启投影仪后检测 gsensor / ToF 回连,可配置选哪种传感器、循环次数与设备上线超时(采集窗口/检测次数/通过阈值硬编码)
- **APP 冷热启动压测脚本**:app_launch_stress — 冷/热启动耗时压测(冷:force-stop 后测冷启动;热:HOME 退到后台后测回到前台),主指标 `am start -W` TotalTime + logcat Displayed 交叉验证,可按 p95 阈值判定(被测 APP 硬编码脚本顶部 `APP_PRESETS`,**内置 Netflix / Prime Video / YouTube TV 三个一起跑**)
- **性能监控脚本(实时曲线)**:perf_monitor — CPU/GPU/内存% + 前台APP CPU% 实时监控,日志面板顶部实时曲线图,一键导出 log.txt + csv + png
- **电池充放电压测脚本(实时曲线)**:battery_inout_stress — **充电/放电分开跑**,电量/温度/电压实时监控(电量靠串口开启 health 轮询),前端电量+温度双曲线,保留跳变/温控检测,**充到 100% 稳定或设备关机自动停**
- **中文 HTML 报告(每个脚本都有,v2.10.0)**:跑完自动写一份**自包含**的中文报告(无外部 CSS/JS/字体,
  拷走单独打开也完整),含结果总览 / 本次参数(标明默认还是已改)/ 完整数据折叠区;任务卡 **`报告`** 按钮直接打开。
  `ir_runner.py` 除外(它没有通过/不通过的概念)。见 [docs/REPORT_FORMAT.md](docs/REPORT_FORMAT.md)
- **每任务三源日志采集(v2.6.0)**:每次跑脚本,**服务端自动**同时采集 **stdout**(脚本输出)+ **logcat**(设备系统日志,恒开);设备卡勾选后**额外**采集**串口 console**(COM 口)。三份日志自动落盘并归档,**脚本不用做任何事**(前端只显示 stdout)

后端 + 前端 + 脚本合计约 **9800 行代码**(server.py 2528 + app.js 2317 + style.css 872 + index.html 155 + ir_runner.py 617 + wifi_* 854 + sensor_reboot_stress.py 521 + app_launch_stress.py 599 + perf_monitor.py 513 + battery_inout_stress.py 601 + bt_reboot_stress.py 225)。

---

## 5 分钟上手

### 1. 准备环境

- Python 3.10 或更高
- ADB 已安装并加入 PATH
- Node.js 不需要

### 2. 安装依赖

```cmd
pip install fastapi uvicorn pydantic pyserial
```

> `pyserial` 是**串口功能**用的(枚举 COM 口 + 电池脚本开串口),不装的话其它一切正常,
> **只是设备卡上的 COM 下拉框永远是空的**。装的时候要用**启动服务端的那个 Python** ——
> `start.bat` 挑的是 PATH 里第一个 `python`,不一定是你在 conda 里装过的那个。
> 服务端窗口启动时会打印 `[OK] Python: xxx at <路径>`,以它为准。

### 3. 启动

双击 start.bat,浏览器自动打开 http://127.0.0.1:8000。

启动行为:
- 弹出 **PPTP-Server** 窗口,实时显示 uvicorn 日志(同时写入 `logs\server.out.log` / `logs\server.err.log`;关闭该窗口即停止服务)
- launcher 窗口在**服务启动成功后自动关闭**;若 20 秒内未就绪或找不到 Python,则**保留失败信息**等待手动关闭
- 若端口 8000 已被占用(PPTP 已在运行),launcher 提示后直接退出

或者命令行(用项目的 conda Python):

```cmd
D:\Conda_Environments\dev_env\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8000
```

### 4. 停止

双击 stop.bat。

---

## 怎么用

1. 把投影仪通过 USB 接到电脑,确保 `adb devices` 命令行能看到
2. 浏览器打开后,**左列"设备"** 列出所有已连接设备
3. **左下"脚本"** 列出 scripts/*.py 下所有脚本
4. **中列"日志"** 实时显示输出,可点其他设备切换视图
5. **右列"任务状态"** 显示每个任务的开始/结束/状态/耗时
6. 点击设备卡 → 该设备被"选中",对应的脚本卡变高亮、可点
7. 在设备卡的下拉框选脚本 → 脚本卡绑定关系建立
8. 点 ir_runner 卡上的 `▶ 跑` 按钮 → 跑该脚本(要先点设备卡使其"选中",否则按钮隐藏)

---

## 日志采集(logcat / 串口)

**每个任务都会自动采集三样东西,不需要在脚本里写任何代码:**

| 通道 | 何时采 | 内容 |
|---|---|---|
| **stdout** | 每个任务 | 脚本自己打印的东西(就是你以前看到的日志) |
| **logcat** | 每个任务**自动开启** | 设备系统日志(`adb logcat -v threadtime`,信息级及以上) |
| **串口** | **仅当你勾选时** | 设备调试串口 console(需要 USB 转串口线接到电脑) |

### 怎么看

**日志栏只显示 stdout**,永远如此 —— logcat 和串口体量太大,不做前端展示(用户 2026-09-11 决定)。它们只在后台采集 + 归档。

任务结束时,日志栏最后会出现**一行归档摘要**,告诉你存到哪了、每个通道采了多少行:

```
[archive] 20260911-123312_perf_monitor_B0403374A2A508001F00/ | stdout 22L | logcat 8L | serial 0L | report.json
```

某个通道出问题时会带上原因,例如 `serial 0L (failed: port not present)`;logcat 被体积上限截断会显示 `capped`。**这一行是串口到底采到没有的唯一信号**,别忽略它。

### 串口怎么开

设备卡上有 **`串口日志` 勾选框 + COM 口下拉框**:

1. 插好 USB 转串口线,下拉框会自动列出电脑上的 COM 口(如 `COM9`);下拉框为空说明没装 `pyserial` 或没插线
2. 勾上 `串口日志`,选好 COM 口 → **该设备的下一次任务**会同时采集串口
3. 选中的 COM 口会被记住(下次打开还在),**勾选状态不会记住**(避免下次误开)
4. 任务运行中控件会锁定,改不了

> **注意**:如果某个脚本**自己要用串口**(目前只有 `battery_inout_stress`),勾选框会**自动禁用**并提示"平台串口抓取已自动让位"。这是故意的 —— 抢口会让电池保活失败、电量曲线变平。

设备卡上还有一个 **`logcat 日志` 勾选框**(v2.7.5),**默认勾选**:

1. 默认每个任务都采集 logcat —— 崩溃现场就在里面,压力测试不该关
2. **长时间监控(如 8 小时的 `perf_monitor`)建议取消勾选**:一次 run 的 logcat 约 1GB,没人会看,
   而且它会和监控本身抢设备 —— 采集行为会干扰被采集的对象
3. 和串口勾选一样:**只对下一次任务生效,不记住**(避免残留的"关"让下一次压测静默丢掉崩溃现场)

**logcat 默认上限 1GB**(长测保护),超了会在文件末尾写明并停止采集。想调整:`PPTP_LOGCAT_MAX_MB=2048` 后再启动服务。参考量级:**空闲时约 1.6 行/秒,但一次重启的开机风暴就有约 1.3MB**(相差两个数量级)—— 三个重启类脚本默认 100 轮,约 128MB。

### 什么时候会"断",断了会怎样

**设备重启、adb 掉线对压测脚本来说是正常的** —— 平台会自动把三条链路接回来,你不需要做任何事:

| 断了什么 | 平台怎么处理 | 你能看到什么 |
|---|---|---|
| **logcat** | 自动重启采集,最多 60 次连续重试(约 2 分钟),每次回读 2000 行把开机日志捡回来 | `logcat.log` 里一行 `--- logcat capture restart #N ---` |
| **串口** | 自动重新打开端口,同样最多 60 次连续重试;收到数据即重置预算 | `serial.log` 里一行 `--- serial capture reconnecting #N ---` |
| **adb 认不回设备** | 每 20 秒发一次 `adb reconnect offline`(只影响离线设备,**不打扰其它正在跑的脚本**) | 服务端控制台 |
| **日志流(网页)** | 服务端发现连接卡死会**主动关掉**,浏览器随即自动重连 | 网页日志自己接上 |

**设备卡不会因为掉线或重启消失** —— 它会变成「临时离线 · 任务在跑」的样子,任务卡也一直在。

真实重启约造成 20-30 秒的缺口,这是设备关机的时间,不可避免;缺口在日志文件里都留了 marker 行,事后查得到。

---

## 自动存档

**每个任务一结束,产物全部自动归拢到一个文件夹,永久保留,不需要手动导出。**

### 存档在哪

项目根目录下的 `archive/`,**按模块分类**,一次运行一个文件夹,名字一眼能认:

```
archive/
├── wifi/
│   └── 20260911-124233_wifi_reboot_stress_B0403374A2A508001F00/
│       ├── stdout.log      脚本输出
│       ├── logcat.log      设备系统日志
│       ├── serial.log      串口 console(勾选了才有内容)
│       ├── report.json     脚本自己写的压测报告
│       ├── <报告同名>.html  同一次运行的中文报告(每个脚本都有)
│       ├── chart.png       性能图表(perf_monitor / battery 才有)
│       └── summary.json    清单:参数/状态/耗时/各文件行数/备注
├── perf/    battery/    sensor/    app-launch/    ir/    bt/
```

模块划分(`wifi` / `sensor` / `perf` / `battery` / `app-launch` / `ir` / `bt`)和 `reports/` 一致。

**怎么打开**:顶栏的 **`打开存档`** 按钮(直接弹资源管理器),或任务卡右上角的 **`存档`** 按钮(跳到那一次运行)。
**看报告**:任务卡上的 **`报告`** 按钮 —— 新标签页直接渲染那次运行的中文 HTML 报告(见下节)。顶栏还会显示总占用(如 `存档 1.2 GB · 37 个`),鼠标悬停可看各模块分别占多少。

### 关于图表

图表由**浏览器**渲染 —— 平台后端没有画图能力(不想为一张图引入第二套渲染)。所以:

- 任务结束时浏览器开着 → 归档里有 `chart.png`(和你屏幕上看到的完全一致)
- 任务结束时浏览器没开 → **没有图**,但三份日志 + `report.json` 都在,`summary.json` 会写明缺图
- 事后打开页面看那个任务,平台会**自动补传**图表

### 关于清理

- **不做任何自动删除**,存档永久保留,磁盘占用在界面上可见
- 任务卡上的 `×` 和「**清空已完成的卡片**」**只从列表移除卡片,不动存档** —— 这是故意的,避免一次误点把要保留的数据删掉
- 想真正删:去 `archive/` 目录手动删

> **`logs/` 和 `reports/` 不用管,也不能删**:
> `logs/` 是运行期暂存区(日志和报告先落这里,任务结束**搬进** `archive/`)+ 服务自身日志
> (`server.out.log`/`server.err.log`);`reports/` 是**脚本契约** —— 脚本脱离平台用命令行单独跑时也写
> 这里。**走平台跑的话这两个目录平时都是空的** —— 东西全被搬进 `archive/` 了。
> **你要找的东西永远在 `archive/` 里**,这两个是实现细节。

### 导出按钮呢

**已隐藏** —— 自动存档取代了它。代码保留着,以后需要可以把 `static/index.html` 里 `#btn-export-log` 的 `hidden` 去掉。

## 中文 HTML 报告(每个脚本都有)

**每个脚本跑完,除了 stdout 里那段汇总,还会写出一份中文 HTML 报告。** 任务卡的 **`报告`** 按钮直接打开它。

报告和 JSON 报告**同目录、同文件名前缀**:

```
reports/stress-test/wifi/wifi_cycle_B0403374A2A508001F00_20260916_092136.json   ← 脚本写的报告
reports/stress-test/wifi/wifi_cycle_B0403374A2A508001F00_20260916_092136.html   ← 中文报告
```

任务一结束,归档会把**两份一起**搬进 `archive/<模块>/<时间>_.../`(主报告改名为 `report.json`,HTML 保留原文件名)。

### 报告里有什么

| 区块 | 内容 |
|---|---|
| 顶部横幅 | 综合结论大字(PASS / FAIL / `—` 不做判定)+ 中文等级 |
| **结果总览** | 一行一个结论:指标 / 结论 / 取值 / 说明。结论分**通过 / 注意 / 异常 / 样本不足 / 信息**五档 |
| **本次参数** | 这次实际生效的参数值,并标明每个是**默认**还是**已改** |
| 脚本自述区块 | 各脚本自己的明细(参与的网络 / 每个 APP 的 p95 / 每轮 P-F 串 …) |
| **完整数据** | 折叠区,把 JSON 报告的内容全部递归展开 —— **HTML 不会比 JSON 少东西** |
| 页脚 | 脚本名+版本 · 报告引擎版本 · 退出码 · 来源文件名 |

### 三个要点

- **完全自包含**:没有外部 CSS / JS / 图表库 / 字体。把 HTML 单独拷走,在一台从没装过 PPTP 的机器上双击打开,
  排版完整、中文正常、**断网也一样**。这是刻意的 —— 报告是要发出去给人看的。
- **`样本不足` 是诚实的结论**:零迭代 / 早停 / 样本全废时**不会编一个 PASS 或 FAIL**,横幅显示 `— 本次运行不做通过/不通过的判定`。
- **口令不会出现在页面里**:WiFi 脚本的报告里含明文口令,渲染时会隐去(页面显示「已隐去」)。
  ⚠ 但**归档里的 `report.json` 仍含明文** —— 分享报告用 HTML,分享 JSON 前自己删。

> `ir_runner.py` **没有报告**:它是 `.ini` 驱动的交互式按键序列执行器,没有参数、也没有通过/不通过的概念,
> 给它编一个结论是造假。
>
> 想给**新脚本**接入,或想了解报告引擎/参数表的完整设计,见 **[docs/REPORT_FORMAT.md](docs/REPORT_FORMAT.md)**。

## IR 序列(可选高级功能)

平台支持"按步骤执行红外命令"序列,定义在 `ir_sequences/*.ini` 文件里。

### 文件格式(5 字段,无 name)

```
[index]-[code]-[Short|LongXXXX]-[delay_ms]-[count]

例:
1-KEY_HOME-Short-2000-1
2-KEY_VCR-Long3000-500-1
```

- `index` 自动编号(1, 2, 3, ...)
- `code` 三种按键族之一(见 [ir_sequences/KEY_REFERENCE.md](ir_sequences/KEY_REFERENCE.md)):
  - `KEY_*`(24 个,如 `KEY_HOME` / `KEY_ENTER`)→ sendevent 直写 event 设备,**需 userdebug/root**(硬编码在 `IRRemote.CODE_NUM_MAP`)
  - `KEYCODE_*` 厂商键(23 个,如 `KEYCODE_BI` / `KEYCODE_IP`)→ `adb shell input keyevent`,**user 版可用**
  - `KEYCODE_*` 安卓原生键(26 个,如 `KEYCODE_POWER` / `KEYCODE_HOME`)→ 名称原样透传,user 版可用
- `Short` 或 `LongXXXX`(XXXX 为长按毫秒,如 `Long3000` = 长按 3 秒)
- `delay_ms` 重复间延迟(整数 ms)
- `count` 重复次数(整数)

完整按键表见 `ir_sequences/KEY_REFERENCE.md`(`KEY_HOME` → 主页键)。

> ⚠️ **KEYCODE_* 长按暂无效**:目前只有短按 `Short` 生效。ini 里写 `LongXXXX` 不会报错、任务照常完成,但底层 `input keydown`/`keyup` 在当前设备(Android 14 / SDK 34)不存在,命令**静默失败、不会真正注入按键**。只有 `KEY_*` 一族支持长按。

### 使用流程

1. 把 .ini 放进 `ir_sequences/`(或复制现有 .ini 修改)
2. 在 ir_runner 脚本卡上点击 → 弹出 modal 选序列
3. 选中一个 → 状态保存到 localStorage
4. 同一台设备重启平台后选择保留
5. `scripts/ir_runner.py` 是默认的序列执行脚本(无限循环 + 长按 + 单按)

---

## 怎么加新脚本

把任意 .py 文件丢进 scripts/ 目录,只要符合下面契约,前端立刻识别:

```python
import argparse, time

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--device", required=True)
    p.add_argument("--params", default="{}")
    args = p.parse_args()

    print(f"run on {args.device}")
    for i in range(3):
        print(f"step {i}")
        time.sleep(1)

if __name__ == "__main__":
    main()
```

契约只有三条:

- 接收 --device `<serial>`(必需,平台自动传入)
- 接收 --params `<json>`(可选,前端可传入参数)
- (可选)定义模块级 `PARAMS` 列表 + 支持 `--dump-params`,前端自动识别并在脚本卡上渲染配置弹窗

其余完全自由 —— 用 subprocess 调 ADB、用 requests 调 HTTP、写文件、画图都可以。
脚本的 stdout/stderr 实时显示在前端日志面板;退出码 0 = finished,非 0 = failed。

### 怎么让参数前端可配(可选)

脚本里声明一个 `PARAMS` 列表,前端就自动弹出配置窗口,后端 / 前端 / 平台代码一行都不用改:

```python
PARAMS = [
    {"name": "cycles", "label": "循环次数", "type": "int", "default": 100, "min": 1, "max": 100000},
    {"name": "use_su", "label": "cmd wifi 使用 su 权限", "type": "bool", "default": True},
]
```

字段键:`name` / `label` / `type` / `default` / `min` / `max`。`type` 支持 `int` / `float` / `bool` / `str`,以及 **`select`(下拉框)**——下拉框需配 `choices` 列表(字符串或 `{value, label}` 对象),例如传感器序列选择:

```python
{"name": "sensor", "label": "传感器序列", "type": "select",
 "choices": ["gsensor", "tof"], "default": "gsensor"}
```

配置值按 **设备 × 脚本** 独立保存,互不干扰。平台通过跑 `python 脚本.py --dump-params` 读取这份自描述 schema,缓存按 `(脚本名, mtime)` 失效。

---

## WiFi 压测脚本

三个独立 WiFi 压测脚本都在 scripts/ 下,`wifi_*` 前缀。各自可配参数通过前端弹窗设置,报告 JSON 落在 `reports/stress-test/wifi/`(已 gitignore)。

| 脚本 | 测什么 | 可配参数 |
|---|---|---|
| `wifi_onoff_stress.py` | WiFi 开关循环:关 → 等 → 开 → 等 → wpa_cli 扫描统计 | `iterations` `off_sec` `on_sec` `scan_sec` `count_threshold` `use_su` |
| `wifi_reboot_stress.py` | adb 重启循环:重启 → 等设备上线 → 等 WiFi 重连 | `iterations` `wait_sec` `wifi_settle_sec` `back_online_timeout` |
| `wifi_switch_stress.py` | 多网络循环切换:按预置列表轮番连接并验证 SSID | `cycles` `connect_wait_sec` `switch_gap_sec` `use_su` |

要点:

- **预置网络列表**:wifi_switch 的 `WIFI_NETWORKS` 硬编码在脚本文件里(SSID / 密码 / security),按项目决策**不做成前端参数**,要换网络直接改脚本顶部。
- **su 权限**:本设备上 `wpa_cli scan_results` 和 `cmd wifi connect-network` 都需要 root。脚本统一用 AOSP 风格 `su 0 <cmd>`(不是 `su -c`)。`use_su` 参数可关(针对允许免 root 的设备)。
- **判定**:wifi_onoff 按扫描到的网络数是否达阈值判 PASS;wifi_switch 按连接成功率 ≥ 98% 判 PASS;wifi_reboot 按每轮设备能否按时上线 + WiFi 重连判 PASS。退出码 0 = PASS,非 0 = FAIL。
- **中断**:运行中可点"中断"(CTRL_BREAK),脚本会打印已完成部分的汇总并保存报告 —— JSON 与 HTML 都写,报告里注明本次被中断、下面的比率只覆盖已跑完的轮次(不注明的话半场会被读成一场差的)。

---

## 蓝牙回连压测脚本(bt_reboot_stress)

重启投影仪后检测**蓝牙音箱是否自动回连**。先在投影仪蓝牙设置里把音箱连上(当前实测 BOGASING-G4),再跑本脚本。每轮:重启 → 等设备上线 → **轮询蓝牙 A2DP 回连**(每 5s 查一次,最多 `bt_reconnect_timeout` 秒)→ 音箱连上即 PASS。

| 脚本 | 测什么 | 可配参数 |
|---|---|---|
| `bt_reboot_stress.py` | adb 重启循环:重启 → 等设备上线 → 等蓝牙音箱 A2DP 回连 | `iterations` `wait_sec` `bt_reconnect_timeout` `back_online_timeout` |

要点:

- **回连判定**:`dumpsys bluetooth_manager` —— 要求蓝牙适配器已开启(`enabled: true`)**且**至少一个 A2DP 状态机处于 `mConnectionState: CONNECTED`。光"蓝牙开着"不算回连,必须音箱真正连上。真机实测正常回连 ~6s。
- **注意**:音箱刚连上后立即重启,回连可能失败(疑似音箱休眠 / 连接未稳定);在蓝牙设置里**重新手动连一次**后再重启,回连即恢复正常。因此正式压测前建议先手动确认音箱能连上。
- **不做开关回连**:本设备蓝牙开关未对用户开放,用户无法手动关蓝牙,所以只测"重启后自动回连",不测"关蓝牙→开蓝牙"。

---

## 传感器压测脚本(sensor_reboot_stress)

重启投影仪后检测指定传感器是否回连。每轮:重启 → 等设备上线 → 循环检测传感器是否能读到有效数据。报告 JSON 落在 `reports/stress-test/sensor/`(已 gitignore)。

| 可配参数 | 说明 | 默认 |
|---|---|---|
| `sensor` | **传感器序列(下拉框)**:`gsensor` / `tof` | `gsensor` |
| `iterations` | 循环次数 | `100` |
| `reboot_timeout` | 设备上线超时(秒) | `80` |

以下为**硬编码常量**(改脚本顶部,按项目决策不做成前端参数):`SENSOR_WINDOW`(采集窗口 5s)、`POLL_COUNT`(上线后检测 8 次)、`POLL_INTERVAL`(检测间隔 3s)、`PASS_THRESHOLD`(通过阈值 98%)。

要点:

- **gsensor**:`cat /dev/gsensor`,每行 7 个逗号分隔整数,第 7 字段为有效加速度(静止约 9800 mm/s²),窗口内至少 3 个有效样本判 OK。
- **ToF**:`cat /sys/class/nd_tof/nds01/ranging_data_fast`,能解析出 `depth:` 即 OK。
- **su 适配**:本设备 su 是"裸 su"(`su -c` 报 `invalid uid/gid -c`),读取会依次尝试多种 su 方式,最终回退到 **stdin 管道交互式 su**(复刻手动 `adb shell` → `su` → `cat` 流程)。运行时会打印实际生效的读取方式。
- **判定**:成功率 ≥ `PASS_THRESHOLD`(硬编码 98%)判 PASS;退出码 0 = PASS,非 0 = FAIL。中断时打印已完成汇总,不做 PASS/FAIL 判定。

---

## APP 冷热启动压测脚本(app_launch_stress)

测量目标 APP 冷启动 / 热启动耗时(到首帧)。**"要跑就三个一起跑"**:脚本顶部 `APP_PRESETS` 内置 Netflix / Prime Video / YouTube TV 三个(含各自启动 Activity,可留空自动解析),按顺序**每个 APP 各跑 `iterations` 轮**。报告 JSON 落在 `reports/stress-test/app-launch/`(已 gitignore),**每个 APP 一份**(`app_launch_<mode>_<label>_*.json`)。

| 可配参数 | 说明 | 默认 |
|---|---|---|
| `mode` | **启动方式(下拉框)**:`cold`(冷)/ `hot`(热),冷热分开跑 | `cold` |
| `iterations` | **每 APP** 循环次数 | `100` |
| `settle_sec` | 每次启动后停留(秒),让 APP 稳定后再进入下一轮 | `3` |
| `gap_sec` | 每次启动前等待(秒) | `1` |
| `launch_timeout` | 单次启动超时(秒) | `20` |
| `p95_threshold_ms` | **判定阈值**:每个 APP 有效样本 p95 ≤ 此值判该 APP PASS;`0` = 不判定 | `2000` |

**被测 APP 硬编码在脚本顶部的 `APP_PRESETS` 列表**(按项目决策不做成前端参数):要换 APP / 改名单,直接编辑列表(加 `label` / `package` / `activity` 即可)。

要点:

- **冷启动**:每轮先 `am force-stop` + `pidof` 确认后台进程已死,再 `am start -W` 测冷启动时间。
- **热启动**:每个 APP 开始前确保进程存活(若死了先冷拉起一次,**不计入样本**);每轮先按 HOME 退到后台,再拉起测回到前台时间。
- **测什么**:主指标 = `am start -W` 的 `TotalTime`(ms);同时清空并抓 logcat `Displayed` 交叉验证(冷启动必有;热启动窗口不重建,通常没有,缺失时以语义校验为准)。Android 12+ 还会打印 `LaunchState: COLD/WARM/HOT`,脚本**硬校验**与所选模式一致——冷启动却拿到 HOT(进程没杀干净)、热启动却拿到 COLD(进程死了),该轮直接判 NG 不计入样本;系统没报状态(`UNKNOWN`/空)时以进程级校验(force-stop / pidof)为准。
- **判定**:每个 APP 各自 p95 ≤ `p95_threshold_ms` 判该 APP PASS,**整体全过才 PASS**;中途某个 APP 无法解析启动 Activity 也会判 FAIL。退出码 0 = PASS,非 0 = FAIL。中断时打印已完成汇总,不做 PASS/FAIL 判定。
- **屏幕**:启动时 `KEYCODE_WAKEUP` + `svc power stayon true`,避免息屏/灭屏污染启动耗时。

---

## 性能监控脚本(perf_monitor)

手工测试 / 播放 APP 时,实时监控设备 **CPU / GPU / 内存** 使用率 + **前台 APP 的 CPU%**。选脚本后点设备卡上的"▶ 跑",**日志面板顶部会切出一块实时曲线图**(200px,和日志一起随任务绑定;切走不残留,切回来从重放重建)。**跑完会打印一份判定块**(见下)。

| 可配参数 | 说明 | 默认 |
|---|---|---|
| `interval_sec` | **基准拍周期 T**(秒),下限 1.0(每拍一次 adb,0.5s 在设备上采不出来) | `2.0` |
| `duration_sec` | 时长(秒);`0` = 手动停止 | `0` |
| `watch_pkg` | 关注的应用(**下拉选择**);选「不关注」= 只看前台是谁,选了就额外记"该 app 掉出前台/崩溃"事件 | *(不关注)* |
| `key_ini` | **长跑时定时发按键**(下拉选择);选一个 `ir_sequences/*.ini`,后台就按这个 ini 反复发按键 —— 用来压掉播放类 app 的「还在看吗」空闲提示 | *(不发送按键)* |

> **`interval_sec` 不是分级采样的替代品**:它是**基准拍周期 T**,MED/SLOW 两层的周期都**由 T 推导**(目标固定 5s / 30s)。所以调它 = 同时调三层,而不是关掉某一层。
>
> **前台 APP 采集恒开,没有开关**:「被压测的 app 挂了 / 掉到后台」是长跑里最该被看见的故障,而 `fg_*` 是唯一能看见它的信号;关掉只省一次 `dumpsys window` + `pidof` + `/proc/<pid>/stat`。
>
> **`watch_pkg` 为什么是下拉而不是填包名**:拼错一个字符会**静默废掉**唯一依赖它的那道门(APP),而报告看起来一切正常。
> 预置五个选项(改 `WATCH_PKG_CHOICES` 一处即可增删):「不关注」/ **YouTube TV** `com.google.android.youtube.tv` /
> **Netflix** `com.netflix.ninja` / **Prime Video** `com.amazon.amazonvideo.livingroom` /
> **本地媒体播放器** `com.mediatek.wwtv.mediaplayer`。
>
> **选错了不会误报异常,会告诉你"样本不足"**:`watch_pkg` 设了、但该应用整场没上过前台 → APP 门判
> **INCONCLUSIVE**,并在原因里写明(没装?没打开?)。**只有两种情况算真异常**:
> ① 它**先出现过**,之后掉出前台;② 它**仍在前台,但进程已经没了**(窗口还挂着 = 崩溃)。

> **`key_ini` 的节奏写在 ini 里,不在参数表里**:ini 中每个 step 的 `delay_ms` 既是「同一 step 内重复的间隔」,
> 也是「一轮跑完后的间隔」,所以**单步 ini 的 `delay_ms` 就是按键周期**。要改节奏就改文件,不再多一个数字对不上。
> 仓库附了一份模板 `ir_sequences/idle_keepalive.ini`(`KEYCODE_MEDIA_PLAY` 短按 @30 分钟)——**随便选、随便改**。
> (往 `ir_sequences/` **新加**文件后,下拉框要等重启服务或脚本改动才会出现;改已有 ini 的内容随时生效。)
>
> **它永远不会改变结论**:ini 路径写错、按键名不认识、设备掉线,都只记进报告的 `key_status`,判定块照常。
> 发送线程是 daemon —— 任务停止(包括「硬停」)时它随进程一起消失,**不会留下一个还在按键的进程**。
> 报告 `config` 里能看到 `key_sent`(真按下去几次)/ `key_failed` / `key_status`,长跑时这就是「keepalive 活着没有」的那一眼。

**四条曲线(颜色区分):** CPU 蓝 / GPU 绿 / MEM 黄 / 前台APP 红。GPU 或前台系列不可用(节点读不到)时自动隐藏对应曲线 —— **前台跟踪恒开,没有开关**。

- **X 轴 = 固定 1h 滚动窗口(墙钟时间)**:标签 HH:MM:SS;始终显示最近 1 小时、约 6 个 10min 刻度(`interval:10min` + `splitNumber:6` + `hideOverlap`),右缘跟随最新样本滚动,超窗数据点自动剔除。完整历史仍保留在缓冲里供 CSV 导出与**全程图 PNG 导出**。**不支持手动缩放**(固定窗口与 dataZoom 互斥)
- **Y 轴分两根**:左侧主轴 0-100% 给 CPU / GPU / MEM;前台APP 用右侧独立轴 0-400% —— 前台 CPU% 是"每秒消耗的 CPU 秒数",**多核时可以超过 100%**(上限 = 核数 × 100%;YouTube TV 播放实测 88.2%)。原需求本就允许"Y 轴可以不同"
- **长时检测友好**:采样缓冲上限 86400(24h@2s 全量保留);**导出的 chart.png 是全程图**(时间轴 = 任务从开始到结束的完整范围,横轴 0.5h 一个刻度),不再受屏幕 1h 实时窗口限制

**数据源(MT9676 真机探明;需说明:本机实为 MT9676,`ro.soc.model=MT9676 / ro.hardware=mt5896 / egl=mali.mt5873`):**

- **CPU%** `/proc/stat` 累计计数器差值(免 root)
- **内存%** `/proc/meminfo` `MemAvailable/MemTotal`(系统整体,免 root;本机仅 ~1.75GB RAM,此指标很有意义)
- **GPU%** `/sys/kernel/debug/mali0/dvfs_utilization` `busy_time/idle_time` 差值(**需 root**,`su 0 <cmd>`)
- **GPU 频率**(附带) `mali0/gpu_clock`
- **前台 APP CPU%** `dumpsys window` 取包名 → `pidof` 取 pid → `/proc/<pid>/stat` 的 `utime+stime` 差值。**不是 `top`**(本板一次 `top` 581ms,且它报的瞬时 %CPU 不可重复:同一包连读三次是 85.0 / 26.4 / 19.6),**也不是 `dumpsys cpuinfo`**(本机是 5 分钟滑动平均)

### 分级采样(为什么采集本身不拖累设备)

不是每拍都读全部节点,而是按代价分三层。**下面表里的 T 就是 `interval_sec`** —— 分级的对象是「每一拍读哪些节点」,不是「还要不要有一个采样间隔」:

| 层 | 周期 | 读什么 |
|---|---|---|
| FAST | 每拍 | `/proc/stat`、进程数、`/proc/uptime` |
| MED | 每 `ceil(5/T)` 拍 | `/proc/meminfo`、GPU 计数器 |
| SLOW | 每 `med*ceil(30/(med*T))` 拍 | `dumpsys window` + `pidof` + `/proc/<pid>/stat` |

**一拍只起一个 adb 进程**:八个 section 拼进同一条复合命令,用随机 nonce 做帧标记,设备侧的杂音撕不开 section 边界。T=2s 实测占空比 **9.06%**。PC 侧超时预算由设备实测代价自适应,不是硬编码常数。

没到期的指标**带上一拍的值**(前端的 `st` 列会显示 `h` = held),不是留空 —— 否则稀疏序列会从图上整条消失。**真掉线那一拍才是空的**,图上会真的断线。

### 收尾判定块

跑完在 stdout 打印一次(纯 ASCII,除最后一行外不出现 `report` 字面量):

```
=== perf verdict (judge v1-uncalibrated) ===
RESULT : OK
RUN    : ticks=39 ok=39 coverage=100% cadence=1.0x
DATA   : samples.csv=perf_..._183055.samples.csv events=0 judge_version=v1-uncalibrated
DEVICE : B0403374A2A508001F00 (B0403374A2A508001F00)
MEMORY : NO LEAK  delta=-0.7%  slope=-51.875%/h  blocks=7/7  n=13  avail_min=670.4MB
CPU    : avg=52.3%  p95=60.3%  max=62.8%  >=90% for 0.0s (load, not a fault)
APP    : STABLE  switches=0  pkgs=1  gone=0  pid_lost=0  pid_dead=0  watch=com.google.android.youtube.tv
EVENTS : timeout=0 offline=0 reboot=0 other=0
GATES  : all pass
COST   : cost_median=141ms duty=10.45%
STAT cpu     : min=44.3 avg=52.3 p50=50.8 p90=58.3 p95=60.3 max=62.8 n=38
STAT procs   : min=382.0 avg=384.0 p50=385.0 p90=385.0 p95=385.0 max=385.0 n=39
STAT mem     : min=61.3 avg=61.8 p50=61.6 p90=62.3 p95=62.7 max=62.7 n=13
STAT gpu     : min=0.0 avg=0.0 p50=0.0 p90=0.0 p95=0.0 max=0.0 n=12
STAT gpu_clk : min=552.0 avg=552.0 p50=552.0 p90=552.0 p95=552.0 max=552.0 n=13
STAT fg_cpu  : min=57.2 avg=57.2 p50=57.2 p90=57.2 p95=57.2 max=57.2 n=2

--- what these mean --------------------------------------------------------
RESULT   worst status across all gates. OK=all passed, WARN=suspicious but not
         a fault, FAIL=a gate failed, INCONCLUSIVE=too little data to have an
         opinion
RUN      coverage = ticks that returned data / ticks attempted (>=80% is
         healthy). cadence = median interval / the interval you asked for;
         1.0x is on time
DATA     the per-tick data file this run wrote, plus the threshold-set version
         it was judged with
DEVICE   adb serial; the short form is what the archived folder is named after
MEMORY   delta = first block -> last block. slope = %/h fitted over the whole
         run. A leak needs BOTH over threshold, so the two are supposed to
         disagree sometimes
CPU      share of all cores. Sustained high CPU is the point of a stress test,
         never a fault
APP      STABLE = one foreground app throughout. CHANGED = it switched
         (navigation and screen savers do that). GONE = the app you set as
         watch_pkg disappeared - the one case worth chasing
EVENTS   timeout/offline/reboot are faults in the PC<->device link, not faults
         in the device
GATES    every gate that did not pass; 'all pass' means nothing was flagged
COST     cost_median = what one sample cost the PC. duty = that as a share of
         the interval, i.e. the load this monitor adds

  html   = <abs path>.html
  report : <abs path>.json
```

**每一行下面为什么是这些字**,都印在块尾的 `--- what these mean ---` 里 —— 不用回文档翻。
两个看起来"自相矛盾"的地方是**设计如此**:

- `MEMORY : NO LEAK delta=-0.7% slope=-51.875%/h` —— 判定要求 `delta` 与 `slope` **同时**越限;
  短跑里 `slope` 只在 15 秒的窗口上拟合,抖到 -51%/h 完全正常,**单看它没有意义**。
- `STAT gpu : min=0.0 avg=0.0` 而 YouTube 正在播 —— 见本节末尾那条。

`RESULT` ∈ `OK / WARN / FAIL / INCONCLUSIVE`。中途只发**事件**(事实),判定只在收尾给一次 —— 两者是分开的通道。中途事件行长这样:

```
[perf] t=   32.0s | [!] offline_start: device unreachable
[perf] t=   44.0s | [i] offline_end: device reachable again
```

`!` = 告警级(掉线/超时/错帧/GPU 读不到),`i` = 信息级(恢复/重启/计数回退/前台 pid 变化),`?` = 未知类型(脚本可能先上新类型)。

跑的过程中每个采样周期还会打一行实时概览(前端曲线图就吃这一行)。**注意这行不是脚本打的,是前端从 `PERF|` JSON 渲染出来的**(表格里的空格是**定宽对齐**用的:缺值也占满自己的格子,不然某一拍掉线会让后面每一列整体左移、竖着读就断了):

```
[perf] t= 272.0s | cpu= 49.0% | gpu=  0.0% | mem= 64.0% | fg= 57.3% | clk= 552MHz | st=     ok:fffhhhhhh | pkg=com.google.android.youtube.tv
```

| 字段 | 含义 |
|---|---|
| `t` | 距开跑的秒数 |
| `cpu` `gpu` `mem` `fg` | 本拍四个指标的**瞬时值**,`fg` = 前台 APP 的 CPU%(>100% 正常,它是多核累加) |
| `clk` | **GPU 频率**(MHz),从 `mali0/gpu_clock` 读;不是使用率。GPU 使用率是 `gpu` 那一栏 |
| `st` | **逐指标状态串**,`<本拍结果>:<9 个字符>`,顺序固定 `cpu,procs,up,mem,gpu,gpu_clk,fg_pkg,fg_pid,fg_cpu` |
| `ok` | 本拍结果,∈ `ok / partial / timeout / offline / error` |
| `pkg` | 本拍读到的前台应用包名 |

**逐字对照:** 前三位是 FAST 层,每拍都真读 —— 除了第一拍的差分基线(`b`),恒为 `f`(本拍新值);`f f f` = CPU / 进程数 / uptime。后六位是 MED 层(mem / gpu / gpu_clk)与 SLOW 层(fg_pkg / fg_pid / fg_cpu),它们**不是每拍都读**,所以本拍要么 `h`,要么 `n`。这一行六个全是 `h`,因为跑到 272 秒时 MED 和 SLOW 早就答过话了,**值一直在被沿用**。

| 字符 | 含义 |
|---|---|
| `f` | 本拍**新采到**了值 |
| `h` | 本拍未到期,沿用上一拍的**保持值**(零阶保持) |
| `n` | 本拍未到期,**且还没有任何可沿用的值**(该通道至今没答过话) |
| `b` | 该指标的第一拍基线 |
| `x` | 本拍**采失败**(掉线 / 超时 / 错帧),值置空 |
| `a` | 设备上**没有这个节点**(比如没 root 读不到 GPU) |
| `d` | 该指标被禁用 |

> 看状态串是**最低成本的体检**:一长串 `samples.csv` 里如果 SLOW 拍**永远是 `n`、从没出现过 `b`/`f`/`h`**,说明那条通道根本没在工作 —— 而覆盖率、门限这些**形状类**指标全都看不出来。v2.9.0 修掉的就是这么一个 bug。

> **判定阈值尚未标定**:所有门限目前是设计假设,**一份完全健康的报告也会显示 `v1-uncalibrated`**,避免假信心。标定需要一次健康的 8 小时基线跑。风险清单见 [TODO.md](TODO.md) §6。

### 中文 HTML 报告(给人看的那一版)

控制台那块是给**日志流**看的,排版受终端限制、且必须纯 ASCII。同一份结论还会写成 `perf_<设备>_<时间>.html`,**给人看**——中文、表格、带指示描述:

- 顶部**四色结果横幅**:通过(绿)/ 注意(黄)/ 异常(红)/ 样本不足(灰);未标定时压一条告警条。
- ①**指标判定** —— 每行一项结论,后面跟一句中文说明(和控制台是同一张 row 表渲染的,不会各说各话)。
  APP 那行的结论有四种:`STABLE` / `CHANGED` / `GONE`(真坏) / `NEVER SEEN`(整场没出现,无法下结论)。
- ②**通道分布** —— 每个指标采到多少拍 / 应采多少拍,带单位;数字看着离谱时先看这里。
- ③**检查门明细** —— **9 道门全列**(不只是没过的),没过的原因写在行内。
- ④**链路事件** —— 掉线 / 超时 / 重启 / 计数器回绕的时间点,**每条都带墙钟时刻**(如 `18:39:12`,悬停看完整日期),旁边才是「相对时刻」那趟跑的第几秒。

**自包含**:CSS 内联、无 JS、不拉任何图表库 —— 目标是能把这个文件拷出归档目录,在任何一台**从没装过 PPTP** 的机器上、几年后打开,还看得懂。它**不是第二份判定**:由 `report.json` 的同一份 payload 渲染,写失败也只是少个附件,不会改变结论。

**一键导出三件**:点击"导出"按钮,除原有 log.txt(含原始 `PERF|` 采样行)外,同时下载 **perf.csv**(可进 Excel,含 `t_sec` + `t_wall` 墙钟列)+ **chart.png**(深色底 2x 图,**全程时间轴,0.5h 一个刻度**)。Chrome/Edge 单次点击允许多个下载;Firefox 可能拦截后两个,届时分次导出或改用 Chrome。无性能数据的任务只导出 log.txt。

**归档**:任务结束时把**四件**一起收进 `archive/perf/<时间>_perf_monitor_<设备>/`:
`perf_<设备>_<时间>` 打头的 **`.json`**(判定报告,机器读)、**`.html`**(中文报告,人读)、**`.samples.csv`**(28 列 + 注释块,**全分辨率原始逐拍转储**)、**`.events.csv`**(链路事件)。判定只依赖内存里的证据,不读 CSV —— 所以**即使 samples.csv 被 Excel 独占打开而写不进去**,脚本会降级到 `csv_degraded` 继续跑,判定仍然成立(丢的是原始转储,不是结论)。

> **wire 契约(v2.8.0 起)**:每拍一行
> `PERF|{"type":"sample","clock":<epoch_ms>,"t":...,"st":"ok:fffhhhhhh","gap_ms":0,"cpu":...,"gpu":...,"mem":...,"fg_cpu":...,"fg_pkg":...,"gpu_clk":...}`;
> 启动时 `PERF|{"type":"meta","sources":{...}}`;中途事件 `PERF|{"type":"event","clock":<epoch_ms>,"t":...,"kind":...,"reason":...,"detail":...}`(与采样行一样带 `clock`,HTML 报告的事件时刻就是由它渲染的)。
> `st` = `<拍结果>:<9 个逐指标状态字符>`,顺序见 `cpu,procs,up,mem,gpu,gpu_clk,fg_pkg,fg_pid,fg_cpu`;
> `f`=新值 `h`=保持 `n`=未到期 `b`=基线 `x`=失败 `a`=缺失 `d`=禁用。
> 前端按 `type` 分发;**未知类型只打印一行提示、不回显原始 JSON**(否则一个新类型会在每次 WS 重连时把 JSON 刷满 console)。

> 播放视频时 GPU% 通常接近 0:视频解码走硬件专用解码器,CPU/GPU 主核都处于空闲,这是正常现象,不是监控失效。
>
> 同理 **`fg_cpu` 也可以是**真的 **0.0**:本地媒体播放器停在界面上时,进程的
> `utime+stime` 十分钟都不动(2026-09-14 实测:10 秒内 delta=0 ticks)。**0.0% 不代表读不到**,
> 代表这个 app 这一刻真的没在干活。要判断是"读不到"还是"真为 0",看 `st` 那一串:
> `f`/`h`=读到了,`x`=读失败,`a`=设备上没有这个节点。
>
> **低 CPU 的 app,单个 `fg_cpu` 值不要单独看**:它是"两个 SLOW 读之间的 CPU 秒数 ÷ 秒数",
> 而 SLOW 间隔是 30 秒。Netflix 停在界面上时实测每 10 秒只走 2–3 个 tick,但**偶尔会有一个
> 17 tick 的尖峰**(加载画面);落在尖峰上的那个 30 秒窗口就会报 0.7% 甚至 1.9%,落在安静段就只有 0.2%。
> 都是对的 —— **要看的是 `STAT fg_cpu` 的 p50/p95 分布,而不是某一次的值**。

---

## 电池充放电压测脚本(battery_inout_stress)

充电或放电过程中实时监控设备 **电量 / 温度 / 电压 / 状态**。**一次只跑充电或放电**(自动插拔做不到):选 `mode` = `charge`(充到 100% 稳定自动停)或 `discharge`(放电到设备关机自动停)。曲线只含 **电量 + 温度** 两条(perf_monitor 同款实时图表),报告 JSON 落在 `reports/stress-test/battery/`(已 gitignore)。

| 可配参数 | 说明 | 默认 |
|---|---|---|
| `mode` | **充/放电(下拉框)**:`charge` / `discharge` | `charge` |
| `serial_port` | **串口 COM(下拉框,运行时枚举)**:电量靠串口开启 health 轮询才实时 | `(空)` |
| `interval_sec` | 采样间隔(秒) | `10.0` |
| `temp_warn_c` | 温控告警阈值(°C,达到则告警) | `45.0` |
| `full_hold_sec` | 100% 稳定窗口(秒):满 100% 连续保持该时长才停;`0` = 一到 100% 就停 | `60` |

**为什么必须选串口**:本机 `ro.config.batteryless=true`,电池由外部 BMS MCU 管理,`dumpsys battery` 默认只显示缓存值。开启 health 轮询的 `set polling true` 受 SELinux 限制,adb shell 执行报 `Failed transaction 2147483646`,**只有串口 console root shell 能开**(波特率 115200)。开启后 dumpsys 才实时刷新。试跑时确认 PC 上哪个 COM 口连到投影仪串口,在前端下拉框选它(选错/未选时脚本告警并继续用缓存值,曲线可能近似直线)。

**中止条件**(单次):① 手动"中断";② 充电到 **100% 稳定**(status=full,或连续满 100% 达 `full_hold_sec`);③ **设备关机**(连续 2 次读电池失败,adb 失联)。

**保留的错误检测**:电量**跳变检测**(方向感知:充电时骤降 / 放电时骤升 / 状态未知大跳变都标异常,`[batt] warning: level jump [...]` + 报告 `summary.jump_counts`)+ **温控告警**。

**曲线(前端)**:`电量` 蓝(左轴 0-100%)+ `温度` 红(右轴 °C),固定 1h 滚动窗口(6×10min 刻度,同 perf_monitor);控制台可读行为英文 `[batt] 15:20:55 (10.0s) level=57% temp=40.8C voltage=11.6V status=charging`,跳变时追加 ` [!] jump[异常降低] 57->55%`(jump_type 为脚本 ASCII 字段)。

**一键导出三件**:log.txt(含原始 `PERF|` 采样行)+ **batt.csv**(表头 `t_sec,t_wall,level_percent,temp_c,voltage_mv,status,jump_delta,jump_type`)+ **chart.png**(全程图,横轴 0.5h 一个刻度)。同 perf_monitor,Chrome/Edge 一次下载三个,Firefox 可能拦后两个。**不输出 Excel**。

**放电测试结束后的设备卡**:放电模式以"设备关机"自动停,任务结束后设备不在 `adb devices`,平台会保留一张 **"放电关机"专用卡**(虚线红边 + "设备已关机"状态 + "放电关机"徽标,仿重启测试的"临时离线"卡)——测完卡片不消失;重新开机后自动恢复在线卡,删除对应任务后卡片移除。

> **语言规则**:脚本文件 100% 英文/ASCII(含参数标签);前端 UI 文案用中文,**但日志控制台(#log-console)输出全英文**(v2.5.1 起)。因此电池脚本的**参数弹窗标签是英文**,这是"代码文件无中文"规则的直接结果。

---

## 状态持久化(localStorage)

平台会把以下用户选择存到浏览器 localStorage,F5 刷新后保留:

- 当前选中的设备(`selectedDeviceSerial`)
- 每台设备选的脚本(`deviceScripts`)
- 每台设备选的序列(`deviceSequences`)
- 每台设备、每个脚本配置的参数(`deviceParams`)

服务器数据(devices / scripts / tasks / server info)不持久化,每次都从后端拉。

key 名:`pptp.deviceState.v1`,存在 `localStorage` 里。

---

## 目录结构

```
ProjectorPressureTest/
├── server.py              # 后端单文件 (FastAPI, 2084 行)
├── start.bat              # 一键启动(启动成功自动关闭,失败保留信息)
├── server_window.ps1      # PPTP-Server 窗口脚本(实时显示 uvicorn 日志 + 写文件)
├── stop.bat               # 一键停止
├── README.md              # 本文档
├── docs/
│   ├── HANDOFF_PROMPT.md # 完整信息源(API、数据模型、设计决策、踩坑)
│   ├── SIMPLE-ARCHITECTURE.md  # 架构总览(数据流、脚本契约;活跃)
│   ├── FRONTEND_UX.md    # 前端交互设计沉淀(为什么这样做;活跃)
│   ├── SIMPLE-PRD.md     # 初版 v2.0 设计 PRD(已归档)
│   └── SIMPLE-PLAN.md    # 初版 1.5 天实施计划(已归档)
├── static/
│   ├── index.html         # 单页 HTML
│   ├── app.js             # 前端逻辑 IIFE
│   ├── style.css          # 样式
│   └── vendor/
│       └── echarts.min.js # ECharts 5.5.1 本地化(性能监控图表,~1MB,pin 版本)
├── scripts/               # 压测脚本
│   ├── ir_runner.py             # 红外序列(自包含:IRRemote 内联,默认无限循环,双通道注入,长按=down+hold+up)
│   ├── wifi_onoff_stress.py     # WiFi 开关压力(关/开循环 + wpa_cli 扫描,参数可配)
│   ├── wifi_reboot_stress.py    # WiFi 重启压力(reboot + 上线等待,参数可配)
│   ├── wifi_switch_stress.py    # WiFi 多网络循环切换(预置列表,参数可配)
│   ├── sensor_reboot_stress.py  # 重启 + 传感器(gsensor/ToF)回连压测,参数可配(含传感器序列下拉框)
│   ├── app_launch_stress.py     # APP 冷/热启动耗时压测(APP_PRESETS 内置 3 个 APP 一起跑,am start -W TotalTime + logcat Displayed)
│   ├── perf_monitor.py          # 性能监控(CPU/GPU/内存% + 前台APP CPU%,PERF| 采样流 → 前端实时图表)
│   ├── battery_inout_stress.py  # 电池充放电压测(充电/放电分开跑,电量/温度/电压,PERF| 采样流 → 前端电量+温度曲线,串口开 health 轮询)
│   └── bt_reboot_stress.py      # 重启 + 蓝牙音箱 A2DP 回连压测(reboot → 上线 → 轮询音箱回连,参数可配)
├── ir_sequences/          # IR 序列 .ini 文件
│   ├── 1.ini                    # 当前默认序列(KEY_VCR + KEYCODE_HDMI,5 字段格式)
│   └── KEY_REFERENCE.md         # 按键速查三张表:KEY_* 24 + KEYCODE_* 厂商 23 + 原生 26(手动维护)
├── reports/               # 脚本写报告的原始位置(脚本契约,gitignore)
│   └── stress-test/{wifi,sensor,app-launch,perf,battery}/   # 平台运行时会复制一份进存档
├── logs/                  # 运行期临时工作区(任务结束后内容会被搬进 archive/)+ 服务日志
│   ├── server.out.log / server.err.log
│   └── <task_id>_<时间>_<脚本>_<设备>.{log,logcat.log,serial.log}
└── archive/               # ★ 任务存档(永久保留,按模块分类)
    └── <模块>/{wifi,perf,battery,sensor,app-launch,ir,bt,other}/
        └── <时间>_<脚本>_<设备>/
            ├── stdout.log / logcat.log / serial.log
            ├── report.json    #   脚本报告(从 reports/ 复制过来)
            ├── chart.png      #   性能图表(浏览器渲染回传,perf/battery 才有)
            └── summary.json   #   清单:参数/状态/耗时/各文件行数/备注
```

> **用户只需要看 `archive/`。** `logs/` 与 `reports/` 都不能删:`logs/` 是运行期工作区 + 服务自身日志;
> `reports/` 是脚本契约(脚本独立 CLI 跑时也写这里),平台运行时把报告**搬**进存档。

---

## API 一览(完整)

| Method | 路径 | 用途 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/api/server/status` | uvicorn PID + uptime + 任务计数 |
| POST | `/api/server/shutdown` | 优雅关停(级联 CTRL_BREAK + 退出) |
| GET | `/api/devices` | ADB 设备列表 |
| GET | `/api/scripts` | scripts/ 下脚本列表(含 has_params 标志) |
| GET | `/api/scripts/{name}/params` | 读脚本自描述的参数 schema(`--dump-params`) |
| GET | `/api/sequences` | ir_sequences/ 下 .ini 列表 |
| GET | `/api/sequences/{name}` | 读单个 .ini 内容 |
| PUT | `/api/sequences/{name}` | 写整个 .ini 内容 |
| DELETE | `/api/sequences/{name}` | 删 .ini(default 拒绝) |
| POST | `/api/sequences` | 新建 .ini(写最小模板) |
| POST | `/api/run` | 启动任务(同设备并发返回 409) |
| POST | `/api/stop/{task_id}` | 单任务中断(CTRL_BREAK) |
| POST | `/api/tasks/force-stop-by-device/{serial}` | SIGKILL 该设备任务 |
| POST | `/api/tasks/force-cleanup` | SIGKILL 全部 + 清空任务 |
| POST | `/api/tasks/cleanup` | 删所有终态任务 + log |
| POST | `/api/adb/reconnect` | adb kill-server + start-server |
| GET | `/api/tasks` | 全部任务状态 |
| GET | `/api/tasks/{id}` | 单任务 |
| GET | `/api/tasks/{id}/log?source=` | 单通道日志尾部读(默认 `stdout`;`logcat` / `serial`;非法 source → 400) |
| DELETE | `/api/tasks/{id}` | 从列表移除任务(存档保留) |
| GET | `/api/serial/ports` | 本机 COM 口列表(串口勾选框用;无 pyserial → 空列表) |
| POST | `/api/tasks/{id}/archive/artifact` | 浏览器回传图表 PNG 进存档(名称白名单 + 8MB 上限) |
| POST | `/api/tasks/{id}/archive/reveal` · `/api/archive/reveal` | 资源管理器打开某任务存档 / 存档根目录 |
| GET | `/api/archive/stats` | 存档总数与总占用 |
| WS | `/ws/logs/{id}?sources=` | 实时推送(RAF 批处理);前端只用默认的 `stdout` |

完整数据模型、状态机、bug 历史见 [docs/HANDOFF_PROMPT.md](docs/HANDOFF_PROMPT.md)。

---

## 故障排查

- 设备列表为空
  确认命令行 `adb devices` 能看到设备。

- **找不到日志**
  去 `archive/<时间>_<脚本>_<设备>/`(任务卡上有 `存档` 按钮直接打开)。`logs/` 只是运行期临时目录,任务一结束内容就搬走了。

- 脚本运行报错
  看该任务存档里的 `stdout.log`,或前端日志面板。

- 存档里没有 `chart.png`
  跑完的时候浏览器没开着。图表由浏览器渲染(Tab 关了就画不出来),事后重新打开页面看那个任务会自动补传。三份日志和 `report.json` 不受影响。

- 串口下拉框是空的
  没装 pyserial(`pip install pyserial`)或没插 USB 转串口线。命令行 `python -m serial.tools.list_ports -v` 可确认。

- **想知道串口到底采到没有**
  看任务结束时日志栏最后那行 `[archive] ...`,里面有每个通道的行数;异常会带原因(如 `serial 0L (failed: port not present)`)。

- 端口 8000 被占用
  跑 stop.bat,或手动 `netstat -ano | findstr :8000` 找进程杀掉。

- 想彻底重置
  跑顶栏"重置"按钮(杀所有任务 + 重启 ADB)。**注意:重置不会删 `archive/`**,存档要清理得手动去目录里删。

- ir_runner 跑起来报 "Key not found" 或 "not in CODE_NUM_MAP"
  检查 ini 里 `code` 字段是否在 [ir_sequences/KEY_REFERENCE.md](ir_sequences/KEY_REFERENCE.md) 三张表内:
  - `KEY_*`(24 个)→ `IRRemote.CODE_NUM_MAP`,新增需改 `scripts/ir_runner.py`
  - `KEYCODE_*` 厂商键(23 个)→ 映射到 Android keycode 名,新增需改 `ANDROID_KEYCODE_MAP`
  - `KEYCODE_*` 原生键 / 未映射键 → 名称原样透传,一般无需改码

- KEYCODE 长按没反应
  已知限制:`KEYCODE_*` 只有短按 `Short` 生效,`LongXXXX` 静默失败(命令不存在,不注入按键)。要用长按只能走 `KEY_*` 一族(需 userdebug/root)。

- WiFi 脚本扫不到网络 / 连接报 SecurityException
  本设备上 wpa_cli 扫描和 `cmd wifi connect-network` 都需要 root。确认脚本参数里 `use_su` 开着(默认 true);若设备免 root,手动关掉再跑。su 用 AOSP 风格 `su 0 <cmd>`,`su -c` 在本设备会报 `invalid uid/gid '-c'`。

- sensor_reboot 读不到传感器数据
  传感器节点(`/dev/gsensor` / `sys/class/nd_tof/...`)需要 root/su 才能读。本设备 su 是"裸 su",脚本会自动尝试多种 su 方式(含 stdin 交互式 su),运行日志会打印实际生效的读取方式;若全部失败,先确认设备已开 root/su。`dumpsys sensorservice` 拿不到这类节点数据。

- 长按没生效 / 设备无响应
  默认 IR event 路径是 `/dev/input/event1`(从原 keyevent.txt 推断)。如果你的设备 event 路径不同:
  - CLI 调试: 加 `--device-event-path /dev/input/eventN`
  - 环境变量: `set IR_EVENT_PATH=/dev/input/eventN`(Windows) / `export IR_EVENT_PATH=/dev/input/eventN`(Linux)
  - 直接改 [scripts/ir_runner.py](scripts/ir_runner.py) 顶部 `DEFAULT_EVENT_PATH` 常量

- 想换设备 event 路径但 PPTP 平台不传 CLI 参数
  当前平台透传的 `--device-event-path` 还没接(改中)。临时方案:设环境变量后重启 uvicorn,所有 ir_runner 任务都会读到。

- 想改平台代码后没生效
  浏览器 **Ctrl+F5** 硬刷(平台加了 `?v=2.5.1` + NoCache 中间件,普通 F5 可能拿到缓存)。

- battery_inout_stress 电量曲线像直线 / 报"polling not enabled"
  确认串口选对了:电量由外部 BMS 管理,必须通过串口 console 开 health 轮询才实时。选错 COM / 串口终端软件占着口 / 波特率不对都会导致开启失败,脚本会打印 `[batt] warning` 并继续用缓存值。先关掉串口终端软件,再在参数弹窗里选对 COM 口重跑。

---

## 后续(本期不做)

本期已实现:设备列表、脚本列表、IR 序列选择 + 创建、modal 配置、脚本参数前端可配(params,按 设备×脚本 独立持久化,含 `select` 下拉框类型)、WiFi 压测脚本(wifi_onoff / wifi_reboot / wifi_switch)、传感器压测脚本(sensor_reboot_stress,gsensor / ToF)、APP 冷热启动压测脚本(app_launch_stress,cold / hot 分开跑,APP_PRESETS 内置 Netflix / Prime Video / YouTube TV 三个一起跑,am start -W TotalTime + logcat Displayed 交叉验证,每 APP 各自 p95 判定、整体全过才 PASS)、性能监控脚本(perf_monitor,CPU/GPU/内存% + 前台APP CPU%,实时图表 + 三件套导出)、电池充放电压测脚本(battery_inout_stress,充电/放电分开跑,电量/温度/电压实时监控,电量靠串口开 health 轮询,100% 稳定或关机自动停,跳变/温控检测保留)、localStorage 持久化、强制停止、重启 ADB、KEYCODE_* 上层注入(厂商 23 + 原生 26 + 透传,user 版可用)。

本期仍不做:
- 报告生成、历史日志检索
- 设备分组、脚本版本管理、收藏
- 多用户、鉴权
- Docker、跨平台(Linux/macOS)
- 数据库(全内存 + 文件 + localStorage)
- 前端构建工具、JavaScript 框架

详细"显式不做"清单见 [docs/SIMPLE-PRD.md §3](docs/SIMPLE-PRD.md#3-显式不做out-of-scope)。
