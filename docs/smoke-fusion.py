"""融合（plan 一等化）冒烟：缺省 plan / 意志字段 / 审计停下 / chat 三段式 / CLI 全链。

要验证的是 grilling 共识的每一条：
- plan 一等、双来源：机制缺省 plan 能跑；LLM 的 plan 字段（renames/caps/window_title/
  intent）逐一生效；审计对两种来源同一套体检；
- 冲突只会停下并列清单（id 撞名 / 能力重名 / 多窗口 / 路径包含 / B 在运行）；
- chat 三段式：意向块 → 摘要注入 → 方案块 → 确认卡 → 执行后意图清除；
- CLI：dry-run 打印 plan，--plan 注入方案，--yes 执行。

用法：python docs/smoke-fusion.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"

A_PROGRAM = [
    'add #root window #win title="主程序" w=900 h=640',
    'add #win col #content pad=16 gap=12 flex=1',
    'data #todos = [{text: "买牛奶"}] of {text: str}',
    'add #content list #rows source=#todos template=#tpl',
    'add #root template #tpl as t',
    'add #tpl text #row text=t.text',
]
B_PROGRAM = [
    'add #root window #win title="副程序"',
    'add #win col #content pad=12',
    'data #notes = [{text: "笔记"}] of {text: str}',
    'add #content text #head text="副程序的标题"',
    'add #content list #items source=#notes template=#btpl',
    'add #root template #btpl as t',
    'add #btpl text #row text=t.text',
]


def main() -> int:
    from puppethub.appdir import AppDir, create_app
    from puppethub.fusion import audit_plan, default_plan, fuse
    from puppethub.session import Session

    work = Path(tempfile.mkdtemp(prefix="puppethub-fuse-"))
    failures = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print("  [%s] %s%s" % ("ok" if ok else "失败", label,
                               ("  ← " + detail) if detail and not ok else ""))
        if not ok:
            failures.append(label)

    def make(parent: Path, name: str, program: list, caps: str = "",
             requires: str = "bs4") -> Path:
        app = create_app(parent, name, name)
        app.write_source(program)
        if caps:
            app.write_capabilities(
                '"""能力。"""\nfrom puppet import capability\n\n'
                'REQUIRES: list[str] = ["%s"]\n\n\n' % requires
                + "\n\n".join(
                    '@capability(returns="str")\ndef %s() -> str:\n'
                    '    """%s 的能力。"""\n    return "ok"\n' % (n, n)
                    for n in caps.split(",") if n) + "\n")
        return app.root

    print("1) 缺省 plan（机制来源）：确定性算法执行全链")
    a_root = make(work, "alpha", list(A_PROGRAM), caps="a_tool")
    b_root = make(work, "beta", list(B_PROGRAM), caps="b_note,b_other", requires="bs4")
    a, b = AppDir(a_root), AppDir(b_root)
    plan = default_plan(a, b)
    check("缺省 plan 的形态", plan["window_title"] == "a"
          and plan["caps"] == {"b_note": "keep", "b_other": "keep"}
          and "b_" in str(plan["intent"]), str(plan)[:180])
    result = fuse(a, b, plan)
    check("融合完成", result["ok"] and result["stage"] == "done", str(result)[:240])
    source = "\n".join(a.read_source())
    check("B 地址带 b_ 前缀 + 引用跟着走",
          "#b_content" in source and "source=#b_notes" in source)
    check("窗口嫁接（无第二窗口）",
          source.count(" window ") == 1 and "add #win col #b_content" in source)
    check("能力并入 + REQUIRES 并集（bs4 进 A 的声明）",
          "b_note" in a.read_capabilities() and "bs4" in a.read_capabilities())
    check("B 归档不删除", list(work.glob("beta.fused-into-alpha.*")) != [])
    session = Session(AppDir(a_root))
    diags = session.start()
    check("融合后的 A 独立装载零错误", not [d for d in diags if d.level == "error"],
          str([(d.code, d.message) for d in diags if d.level == "error"]))

    print("\n2) 意志字段：caps drop/rename、window_title、intent")
    a2_root = make(work, "a2", list(A_PROGRAM), caps="b_note")   # 撞名逼出 caps 动作
    b2_root = make(work, "b2", list(B_PROGRAM), caps="b_note,b_other")
    plan2 = {"renames": {}, "caps": {"b_note": "drop", "b_other": "rename:merged_note"},
             "window_title": "融合后的家", "intent": "B 只留另一个能力，笔记重复"}
    result = fuse(AppDir(a2_root), AppDir(b2_root), plan2)
    check("带意志字段的融合完成", result["ok"], str(result)[:240])
    a2_caps = AppDir(a2_root).read_capabilities()
    check("drop 生效（B 的 b_note 摘除，A 自己的 b_note 保留——只剩一份）",
          a2_caps.count("def b_note") == 1)
    check("rename 生效（def merged_note）", "def merged_note" in a2_caps)
    check("window_title 自定义生效",
          'title="融合后的家"' in "\n".join(AppDir(a2_root).read_source()))
    check("intent 进决策流水", "B 只留另一个能力" in AppDir(a2_root).read_design())

    print("\n3) 审计只会停下并列清单：id 撞名 / 能力重名 / 多窗口 / 路径包含 / B 在跑")
    a3_root = make(work, "a3", list(A_PROGRAM) + ['add #content text #b_head text="占位"'], "")
    b3_root = make(work, "b3", list(B_PROGRAM), "")
    result = fuse(AppDir(a3_root), AppDir(b3_root),
                  default_plan(AppDir(a3_root), AppDir(b3_root)))
    check("id 撞名停下且列清单", not result["ok"] and any("撞名" in e for e in result["errors"]),
          str(result)[:240])
    check("停下时 B 未归档", b3_root.is_dir())

    a4_root = make(work, "a4", list(A_PROGRAM), caps="b_note")   # 只有能力冲突，无 id 冲突
    b4_root = make(work, "b4", list(B_PROGRAM), caps="b_note,b_other")
    result = fuse(AppDir(a4_root), AppDir(b4_root),
                  default_plan(AppDir(a4_root), AppDir(b4_root)))
    check("能力重名停下并指路 caps", not result["ok"]
          and any("caps" in e for e in result["errors"]), str(result)[:240])

    b5_root = make(work, "b5", list(B_PROGRAM) + ['add #root window #win2 title="二号"'], "")
    result = fuse(AppDir(a3_root), AppDir(b5_root),
                  default_plan(AppDir(a3_root), AppDir(b5_root)))
    check("多窗口停下", not result["ok"] and any("窗口" in e for e in result["errors"]),
          str(result)[:240])

    sub_app = make(a3_root, "inner-app", list(A_PROGRAM), "")
    result = fuse(AppDir(a3_root), AppDir(sub_app),
                  default_plan(AppDir(a3_root), AppDir(sub_app)))
    check("路径包含关系停下（B 在 A 里面）", not result["ok"]
          and any("包含关系" in e for e in result["errors"]), str(result)[:240])

    print("\n5) CLI 全链：dry-run 打印 plan → --plan 注入 → --yes 执行")
    a6_root = make(work, "a6", list(A_PROGRAM), caps="a_tool")
    b6_root = make(work, "b6", list(B_PROGRAM), caps="b_note,b_other")
    plan_file = work / "plan.json"
    plan_file.write_text(json.dumps({
        "renames": {}, "caps": {"b_note": "drop", "b_other": "keep"},
        "window_title": "b", "intent": "保留 B 的标题与另一个能力"}), encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    dry = subprocess.run([sys.executable, "-m", "puppethub", "fuse",
                          str(a6_root), str(b6_root)],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", env=env)
    check("dry-run 退出码 0 且不写真源",
          dry.returncode == 0 and "b_note" not in AppDir(a6_root).read_capabilities(),
          (dry.stdout + dry.stderr)[-240:])
    check("dry-run 打印了 plan 全文", "window_title" in dry.stdout and "intent" in dry.stdout)
    done = subprocess.run([sys.executable, "-m", "puppethub", "fuse",
                           str(a6_root), str(b6_root), "--plan", str(plan_file), "--yes"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)
    check("--plan + --yes 执行成功", done.returncode == 0, (done.stdout + done.stderr)[-300:])
    check("caps 动作生效（b_note drop / b_other keep）",
          "def b_note" not in AppDir(a6_root).read_capabilities()
          and "def b_other" in AppDir(a6_root).read_capabilities())
    check("标题用了 B 的（window_title=b）",
          'title="副程序"' in "\n".join(AppDir(a6_root).read_source()))

    print("\n4) chat 三段式：意向 → 摘要注入 → 方案 → 确认卡 → 执行")
    work2 = Path(tempfile.mkdtemp(prefix="puppethub-fuse-chat-"))
    plugins = work2 / "plugins"
    plugins.mkdir()
    shutil.copyfile(FIXTURES / "fake_provider.py", plugins / "fake_provider.py")
    replies = [
        '把旁边的 beta 合进来。\n\n```fuse\n{"b": "beta"}\n```\n',
        '```fuse\n{"b": "beta", "window_title": "b", "intent": "beta 的笔记能力有用"}\n```\n',
    ]
    (work2 / "replies.json").write_text(json.dumps(replies, ensure_ascii=False),
                                        encoding="utf-8")
    os.environ["PUPPETHUB_PLUGINS"] = str(plugins)
    b7_root = make(work2, "beta", list(B_PROGRAM), caps="b_note,b_other")
    a7_root = make(work2, "alpha", list(A_PROGRAM), caps="a_tool")
    (a7_root / "puppethub.toml").write_text(
        'llm_provider = "fake"\nstorage = "file"\n\n[plugins.fake]\nscript = "%s"\n'
        % (work2 / "replies.json").as_posix(), encoding="utf-8")

    session = Session(AppDir(a7_root))
    session.start()
    codes = lambda: [entry["code"] for entry in session.log]
    r1 = session.chat_turn("把旁边的 beta 合进来")
    check("意向块被接住", "FUSION_INTENT" in codes(), str(codes()[-4:]))
    check("融合对象只存解析后的路径", session.fusion_target
          and session.fusion_target.replace("\\", "/").endswith("beta"),
          str(session.fusion_target))
    session.chat_turn("出方案")
    brief_blocks = [m["content"] for m in session.chat.last_messages
                    if "融合对象的结构摘要" in m.get("content", "")]
    check("摘要进了方案轮上下文", bool(brief_blocks) and "地址清单" in brief_blocks[0],
          (brief_blocks[0][:160] if brief_blocks else "无"))
    check("方案过体检进入确认卡", session.chat.pending is not None
          and session.chat.pending["kind"] == "fuse", str(session.chat.pending)[:180])
    check("确认卡里是 plan 全文（JSON 含能力取舍）",
          "b_note" in str(session.chat.pending["body"]))
    session.approve_pending()
    check("确认后执行且意图清除", "FUSION_APPLIED" in codes()
          and session.fusion_target is None, str(codes()[-6:]))
    check("B 的内容真进了 A", "#b_content" in session.program_text())
    check("标题归属 b 生效（window_title=b = 用 B 的标题值）",
          'title="副程序"' in session.program_text())

    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(work2, ignore_errors=True)
    print("\n%s（%d 项失败）" % ("全部通过" if not failures else "有失败", len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
