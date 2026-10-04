"""首页截图工具：**一次进程只截一张**（与 `shot-ui.py` 同一条实测纪律——
同进程重复 `take_screenshot()` 只会回旧帧）。

用法：
    python docs/shot-home.py <状态> [输出目录]
    python docs/shot-home.py --all [输出目录]

状态：empty · projects · running · bus · chat · denied · nollm · new · opendir · quit

为什么要有它：首页的**界面**以前从未真的渲染过——本项目最密集的 bug 就出在这一层
（竖向预算手算溢出 38px、`rule()` 带 expand 变成灰柱，都是截图发现的）。逻辑有冒烟，
**外观只能看图**。

它用**真的** Society / SystemOps / HomeWindow 驱动，喂进预置的 MRU 与账本来摆状态；
"在跑"那条用一个真应答 `{"protocol": "1"}` 的假监听来撑（不做假数据）。
"""
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

import flet as ft

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATES = ("empty", "projects", "running", "bus", "chat", "denied",
          "nollm", "new", "opendir", "quit")

TALK = [
    ("user", "把 alpha 和 beta 连起来：beta 缺数据就问 alpha 要。"),
    ("assistant", "› 先把两个都拉起来，再给它们建一条话题。"),
    ("info", "up 已拉起 alpha → 127.0.0.1:9401（pid 8123），等握手…"),
    ("info", "up alpha 握手成功"),
    ("info", "tell 投递给 topic=data 的订阅者；`beta` 只是意图标注"),
]


class FakeProvider:
    """只为把调度官面板撑起来（不联网、不调模型）。"""

    def __init__(self):
        self.seen = []

    def stream(self, messages):
        self.seen.append(messages)
        yield "（截图工具：不真的调模型。）"

    def context_limit(self):
        return None


def _listen(port: int) -> socket.socket:
    """一个真应答 `{"protocol": "1"}` 的假实例——让"在跑"是**真的**探出来的。"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(4)

    def loop():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.makefile("r", encoding="utf-8").readline()
                    conn.sendall((json.dumps({"protocol": "1", "rendering": {},
                                              "catalog": [], "service": {}}) + "\n")
                                 .encode("utf-8"))
                except OSError:
                    pass

    threading.Thread(target=loop, daemon=True).start()
    return server


def _free(start: int) -> int:
    port = start
    while True:
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
            probe.close()
            return port
        except OSError:
            probe.close()
            port += 1


def run_one(state: str, out_dir: str | None) -> None:
    home = Path(tempfile.mkdtemp(prefix="home-shot-home-"))
    os.environ["PUPPETHUB_HOME"] = str(home)

    from puppethub import recent
    from puppethub.appdir import AppDir, create_app
    from puppethub.home import HomeWindow
    from puppethub.orchestrator import Orchestrator
    from puppethub.society import Society, SocietyOps

    out = Path(out_dir) if out_dir else ROOT / ".codebuddy" / "shots"
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="home-shot-work-"))
    alpha = create_app(work, "alpha", "Alpha")
    beta = create_app(work, "beta", "Beta")
    ghost = work / "notes"                 # 存在但不是 app
    ghost.mkdir()
    gone = work / "old-experiment"         # 已不在

    society = Society(log=lambda *_: None)
    live_a, live_b = _free(9411), _free(9421)
    keep = [_listen(live_a), _listen(live_b)]
    ops = SocietyOps(society, log=lambda *_: None,
                     config={"plugins": {"openai-compat": {}}})

    if state != "empty":
        for path in (alpha.root, beta.root, ghost, gone):
            recent.record(path)

    if state in ("running", "bus", "denied", "quit"):
        society.start()
        data = society.ledger()
        data["apps"]["alpha"] = {"root": str(alpha.root), "port": live_a, "pid": 8123}
        data["windows"]["beta"] = {"root": str(beta.root), "port": live_b, "pid": 8124}
        data["apps"]["old-experiment"] = {"root": str(gone), "port": 9, "pid": 8125}
        if state == "running":
            # 同名并存：无头 + 窗口（首页要**并列显示**这两条）
            data["windows"]["alpha"] = {"root": str(alpha.root), "port": live_b,
                                        "pid": 8126}
        society._save(data)
        if state in ("running", "quit"):
            society._windows["beta"] = 8124        # 本页开的那个
        for index in range(4 if state == "bus" else 1):
            with open(society.bus_audit_path(), "a", encoding="utf-8") as fh:
                if index % 2 == 0:
                    fh.write(json.dumps({"time": "2026-10-04 19:2%d:0%d" % (index, index),
                                         "from": "alpha", "topic": "data",
                                         "title": "今日进度",
                                         "text": "今天的数还没出来，先别下结论。"},
                                        ensure_ascii=False) + "\n")
                else:
                    fh.write(json.dumps({"time": "2026-10-04 19:2%d:0%d" % (index, index),
                                         "from": "alpha", "topic": "data",
                                         "delivery": {"ok": ["beta"], "failed": []}},
                                        ensure_ascii=False) + "\n")

    orchestrator = None
    if state != "nollm":
        orchestrator = Orchestrator(
            ops, log=lambda *_: None, provider=FakeProvider(),
            raw_config={"autonomous": {"enable": True, "reflect_every": 0,
                                       "allow": ["up", "tell"]}})
    window = HomeWindow(society, ops, orchestrator)

    def shot_sync(page: ft.Page) -> None:
        window._main(page)
        window._probing = True                     # 冻住 2s 探活，别把摆好的状态盖掉

        if state != "empty":
            window.rows = society.rows()
        for kind, text in (TALK if state in ("chat", "denied", "bus") else TALK[:2]):
            window._note_transcript(kind, text)
        if state == "denied":
            window._note_transcript("warning",
                                    "up `open` 未列入自主白名单（[autonomous] allow），"
                                    "自主时默认拒绝——危险动作只能人手动授权")
        if state in ("chat", "denied"):
            if window.composer is not None:
                window.composer.value = "顺手把它们的日志也接上"
        if state == "new":
            window._new_app()
        if state == "opendir":
            window._open_dir()
        if state == "quit":
            window.rows = society.rows()
            window._request_close()                # 有实例在跑 → 弹"社会层将下线"的确认
        window._reload()
        window._render_status()
        window._render_bus()
        page.update()

    async def shot(page: ft.Page):
        shot_sync(page)
        for _ in range(3):                         # 让它彻底落定（不许看半帧）
            await asyncio.sleep(0.6)
        page.update()
        image = await page.take_screenshot()
        if image:
            path = out / ("home-%s.png" % state)
            path.write_bytes(image)
            print("saved:", path, len(image))
        else:
            print("capture failed:", state)
        await asyncio.sleep(0.2)
        try:
            page.window.prevent_close = False
            await page.window.destroy()
        except Exception as ex:  # noqa: BLE001
            print("close:", ex)

    ft.run(shot, view=ft.AppView.FLET_APP)
    for server in keep:
        try:
            server.close()
        except OSError:
            pass


def main() -> None:
    argv = list(sys.argv[1:])
    if not argv or argv[0] == "--all":
        for state in STATES:
            run_one(state, argv[1] if len(argv) > 1 else None)
        return
    run_one(argv[0], argv[1] if len(argv) > 1 else None)


if __name__ == "__main__":
    main()
