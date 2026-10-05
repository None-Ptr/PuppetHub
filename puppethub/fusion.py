"""融合：**plan 一等数据结构，双来源，单一审计**（design-v3-fusion.md 的实现）。

分工：**LLM 或驱动者出/改方案，机制做体检并执行，人按确认**。三条来源无关性：

- 机制能生成**缺省 plan**（确定性算法：`b_` 前缀、能力全保留、标题归 A）——
  `--no-llm` 实例与 CLI 的驱动者照样能融合；
- LLM 的方案只是**覆盖缺省 plan 的字段**（renames / caps / window_title / intent），
  不是从零发明——幻觉表面积最小；
- 审计（`audit_plan`）对任何来源的 plan 做同一套体检，**冲突只停下列清单**。

三个不可妥协的实现选择：

1. **改名在 IR 层做，不做文本替换**。`#cafe` 是合法标识符也是合法颜色，
   文本替换救不了这种歧义；而 IR 的地址是结构化的，逐处改名 + 重打印天然合法。
2. **B 的窗口子树嫁接进 A 的窗口**（窗口行退场 + 直接子行改父）——双窗口没有
   定义良好的渲染语义；嫁接后 B 的界面内容原样活在 A 里。
3. **先干跑后落盘**：合并文本先过 parse → apply → validate，有 error 就停下——
   绝不允许"融合写到一半发现程序不合法"。

plan 的字段边界（grilling 共识）：意志可覆盖 `renames` / `caps` / `window_title` /
`intent` 四项；**第一版无 `drops`**——B 归档保证不丢内容，"不想要"用融合后的
普通 `del` 命令批（守卫现成）；REQUIRES 并集、快照、归档、记忆不合并由机制定死。
"""

from __future__ import annotations

import ast
import datetime as _dt
import json
import re
from pathlib import Path

from puppet.ir import ROOT, apply_stmt, new_program, validate
from puppet.lang import parse_program
from puppet.serialize import program_lines

from .appdir import AppDir

PREFIX = "b_"                      # B 的地址前缀：`#win` → `#b_win`（标识符无连字符）


# ------------------------------------------------------------------ 解析与摘要

def parse_side(app: AppDir) -> tuple:
    """app 真源 → (Program, 诊断)。失败（语法错）由调用方可见地停下。"""
    program = new_program()
    diags: list = []
    stmts, parse_diags = parse_program(app.read_source())
    diags += parse_diags
    for stmt in stmts:
        apply_stmt(program, stmt, diags)
    diags += validate(program)
    return program, diags


def _addresses(program) -> set:
    return set(program.nodes) | set(program.data)


def brief(b: AppDir) -> dict:
    """B 的**结构摘要**（清单级）：注入 LLM 上下文的 `fusion_brief` 的内容。

    刻意**不含属性值全文**——融合方案的决策依据（撞名表、能力清单、数据源、
    标题值、契约）全部是清单级信息；属性值对出方案没有用，只会烧预算。
    """
    program, diags = parse_side(b)
    caps_text = b.read_capabilities()
    nodes = []
    for node in program.nodes.values():
        if node.id == ROOT:
            continue
        entry = {"id": node.id, "type": node.type, "parent": node.parent,
                 "attrs": sorted(node.attrs)}
        if node.type == "window":
            title = node.attrs.get("title")
            if title is not None and getattr(title, "value", None) is not None:
                entry["title"] = str(title.value)
        nodes.append(entry)
    contract = _contract_section(b.read_design())
    return {
        "b_path": str(b.root),
        "name": b.name,
        "nodes": nodes,
        "data": sorted(program.data),
        "caps": _cap_infos(caps_text),
        "contract": {key: contract.get(key, "") for key in ("依赖", "非目标", "已知限制")},
        "window_count": sum(1 for node in program.nodes.values() if node.type == "window"),
        "diags": [d.to_dict() for d in diags],
    }


