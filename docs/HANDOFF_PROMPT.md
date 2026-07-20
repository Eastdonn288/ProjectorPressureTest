# PPTP — Handoff Prompt for the Next Session

> **本文件是 PPTP 项目的当前真源。**其他文档(SIMPLE-PRD.md / SIMPLE-PLAN.md / SIMPLE-ARCHITECTURE.md)已标记为历史归档,只供参考。README.md 是用户面向的精简说明,[FRONTEND_UX.md](FRONTEND_UX.md) 是前端交互设计沉淀(决策 + 改进建议 + 开放问题),本文件包含完整的 API、数据模型、设计决策、bug 历史等内部细节。

> 你是下一任接手 PPTP 项目的 Claude / 任意 LLM。这份文档是项目当前状态的自包含摘要,用来在你没有对话历史的情况下迅速上手,避免基于陈旧/不完整记忆产出不存在或已废弃的特性。
>
> **使用方式**: 把你和用户的实际任务描述放在这份 Prompt 之后,逐节阅读。动手前先 grep / Read 标注的源码行确认。

---

## 0. You are working on

**PPTP — Projector Pressure Test Platform**。一个本地单机的 Web 压测平台,用于在多台 ADB Android 投影仪设备上启动 Python 压测脚本(IR 遥控器模拟、WiFi 重启压力测试、电池测试),实时看每台设备输出的日志。

设计原则:
- **纯本地 / 单机** — 所有数据落本地,无鉴权
- **vanilla JS, 无前端框架** — 单一 IIFE 模块
- **后端最小化** — FastAPI 单文件
- **只一个跨设备数据源:ADB** — WebSocket 推 ADB 进程 stdout

---

## 1. Repo layout (关键文件 + 行号)

```
E:\ProjectorPressureTest\
├── server.py                       (870 行) — FastAPI 后端,所有 API + WS
├── start.bat                       — 启动 launcher (uiautocator 路径已清)
├── stop.bat
├── docs\
│   ├── HANDOFF_PROMPT.md            ← 你正在读
│   ├── SIMPLE-PRD.md
│   ├── SIMPLE-ARCHITECTURE.md
│   └── SIMPLE-PLAN.md               — 早期需求/架构/计划
├── scripts\                         (平台调用的压测脚本)
│   ├── ir_runner.py                 — 红外遥控序列循环(自包含 IRRemote,默认无限循环;长按=down+hold+up)
│   └── wifi_reboot_stress.py        — 重启+ WiFi 重连压力测试(与 IR 无关,独立)
├── ir_sequences\                    (用户可编辑的 .ini 序列文件 + 按键速查)
│   ├── default.ini                  — 默认 14 步序列
│   └── KEY_REFERENCE.md              — 24 个按键的 KEY_NAME 速查(手动维护)
├── static\
│   ├── app.js                        (1249 行) — 前端所有逻辑
│   ├── index.html                    (110 行)  — 4 面板 + 1 模态
│   └── style.css                     (638 行)
└── logs\
    ├── server.log                    — uvicorn 日志
    └── <task_id>.log                 — 每任务 stdout(原始 + replay)
```

> **优先看的文件**: `server.py` + `static/app.js` + `static/index.html` + `static/style.css`。其余均为辅助。

---

## 2. Tech stack (单行)

**Backend**: Python 3.10+, FastAPI, uvicorn, pydantic 2.x, 纯 stdlib subprocess。

**Frontend**: Vanilla JS (ES2017+), 无任何框架/构建工具, 无 npm。
- 4 面板 CSS Grid: `240px 240px 1fr 300px` (设备 / 脚本 / 日志 / 任务)
- 单个 IIFE 包裹的 `state` 全局
- 模态用纯 HTML + CSS, 无 portal

**AD-hoc infra**: `start.bat` 启动 `uvicorn` + 打开浏览器。`stop.bat` 强杀。

---

## 3. How to run

