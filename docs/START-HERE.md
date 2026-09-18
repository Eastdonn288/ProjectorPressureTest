# PPTP — 从这里开始(新会话入口)

> 你是下一任接手 PPTP 的 Claude / 任意 LLM。本文件只回答两件事:**这是什么**,以及**动手前先读哪**。
> 项目的细节不在这里 —— 它在 `docs/` 下**按职责分开**,必须**懒加载、按需读**。
> **严禁从某份文档的第一节开始通读**,也**严禁 `cat` 整个文件**。

---

## 0. 这是什么

**PPTP — Projector Pressure Test Platform**。一个**本地单机**的 Web 压测平台:在多台 ADB Android
投影仪设备上启动 Python 压测脚本(红外遥控模拟、WiFi 开关/重启/切换、蓝牙回连、APP 冷热启动、
电池充放电、性能监控),实时看每台设备输出的日志。脚本用 `PARAMS` 自描述可配置项,
前端自动识别并弹窗配置。

设计原则:

- **纯本地 / 单机** —— 所有数据落本地,无鉴权、无远程、单用户
- **vanilla JS,无前端框架** —— 单一 IIFE 模块
- **后端最小化** —— FastAPI 单文件
- **只一个跨设备数据源:ADB** —— WebSocket 推 ADB 进程 stdout

技术栈、目录结构、API 面、数据流,以及**「不存在的东西」清单** → [ARCHITECTURE.md](ARCHITECTURE.md)。

---

## 1. 先读哪 —— 按任务选,不要通读

| 你要做的事 | 读这份 |
|---|---|
| 不知道某个东西存不存在 / 系统怎么组成 | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 这个现象眼熟吗 / 上次的规矩是什么 | [PITFALLS.md](PITFALLS.md) |
| 我想改某个设计 —— 用户批过没有 | [DECISIONS.md](DECISIONS.md) |
| 动前端 / UI 交互 / 样式 | [FRONTEND.md](FRONTEND.md) |
| 动脚本 stdout / 报告 / 参数表 | [REPORT_FORMAT.md](REPORT_FORMAT.md) |
| 动 `perf_monitor` | [PERF_MONITOR_V2.md](PERF_MONITOR_V2.md) |
| 这是已知但没修的问题吗 | [../TODO.md](../TODO.md) |
| 这一版改了什么 | [../CHANGELOG.md](../CHANGELOG.md) |
| 用户视角怎么用 / 怎么排查 | [../README.md](../README.md) |

⚠ **先读这一条再碰代码**:本机有 **E-SafeNet 透明加密**,某些 `.py` 文件带着密文壳,
`Read` / `Grep` 会**静默失效**(0 命中、**不报错**)。完整的现象、规则和逃生口在
[PITFALLS.md](PITFALLS.md) **#0**;当前受影响文件清单在 [../TODO.md](../TODO.md) §5。
**看到 Grep 0 命中时,先怀疑密文壳,再怀疑代码不存在。**

---

## 2. 怎么跑

```bash
# 启动(开发)
cd E:\ProjectorPressureTest
D:\Conda_Environments\dev_env\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8000

# 浏览器打开 http://127.0.0.1:8000

# 验证
curl http://127.0.0.1:8000/healthz
```

