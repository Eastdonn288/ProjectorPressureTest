# PPTP 报告格式与生成架构

> **本文是报告格式与参数架构的唯一真源(v2.10.0,2026-09-16)。**
> 面向两种读者:① 想读懂一份已生成报告的人;② 要给**新脚本**接入报告的开发者。
> **分工**:凡本文写到的(payload / row 模型 / HTML / params / stdout / 引擎 API / 归档命名与 `<stem>` 配对规则)
> 都以本文为准;**平台的认领与归档时机**(`_sniff_report_path` / `_CLAIMED_REPORTS`)归
> [ARCHITECTURE.md](ARCHITECTURE.md) §4.3,不属于本文。**文内不再有"以某文为准"的转引。**
> 平台整体架构见 [ARCHITECTURE.md](ARCHITECTURE.md)。

> 版本:v2.10.0 | 引擎版本:`_pptp_report.ENGINE_VERSION = 1.0.0` | 报告 schema:`pptp-report/1`

---

## 1. 它解决什么问题

一次压测跑完,stdout 里那一小段 `=== results ===` / `========== summary ==========` 是给**跑的时候的人**看的:
它在滚动的控制台里、跑完就沉了、没有排版、看不出哪个数偏离了、也没法发给别人。

报告要解决的是**跑完之后的事**:

| 需求 | 设计后果 |
|---|---|
| 给人看,不是给程序看 | 中文、有排版、结论按行拆开、每个数带一句"怎么读" |
| 能脱离平台存在 | **完全自包含**:无外部 CSS / JS / 图表库 / 字体 / 图片。拷出 `archive/` 目录,在一台从没装过 PPTP 的机器上、几年后双击打开,必须完整 |
| 不能比 stdout 少东西 | HTML 里有一个**「完整数据」折叠区**,把脚本写的那份 JSON 报告整体递归渲染进去 → `HTML ⊇ JSON` |
| 不能泄露口令 | 报告 dict 里的敏感键由脚本用 `hide_keys` 点名,渲染时**按键名在任意深度**隐去 |
| 不能连累主流程 | 写 HTML 失败**绝不抛出**(`reports/` 只读也不该让一次成功的跑变失败) |

**一条反向约束**:报告**不替代** stdout,也**不由引擎渲染 stdout**。用户决策原话是"只加 HTML,stdout 一个字不改"——
每个脚本的汇总块保持原样(三种风格并存),只在末尾多一行 `html =`。理由:控制台是用户实时在看的东西,不能为了统一风格去动它。

---

## 2. 架构:一份数据,两个渲染器

```
       脚本已有的局部变量(planned / actual / success / passed ...)
                    │
        ┌───────────┴────────────┐
        │                        │
        ▼                        ▼
  现有 print(...)          rows=[row(...), ...]
  ★ 一字不改               ★ 值表达式直接抄旁边那一句
                                 │
                                 ▼
                    doc = _pptp_report.build_payload(...)
                                 │   ← payload 是纯 dict,引擎不碰 stdout
                                 ▼
                    html = _pptp_report.write_html_report(json_path, doc)
                                 │
                                 ▼
                    <报告同目录>/<报告同stem>.html
```

**四个关键设计点,每个都有代价,都别推翻:**

1. **stdout 不由引擎渲染。** `rows` 由脚本在它现有 print 块**旁边**构建,值的表达式直接抄现成的那一句
   (例:`f"{success}/{actual} = {success_rate:.2f}%"`)。这样两处显示的数**不可能各算一遍而算出两个值**。
   代价:每个脚本多一个 3~8 行的 `rows=[...]`,不能自动从 print 语句反推。

2. **不碰现有 JSON 报告。** 全库没有任何消费者解析报告 JSON,所以它保持原样 = 零回归风险。
   HTML 通过「完整数据」折叠区把 JSON 整体渲染进去,保证内容不漏(见 §5)。

3. **路径推导封在引擎里。** `write_html_report(json_path, doc)` 自己由 JSON 路径推出 HTML 路径,
   **不给调用方拼字符串的机会** —— 因为 stem 必须逐字相同,归档才收得到它(见 §7)。这是硬约束,不是风格。

4. **引擎吞掉自己的异常。** `write_html_report` 从不抛出,失败时打一行 `[warn] failed to save html report: ...`
   并返回 `""`(空串,不是 `None`)。所以调用方写 `if html_path:` 而不是 `try:`。

