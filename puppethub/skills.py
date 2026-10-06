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

# 单轮注入的**不成组**技能数与字符预算（与 spec 片段同量级的克制）。
SELECT_LIMIT = 2
SELECT_CHARS = 1600

# **技能组**：一组"同属一个判断体系"的知识（美学、感知、能力…）。
#
# 机制（2026-10-06 实测后改）：命组内任一份 → 进**命中的那些** + **一份组内总纲**，
# **其余成员不再连坐**。总纲由组里 `order` 最小的那份承担（= 共同纪律，每轮都用得上）。
#
# 为什么改：原机制是"整组连坐"——请求里出现「按钮」一词就命中 `图标与文案`，
# 于是整个美学组 5 份 ≈10k 字全进。实测一轮请求里**技能占 78%**、程序全文只占 1.5%，
# 这与"按需注入"完全相反。一致性改由**总纲**保住，专项细节按需 + 可自取。
#
# 依据：docs/design-skills.md §注入预算；实测见 docs/review-agent-gap.md 附注。
GROUP_LIMIT = 5      # 一组最多进几份（含总纲）
# 组共享字符预算。**只有命中的成员吃这份预算**，故比旧值宽松些也安全。
# 注：**旧的单份 1600 字预算本身就是 bug**——`视觉规范.md` 有 2156 字，
# 在旧机制下必然被截断成"…（本技能被截断）"，等于只给了半份知识。
GROUP_CHARS = 13000

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


@dataclass
class Skill:
    name: str
    triggers: List[str] = field(default_factory=list)
    body: str = ""
    source: str = ""
    builtin: bool = False
    about: str = ""          # 一句话简介：进常驻的技能索引（LLM 据此决定取哪份）
    group: str = ""          # 技能组名（空 = 不成组，按老规矩单份注入）
    order: int = 0           # 组内补位次序（小的先；0 = 未指定，按文件名兜底）
    always: bool = False     # **常驻**：不经触发词，每轮都注入（"不知道就写不出东西"的基础知识）
    synopsis: str = ""       # **组内总纲**：组里 order 最小的那份承担（见 GROUP_SYNOPSIS）


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
    group = ""
    order = 0
    always = False
    synopsis = ""
    body = text.strip()
    match = _FRONT.match(text)
    if match:
        raw_lines = match.group(1).splitlines()
        index = 0
        while index < len(raw_lines):
            line = raw_lines[index]
            index += 1
            # **块标量**（`key: |`）：本字段是多行散文，续行到下一个 `键:` 为止。
            # 不支持它的话，总纲这种长句会被逐行当成"缺少冒号"的坏行——
            # 那是一条**假的 ERROR**，比不支持这个特性更糟（它会污染技能表诊断）。
            if ":" in line:
                probe = line.partition(":")[0].strip().lower()
                if line.partition(":")[2].strip() in ("|", "|-", ">", ">-"):
                    block = [line.partition(":")[2].strip()[1:].strip()]
                    while index < len(raw_lines) and ":" not in raw_lines[index]:
                        block.append(raw_lines[index].strip())
                        index += 1
                    if probe == "synopsis":
                        synopsis = "\n".join(x for x in block if x)
                    # 其他键暂不支持块标量：明说，不静默吞掉
                    else:
                        diags.append(Diagnostic(
                            "SKILL_FRONTMATTER", WARNING,
                            "技能 %s 的 %s 用了块标量（|），本版本只支持 synopsis 用它，"
                            "该字段被忽略" % (filename, probe)))
                    continue
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
            elif key == "group":
                group = value
            elif key == "order":
                try:
                    order = int(value)
                except ValueError:
                    diags.append(Diagnostic(
                        "SKILL_FRONTMATTER", WARNING,
                        "技能 %s 的 order 不是整数：%r，按 0 处理" % (filename, value)))
            elif key == "always":
                always = value.lower() in ("true", "1", "yes", "on")
            elif key == "synopsis":
                synopsis = value
            elif key in ("name", "when", "about", "group", "order", "always",
                         "synopsis"):
                pass
            else:
                diags.append(Diagnostic(
                    "SKILL_FRONTMATTER", WARNING,
                    "技能 %s 有未知 front-matter 键 %r"
                    "（可用：name / when / about / group / order / always / synopsis）"
                    % (filename, key)))
        body = text[match.end():].strip()
    if not body:
        diags.append(Diagnostic("SKILL_EMPTY", ERROR,
                                "技能 %s 正文为空，不予加载" % filename))
        return None
    if not triggers:
        triggers = [name]
    return Skill(name=name, triggers=triggers, body=body, source=path,
                 builtin=builtin, about=about, group=group, order=order,
                 always=always, synopsis=synopsis)


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


