#!/usr/bin/env python3
"""
Serializer for the `var WEEK = {...}` block in index.html.

Emits the compact hand-authored JS style the site uses (unquoted keys, the five
header fields on the option's opening line, `steps` on a single line, groceries
one-per-line) so that the weekly routine's diffs stay small and reviewable and
the file matches the format Taylor maintains by hand.

Also provides splice_week(): a string-aware brace matcher that replaces ONLY the
WEEK block inside a full index.html, leaving all surrounding markup/JS untouched.
"""

import json, re


def _js(s):
    # valid double-quoted JS string literal, keeping unicode literal (– × °F etc.)
    return json.dumps(s if s is not None else "", ensure_ascii=False)


def _render_steps(steps):
    parts = []
    for st in steps or []:
        do = ",".join(_js(x) for x in st.get("do", []))
        parts.append("{part:%s,do:[%s]}" % (_js(st.get("part", "")), do))
    return "[" + ",".join(parts) + "]"


def _grocery(g):
    return "{name:%s, det:%s, cat:%s}" % (
        _js(g.get("name", "")), _js(g.get("det", "")), _js(g.get("cat", "")))


def _render_option(o, last, is_default):
    L = []
    header = "      { " + ", ".join([
        "title:" + _js(o.get("title", "")),
        "sides:" + _js(o.get("sides", "")),
        "protein:" + _js(o.get("protein", "")),
        "time:" + _js(o.get("time", "")),
        "img:" + _js(o.get("img", "")),
    ]) + ","
    L.append(header)
    L.append("        included:" + _js(o.get("included", "")) + ",")
    if o.get("swapNote"):
        L.append("        swapNote:" + _js(o["swapNote"]) + ",")
    L.append("        steps:" + _render_steps(o.get("steps", [])) + ",")
    L.append("        lunchTitle:" + _js(o.get("lunchTitle", "")) + ",")
    L.append("        lunch:" + _js(o.get("lunch", "")) + ",")
    gro = o.get("groceries", [])
    tail = "" if last else ","
    if is_default:
        # featured default: one grocery per line
        L.append("        groceries:[")
        for i, g in enumerate(gro):
            L.append("          " + _grocery(g) + ("," if i < len(gro) - 1 else ""))
        L.append("        ]}" + tail)
    else:
        # swap alternate: groceries inline on one line
        L.append("        groceries:[" + ",".join(_grocery(g) for g in gro) + "]}" + tail)
    return "\n".join(L)


def _render_meal(m, last):
    L = ["    { id:%s, day:%s, polo:%s, options:[" % (
        _js(m.get("id", "")), _js(m.get("day", "")), "true" if m.get("polo") else "false")]
    opts = m.get("options", [])
    for i, o in enumerate(opts):
        L.append(_render_option(o, i == len(opts) - 1, i == 0))
    L.append("    ]}" + ("" if last else ","))
    return "\n".join(L)


def render_week(w):
    """Serialize a WEEK dict to the compact `var WEEK = {...};` JS block (no trailing newline)."""
    L = ["var WEEK = {"]
    L.append("  label: " + _js(w.get("label", "")) + ",")
    L.append("  version: " + _js(w.get("version", "")) + ",")
    L.append("  intro: " + _js(w.get("intro", "")) + ",")
    L.append("  portions: " + _js(w.get("portions", "")) + ",")
    L.append("  meals: [")
    meals = w.get("meals", [])
    for i, m in enumerate(meals):
        L.append(_render_meal(m, i == len(meals) - 1))
    # `staples` = always-on grocery items independent of which swap is chosen (schema
    # field Taylor added). Preserve it; empty is the common case.
    staples = w.get("staples", [])
    if staples:
        L.append("  ],")
        L.append("  staples:[" + ",".join(_grocery(g) for g in staples) + "]")
    else:
        L.append("  ],")
        L.append("  staples:[]   // always-on items independent of swaps (none this week)")
    L.append("};")
    return "\n".join(L)


def _week_span(text):
    """(start, end) indices covering `var WEEK = { ... };` via string-aware brace match."""
    m = re.search(r"var WEEK\s*=\s*", text)
    if not m:
        raise RuntimeError("could not find 'var WEEK =' in index.html")
    i = text.index("{", m.end())
    depth, j, in_str, esc, q = 0, i, False, False, ""
    while j < len(text):
        c = text[j]
        if in_str:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == q: in_str = False
        else:
            if c in "\"'": in_str, q = True, c
            elif c == "{": depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
        j += 1
    end = j + 1
    while end < len(text) and text[end] in " \t":
        end += 1
    if end < len(text) and text[end] == ";":
        end += 1
    return m.start(), end


def splice_week(index_html, new_week_js):
    """Return index_html with its WEEK block replaced by new_week_js (everything else intact)."""
    s, e = _week_span(index_html)
    return index_html[:s] + new_week_js + index_html[e:]


def current_label(index_html):
    s, e = _week_span(index_html)
    m = re.search(r'"?label"?\s*:\s*"([^"]*)"', index_html[s:e])
    return m.group(1) if m else ""
