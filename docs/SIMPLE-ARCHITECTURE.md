# PPTP 极简版 架构

> ⚠️ **本文件为 v2.0 极简版初版架构图,已不再完全代表当前实现。**
> 2026-07-18 起,信息以 [HANDOFF_PROMPT.md](HANDOFF_PROMPT.md) 为准(列出了真实 API、数据模型、设计决策)。
> 保留本文档作为初始架构决策的历史参考。

> 版本:v2.0(精简版) | 配套 PRD:[SIMPLE-PRD.md](SIMPLE-PRD.md)

---

## 1. 总体架构

```
┌─────────────────────────────────────────────────────────────┐
│  Browser (单 HTML + 原生 JS, 无构建步骤)                       │
│  ┌────────────┬────────────┬─────────────────┬────────────┐  │
│  │ DeviceList │ ScriptList │  LogConsole     │ TaskPanel  │  │
│  │ 每 3s 拉取 │ 启动时拉取  │  WS 逐行推送     │ 每 2s 拉取 │  │
│  └────────────┴────────────┴─────────────────┴────────────┘  │
└────────────────▲────────────────────▲────────────────────────┘
                 │ REST               │ WebSocket
                 │ (GET/POST)         │ (text frames)
┌────────────────▼────────────────────▼────────────────────────┐
│  server.py (FastAPI 单文件)                                    │
│                                                              │
│   in-memory: tasks = { task_id: TaskState }                  │
│                                                              │
│   /api/devices        → adb devices -l (subprocess)           │
│   /api/scripts        → listdir(scripts/)                     │
│   /api/run            → Popen(python script.py --device X)   │
│   /api/stop/{id}      → proc.terminate()                      │
│   /api/tasks          → return tasks dict (no proc handle)    │
│   /ws/logs/{id}       → tail stdout, push line-by-line        │
│                                                              │
│   mounts /static for index.html / app.js / style.css          │
└────────────────▲─────────────────────────────────────────────┘
                 │ subprocess.Popen(...)
                 │
┌────────────────▼─────────────────────────────────────────────┐
│  scripts/*.py  (用户的脚本, 零修改或加 --device 参数即可)       │
└──────────────────────────────────────────────────────────────┘
```

---

## 2. 关键数据流

### 2.1 启动任务

```
[前端]  POST /api/run  {device: "abc", script: "ir_remote.py"}
   │
   ▼
[后端]  task_id = uuid4()
        proc = Popen(["python", "scripts/ir_remote.py",
                      "--device", "abc"],
                     stdout=PIPE, stderr=STDOUT, text=True, bufsize=1)
        tasks[task_id] = {device, script, status: "running",
                          started_at: now(), proc: proc, log_buf: []}
        asyncio.create_task(_stream_logs(task_id, proc))
   │
   ▼
[前端]  收到 {task_id}; TaskPanel 立即显示 running
```

### 2.2 日志推送

```
[子进程 stdout]  ──逐行──▶  [_stream_logs 协程]
                                  │
                                  ├─▶ 写入 logs/<task_id>.log(可选)
                                  ├─▶ 追加到 tasks[task_id].log_buf(给历史查询用)
                                  └─▶ 通过 ws 给前端推一行
                                            │
                                            ▼
                                  [前端]  appendChild(<div>)
```

### 2.3 停止任务

```
[前端]  POST /api/stop/abc-123
   │
   ▼
[后端]  proc.terminate()  → SIGTERM
        tasks[task_id].status = "interrupting"
   │
   ▼
[_stream_logs 协程]  读到 proc.stdout EOF
        tasks[task_id].status = "interrupted"
        tasks[task_id].ended_at = now()
        关闭 ws
```

---

## 3. 目录结构

```
ProjectorPressureTest/
├── server.py              # 后端单文件 (~250 行)
├── static/
│   ├── index.html         # 单页 HTML (~80 行)
│   ├── app.js             # 原生 JS (~200 行, 无依赖)
│   └── style.css          # 极简样式 (~50 行)
├── scripts/
│   ├── ir_remote.py       # 现有红外脚本(只需加 --device 参数)
│   └── (后续其他脚本)
├── data/
│   └── tasks.json         # 任务状态持久化(可选,关闭重启可恢复)
├── logs/
│   └── <task_id>.log      # 每个任务的完整 stdout
├── start.bat              # 一键启动 (~10 行)
└── README.md
```

**总代码量预估**:后端 250 行 + 前端 350 行 = **约 600 行**,1~2 天搞定。

---

## 4. 关键技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 后端框架 | **FastAPI 单文件** | 异步 + WebSocket 一行代码,生态最熟 |
| 前端方案 | **原生 JS + 单 HTML** | 4 个面板无路由需求,框架是负担 |
| 状态存储 | **内存 dict + 可选 JSON 落盘** | 单用户场景无并发写,数据库杀鸡用牛刀 |
| 任务隔离 | **`subprocess.Popen` 每任务一进程** | 用户要求多设备并发,进程级隔离最稳 |
| 日志传输 | **WebSocket 逐行推送** | 用户要求"实时显示",HTTP 轮询延迟太高 |
| 日志持久化 | **按 task_id 写文件** | 文件比数据库简单,grep 就能查 |
| 脚本契约 | **`--device` 命令行参数** | 脚本保持 CLI 性质,独立可跑,平台零耦合 |
| 部署方式 | **start.bat + uvicorn** | 用户本地 Windows,无需 Docker |

---

## 5. 不引入的依赖

| 不引入 | 替代方案 |
|---|---|
| Vue / React / 任何前端框架 | 原生 JS |
| Vite / Webpack / 任何构建工具 | 静态文件直出 |
| SQLAlchemy / Alembic | 内存 dict + JSON |
| Pinia / Redux | 模块级变量 |
| Pydantic 复杂 schema | 仅作请求体解析 |
| Element Plus / Ant Design | 手写极简 CSS |
| 插件加载器 | 约定 `scripts/*.py` |
| Jinja2 | 不需要 HTML 模板 |
| Celery | asyncio + subprocess |
| pytest | 手动验证(1~2 天项目没必要) |