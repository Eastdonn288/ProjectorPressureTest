# PPTP 极简版 项目计划

> ⚠️ **本文件为 v2.0 极简版初版实施计划记录,已不再完全代表当前实现。**
> 2026-07-18 起,信息以 [HANDOFF_PROMPT.md](HANDOFF_PROMPT.md) 为准(列出了真实 API、数据模型、设计决策)。
> 保留本文档作为开发历史参考。

> 版本:v2.0(精简版) | 配套 [SIMPLE-PRD.md](SIMPLE-PRD.md) / [SIMPLE-ARCHITECTURE.md](SIMPLE-ARCHITECTURE.md)
>
> **相比 v1.0**:从 12 周 / 7 个阶段压缩到 **1~2 天 / 3 个步骤**。

---

## 总体时间表

| 步骤 | 内容 | 估时 |
|---|---|---|
| Step 1 | 后端骨架 + 设备列表 + 脚本列表 + 运行接口 + WebSocket 日志 | 0.5 天 |
| Step 2 | 前端单页 + 4 个面板 + WebSocket 客户端 | 0.5 天 |
| Step 3 | 端到端联调 + 启动脚本 + 用现有 IR 脚本跑通 | 0.5 天 |
| **合计** | | **~1.5 天** |

---

## Step 1 — 后端骨架(0.5 天)

### 1.1 任务

| ID | 任务 | 产出 |
|---|---|---|
| T1.1 | 初始化 `server.py`,FastAPI + uvicorn 启动,`/healthz` 返回 200 | 可运行的空后端 |
| T1.2 | `GET /api/devices` —— 调用 `adb devices -l`,解析输出为 `[{serial, status, model}]` | 浏览器/curl 可见设备 |
| T1.3 | `GET /api/scripts` —— 扫描 `scripts/*.py`,返回 `[{name, path}]` | 可见脚本列表 |
| T1.4 | `POST /api/run` —— `subprocess.Popen(["python", script, "--device", serial])`,分配 task_id,存内存状态 | 任务能起 |
| T1.5 | `POST /api/stop/{id}` —— `proc.terminate()`,状态改 interrupting | 能中断 |
| T1.6 | `GET /api/tasks` —— 返回所有任务(去掉 proc 句柄) | 前端可拉 |
| T1.7 | `WS /ws/logs/{id}` —— 协程逐行读 stdout,推 ws,子进程结束自动关 ws | 日志实时推送 |
| T1.8 | 挂载 `/static`,提供 index.html | 前端可访问 |

### 1.2 数据结构(内存)

```python
tasks: dict[str, dict] = {
    "uuid-xxx": {
        "device": "abc123",
        "script": "ir_remote.py",
        "status": "running",  # idle / running / interrupting / finished / failed / interrupted
        "started_at": "2026-07-13T10:00:00",
        "ended_at": None,
        "exit_code": None,
        "log_file": "logs/uuid-xxx.log",
        "_proc": Popen(...)  # 不序列化出去
    }
}
```

### 1.3 输出/验证

- [ ] `curl http://127.0.0.1:8000/healthz` → 200
- [ ] `curl http://127.0.0.1:8000/api/devices` → 设备 JSON
- [ ] `curl http://127.0.0.1:8000/api/scripts` → 脚本 JSON
- [ ] 手测:跑一个 `print("hello")` 的测试脚本,WebSocket 能收到

---

## Step 2 — 前端单页(0.5 天)

### 2.1 任务

| ID | 任务 | 产出 |
|---|---|---|
| T2.1 | 单 HTML,4 区域布局(CSS Grid 一行搞定) | `static/index.html` |
| T2.2 | `DeviceList` —— `setInterval` 3s 拉 `/api/devices`,渲染列表,每行一个"运行"按钮(下拉选脚本) | 设备能见能操作 |
| T2.3 | `ScriptList` —— 启动时拉一次,显示脚本名 | 脚本能见 |
| T2.4 | `LogConsole` —— 选中某设备的某任务后,开 WebSocket,逐行 append,提供"清空""停止"按钮 | 实时日志 |
| T2.5 | `TaskPanel` —— 每 2s 拉 `/api/tasks`,显示每台设备当前任务的 `status / started_at / ended_at` | 状态可见 |
| T2.6 | 切换设备 → 关闭旧 ws → 开新 ws(用 task_id 而非 device 唯一对应) | 视图切换流畅 |

### 2.2 页面布局(伪 HTML)

```
┌────────────────────────────────────────────────────────────┐
│  PPTP - Projector Pressure Test                          │
├──────────────────┬─────────────────────────┬──────────────┤
│ 设备列表         │  日志控制台               │ 任务状态      │
│ ┌──────────────┐ │ ┌─────────────────────┐ │ ┌──────────┐│
│ │abc123 [运行▼]│ │ │ [abc123 / ir_remote] │ │ │abc123    ││
│ │def456 [运行▼]│ │ │                     │ │ │ running  ││
│ │...           │ │ │ 10:00:01 start      │ │ │ 10:00:01 ││
│ └──────────────┘ │ │ 10:00:02 step 1     │ │ └──────────┘│
│ 脚本列表         │ │ 10:00:03 step 2     │ │ def456    │
│ ┌──────────────┐ │ │ ...                 │ │ │ idle     │
│ │ir_remote.py  │ │ │                     │ │ └──────────┘│
│ │keyevent.py   │ │ └─────────────────────┘ │              │
│ └──────────────┘ │ [清空] [停止]            │              │
└──────────────────┴─────────────────────────┴──────────────┘
```

### 2.3 输出/验证

- [ ] 打开 `http://127.0.0.1:8000`,4 区域可见
- [ ] 设备列表 3s 内自动刷新
- [ ] 选设备 → 选脚本 → 点"运行" → 任务面板出现该设备状态为 running
- [ ] 日志面板 1s 内开始滚日志
- [ ] 点"停止" → 状态变 interrupted,日志停

---

## Step 3 — 端到端 + 启动脚本(0.5 天)

### 3.1 任务

| ID | 任务 | 产出 |
|---|---|---|
| T3.1 | 改造 `scripts/ir_remote.py`,加 `--device` 参数(原脚本逻辑保持) | 红外脚本可在平台跑 |
| T3.2 | `start.bat` —— 启动 uvicorn,等端口就绪,自动打开浏览器 | 一键启动 |
| T3.3 | README 写"5 分钟上手" | 新人可上手 |
| T3.4 | 端到端验收:连一台投影 → 选红外脚本 → 跑 → 看到按键序列日志 → 切换设备 → 看另一台日志 | 满足 PRD 全部 10 条 DoD |

### 3.2 验收清单(对应 PRD DoD)

- [ ] 双击 start.bat 浏览器自动打开
- [ ] 设备列表自动刷新 + 手动刷新
- [ ] 脚本列表显示 `scripts/*.py`
- [ ] 运行按钮 → 状态 running + 开始时间
- [ ] 日志 500ms 内出现
- [ ] 切换设备查看另一台日志
- [ ] 中断按钮 → 状态 interrupted + 子进程被 kill
- [ ] 自然结束 → 状态 finished/failed + 结束时间
- [ ] 单任务故障不影响其他
- [ ] 关闭重启后内存重建(可选)

---

## 后续(本期不做,记录在此供回顾)

- 报告生成、任务历史检索、设备分组
- 脚本元数据、参数 schema、收藏/预设/编排
- 多用户/鉴权
- Docker / 跨平台