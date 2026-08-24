# 更新日志 (Changelog)

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
