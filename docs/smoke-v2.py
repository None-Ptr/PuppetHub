"""V2 全阶段冒烟：写者状态机 / 自主回路 / 热重载 / 沙箱 / 远程绑定 / app 级插件目录。

要验证的不是"能跑"，是每一条**边界**：
- 写者互斥：自主当值时共作者**只读**；暂停时谁都不能写；交回后恢复。
- 自主的边界更硬：危险能力默认拒绝（白名单只能人预先给）、批行数预算、
  连续失败熔断切回无人 + 决策流水留痕 + autonomous.jsonl 审计。
- 热重载的能力在盘上变化就生效（可见），插件热重载在流式/待确认时拒绝。
- 沙箱：内置插件诚实拒绝（说了为什么不假裝）；文件插件真进子进程且全通道可用。
- 远程绑定：同一套操作、单写者不破例、做不到的操作**说**做不到。

用法：python docs/smoke-v2.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 便于读 fixtures

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _denied_paths(storage) -> bool:
    """storage 直读程序资产必须被拒（白名单守卫在沙箱外依旧成立）。"""
    try:
        storage.read_text("app.puppet")
        return False
    except Exception:
        return True


def build_replies(work: Path) -> Path:
    """共作者与自主回路**共用一个脚本化 provider**（同一队列，按调用顺序消费）。"""
    lines_50 = "\n".join('add #content text #b%d text="x"' % i for i in range(50))
    replies = [
        # 1) 共作者（只读期）：带写入块 → 必须被 WRITER_READONLY 拦下
        '```puppet\nadd #content text #r text="只读期不该写进来"\n```\n',
        # 2) 自主步进（当值）：正常的一小批
        '状态正常，补一个占位说明。\n\n```puppet\n'
        'add #content text #auto text="自主补的"\n```\n',
        # 3) 自主步进：危险能力不在白名单 → 默认拒绝
        '```puppet\ncall send_mail with {to: "a@b.c"} into #m\n```\n',
        # 4) 自主步进：50 行大批 → 超预算
        '```puppet\n' + lines_50 + '\n```\n',
        # 5/6/7) 自主步进：连续报错 → 熔断
        '```puppet\nset #ghost text="x"\n```\n',
        '```puppet\nset #ghost text="x"\n```\n',
        '```puppet\nset #ghost text="x"\n```\n',
        # 8) 交回共作者后：恢复正常
        '```puppet\nadd #content text #back text="写者回来了"\n```\n',
    ]
    script = work / "replies.json"
    script.write_text(json.dumps(replies, ensure_ascii=False), encoding="utf-8")
    return script


def main() -> int:
    from puppethub.appdir import create_app
    from puppethub.session import Session

    work = Path(tempfile.mkdtemp(prefix="puppethub-v2-"))
    plugins = work / "plugins"
    plugins.mkdir()
    shutil.copyfile(FIXTURES / "fake_provider.py", plugins / "fake_provider.py")
    shutil.copyfile(FIXTURES / "fileplus.py", plugins / "fileplus.py")
    script = build_replies(work)
    os.environ["PUPPETHUB_PLUGINS"] = str(plugins)

    failures = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print("  [%s] %s%s" % ("ok" if ok else "失败", label,
                               ("  ← " + detail) if detail and not ok else ""))
        if not ok:
            failures.append(label)

    codes = lambda: [entry["code"] for entry in session.log]

    # ============================================================ 主体 app
    app = create_app(work / "app", "v2-smoke", "V2 冒烟")
    # app 级插件目录（V2 阶段 6）：放进去就该被发现——名字与全局唯一性照旧。
    app_plugins = app.root / ".puppethub" / "plugins"
    app_plugins.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "applevel_prompt.py", app_plugins / "applevel_prompt.py")
    (app.root / "app.puppet").write_text(
        'add #root window #win title="V2"\n'
        'add #win col #content pad=16 gap=12 flex=1\n', encoding="utf-8")
    app.config_path.write_text(
        'llm_provider = "fake"\nstorage = "file"\n'
        'prompt = ["default", "applevel"]\n\n'
        '[plugins.fake]\nscript = "%s"\n\n'
        '[autonomous]\nfail_budget = 2\nmax_batch_lines = 40\nallow_calls = []\n'
        % script.as_posix(), encoding="utf-8")

    print("1) 写者状态机（V2 M1）：llm / autonomous / none 互斥")
    session = Session(app)
    session.start()
    check("默认写者 = llm", session.writer == "llm", session.writer)
    check("app 级插件被发现",
          "applevel" in (session.slot_names.get("prompt") or []),
          str(session.slot_names))
    check("写者切换留痕前：DESIGN.md 无此行",
          "写者切换" not in app.read_design())
    check("交回自主", session.set_writer("autonomous", origin="user"))
    check("切换写进了决策流水", "写者切换" in app.read_design())

    print("\n2) 自主当值时共作者只读（WRITER_READONLY，程序不得变）")
    before = session.program_text()
    r = session.chat_turn("我来补一句")
    check("只读期写入被拒", "WRITER_READONLY" in codes(), str(codes()[-4:]))
    check("程序确实未变", session.program_text() == before)
    check("拒绝的话说清了写者是谁", any("只读" in s and "autonomous" in s
                                    for s in r.skipped), str(r.skipped))

    print("\n3) 自主步进：正常批 origin=autonomous + 审计入账")
    entry = session.autonomous_step("定时检查：补一个占位说明")
    check("正常步进已应用", any("命令批" in a for a in (entry or {}).get("applied", [])),
          str(entry))
    check("观察流标 origin=autonomous",
          any("autonomous" in entry2["text"] for entry2 in session.log
              if entry2["code"] == "WRITEBACK"), str(codes()[-6:]))
    check("审计文件有账", app.root.joinpath(".puppethub/autonomous.jsonl").is_file())

    print("\n4) 自主的边界更硬：危险能力默认拒绝 / 批行数预算")
    session.autonomous_step("用户让我发邮件")
    check("白名单外能力被拒", "AUTONOMOUS_DENIED" in codes(), str(codes()[-4:]))
    session.autonomous_step("一次加 50 个节点")
    check("批行数超预算被拒", "AUTONOMOUS_BUDGET" in codes(), str(codes()[-4:]))

    print("\n5) 熔断（V2 M3）：连续失败 → 写者切回 none + 流水 + 可选回滚")
    for _ in range(3):
        session.autonomous_step("引擎在报错，修一下")
    check("熔断可见", "AUTONOMOUS_CIRCUIT" in codes(), str(codes()[-8:]))
    check("写者已切回 none", session.writer == "none", session.writer)
    check("熔断进决策流水", "熔断" in app.read_design())
    entry = session.autonomous_step("再试一次")
    check("熔断后不再步进", entry is None or "未当值" in (entry.get("error") or ""),
          str(entry))

    print("\n5b) 写者核对在**写入路径**上：在途调用（入口检查已过）也拦得下")
    before = session.program_text()
    diags = session.send(['add #content text #late text="在途写入"'],
                         origin="autonomous")
    check("在途写入被拒且可见",
          any(d.code == "WRITER_DENIED" for d in diags), str([d.code for d in diags]))
    check("真源未变", session.program_text() == before)

    print("\n6) 交回共作者：恢复正常写入")
    check("交回 llm", session.set_writer("llm", origin="user"))
    r = session.chat_turn("继续")
    check("共作者恢复写入", "back" in session.program_text(),
          session.program_text()[-120:])
    check("自主审计 ≥ 6 条", len(session.autonomous.recent(50)) >= 6,
          str(len(session.autonomous.recent(50))))

    print("\n7) 能力热重载（V2 阶段 3）：盘上变化自动生效且可见")
    app.write_capabilities(app.read_capabilities().rstrip("\n") +
                           '\n\n\n@capability(returns="str")\n'
                           'def ping() -> str:\n'
                           '    """探活。"""\n'
                           '    return "pong"\n')
    time.sleep(0.02)
    check("check_capabilities 察觉变化", session.check_capabilities() is True)
    check("新能力进了目录", any(item["name"] == "ping" for item in session.catalog()),
          str([item["name"] for item in session.catalog()]))
    check("重载有专门的码", "CAPABILITY_RELOAD" in codes())
    check("无变化时不误报", session.check_capabilities() is False)

    print("\n7b) 热重载的竞态守卫：流式中 / 写入进行中跳过本轮检查")
    app.write_capabilities(app.read_capabilities().rstrip("\n") +
                           '\n\n\n@capability(returns="str")\n'
                           'def ping2() -> str:\n'
                           '    """探活二号。"""\n'
                           '    return "pong"\n')
    time.sleep(0.02)
    session.chat.streaming = True
    check("流式中跳过重载", session.check_capabilities() is False)
    session.chat.streaming = False
    check("流式结束后补上重载", session.check_capabilities() is True)
    check("ping2 进了目录", any(item["name"] == "ping2" for item in session.catalog()),
          str([item["name"] for item in session.catalog()]))

    print("\n8) 插件热重载（V2 阶段 3）：护栏 + 生效链保持")
    history_before = len(session.chat.history())
    session.chat.streaming = True
    check("流式中拒绝", session.reload_plugins() is False)
    session.chat.streaming = False
    session.chat.pending = {"kind": "batch", "body": "x", "reason": "r", "calls": []}
    check("待确认时拒绝", session.reload_plugins() is False)
    session.chat.pending = None
    old_chat = session.chat
    check("重载成功", session.reload_plugins() is True)
    check("回路换了新实例", session.chat is not old_chat)
    check("写者已恢复", session.writer == "llm", session.writer)
    check("过程记忆没丢", len(session.chat.history()) == history_before,
          "%d vs %d" % (history_before, len(session.chat.history())))

    # ============================================================ 沙箱（V2 阶段 3）
    print("\n9) 沙箱：内置插件诚实拒绝；文件插件真进子进程")
    app2 = create_app(work / "app2", "v2-sandbox-builtin", "沙箱内置")
    app2.config_path.write_text(
        'llm_provider = "fake"\nstorage = "file"\n\n'
        '[plugins.fake]\nscript = "%s"\n\n[sandbox]\nenabled = true\n'
        % script.as_posix(), encoding="utf-8")
    session2 = Session(app2)
    session2.start()
    check("内置插件被诚实拒绝沙箱化",
          any("内置插件" in entry["text"] for entry in session2.log
              if entry["code"] == "SANDBOX"), str(codes()[-6:]))

    app3 = create_app(work / "app3", "v2-sandbox-file", "沙箱文件插件")
    app3.config_path.write_text(
        'llm_provider = "fake"\nstorage = "fileplus"\n\n'
        '[plugins.fake]\nscript = "%s"\n\n[sandbox]\nenabled = true\n'
        % script.as_posix(), encoding="utf-8")
    from puppethub.sandbox import SubprocessStorage
    session3 = Session(app3)
    session3.start()
    check("storage 机制运行在子进程",
          isinstance(session3.storage.plugin, SubprocessStorage),
          type(session3.storage.plugin).__name__)
    session3.send(['add #content text #s text="沙箱里写的"'], origin="driver")
    check("子进程里完成的写回生效", "沙箱里写的" in session3.program_text())
    check("引擎状态也经子进程落盘",
          session3.storage.exists(".puppet/state/state.json"))
    state_via_channel = session3.storage.read_text(".puppet/state/state.json")
    check("子进程沙箱内能读回（引擎状态文件，经通道）",
          isinstance(json.loads(state_via_channel), dict), state_via_channel[:120])
    check("程序资产仍被白名单挡住（沙箱不改变守卫）",
          _denied_paths(session3.storage))

    # ============================================================ 远程绑定（V2 阶段 4）
    print("\n10) 远程/多客户端绑定：同一套操作，单写者不破例")
    from puppethub.remote import TcpBinding, serve_tcp
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    binding = TcpBinding(app)
    thread = threading.Thread(target=serve_tcp, args=(binding, port), daemon=True)
    thread.start()
    time.sleep(0.6)

    def rpc(request: dict):
        conn = socket.create_connection(("127.0.0.1", port), timeout=5)
        conn.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        reply = json.loads(conn.makefile("r", encoding="utf-8").readline())
        return reply, conn

    hello, conn = rpc({"op": "hello"})
    check("hello 经远程可达", hello.get("protocol") == "1"
          and hello.get("transport") == "tcp", str(hello)[:120])
    conn.close()
    reply, conn = rpc({"op": "send", "batch": ['add #content text #remote text="远程写的"']})
    check("远程命令批走同一条写入路径",
          "远程写的" in binding.session.program_text(), str(reply)[:160])
    conn.close()
    reply, conn = rpc({"op": "dump"})
    check("dump 可读", any("#remote" in line for line in reply.get("program", [])))
    conn.close()
    reply, conn = rpc({"op": "load"})
    check("远程拒绝整份替换且说明理由", "整份替换" in reply.get("error", ""), str(reply))
    conn.close()
    reply, conn = rpc({"op": "snapshot"})
    check("无渲染器的降级被明说", "渲染器" in reply.get("error", ""), str(reply))
    conn.close()

    print("\n%s（%d 项失败）" % ("全部通过" if not failures else "有失败", len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
