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
sys.path.insert(0, r"e:\Projects\Puppet")

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
    check("五份内置配方", names == ["列表与模板", "删改数据", "数字与文本", "读用户输入", "调用能力"],
          names)
    check("全部标记为内置", all(s.builtin for s in builtin))
    check("零诊断（内置文件是好的）", not diags, [d.message for d in diags])

    print("\n2) 触发词命中：请求原文 / 程序源")
    hits = skill_mod.select(builtin, "帮我把这条记录删掉", "")
    check("请求说'删除'命中删改数据", [h["name"] for h in hits] == ["删改数据"],
          [h["name"] for h in hits])
    hits = skill_mod.select(builtin, "帮我算一下总价", "")
    check("'总价'命中数字与文本", [h["name"] for h in hits] == ["数字与文本"],
          [h["name"] for h in hits])
    hits = skill_mod.select(builtin, "", "on #d click: remove_where #t as r where r.x == 1")
    check("程序源出现 remove_where 也命中", [h["name"] for h in hits] == ["删改数据"],
          [h["name"] for h in hits])
    hits = skill_mod.select(builtin, "今天天气怎么样", "")
    check("不相关请求 → 一个都不注入", hits == [], hits)
    body = skill_mod.select(builtin, "把这条记录删掉", "")[0]["text"]
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

    shutil.rmtree(work, ignore_errors=True)
    print("\n%s" % ("全部通过" if not FAILED else "失败：%s" % "、".join(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