def select(skills: List[Skill], request: str, source: str, extra: str = "",
           limit: int = SELECT_LIMIT, chars: int = SELECT_CHARS) -> List[dict]:
    """按触发词命中本轮任务，产出注入条目。没命中一个都不注入（常驻技能例外）。

    检索源 = 请求原文 ∪ 程序源 ∪ `extra`（调用方补的第三源，**放诊断文本**——
    诊断码只出现在诊断里，不在请求也不在源码里，不给这一源，`诊断速查` 这类
    技能就永远不会被触发。见 `context.build` 的调用处）。

    三档注入，**优先级从高到低**：
    1. **常驻技能**（`always: true`）：不经触发词，**每轮都进**且排最前。
       用于"不知道就根本写不出东西"的知识（语言速查）——它不该靠触发词运气。
    2. **成组技能**（`group:` 非空）：组内任一份命中 → 整组一起进（含未命中成员）。
    3. **不成组的技能**：按老规矩，`limit` 份、`chars` 预算。

    预算不够时**不静默丢弃**：末尾附一行"还有 N 份未注入"，让 LLM 知道有边界
    （第一原则：丢东西必须看得见）。
    """
    if not skills:
        return []
    haystack = (str(request or "") + "\n" + str(source or "")
                + "\n" + str(extra or "")).lower()
    hits = []
    for skill in skills:
        if skill.always or any(t and t.lower() in haystack for t in skill.triggers):
            hits.append(skill)

    out: List[dict] = []
    used: set = set()

    # ---- 第零档：常驻技能（always）----
    # **排最前、不受触发词约束、不参与后续的组/散装分配**。
    # 它用的是自己的字预算（`always` 通常只有 1 份语言速查，量可控）。
    for skill in sorted([s for s in skills if s.always], key=lambda s: (s.order, s.name)):
        out.append({"name": skill.name, "text": skill.body, "source": skill.source})
        used.add(skill.name)

    # ---- 第一档：成组技能 ----
    # **命组内的任一份 → 进「命中的那些」+ 一份组内总纲**，其余成员**不再连坐**。
    #
    # 为什么不再整组连坐（2026-10-06 实测后改）：整组进是"连坐制"——请求里出现
    # 「按钮」一个词就命中 `图标与文案`，于是**整个美学组 5 份、约 10k 字符全进**，
    # 而实测这一轮只占请求的 **78%**，程序全文才 1.5%。这与"按需注入"的初衷相反。
    #
    # 一致性怎么保住：组里 **order 最小的那份承担"总纲"**（`GROUP_SYNOPSIS`）——
    # 它讲的是**全组共同的纪律**（值怎么定、间距怎么收），几百字，正是每轮都要用
    # 的那部分；各专项细节留给命中的成员，漏了的可 ```skill 名字``` 自取。
    # 一句话：**共同纪律常驻，专项细节按需。**
    groups: dict = {}
    for skill in hits:
        if skill.group and not skill.always:
            groups.setdefault(skill.group, []).append(skill)
    for gname in sorted(groups, key=lambda g: -len(groups[g])):
        matched = sorted(groups[gname], key=lambda s: (s.order, s.name))
        # 组内总纲 = 全组 order 最小的那份（无论本轮是否命中：它是"共同纪律"，常驻）
        all_members = [s for s in skills
                       if s.group == gname and not s.always]
        lead = sorted(all_members, key=lambda s: (s.order, s.name))[0] \
            if all_members else None
        injected_names = set()
        budget = GROUP_CHARS
        # **次序：命中的专项在前，总纲垫后**。总纲是"共同纪律"（背景），
        # 专项才是本轮真正要查的东西——把总纲排第一会让"深色主题"这类请求
        # 先读到通用配色纪律，专项反而排在后面（实测踩到过）。
        #
        # 注意：总纲自己**也常被命中**（它的触发词覆盖面广，如"深色"），
        # 此时它不特殊对待——但**注入的仍是 synopsis**（省 token 的关键不能丢），
        # 且排位按"它是不是本轮的专项"决定：命中的其他成员在前，它垫后。
        lead_synopsis = ""
        if lead is not None:
            lead_synopsis = lead.synopsis or lead.body
        others = [s for s in matched if s.name != (lead.name if lead else None)]
        queue = others[:]
        if lead is not None:
            # 总纲排最后：它要么是纯背景（未命中），要么是宽覆盖的常识（命中但不够专项）
            queue.append(lead)
        for skill in queue:
            is_lead = (lead is not None and skill.name == lead.name)
            if skill.name in injected_names:
                continue
            if len(injected_names) >= GROUP_LIMIT:
                break
            # **总纲只给 synopsis**（几百字共同纪律），无论它是否被命中——
            # 它是"背景"不是"本轮要查的正文"，给了整份就等于没省。
            text = lead_synopsis if is_lead else skill.body
            if len(text) > budget:
                if budget < 200:
                    break
                text = text[:budget] + "\n…（本技能篇幅超预算，已截断）"
            budget -= len(text)
            out.append({"name": skill.name, "text": text, "source": skill.source})
            used.add(skill.name)
            injected_names.add(skill.name)
        # **未进的那几份必须点名列出**（第一原则：不静默丢弃）——它们可自取。
        missing = [s for s in all_members if s.name not in injected_names]
        if missing:
            out.append({"name": gname, "source": "",
                        "text": "（技能组「%s」还有 %d 份**相关**知识本轮未注入：%s。"
                                "它们与本轮触碰的主题同属一组，**可能相关**——"
                                "若你觉得需要，用 ```skill 名字``` 自取。）"
                                % (gname, len(missing),
                                   "、".join(s.name for s in missing))})

    # ---- 第二档：不成组技能（老规矩，且不重复常驻/成组已进的）----
    loose = [s for s in hits if not s.group and not s.always and s.name not in used]
    budget = chars
    for skill in loose[:limit]:
        text = skill.body
        if len(text) > budget:
            text = text[:budget] + "\n…（本技能被截断）"
        budget -= len(text)
        out.append({"name": skill.name, "text": text, "source": skill.source})
    total_hits = len([s for s in hits if not s.always])
    injected = len([h for h in out if h["name"] not in {s.name for s in skills if s.always}])
    if total_hits > injected:
        out.append({"name": "（未注入）", "source": "",
                    "text": "（本轮另有 %d 份相关知识命中但未注入——受单轮份数/字数上限限制。"
                            "需要时用 ```skill 名字``` 取。）" % (total_hits - injected)})
    return out


