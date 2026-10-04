"""运行期记忆冒烟：`.puppet/memory/`。

要验证的是**四条硬规矩**，不是"能读能写"：

1. 每次写入留诊断（谁 / 何时 / 改了什么）。
2. **可读可改**：人直接编辑文件，宿主按 id 增量合并——**不整体覆盖**（否则人改的会被悄悄吃掉）。
3. 上限 + **语义化裁剪**：先丢最不重要、其次最久未访问，**不按时间一刀切**。
4. 丢弃可见，且与"人主动清空"在观察流里**可区分**（`MEMORY_TRIMMED` vs `MEMORY_WIPED`）。

另加两条不变量：**重置状态保留记忆**；`--wipe-memory` 才清空。

用法：python docs/smoke-memory.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fake_provider.py"
SPY = Path(__file__).resolve().parent / "fixtures" / "spy_storage.py"

REPLIES = [
    # 1) 记三条：一条高重要度
    '先记下几件事。\n\n```remember\n'
    '{"text": "用户偏好中文界面", "importance": 1}\n'
    '{"text": "这个 app 只服务单机，不联网", "importance": 3, "tags": ["约束"]}\n'
    '一句话也当一条记（不是 JSON 就是纯文本）\n'
    '```\n',
    # 2) 再记两条 → 超出上限（3）→ 必须丢最不重要的
    '```remember\n'
    '{"text": "随手记的 A", "importance": 1}\n'
    '{"text": "随手记的 B", "importance": 1}\n'
    '```\n',
    # 3) 按 id 忘掉一条
    '```forget\nm4\n```\n',
    # 4) 什么都不写（确认"没有指令块 = 什么都不改"）
    '这轮我只是说说话。\n',
]


class _Sess:
    """`ft.Page` 需要可弱引用的 sess（本冒烟不启动 flet 运行时）。"""


def main() -> int:
    from puppethub.appdir import create_app
    from puppethub.session import Session

    work = Path(tempfile.mkdtemp(prefix="puppethub-mem-"))
    plugins = work / "plugins"
    plugins.mkdir()
    shutil.copyfile(FIXTURE, plugins / "fake_provider.py")
    shutil.copyfile(SPY, plugins / "spy_storage.py")
    script = work / "replies.json"
    script.write_text(json.dumps(REPLIES, ensure_ascii=False), encoding="utf-8")
    os.environ["PUPPETHUB_PLUGINS"] = str(plugins)

    app = create_app(work / "app", "mem-smoke", "记忆冒烟")
    app.config_path.write_text(
        'llm_provider = "fake"\nstorage = "spy-storage"\n\n'
        '[plugins.fake]\nscript = "%s"\n\n'
        '[memory]\nmax_entries = 3\ncontext_chars = 600\n' % script.as_posix(),
        encoding="utf-8")

    session = Session(app)
    session.start()

    failures = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print("  [%s] %s%s" % ("ok" if ok else "失败", label,
                               ("  ← " + detail) if detail and not ok else ""))
        if not ok:
            failures.append(label)

    memory_file = app.memory_dir / "memory.jsonl"
    codes = lambda: [entry["code"] for entry in session.log]

    print("1) 记三条：写入留诊断，且文件是人可读的 JSONL")
    r1 = session.chat_turn("记点东西")
    entries = session.memory_entries()
    check("三条已落盘", len(entries) == 3, str([e["id"] for e in entries]))
    check("写入有诊断", "MEMORY_WRITE" in codes(), str(codes()[-6:]))
    check("文件是可读的 JSONL",
          memory_file.is_file() and all(json.loads(line) for line in
                                        memory_file.read_text(encoding="utf-8").splitlines()),
          str(memory_file))
    check("非 JSON 的一行也当文本记", any("一句话" in e["text"] for e in entries),
          str([e["text"][:12] for e in entries]))
    check("本轮结果里报告了应用", len(r1.applied) == 3, str(r1.applied))

    print("\n2) 上下文注入：下一轮真的看得到记忆")
    r2 = session.chat_turn("继续")
    user_msg = [m for m in session.chat.last_messages if m["role"] == "user"][-1]["content"]
    check("prompt 里有记忆块", "运行期记忆" in user_msg)
    check("记忆内容进了上下文", "不联网" in user_msg)
    after = session.memory_entries()
    check("被注入的记忆记了一次访问",
          all(e.get("uses", 0) >= 1 for e in after if "不联网" in e["text"]),
          str([(e["id"], e.get("uses")) for e in after]))

    print("\n3) 人的手改必须活下来（按 id 增量合并，不整体覆盖）")
    human_line = json.dumps({"id": "m99", "text": "人手工加的一条", "importance": 2,
                             "tags": ["人工"], "created": "2026-10-03 00:00:00",
                             "accessed": "2026-10-03 00:00:00", "uses": 0,
                             "origin": "user"}, ensure_ascii=False)
    with open(memory_file, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(human_line + "\n")
    # 再触发一次写（第 2 轮已经写过一版，这里用一次显式 remember 确保写入路径跑过）
    session.memory.remember("触发一次写入", importance=1, origin="user")
    still_there = [e for e in session.memory_entries() if e.get("id") == "m99"]
    check("人加的那条还在", bool(still_there), str([e["id"] for e in session.memory_entries()]))

    print("\n4) 语义化裁剪：先丢最不重要的，且丢弃可见")
    ids = [e["id"] for e in session.memory_entries()]
    check("条数被压回上限", len(ids) <= 3, str(ids))
    check("高重要度的那条活下来了",
          any("不联网" in e["text"] for e in session.memory_entries()),
          str([e["text"][:14] for e in session.memory_entries()]))
    check("丢弃有专门的码", "MEMORY_TRIMMED" in codes(), str(codes()[-8:]))
    trimmed = [entry for entry in session.log if entry["code"] == "MEMORY_TRIMMED"]
    check("丢弃的理由写清了（不是人删的）",
          bool(trimmed) and "不是人主动删的" in trimmed[-1]["text"], str(trimmed[-1:]))

    print("\n5) forget 按 id 删；目标不存在要报出来")
    r3 = session.chat_turn("忘掉 m4")
    if any(e.get("id") == "m4" for e in session.memory_entries()):
        check("m4 已删", False, str([e["id"] for e in session.memory_entries()]))
    else:
        check("m4 已删（或本来就被裁掉了）", True)
    session.memory.forget("m404")
    check("删不存在的要可见报警", "MEMORY_FORGET_MISS" in codes(), str(codes()[-4:]))

    print("\n6) 重置状态**保留**记忆；清空记忆是另一件事")
    before = len(session.memory_entries())
    session.reset()
    check("重置后记忆还在", len(session.memory_entries()) == before,
          "%d → %d" % (before, len(session.memory_entries())))
    session.wipe_memory(origin="user")
    check("清空后为空", session.memory_entries() == [], str(session.memory_entries()))
    check("清空与裁剪可区分",
          "MEMORY_WIPED" in codes() and "MEMORY_TRIMMED" in codes(),
          str(codes()[-4:]))

    print("\n7) 没有指令块 = 什么都不改")
    r4 = session.chat_turn("只是聊聊")
    check("无指令块被说出来", "LLM_NO_ACTION" in codes(), str(codes()[-3:]))
    check("记忆仍为空", session.memory_entries() == [])

    print("\n8) 派生数据走 storage 通道（快照/引擎状态不绕过槽位直接写盘）")
    spy = sys.modules.get("puppethub_plugin_spy_storage")
    check("spy 插件已装入", spy is not None)
    session.send(['add #content text #chan text="通道"'], origin="system")
    writes = spy.WRITES
    check("引擎状态经通道写入",
          any(p.endswith(".puppet/state/state.json") for p in writes), str(writes[-5:]))
    check("记忆本来就经通道",
          any(p.endswith(".puppet/memory/memory.jsonl") for p in writes))
    check("真源写回不经通道（程序资产走命令批，白名单刻意 exclude）",
          not any(p.endswith("/app.puppet") and ".puppet/snapshots/" not in p
                  for p in writes), str(writes[-8:]))
    session.named_snapshot("通道冒烟")
    writes = spy.WRITES
    snaps = [p for p in writes if ".puppet/snapshots/" in p]
    check("快照三件都经通道",
          any(p.endswith("/app.puppet") for p in snaps)
          and any(p.endswith("/capabilities.py") for p in snaps)
          and any(p.endswith("/meta.json") for p in snaps), str(snaps[-6:]))
    check("重置/清空也走通道",
          any(p.endswith(".puppet/state") for p in spy.REMOVES)
          and any(p.endswith(".puppet/memory") for p in spy.REMOVES), str(spy.REMOVES))
    check("快照列表经通道读回",
          any(s.kind == "named" and s.reason == "通道冒烟" for s in app.list_snapshots()),
          str([(s.kind, s.reason) for s in app.list_snapshots()][-3:]))

    print("\n最后 6 条诊断：")
    for entry in list(session.log)[-6:]:
        print("   %-20s %s" % (entry["code"], entry["text"][:76]))

    print("\n%s（%d 项失败）" % ("全部通过" if not failures else "有失败", len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