```bash
# 启动(开发)
cd E:\ProjectorPressureTest
E:\Conda_Environments\dev_env\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8000

# 浏览器打开 http://127.0.0.1:8000

# 验证
curl http://127.0.0.1:8000/healthz
# {"ok":true,"version":"2.0","time":"..."}
```

Python 依赖: `pip install fastapi uvicorn pydantic` (就这三个,没别的)。

---

## 4. Backend API surface (server.py)

所有路由(`@app.<verb>`):

| Method | Route | Purpose |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/` | 静态 index.html |
| GET | `/api/server/status` | uvicorn PID + uptime + 任务计数 |
| POST | `/api/server/shutdown` | 优雅关停(级联 CTRL_BREAK + os._exit) |
| GET | `/api/devices` | adb devices -l 解析结果 |
| GET | `/api/scripts` | scripts/*.py 列表 |
| GET | `/api/sequences` | **ir_sequences/*.ini 列表** |
| GET | `/api/sequences/{name}` | 读单个 .ini 内容 |
| PUT | `/api/sequences/{name}` | 写整个 .ini 内容 |
| DELETE | `/api/sequences/{name}` | 删 .ini(default 拒绝) |
| POST | `/api/sequences` | **新建** .ini(模板) |
| POST | `/api/run` | 启动任务子进程(409 冲突 / device-uniqueness) |
| POST | `/api/stop/{task_id}` | 单任务 CTRL_BREAK |
| POST | `/api/tasks/force-stop-by-device/{serial}` | SIGKILL 该设备所有任务 |
| POST | `/api/tasks/force-cleanup` | SIGKILL 全部 + 清空 TASKS |
| POST | `/api/tasks/cleanup` | 删所有终态任务 + log |
| POST | `/api/adb/reconnect` | adb kill-server + start-server |
| GET | `/api/tasks` | 全部任务(无 _proc) |
| GET | `/api/tasks/{task_id}` | 单个 |
| GET | `/api/tasks/{task_id}/log` | 全量日志(供前端 export) |
| DELETE | `/api/tasks/{task_id}` | 单任务删除 |
| WS | `/ws/logs/{task_id}` | 实时 stdout 推送(RAF 批处理) |

**关键不变量**:
- 一台设备同时只能跑一个任务(`api_run` 检查并发 409)
- 临时文件 `_seq_<uuid>.ini` 创建后由 `ir_runner.py` 在 finally 块里删除
- `delete /api/sequences/default` 拒绝(400)

---

## 5. Frontend state shape (static/app.js L14-40)

```js
const state = {
  devices: [],                 // GET /api/devices
  scripts: [],                 // GET /api/scripts
  tasks: [],                   // GET /api/tasks
  deviceScripts: {},           // { [serial]: "ir_runner.py" }  — 设备→脚本
  deviceSequences: {},        // { [serial]: "default" }     — 设备→序列文件名
  selectedDeviceSerial: null, // 当前选中的设备(驱动脚本卡高亮)
  currentDeviceSerial: null,  // 日志面板绑定的设备
  currentTaskId: null,        // 日志面板绑定的任务
  currentWs: null,            // 当前 WebSocket
  backendOnline: false,
  userScrolledUp: false,
  server: null,                // GET /api/server/status
  shuttingDown: false,
  seqContext: null,           // modal context: {target, file, device}
};
```

**localStorage 持久化**(`STORAGE_KEY = "pptp.deviceState.v1"`,L1201):
- 保存:`selectedDeviceSerial` + `deviceScripts` + `deviceSequences`
- **不保存** devices/scripts/tasks(始终从 server 拉)
- 防抖 200ms

---

## 6. Recent design decisions (DO NOT REVERT)

按时间倒序,这些是用户已经"批准"的设计选择:

1. **Per-device sequence binding (each device independent)**
   - `state.deviceSequences: { [serial]: filename }` — A 选 default, B 选 aging 互不影响
   - 切设备时脚本卡实时刷新
   - 选中设备的 ir_runner 卡显示该设备的序列名
   - 禁止全局 "pinned script" 这种共享模式

2. **Sequence files instead of in-memory editor**
   - 用户改 ini 文件直接编辑
   - modal 只做选择 / 创建空模板,**不做 step 编辑**
   - 之前做过 step-by-step 编辑(代码/kind/delay/count 行),已废弃

3. **Run button on script card (not device card)**
   - ir_runner 脚本卡右侧"▶ 跑"按钮
   - 设备卡只显示状态(chip/运行中/离线)
   - 按钮在脚本卡因为 runnability 取决于 "设备 + 脚本 + 序列" 三态

4. **Script dropdown gating**
   - 设备未选中 → dropdown 禁用 + 提示"先点击选中"
   - 跑任务中 → 禁用(脚本锁定)
   - 离线 → 禁用
   - 选中 + 已选脚本 + 有序列 → 启用

5. **Visual badges**
   - 设备卡 active: 蓝色边框 + 右上 "✓ 已选中" 蓝色 pill(角标,完全在卡内)
   - 设备卡 active+running: 右上 "⚡ 运行中" 绿色 pill
   - 脚本卡 active: 置顶 + 边框 + 左上 "▸ 当前选中" 蓝色 pill(完全在卡内)
   - "跑 X" 旧 badge 已删除(避免重复)

6. **localStorage 刷新保留状态**
   - 已选设备 / 已选脚本 / 已选序列跨刷新保留
   - 服务器数据(任务/devices)不持久化,总是从 server 拉

7. **RAF 批量推送日志(避免"刷")**
   - 客户端批量 buffer,RAF 一次性 textContent 更新
   - DOM 1000 行上限(类似 VSCode 终端 scrollback)
   - 10000 行上限的服务端 replay(防止异常巨大 log)

8. **modal 标题动态化**
   - 文件模式:`${seqFilename}.ini`
   - 设备模式:`设备 ${serial} 的序列`

9. **Cache busting + NoCache middleware**
   - `index.html` script/css link 带 `?v=2.0.1`
   - server 加 `NoCacheMiddleware`,静态文件 `Cache-Control: no-store`

> 完整前端交互设计沉淀(为什么这样做、踩过什么坑、还有什么没定)见 [FRONTEND_UX.md](FRONTEND_UX.md)。

---

## 7. What does NOT exist (防幻觉清单)

**绝对不要** 假定这些存在 — 如果用户提到这些,先 grep + Read 再决定:

- ❌ 数据库(SQLite/PostgreSQL) — 全是内存 + 文件
- ❌ 用户认证 / 登录
- ❌ 多用户/多租户
- ❌ WebSocket 自动重连(除 ir_runner 任务外的纯客户端流;log WS 有手动重连)
- ❌ 任务调度(只手动触发,无 cron)
- ❌ 报告/统计/历史趋势分析
- ❌ 邮件/通知
- ❌ 远程控制(全本地)
- ❌ 任何 JavaScript 框架(React/Vue/...)或构建工具
- ❌ 任何 npm 依赖
- ❌ Tailwind / 任何 UI 框架
- ❌ Docker / 容器化
- ❌ HTTPS(纯 HTTP)
- ❌ 任何 /api/ir/keys 端点(原 ir_runner keys 已被移除)
- ❌ modal 内的 step 编辑(code/kind/delay/count 行)— 之前做过, 已废弃
- ❌ 任何 "在设备上运行" 多设备同时启动 UI 流程
- ❌ "上次已保存的序列"自动 fallback 选择

---

## 8. If you're given a task — SOP

1. **不要直接写代码**。先:
   - Read 标注的源文件 + grep 相关函数名
   - 如果涉及 IR 序列,看 `scripts/ir_runner.py`(自包含 IRRemote,无外部依赖)+ `ir_sequences/default.ini`
2. **列影响面**:
   - 后端 → server.py 哪个 route / 函数
   - 前端 → app.js 哪些函数 / state 哪些字段
   - 状态持久化 → 是否需要更新 localStorage
3. **不破坏的设计决策** (见 §6)。如果用户的请求跟它们冲突,先提出矛盾再写代码。
4. **写代码 + 验证**:
   - 后端改完 → `taskkill /F /PID <uvicorn-pid>` 强杀,再 `python -m uvicorn server:app --host 127.0.0.1 --port 8000` 重启
   - 前端改完 → 浏览器 **Ctrl+F5** 硬刷(必须 F5 不够,因为有 ?v=)
   - 端到端 curl 测试:`curl http://127.0.0.1:8000/api/...`
