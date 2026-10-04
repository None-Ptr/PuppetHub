"""窗口路径冒烟：真开一次 flet 窗口（**隐藏**），走完 `HubWindow` 的全部粘合。

与 `smoke-render.py` / `smoke-llm.py` 的分工：

- `smoke-render.py` 不启动 flet 运行时，只验证"值 → 控件"的映射；
- `smoke-llm.py` 不碰界面，只验证对话回路的机制；
- **这个**启动真实事件循环，验证 `page.add` / `page.update` / 后台线程跑 LLM /
  流式上屏 / 驾驶舱刷新 / 关窗拦截这条链路。

`FLET_APP_HIDDEN`（spike 2 实测可用）让它不弹窗；跑完自动关窗退出。
用法：python docs/smoke-window.py [app 目录]
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import flet as ft  # noqa: E402

from puppethub.appdir import AppDir, create_app  # noqa: E402
from puppethub.session import Session  # noqa: E402
from puppethub.window import HubWindow  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fake_provider.py"

BATCH = [
    'add #content text #hello text="你好" size=20',
    'add #content row #tools gap=8',
    'add #tools button #b1 text="加一条"',
    'add #tools text #count text="0 条"',
    'data #todos = [] of {text: str}',
    'on #b1 click:',
    '    append #todos item={text: "新条目"}',
    'on #todos change:',
    '    set #count text=str(count(#todos)) + " 条"',
]

# 对话那一轮必须是**另一批**（同一批再来一次只会得到一串 ID_DUP）。
REPLY = ('好，再加一句说明。\n\n```puppet\n'
         'add #content text #note text="由共作者添加" fg=#64748b\n```\n')


def make_app() -> tuple[AppDir, Path]:
    """临时 app + 临时插件目录（假 provider 走的是真实插件的发现路径）。"""
    work = Path(tempfile.mkdtemp(prefix="puppethub-window-"))
    plugins = work / "plugins"
    plugins.mkdir()
    shutil.copyfile(FIXTURE, plugins / "fake_provider.py")
    script = work / "replies.json"
    script.write_text(json.dumps([REPLY], ensure_ascii=False), encoding="utf-8")
    os.environ["PUPPETHUB_PLUGINS"] = str(plugins)
    app = create_app(work / "app", "win-smoke", "窗口冒烟")
    app.config_path.write_text(
        'llm_provider = "fake"\n\n[plugins.fake]\nscript = "%s"\n' % script.as_posix(),
        encoding="utf-8")
    return app, work


def main() -> int:
    app, _work = make_app() if len(sys.argv) <= 1 else (AppDir(sys.argv[1]), None)
    session = Session(app)
    hub = HubWindow(session)

    async def gui(page: ft.Page):
        hub._main(page)                      # 与 `puppethub run` 走同一条路
        print("窗口路径：page.controls=%d · 顶层控件=%d"
              % (len(page.controls), len(hub.renderer.host.controls)))

        session.send(BATCH, origin="driver")
        session.fire("b1", "click")
        session.fire("b1", "click")
        hub.repaint()
        attrs = session.engine.observe()["attrs"]
        print("交互：#count.text = %r · #todos 行数 = %d"
              % (attrs.get("#count.text"), len(session.engine.items.get("todos", []))))

        if session.chat is not None and type(session.chat.provider).__name__ == "FakeProvider":
            print("对话：在后台线程里跑一轮（%s）" % session.plugin_chain())
            hub.cockpit.submit("做一个待办清单")
            for _ in range(200):             # 等后台线程跑完（界面线程不能阻塞它）
                if not hub.cockpit._busy:
                    break
                await asyncio.sleep(0.05)
            history = session.chat.history()
            last = history[-1] if history else {}
            print("对话：最后一轮 role=%s 长度=%d；消息数=%d；真源 %d 行"
                  % (last.get("role"), len(last.get("text") or ""),
                     len(session.chat.last_messages), len(app.read_source())))
            print("对话：本轮结果 = %s" % "、".join(
                entry["text"][:48] for entry in list(session.log)[-3:]))

            # **人工接管的运行期入口**：卡住/停手必须在界面上带一个解开的开关。
            session.chat.stuck_note = "（冒烟注入的卡住信号）"
            hub.repaint()
            print("接管栏可见（卡住时）= %s" % hub.cockpit.takeover_row.visible)
            session.resume_chat(origin="user")
            hub.repaint()
            print("接管后：stuck_note=%r · 接管栏=%s · LLM_RESUME=%s"
                  % (session.chat.stuck_note, hub.cockpit.takeover_row.visible,
                     any(e["code"] == "LLM_RESUME" for e in session.log)))

        errors = [e for e in session.log if e["level"] == "错误"]
        print("错误数 = %d" % len(errors))
        for entry in errors:
            print("   %s %s" % (entry["code"], entry["text"][:100]))
        print("关窗拦截已安装 = %s（脏状态要在关窗前拦一道）"
              % bool(page.window.prevent_close and page.window.on_event))
        page.window.prevent_close = False    # 冒烟里直接放行，免得自己拦住自己
        await page.window.destroy()

    ft.run(gui, view=ft.AppView.FLET_APP_HIDDEN)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
