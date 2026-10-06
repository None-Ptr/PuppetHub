"""Skill 机制冒烟：一个 .md 文件 = 一份按需注入的领域知识。

要验证的是**边界**，不是"能跑"：
- 内置配方加载（4 份，中文名）；
- 触发词命中：请求原文 / 程序源里出现触发词才注入，没命中**一个都不注入**；
- app 级目录放文件即生效，且**覆盖**同名内置（先到先得对用户有利：可定制）；
- 坏文件可见不静默：front-matter 缺冒号、空正文、未知键（WARNING）、重名（WARNING）；
- 预算：命中超预算截断，且在上下文里显式声明；
- 与 context.build 的集成：`skills` 段进上下文、prompt 里出现"技能 ·"块。

用法：python docs/smoke-skills.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 语言仓与 Hub 仓同级（`D:\OI\Projects\OpenPuppet\{Puppet,PuppetHub}`）。
# 不写死盘符：D 盘是主线，E 盘只是冗余副本——写死会在换机/换盘时静默导入旧代码。
sys.path.insert(0, str(ROOT.parent / "Puppet"))

from puppet import Diagnostic, ERROR                      # noqa: E402
from puppethub import skills as skill_mod                 # noqa: E402
from puppethub.appdir import create_app                   # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("✓" if ok else "✗", name,
                         ("—— " + str(detail)[:160]) if (detail and not ok) else ""))
    if not ok:
        FAILED.append(name)


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="puppethub-smoke-skills-"))
    app = create_app(work, "demo", "技能冒烟")
    logs = []

    print("\n1) 内置配方加载")
    builtin, diags = skill_mod.load(str(app.root))
    names = sorted(s.name for s in builtin)
    # 断言"内置目录里的文件数与加载数一致"，而不是写死份数：
    # 加一份内置技能不该让冒烟变红（那是断言的问题，不是技能的问题）。
    builtin_dir = Path(skill_mod.__file__).with_name("builtin_skills")
    expected = sorted(p.stem for p in builtin_dir.glob("*.md"))
    check("内置配方与目录一一对应（%d 份）" % len(expected),
          names == expected, names)
    check("全部标记为内置", all(s.builtin for s in builtin))
    check("零诊断（内置文件是好的）", not diags, [d.message for d in diags])

    print("\n2) 触发词命中：请求原文 / 程序源")
    # **常驻技能**（always: true，如"语言速查"）每轮都进且排最前——它不参与
    # 触发词匹配，断言里要先摘掉它，否则测的是常驻而不是命中。
    always_names = {s.name for s in builtin if getattr(s, "always", False)}

    def auto(hits):
        return [h for h in hits if h["name"] not in always_names]

    check("有常驻技能（语言速查每轮必进）",
          {"语言速查"} <= always_names, sorted(always_names))
    hits = skill_mod.select(builtin, "帮我把这条记录删掉", "")
    check("命中项里常驻技能排最前", hits and hits[0]["name"] in always_names,
          [h["name"] for h in hits])
    check("请求说'删除'命中删改数据", [h["name"] for h in auto(hits)] == ["删改数据"],
          [h["name"] for h in hits])
    hits = skill_mod.select(builtin, "帮我算一下总价", "")
    check("'总价'命中数字与文本", [h["name"] for h in auto(hits)] == ["数字与文本"],
          [h["name"] for h in hits])
    hits = skill_mod.select(builtin, "", "on #d click: remove_where #t as r where r.x == 1")
    check("程序源出现 remove_where 也命中", [h["name"] for h in auto(hits)] == ["删改数据"],
          [h["name"] for h in hits])
    hits = skill_mod.select(builtin, "今天天气怎么样", "")
    check("不相关请求 → 只有常驻技能注入（零噪音）",
          auto(hits) == [], [h["name"] for h in hits])
    # 第三检索源：诊断文本——诊断码只出现在诊断里，不给这一源，诊断类技能永不触发。
    hits = skill_mod.select(builtin, "改一下", "", extra="SYNTAX 该行不符合语法")
    check("诊断文本作第三源 → 命中诊断速查",
          "诊断速查" in [h["name"] for h in hits], [h["name"] for h in hits])
    hits = skill_mod.select(builtin, "把这条记录删掉", "")
    body = [h["text"] for h in hits if h["name"] == "删改数据"][0]
    check("注入的是正文（含完整写法）", "remove_where" in body, body[:120])

    print("\n3) app 级目录：放文件即生效 + 同名覆盖内置")
    hub_skills = app.root / ".puppethub" / "skills"
    hub_skills.mkdir(parents=True, exist_ok=True)
    (hub_skills / "删改数据.md").write_text(
        "---\nname: 删改数据\nwhen: 删除 移除\n---\n项目专属的删除纪律：必须先确认。", encoding="utf-8")
    (hub_skills / "坏文件.md").write_text(
        "---\nname 坏的\n---\n正文", encoding="utf-8")       # 冒号缺失 → 诊断
    (hub_skills / "空正文.md").write_text("---\nname: 空\n---\n", encoding="utf-8")
    (hub_skills / "未知键.md").write_text(
        "---\nname: 未知键\npriority: 高\n---\n正文", encoding="utf-8")  # WARNING
    loaded, diags = skill_mod.load(str(app.root))
    custom = [s for s in loaded if s.name == "删改数据"]
    check("app 级覆盖内置（仅一份且是项目版）",
          len(custom) == 1 and not custom[0].builtin and "项目专属" in custom[0].body)
    codes = [(d.code, d.level) for d in diags]
    check("坏 front-matter 可见（ERROR）", ("SKILL_FRONTMATTER", "error") in codes, codes)
    check("空正文可见（ERROR）", ("SKILL_EMPTY", "error") in codes, codes)
    check("未知键是 WARNING（宽容但不静默）", ("SKILL_FRONTMATTER", "warning") in codes, codes)
    check("坏文件不拖累好文件", any(s.name == "未知键" for s in loaded))

    print("\n4) 预算与截断")
    huge = skill_mod.Skill(name="巨大", triggers=["删除"], body="长" * 3000, source="x")
    picked = skill_mod.select([huge], "删除一条", "")
    check("超预算截断且声明", len(picked) == 1 and picked[0]["text"].endswith("…（本技能被截断）"))

    print("\n5) 与 context.build 集成")
    from puppethub.context import build
    from puppet import Engine
    ctx = build(app_dir=app, engine=Engine(),
                catalog=[], diagnostics=[], turns=[], rendering={},
                request="帮我把这条记录删除", mode="执行",
                skills=skill_mod.load(str(app.root))[0])
    names = [s["name"] for s in ctx.get("skills") or []]
    check("context['skills'] 命中项目版", "删改数据" in names, names)

    print("\n6) prompt 组装出现技能块")
    from puppethub.builtin_plugins.default_prompt import DefaultPrompt
    out = DefaultPrompt(None).rewrite({"vocabulary": {}, "observation": {},
                                       "style": "", "turns": [], "skills": ctx["skills"],
                                       "skill_index": skill_mod.index_block(builtin)})
    # 技能块走 _request（本轮的固定层，user 消息），不在 system。
    request_text = (out["messages"] or [{}])[-1].get("content", "")
    check("prompt 含【技能 · 删改数据】", "【技能 · 删改数据】" in request_text,
          request_text[:200])
    sysmsg = out["system"]
    check("技能索引常驻（【可用技能】+ about 一句话）",
          "【可用技能】" in sysmsg and "删/改一条数据的行内配方" in sysmsg, sysmsg[:300])

    print("\n7) LLM 自取通道（skill 块）")
    from puppethub.chat import parse_response
    parsed, pdiags = parse_response("我来取一下写法。\n\n```skill 调用能力\n```\n")
    skill_blocks = [b for b in parsed if b.kind == "skill"]
    check("skill 块被解析出名字", len(skill_blocks) == 1 and skill_blocks[0].path == "调用能力",
          [(b.kind, getattr(b, "path", None)) for b in parsed])
    parsed2, _ = parse_response("```skill 数字与文本```\n")   # 同行闭合也能取
    check("同行闭合形式同样可取", any(b.path == "数字与文本" for b in parsed2
                                    if b.kind == "skill"))
    check("find 按名取技能", skill_mod.find(builtin, "调用能力") is not None
          and skill_mod.find(builtin, "不存在的") is None)
    check("index 给出取用指引", "```skill 名字```" in skill_mod.index_block(builtin))

    print("\n8) 技能组：共同纪律常驻 + 专项细节按需（2026-10-06 改）")
    # 改动前是「整组连坐」：命中任一份 → 全组进。实测一轮请求里技能占 78%，
    # 而程序全文只占 1.5%——那与"按需注入"相反。改后：总纲（synopsis）常驻 +
    # 只有命中的成员进，其余点名可自取。
    a_group = [s for s in builtin if s.group]
    check("内置技能里有成组的", bool(a_group), len(a_group))
    leads = [s for s in a_group if s.synopsis]
    check("每个组都有总纲（synopsis）", len(leads) == len({s.group for s in a_group}),
          [(s.group, s.name, bool(s.synopsis)) for s in a_group])
    check("总纲明显短于整份正文（否则省不下来）",
          all(len(s.synopsis) < len(s.body) for s in leads),
          [(s.name, len(s.synopsis), len(s.body)) for s in leads])
    # 命中组内任一份：总纲 + 那一份进，**同组没命中的不进**
    hits = skill_mod.select(builtin, "这个图标选得不好", "add #a button #b text=x")
    names = [h["name"] for h in hits]
    check("命中组内一份时，总纲在场（共同纪律没丢）",
          any(s.synopsis and s.name in names for s in leads), names)
    groups_touched = {s.group for s in a_group
                      if any(h["name"] == s.name for h in hits)}
    not_injected = [s.name for s in a_group
                    if s.group in groups_touched and s.name not in names
                    and not s.synopsis]
    check("同组未命中的成员**不再连坐**（这是省 token 的关键）",
          all(h["text"] != next(x.body for x in a_group if x.name == n)
              for h in hits for n in not_injected), (not_injected, names))
    # 未进的那几份必须**点名**（第一原则：不静默丢弃），且给出自取指引
    pointer = [h for h in hits if h["name"] in {s.group for s in a_group}]
    check("未注入的同组知识被点名 + 给出 ```skill 自取``` 指引",
          bool(pointer) and all("```skill" in h["text"] for h in pointer),
          [h["text"][:80] for h in pointer])
    # 块标量：synopsis 是多行散文，解析器必须支持且不误报
    _, fdiags = skill_mod.load(str(ROOT), log=None)
    check("块标量 synopsis 不产生假 ERROR",
          not [d for d in fdiags if d.level == "ERROR" and "冒号" in d.message],
          [d.message[:60] for d in fdiags if d.level == "ERROR"][:3])

    shutil.rmtree(work, ignore_errors=True)
    print("\n%s" % ("全部通过" if not FAILED else "失败：%s" % "、".join(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
