"""基础上下文块：内核只提供原料，**组装交给 prompt 插件**。

- **每轮固定**：真源全文 · 最近 K 轮对话 · 能力目录紧凑签名 · 本轮诊断摘要 ·
  词汇边界（渲染器自述）· `DESIGN.md` · 资产清单。
- **按需**：按**本轮触碰的词汇**检索的规范章节（关键词映射表，**不引向量库**）。
- **超限**：分级注入 + **显式声明省略**，绝不静默截断。

两条不能让步的细节：

1. **真源不部分省略**。语言有活绑定与绑定传播，"只送相关子树"会让 LLM 漏掉它要改的地方。
   要省就省别的层；省完还超，就如实报告（"程序大到装不下"是诚实的产品上限）。
2. **省了什么必须写进上下文**。静默截断会让它**自信地改错**——那是搭建回路失效最隐蔽的形式。
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from puppet import spec_dir, vocab

# 注入的最近轮数与 `chat.jsonl` 的保留上限**取同一个数**：
# 保证"文件里有的"与"上下文里有的"一致，不出现"文件里有但 LLM 看不到"的困惑。
HISTORY_K = 12

# 分级省略的顺序（从最可省到最不可省）。真源不在其中。
# 记忆排在 design 之前：设计文档是**意图的唯一载体**，而记忆是可丢的状态。
_DROP_ORDER = ("spec", "assets", "memory", "design", "turns")

DESIGN_HEAD_CHARS = 1200
SPEC_SECTION_LIMIT = 3
SPEC_CHARS_LIMIT = 2600


# ------------------------------------------------------------------ 触碰的词汇

def touched_tokens(engine, diagnostics) -> List[str]:
    """本轮程序"碰到"的词汇：控件 / 属性 / 图标 / 动效 / 内置函数 / 诊断码。

    用它去检索规范章节——按需求注入，而不是把整份规范塞进上下文。
    """
    tokens: set = set()
    source_parts: List[str] = []
    for node in engine.program.nodes.values():
        tokens.add(node.type)
        for name, expr in node.attrs.items():
            tokens.add(name)
            value = getattr(expr, "value", None)
            if name == "icon" and isinstance(value, str):
                tokens.add(value)
            if name == "animate":
                if isinstance(value, str):
                    tokens.add(value)
                elif isinstance(value, list):
                    tokens.update(str(item) for item in value)
        source_parts.append(node.id)
    for diag in diagnostics or []:
        tokens.add(getattr(diag, "code", "") or diag.get("code", ""))
    source = "\n".join(engine.program_lines())
    for name in vocab.BUILTINS:
        if name + "(" in source:
            tokens.add(name)
    return sorted(token for token in tokens if token and len(str(token)) >= 3)


# ------------------------------------------------------------------ 规范片段

_INDEX: Optional[Dict[str, list]] = None
_INDEX_DIR = ""


def _spec_index() -> Dict[str, list]:
    """规范分节索引：`{标题路径: 正文}`，按标题层级拼路径。

    进程内缓存（规范是只读资产，随包分发、运行期不变）。
    """
    global _INDEX, _INDEX_DIR
    directory = spec_dir()
    if _INDEX is not None and _INDEX_DIR == directory:
        return _INDEX
    index: Dict[str, list] = {}
    if os.path.isdir(directory):
        for filename in sorted(os.listdir(directory)):
            if not filename.endswith(".md"):
                continue
            path = os.path.join(directory, filename)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                continue
            stack: List[str] = []          # 各级标题（栈顶最深）
            buffer: List[str] = []
            for line in lines:
                if line.startswith("#"):
                    if buffer:
                        index[_path_of(filename, stack)] = buffer
                        buffer = []
                    level = len(line) - len(line.lstrip("#"))
                    title = line.lstrip("#").strip() or filename
                    stack = stack[:max(level - 1, 0)] + [title]
                    continue
                buffer.append(line)
            if buffer:
                index[_path_of(filename, stack)] = buffer
    _INDEX, _INDEX_DIR = index, directory
    return index


def _path_of(filename: str, stack: List[str]) -> str:
    return " · ".join([filename] + stack) if stack else filename


def spec_sections(tokens: List[str], limit: int = SPEC_SECTION_LIMIT) -> List[dict]:
    """按词汇命中取若干规范章节。命中数为 0 的章节不返回（不注入噪音）。"""
    if not tokens:
        return []
    lowered = [str(token).lower() for token in tokens]
    scored = []
    for title, lines in _spec_index().items():
        text = "\n".join(lines).lower()
        score = sum(1 for token in lowered if token in text)
        if score:
            scored.append((score, title, lines))
    scored.sort(key=lambda item: (-item[0], item[1]))
    out = []
    budget = SPEC_CHARS_LIMIT
    for score, title, lines in scored[:limit]:
        text = "\n".join(lines).strip()
        if not text:
            continue
        if len(text) > budget:
            text = text[:budget] + "\n…（本节被截断）"
        budget -= len(text)
        out.append({"title": title, "text": text, "score": score})
    return out


# ------------------------------------------------------------------ 组装

def build(*, app_dir, engine, catalog: List[dict], diagnostics: List[dict],
          turns: List[dict], rendering: dict, request: str, mode: str,
          budget: Optional[int] = None, stuck: str = "", memory: str = "",
          fusion_brief: str = "") -> dict:
    """产出基础上下文块。`turns` 是**已经截到 K 轮**的历史。"""
    source = "\n".join(engine.program_lines())
    design = app_dir.read_design()
    assets = _assets(app_dir)
    tokens = touched_tokens(engine, diagnostics)
    sections = spec_sections(tokens)

    context = {
        "mode": mode,
        "app_name": app_dir.name,
        "source": source,
        "design": design[:DESIGN_HEAD_CHARS] if len(design) > DESIGN_HEAD_CHARS else design,
        "catalog": catalog,
        "vocabulary": {
            "controls": list(rendering.get("controls") or []),
            "attributes": list(rendering.get("attributes") or []),
            "animations": list(rendering.get("animations") or []),
            "icons": list(rendering.get("icons") or []),
        },
        # 观测布尔（geometry / snapshot / interaction / headless）+ 渲染器自述。
        # **诚实降级必须让 LLM 看见**：它若以为"几何可核对"，就会把布局押在坐标上，
        # 而那条断言根本没人能验证——错误要等到人眼看到才被发现。
        "observation": {
            "geometry": bool(rendering.get("geometry")),
            "snapshot": bool(rendering.get("snapshot")),
            "interaction": bool(rendering.get("interaction")),
            "headless": bool(rendering.get("headless")),
            "notes": str(rendering.get("notes") or ""),
        },
        "diagnostics": diagnostics,
        "turns": turns,
        "assets": assets,
        "memory": memory,
        "fusion_brief": fusion_brief,
        "spec": sections,
        "touched": tokens,
        "request": request,
        "stuck": stuck,
        "omitted": [],
    }
    if budget:
        _fit(context, budget)
    context["size"] = size(context)
    return context


def size(context: dict) -> dict:
    def chars(value) -> int:
        return len(json.dumps(value, ensure_ascii=False, default=str))

    return {
        "source": len(context.get("source") or ""),
        "design": len(context.get("design") or ""),
        "turns": sum(len(turn.get("text") or "") for turn in context.get("turns") or []),
        "total": chars({k: v for k, v in context.items() if k != "size"}),
    }


def _assets(app_dir) -> List[dict]:
    directory = app_dir.assets_dir
    if not os.path.isdir(directory):
        return []
    out = []
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            try:
                out.append({"name": name, "size": os.path.getsize(path)})
            except OSError:
                out.append({"name": name, "size": 0})
    return out


def _fit(context: dict, budget: int) -> None:
    """分级省略。**真源永不部分省略**——省完别的还超，就如实报告。"""
    for layer in _DROP_ORDER:
        if size(context)["total"] <= budget:
            return
        if layer == "spec" and context.get("spec"):
            context["omitted"].append("规范片段 %d 节（按词汇检索所得，本轮到上限被省略）"
                                      % len(context["spec"]))
            context["spec"] = []
        elif layer == "assets" and context.get("assets"):
            context["omitted"].append("assets/ 清单 %d 项" % len(context["assets"]))
            context["assets"] = []
        elif layer == "memory" and context.get("memory"):
            context["omitted"].append("运行期记忆（本轮未注入）")
            context["memory"] = ""
        elif layer == "design" and context.get("design"):
            if len(context["design"]) > 300:
                context["omitted"].append("DESIGN.md 正文（只留前 300 字符）")
                context["design"] = context["design"][:300]
            else:
                context["omitted"].append("DESIGN.md")
                context["design"] = ""
        elif layer == "turns":
            keep = []
            for turn in context.get("turns") or []:
                keep.append(turn)
                if len(keep) >= max(1, len(context["turns"]) // 2):
                    break
            if len(keep) < len(context["turns"]):
                context["omitted"].append("较早的 %d 轮对话"
                                          % (len(context["turns"]) - len(keep)))
                context["turns"] = keep
    if size(context)["total"] > budget:
        context["omitted"].append(
            "**仍超出上限**：真源不做部分省略（只送相关子树会漏掉要改的地方），"
            "故整份保留。请缩小程序，或改用窗口更大的模型。")
