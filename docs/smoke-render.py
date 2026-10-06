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

    # 输入框跨帧复用：打字的 on_change 正是 repaint 的触发者，若每帧重建输入框，
    # 上一个字触发的重绘就把正打的字连焦点一起换掉（实测"一输入就消失"）。
    # 机制断言：连续多帧 apply，输入框必须是**同一个 Python 实例**（uid 不变，
    # flet 只 diff 属性）；程序外部改值时复用实例的值要跟新。
    print("\n输入框跨帧复用：")
    session.send(["add #content input #name placeholder=\"名字\" pad=4"], origin="driver")
    session.refresh()
    first = next(iter(renderer._inputs.values()), None)
    first_outer = renderer._input_outer.get(("name", None))
    session.fire("name", "change", None, "小明")
    session.refresh()
    second = next(iter(renderer._inputs.values()), None)
    second_outer = renderer._input_outer.get(("name", None))
    typed_value = getattr(second, "value", None)   # 立即读：second/third 是同一对象
    session.send(["set #name value=\"外部值\""], origin="driver")
    session.refresh()
    third = next(iter(renderer._inputs.values()), None)
    third_outer = renderer._input_outer.get(("name", None))
    ok_input = (first is not None and first is second is third
                and first_outer is second_outer is third_outer
                and typed_value == "小明"
                and getattr(third, "value", None) == "外部值")
    print("   三帧同一实例：%s · 外层同一实例：%s · 打字后 value=%r · 外部改值后 value=%r"
          % (first is second is third, first_outer is second_outer is third_outer,
             typed_value, getattr(third, "value", None)))
    print("   结论：%s" % ("复用生效" if ok_input else "复用未生效"))

    # 焦点恢复：重绘整树重建会把 flet 焦点夺走（实测"点进去打不了字 /
    # 打一个字就断"）——渲染器记下谁在焦点，宿主 page.update() 后
    # restore_focus 通过 page.run_task 把焦点还回去。
    print("\n焦点恢复：")
    focus_calls = []

    class _SpyPage:
        """只截 `run_task`；其余（如 `_apply_dialogs` 要读的 overlay）代理真 page。"""

        def __init__(self, real):
            self._real = real

        @property
        def overlay(self):
            return self._real.overlay

        def run_task(self, coro_fn, *args):
            focus_calls.append(coro_fn)

    saved_page = renderer.page
    renderer.page = _SpyPage(saved_page)
    second.on_focus()
    tracked = renderer._focused_key
    renderer.restore_focus()
    # 焦点期间外部改值**不得**写进部件（flet 对焦点中的 TextField 改 value
    # 会把整段文本全选，实测"一输入就全选"）；且**整树冻结**（祖先链每帧新建
    # 会换父=重挂载=丢焦点，refocus 再全选——探针实证），失焦后下一帧对齐。
    frozen = renderer.host.controls
    session.send(["set #name value=\"焦点外改值\""], origin="driver")
    session.refresh()
    during_focus = second.value
    frozen_held = renderer.host.controls is frozen
    second.on_blur()
    tracked_after_blur = renderer._focused_key
    renderer.restore_focus()
    session.refresh()
    after_blur = second.value
    unfrozen = renderer.host.controls is not frozen
    renderer.page = saved_page
    ok_focus = (tracked == ("name", None) and len(focus_calls) == 1
                and tracked_after_blur is None and len(focus_calls) == 1)
    ok_focus_skip = (during_focus == "外部值" and after_blur == "焦点外改值"
                     and frozen_held and unfrozen)
    print("   聚焦后记下 %r · 恢复调度 %d 次 · 失焦后清掉：%s"
          % (tracked, len(focus_calls), tracked_after_blur is None))
    print("   结论：%s" % ("焦点恢复生效" if ok_focus else "焦点恢复未生效"))
    print("\n焦点期间整树冻结：")
    print("   焦点中 value=%r（应保持聚焦前的外部值）· 失焦后 value=%r（应对齐外部改值）"
          % (during_focus, after_blur))
    print("   焦点中整树未动：%s · 失焦后重建：%s"
          % (frozen_held, unfrozen))
    print("   结论：%s" % ("冻结/解冻生效" if ok_focus_skip else "冻结/解冻未生效"))

    errors = [e for e in session.log if e["level"] == "错误"]
    print("错误数：%d" % len(errors))
    return 1 if (errors or not ok_input or not ok_focus or not ok_focus_skip) else 0


if __name__ == "__main__":
    raise SystemExit(main())
