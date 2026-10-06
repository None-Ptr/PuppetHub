"""watch 块冒烟：把 cron/阈值交给 LLM 自设，并接进自主回路。

依据 `docs/discuss-sensing.md` §4 与 §8。验的是**边界与触发语义**，不是"能跑"：

1. **when-only 上升沿**：值跨过边界才唤醒；持续越界不重复唤醒（首评建基线）
2. **when-only 不触发**：条件一直不满足 → 不唤醒
3. **every-only 定时器**：到点必唤醒（每 tick 一次），无 when 不看条件
4. **every + when**：到点才求值，真才唤醒；值回落后再次上升仍会唤醒
5. **非自主当值不唤醒**：人在驱动时看门狗不插嘴；切回自主上升沿才唤醒
6. **边界可见**：every < 30s → WATCH_REJECTED；复杂 when → WATCH_INVALID；满 8 个 → 第 9 个拒绝
7. **落盘**：watch 写入 `.puppethub/watches.json`
8. **合并**：同帧多个 when 同时上升 → 合并成一次唤醒

用法：python docs/smoke-watch.py
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "Puppet"))

from puppethub.appdir import create_app          # noqa: E402
from puppethub.session import Session            # noqa: E402
from puppethub.watch import Watcher             # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("✓" if ok else "✗", name,
                         ("—— " + str(detail)[:200]) if (detail and not ok) else ""))
    if not ok:
        FAILED.append(name)


class _FakeChat:
    halted = False


class _FakeRunner:
    """假的自主回路：只记录 step 触发的 trigger，不真跑 LLM。"""

    def __init__(self):
        self.triggers = []
        self.chat = _FakeChat()

    def step(self, trigger):
        self.triggers.append(trigger)

    # session._note 对"错误"级会回调 autonomous.on_diagnostic；假回路给 no-op。
    def on_diagnostic(self, *a, **k):
        pass

    def on_interaction(self, *a, **k):
        pass

    def on_state_change(self, *a, **k):
        pass


PROGRAM = [
    'add #root window #win title="watch 冒烟" w=400 h=300',
    'add #win col #main pad=12 gap=8',
    'add #main text #t text="hi"',
    'add #main text #n text="50"',
    'add #main text #count text=str(count(#items))',
    'data #items = [1, 2] of {n: int}',
]

SLEEP = 0.08  # 等守护线程把 trigger 写进 recorder


def _reset_watches(session):
    """清空 watches.json 并重装看门狗（每个用例独立）。"""
    session.storage.write_json(".puppethub/watches.json", [])
    session.watcher.load()
    session.autonomous.triggers.clear()


def _set_text(session, nid, value):
    """走真实命令批改节点属性（解析成表达式，value_of 才能正确求值）。"""
    session.send(['set #%s text="%s"' % (nid, value)], origin=session.writer)


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="puppethub-smoke-watch-"))
    app = create_app(work, "watch", "watch 冒烟")
    app.write_source(PROGRAM, origin="system")

    session = Session(app)
    session.start()
    # 测试环境未必有 LLM provider → 自主回路可能没建。手动装上假的，保证看门狗可武装。
    rec = _FakeRunner()
    session.autonomous = rec
    session.watcher = Watcher(session)
    session.watcher.load()
    session.set_writer("autonomous", origin="user")

    print("1) when-only 上升沿：跨过边界才唤醒，持续越界不重复唤醒")
    _reset_watches(session)
    ok, _ = session.set_watch("when: #n.text < 10\nwhy: 数字低于 10", origin="llm")
    check("watch 设定成功", ok)
    session.refresh()                                   # 首评建基线（n=50→假，无触发）
    check("首评建基线不触发", len(rec.triggers) == 0, rec.triggers)
    _set_text(session, "n", "20")                       # 仍不满足
    session.refresh()
    check("条件不满足不触发", len(rec.triggers) == 0, rec.triggers)
    _set_text(session, "n", "3")                        # 上升沿：满足
    session.refresh()
    time.sleep(SLEEP)
    check("上升沿触发一次", len(rec.triggers) == 1, rec.triggers)
    session.refresh()                                   # 持续满足：不重复
    time.sleep(SLEEP)
    check("持续满足不重复触发", len(rec.triggers) == 1, rec.triggers)

    print("\n2) when-only 不触发：条件一直不满足")
    _reset_watches(session)
    session.set_watch("when: #n.text < 1\nwhy: 数字低于 1", origin="llm")
    _set_text(session, "n", "50")
    session.refresh()
    session.refresh()
    check("条件不满足从不触发", len(rec.triggers) == 0, rec.triggers)

    print("\n3) every-only 定时器：到点必唤醒（直接模拟 tick）")
    _reset_watches(session)
    session.set_watch("every: 5m\nwhy: 每 5 分钟巡检", origin="llm")
    wid = session.watcher.watches[0]["id"]
    session.watcher._tick(wid)
    time.sleep(SLEEP)
    check("every-only 到点唤醒", len(rec.triggers) == 1, rec.triggers)
    session.watcher._tick(wid)
    time.sleep(SLEEP)
    check("every-only 每 tick 都唤醒（无 when 不看条件）",
          len(rec.triggers) == 2, rec.triggers)

    print("\n4) every + when：到点才求值，真才唤醒；回落后再次上升仍唤醒")
    _reset_watches(session)
    session.set_watch("every: 5m\nwhen: #n.text < 10\nwhy: 到点看一眼数字", origin="llm")
    wid = session.watcher.watches[0]["id"]
    # 定时器读的是「最近一次 refresh 的快照」——真实运行里 refresh 每帧都在跑，
    # 这里手动 refresh 喂快照，等价模拟。
    _set_text(session, "n", "20")                       # 到点但条件不满足
    session.refresh()
    time.sleep(SLEEP)
    session.watcher._tick(wid)
    time.sleep(SLEEP)
    check("到点但条件不满足不唤醒", len(rec.triggers) == 0, rec.triggers)
    _set_text(session, "n", "3")                        # 到点且条件满足
    session.refresh()
    time.sleep(SLEEP)
    session.watcher._tick(wid)
    time.sleep(SLEEP)
    check("到点且条件满足唤醒", len(rec.triggers) == 1, rec.triggers)
    session.watcher._tick(wid)                         # 保持满足：不重复（边沿）
    time.sleep(SLEEP)
    check("保持满足不重复唤醒", len(rec.triggers) == 1, rec.triggers)
    _set_text(session, "n", "50")                       # 回落
    session.refresh()
    time.sleep(SLEEP)
    session.watcher._tick(wid)
    time.sleep(SLEEP)
    _set_text(session, "n", "2")                        # 再上升
    session.refresh()
    time.sleep(SLEEP)
    session.watcher._tick(wid)
    time.sleep(SLEEP)
    check("回落后再上升再次唤醒", len(rec.triggers) == 2, rec.triggers)

    print("\n5) 非自主当值不唤醒；切回自主上升沿才唤醒")
    _reset_watches(session)
    session.set_watch("when: #n.text < 10\nwhy: 数字低于 10", origin="llm")
    session.set_writer("llm", origin="user")
    _set_text(session, "n", "1")                        # 共作者当值：不唤醒
    session.refresh()
    time.sleep(SLEEP)
    check("共作者当值：when 不唤醒", len(rec.triggers) == 0, rec.triggers)
    session.set_writer("autonomous", origin="user")     # 切回自主
    _set_text(session, "n", "50")                       # 基线：假
    session.refresh()
    time.sleep(SLEEP)
    check("切回自主先建基线（不触发）", len(rec.triggers) == 0, rec.triggers)
    _set_text(session, "n", "3")                        # 上升沿
    session.refresh()
    time.sleep(SLEEP)
    check("切回自主后上升沿触发", len(rec.triggers) == 1, rec.triggers)

    print("\n6) 边界可见：间隔过短 / 复杂 when / 满 8 个")
    _reset_watches(session)
    ok, diag = session.set_watch("every: 5s\nwhy: 太密", origin="llm")
    check("every 5s 被拒（WATCH_REJECTED）",
          (not ok) and diag and diag["code"] == "WATCH_REJECTED", diag)
    ok, diag = session.set_watch("when: #a.value > #b.value\nwhy: 复杂", origin="llm")
    check("复杂 when 被拒（WATCH_INVALID）",
          (not ok) and diag and diag["code"] == "WATCH_INVALID", diag)
    ok, diag = session.set_watch("foo bar baz\nwhy: 损坏", origin="llm")
    check("无 every/when 被拒（WATCH_INVALID）",
          (not ok) and diag and diag["code"] == "WATCH_INVALID", diag)
    _reset_watches(session)
    for i in range(8):                                  # 填满 8 个
        session.set_watch("when: #n.text < %d\nwhy: 第%d个" % (100 + i, i), origin="llm")
    check("已装满 8 个 watch", len(session.watcher.watches) == 8,
          len(session.watcher.watches))
    ok, diag = session.set_watch("when: #n.text < 0\nwhy: 第9个", origin="llm")
    check("第 9 个被拒（WATCH_REJECTED）",
          (not ok) and diag and diag["code"] == "WATCH_REJECTED", diag)
    ok, _ = session.set_watch("when: #n.text < 100\nwhy: 第0个", origin="llm")  # 同声明=更新
    check("相同声明重设不超上限（原地更新）", ok and len(session.watcher.watches) == 8,
          len(session.watcher.watches))

    print("\n7) 落盘：watch 写入 .puppethub/watches.json")
    _reset_watches(session)
    session.set_watch("every: 30s\nwhen: #n.text < 10\nwhy: 落盘验证", origin="llm")
    saved = session.storage.read_json(".puppethub/watches.json", [])
    check("watches.json 有 1 条", len(saved) == 1, saved)
    if saved:
        check("条目含 id / every / when / why",
              all(k in saved[0] for k in ("id", "every", "every_sec", "when", "why")),
              saved[0])

    print("\n8) 合并：同帧多个 when 同时上升 → 一次唤醒")
    _reset_watches(session)
    session.set_watch("when: #n.text < 10\nwhy: 看 n", origin="llm")
    session.set_watch("when: #n.text < 5\nwhy: 看 n 更严", origin="llm")
    _set_text(session, "n", "50")                       # 两条都不满足（基线）
    session.refresh()
    time.sleep(SLEEP)
    check("基线不触发", len(rec.triggers) == 0, rec.triggers)
    _set_text(session, "n", "3")                        # 两条都上升（<10 且 <5）
    session.refresh()
    time.sleep(SLEEP)
    check("同帧多触发合并成一次唤醒", len(rec.triggers) == 1, rec.triggers)
    if rec.triggers:
        check("合并触发文案标注多处同时触发", "多处同时触发" in rec.triggers[0],
              rec.triggers[0])

    print("\n%s" % ("失败：" + "、".join(FAILED) if FAILED else "全部通过"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
