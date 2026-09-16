"""Shared Chinese HTML report engine for PPTP stress scripts.

Why this module exists
----------------------
Every script prints an English summary block to stdout, and the platform only
ever shows stdout. That block is fine live and useless a month later: it is
gone when the task is deleted, it is English, and it has no explanation of what
the numbers mean. perf_monitor v2.9.0 grew a self-contained Chinese HTML twin
of its JSON report for exactly that reason; this module is that idea made
reusable so the other scripts can have it too.

The architecture (one dataset, two renderers)
---------------------------------------------
    script's own local variables
        |
        +--> existing print(...) calls      <- NOT rendered by this module
        |
        +--> rows = [row(...), ...]         <- new, built beside those prints
                    |
                    v
             build_payload(...)  ->  a plain dict
                    |
                    v
             write_html_report(json_path, payload)  ->  <same stem>.html

This module deliberately does NOT render the console. Each script keeps its
own print statements byte-for-byte (user decision, 2026-09-16: the console is
what the user watches live, do not churn it). What the module guarantees
instead is that the HTML is built from the SAME local variables, so the two
surfaces cannot silently disagree about what happened.

Self-contained output
---------------------
No external stylesheet, no script tag, no chart library, no network font. The
file has to survive being mailed to someone, dropped in a ticket, or opened on
a machine that has never run PPTP - possibly years from now. Everything the
page needs is in the page.

Import rules (load-bearing)
---------------------------
1. stdlib only, and NO side effects at import time. The platform imports this
   before a script decides what to do, and `--dump-params` must stay a cheap
   pure-stdout call that works with no device attached.
2. Importing a sibling requires `scripts/` on sys.path. That holds for every
   invocation this project uses - `python -u <abs>/scripts/x.py`, from CLI or
   from server.py - because CPython puts the script's own directory at
   sys.path[0]. It does NOT hold for `python -m scripts.x`. Do not use -m.
3. The leading underscore is required: server.py skips `_`-prefixed .py files
   when it discovers scripts, which is what keeps this file out of the task
   panel's script list. See _list_scripts().

Language convention
-------------------
Code and comments here are ASCII, same as every other scripts/*.py. Chinese
appears only inside string literals that are DATA FOR A HUMAN READER - table
headers, verdict words, explanatory notes. That is the same precedent the
existing PARAMS entries set (`"label": "循环次数"`), and it is the whole point
of this module. Nothing here is ever printed to stdout.
"""

from __future__ import annotations

import html
import json
import os
from datetime import datetime

ENGINE_VERSION = "1.0.0"
SCHEMA = "pptp-report/1"

# Level vocabulary, shared with perf_monitor's gate vocabulary so a reader who
# learned the colours there does not have to learn a second set here.
# "info" is the neutral one: a number that is neither good nor bad.
LEVELS = ("ok", "warn", "fail", "inconclusive", "info")
LEVEL_ZH = {
    "ok": "通过",
    "warn": "注意",
    "fail": "异常",
    "inconclusive": "样本不足",
    "info": "信息",
}

