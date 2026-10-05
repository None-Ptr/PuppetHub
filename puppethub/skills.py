"""Skill：**一个 `.md` 文件 = 一份按需注入的领域知识**。

三个目录，放文件即生效（与插件目录同一模式）：

- 内置：`puppethub/builtin_skills/`（随包分发——语言用法配方住在这里）
- app 级：`<app>/.puppethub/skills/`（**随 app 走**——融合/拷贝不掉件）
- 用户级：`~/.puppethub/skills/`

文件格式（front-matter 可省，省了就用文件名当名字与触发词）：

    ---
    name: 删改数据
    when: 删除 修改 remove_where update_where
    ---
    正文 = 命中后注入 prompt 的内容。

边界与哲学：

- skill 是**纯文本知识**，不是代码——它不执行、拿不到引擎（与插件同一条铁律），
  最坏只能让 LLM 表现变差，而那始终可见。
- 触发是**显式触发词**的子串匹配（请求原文 ∪ 程序源），不是向量检索——不引依赖、
  结果可解释；没命中一个都不注入（不产生噪音）。
- 加载失败**可见不静默**：坏 front-matter、重名，一律产生诊断并跳过该文件
  （隔离但不静默；与插件 `load_plugin_file` 同一条纪律）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from puppet import Diagnostic, ERROR, WARNING

# 单轮注入的技能数与字符预算（与 spec 片段同量级的克制）。
SELECT_LIMIT = 2
SELECT_CHARS = 1600

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


@dataclass
class Skill:
    name: str
    triggers: List[str] = field(default_factory=list)
    body: str = ""
    source: str = ""
    builtin: bool = False
    about: str = ""          # 一句话简介：进常驻的技能索引（LLM 据此决定取哪份）


def skill_dirs(app_root: str | os.PathLike) -> List[Tuple[str, bool]]:
    """待扫描目录（顺序即覆盖优先级：app 级 > 用户级 > 内置——重名时先到先得）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return [
        (os.path.join(str(app_root or ""), ".puppethub", "skills"), False),
        (os.path.join(os.path.expanduser("~"), ".puppethub", "skills"), False),
        (os.path.join(here, "builtin_skills"), True),
    ]


def _parse(text: str, filename: str, path: str, builtin: bool,
           diags: List[Diagnostic]) -> Optional[Skill]:
    name = os.path.splitext(filename)[0]
    triggers: List[str] = []
    about = ""
    body = text.strip()
    match = _FRONT.match(text)
    if match:
        for line in match.group(1).splitlines():
            if ":" not in line:
                diags.append(Diagnostic(
                    "SKILL_FRONTMATTER", ERROR,
                    "技能 %s 的 front-matter 行缺少冒号：%r，该行被忽略" % (filename, line)))
                continue
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "name" and value:
                name = value
            elif key == "when":
                triggers = [t for t in re.split(r"[,\s，、]+", value) if t]
            elif key == "about":
                about = value
            elif key in ("name", "when", "about"):
                pass
            else:
                diags.append(Diagnostic(
                    "SKILL_FRONTMATTER", WARNING,
                    "技能 %s 有未知 front-matter 键 %r（可用：name / when）" % (filename, key)))
        body = text[match.end():].strip()
    if not body:
        diags.append(Diagnostic("SKILL_EMPTY", ERROR,
                                "技能 %s 正文为空，不予加载" % filename))
        return None
    if not triggers:
        triggers = [name]
    return Skill(name=name, triggers=triggers, body=body, source=path,
                 builtin=builtin, about=about)


def load(app_root: str | os.PathLike,
         log: Optional[Callable[[str, str], None]] = None
         ) -> Tuple[List[Skill], List[Diagnostic]]:
    """加载三个目录的全部技能。重名先到先得（app 级 > 用户级 > 内置）且**可见**。"""
    out: List[Skill] = []
    diags: List[Diagnostic] = []
    seen: set = set()
    for directory, builtin in skill_dirs(app_root):
        if not os.path.isdir(directory):
            continue
        for filename in sorted(os.listdir(directory)):
            if not filename.endswith(".md") or filename.startswith("_"):
                continue
            path = os.path.join(directory, filename)
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError as ex:
                diags.append(Diagnostic("SKILL_READ", ERROR,
                                        "技能文件读不了（%s）：%s" % (path, ex)))
                continue
            before = len(diags)
            skill = _parse(text, filename, path, builtin, diags)
            if skill is None:
                continue
            if skill.name in seen:
                diags.append(Diagnostic(
                    "SKILL_DUP", WARNING,
                    "技能 %s 重名（先到者生效，后者来自 %s）" % (skill.name, path)))
                continue
            seen.add(skill.name)
            out.append(skill)
            if len(diags) > before and log is not None:
                for diag in diags[before:]:
                    log(diag.level, diag.message)
    return out, diags


def select(skills: List[Skill], request: str, source: str,
           limit: int = SELECT_LIMIT, chars: int = SELECT_CHARS) -> List[dict]:
    """按触发词命中本轮任务，产出注入条目。没命中一个都不注入。"""
    if not skills:
        return []
    haystack = (str(request or "") + "\n" + str(source or "")).lower()
    hits = []
    for skill in skills:
        if any(t and t.lower() in haystack for t in skill.triggers):
            hits.append(skill)
    out: List[dict] = []
    budget = chars
    for skill in hits[:limit]:
        text = skill.body
        if len(text) > budget:
            text = text[:budget] + "\n…（本技能被截断）"
        budget -= len(text)
        out.append({"name": skill.name, "text": text, "source": skill.source})
    return out


def index_block(skills: List[Skill]) -> str:
    """技能索引：**每轮常驻**的一段清单（名字 + 一句话简介）。

    这是触发词匹配之外的**第二条获取通道**（根治"LLM 不知道有什么可取"）：
    索引常驻让 LLM 知道知识面，需要时输出 ` ```skill 名字``` ` 块自取全文——
    它比任何触发词都聪明，因为判断"这个任务需要什么知识"的正是 LLM 自己。
    """
    if not skills:
        return ""
    lines = ["【可用技能】需要某种写法时，单独输出一个 ```skill 名字``` 块取它"
             "（全文将从下一轮起持续注入，取一次即可）："]
    for skill in skills:
        about = skill.about or "、".join(skill.triggers[:3])
        lines.append("- %s：%s" % (skill.name, about))
    return "\n".join(lines)


def find(skills: List[Skill], name: str) -> Optional[Skill]:
    """按名字找技能（`skill` 块的自取通道）。找不到返回 None——调用方必须可见报错。"""
    for skill in skills or []:
        if skill.name == (name or "").strip():
            return skill
    return None
