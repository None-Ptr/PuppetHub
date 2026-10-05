"""基础上下文块：内核只提供原料，**组装交给 prompt 插件**。

- **每轮固定**：真源全文 │ 最近 K 轮对话 │ 能力目录紧凑签名 │ 本轮诊断摘要 │
  词汇边界（渲染器自述）│ `DESIGN.md` │ 资产清单。
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
import re
from typing import Dict, List, Optional

from puppet import spec_dir, vocab

# 注入的最近轮数与 `chat.jsonl` 的保留上限**取同一个数**：
# 保证"文件里有的"与"上下文里有的"一致，不出现"文件里有但 LLM 看不到"的困惑。
HISTORY_K = 12

# 分级省略的顺序（从最可省到最不可省）。真源不在其中。
# 记忆排在 design 之前：设计文档是**意图的唯一载体**，而记忆是可丢的状态。
# skills 比 spec 后省：触发词是显式命中的（请求/源里真出现了），比按词汇
# 计分的规范章节更准——都是可再生知识，但省的时候先舍"可能不相关"的。
_DROP_ORDER = ("spec", "skills", "assets", "memory", "design", "turns")

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


def capability_docs(catalog: List[dict], source: str, request: str) -> Dict[str, str]:
    """触碰的能力 → **docstring 全文**（能力文档的"按需"层）。

    签名 + 说明首行常驻（"有什么"必须每轮可见）；正文（参数语义 / 返回 / 示例）
    只在触碰时注入：程序正在调用它（`name(` 出现在源里），或请求文本点名它。
    触发前 LLM 调错参数只能靠 `CALL_CONTRACT` 诊断试错——按需注入让它第一次就读对。
    只收**多行**文档：单行说明已在常驻清单里，注入全文没有增量。
    """
    req = str(request or "")
    out: Dict[str, str] = {}
    for entry in catalog or []:
        name = str(entry.get("name") or "")
        if not name:
            continue
        # 触碰 = 源里作为**词**出现（`call send_mail with …` 名后是空格，
        # `name + "("` 只盖得住表达式里的调用——所以用词边界正则），
        # 或请求文本点名了它。
        touched = (re.search(r"\b%s\b" % re.escape(name), source) is not None
                   or re.search(r"\b%s\b" % re.escape(name), req) is not None)
        if not touched:
            continue
        doc = (entry.get("doc") or "").strip()
        if doc and len(doc.splitlines()) > 1:
            out[name] = doc
    return out


def request_tokens(request: str) -> List[str]:
    """本轮**请求文本**的 2/3 字滑窗——任务词的检索源。

    配方库（`spec/07-recipes.md`）按"用户想做什么"命中：程序里还没有
    `remove_where` 时，用户说"删除这条记录"，"删除"这个 2-gram 就能命中
    配方章节——**第一次就写对**的引导靠它，而不只是写错后靠诊断回灌纠正。
    2-gram 噪音不小，但检索是计分排序 + 节数/字符双上限，噪音顶多稀释、
    不会挤掉高分章节。
    """
    text = re.sub(r"\s+", " ", str(request or "")).strip()
    if not text:
        return []
    grams: set = set()
    for size in (2, 3):
        for i in range(len(text) - size + 1):
            grams.add(text[i:i + size])
    return sorted(grams)


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
    return " │ ".join([filename] + stack) if stack else filename


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
          fusion_brief: str = "", skills: list = ()) -> dict:
    """产出基础上下文块。`turns` 是**已经截到 K 轮**的历史。"""
    source = "\n".join(engine.program_lines())
    design = app_dir.read_design()
    assets = _assets(app_dir)
    # 检索源 = 程序触碰的词汇 + 请求文本的任务词（后者让"第一次就写对"成为可能：
    # 程序里还没有 remove_where 时，"删除这条记录"就能命中配方章节）。
    tokens = touched_tokens(engine, diagnostics) + request_tokens(request)
    sections = spec_sections(tokens)
    # Skill（.md 文件）：显式触发词命中才注入——机制见 skills.select。
    from . import skills as _skills
    skill_hits = _skills.select(list(skills or []), request, source)

    context = {
        "mode": mode,
        "app_name": app_dir.name,
        "source": source,
        # 触碰能力的 docstring 全文（"有什么"常驻、"怎么用对"按需——见 capability_docs）。
        "capability_docs": capability_docs(catalog, source, request),
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
        "skills": skill_hits,
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
        elif layer == "skills" and context.get("skills"):
            context["omitted"].append("技能 %d 份（%s；本轮到上限被省略）"
                                      % (len(context["skills"]),
                                         "、".join(s["name"] for s in context["skills"])))
            context["skills"] = []
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
            # 砍掉**较早**的一半，保留最近的一半：要省的是"很久以前说过什么"，
            # 最近几轮恰恰是理解本轮请求最需要的。反过来保留最旧的等于
            # 把上下文里最该留下的部分丢掉（且与下面那句 omitted 的措辞自相矛盾）。
            turns = context.get("turns") or []
            keep = turns[-max(1, len(turns) // 2):] if turns else []
            if len(keep) < len(turns):
                context["omitted"].append("较早的 %d 轮对话" % (len(turns) - len(keep)))
                context["turns"] = keep
    if size(context)["total"] > budget:
        context["omitted"].append(
            "**仍超出上限**：真源不做部分省略（只送相关子树会漏掉要改的地方），"
            "故整份保留。请缩小程序，或改用窗口更大的模型。")
