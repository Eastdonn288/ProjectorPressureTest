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
- 任务控制:运行 / 中断 / 硬停 / 重置,所见即所得
- **IR 序列**:ir_sequences/ 下的 .ini 文件定义按键顺序,前端 modal 选取并绑定到设备

后端 + 前端合计约 **2900 行代码**(server.py 870 + app.js 1249 + style.css 638 + index.html 110)。

---

## 5 分钟上手

### 1. 准备环境

- Python 3.10 或更高
- ADB 已安装并加入 PATH
- Node.js 不需要

### 2. 安装依赖

```cmd
pip install fastapi uvicorn pydantic
```

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
- `code` `KEY_HOME` / `KEY_ENTER` 等(24 个 KEY_NAME,见 [ir_sequences/KEY_REFERENCE.md](ir_sequences/KEY_REFERENCE.md),代码里硬编码在 `IRRemote.CODE_NUM_MAP`)
- `Short` 或 `LongXXXX`(XXXX 为长按毫秒,如 `Long3000` = 长按 3 秒)
- `delay_ms` 重复间延迟(整数 ms)
- `count` 重复次数(整数)

完整按键表见 `ir_sequences/KEY_REFERENCE.md`(`KEY_HOME` → 主页键)。

### 使用流程

1. 把 .ini 放进 `ir_sequences/`(或复制 default.ini 修改)
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

契约只有两条:

- 接收 --device `<serial>`(必需,平台自动传入)
- 接收 --params `<json>`(可选,前端可传入参数)

其余完全自由 —— 用 subprocess 调 ADB、用 requests 调 HTTP、写文件、画图都可以。
脚本的 stdout/stderr 实时显示在前端日志面板;退出码 0 = finished,非 0 = failed。

scripts/wifi_reboot_stress.py 是 WiFi 重连压力测试,可以直接用。scripts/ir_runner.py 是红外序列执行脚本(默认无限循环,要点"中断"停止)。

---

## 状态持久化(localStorage)

平台会把以下用户选择存到浏览器 localStorage,F5 刷新后保留:

- 当前选中的设备(`selectedDeviceSerial`)
- 每台设备选的脚本(`deviceScripts`)
- 每台设备选的序列(`deviceSequences`)

服务器数据(devices / scripts / tasks / server info)不持久化,每次都从后端拉。

key 名:`pptp.deviceState.v1`,存在 `localStorage` 里。

---

## 目录结构

```
ProjectorPressureTest/
├── server.py              # 后端单文件 (FastAPI, 870 行)
├── start.bat              # 一键启动(启动成功自动关闭,失败保留信息)
├── server_window.ps1      # PPTP-Server 窗口脚本(实时显示 uvicorn 日志 + 写文件)
├── stop.bat               # 一键停止
├── README.md              # 本文档
├── docs/
│   ├── HANDOFF_PROMPT.md # 完整信息源(API、数据模型、设计决策)
│   ├── SIMPLE-PRD.md     # 初版 v2.0 设计 PRD(已归档)
│   ├── SIMPLE-PLAN.md     # 初版 1.5 天实施计划(已归档)
│   └── SIMPLE-ARCHITECTURE.md  # 初版架构图(已归档)
├── static/
│   ├── index.html         # 单页 HTML
│   ├── app.js             # 前端逻辑 IIFE
│   └── style.css          # 样式
├── scripts/               # 压测脚本
│   ├── ir_runner.py             # 红外序列(自包含:IRRemote 内联,默认无限循环,长按=down+hold+up)
│   └── wifi_reboot_stress.py    # WiFi 重启压力(与 IR 无关,独立脚本)
├── ir_sequences/          # IR 序列 .ini 文件(单一 ini)
│   ├── default.ini              # 默认 14 步序列(全 Short)
│   └── KEY_REFERENCE.md         # 24 个按键的 KEY_NAME 速查(手动维护)
└── logs/                  # 运行时日志
    ├── server.out.log     # uvicorn stdout
    ├── server.err.log     # uvicorn stderr
    └── <task_id>.log      # 每个任务的完整日志
```

---

## API 一览(完整)