日常用 `start.bat` / `stop.bat` / `server_window.ps1`(`server_window.ps1` 负责设
`PYTHONIOENCODING=utf-8` —— 少了它脚本 stdout 会按 GBK 编码,见 [PITFALLS.md](PITFALLS.md) #35)。

Python 依赖:`pip install fastapi uvicorn pydantic` + **`pyserial`**(可选但强烈建议)。
`pyserial` **不是**给服务端自己用的 —— 平台靠它枚举 COM 口(`serial.tools.list_ports.comports()`,
读的是 Windows 注册表 `HARDWARE\DEVICEMAP\SERIALCOMM`,和「设备管理器」同源),
`battery_inout_stress` 也靠它开串口。**没装的表现极具误导性:任务照跑、没有任何报错,
只是设备卡上的 COM 下拉框永远是空的**(见PITFALLS.md #36)。装的时候务必用**启动服务端的那个 Python**
(`start.bat` 会挑 PATH 里第一个 `python`,不一定是你在 conda 里装过的那个 ——
服务端窗口启动时打印的 `[OK] Python: ... at <路径>` 才是真身)。

Python 解释器固定用 `D:\Conda_Environments\dev_env\python.exe`(bash PATH 里没有 `python`)。

---

## 3. SOP —— 接到任务之后

1. **不要直接写代码。** 先 `Read` 相关源文件 + `grep` 相关函数名,确认现状。
2. **按脚本找入口**:IR 序列 → `scripts/ir_runner.py`(自包含 IRRemote,无外部依赖)+
   `ir_sequences/*.ini`;WiFi → `scripts/wifi_*.py`(共用契约 `--device` + `--params` +
   `--dump-params`);传感器 → `scripts/sensor_reboot_stress.py`;APP 冷热启动 →
   `scripts/app_launch_stress.py`(`APP_PRESETS` 硬编码在顶部);性能 → `scripts/perf_monitor.py`;
   电池 → `scripts/battery_inout_stress.py`。脚本的完整清单见 [../README.md](../README.md)。
3. **列影响面**:后端 → `server.py` 哪个 route / 函数;前端 → `static/app.js` 哪些函数、
   `state` 哪些字段;状态持久化 → 是否需要更新 localStorage。
4. **对照 [DECISIONS.md](DECISIONS.md)。** 如果用户的请求跟已批准的设计冲突,
   **先提出矛盾再写代码**,不要静默回退。
5. **写代码 + 验证**:后端改完强杀 uvicorn 重启;前端改完浏览器 **Ctrl+F5 硬刷**
   (必须 F5 不够,因为有 `?v=`);端到端用 `curl http://127.0.0.1:8000/api/...` 测。
6. **改前端的交互模型之前先读 [FRONTEND.md](FRONTEND.md)**,那里是「为什么这样定」的唯一归属。

---

## 4. 完成前自检

- [ ] 后端:`/healthz` 200;修改过的 route 用 curl 测过
- [ ] 前端:Ctrl+F5 后 F12 console 无 JS 报错
- [ ] state 持久化(如有):改 → F5 → 应保持
- [ ] 没有"上一版的旧实现"残留(grep 一下 `// TODO`、`// 废弃` 之类)
- [ ] 没有实现 [ARCHITECTURE.md](ARCHITECTURE.md) 末节列的「不存在的东西」
- [ ] **改了脚本 stdout** → 过一遍 [REPORT_FORMAT.md](REPORT_FORMAT.md) §8 自查清单:
      `report :` 仍是最后一行、`html =` 用 `=`、`--dump-params` 仍只吐一行 JSON、
      `reports/stress-test/<模块>/` 下 `.json` 与 `.html` 同 stem 成对且无孤儿
- [ ] 改了 `_pptp_report.py` / 报告渲染 → `perf_monitor.py --selftest` 必须仍 0 failures
      (尤其 `exactly one sniffable report path, on the last line` 与 `html line is not sniffable`)
- [ ] 版本号全库一致:`server.py` 的 `version="x.y.z"`、healthz 返回的 version、
      `static/index.html` 的 `?v=`(3 处),并在 CHANGELOG / README 同步

---

## 5. 常见请求 → 去哪找

| 用户说 | 先看 |
|---|---|
| "X 不显示 / 位置不对" | `static/style.css` 对应 class,查 overflow / z-index / position |
| "切换设备 Y 不更新" | `state.selectedDeviceSerial` + `renderScripts()` + `onSelectDevice()` |
| "刷新页面 Z 丢了" | localStorage 持久化(`STORAGE_KEY` 与 save/load 点) |
| "ir_runner 行为不对" | `scripts/ir_runner.py` 的 `argparse` 与 infinite loop 逻辑 |
| "新增设备类型 / 操作类型" | 后端 `api_run` 校验 + 前端 dropdown 配置 |
| "样式丑了" | `static/style.css` 找对应 class(`.device-card` / `.script-card` / `.btn` / `.modal`) |
| "perf 判定不对 / 阈值怎么来的" | [PERF_MONITOR_V2.md](PERF_MONITOR_V2.md) §12;残余风险见 [../TODO.md](../TODO.md) §6 |
| "加个新功能" | 找最近的相似功能(modal / 新端点 / 按钮)当模板 |

---

## 6. 红线 —— 不要做的事

- ❌ **不要** 修改 `state` 的字段名 —— 下游所有 `state.X` 引用会一起断
- ❌ **不要** 用 ES modules / 任何前端框架 —— 保持 vanilla JS 单文件
- ❌ **不要** 用 SQLite 替代内存 —— 全平台靠内存 + 文件 + localStorage
- ❌ **不要** 加 `console.log` 调试(用户看不到);关键提示用 `console.warn`
- ❌ **不要** 假设有 Python 虚拟环境 —— 固定用 `D:\Conda_Environments\dev_env\python.exe`
- ❌ **不要** 在 dev 时关掉 `STORAGE_KEY` 持久化
- ❌ **不要** 大改 README / 文档,除非用户要求

**前端交互模型另有一组禁区**(不要在 2s 轮询里塞副作用、不要绕过 WS 帧过滤、
只有一个全局 `#log-console` / `#perf-chart`、不要在 hidden 元素上 `echarts.init` 等)
—— 见 [FRONTEND.md](FRONTEND.md);**已批准的设计决策全表**见 [DECISIONS.md](DECISIONS.md)。

---

## 7. 常用一行命令

```bash
# 查 API 路由
grep -nE "^@app\.(get|post|put|delete|websocket)" server.py

# 查前端 state 字段被哪里用
grep -nE "state\." static/app.js | head -50

# 查某个 CSS class
grep -nE "^\.device-card " static/style.css

# 重启服务(开发用)
netstat -ano | grep ":8000.*LISTENING" | awk '{print $NF}' | head -1 | while read pid; do taskkill /F /PID $pid; done
D:\Conda_Environments\dev_env\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8000

# 端到端测试
curl -X POST http://127.0.0.1:8000/api/sequences -H "Content-Type: application/json" -d '{"name":"test_seq"}'
curl http://127.0.0.1:8000/api/sequences
```

> ⚠ `grep` 对**带密文壳的 `.py`** 静默失效(见 §1 的警告与 [PITFALLS.md](PITFALLS.md) #0)。
> 上面这几条命令里凡是 `grep` 一个 `.py` 的,**0 命中不等于不存在**。