5. **完成后**:
   - 报改了哪些文件 + 行号
   - 列验证步骤结果
   - 如有 UI 改动,告诉用户"硬刷 Ctrl+F5"

---

## 9. Verify before done

最低验证清单(任何功能改动):

- [ ] 后端:`/healthz` 200; 修改的 route 用 curl 测过
- [ ] 前端:浏览器 Ctrl+F5 后,无 JS 报错(F12 console)
- [ ] state 持久化(如有):改 → F5 → 应保持
- [ ] 没有"上一版的旧实现"残留在文件中(grep 一下"// TODO"、"// 废弃"等注释)
- [ ] 没有改到 §7 列的"不存在的特性"

---

## 10. Quick reference — 关键函数名 + 行号

**Backend (server.py)**:
- `_list_adb_devices()` L198 — `adb devices -l` 解析
- `_list_scripts()` L242 — 扫 `scripts/*.py`
- `_stream_logs(task_id)` L255 — 主异步协程,读 stdout + 推 WS
- `_reap_orphan_scripts()` L98 — startup hook,清 python.exe 孤儿 + 旧 `_seq_*.ini`
- `NoCacheMiddleware` L60+ — `Cache-Control: no-store` for static
- `api_run()` L521 — 含 device-uniqueness 409 校验
- `api_force_cleanup()` L650 — SIGKILL all + 清空 TASKS
- `api_create_sequence()` L491 — 写模板
- `api_ir_keys()` — **已删除**(ir_runner keys 不再需要)

