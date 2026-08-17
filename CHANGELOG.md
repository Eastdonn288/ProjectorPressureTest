# 更新日志 (Changelog)

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