HTML_CSS = """:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 28px 32px 48px; background: #f4f6f8;
  color: #1d2733; font: 14px/1.6 "Microsoft YaHei", "PingFang SC",
  "Noto Sans CJK SC", "Segoe UI", Arial, sans-serif; }
h1 { margin: 0 0 4px; font-size: 20px; }
h2 { margin: 30px 0 10px; font-size: 15px; color: #33475b; }
h2 span { font-weight: 400; color: #7a8b9c; font-size: 13px; }
.sub { color: #67788a; font-size: 13px; margin-bottom: 18px; }
.banner { display: flex; align-items: baseline; gap: 14px; padding: 16px 20px;
  border-radius: 8px; border-left: 6px solid #8a9aa8; background: #fff;
  box-shadow: 0 1px 3px rgba(20,40,60,.09); }
.banner b { font-size: 26px; letter-spacing: .5px; }
.banner .zh { color: #4a5a6a; }
.banner.ok { border-color: #2f9e5f; } .banner.ok b { color: #227a49; }
.banner.warn { border-color: #d98c12; } .banner.warn b { color: #a86a06; }
.banner.fail { border-color: #cf3f3f; } .banner.fail b { color: #a92c2c; }
.banner.inconclusive { border-color: #8a9aa8; } .banner.inconclusive b { color: #55636f; }
.banner.info { border-color: #8a9aa8; } .banner.info b { color: #55636f; }
table { width: 100%; border-collapse: collapse; background: #fff;
  box-shadow: 0 1px 3px rgba(20,40,60,.09); border-radius: 6px;
  overflow: hidden; }
th, td { padding: 9px 12px; text-align: left; vertical-align: top;
  border-bottom: 1px solid #e6ebf0; }
th { background: #eef2f6; font-weight: 600; color: #33475b; white-space: nowrap;
  font-size: 13px; }
tr:last-child td { border-bottom: 0; }
td.k { font-weight: 600; color: #22303d; white-space: nowrap; }
td.k small, td.note small { display: block; font-weight: 400; color: #8a9aa8;
  font-size: 11.5px; }
td.v { font-family: Consolas, "Courier New", monospace; font-size: 13px;
  color: #1d2733; }
td.note { color: #5b6b7c; font-size: 13px; }
td.num { text-align: right; font-family: Consolas, "Courier New", monospace;
  font-size: 13px; }
.chip { display: inline-block; min-width: 52px; text-align: center;
  padding: 1px 8px; border-radius: 10px; font-size: 12px; color: #fff;
  background: #8a9aa8; }
.chip.ok { background: #2f9e5f; } .chip.warn { background: #d98c12; }
.chip.fail { background: #cf3f3f; } .chip.inconclusive { background: #8a9aa8; }
.chip.info { background: #8a9aa8; }
.foot { margin-top: 26px; color: #7a8b9c; font-size: 12.5px; }
.foot code { color: #4a5a6a; }
.warnbar { margin-top: 18px; padding: 10px 14px; border-radius: 6px;
  background: #fff6e5; border-left: 4px solid #d98c12; color: #7a5405;
  font-size: 13px; }
details { margin-top: 26px; }
details summary { cursor: pointer; color: #33475b; font-weight: 600;
  font-size: 15px; }
details .sub { margin: 8px 0 10px; }
.redacted { color: #a86a06; font-style: italic; }
"""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def esc(v) -> str:
    """HTML-escape a value. None renders as an em dash, never as 'None'."""
    if v is None:
        return "&mdash;"
    return html.escape(str(v), quote=True)


def html_path_for(json_path: str) -> str:
    """The HTML twin of a JSON report: same directory, same stem, .html.

    This derivation is the ONLY sanctioned one, because the stem pairing is
    exactly what server.py's _companion_files uses to decide whether the HTML
    gets archived at all. It matches on `report.stem + "."` with a suffix in
    (".csv", ".html"). Any other name is never claimed, and the file then sits
    in reports/ forever while the archive looks perfectly fine - a silent loss.
    Hence the hard check rather than a silent string append.
    """
    stem, ext = os.path.splitext(json_path)
    if ext.lower() != ".json":
        raise ValueError(f"report path is not a .json: {json_path!r}")
    return stem + ".html"


def atomic_write_text(path: str, text: str) -> None:
    """Write via a temp file + os.replace: a reader never sees a half file.

    newline="\\n" is forced so the artefact does not depend on the writing
    machine's platform.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def atomic_write_json(path: str, payload, *, compact: bool = False) -> None:
    """Same temp-file discipline as atomic_write_text.

    `compact` picks perf_monitor's historical format (ASCII-escaped, no
    spaces). It defaults to False because the stress scripts' existing reports
    are `ensure_ascii=False, indent=2`, and quietly reformatting six report
    shapes while adding an HTML twin would be an unasked-for change.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(payload, f, ensure_ascii=True, separators=(",", ":"))
        else:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# The document model