def brief_text(b: AppDir) -> str:
    """摘要的文本形态（进 LLM 上下文）。B 出问题（被删/被改坏）也**如实陈述**。"""
    try:
        info = brief(b)
    except Exception as ex:  # noqa: BLE001 - 摘要失败必须可见，融合意图不会被静默吞掉
        return "（B 摘要生成失败：%s: %s——请检查该目录是否还是合法 app）" % (type(ex).__name__, ex)
    if info["diags"]:
        return ("（B 当前有未通过静态校验的问题，先修 B 再融合：%s）"
                % json.dumps(info["diags"], ensure_ascii=False)[:400])
    lines = ["- 地址清单（id │ 类型 │ 父 │ 属性名%s）" % (" │ title" if info["window_count"] else "")]
    for node in info["nodes"]:
        line = "  │ #%-14s %-10s parent=#%s attrs=%s" % (
            node["id"], node["type"], node["parent"], ",".join(node["attrs"]))
        if node.get("title") is not None:
            line += "  title=%r" % node["title"]
        lines.append(line)
    lines.append("- 数据源：%s" % ("、".join("#" + d for d in info["data"]) or "（无）"))
    lines.append("- 能力：")
    for cap in info["caps"]:
        lines.append("  │ %s  # %s" % (cap["name"], cap["doc"]))
    lines.append("- 契约：依赖〔%s〕非目标〔%s〕已知限制〔%s〕"
                 % (info["contract"]["依赖"], info["contract"]["非目标"],
                    info["contract"]["已知限制"]))
    return "\n".join(lines)


def _cap_infos(text: str) -> list:
    """能力签名清单：`def 名字` + docstring 首行（经 ast，不靠正则碰运气）。"""
    infos = []
    try:
        tree = ast.parse(text)
    except SyntaxError as ex:
        return [{"name": "？", "doc": "capabilities.py 解析失败（%s）" % ex}]
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = (ast.get_docstring(node) or "").splitlines()
            infos.append({"name": node.name, "doc": doc[0].strip() if doc else ""})
    return infos


# ------------------------------------------------------------------ plan 与审计

def default_plan(a: AppDir, b: AppDir) -> dict:
    """机制生成的**缺省 plan**（确定性算法）：`b_` 前缀、能力全保留、标题归 A。"""
    return {"renames": {},
            "caps": {name: "keep" for name in _cap_names(b.read_capabilities())},
            "window_title": "a",
            "intent": "按缺省方案融合（b_ 前缀隔离、能力全保留、标题归 A）"}


def _contract_section(design: str) -> dict:
    """DESIGN.md 头部契约的约束性字段（依赖 / 非目标 / 已知限制）。"""
    fields: dict = {}
    inside = False
    for line in design.splitlines():
        if line.strip().startswith("## "):
            inside = line.strip() == "## 头部契约"
            continue
        if not inside:
            continue
        match = re.match(r"-\s*\*\*(.+?)\*\*[:：]\s*(.*)", line.strip())
        if match:
            fields[match.group(1)] = match.group(2).strip()
    return fields