---

## 3. Row 模型 —— 一份报告的主体

```python
_pptp_report.row(key, label_zh, value, level="info", note_zh="")
```

| 字段 | 含义 | 渲染位置 |
|---|---|---|
| `key` | 机器名,ASCII。同一份报告内唯一 | 不显示(留给将来做锚点/机器读) |
| `label_zh` | 这一行在量什么,中文 | 「指标」列 |
| `value` | 取值。字符串或数字,引擎负责转义 | 「取值」列 |
| `level` | 这一行的结论 | 「结论」列的中文色块 |
| `note_zh` | 怎么读这个数(阈值是什么、为什么算异常) | 「说明」列 |

### `level` 五个值 —— 用词一旦固定就别改

| `level` | 中文 | 什么时候用 |
|---|---|---|
| `ok` | 通过 | 达标 |
| `warn` | 注意 | 没到失败线,但已经在边上 |
| `fail` | 异常 | 不达标 |
| `inconclusive` | 样本不足 | **没跑够,不足以判定** —— 不是"通过"也不是"失败" |
| `info` | 信息 | 陈述事实,本就不做判定 |

`rows` 的 `level` 是**每一行自己的**结论;页头那条大字结论横幅用的是 `build_payload(level=...)`,两者独立。
一份"整体 PASS 但有一行 warn"的报告是正常且合法的。

**`rows` 通常 3~8 行。** 一行的粒度 = "一个能单独被质疑的结论"。把三个指标挤进一行 `value` 会让人无法指认是哪个出了问题。

### `inconclusive` 的分量(别用 PASS 掩盖它)

零迭代 / 早停 / 全部样本无效时,**不要编一个 PASS 或 FAIL**。正确写法是:

```python
judged = total > 0                       # 真的判过吗
result = ("PASS" if all_passed else "FAIL") if judged else None
level  = ("ok" if all_passed else "fail") if judged else "inconclusive"
```

`result=None` 时页头横幅渲染成 `— 本次运行不做通过/不通过的判定`,而不是绿色 PASS。

---

## 4. Payload schema `pptp-report/1`

`build_payload()` 的返回值,也是 `render_html()` 的唯一输入。全部字段都由 `build_payload` 签名约束:

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema` | str | 恒为 `"pptp-report/1"`。格式改了要换这个串 |
| `engine_version` | str | 渲染它的引擎版本,写进页脚 |
| `script` | str | 脚本文件名,如 `wifi_switch_stress.py` |
| `script_version` | str | 脚本自己的 `SCRIPT_VERSION` 常量 |
| `test_name` | str | 中文标题,渲染成 `<h1>` 与 `<title>` |
| `device_id` | str | 设备序列号 |
| `test_time` | str | `YYYY-MM-DD HH:MM:SS`,不给则取当前时间 |
| `result` | str \| None | `"PASS"` / `"FAIL"` / `None`(不判定) |
| `level` | str | 页头横幅用,必须是 §3 那五个之一;非法值降级为 `info` |
| `count_zh` | str | 横幅上的计数补充,如 `"4/4 次尝试成功"` |
| `warn_zh` | str | 非空则渲染成横幅下方一条黄色提示条 |
| `rows` | list[dict] | §3 的 row 列表。空列表 → **整个「结果总览」区块不出现** |
| `params_schema` | list[dict] | 脚本 `PARAMS` 原样传入 → §6 |
| `params_values` | dict | 本次生效值 `{name: value}` |
| `sections` | list[dict] | 脚本自述区块,见 §5 |
| `detail` | dict | 原始报告 dict → 「完整数据」折叠区 |
| `extra` | dict | 可选 `exit_code` / `report_name`,只进页脚 |
| `hide_keys` | list[str] | **不由 `build_payload` 产生**,脚本在拿到 doc 后自己加:`doc["hide_keys"] = ["password"]` |

`build_payload` 会把 `rows` / `sections` 里的假值过滤掉,`level` 非法时降级 —— 它**不校验** `detail` 的内容,那是脚本的责任。
未知字段一律忽略,所以将来加字段是向后兼容的。

---

## 5. HTML 页面结构

自上而下,区块编号是**一个贯穿全页的计数器**(`cn_num()`),保证不会出现"一、三、"这种跳号。

| 顺序 | 区块 | 谁提供 | 内容 |
|---|---|---|---|
| — | `<h1>` | 引擎 | `test_name` |
| — | `.sub` 副标题 | 引擎 | 设备 · 时间 · 脚本名+版本 · 报告引擎版本 |
| — | `.banner.{level}` | 引擎 | 综合结论大字 + 中文等级 + `count_zh`;`result=None` 时显示 `—` |
| — | `.warnbar` | 引擎 | `warn_zh`,可无 |
| 一 | **结果总览** | 引擎 | `rows` → 指标 / 结论 / 取值 / 说明 四列表 |
| 二 | **本次参数** | 引擎 | §6,`params_schema` 非空才出现 |
| 三…N | 脚本自述区块 | **脚本** | `sections`,有几个排几个 |
| 末 | **完整数据** | 引擎 | `<details>` 折叠,`detail` 递归渲染成键值表 |
| — | `.foot` 页脚 | 引擎 | 脚本名+版本 · 引擎版本 · schema · exit code · 原始报告文件名 |

### 脚本自述区块的三种形状

`build_payload(sections=[...])`,每个 `section` 由这三个构造器之一产生:

```python
_pptp_report.section_table(key, title_zh, head, rows, sub_zh="", empty_zh="")
    # head: 表头 ["SSID", "加密方式"];rows: 行列表;empty_zh: 无数据时的占位文字