# ---------------------------------------------------------------------------
def row(key: str, label_zh: str, value, level: str = "info",
        note_zh: str = "") -> dict:
    """One result row: a name, what it measured, and how to read it.

    `level` colours the row on the page and must be one of LEVELS. Scripts
    with no pass/fail semantics leave it at "info" rather than inventing a
    verdict the run did not produce.
    """
    if level not in LEVELS:
        level = "info"
    return {"key": key, "label_zh": label_zh, "value": value,
            "level": level, "note_zh": note_zh}


def section_table(key: str, title_zh: str, head: list, rows: list,
                  sub_zh: str = "", empty_zh: str = "") -> dict:
    return {"kind": "table", "key": key, "title_zh": title_zh, "head": head,
            "rows": rows, "sub_zh": sub_zh, "empty_zh": empty_zh}


def section_kv(key: str, title_zh: str, pairs: list, sub_zh: str = "") -> dict:
    """pairs: [(label_zh, value), ...] - a two-column table."""
    return {"kind": "kv", "key": key, "title_zh": title_zh, "pairs": pairs,
            "sub_zh": sub_zh}


def section_list(key: str, title_zh: str, items: list, sub_zh: str = "",
                 empty_zh: str = "") -> dict:
    return {"kind": "list", "key": key, "title_zh": title_zh, "items": items,
            "sub_zh": sub_zh, "empty_zh": empty_zh}


def _cell(c) -> tuple:
    """Render one table cell to (css_class, inner_html).

    Returns the class separately rather than emitting a whole <td> so the
    caller never has to ask "is this string already markup?" - a question that
    can only be answered by inspecting the data, which is exactly how a value
    that begins with "<td" would escape escaping.

    A cell is either a plain value, or:
        {"text":..., "sub":..., "cls": "k|v|num|note", "chip": <level>}
    """
    if isinstance(c, dict):
        if c.get("chip"):
            lv = c["chip"] if c["chip"] in LEVELS else "info"
            return "", (f'<span class="chip {lv}">{esc(LEVEL_ZH.get(lv, lv))}'
                        f"</span>")
        body = esc(c.get("text"))
        if c.get("sub"):
            body += f"<small>{esc(c['sub'])}</small>"
        cls = c.get("cls")
        return (cls if cls in ("k", "v", "num", "note") else "", body)
    return "", esc(c)


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------
def build_payload(*, script: str, test_name: str, device_id, test_time=None,
                  script_version: str = "", result=None, level: str = "info",
                  count_zh: str = "", warn_zh: str = "", rows=(),
                  params_schema=None, params_values=None, sections=(),
                  detail=None, extra=None) -> dict:
    """Assemble the document the HTML renderer consumes.

    `rows` is the heart of it: usually 3-8 entries, each naming one thing the
    run measured and how to read it. Everything else is chrome.
    """
    if level not in LEVELS:
        level = "info"
    return {
        "schema": SCHEMA,
        "engine_version": ENGINE_VERSION,
        "script": script,
        "script_version": script_version,
        "test_name": test_name,
        "device_id": device_id,
        "test_time": test_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "result": result,
        "level": level,
        "count_zh": count_zh,
        "warn_zh": warn_zh,
        "rows": [r for r in rows if r],
        "params_schema": list(params_schema or []),
        "params_values": dict(params_values or {}),
        "sections": [s for s in sections if s],
        "detail": detail,
        "extra": dict(extra or {}),
    }


