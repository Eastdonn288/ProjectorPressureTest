# 按键对照表

> 手动维护 · 两种按键族 · 新增 KEYCODE_* 上层注入按键(2026-08-17)
> 改 ini 时查这里:左边是按钮的中文描述,右边是 ini 里要写的 KEY_NAME / KEYCODE_NAME。
> 两族都支持短按 `Short` 与长按 `LongXXXX`(XXXX = 按住毫秒,缺省 1500);名称由 `ir_runner` 按前缀自动分发。

## 一、sendevent 按键(KEY_*,需 userdebug/root)

> 走 `adb shell sendevent` 直接写 `/dev/input/eventN`;user 版无权限,需 userdebug 或 root。

| 中文名 | KEY_NAME |
|--------|----------|
| YoutubeMusic热键 | `KEY_AB` |
| Voice键 | `KEY_ASSISTANT` |
| 返回键 | `KEY_BACK` |
| Profile键 | `KEY_BOOKMARKS` |
| Setting键 | `KEY_CALENDAR` |
| 调焦down键 | `KEY_CHANNELDOWN` |
| 调焦up键 | `KEY_CHANNELUP` |
| 下键 | `KEY_DOWN` |
| OK键 | `KEY_ENTER` |
| Home键 | `KEY_HOME` |
| 信源键 | `KEY_KP1` |
| 左键 | `KEY_LEFT` |
| Memo键 | `KEY_MEMO` |
| Menu键 | `KEY_MENU` |
| Mute键 | `KEY_MUTE` |
| Netflix热键 | `KEY_PAGEUP` |
| Power键 | `KEY_POWER` |
| 右键 | `KEY_RIGHT` |
| 梯形对焦键 | `KEY_TV2` |
| 上键 | `KEY_UP` |
| Youtube热键 | `KEY_VCR` |
| PrimeVideo热键 | `KEY_VCR2` |
| VolumeDown键 | `KEY_VOLUMEDOWN` |
| VolumeUp键 | `KEY_VOLUMEUP` |

## 二、上层注入按键(KEYCODE_*,user 版可用,无需 userdebug)

> 走 `adb shell input keyevent`,Android keycode **名称**由设备端解析为数值,故不需硬编码数字。
> 中文名为**占位符**,后续由你自己修改。未列入下表的标准 KEYCODE_*(如 `KEYCODE_POWER`)会原样透传,可直接用。

> ⚠️ **【KEYCODE 长按暂无实际作用】(2026-08-17 确认)**
> 此族按键**只支持短按 `Short`**。写 `LongXXXX` 不会报错、任务照常完成,
> 但底层 `adb shell input keydown` / `input keyup` 子命令在当前 Android 版本
> (实机 Android 14 / SDK 34)不存在,命令**静默失败**——不注入任何按键,白等 `XXXX` 毫秒。
> 只有短按(以及 Android 12+ 的 `--longpress`,本平台暂未采用)能真正注入。
> 若真机确有"长按退出"类需求,需另议方案(见下方备注)。

| 中文名(占位) | ini 里写 | 注入目标 Android keycode |
|--------------|----------|--------------------------|
| 2D3D(占位) | `KEYCODE_2D3D` | KEYCODE_TV_INPUT_HDMI_1 |
| BI(占位) | `KEYCODE_BI` | KEYCODE_TV_INPUT_HDMI_3 |
| PIC(占位) | `KEYCODE_PIC` | KEYCODE_TV_INPUT_HDMI_4 |
| ADC(占位) | `KEYCODE_ADC` | KEYCODE_TV_NETWORK |
| COLOR(占位) | `KEYCODE_COLOR` | KEYCODE_TV_ANTENNA_CABLE |
| REST1(占位) | `KEYCODE_REST1` | KEYCODE_BUTTON_2 |
| REST2(占位) | `KEYCODE_REST2` | KEYCODE_TV_TERRESTRIAL_ANALOG |
| REST3(占位) | `KEYCODE_REST3` | KEYCODE_BUTTON_5 |
| ATV(占位) | `KEYCODE_ATV` | KEYCODE_ZENKAKU_HANKAKU |
| DTV(占位) | `KEYCODE_DTV` | KEYCODE_BUTTON_15 |
| FAC(占位) | `KEYCODE_FAC` | KEYCODE_HENKAN |
| WRITE_MAC(占位) | `KEYCODE_WRITE_MAC` | KEYCODE_BUTTON_12 |
| AV(占位) | `KEYCODE_AV` | KEYCODE_BUTTON_9 |
| YPBPR(占位) | `KEYCODE_YPBPR` | KEYCODE_TV_TIMER_PROGRAMMING |
| HDMI(占位) | `KEYCODE_HDMI` | KEYCODE_NAVIGATE_PREVIOUS |
| VGA(占位) | `KEYCODE_VGA` | KEYCODE_NAVIGATE_NEXT |
| USB(占位) | `KEYCODE_USB` | KEYCODE_NAVIGATE_IN |
| MAC(占位) | `KEYCODE_MAC` | KEYCODE_NAVIGATE_OUT |
| DDC(占位) | `KEYCODE_DDC` | KEYCODE_TV_RADIO_SERVICE |
| IP(占位) | `KEYCODE_IP` | KEYCODE_BUTTON_14 |
| Menu键(占位) | `KEYCODE_MENU` | KEYCODE_BUTTON_13 |
| Home键(占位) | `KEYCODE_HOME` | KEYCODE_F3 |
| FOCUS_KEYSTONE(占位) | `KEYCODE_FOCUS_KEYSTONE` | KEYCODE_TV_SATELLITE_SERVICE |

> 备注:KEYCODE_MENU / KEYCODE_HOME 与第一张表的 KEY_MENU / KEY_HOME 是**同一物理按钮的两个命名**——
> KEY_* 走 sendevent(userdebug),KEYCODE_* 走上层注入(user 版)。按需选一族使用,勿混用同一物理键的两族做同一步。