| Method | 路径 | 用途 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/api/server/status` | uvicorn PID + uptime + 任务计数 |
| POST | `/api/server/shutdown` | 优雅关停(级联 CTRL_BREAK + 退出) |
| GET | `/api/devices` | ADB 设备列表 |
| GET | `/api/scripts` | scripts/ 下脚本列表 |
| GET | `/api/sequences` | ir_sequences/ 下 .ini 列表 |
| GET | `/api/sequences/{name}` | 读单个 .ini 内容 |
| PUT | `/api/sequences/{name}` | 写整个 .ini 内容 |
| DELETE | `/api/sequences/{name}` | 删 .ini(default 拒绝) |
| POST | `/api/sequences` | 新建 .ini(写最小模板) |
| POST | `/api/run` | 启动任务(同设备并发返回 409) |
| POST | `/api/tasks/force-stop-by-device/{serial}` | SIGKILL 该设备任务 |
| POST | `/api/tasks/force-cleanup` | SIGKILL 全部 + 清空任务 |
| POST | `/api/tasks/cleanup` | 删所有终态任务 + log |
| POST | `/api/adb/reconnect` | adb kill-server + start-server |
| GET | `/api/tasks` | 全部任务状态 |
| GET | `/api/tasks/{id}` | 单任务 |
| GET | `/api/tasks/{id}/log` | 任务完整日志(导出用) |
| DELETE | `/api/tasks/{id}` | 单任务删除 |
| WS | `/ws/logs/{id}` | 实时 stdout 推送(RAF 批处理) |

完整数据模型、状态机、bug 历史见 [docs/HANDOFF_PROMPT.md](docs/HANDOFF_PROMPT.md)。

---

## 故障排查

- 设备列表为空
  确认命令行 `adb devices` 能看到设备。

- 脚本运行报错
  看 logs/<task_id>.log,或前端日志面板。

- 端口 8000 被占用
  跑 stop.bat,或手动 `netstat -ano | findstr :8000` 找进程杀掉。

- 想彻底重置
  跑顶栏"重置"按钮(杀所有任务 + 重启 ADB),或手动删 logs/*.log。

- ir_runner 跑起来报 "Key not found" 或 "not in CODE_NUM_MAP"
  检查 ini 里 `code` 字段是否在 [ir_sequences/KEY_REFERENCE.md](ir_sequences/KEY_REFERENCE.md) 列表内(24 个内置按键)。新增按键需改 `scripts/ir_runner.py` 的 `IRRemote.CODE_NUM_MAP`。

- 长按没生效 / 设备无响应
  默认 IR event 路径是 `/dev/input/event1`(从原 keyevent.txt 推断)。如果你的设备 event 路径不同:
  - CLI 调试: 加 `--device-event-path /dev/input/eventN`
  - 环境变量: `set IR_EVENT_PATH=/dev/input/eventN`(Windows) / `export IR_EVENT_PATH=/dev/input/eventN`(Linux)
  - 直接改 [scripts/ir_runner.py](scripts/ir_runner.py) 顶部 `DEFAULT_EVENT_PATH` 常量

- 想换设备 event 路径但 PPTP 平台不传 CLI 参数
  当前平台透传的 `--device-event-path` 还没接(改中)。临时方案:设环境变量后重启 uvicorn,所有 ir_runner 任务都会读到。

- 想改平台代码后没生效
  浏览器 **Ctrl+F5** 硬刷(平台加了 `?v=2.0.1` + NoCache 中间件,普通 F5 可能拿到缓存)。

---

## 后续(本期不做)

本期已实现:设备列表、脚本列表、IR 序列选择 + 创建、modal 配置、localStorage 持久化、强制停止、重启 ADB、modal 选序列。

本期仍不做:
- 报告生成、历史日志检索
- 设备分组、脚本版本管理、收藏
- 多用户、鉴权
- Docker、跨平台(Linux/macOS)
- 数据库(全内存 + 文件 + localStorage)
- 前端构建工具、JavaScript 框架

详细"显式不做"清单见 [docs/SIMPLE-PRD.md §3](docs/SIMPLE-PRD.md#3-显式不做out-of-scope)。