**Frontend (static/app.js)**:
- `state` L14-40
- `STORAGE_KEY` L1201
- `loadPersistedState()` / `savePersistedState()` L1203 / L1220
- `renderScripts()` L122 — 4 状态(active/disabled), ir_runner 卡有 Run 按钮
- `renderDevices()` L207 — 设备卡 active 角标 / 脚本 chip
- `openSeqModal()` L1086 — 纯 picker 模态
- `onRunClick()` L670 — 无参,读 state.selectedDeviceSerial
- `onSelectDevice()` L638 — 同步 device + log
- `openWs()` / `scheduleReconnect()` — 日志 WS 重连

**Frontend (static/index.html)**:
- 4 个 `.panel`(`#panel-devices/scripts/logs/tasks`)
- `#modal-seq` (line 89) sequence picker
- 顶部 `.topbar` 含 "重置" 和 "关机" 按钮 + 服务信息
- `<script src="/static/app.js?v=2.0.1">` — 版本号防缓存

**CSS 章节** (static/style.css):
- 顶部 25-105 行:基础元素(panel/btn/list)
- L107-203: `.script-card` 4 状态
- L205-285: `.device-card` 4 状态
- L288-385: `.script-select` 自定义箭头
- L388-432: `.task-card` + `×` 删除按钮
- L434-455: `.log-info` 条
- L457-466: 状态徽章颜色
- L468-476: `.console` 字体 + 自适应
- L478-636: `.modal` + `.seq-list` + `.seq-target`

---

## 11. Common user requests 分类

按用户曾经提过的问题模式(下次遇到类似的可快速定位):