_pptp_report.section_kv(key, title_zh, pairs, sub_zh="")
    # pairs: [(label_zh, value), ...] 的两列表
_pptp_report.section_list(key, title_zh, items, sub_zh="", empty_zh="")
    # items: 字符串列表 → 项目符号列表
```

### 敏感信息:`hide_keys`

`_detail_html(detail, hide_keys)` 在**任意深度**按键名匹配并隐去。所以:

```python
doc["hide_keys"] = ["password"]      # wifi_switch_stress 的写法
```

只要 JSON 里任何一层的键叫 `password`,值就渲染成「已隐去」。**注意这只管渲染,明文仍然在 JSON 报告里** ——
「完整数据」是为了 `HTML ⊇ JSON`,隐去是为了分享安全,两者在此处有意冲突,取隐去。
`archive/` 里的 `report.json` 仍含明文,这是既有的、已知的行为。

---

## 6. Params 架构 —— 参数表从哪来

这是用户点名要沉淀的那一块。**参数表不是脚本手写的,是从 `PARAMS` 声明生成的**,
所以它和前端配置弹窗**不可能对不上** —— 两者读的是同一个 schema。

### `PARAMS` schema 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | str | 参数名,与 `argparse` 的 `--name` 一致 |
| `label` | str | 中文短标签,前端与报告都显示它(这是**允许出现中文的字符串字面量**,与代码无中文的约束不冲突) |
| `type` | str | `int` / `float` / `bool` / `select` / `multiselect` |
| `default` | any | 默认值。**`""` 是有意义的默认值**(如 `watch_pkg` 的"不关注任何应用")。`multiselect` 默认值是**列表** |
| `min` / `max` | number | `int`/`float` 的取值范围,前端用 |
| `choices` | list | `select` 与 `multiselect` 用,**两种形状都存在,渲染必须都吃**(见下) |

### `choices` 的两种形状 —— 这是最容易踩的坑

```python
# 形状 A:纯字符串列表(app_launch / sensor_reboot / 多数脚本)
"choices": ["cold", "hot"]

