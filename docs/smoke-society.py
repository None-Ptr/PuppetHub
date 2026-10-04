"""V4 agent 社会冒烟：B 服务化 · A 协作总线 · C 深度自主。

要验证的是**铁律在协作里依然成立**：
- 服务清单全派生（interactions/data/lend），不是手写的第二份真相；
- 能力借出 = 被调方说了算：不在 lend 清单一律默认拒绝；
- 消息是刺激不是写入：投递进观察流 / 触发自主回路，**永不写对方真源**；
- tell / goal 走当值写者路径，目标跨会话可见、进自主 prompt；
- hub 集成：子进程自动拿 --hub-port 接入总线并注册订阅（回执可证）。

用法：python docs/smoke-society.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROGRAM = [
    'add #root window #win title="协作"',
    'add #win col #content pad=16',
    'add #content button #go text="走"',
    'on #go click:',
    '    set #win title="走了"',
]

CAPS = ('"""能力。"""\nfrom puppet import capability\n\n\n'
        '@capability(returns="str")\ndef shout(text: str) -> str:\n'
        '    """原样返回。"""\n    return text\n')


def _tcp_call(port: int) -> dict:
    """对远程 app 发 call op（agent4 没有借出任何能力——应答是可见拒绝也行，
    关键是**有应答**：进程活着且服务面通）。"""
    import socket
    conn = socket.create_connection(("127.0.0.1", port), timeout=3)
    try:
        conn.sendall(b'{"op": "call", "name": "shout", "args": {"text": "x"}}\n')
        return json.loads(conn.makefile("r", encoding="utf-8").readline())
    finally:
        conn.close()


def main() -> int:
    from puppethub.appdir import AppDir, create_app
    from puppethub.bus import BusClient, HubBus, bind_bus, serve_bus
    from puppethub.chat import parse_response
    from puppethub.remote import TcpBinding, serve_tcp
    from puppethub.session import Session

    work = Path(tempfile.mkdtemp(prefix="puppethub-society-"))
    failures = []

    # 脚本化 provider 走与真实插件完全相同的发现路径（PUPPETHUB_PLUGINS）
    plugins = work / "plugins"
    plugins.mkdir()
    shutil.copyfile(ROOT / "docs" / "fixtures" / "fake_provider.py",
                    plugins / "fake_provider.py")
    import os
    os.environ["PUPPETHUB_PLUGINS"] = str(plugins)

    def check(label: str, ok: bool, detail: str = "") -> None:
        print("  [%s] %s%s" % ("ok" if ok else "失败", label,
                               ("  ← " + detail) if detail and not ok else ""))
        if not ok:
            failures.append(label)

    # ---------------------------------------------------------------- 装配
    a1 = create_app(work, "agent1", "发送方")
    a1.write_source(list(PROGRAM))
    a1.write_capabilities(CAPS)          # 有能力但 config 不写 [service] lend → 缺省不借
    a1.config_path.write_text(
        'llm_provider = "fake"\nstorage = "file"\n\n'
        '[plugins.fake]\nscript = "%s"\n' % (work / "replies.json").as_posix(),
        encoding="utf-8")
    a2 = create_app(work, "agent2", "接收方")
    a2.write_source(list(PROGRAM))
    a2.write_capabilities(CAPS)
    a2.config_path.write_text(
        'storage = "file"\n\n[service]\nlend = ["shout"]\n\n'
        '[collab]\nsubscribe = ["status"]\n', encoding="utf-8")
    (work / "replies.json").write_text(json.dumps([
        "收到，我给同行捎个信。\n```tell agent2 status\n提醒时间提前了十分钟。\n```",
        "说明：目标已记下。\n```goal\n盯住完成率，发现数据断更就先修数据源。\n```",
        "（无事可做，本轮不改。）",
    ], ensure_ascii=False), encoding="utf-8")

    # 总线（线程内真 TCP）：lookup 直接指向进程内 app2 的服务端口
    bus_port = 8941
    bus = HubBus(lookup_port=lambda name: 8942 if name == "agent2" else None,
                 audit_path=work / "bus.jsonl")
    server = bind_bus(bus_port)
    threading.Thread(target=serve_bus, args=(bus, bus_port, server),
                     daemon=True).start()

    binding2 = TcpBinding(AppDir(a2.root), hub_port=bus_port)
    threading.Thread(target=serve_tcp, args=(binding2, 8942), daemon=True).start()
    time.sleep(0.5)

    session1 = Session(AppDir(a1.root))
    session1.start()
    session1.attach_hub(bus_port)

    print("1) B · 服务清单：全派生，不手写")
    hello = binding2.handle({"op": "hello"})
    service = hello.get("service") or {}
    check("interactions 从处理器派生", "#go.click" in service.get("interactions", []),
          str(service))
    check("data 清单派生", service.get("data") == [], str(service))
    check("lend 就是配置里的借出清单", service.get("lend") == ["shout"], str(service))
    check("hello 带服务清单（服务发现同一握手）", hello.get("protocol") == "1"
          and "service" in hello)

    print("\n2) B · 能力借出：被调方说了算（默认拒绝）")
    reply = binding2.handle({"op": "call", "name": "shout", "args": {"text": "嗨"}})
    check("清单内能力跨进程同步调用成功",
          reply.get("ok") is True and reply.get("value") == "嗨", str(reply))
    reply = binding2.handle({"op": "call", "name": "shout2", "args": {}})
    check("未知能力可见拒绝", reply.get("ok") is False, str(reply))
    lend_none = Session(AppDir(a1.root))
    lend_none.start()
    reply = lend_none.call_capability("shout", {"text": "x"})
    check("无 lend 配置的实例默认拒绝（缺省空 = 一律不借）",
          reply.get("ok") is False and "未借出" in (reply.get("error") or ""),
          str(reply))
    reply = binding2.handle({"op": "call", "name": "shout"})
    check("缺参数契约校验", reply.get("ok") is False and "缺少必需参数" in
          (reply.get("error") or ""), str(reply))

    print("\n3) A · 消息是刺激不是写入")
    client = BusClient("agent1", bus_port)
    program_before = binding2.session.engine.program_lines()
    reply = client.post("status", "提醒时间提前了十分钟。", title="同步")
    check("投递给订阅者", reply.get("delivered") == ["agent2"], str(reply))
    time.sleep(0.3)
    peer_notes = [e for e in binding2.session.log if e["code"] == "PEER_MESSAGE"]
    check("进对方观察流（PEER_MESSAGE 可见）", peer_notes and
          "提醒时间" in peer_notes[-1]["text"], str(peer_notes[-1:]) )
    check("**对方程序一个字没变**（消息不是写入）",
          binding2.session.engine.program_lines() == program_before)
    reply = client.post("没人听的话题", "...")
    check("无订阅者如实回执（不装作送达）", reply.get("delivered") == [], str(reply))
    entries = bus.recent(10)
    check("总线审计落账（消息 + 回执）", len([e for e in entries
                                             if "delivery" in e]) >= 1
          and len([e for e in entries if "delivery" not in e]) >= 1, str(entries))

    print("\n4) tell / goal 指令块：当值写者的动作")
    parsed, diags = parse_response("```tell agent2 status\n正文\n```")
    check("tell 块解析（app + topic）", parsed and parsed[0].kind == "tell"
          and parsed[0].path == "agent2" and parsed[0].info == "status", str(parsed))
    r1 = session1.chat_turn("给同行捎信")
    check("tell 块执行：applied 可见", any("agent2" in s for s in r1.applied),
          str(r1.applied))
    time.sleep(0.3)
    peer_notes = [e for e in binding2.session.log if e["code"] == "PEER_MESSAGE"]
    check("链路端到端：对方真的收到了", peer_notes and "提醒" in peer_notes[-1]["text"])
    r2 = session1.chat_turn("定个目标")
    check("goal 块执行", any("目标" in s for s in r2.applied), str(r2.applied))
    check("目标可读（跨会话的载体 = storage）",
          session1.current_goal() == "盯住完成率，发现数据断更就先修数据源。",
          session1.current_goal())
    check("目标进决策流水（DESIGN.md append-only）",
          "设定目标" in a1.read_design() and "盯住完成率" in a1.read_design(),
          a1.read_design()[-160:])

    print("\n5) C · 自主二阶：目标进 prompt，同胞消息触发")
    session1.set_writer("autonomous", origin="user")
    entry = session1.autonomous.step("手动触发")
    check("自主步进留审计", entry is not None and entry.get("trigger"), str(entry))
    provider = session1.slots["llm_provider"]
    last_prompt = json.dumps(provider.seen[-1], ensure_ascii=False)
    check("prompt 带当前目标", "盯住完成率" in last_prompt, last_prompt[-200:])
    entry = session1.autonomous.on_peer_message("agent2", "status", "你好") or True
    time.sleep(1.5)
    check("同胞消息触发自主步进（审计有记录）",
          any("agent2" in (item.get("trigger") or "")
              for item in session1.autonomous.recent(3)),
          str(session1.autonomous.recent(3)))
    session1.set_writer("llm", origin="user")

    print("\n6) hub 集成：子进程自动拿 --hub-port 接入总线（真链路）")
    from puppethub import hub
    work2 = work / "federation"
    work2.mkdir()
    a3 = create_app(work2, "agent3", "订阅者")
    a3.config_path.write_text('storage = "file"\n\n[collab]\nsubscribe = ["news"]\n',
                              encoding="utf-8")
    a4 = create_app(work2, "agent4", "普通")
    started = hub.up(work2, base_port=8960)
    check("两个子进程都等到握手", all(item["ready"] for item in started),
          str(started))
    state = hub._load_state(work2)
    check("总线端口入账本", bool(state.get("bus_port")), str(state.get("bus_port")))
    # 从 hub 进程外的视角直接对总线说话：投递回执含 agent3 = 子进程确实
    # 收到了 --hub-port 并自动注册了订阅（这是接线存在的可观察证据）。
    outside = BusClient("hub-cli", state["bus_port"])
    reply = outside.post("news", "全社通知：今晚例行自检。")
    check("子进程自动注册订阅（回执可证）",
          reply.get("delivered") == ["agent3"], str(reply))
    agent2_port = state["apps"]["agent4"]["port"]
    call_reply = _tcp_call(agent2_port)
    check("hub 拉起的子进程服务面可应答（call op 返回结构化结果）",
          isinstance(call_reply, dict) and ("ok" in call_reply or "error" in call_reply),
          str(call_reply))
    hub.down(work2)

    shutil.rmtree(work, ignore_errors=True)
    print("\n%s（%d 项失败）" % ("全部通过" if not failures else "有失败", len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
