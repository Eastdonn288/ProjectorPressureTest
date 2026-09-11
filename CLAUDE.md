# PPTP — 投影仪压测平台 · 项目指令

> 本文件是会话启动时唯一自动载入的项目指令,必须保持精简(<100 行)。
> 项目全部细节在 docs/ 下,**懒加载、按需读** —— 详见下方"文档纪律"。

## 项目是什么
本地单机 Web 压测平台:在多台 ADB 投影仪上跑 Python 压测脚本(scripts/*.py),前端实时看每台设备日志。
**铁律:纯本地/单机、无远程、无鉴权、单用户。** 技术栈:FastAPI 单文件 server.py + 原生 JS(static/app.js) + ECharts 本地 vendor(无构建步骤)。

## 文档纪律(Context 预算铁律,最重要)
- **docs/HANDOFF_PROMPT.md 是真源(2026-09-11 v2.7.4)**,但**永远懒加载**:任务涉及哪个模块,就 `grep`/`Read` 对应章节;**严禁从 §1 开始通读全文档,严禁 `cat` 整个文件**。
- docs/SIMPLE-ARCHITECTURE.md(架构速览)、docs/FRONTEND_UX.md(前端决策)、README.md(用户向)、CHANGELOG.md(版本沿革)同理按需读。
- **会话启动 auto-load 面阈值**:CLAUDE.md + auto-memory 索引合计 **<5K tokens**;CLAUDE.md 本身 **<100 行**。新增长期事实 → 写进 memory 目录(每文件一条事实),**不要**堆进本文件。
- 新会话:记忆索引 MEMORY.md 已自动载入,需要细节再按 `[[slug]]` 读对应记忆文件。

## 代码约束(不可违背)
- **代码文件无中文**(scripts/*.py、static/app.js、static/style.css、static/index.html 等);中文只进 md 文档。
- 脚本 stdout **全英文/ASCII**(Windows 控制台 + WS 编码);前端 `#log-console` 输出全英文,UI 文案可中文。
- scripts/ 下脚本**独立可 CLI 跑**(`python -u scripts/x.py --device <serial>`);平台自动发现 scripts/*.py,零注册。
- **日志采集归平台,不归脚本**(v2.6.0):脚本**不要**自己抓 logcat / 开串口,平台在服务端统一采(所以脚本零改动)。唯一例外是脚本业务本身要串口(`serial_port` 参数)→ 平台自动让位。
- **前端只显示 stdout,永久**(v2.7.0):logcat / 串口照采、照归档,**前端永不展示**。v2.6.0 的通道显示方案(计数 pills / tab 接缝)已整体删除 —— 不要再加回来。
- **产物归 `archive/<模块>/<时间>_<脚本>_<设备>/`,永久保留**(v2.7.0/2.7.1):日志/报告/图表任务结束自动归档;**删任务不动存档**。图由浏览器渲染后回传(服务端无绘图库)。`logs/` 与 `reports/` **不能删**(运行期工作区 / 脚本契约),用户只需看 `archive/`。
- 已知但**暂不修改**的问题看 [TODO.md](TODO.md),别自作主张去修。
- ⚠ **若 Read/Edit 在某个文件上读到乱码、或 Grep 完全搜不到它**:本机有 E-SafeNet 透明加密,该文件被包了密文壳。**立刻停手别 Edit**(会把密文写回去毁文件),改用 Python 脚本读写 + 断言。2026-09-11 已在 server.py 上出现过一次,详见 [TODO.md](TODO.md) 第 5 节。
- 改动前先读 docs/HANDOFF_PROMPT.md §6"已批准设计决策(DO NOT REVERT)";与之冲突 → 先提矛盾再写代码。

## 版本与完成标准
- 每次改 server.py / 前端 / 脚本后 bump 版本:server.py 的 `version="x.y.z"`、healthz 返回的 version、static/index.html 的 `?v=` ×3;并在 CHANGELOG / README / HANDOFF 同步。**行号会漂,自己搜。**
- "完成" = `python -m py_compile` 通过 + 相关脚本 `--dump-params`/`--probe` 短跑验证 + 版本号 grep 全库一致。
- Python 解释器:`D:\Conda_Environments\dev_env\python.exe`(bash PATH 里没有 `python`)。

## 工作流偏好
- 大功能先对齐方案再写代码(用户偏好:Prompt 优化 → 计划 → 工程化)。
- 改动影响面先列出再动手;完成后如实报告验证结果(失败也要说)。