# 形状 B:{value, label} dict 列表(battery_inout / perf_monitor 的 WATCH_PKG_CHOICES)
"choices": [{"value": "", "label": "不关注"}, {"value": "com.netflix.ninja", "label": "Netflix"}]
```

引擎的 `_choice_label(choices, value)` 两者都处理;**值在两种形状里都匹配不上时,原样显示该值而不是丢掉它** ——
丢掉会让人以为参数没生效。新脚本两种都能用,但同一个脚本内别混用。

### 表格长什么样:三列

```
参数                本次取值                    来源
循环次数  cycles     5                          已改        默认 100
关注应用  watch_pkg  (不关注 / 只看前台是哪个应用)  默认        本次未传,取声明默认值
```

**「本次取值」列**按 `type` 渲染:

| 情况 | 渲染成 |
|---|---|
| `type="bool"` | `是` / `否`(**绝不打 `True`/`False`**) |
| `type="select"` | 用 `_choice_label` 解析后的中文标签 |
| `type="multiselect"` | 每个取值过 `_choice_label` 后用 `、` 连接(**绝不打 Python 列表字面量** `['mem', 'gpu']`)。**空选择渲染「(未勾选)」,不是 `—`** —— 空选择是一个真实、故意的答案(`—` 的含义是"没传这个参数")。逗号串也吃(CLI 就是那个形状) |
| 值 `None` | `—` |
| 其他 | 原样转义 |

**「来源」列**回答"这个值是哪来的" —— 这是读归档报告的人真正关心的问题。三态,由 `params_values` 里**有没有这个键**和**值是否等于 default**共同决定:

| 情况 | 渲染成 | 「本次取值」列显示 |
|---|---|---|
| `params_values` 里**没有**这个键 | `默认` + `本次未传,取声明默认值` | schema 的 `default` |
| 有,且值 `== default` | `默认` + `传入了与默认相同的值` | 传入的值 |
| 有,且值 `!= default` | `已改` + `默认 <default>` | 传入的值 |

所以"用户改没改过这个参数"在报告里是**看得出来的**,不用去翻 `summary.json` 的 `params`。

> `(空)` 与 `—` 只出现在**「来源」列的小字里**,用来把 schema 的默认值写清楚 ——
> 因为空串在本仓库是有意义的默认值(`watch_pkg` 的"不关注任何应用"),裸打印会被误读成渲染故障。
> 它们**不是**给「本次取值」列用的。

**遍历顺序是 schema 声明顺序,不是值的字典顺序** —— 所以每个声明的参数都有一行,顺序与前端配置弹窗一致。
`params_values` 里漏传的参数不会消失(回退到声明默认值,并把这件事说明白),所以这能反向查出脚本漏传了参数。

### `--dump-params` 契约(接入时必须保护)

平台通过 `python scripts/x.py --dump-params` 读 schema,要求:

- **纯 stdout 输出合法 JSON**,不夹任何其他行;
- **在 `argparse` 之前短路返回** —— 它必须能在没有任何参数、也没有设备的情况下跑;
- 所以**引擎的 import 不能有副作用**。`_pptp_report` 是 import-pure 的(只定义常量与函数),
  接入时把 `import _pptp_report` 放在文件头不会拖慢或污染 `--dump-params`。

---

## 7. 文件命名与归档 —— 新增脚本必须守的约定

### 配对规则(唯一的硬约束)

```
reports/stress-test/<模块>/<前缀>_<设备短名>_<时间戳>.json     ← 脚本写的报告
reports/stress-test/<模块>/<前缀>_<设备短名>_<时间戳>.html     ← 引擎写的报告 ← 同目录、同 stem
```

时间戳与设备短名在 `.json` 和 `.html` 里**是同一串字符**,因为它们都是从同一个 `json_path` 推导出来的。
这不是审美要求,是归档能收到 HTML 的**唯一条件**:

`server.py` 的 `_companion_files(report)` 收编一个文件的全部条件:

1. 与报告**同目录**;
2. 文件名以 `<报告的 stem> + "."` 开头;
3. 后缀 ∈ `(".csv", ".html")`(大小写不敏感)。

三条同时满足才带走。**用 mtime 或名字子串配对是被明确拒绝的** —— 同一秒内跑两次会让一次运行捡到邻居的数据。

> 推论:如果你绕开 `write_html_report` 自己拼 HTML 路径,只要 stem 差一个字符,HTML 就静默不进归档。
> **不要绕开它。**

### 归档里长什么样

归档时主报告被**改名**成 `report.json`(`report-2.json` / `report-3.json` … 多报告时),
但**伴随文件保持原文件名**。所以归档目录里看到的是:

```
archive/wifi/20260916-092055_wifi_switch_stress_B0403374A2A508001F00/
├── stdout.log
├── logcat.log
├── summary.json                    ← 含 artifacts 清单,前端「报告」按钮读它
├── report.json                     ← 主报告,改过名
└── wifi_cycle_B0403374A2A508001F00_20260916_092136.html   ← 伴随文件,原文件名
```

### 三个平台侧的表,新增脚本要看

| 表 | 作用 | 漏了会怎样 |
|---|---|---|
| `ARCHIVE_MODULES` | 脚本名 → 归档模块目录名(如 `wifi_switch_stress.py → "wifi"`)。未登记落 `other/` | 报告进 `archive/other/` |
| `REPORT_WRITING_SCRIPTS` | **两个作用**:① 措辞 `summary.json` 里"本脚本不写报告"的说明;② **mtime 兜底扫描的准入闸门** | 硬杀(脚本没来得及打 `report :` 行)时**静默丢报告** |
| `ARCHIVE_REPORT_NAME` | 归档里主报告的名字,恒为 `report.json` | — |

---

## 8. stdout 契约 —— 改 stdout 前必须自查

平台靠**嗅探 stdout** 找报告,规则在 `server.py` 的 `_sniff_report_path`:一行算命中,当且仅当

1. 行内含字面量 `" : "`(**空格-冒号-空格**),且
2. 行内含子串 `report`,且
3. 最后一个 `" : "` 右边 `.strip().lower()` 以 `.json` 结尾。

**只取第一行命中。** 由此推出接入时两条不可违反的规则:

| 规则 | 原因 |
|---|---|
| **`report :` 行保持最后一行** | 嗅探取第一行命中;把它挪到 `html =` 之后不会立刻出错,但一旦将来多出一行也含 `report` 的输出,归属就可能错到别人头上 |
| **`html =` 行必须用 `=` 而不是 `:`** | 若写成 `html : ....html`,第 3 条以 `.json` 结尾不满足,不会被误认 —— 但用 `=` 是**结构性**避免,不依赖后缀巧合 |

### 落地形状(照抄)

```python
    try:
        report_path = save_report(...)                    # 原样
        html_path = _pptp_report.write_html_report(report_path, doc)
        if html_path:
            print(f"  html             = {html_path}")     # 用 =,不是 :
        print(f"  report           : {report_path}")       # 最后一行
    except Exception as e:
        print(f"[warn] failed to save report: {e}")
