"""端到端冒烟：把"装载 → 渲染 → 命令批 → 写回真源 → 交互 → 再渲染"跑一遍。

为什么需要它：`puppethub run` 会弹出窗口并阻塞，没法在无人值守下验证渲染层。
本脚本**不启动 flet 运行时**，只用一个真实的 `ft.Page`（无会话）跑完整条回路——
它验证的是风险最高的那部分：值从引擎到控件的映射、模板行展开、覆盖层、事件回填。

用法：python docs/smoke-render.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import flet as ft  # noqa: E402

from puppethub.appdir import create_app  # noqa: E402
from puppethub.render import FletRenderer  # noqa: E402
from puppethub.session import Session  # noqa: E402

BATCH = [
    'add #content text #hello text="你好" size=20',
    'add #content button #b1 text="点我" icon=add',
    'add #content list #rows source=#todos template=#tpl',
    'add #content dialog #dlg bgcolor=#ffffff',
    'add #dlg text #ask text="确定要删除吗？"',
    'add #root template #tpl as t',
    'add #tpl row #line gap=8',
    'add #tpl text #label text=t.text flex=1',
    'data #todos = [{text: "写文档"}, {text: "跑用例"}] of {text: str}',
    'on #b1 click:',
    '    set #hello text="已点击"',
    '    set #dlg visible=true',
]


class _Sess:
    """`ft.Page` 需要一个**可弱引用**的 session 对象；无会话时它只是个占位。"""


def dump(session: Session, title: str) -> None:
    print("\n== %s ==" % title)
    for entry in list(session.log)[-12:]:
        print("   %-4s %-22s %s" % (entry["level"], entry["code"], entry["text"][:96]))


def tree(control, depth: int = 0) -> list:
    """打印 flet 控件树（"界面真的重绘了"要能被看见，而不是只看计数）。"""
    if depth > 5:
        return []
    value = getattr(control, "value", None)
    detail = " value=%r" % (value,) if isinstance(value, (str, int, float, bool)) else ""
    visible = "" if getattr(control, "visible", True) else " [hidden]"
    lines = ["  " * depth + type(control).__name__ + detail + visible]
    children = getattr(control, "controls", None)
    if children:
        for child in children:
            lines += tree(child, depth + 1)
    elif getattr(control, "content", None) is not None and depth < 3:
        lines += tree(control.content, depth + 1)
    return lines


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="puppethub-smoke-")
    app = create_app(Path(tmp), "smoke", "冒烟")
    page = ft.Page(sess=_Sess())
    session = Session(app)
    renderer = FletRenderer(page, session.engine, on_event=lambda *a: None,
                            assets_dir=str(app.assets_dir))
    session.renderer = renderer

    diags = session.start()
    session.refresh()
    print("骨架装载：%d 条诊断，顶层 %d 个控件" % (len(diags), len(renderer.host.controls)))
    dump(session, "装载后")

    session.send(BATCH, origin="driver")
    session.refresh()
    print("\n真源写回（%d 行）：" % len(app.read_source()))
    print("\n".join("   " + line for line in app.read_source()))
    print("顶层控件数：%d" % len(renderer.host.controls))
    print("控件树：")
    for line in tree(renderer.host):
        print("   " + line)
    dump(session, "命令批后")

    session.fire("b1", "click")
    session.refresh()
    attrs = session.engine.observe().get("attrs", {})
    print("\n点击后 #hello.text = %r" % (attrs.get("#hello.text"),))
    print("覆盖层（dialog）：%d 个，可见 = %s"
          % (len(page.overlay), getattr(page.overlay[-1], "open", None) if page.overlay else "-"))
    print("窗口：title=%r %dx%d · 主题主色=%s"
          % (page.title, page.window.width, page.window.height,
             getattr(page.theme, "color_scheme_seed", None)))

    snapshots = app.list_snapshots()
    print("自动快照：%d 份（%s）" % (len(snapshots), snapshots[0].reason if snapshots else "-"))
    errors = [e for e in session.log if e["level"] == "错误"]
    print("错误数：%d" % len(errors))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