def _cap_names(text: str) -> set:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    return {node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _requires(text: str) -> list:
    match = re.search(r"REQUIRES:\s*list\[str\]\s*=\s*\[([^\]]*)\]", text)
    if not match:
        return []
    return [item.strip().strip("\"'") for item in match.group(1).split(",") if item.strip()]


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def audit_plan(a: AppDir, b: AppDir, plan: dict) -> dict:
    """对**任何来源**的 plan 做同一套体检。冲突/不合法只停下并列清单。

    拒绝清单（grilling 共识）：路径合法 │ B 未在运行 │ 改名**级联合法**（最终映射
    重查撞名）│ caps 动作合法 │ 干跑（在 `fuse` 里，落在确认之后执行之前）。
    品味字段（intent / window_title）机制不打分——那是确认卡上人拍板的事。
    """
    a_program, a_diags = parse_side(a)
    b_program, b_diags = parse_side(b)
    errors: list = []
    # 路径合法性：不能是自己，也不能互为祖先/子目录（归档改名会自噬）
    a_root = a.root
    b_root = b.root
    if b_root == a_root or a_root in b_root.parents or b_root in a_root.parents:
        errors.append("B 的目录（%s）与 A（%s）相同或存在包含关系——融合对象必须是"
                      "另一个独立 app 目录" % (b_root, a_root))
    renames = {str(k): str(v) for k, v in (plan.get("renames") or {}).items()}

    # 1) 改名的目标必须是合法标识符
    for old, new in renames.items():
        if not _IDENT.match(new):
            errors.append("改名目标 %r 不是合法标识符（字母/下划线开头，仅字母数字下划线）" % new)

    # 2) 地址映射：rename 覆盖 `b_` 缺省；`root` 是结构锚点，绝不进映射
    a_addrs = _addresses(a_program)
    b_addrs = _addresses(b_program) - {ROOT}
    for old in renames:
        if old not in b_addrs:
            errors.append("改名针对的地址 #%s 在 B 中不存在" % old)
    mapping = {addr: renames.get(addr, PREFIX + addr) for addr in b_addrs}

    # 3) 以**最终映射**重查撞名（防"用改名把一个地址撞到另一个地址上"）
    id_conflicts = sorted({mapping[addr] for addr in b_addrs} & a_addrs)
    if id_conflicts:
        errors.append("融合后与 A 撞名的地址：%s（用 renames 解，或先在 B 里重命名）"
                      % "、".join("#" + c for c in id_conflicts))

    # 4) caps 动作合法：名字必须存在于 B；动作 ∈ {keep, drop, rename:<new>}
    a_caps, b_caps = a.read_capabilities(), b.read_capabilities()
    a_cap_names, b_cap_names = _cap_names(a_caps), _cap_names(b_caps)
    plan_caps = {str(k): str(v) for k, v in (plan.get("caps") or {}).items()}
    for name, action in plan_caps.items():
        if name not in b_cap_names:
            errors.append("caps 里引用的能力 %s 在 B 中不存在" % name)
            continue
        if action == "keep":
            continue
        if action == "drop":
            continue
        if action.startswith("rename:"):
            new = action.split(":", 1)[1].strip()
            if not _IDENT.match(new):
                errors.append("能力 %s 的改名目标 %r 不是合法标识符" % (name, new))
            elif new in a_cap_names or new in b_cap_names:
                errors.append("能力 %s 改名为 %s 会撞名（A 或 B 里已有）" % (name, new))
        else:
            errors.append("caps 里 %s 的动作 %r 不认识（可用：keep / drop / rename:<新名>）"
                          % (name, action))
    cap_conflicts = sorted(
        {name for name in b_cap_names
         if plan_caps.get(name, "keep") == "keep"} & a_cap_names)
    if cap_conflicts:
        errors.append("能力重名（B 保留的这些与 A 撞名）：%s——用 caps 的 rename:<新名> 或 drop 解"
                      % "、".join(cap_conflicts))

    # 5) 窗口：A 必须有、B 恰好一个
    a_window = next((node.id for node in a_program.nodes.values()
                     if node.type == "window"), None)
    b_windows = [node.id for node in b_program.nodes.values() if node.type == "window"]
    if not a_window:
        errors.append("A 没有窗口节点：融合没有可嫁接的根布局")
    if len(b_windows) != 1:
        errors.append("B 有 %d 个窗口节点（需要恰好 1 个）：多窗口没有定义良好的融合语义"
                      % len(b_windows))

    # 6) B 未在运行（hub 账本 + 端口）：在跑的 app 不可作为合并源
    running = _hub_running(b)
    if running:
        errors.append("B 正在运行（hub 编排，pid %s）：先 `puppethub hub <父目录> down` 再融合"
                      % running)

    return {"ok": not errors, "errors": errors,
            "id_conflicts": id_conflicts, "cap_conflicts": cap_conflicts,
            "a_window": a_window, "b_window": (b_windows or [None])[0],
            "mapping": mapping,
            "deps": {"a": _contract_section(a.read_design()).get("依赖", ""),
                     "b": _contract_section(b.read_design()).get("依赖", "")},
            "a_program": a_program, "b_program": b_program}


def _hub_running(b: AppDir):
    """B 若被 hub 编排且在跑 → 返回 pid。账本在 B 的父目录 `.puppethub-hub/`。"""
    state = b.root.parent / ".puppethub-hub" / "hub.json"
    if not state.is_file():
        return None
    try:
        entries = json.loads(state.read_text(encoding="utf-8")).get("apps") or {}
    except (json.JSONDecodeError, OSError):
        return None
    entry = entries.get(b.name)
    if entry and _port_alive(entry.get("port")):
        return entry.get("pid")
    return None


def _port_alive(port) -> bool:
    import socket
    if not port:
        return False
    probe = socket.socket()
    probe.settimeout(1.0)
    try:
        probe.connect(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        probe.close()


# ------------------------------------------------------------------ 执行

def _rename_expr(expr, mapping: dict) -> None:
    if expr is None:
        return
    from puppet.lang import collect_refs
    try:
        refs = list(collect_refs(expr))
    except Exception:  # noqa: BLE001 - 漏改会被干跑抓住
        return
    for ref in refs:
        ref.addr = mapping.get(ref.addr, ref.addr)


def rename_program(program, mapping: dict, func_mapping: dict | None = None) -> None:
    """IR 就地改名。覆盖面 = `serialize.program_lines` 会读的一切地址 + 能力调用名。"""
    func_mapping = func_mapping or {}
    old_nodes = program.nodes
    program.nodes = {}
    for key, node in old_nodes.items():
        node.id = mapping.get(node.id, node.id)
        node.parent = mapping.get(node.parent, node.parent) if node.parent else node.parent
        node.children = [mapping.get(c, c) for c in node.children]
        for expr in node.attrs.values():
            _rename_expr(expr, mapping)
        program.nodes[node.id] = node
    old_data = program.data
    program.data = {}
    for key, ds in old_data.items():
        ds.id = mapping.get(ds.id, ds.id)
        _rename_expr(ds.initial, mapping)
        program.data[ds.id] = ds
    for handler in program.handlers:
        handler.target = mapping.get(handler.target, handler.target)
        _rename_expr(handler.when, mapping)
        for action in handler.actions:
            action.target = mapping.get(action.target, action.target)
            action.into = mapping.get(action.into, action.into)
            action.func = func_mapping.get(action.func, action.func)
            _rename_expr(action.item, mapping)
            _rename_expr(action.where, mapping)
            for expr in action.attrs.values():
                _rename_expr(expr, mapping)
            for expr in action.params.values():
                _rename_expr(expr, mapping)
    for listen in program.listens:
        listen.target = mapping.get(listen.target, listen.target)
    for call in program.calls:
        call.into = mapping.get(call.into, call.into)
        call.func = func_mapping.get(call.func, call.func)
        for expr in call.params.values():
            _rename_expr(expr, mapping)
    for probe in program.probes:
        probe.target = mapping.get(probe.target, probe.target)


def _graft_lines(b_lines: list, b_window: str, a_window: str) -> list:
    """窗口嫁接的**窄口径**文本变换：只动两类已知行——

    - B 的窗口行（`add #root window #<b_window> …`）退场（A 的窗口就是根布局）；
    - B 窗口的**直接子行**（`add #<b_window> …`）改挂到 A 的窗口；
    更深的行引用的是各自的父，不受影响。窗口 id 是审计确认过的具体值，
    不是模式匹配——这是"知道自己在改什么"的变换，不是正则碰运气。
    """
    out = []
    for line in b_lines:
        if line.startswith("add #root window #%s " % b_window) \
                or line.strip() == "add #root window #%s" % b_window:
            continue
        if line.startswith("add #%s " % b_window):
            line = "add #%s %s" % (a_window, line[len("add #%s " % b_window):])
        out.append(line)
    return out


def _apply_caps_actions(b_caps: str, plan_caps: dict) -> str:
    """对 B 的能力文件执行 caps 动作：drop = 摘除函数块；rename = 改 `def` 名。

    行级摘除的边界：`def <名>(` 行向上吞相邻的 `@` 装饰器行，向下到下一个
    顶层语句（`@` / `def` / `class` / `REQUIRES` / 注释外的顶格行）或文件尾。
    """
    if not plan_caps:
        return b_caps
    lines = b_caps.splitlines(keepends=True)
    drop_names = {n for n, act in plan_caps.items() if act == "drop"}
    rename_map = {n: act.split(":", 1)[1].strip()
                  for n, act in plan_caps.items() if act.startswith("rename:")}
    out: list = []
    skipping = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("def "):
            name = re.match(r"def (\w+)\(", stripped)
            name = name.group(1) if name else ""
            if name in drop_names:
                skipping = True          # 从 def 行起到下一个顶层语句，整块摘除
                continue
            if name in rename_map:
                line = line.replace("def %s(" % name, "def %s(" % rename_map[name], 1)
                skipping = False
                out.append(line)
                continue
            skipping = False
            out.append(line)
            continue
        if skipping:
            if stripped.startswith("@") or (stripped and not line[0].isspace()):
                skipping = stripped.startswith("@")   # 连续装饰器随块摘除
                if not skipping:
                    out.append(line)
            continue                                  # 块内（含缩进体）照摘
        out.append(line)
    return "".join(out)


def _merge_capabilities(a_caps: str, b_caps: str, b_name: str) -> str:
    """能力文件合并。REQUIRES 取并集——不合并的话后声明的会覆盖前声明，
    A 声明的第三方依赖就丢了（部署机缺包，运行期才炸）。"""
    requires = sorted(set(_requires(a_caps)) | set(_requires(b_caps)))
    block = "\n\n# —— 以下来自 %s 的融合 ——\n\n" % b_name
    merged = a_caps.rstrip("\n") + block + b_caps.strip("\n") + "\n"
    if requires:
        union = "REQUIRES: list[str] = [%s]" % ", ".join('"%s"' % r for r in requires)
        merged = re.sub(r"REQUIRES:\s*list\[str\]\s*=\s*\[[^\]]*\]",
                        union, merged, count=1)
        # B 段里那份 REQUIRES 声明去掉（声明只留一份，避免后者覆盖前者）
        merged = re.sub(r"(# —— 以下来自[\s\S]*?)REQUIRES:\s*list\[str\]\s*=\s*\[[^\]]*\]",
                        r"\1", merged, count=1)
    return merged


def _dry_run(merged: list) -> list:
    """合并文本过一遍 parse → apply → validate（零状态副作用）。"""
    program = new_program()
    diags: list = []
    stmts, parse_diags = parse_program(list(merged))
    diags += parse_diags
    for stmt in stmts:
        apply_stmt(program, stmt, diags)
    diags += validate(program)
    return [d for d in diags if d.level == "error"]


def append_fusion_section(a: AppDir, b: AppDir, plan: dict) -> None:
    """DESIGN.md 追加融合段：B 的头部契约随档案走，A 里留对照（append-only）。"""
    match = re.search(r"(## 头部契约[\s\S]*?)(?=\n## |\Z)", b.read_design())
    section = match.group(1).rstrip() if match else "（B 无头部契约）"
    a.append_decision("融合归档：%s（原契约附下）" % b.name,
                      "内容已并入本程序；B 目录已改名归档", "全程序")
    a.append_decision("—— %s 的头部契约（融合归档） ——" % b.name,
                      section.replace("\n", " ⏎ ")[:400], "意图档")
    if str(plan.get("intent") or "").strip():
        a.append_decision("融合意图", str(plan["intent"]).strip()[:300], "意图档")


def fuse(a: AppDir, b: AppDir, plan: dict, origin: str = "system") -> dict:
    """按 plan 执行融合。**确认式系统动作**：兜底快照 + 整份写回真源文件。

    `a` 必须是 `AppDir`——本函数**只写文件，不 reload**；运行中的实例由
    `session.fuse` 在成功后显式重载（这里曾出过"文件新、引擎旧"的静默分叉，
    根因就是两条分支并存让调用方以为 reload 有人管）。B 归档 = 改名（档案也是
    记忆的一种），绝不删除。"""
    plan = dict(plan or {})
    report = audit_plan(a, b, plan)
    if not report["ok"]:
        return {"ok": False, "stage": "audit",
                "errors": report["errors"], "report": report}

    b_program = report["b_program"]
    b_window = report["b_window"]
    mapping = {addr: target for addr, target in report["mapping"].items()
               if addr in b_program.nodes or addr in b_program.data}

    # 能力动作：B 的 caps 文本先摘除/改名；程序里的能力调用名跟着改名走
    plan_caps = {str(k): str(v) for k, v in (plan.get("caps") or {}).items()}
    func_mapping = {old: act.split(":", 1)[1].strip()
                    for old, act in plan_caps.items() if act.startswith("rename:")}
    b_caps_text = _apply_caps_actions(b.read_capabilities(), plan_caps)

    rename_program(b_program, mapping, func_mapping)
    b_lines = program_lines(b_program)
    merged = a.read_source() + [""] + _graft_lines(b_lines, mapping[b_window],
                                                   report["a_window"])

    # 标题归属：a = 不动；b = 用 B 的 title 值；自定义 = 直接用给定字符串
    title = plan.get("window_title", "a")
    if title == "b":
        node = b_program.nodes.get(mapping[b_window])
        title_expr = node.attrs.get("title") if node else None
        value = getattr(title_expr, "value", None)
        if value is not None:
            merged.append('set #%s title=%s' % (report["a_window"], _quote(value)))
    elif title != "a":
        merged.append("set #%s title=%s" % (report["a_window"], _quote(title)))

    errors = _dry_run(merged)
    if errors:
        return {"ok": False, "stage": "dry-run",
                "errors": ["合并后的程序没有通过静态校验（未写入任何东西）"],
                "report": {**report, "diags": [d.to_dict() for d in errors]}}

    a.push_snapshot("rebuild", "融合 %s 前的兜底存档（不参与淘汰）" % b.name, origin)
    a.write_capabilities(_merge_capabilities(a.read_capabilities(), b_caps_text, b.name))
    a.write_source(merged, origin=origin)   # 只写文件；运行中实例的 reload 归 session.fuse
    append_fusion_section(a, b, plan)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = b.root.parent / ("%s.fused-into-%s.%s" % (b.name, a.name, stamp))
    b.root.rename(archive)
    return {"ok": True, "stage": "done", "report": report,
            "merged_lines": len(merged), "archive": str(archive)}


def _quote(value) -> str:
    return '"%s"' % str(value).replace("\\", "\\\\").replace('"', '\\"')
