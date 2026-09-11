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

## 5. ⚠ `server.py` 被 E-SafeNet 透明加密,破坏了 Claude Code 的文件工具

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

**待办**:确认哪些文件受影响、是否有办法让 Agent 工具走白名单进程(或反过来避免用非白名单
进程写这些文件)。在那之前,`server.py` 一律按上面的方式改。