```

`html =` 那一行的空格数**对齐到本文件自己 `report :` 的冒号列**(每个脚本 padding 宽度不同,
用固定空格数会在多数文件里看起来是歪的)。

### 报告写入**不要**放在「跑满才写」的条件里

上式的 `try` **不能**被任何形如 `if not interrupted:` 的守卫包住。判据很简单:
**脚本已经打印了 Overall Result,它就欠一份报告** —— stdout 有结论而产物为零,
是这套契约里最难解释的一种状态(读者手里只有一个数字,却没有任何东西可以对账)。

若中断时数据确实不足以支撑原来的判定,**不要**靠「不写」来回避,而是**照写 + 在报告里注明**
(`interrupted` / `interrupted_note` 进 JSON,同一句话进 `warn_zh` → 页面横幅下的黄条),
判定本身一个字不改 —— 这样控制台与页面**不可能**互相打架。

另:守卫通常还会顺带藏起一个事实 —— **中断时 stdout 已经在打 Overall Result 了**,
所以「不写报告」并不会让那次中断更不可见,只是让它更不可查。见 §12。

### 自查清单(改完 stdout 逐条过)

- [ ] `report :` 仍是**最后一行**输出
- [ ] `report :` 行含 `" : "`、含 `report`、右侧以 `.json` 结尾
- [ ] `html =` 用的是 `=`,且右侧不以 `.json` 结尾
- [ ] 运行 `python scripts/x.py --dump-params` 仍然只吐一行合法 JSON
- [ ] 除新增的那一行 `html =` 外,**stdout 与本轮改动前逐字节一致**
- [ ] `reports/stress-test/<模块>/` 下 `.json` 与 `.html` **同 stem 成对**出现,没有孤儿
- [ ] 若脚本有 `--selftest`(目前只有 `perf_monitor`),必须仍然全绿
- [ ] 报告写入**没有**被「只在跑满时写」的条件包住;中断路径上会打印 Overall Result 的脚本,
      那条路径也必须写报告(见上一节)

---

## 9. 引擎 API 与最小接入示例

```python
import _pptp_report

