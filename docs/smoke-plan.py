"""规划层冒烟：把 `goal` 展开成**由机制核验**的步骤序列。

依据 `docs/review-agent-gap.md` §2.4（plan 此前只存在于 fusion）。验的是**语义**：

1. **完成判据由机制求值，不是 LLM 自报**（这是本设计的命门）
2. **无 `done:` 的步骤不判完成**（诚实：既不假装完成，也不假装失败）
3. **未达成时说清"当前值是多少"**，不是干巴巴一句"没做到"
4. **判据用不了 → 可见告警**，不退化成"算完成"
5. **当前该做哪一步由机制指认**（第一条未达成）
6. **边界**：步数超限 `PLAN_REJECTED` / 非法判据 `PLAN_INVALID` / `drop` 撤销留痕
7. **plan 不增预算**（只决定做什么）

用法：python docs/smoke-plan.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "Puppet"))

from puppethub import plan as P                        # noqa: E402
from puppethub.appdir import create_app                # noqa: E402
from puppethub.session import Session                  # noqa: E402
from puppethub.chat import parse_response             # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("✓" if ok else "✗", name,
                         ("—— " + str(detail)[:200]) if (detail and not ok) else ""))
    if not ok:
        FAILED.append(name)


ATTRS = {
    "#arch.visible": True,
    "#list.count": 5,
    "#stock.value": 3,
    "#regress.value": False,
}


def main() -> int:
    print("1) 解析：goal / step（带判据）/ drop")
    plan, diag = P.parse_block(
        "goal: 让待办能归档\n"
        "step: 给列表加归档按钮 | done: #arch.visible == true\n"
        "step: 归档后从主列表移除 | done: #list.count == 2\n")
    check("解析成功", diag is None and plan is not None, diag)
    check("goal 收下了", plan["goal"] == "让待办能归档", plan)
    check("两条 step", len(plan["steps"]) == 2, plan["steps"])
    check("判据原样保留", plan["steps"][0]["done"] == "#arch.visible == true",
          plan["steps"][0])

    print("\n2) 完成判据由机制求值（不是自报）")
    done, lines, warns = P.evaluate(plan["steps"], ATTRS)
    check("第 1 步达成（visible==true）", 0 in done, lines)
    check("第 2 步未达成（count 5 != 2）", 1 not in done, lines)
    check("告警为空（判据都可用）", warns == [], warns)
    nxt = P.current_step(lines, done)
    check("当前该做的是**第一条未达成**", nxt and nxt["index"] == 1, nxt)

    print("\n3) 未达成时说清当前值（不是干巴巴一句）")
    status = [s for i, w, s, v in lines if i == 1][0]
    check("状态里带上了当前值", "5" in status, status)
    check("状态里带上了要求的判据", "== 2" in status, status)

    print("\n4) 无 done: 的步骤 → 不判完成（诚实）")
    plan2, _ = P.parse_block("goal: 清理历史包袱\nstep: 删掉没人用的控件")
    done2, lines2, warns2 = P.evaluate(plan2["steps"], ATTRS)
    check("无判据的步骤不算完成", done2 == set(), done2)
    check("且明说需确认（不假装失败）",
          any("需人" in s or "确认" in s for _, _, s, _ in lines2), lines2)
    check("它仍被指为当前该做的一步", P.current_step(lines2, done2)["index"] == 0)

    print("\n5) 判据用不了 → 可见告警，不退化成'算完成'")
    plan3, _ = P.parse_block("goal: g\nstep: 修库存 | done: #nope.value < 3")
    done3, lines3, warns3 = P.evaluate(plan3["steps"], ATTRS)
    check("取不到属性时**不算完成**", done3 == set(), done3)
    check("有可见告警", warns3 and "nope" in warns3[0], warns3)
    check("行状态标出取不到", any("取不到" in s for _, _, s, _ in lines3), lines3)

    print("\n6) 非法判据 / 步数超限 → 可见拒绝")
    _, diag = P.parse_block("goal: g\nstep: s | done: #a.v > #b.v")
    check("复杂判据被拒（PLAN_INVALID）",
          diag and diag["code"] == "PLAN_INVALID", diag)
    _, diag = P.parse_block("goal: g\nstep: " + "\nstep: ".join("第%d步" % i for i in range(20)))
    check("步数超限被拒（PLAN_REJECTED）",
          diag and diag["code"] == "PLAN_REJECTED", diag)
    _, diag = P.parse_block("goal: 只有一个目标没有步骤")
    check("只有 goal 没有 step 被拒（那是许愿）",
          diag and diag["code"] == "PLAN_INVALID", diag)
    check("报错文案点破了它", diag and "许愿" in diag["message"], diag)
    _, diag = P.parse_block("goal: g\nstep: a | #x.y < 1")
    check("判据前缺 done: 标签被拒（PLAN_INVALID）",
          diag and diag["code"] == "PLAN_INVALID" and "done:" in diag["message"], diag)

    print("\n7) 注入块：进度可见 + 指认下一步 + 全达成时收手")
    block = P.plan_block(plan, lines, done)
    check("块里有目标", "让待办能归档" in block, block)
    check("块里标了核验进度", "1/2" in block, block)
    check("已达成那步打了勾", "[x] 1." in block, block)
    check("未达成那步没打勾", "[ ] 2." in block, block)
    check("明确指出当前该做第几步", "当前该做的是第 2 步" in block, block)
    all_done, all_lines, _ = P.evaluate(plan["steps"],
                                        dict(ATTRS, **{"#list.count": 2}))
    blk = P.plan_block(plan, all_lines, all_done)
    check("全达成时叫它收手（别做多余的事）", "都已达成" in blk, blk)
    check("空计划不注废话", P.plan_block({"goal": "x", "steps": []}, [], set()) == "")

    print("\n8) 落盘与块解析（宿主侧块，不是语言语法）")
    blocks, _ = parse_response("先做第一步\n```plan\ngoal: g\nstep: s | done: #a.b < 1\n```\n")
    check("plan 块被识别", any(b.kind == "plan" for b in blocks), [b.kind for b in blocks])
    blocks, diags = parse_response("```planx\nfoo\n```")
    check("不认识的信息串仍报出来（零静默）",
          any(d.code == "LLM_BLOCK_UNKNOWN" for d in diags), [d.code for d in diags])

    work = Path(tempfile.mkdtemp(prefix="puppethub-smoke-plan-"))
    app = create_app(work, "plan", "plan 冒烟")
    app.write_source(['add #root window #w title="p" w=400 h=300',
                      'add #w col #main pad=8',
                      'add #main text #t text="hi"'], origin="system")
    session = Session(app)
    session.start()
    ok, info = session.set_plan("goal: g\nstep: a | done: #t.text == \"hi\"", origin="llm")
    check("写盘成功", ok and info["steps"] == 1, info)
    check("报告了可核验步数", info["judged"] == 1, info)
    check("plan.json 落在 .puppethub（与 goal.json 同级）",
          (session.app.root / ".puppethub" / "plan.json").is_file())
    check("决策流水留痕",
          "设定计划" in (session.app.read_design() or ""),
          (session.app.read_design() or "")[-200:])
    # 真机求值：喂一帧快照给 session，看块是否成文
    session._last_attrs = {"#t.text": "hi"}
    blk = session.plan_block()
    check("session.plan_block 用机制判出达成", "1/1" in blk, blk)
    session._last_attrs = {"#t.text": "bye"}
    blk = session.plan_block()
    check("条件不满足时不算达成（不是自报）", "0/1" in blk, blk)
    # drop：撤销步骤
    ok, info = session.set_plan("drop: 做不了的那步", origin="llm")
    check("只剩 drop 时清空计划并如实说",
          ok and session.current_plan() == {}, session.current_plan())

    print("\n%s" % ("失败：" + "、".join(FAILED) if FAILED else "全部通过"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