- **"X 不显示/位置不对"** → CSS 章节 → 检查 overflow/z-index/position
- **"切换设备 Y 不更新"** → `state.selectedDeviceSerial` + `renderScripts()` + `onSelectDevice()`
- **"刷新页面 Z 丢了"** → localStorage 持久化(检查 `STORAGE_KEY` 和 save/load 点)
- **"ir_runner 行为不对"** → `scripts/ir_runner.py` 的 `argparse` 和 infinite loop 逻辑
- **"新增设备类型/操作类型"** → 后端 `api_run` 校验 / 前端 dropdown 配置
- **"样式丑了"** → CSS 章节找对应 class(`.device-card / .script-card / .btn / .modal`)
- **"加个新功能"** → 找最近的相似功能(如 modal、新端点、按钮)作模板

---

## 12. Notes on user's style preferences

- **极简主义** — 用户反复删多余 UI 元素(如"见 ir_runner 卡片"提示、跑 X 重复 badge)
- **不预设复杂架构** — 删过 plugin 系统、Vue 框架、SQLite 集成等
- **现代简洁的样式** — 喜欢渐变 + 圆角 + 微动画 + 视觉层次
- **喜欢 Ctrl+F5 测试** — 接受"硬刷"作为正常流程
- **不需要"上一版兼容"** — 删旧代码毫不犹豫(常配合 `git log` 找回)

---

## 13. Critical anti-patterns to avoid

- ❌ **不要** 修改 `state` 字段名(下游所有 `state.X` 引用都会断)
- ❌ **不要** 添加 console.log 调试(用户看不到,但有 `console.warn` 用于关键提示)
- ❌ **不要** 在 modal 里加 step 编辑 UI(已废弃)
- ❌ **不要** 在前端用 ES modules / 框架(保持 vanilla JS 单文件)
- ❌ **不要** 用 SQLite 替代内存(全平台靠内存 + 文件 + localStorage)
- ❌ **不要** 创建 README 大改 / 文档大改除非用户要求
- ❌ **不要** 假设 Python 虚拟环境(用 system conda env `D:\Conda_Environments\dev_env\python.exe`)
- ❌ **不要** 在 dev 时关掉 `STORAGE_KEY` 持久化

---

## 14. Useful one-liners

```bash
# 查 API 路由
grep -nE "^@app\.(get|post|put|delete|websocket)" E:\ProjectorPressureTest\server.py

# 查前端 state 字段被哪里用
grep -nE "state\." E:\ProjectorPressureTest\static\app.js | head -50

# 查某个 CSS class
grep -nE "^\.device-card " E:\ProjectorPressureTest\static\style.css

# 重启服务(开发用)
netstat -ano | grep ":8000.*LISTENING" | awk '{print $NF}' | head -1 | while read pid; do taskkill /F /PID $pid; done
D:\Conda_Environments\dev_env\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8000

# 端到端测试
curl -X POST http://127.0.0.1:8000/api/sequences -H "Content-Type: application/json" -d '{"name":"test_seq"}'
curl http://127.0.0.1:8000/api/sequences
```

---

## 15. 怎么用这份文档

新会话开场建议结构:

```
[粘贴本文件内容,然后加你的任务]
---

## 你的当前任务

<用户给的具体需求>

## 期望产出

<具体的预期 / 验收标准>
```

下一任会:
1. Read §0-2 知道是什么
2. Read §6 设计决策(避免回退)
3. Read §7 "不存在"清单(避免幻觉)
4. Read §8 SOP
5. 然后开始 grep 源码,写代码

如果下一任在 5 分钟内没动手写代码,说明它没读完这份文档。

---

**最后更新**: 2026-07-18(当前会话末尾)
**会话状态**: 完整运行中,所有改动已通过端到端验证
**未完成需求**: 无(等用户给新任务)
**已知小问题**:
- WS 在 server restart 时 log console 会"卡"在最后一行(没手动 rejoin)
- 任务被删除时 log 文件可能短暂残留(orphan cleanup 只清 startup 时的)
- IR event 设备路径(默认 `/dev/input/event1`)是 hardcode 的,如果换设备要改 `ir_runner.py` 顶部 `DEFAULT_EVENT_PATH`(详见 [README.md 故障排查](../README.md))