def index_block(skills: List[Skill]) -> str:
    """技能索引：**每轮常驻**的一段清单（名字 + 一句话简介）。

    这是触发词匹配之外的**第二条获取通道**（根治"LLM 不知道有什么可取"）：
    索引常驻让 LLM 知道知识面，需要时输出 ` ```skill 名字``` ` 块自取全文——
    它比任何触发词都聪明，因为判断"这个任务需要什么知识"的正是 LLM 自己。
    """
    if not skills:
        return ""
    # **常驻技能不进索引**：它已经每轮注入了，列在"可取"清单里只会造成"要取吗"的困惑。
    available = [s for s in skills if not s.always]
    if not available:
        return ""
    lines = ["【可用技能】需要某种写法时，单独输出一个 ```skill 名字``` 块取它"
             "（全文将从下一轮起持续注入，取一次即可）："]
    for skill in available:
        about = skill.about or "、".join(skill.triggers[:3])
        lines.append("- %s：%s" % (skill.name, about))
    return "\n".join(lines)


def find(skills: List[Skill], name: str) -> Optional[Skill]:
    """按名字找技能（`skill` 块的自取通道）。找不到返回 None——调用方必须可见报错。"""
    for skill in skills or []:
        if skill.name == (name or "").strip():
            return skill
    return None
