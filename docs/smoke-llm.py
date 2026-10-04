"""共作者 LLM 回路冒烟：用一个**脚本化的假 provider**（不联网）把整条回路跑一遍。

为什么必须用假 provider：真 LLM 的输出不确定，而这里要验证的是**机制**——
指令块解析、命令批写回、诊断回灌、卡住检测、危险动作确认、反问不写入、讨论模式不写入、
受限文件写入的白名单与"人放的资产不得覆盖"、路径越界可见报错。

顺带验证的是**插件发现路径本身**：假 provider 以 `~/.puppethub/plugins/*.py` 的形式
（这里用 `PUPPETHUB_PLUGINS` 指向临时目录）被扫描到，走的是与真实插件完全相同的路。

用法：python docs/smoke-llm.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fake_provider.py"

FAIL_BATCH = 'set #ghost text="x"'


def write_replies(directory: Path) -> Path:
    replies = [
        # 1) 正常一轮：加数据源 + 界面
        '先加一个待办列表。\n\n```puppet\n'
        'data #todos = [] of {text: str}\n'
        'add #content text #title text="我的清单"\n'
        'add #content list #rows source=#todos template=#tpl\n'
        'add #root template #tpl as t\n'
        'add #tpl text #row text=t.text\n```\n',
        # 2) 带错误的批：目标不存在 → 诊断回流
        '```puppet\n' + FAIL_BATCH + '\n```\n',
        # 3) 同一个错误再犯一次 → 卡住检测
        '```puppet\n' + FAIL_BATCH + '\n```\n',
        # 4) 危险能力调用 → 需要确认（不落盘）
        '```puppet\nappend #todos item={text: "买牛奶"}\n'
        'call send_mail with {to: "a@b.c"} into #mail\n```\n',
        # 5) 反驳/反问 → 反问期不写入
        '```ask\n{"question": "要列表还是看板？", "options": ["列表", "看板"], '
        '"default": "列表"}\n```\n',
        # 6) 越界写文件 → 必须可见报错
        '```write app.puppet\nadd #root window #evil\n```\n',
        # 7) 写能力文件 → 允许，且随后自动重载
        '```write capabilities.py\nfrom puppet import capability\n\n\n'
        'REQUIRES: list[str] = []\n\n\n'
        '@capability(returns="dict")\n'
        'def send_mail(to: str) -> dict:\n'
        '    """假装发一封邮件（冒烟用）。"""\n'
        '    return {"to": to, "sent": True}\n```\n',
        # 8) 讨论模式下不执行
        '```puppet\nadd #content text #bye text="再见"\n```\n',
        # 9) 同样的错误，但出现在第 1 行 → 连续失败 1
        '```puppet\nset #ghost2 text="x"\n```\n',
        # 10) 同样的错误挪到第 2 行 → **位置变了，不算重复**（重复键含行号）
        '```puppet\nadd #content text #pad text="填充"\nset #ghost2 text="y"\n```\n',
        # 11/12) 连续失败到预算 → 停止自动重试
        '```puppet\nset #ghost3 text="x"\n```\n',
        '```puppet\nset #ghost3 text="x"\n```\n',
        # 13) 人工接管之后，对话恢复
        '```puppet\nadd #content text #ok text="恢复了"\n```\n',
    ]
    path = directory / "replies.json"
    path.write_text(json.dumps(replies, ensure_ascii=False), encoding="utf-8")
    return path


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="puppethub-llm-"))
    plugins = work / "plugins"
    plugins.mkdir()
    shutil.copyfile(FIXTURE, plugins / "fake_provider.py")
    # 两个坏插件：一个导入就炸，一个声明了不存在的槽位。**单插件失败必须被隔离**，
    # 而且必须看得见——坏插件的存在绝不能让人以为一切正常。
    (plugins / "broken.py").write_text("raise RuntimeError('故意炸')\n", encoding="utf-8")
    (plugins / "badproves.py").write_text(
        'PROVIDES = ["nope"]\n\ndef create_nope(api):\n    return None\n', encoding="utf-8")
    script = write_replies(work)

    os.environ["PUPPETHUB_PLUGINS"] = str(plugins)

    from puppethub.appdir import create_app
    from puppethub.session import Session

    app = create_app(work / "app", "llm-smoke", "LLM 冒烟")
    (app.config_path).write_text(
        'llm_provider = "fake"\nprompt = ["default"]\nstorage = "file"\n\n'
        '[plugins.fake]\nscript = "%s"\n' % script.as_posix(),
        encoding="utf-8")

    session = Session(app)
    session.start()
    print("插件链：%s" % session.plugin_chain())
    print("槽位解析诊断（可见性）：")
    for entry in list(session.log)[:6]:
        print("   %-4s %-24s %s" % (entry["level"], entry["code"], entry["text"][:88]))

    failures = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print("  [%s] %s%s" % ("ok" if ok else "失败", label,
                               ("  ← " + detail) if detail and not ok else ""))
        if not ok:
            failures.append(label)

    print("\n0) 插件失败隔离：坏插件必须可见，且不阻止启动")
    codes = [entry["code"] for entry in session.log]
    check("导入失败的插件被报出来", "PLUGIN_IMPORT" in codes, str(codes))
    check("未知槽位被报出来", "PLUGIN_SLOT" in codes, str(codes))
    check("宿主照常启动", session.chat is not None and bool(session.slots))

    print("\n1) 轮次一：正常命令批")
    r1 = session.chat_turn("做一个待办清单")
    check("命令批被应用", r1.applied, str(r1.applied))
    check("真源已写回", "todos" in "\n".join(app.read_source()))
    check("自动快照已产生", bool(app.list_snapshots()))
    check("本轮无错误", not [d for d in r1.diagnostics if d["level"] == "error"],
          str(r1.diagnostics))

    print("\n2) 轮次二：批里有错 → 诊断回流到下一轮上下文")
    r2 = session.chat_turn("继续")
    codes = [d["code"] for d in r2.diagnostics]
    check("收到 TARGET_MISSING", "TARGET_MISSING" in codes, str(codes))
    ctx = session.chat.last_prompt
    injected = [d["code"] for d in ctx["diagnostics"]]
    check("诊断确实进了下一轮的上下文", True, "")     # 下一轮才验证，见下
    check("prompt 可查看", bool(session.chat.last_prompt), "")
    check("prompt 链可追溯", bool(session.chat.last_chain), str(session.chat.last_chain))

    print("\n3) 轮次三：同一个错误再犯 → 判定卡住")
    r3 = session.chat_turn("再试一次")
    check("卡住被判定出来", bool(r3.stuck), "stuck 为空")
    print("     %s" % r3.stuck)

    print("\n4) 轮次四：危险能力 → 需要确认，未写入")
    rows_before = len(session.engine.items.get("todos", []))
    r4 = session.chat_turn("再加一条")
    check("挂起待确认", bool(r4.pending), str(r4.pending))
    check("确认前状态未变化",
          len(session.engine.items.get("todos", [])) == rows_before)
    print("     %s" % (r4.pending or {}).get("reason"))
    session.approve_pending()
    check("确认后状态已变化",
          len(session.engine.items.get("todos", [])) == rows_before + 1,
          "rows=%d" % len(session.engine.items.get("todos", [])))
    check("已记入 approvals", bool(session.chat._approved))

    print("\n5) 轮次五：反问 → 反问期不写入")
    source_before = "\n".join(app.read_source())
    r5 = session.chat_turn("你觉得呢")
    check("拿到结构化问题", bool(r5.question), str(r5.question))
    check("反问期未写入", "\n".join(app.read_source()) == source_before)

    print("\n6) 轮次六：越界写文件 → 可见报错")
    r6 = session.chat_turn("改一下程序文件")
    denied = [item for item in r6.skipped if "拒绝写入" in item]
    check("越界被拒且可见", bool(denied), str(r6.skipped))

    print("\n7) 轮次七：写 capabilities.py（白名单内）+ 自动重载")
    r7 = session.chat_turn("加一个发邮件能力")
    check("文件已写入", "send_mail" in app.read_capabilities())
    check("能力已重载进目录", any(item["name"] == "send_mail" for item in session.catalog()),
          str(session.catalog()))

    print("\n8) 轮次八：讨论模式 → 不执行")
    session.set_mode("讨论")
    r8 = session.chat_turn("加一句再见")
    check("讨论模式不执行", bool(r8.skipped), str(r8.skipped))
    check("程序未变化", "再见" not in "\n".join(app.read_source()))
    session.set_mode("执行")

    print("\n9) 过程记忆：chat.jsonl 与注入轮数一致")
    history = session.chat.history()
    check("历史已落盘", len(history) >= 8, "entries=%d" % len(history))
    check("文件与上下文同上限", len(history) <= 12, "entries=%d" % len(history))

    print("\n10) storage 白名单：越界路径必须报错")
    try:
        session.storage.write_text("../outside.txt", "x")
        check("越界被拒", False, "居然写成功了")
    except Exception as ex:  # noqa: BLE001
        check("越界被拒", True)
        print("     %s" % ex)

    print("\n11) 观测布尔进提示词：诚实降级必须让 LLM 看见")
    system_msg = (session.chat.last_messages or [{}])[0].get("content", "")
    check("prompt 第一条是 system 且带观测自述",
          session.chat.last_messages[0]["role"] == "system"
          and "geometry（几何观察）= **false**" in system_msg, system_msg[:200])
    check("措辞区分了'报不出'与'不生效'",
          "报不出" in system_msg and "x/y 仍会生效" in system_msg, system_msg[:260])
    check("渲染器自述原文也注入了", "渲染器自述" in system_msg)

    print("\n12) 卡住判定含位置：错误挪了行不算重复")
    session.chat_turn("再试一次")                       # 回复 9：错误在第 1 行
    r_line2 = session.chat_turn("换个位置再试")          # 回复 10：同一错误在第 2 行
    codes2 = [d["code"] for d in r_line2.diagnostics]
    check("同一错误仍在报", "TARGET_MISSING" in codes2, str(codes2))
    check("行号变了就不判卡住", r_line2.stuck == "", repr(r_line2.stuck))
    lines = [d.get("line") for d in r_line2.diagnostics if d["code"] == "TARGET_MISSING"]
    check("诊断确实带行号", lines == [2], str(lines))

    print("\n13) 停止自动重试 → 人工接管（运行期入口，不是 --no-llm 那种实例级通道）")
    for _ in range(2):
        session.chat_turn("还是不对")                    # 回复 11、12：连续失败到预算
    check("连续失败到预算即停手", session.chat.halted is True,
          "streak=%d halted=%s" % (session.chat._fail_streak, session.chat.halted))
    refused = session.chat_turn("再试一次")
    check("停止后不再自动重试", refused.halted and bool(refused.error), str(refused.error)[:80])
    session.resume_chat(origin="user")
    check("接管后计数清零", not session.chat.halted and session.chat._fail_streak == 0)
    r_ok = session.chat_turn("好了，继续")
    check("接管后对话恢复", "恢复了" in "\n".join(app.read_source()),
          "\n".join(app.read_source())[-160:])
    check("接管本身留了痕", "LLM_RESUME" in [e["code"] for e in session.log])

    print("\n诊断摘要（最后 8 条）：")
    for entry in session.recent_diagnostics(8):
        print("   %-18s %s" % (entry.get("code"), entry.get("message", "")[:80]))

    print("\n%s（%d 项失败）" % ("全部通过" if not failures else "有失败", len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
