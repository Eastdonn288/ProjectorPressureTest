# PPTP — 前端交互设计沉淀

> **本文档聚焦"前端怎么用、为什么这样设计"。**它跟 [HANDOFF_PROMPT.md](HANDOFF_PROMPT.md) 互补:HANDOFF 写的是"代码长啥样 + 改了啥",本文写的是"为什么这样做、踩过什么坑、还有什么没定"。
>
> 维护人:Claude 协助用户在 2026-07 系列会话中沉淀,后续若有新决策请追加到对应小节,不要覆盖。

---

## §1. 已敲定的交互决策(用户已确认)

每条决策格式:**做什么 / 为什么 / 涉及代码位置**。

### 1.1 Per-device sequence binding(每设备独立)

- **做什么**: `state.deviceSequences: { [serial]: filename }`,设备 A 选 default、设备 B 选 aging,互不影响。
- **为什么**: 真实压测场景下多台投影仪跑不同序列(老化 vs 工厂),不允许全局共享。
- **涉及**: [app.js:30-32](static/app.js#L30-L32) state 字段;[onSelectDevice L696-731](static/app.js#L696-L731)。

### 1.2 序列文件 > 内存编辑

- **做什么**: 用户改 ini 直接编辑文件;modal 只做 picker + 创建空模板,不做 step 编辑。
- **为什么**: 编辑器复杂度远高于实际收益(用户已会写 ini)。第一版做过 step 编辑器,被废弃。
- **涉及**: [openSeqModal L1086](static/app.js#L1086);`/api/sequences/{name}` PUT 端点直写文件。

### 1.3 Run 按钮在脚本卡,不在设备卡

- **做什么**: ir_runner 脚本卡右侧 "▶ 跑" 按钮;设备卡只显示状态。
- **为什么**: runnability 取决于「设备 + 脚本 + 序列」三态。把按钮放脚本卡可以让所有 gating 状态集中显示在脚本卡(active / disabled / 已选序列)。
- **涉及**: [renderScripts L122](static/app.js#L122);[onRunClick L664](static/app.js#L664)。

### 1.4 设备卡 dropdown gating

- **做什么**: 设备未选中 / 跑任务中 / 离线时,dropdown 禁用 + tooltip 提示"先点击选中"。
- **为什么**: 防止用户给一台"还在跑"的设备改脚本(脚本锁定是后端 409 的语义前端化)。
- **涉及**: [renderDevices L289-307](static/app.js#L289-L307)。

### 1.5 视觉徽章(active 角标,完全在卡内)

- **做什么**: 用 chip 角标表示状态,**全部在卡片内部不溢出**:
  - 设备卡 active: 蓝色边框 + 右上 "✓ 已选中" 蓝色 pill
  - 设备卡 active+running: 右上 "⚡ 运行中" 绿色 pill(替换 ✓)
  - 脚本卡 active: 置顶 + 边框 + 左上 "▸ 当前选中" 蓝色 pill
- **为什么**: 用户反复要求"角标不要溢出卡片";删除过 "跑 X" 重复 badge(避免冗余信息)。
- **涉及**: [renderDevices L286-340](static/app.js#L286-L340);[renderScripts L122-204](static/app.js#L122-L204)。

### 1.6 localStorage 持久化

- **做什么**: key `pptp.deviceState.v1`,**只保存** `selectedDeviceSerial` + `deviceSequences`(防抖 200ms)。
- **不保存**: `deviceScripts`(会话状态,2026-07-20 反转 — 详见 §2.0b)、devices / scripts / tasks(总从 server 拉)。
- **为什么**: 配置(哪个设备、跑哪个序列)跨刷新保留;会话状态(现在想跑什么)不持久化 — 否则重启/刷新后卡片显示上次的脚本选择,语义不对。
- **副作用权衡**: 同一个 localStorage 无法区分 Ctrl+F5(刷新)和 server restart,所以"重启干净"等价于"刷新也清"。**用户接受这个权衡**(2026-07-20):运行状态下拉框本来就置灰、改不动;非运行状态下拉框"无脑清空"也无害,反正随时能改。不加 "重置按钮" 区分这两类场景(避免过度设计,见 §3.3)。
- **涉及**: [STORAGE_KEY L1201](static/app.js#L1201);[loadPersistedState / savePersistedState](static/app.js#L1216-L1252)。

### 1.7 RAF 批量推送日志

- **做什么**: log buffer 累积,requestAnimationFrame 一次性 textContent 更新;DOM 上限 1000 行;服务端 replay 上限 10000 行。
- **为什么**: 防止"刷"感;replay 2000+ 行时单行 textContent += 是 O(n²),会明显卡顿。
- **涉及**: [appendLogLine L473](static/app.js#L473);[flushLogBuffer L480](static/app.js#L480);[LOG_DOM_CAP L471](static/app.js#L471)。

### 1.8 Modal 标题动态化

- **做什么**: 同一 modal 根据 context 切标题:
  - 文件模式: `${seqFilename}.ini`(从顶部序列卡点)
  - 设备模式: `设备 ${serial} 的序列`(从设备卡点)
- **为什么**: 一个 modal 复用两种入口,避免重复 UI。
- **涉及**: [openSeqModal L1086](static/app.js#L1086);`state.seqContext` 字段。

### 1.9 缓存破坏 + NoCache

- **做什么**: index.html script/css link 带 `?v=2.0.1`;FastAPI `NoCacheMiddleware` 给静态文件 `Cache-Control: no-store`。
- **为什么**: 平台经常小改前端,F5 不够硬;开发流程接受 "Ctrl+F5" 作为正常操作。
- **涉及**: server.py `NoCacheMiddleware`;[index.html script src](static/index.html) query string。

### 1.10 WS 重连指数退避

- **做什么**: WS onclose 后 1s/2s/3s/4s/5s 重试,5 次后放弃并提示用户刷新页面。
- **为什么**: server 重启 / 临时网络抖动,自动恢复比弹错误更友好;但无限重试会浪费资源。
- **涉及**: [scheduleReconnect L634](static/app.js#L634);`state._wsReconnectAttempts` 计数器。

### 1.11 WS taskId 过滤(切设备时不污染 console)

- **做什么**: WS onmessage 收到帧时,如果 `state.currentTaskId !== ws._taskId` 直接丢弃。
- **为什么**: 用户切到无任务的设备时,旧设备的 WS 还在连;不滤的话旧日志会"复活"覆盖空 console。
- **涉及**: [openWs onmessage L600-624](static/app.js#L600-L624) filter 行。

### 1.12 onSelectDevice 无任务时关 WS

- **做什么**: 切换到的设备没有任何任务时,显式关闭旧 WS + 清 currentTaskId。
- **为什么**: 双保险 — 即使 onmessage filter 漏掉,关掉连接就根本不会推帧;同时也释放 server 端 fd。
- **涉及**: [onSelectDevice L717-728](static/app.js#L717-L728)。

---

## §2. 我提的改进建议 + 已采纳(从对话累积)

按时间排序,标记 ✅ 已采纳 / ⏸️ 待定 / 🔄 决策反转。

### 2.0 🔄 设备卡 "运行中" pill 去重 + 配色区分(2026-07-20)

- **背景**: 设备卡右下"运行中"和右上 ⚡ 重复;用户提出删右上 + 左下换色。
- **改动**:
  - [style.css L253-257 删掉](static/style.css#L253-L257)`.device-card.active.device-card-temp-offline::after` 的"⚡ 运行中" pseudo-element
  - [style.css L383](static/style.css#L383) `.badge-running` 配色改为橙色 `--warn` + italic + 加粗,与蓝色"✓ 已选中"做视觉区分
- **理由**: 左下 badge 作为 run 状态的唯一指示器;橙色 + 斜体让"在跑"和"已选中"一眼能区分。
- **决策反转**: 之前的"active + temp-offline 右上 pill"在视觉上跟已选中冲突。

### 2.0b 🔄 localStorage deviceScripts 决策反转(2026-07-20)

- **背景**: 用户反馈"关机后再开,设备卡脚本选择不是默认状态,而是上次选择的"。
- **原决策** (§1.6): 刷新保留 `deviceScripts`、跨刷新不丢配置。
- **新决策** (§1.6 修订): **`deviceScripts` 不再持久化**;`selectedDeviceSerial` + `deviceSequences` 仍保留。
- **改动**: [app.js loadPersistedState](static/app.js#L1216-L1235) 跳过 deviceScripts;[savePersistedState](static/app.js#L1237-L1252) 也不写。
- **语义**:
  - deviceScripts = "会话状态"(现在想跑什么)→ 不持久化
  - deviceSequences = "配置"(哪个设备跑哪个序列)→ 持久化
  - selectedDeviceSerial = "用户偏好"(看着哪台)→ 持久化
- **副作用**: Ctrl+F5 也会清空脚本选择(同一个 localStorage,无法区分刷新 vs 重启)。**用户接受这个副作用,不加"重置"按钮解**(2026-07-20 同日用户反馈:"运行状态置灰了,非运行状态改脚本也无所谓")。原因:非运行状态下拉框无副作用,不需要精细的"重置 vs 不重置"语义。

### 2.1 ✅ 临时离线设备的合成卡片

- **背景**: wifi_reboot 压测中设备会临时掉线,但任务还在跑;用户看不到设备卡。
- **建议**: 后端用 adb devices 解析 + 任务列表合成的"临时离线"卡片,标注"(临时离线 · 任务在跑)"。
- **采纳**: [renderDevices L249-269](static/app.js#L249-L269)。
- **副作用**: dropdown 仍可用(交互锁在 running),`btn-hard-stop` 只在这种状态下启用。

### 2.2 ✅ "硬停"按钮(temp-offline 专属)

- **背景**: 设备掉线时子进程可能卡死,`stop/{task_id}` 发 CTRL_BREAK 没用。
- **建议**: 加 "硬停" 按钮,只在当前任务在 temp-offline 状态时启用,调 `/api/tasks/force-stop-by-device/{serial}`(SIGKILL)。
- **采纳**: [renderLogInfo L457-459](static/app.js#L457-L459);[index.html btn-hard-stop](static/index.html)。

### 2.3 ✅ 日志 replay 截断提示

- **背景**: 服务端 10000 行上限截断后,前端只显示最近 N 行,用户不知道。
- **建议**: 收到 `replay_meta` 消息时,在 console 顶部插一行 "── 共 X 行 · 本次显示最近 Y 行 · 完整日志见 logs/... ──"。
- **采纳**: [openWs replay_meta handler L614-622](static/app.js#L614-L622)。

### 2.4 ✅ 删除任务 = 删除日志文件

- **背景**: 任务结束后的 log 文件残留在 `logs/` 目录,长期累积。
- **建议**: `DELETE /api/tasks/{task_id}` 后端同时删 log 文件。
- **采纳**: server.py `api_delete_task()` 已加。

### 2.5 ✅ "未选脚本" muted 提示

- **背景**: 设备没选脚本时,设备卡 actions 区域空着,用户疑惑。
- **建议**: 显示 `<span class="muted">未选脚本</span>` 灰色 11px 提示。
- **采纳**: [renderDevices L317-318](static/app.js#L317-L318)。

### 2.6 ✅ 任务卡片 active 高亮

- **背景**: 当前看的任务在 task panel 里没视觉区分。
- **建议**: `state.currentTaskId === t.task_id` 时加 `.active` class。
- **采纳**: [renderTasks L387](static/app.js#L387)。

### 2.7 ✅ 设备卡 chip 显示已选脚本

- **背景**: 用户选了脚本后,设备卡 actions 区显示 "未选脚本" 或 "运行中",但选的是什么脚本不直观。
- **建议**: row2 显示 `device-script-chip`(已选脚本名,无 .py 后缀)。
- **采纳**: [renderDevices L337](static/app.js#L337)。

### 2.8 ✅ 切设备无任务时 console 显示 "暂无任务"

- **背景**: 切到无任务设备后,console 残留上一设备的最后几行(被 RAF flush 延后)。
- **建议**: clearConsole 之后 setConsoleTarget(null, serial) → renderLogInfo 显示 "查看设备 X 的日志(该设备暂无任务)"。
- **采纳**: [renderLogInfo L431-435](static/app.js#L431-L435)。

### 2.9 ✅ 文档统一:default.ini 5/6 字段 bug 修复

- **背景**: ir_runner.py parser 是 5 字段,但 default.ini 旧版是 6 字段(带 name),跑起来报 "Key not found"。
- **建议**: 删 default.ini name 字段 + 同步 README 备注 + 改 default.ini 头部注释。
- **采纳**: 2026-07-20 完成,default.ini 现在纯 5 字段。

### 2.10 ✅ 文档统一:HANDOFF 为真源 + SIMPLE-* 归档

- **背景**: README / SIMPLE-PRD / SIMPLE-PLAN / SIMPLE-ARCHITECTURE 互相打架。
- **建议**: HANDOFF 加 "本文件是真源" 注 + SIMPLE-* 加归档 banner + README 全量更新(脚本名、API 表、目录树、IR 序列章节、localStorage 章节)。
- **采纳**: 2026-07-18 完成。

---

## §3. 开放问题 / 待用户拍板(下一会话可以问)

按重要性排序。

### 3.1 ir_runner 无限循环 vs N 圈配置

- **现状**: 默认无限循环,只能外部 stop。
- **问题**: 用户压测一天后想跑"正好 100 圈对比",怎么办?
- **建议**: ir_runner 加 `--loops N` 参数,backend 接受后透传;UI 上跑按钮加个"跑 N 次"变体。
- **状态**: ⏸️ 待用户拍板。

### 3.2 任务历史保留时长

- **现状**: 内存 dict,server 重启全丢(只有 log 文件残留)。
- **问题**: 跑完的任务历史应该保留多久?
- **选项**: (a) session 内永久保留;(b) 保留最近 N 条;(c) 落 JSON 文件 server 启动恢复。
- **状态**: ⏸️ 待用户拍板。**当前默认 a**,无清理机制。

### 3.3 ⛔ "重置"按钮的语义(已关闭,不做)

- **现状**: topbar 有 "重置" 按钮,但**没绑事件**。
- **原候选**:
  - 清空 localStorage(已选设备 / 已选脚本 / 已选序列)
  - 后端清空所有任务 + log 文件
  - 二者都做
- **关闭理由(2026-07-20)**: 用户接受"刷新也清空 deviceScripts"的副作用(因为非运行状态下拉框改脚本无害,运行中又被置灰),不需要做精细的"刷新保留 vs 重启清空"区分。加重置按钮反而引入新的认知成本(什么时候点?清什么?)。遵循用户"极简、不预设复杂架构"的偏好(见 [HANDOFF_PROMPT §12](HANDOFF_PROMPT.md))。
- **状态**: ⛔ 已关闭,暂不实施。如未来真的有"批量清任务/日志"的需求,再单独评估,不与本条混淆。

### 3.4 日志 DOM 上限 1000 行

- **现状**: [LOG_DOM_CAP L471](static/app.js#L471) 硬编码 1000(模仿 VSCode 终端)。
- **问题**: 用户查早 10 分钟的日志会被截,要看完整只能 export。
- **建议**: 暴露为设置项(暂存 settings 没做)。
- **状态**: ⏸️ 当前 1000 是默认值,不动;如需要再调。

### 3.5 WS 重连 max 5 次

- **现状**: 5 次后弹 "请刷新页面"。
- **问题**: server 重启慢于 5+5=15s 时,自动恢复就废了。
- **建议**: max 提到 10 次,或直接无限(代价:server 挂了白推)。
- **状态**: ⏸️ 当前 5 是合理值,不动;若用户反馈再调。

### 3.6 adb 设备列表刷新间隔

- **现状**: 3s 轮询。
- **问题**: 快设备列表刷得快(浪费) / 慢设备列表刷得慢(不实时)。
- **建议**: 改为 5s + 手动刷新按钮;或保留 3s(当前)。
- **状态**: ⏸️ 当前 3s OK,不动。

### 3.7 删除任务时是否要 confirm dialog?

- **现状**: `×` 按钮直接删除(无 confirm)。
- **问题**: 误点会丢日志。
- **建议**: 加 confirm(`confirm("删除任务 + 日志?")`)。
- **状态**: ⏸️ 当前无 confirm,用户没反馈过;若误删过再加。

### 3.8 多脚本队列(device-uniqueness 是否过严)

- **现状**: 后端 409,一台设备同时只能跑 1 个任务。
- **问题**: 想跑 "先跑 wifi_reboot 30 次,再跑 ir_runner" 这种串行编排怎么办?
- **选项**: (a) 保留 strict 1 个;(b) 加任务队列(`/api/run` 支持排队);(c) orchestrator(超出平台范围)。
- **状态**: ⏸️ 当前 strict 1 个,符合 MVP;如要做选 b。

### 3.9 Long 按键阈值

- **现状**: `ir_runner.py` parser 把 kind=`LongXXXX` 解析为长按 XXXX ms(`startswith("long")`)。
- **问题**: 用户写 `Long3000` 还是 `LongPress3000` 还是 `Long-3000`?
- **建议**: 统一格式 + README / KEY_REFERENCE 加示例。
- **状态**: ⏸️ 当前是 `Long3000`,需要 README 加一行示例。

### 3.10 设备名很长时的 truncate

- **现状**: `.device-name` title 属性给完整 serial,正文 ellipsis(但没测过)。
- **建议**: 加 CSS `text-overflow: ellipsis; overflow: hidden; white-space: nowrap;` 兜底。
- **状态**: ⏸️ 当前纯靠 flex 收缩,没测过长 serial。

---

## §4. 已知 UX 边界 case(已被发现但未修)

| # | 场景 | 当前行为 | 备注 |
|---|---|---|---|
| 1 | WS 在 server restart 时 log console 卡最后一行 | 没手动 rejoin | 用户得手动点 task card 或重启浏览器 |
| 2 | 任务被删除时 log 文件可能短暂残留 | 已修(后端同步删) | 偶发 race(删除 + 写同时) |
| 3 | modal 在小屏幕(< 1024px)的响应式 | 没做 | 桌面端 OK,平板可能溢出 |
| 4 | dark mode 切换 | 只有一种暗色主题 | 用户没要求过亮色 |
| 5 | 设备卡拖拽排序 | 没做 | 设备列表按 adb 返回顺序 |
| 6 | 服务器时区 | 用 `datetime.now()` 本地时间 | 跨时区会乱;用户单机 OK |
| 7 | 长 serial 截断 | 靠 flex 收缩 | 真没测过 |
| 8 | ir_runner 长按超长(Long99999) | parser 不限 | 实际按键不会这么久 |
| 9 | 多浏览器标签页同时打开 | 共享 localStorage 但各开各的 WS | 没冲突,但 UI 状态可能不同步 |
| 10 | Ctrl+F5 后 localStorage 保留(应该) | 保留 | 已验证 |

---

## §5. 视觉规范摘要

### 5.1 颜色 token

- `--bg`: 深灰背景
- `--panel`: 卡片背景
- `--accent`: 蓝色(选中 / 高亮)
- `--ok`: 绿色(运行中)
- `--err`: 红色(离线 / 失败)
- `--warn`: 橙色(中断中)

### 5.2 卡片 4 状态

| 状态 | 边框 | 背景 | 角标 |
|---|---|---|---|
| default | 无 | panel | 无 |
| active | accent 1px | panel 加亮 | ✓ 已选中 / ⚡ 运行中 |
| disabled | 无 | panel 60% 不透明 | 灰色 cursor not-allowed |
| error | err 1px | panel | 离线 |

### 5.3 字体

- 主字体: 系统 sans-serif(Win = Segoe UI / Mac = -apple-system)
- console: monospace(Win = Consolas)
- 标题: 14-16px
- 正文: 12-13px
- chip / 角标: 10-11px

### 5.4 间距 / 圆角

- panel gap: 8px
- card padding: 12-16px
- card radius: 8px
- chip radius: 4px

---

## §6. 文件清单与引用

| 文档 | 用途 | 状态 |
|---|---|---|
| [HANDOFF_PROMPT.md](HANDOFF_PROMPT.md) | 真源(代码 / API / 数据模型 / bug 历史) | 活跃 |
| [FRONTEND_UX.md](FRONTEND_UX.md) ← 你正在读 | 前端交互设计沉淀 | 活跃 |
| [SIMPLE-PRD.md](SIMPLE-PRD.md) | v2.0 初版 PRD | **历史归档** |
| [SIMPLE-PLAN.md](SIMPLE-PLAN.md) | v2.0 初版实施计划 | **历史归档** |
| [SIMPLE-ARCHITECTURE.md](SIMPLE-ARCHITECTURE.md) | v2.0 初版架构图 | **历史归档** |
| [README.md](../README.md) | 用户面向精简说明 | 活跃 |
| [KEY_REFERENCE.md](../ir_sequences/KEY_REFERENCE.md) | 23 个按键的 KEY_NAME 速查 | 活跃(手动维护) |

---

**最后更新**: 2026-07-20
**会话来源**: 累计多轮 IR 序列、modal picker、device card dropdown gating、localStorage 持久化、UI 徽章、WS 重连、WS taskId filter、onSelectDevice 关 WS、文档统一清理、default.ini 5/6 字段修复等话题。
**维护规则**: 新交互决策追加到 §1;新改进建议追加到 §2;新开放问题追加到 §3。**不要覆盖已有条目**(保留时间线)。