# —— 数据 ——
row(key, label_zh, value, level="info", note_zh="") -> dict
section_table(key, title_zh, head, rows, sub_zh="", empty_zh="") -> dict
section_kv(key, title_zh, pairs, sub_zh="") -> dict
section_list(key, title_zh, items, sub_zh="", empty_zh="") -> dict
build_payload(*, script, test_name, device_id, test_time=None,
              script_version="", result=None, level="info",
              count_zh="", warn_zh="", rows=(), params_schema=None,
              params_values=None, sections=(), detail=None, extra=None) -> dict

# —— 渲染与落盘 ——
render_html(doc) -> str                 # doc -> 一个自包含 HTML 字符串
render_params_table(values, schema, num=None, title_zh="本次参数") -> str
write_html_report(json_path, doc) -> str   # ★ 正常入口:never raises,失败返回 ""
html_path_for(json_path) -> str            # 由 JSON 路径推 HTML 路径(调用方一般不需要)
cn_num(n) -> str                           # 1 -> "一"
esc(v) -> str                              # 转义;None -> &mdash;

# —— 原子写(自己写报告 JSON 的脚本用)——
atomic_write_text(path, text) -> None      # 临时文件 + os.replace
atomic_write_json(path, payload, *, compact=False) -> None
    # compact=True 复现 perf_monitor 的 ensure_ascii=True,separators=(",",":")

# —— 预留(当前无调用方,见 §10)——
attach(payload, doc) -> dict            # 把 doc 折进 JSON,使 JSON 能重建页面
document_of(payload) -> dict            # attach() 的逆
```

### 最小完整接入(可直接抄)

```python
# 文件头(放在其他 import 之后)
# Shared report engine, for the .html twin of this script's JSON report.
# A sibling module, resolved via sys.path[0]: the platform launches scripts by
# absolute path and a plain `python scripts/x.py` puts scripts/ on sys.path too.
# See docs/REPORT_FORMAT.md.
import _pptp_report

SCRIPT_VERSION = "1.0.0"

# ... main() 里,汇总打印之后 ...
    judged = total > 0
    all_passed = judged and passed == total
    doc = _pptp_report.build_payload(
        script="x_stress.py", script_version=SCRIPT_VERSION,
        test_name="某某压测", device_id=args.device,
        result=("PASS" if all_passed else "FAIL") if judged else None,
        level=("ok" if all_passed else "fail") if judged else "inconclusive",
        count_zh=f"{passed}/{total} 次通过" if judged else "",
        rows=[
            _pptp_report.row("result", "综合结果", "PASS" if all_passed else "FAIL",
                             "ok" if all_passed else "fail",
                             "全部迭代通过" if all_passed else "存在失败迭代"),
            _pptp_report.row("rate", "通过率", f"{rate:.1f}%",
                             "ok" if rate >= 98 else "fail", "阈值 >= 98%"),
        ],
        params_schema=PARAMS, params_values={"cycles": cycles},
        detail={"planned": cycles, "passed": passed, "total": total},
        extra={"exit_code": 0 if all_passed else 1},
    )

    try:
        report_path = save_report(...)
        html_path = _pptp_report.write_html_report(report_path, doc)
        if html_path:
            print(f"  html          = {html_path}")
        print(f"  report        : {report_path}")
    except Exception as e:
        print(f"[warn] failed to save report: {e}")
