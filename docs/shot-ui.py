"""UI 截图工具：**一次进程只截一张**（实测唯一的可靠姿势）。

用法：
    python docs/shot-ui.py <状态> [输出目录]
    python docs/shot-ui.py --all [输出目录]

状态：empty · observe · program · caps · spec · ops · collapsed · chat

**为什么一次一进程**：`page.take_screenshot()` 在同一进程里反复调用会**重复返回
同一帧**（旧图）——本机实测，连调试用的洋红/绿都不出现。所以一个状态 = 一次
全新进程 = 一张图；`--all` 由外层循环逐个拉起。
"""
import asyncio
import sys
import tempfile
from pathlib import Path

import flet as ft

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATES = ("empty", "observe", "program", "caps", "spec", "ops", "tools",
          "settings", "edit", "collapsed", "chat")
VIEW_OF = {"observe": "观察", "program": "程序", "caps": "能力",
           "spec": "规格", "ops": "操作", "tools": "工具", "collapsed": None}


def main() -> None:
    argv = [a for a in sys.argv[1:]]
    if not argv or argv[0] == "--all":
        for state in STATES:
            run_one(state, argv[1] if len(argv) > 1 else None)
        return
    run_one(argv[0], argv[1] if len(argv) > 1 else None)


def run_one(state: str, out_dir: str | None) -> None:
    from puppethub.appdir import create_app
    from puppethub.session import Session
    from puppethub.window import HubWindow

    out = Path(out_dir) if out_dir else ROOT / ".codebuddy" / "shots"
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="ui-shot-"))
    app = create_app(work / "w", "myapp", "待办 · 演示")
    session = Session(app)
    hub = HubWindow(session)

    async def shot(page: ft.Page):
        hub._main(page)
        # 有内容的假历史（否则看到的是"空态"，那是另一个状态）
        if state != "empty" and session.chat is not None:
            session.chat._append_history("user", "做一个待办清单")
            session.chat._append_history(
                "assistant",
                "好，先立窗口与清单容器：\n"
                "```puppet\nadd #content list #items source=#todos template=#tpl\n```\n"
                "数据源随后声明。")
            session.chat._append_history("user", "再加一个添加按钮，输入框在上面")
        session.note("info", "SESSION", "已装载 myapp · 4 个节点 · 0 项能力")
        session.note("warning", "LLM_STUCK",
                     "同一诊断（含位置）在最近两轮里重复出现——换个写法")
        session.note("error", "WRITER_DENIED", "当前写者是 autonomous，llm 的写入被拒绝")
        if state in VIEW_OF:
            hub.cockpit._set_view(VIEW_OF[state])
        if state == "settings":
            hub.cockpit.tools.settings()          # 模型设置浮层
        if state == "edit":
            hub.cockpit.tools.human_edit()        # 人的写入浮层
        if state == "chat":
            session.chat._append_history("assistant", "已加好按钮与输入框，试试点击。")
        for _ in range(3):                       # 让它彻底落定（不许看半帧）
            hub.repaint()
            await asyncio.sleep(0.7)
        image = await hub.page.take_screenshot()
        if image:
            path = out / ("ui-%s.png" % state)
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


if __name__ == "__main__":
    main()