# -- params -----------------------------------------------------------------
def _choice_label(choices, value):
    """Resolve a select value to its human label. Two shapes exist in this
    repo and both must work:
        ["cold", "hot"]                          - plain strings
        [{"value": "", "label": "不关注"}, ...]  - value/label dicts
    A value with no match is shown as-is rather than dropped.
    """
    for c in choices or []:
        if isinstance(c, dict):
            if str(c.get("value")) == str(value):
                return c.get("label", value)
        elif str(c) == str(value):
            return c
    return value


def _plain(value) -> str:
    """A schema default as the reader should see it. An empty string is a
    meaningful default in this repo (watch_pkg's 'do not watch anything'), but
    printed bare it reads as a rendering fault - so name it."""
    if value is None:
        return "—"
    if value == "":
        return "(空)"
    return str(value)


def _fmt_param(field: dict, value) -> str:
    if value is None:
        return "&mdash;"
    t = field.get("type")
    if t == "bool":
        return "是" if value else "否"
    if t == "select":
        return esc(_choice_label(field.get("choices"), value))
    if isinstance(value, bool):
        # A bool field whose schema forgot the type: still never print "True".
        return "是" if value else "否"
    return esc(value)


def cn_num(n: int) -> str:
    """1 -> 一. Section numbers are shared by the engine and any script that
    wants to open its own subsection, so the mapping lives in one place."""
    words = "零一二三四五六七八九"
    if n <= 0:
        return str(n)
    if n < 10:
        return words[n]
    if n < 20:
        return "十" + (words[n % 10] if n % 10 else "")
    return words[n // 10] + "十" + (words[n % 10] if n % 10 else "")


def render_params_table(values: dict, schema: list, num=None,
                        title_zh: str = "本次参数") -> str:
    """The parameters this run actually used, straight off the PARAMS schema.

    Iterating the SCHEMA (not the values dict) is what makes the table stable
    across runs: every declared parameter gets a row, in declaration order,
    whether or not this particular run overrode it. Each row says whether the
    value came from the default or was changed for this run - which is the
    question someone reading an archived report actually has.

    `num` is the section number from the caller. Pass None for an unheaded
    table (embedding it inside someone else's section), which is why the
    heading is built here rather than in render_html: a table that numbers
    itself nothing and a renderer that numbers it something else would drift
    apart, and the page would show a gap in the count.
    """
    if not schema:
        return ""
    heading = f"{cn_num(num)}、" if num else ""
    parts = [f'<h2>{esc(heading + title_zh)}',
             '<span>来自脚本声明的 PARAMS,顺序与'
             '前端配置弹窗一致</span></h2>',
             '<table><tr><th>参数</th><th>本次取值</th>'
             '<th>来源</th></tr>']
    for f in schema:
        name = f.get("name")
        if not name:
            continue
        label = f.get("label") or name
        given = name in values
        value = values.get(name, f.get("default"))
        default = f.get("default")
        # Compare on the raw values, but a value that was never passed in is
        # only "the default" because we filled it in - say so plainly.
        if not given:
            origin = "默认"
            origin_note = "本次未传,取声明默认值"
        elif value == default:
            origin = "默认"
            origin_note = "传入了与默认相同的值"
        else:
            origin = "已改"
            origin_note = f"默认 {_plain(default)}"
        parts.append(
            f'<tr><td class="k">{esc(label)}<small>{esc(name)}</small></td>'
            f'<td class="v">{_fmt_param(f, value)}</td>'
            f'<td class="note">{esc(origin)}'
            f'<small>{esc(origin_note)}</small></td></tr>')
    parts.append("</table>")
    return "".join(parts)


# -- detail (the raw report dict) -------------------------------------------
def _detail_html(obj, hide_keys=()) -> str:
    """Recursive key/value render of a report dict.

    Not pretty on purpose: it is the escape hatch, so nothing a script put in
    its report can be missing from the page. `hide_keys` redacts by key name at
    any depth - the wifi scripts hold plaintext credentials in their reports
    and this page is meant to be handed to someone else.
    """
    hide = {k.lower() for k in hide_keys}

    def val(v):
        if isinstance(v, dict):
            if not v:
                return "{}"
            items = "".join(
                f'<tr><td class="k">{esc(k)}</td><td class="v">'
                + ('<span class="redacted">已隐去</span>'
                   if str(k).lower() in hide else val(x))
                + "</td></tr>" for k, x in v.items())
            return f"<table>{items}</table>"
        if isinstance(v, list):
            if not v:
                return "[]"
            return "<ol>" + "".join(f"<li>{val(x)}</li>" for x in v) + "</ol>"
        if isinstance(v, bool):
            return "是" if v else "否"
        return esc(v)

    return val(obj)


def _section_html(sec: dict, num: int) -> str:
    title = f"{cn_num(num)}、{sec.get('title_zh') or sec.get('key') or ''}"
    sub = sec.get("sub_zh")
    head = f'<h2>{esc(title)}'
    if sub:
        head += f"<span>{esc(sub)}</span>"
    head += "</h2>"
    kind = sec.get("kind")
    if kind == "kv":
        body = "".join(f'<tr><td class="k">{esc(k)}</td><td class="v">{esc(v)}</td></tr>'
                       for k, v in sec.get("pairs") or [])
        return head + (f"<table>{body}</table>" if body else
                       '<p class="sub">无</p>')
    if kind == "list":
        items = sec.get("items") or []
        if not items:
            return head + f'<p class="sub">{esc(sec.get("empty_zh") or "无")}</p>'
        return head + "<ul>" + "".join(f"<li>{esc(i)}</li>" for i in items) + "</ul>"
    # table (the default)
    head_cols = "".join(f"<th>{esc(h)}</th>" for h in sec.get("head") or [])
    rows = sec.get("rows") or []
    if not rows:
        return (head + f'<p class="sub">{esc(sec.get("empty_zh") or "本次无数据")}</p>')
    body = "".join(
        "<tr>" + "".join(
            f'<td class="{cls}">{inner}</td>' if cls else f"<td>{inner}</td>"
            for cls, inner in (_cell(c) for c in r)) + "</tr>"
        for r in rows)
    return head + f"<table><tr>{head_cols}</tr>{body}</table>"


# -- the page ---------------------------------------------------------------
def render_html(doc: dict) -> str:
    """Render the document to one self-contained HTML string."""
    rows = doc.get("rows") or []
    level = doc.get("level") if doc.get("level") in LEVELS else "info"
    result = doc.get("result")

    p = ['<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width, initial-scale=1">',
         f"<title>{esc(doc.get('test_name'))} · "
         f"{esc(doc.get('device_id'))}</title>",
         f"<style>{HTML_CSS}</style></head><body>",
         f"<h1>{esc(doc.get('test_name'))}</h1>"]

    sub = (f"设备 <b>{esc(doc.get('device_id'))}</b>"
           f"　·　时间 {esc(doc.get('test_time'))}")
    sv = doc.get("script_version")
    sub += (f"　·　脚本 {esc(doc.get('script'))}"
            + (f" v{esc(sv)}" if sv else ""))
    sub += (f"　·　报告引擎 v{esc(doc.get('engine_version'))}")
    p.append(f'<div class="sub">{sub}</div>')

    if result is not None:
        zh = LEVEL_ZH.get(level, "")
        extra = ""
        if doc.get("count_zh"):
            extra += f'<span class="zh">{esc(doc["count_zh"])}</span>'
        p.append(f'<div class="banner {level}"><b>{esc(result)}</b>'
                 f'<span class="zh">{esc(zh)}</span>{extra}</div>')
    else:
        p.append('<div class="banner info"><b>&mdash;</b>'
                 '<span class="zh">本次运行不做'
                 '通过/不通过的判定</span></div>')

    if doc.get("warn_zh"):
        p.append(f'<div class="warnbar">{esc(doc["warn_zh"])}</div>')

    # One counter for the whole page. Every numbered block draws from it, so a
    # block that decides to skip itself (no rows, no params) cannot leave a gap
    # in the sequence - the reader's sense that a section is missing would be
    # correct and there would be nothing to point at.
    num = 0

    # the result rows
    if rows:
        num += 1
        p.append(f'<h2>{cn_num(num)}、结果总览'
                 '<span>每行结论由它自己'
                 '那组数据决定</span></h2>')
        body = ""
        for r in rows:
            lv = r.get("level") if r.get("level") in LEVELS else "info"
            body += (f'<tr><td class="k">{esc(r.get("label_zh"))}</td>'
                     f'<td><span class="chip {lv}">{esc(LEVEL_ZH.get(lv, lv))}</span></td>'
                     f'<td class="v">{esc(r.get("value"))}</td>'
                     f'<td class="note">{esc(r.get("note_zh"))}</td></tr>')
        p.append('<table><tr><th>指标</th><th>结论</th>'
                 '<th>取值</th><th>说明</th></tr>'
                 f"{body}</table>")

    # the params actually used
    if doc.get("params_schema"):
        num += 1
        p.append(render_params_table(doc.get("params_values") or {},
                                     doc.get("params_schema") or [], num=num))

    # script-supplied sections
    for sec in doc.get("sections") or []:
        num += 1
        p.append(_section_html(sec, num))

    # the raw report, so nothing the script recorded can be missing here
    if doc.get("detail"):
        num += 1
        p.append(f'<details><summary>{cn_num(num)}、完整数据'
                 '<span class="sub">报告文件的全部'
                 '内容，展开查看</span></summary>'
                 f'{_detail_html(doc["detail"], doc.get("hide_keys") or ())}'
                 "</details>")

    foot = (f"脚本 <code>{esc(doc.get('script'))}</code>"
            + (f" v{esc(sv)}" if sv else "")
            + f" · 报告引擎 <code>_pptp_report "
              f"v{esc(doc.get('engine_version'))}</code>"
              f" · schema <code>{esc(doc.get('schema'))}</code>")
    ex = doc.get("extra") or {}
    if ex.get("exit_code") is not None:
        foot += f" · exit <code>{esc(ex['exit_code'])}</code>"
    if ex.get("report_name"):
        foot += (f"<br>本页由 <code>{esc(ex['report_name'])}</code> "
                 "渲染而成，两者同目录"
                 "同前缀。")
    p.append(f'<div class="foot">{foot}</div>')
    p.append("</body></html>")
    return "".join(p)


def write_html_report(json_path: str, doc: dict) -> str:
    """Write the HTML twin of `json_path`. Returns its path.

    Never raises: a report is a courtesy, and a read-only reports/ folder must
    not be able to fail a run that is otherwise fine. Callers print the path
    only when this returns a truthy value.
    """
    try:
        path = html_path_for(json_path)
        atomic_write_text(path, render_html(doc))
        return path
    except Exception as e:
        print(f"[warn] failed to save html report: {e}")
        return ""


def attach(payload: dict, doc: dict) -> dict:
    """Fold the rendered document into a JSON report before it is written.

    With this, render_html(document_of(json.load(report.json))) reproduces the
    archived page exactly - so a consumer that only has the JSON can still
    rebuild the human view. Optional; a script that does not call it loses
    only that property.
    """
    payload["pptp"] = doc
    return payload


def document_of(payload: dict) -> dict:
    """The document embedded by attach(), with the reporter's own identity
    filled in.

    Reserved: no caller in this repo yet - every script renders its page
    directly from the doc it already holds, so nothing needs the round trip.
    Kept because the escape hatch is the whole point of attach(), and a
    JSON-only consumer is a plausible future need."""
    doc = dict(payload.get("pptp") or {})
    doc.setdefault("engine_version", ENGINE_VERSION)
    doc.setdefault("schema", SCHEMA)
    return doc