```

---

## 10. 新脚本接入清单

1. `import _pptp_report`(**放在其他 import 之后**,带上面的注释);加 `SCRIPT_VERSION = "1.0.0"`
2. 确认 `PARAMS` 存在且 `--dump-params` 短路在 `argparse` **之前**
3. 在现有汇总打印**旁边**构建 `rows` —— 值表达式直接抄那一句 print,不要重算
4. 需要补充说明的,加 `sections=[...]`(用 `section_table` / `section_kv` / `section_list`)
5. `detail=` 传脚本写的那份报告 dict,保证 `HTML ⊇ JSON`
6. 报告 dict 里有口令/密钥 → `doc["hide_keys"] = ["password"]`
7. 在 `report :` 打印**之前**插入 `html =` 行,用 `=`,对齐到本文件冒号列
8. **报告写入不要藏在「跑满才写」的条件里** —— 脚本一旦在中断路径上也打印 Overall Result,
   那条路径就同样欠一份报告;数据不足**靠报告里注明**,不靠「不写」(见 §8 / §12)
9. 在 `server.py` 的 `ARCHIVE_MODULES` 登记模块名(未登记落 `other/`)
10. 若脚本从「不写报告」变成「写报告」,在 `REPORT_WRITING_SCRIPTS` 加上它(**别漏**,漏了硬杀时静默丢报告)
11. 过一遍 §8 的自查清单,再跑一遍 §11 的验证

---

## 11. 验证一份报告该看什么

| 层次 | 检查 |
|---|---|
| 静态 | `python -m py_compile scripts/x.py`;`--dump-params` 仍是一行合法 JSON |
| stdout | 与改动前逐字节比对,差异**只有**多出的那一行 `html =` |
| 落盘 | `reports/stress-test/<模块>/` 下 `.json` 与 `.html` 同 stem 成对,无孤儿 |
| 页面 | 浏览器打开:中文正常;**断网后排版仍完整**(自包含验证);参数表显示的是生效值且默认/已改标记正确 |
| 归档 | 平台跑一轮 → `archive/<模块>/<时间>_.../` 里 `report.json` 与 `<stem>.html` 都在;`summary.json` 的 `artifacts` 列出 html;`report_source` 是 `stdout_line` |
| 前端 | 任务卡「报告」按钮 → 新标签页渲染该次 HTML |
| 中断 | 平台「中断」→ 报告与 html 仍然写出;**报告里必须注明本次被中断**(见 §8 与 §12),否则半场会被读成一场差的 |

---

## 12. 已知残余(如实记录,别当成 bug 去"修")

| 残余 | 说明 |
|---|---|
| ~~`wifi_onoff_stress.py` 中断时不写报告~~ **已解决(2026-09-16 用户裁决)** | 原先报告写入被 `if not interrupted:` 包着(脚本自身既有设计),而 `=== results ===` + `OVERALL` 在守卫**外**、中断时照打 —— 于是中断时 stdout 有 Overall Result 而 HTML 没有。**用户裁决**「中断时报告与 HTML 都写,报告简单注明一下吧」:守卫**整条删掉**(只补 HTML 不可能 —— 同 stem 才能被归档收走,孤儿 HTML 会被静默丢弃),判定**一个字不改**,中断进 `interrupted` / `interrupted_note` 并渲染成页面横幅下那条黄条 |
| `attach` / `document_of` 是死 API | 引擎里定义了、有 docstring,但全库无调用方(JSON 能重建页面这个能力目前没人用)。保留作预留,docstring 已改成如实说明 |
| `app_launch_stress` 一次运行多份报告 | 每个 APP 一份 json + html。前端「报告」按钮**只开第一份**(按文件名排序),其余要从「存档」进去取。按钮 tooltip 里写了共几份 |
| `ir_runner.py` 无报告 | **有意**:没有 `PARAMS`、没有 PASS/FAIL 语义,是 `.ini` 驱动的交互式序列执行器。给它编一个不存在的结论是造假 |
| 明文口令仍在归档 JSON 里 | `hide_keys` 只管 HTML 渲染。`archive/.../report.json` 仍含明文 Wi-Fi 口令,这是既有行为 |
| `python -m scripts.x` 拉不起 | 兄弟模块 import 靠 `sys.path[0]` = 脚本所在目录。平台用绝对路径拉起、CLI 直跑都满足;`-m` 方式不满足。**当前全库没有这种调用方式** |
| 引擎约 590 行,计划里估的 320 | 计划估少了。多出来的是 params 表(两种 `choices` 形状)、`_detail_html` 递归隐去、区块构造器 |

---

## 13. 相关文档

- [START-HERE.md](START-HERE.md) —— 新会话入口(这是什么、先读哪、SOP)
- [ARCHITECTURE.md](ARCHITECTURE.md) —— 平台侧归档逻辑、`_sniff_report_path` / `_CLAIMED_REPORTS`、数据流全景
- [DECISIONS.md](DECISIONS.md) —— 报告引擎的已批准决策(DO NOT REVERT,分组「报告引擎」)
- [PITFALLS.md](PITFALLS.md) —— 改脚本 stdout / 报告渲染时会踩到的坑
- [PERF_MONITOR_V2.md](PERF_MONITOR_V2.md) —— `perf_monitor` 自身的判定模型(`judge()` / gates / `ROW_SPEC`)
- [CHANGELOG.md](../CHANGELOG.md) —— v2.10.0 起每次变动的记录
