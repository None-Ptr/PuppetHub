"""首页窗口：**左社会台 + 右调度官面板**（`docs/design-home.md`）。

它**没有 `Session`**——这是刻意的：首页永不写任何 app 的真源，所以它连"能写"的
对象都不该手里有。它只有 `SocietyOps`（白名单）与一个调度官。

布局与纪律沿驾驶舱：顶栏同构、面板同宽（`PANEL_WIDTH`）、转录无气泡、
琥珀只给"警告 + 选中行反白块"、全篇零圆角零阴影零动效。

**运行态措辞不断言"没在跑"**：`○ 未编排` 的意思是"我不知道"——窗口实例不监听
端口，首页看不见外部手工开的窗口（这是显式选择的盲区）。
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import flet as ft

from . import recent, theme
from .cockpit import PANEL_WIDTH
from .orchestrator import TurnResult

WINDOW_WIDTH = PANEL_WIDTH + 560
WINDOW_HEIGHT = 700
LIST_MIN_WIDTH = 520
BUS_TAIL_LINES = 5
PROBE_INTERVAL = 2.0

HINTS = ("↑↓ 选择 · Enter 打开 · n 新建 · o 打开目录 · u 拉起 · d 停止 · "
         "f 遗忘 · m 自主 · r 重读 · q 退出")

KEYS_UP = ("arrowup", "arrow up", "up")
KEYS_DOWN = ("arrowdown", "arrow down", "down")


class HomeWindow:
    def __init__(self, society, ops, orchestrator=None):
        self.society = society
        self.ops = ops
        self.orchestrator = orchestrator
        self.page: ft.Page | None = None
        self.entries: list = []
        self.rows: list = []
        self.index = 0
        self._events: list = []
        self._probing = False
        self._typing = False
        self._stream = ""
        self._closing = False
        # 控件
        self.list_view = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO)
        self.bus_view = ft.Column(spacing=0)
        self.status = theme.mono("", size=theme.SIZE_MICRO, color=theme.DIM,
                                 selectable=False)
        self.transcript = ft.Column(spacing=2, scroll=ft.ScrollMode.AUTO,
                                    auto_scroll=True)
        self.composer = None
        self.orchestrator_state = theme.mono("", size=theme.SIZE_MICRO,
                                             color=theme.DIM, selectable=False)
        self.model_state = theme.mono("", size=theme.SIZE_MICRO, color=theme.DIM,
                                      selectable=False)

    # ------------------------------------------------------------ 启动

    def run(self) -> None:
        ft.run(self._main, view=ft.AppView.FLET_APP)

    def _main(self, page: ft.Page) -> None:
        self.page = page
        page.title = "PuppetHub · 首页"
        page.padding = 0
        page.spacing = 0
        page.bgcolor = theme.BG
        page.theme_mode = ft.ThemeMode.DARK
        page.theme = theme.page_theme()
        page.enable_screenshots = True
        try:
            page.window.width = WINDOW_WIDTH
            page.window.height = WINDOW_HEIGHT
        except Exception:  # noqa: BLE001 - 平台不支持就算了（尺寸只是偏好）
            pass
        self._install_close_guard(page)
        page.add(ft.Column(expand=True, spacing=0, controls=[
            self._topbar(),
            theme.rule(),
            ft.Row(expand=True, spacing=0,
                   controls=[self._left(), self._right()]),
        ]))
        if self.orchestrator is not None:
            # 总线消息 → 调度官的刺激源（社会层的观察者）；流式回调接进事件泵。
            self.society.on_peer_message = self.orchestrator.notify
            self._install_autonomous_delta()
            self.orchestrator.start_autonomous()
        self._reload(reason="启动")
        self._note_transcript("系统", "首页 = 社会层宿主（总线在**本进程**内）。" if self.orchestrator is not None
                              else "没有调度官：社会台照常可用（键盘与按钮都在）。")
        page.on_keyboard_event = self._on_key
        page.run_task(self._pump)
        page.run_task(self._periodic)
        page.update()

    def _topbar(self) -> ft.Container:
        return ft.Container(
            height=26, bgcolor=theme.BG, padding=ft.Padding(10, 0, 10, 0),
            content=ft.Row(spacing=8, controls=[
                theme.mono("PUPPETHUB", size=theme.SIZE_MICRO, color=theme.AMBER,
                           selectable=False),
                theme.mono("/", size=theme.SIZE_MICRO, color=theme.RULE,
                           selectable=False),
                theme.mono("首页", size=theme.SIZE_BODY, color=theme.TEXT,
                           selectable=False),
                ft.Container(expand=True),
                self.model_state,
            ]))

    # ------------------------------------------------------------ 左栏：社会台

    def _left(self) -> ft.Container:
        return ft.Container(
            expand=True, bgcolor=theme.BG, padding=ft.Padding(12, 8, 12, 8),
            content=ft.Column(expand=True, spacing=6, controls=[
                theme.section("项目", "打开过的 app（零扫描：列表就是历史）"),
                theme.rule(),
                ft.Container(content=self.list_view, expand=True),
                theme.rule(),
                self.bus_view,
                theme.rule(),
                theme.mono(HINTS, size=theme.SIZE_MICRO, color=theme.DIM,
                           selectable=False),
                self.status,
            ]))

    # ------------------------------------------------------------ 右栏：调度官

    def _right(self) -> ft.Container:
        if self.orchestrator is None:
            body = ft.Column(expand=True, spacing=6, controls=[
                theme.mono("（没有调度官）", size=theme.SIZE_BODY, color=theme.DIM,
                           selectable=False),
                theme.rule(),
                theme.markup(self._unavailable_text(), size=theme.SIZE_DATA),
                theme.mono("· 社会台照常可用：Enter 打开、n 新建、u 拉起、d 停止",
                           size=theme.SIZE_MICRO, color=theme.DIM, selectable=False),
            ])
        else:
            self.composer = theme.field(hint="对调度官说一句话（社会层）",
                                        on_submit=self._submit)
            self.composer.on_focus = self._focus_on
            self.composer.on_blur = self._focus_off
            body = ft.Column(expand=True, spacing=6, controls=[
                ft.Row(spacing=8, controls=[
                    theme.mono("▌ 调度官", color=theme.TEXT,
                               weight=ft.FontWeight.BOLD),
                    ft.Container(expand=True),
                    self.orchestrator_state,
                ]),
                theme.rule(),
                ft.Container(content=self.transcript, expand=True),
                theme.rule(),
                self.composer,
                theme.mono("Enter 发送 · 上方快捷键在输入框未聚焦时生效",
                           size=theme.SIZE_MICRO, color=theme.DIM, selectable=False),
            ])
        return ft.Container(
            width=PANEL_WIDTH, bgcolor=theme.RAISE,
            padding=ft.Padding(12, 8, 12, 8),
            border=ft.Border.only(left=ft.BorderSide(1, theme.RULE)),
            content=body)

    def _unavailable_text(self) -> str:
        error = (getattr(self.orchestrator, "provider_error", "")
                 or getattr(self, "provider_error", ""))
        if not error:
            error = "没有可用的 llm_provider"
        return "调度官不可用：\n%s" % error

    # ------------------------------------------------------------ 键盘

    def _focus_on(self, _event=None) -> None:
        self._typing = True

    def _focus_off(self, _event=None) -> None:
        self._typing = False

    def _on_key(self, event) -> None:
        if self._typing:
            return
        key = str(getattr(event, "key", "") or "").strip().lower()
        if not key:
            return
        if key in KEYS_UP:
            self._move(-1)
        elif key in KEYS_DOWN:
            self._move(1)
        elif key == "enter":
            self._open_selected()
        elif key == "n":
            self._new_app()
        elif key == "o":
            self._open_dir()
        elif key == "u":
            self._act("up")
        elif key == "d":
            self._act("down")
        elif key == "f":
            self._act("forget")
        elif key == "m":
            self._toggle_autonomous()
        elif key == "r":
            self._reload(reason="手动重读")
        elif key == "q":
            self._request_close()

    # ------------------------------------------------------------ 刷新

    def _reload(self, reason: str = "") -> None:
        for problem in recent.problems():      # 读不出来的东西**说出来**，不装作没这回事
            self._note_transcript("warning", problem)
        recent.clear_problems()
        self.entries = [recent.state_of(item["root"]) for item in recent.entries()]
        if self.index >= len(self.entries):
            self.index = max(0, len(self.entries) - 1)
        self._render_list()
        self._render_status()
        self._render_bus()
        if reason and self.page is not None:
            self.page.update()

    def _render_list(self) -> None:
        controls = []
        if not self.entries:
            controls.append(theme.markup(
                "（还没有项目）——`n` 新建一个，或 `o` 打开一个已有目录。",
                size=theme.SIZE_DATA, color=theme.DIM))
        for position, item in enumerate(self.entries):
            controls.append(self._row(position, item))
        self.list_view.controls = controls

    def _row(self, position: int, item: dict) -> ft.Control:
        selected = position == self.index
        state = self._state_text(item)
        if selected:
            return ft.Container(
                bgcolor=theme.AMBER, padding=ft.Padding(6, 3, 6, 3),
                content=ft.Row(spacing=8, controls=[
                    theme.mono("▌ %s" % item["name"], size=theme.SIZE_BODY,
                               color=theme.BG, weight=ft.FontWeight.BOLD,
                               selectable=False),
                    theme.mono(item["root"], size=theme.SIZE_MICRO, color=theme.BG,
                               selectable=False, expand=True),
                    theme.mono(state, size=theme.SIZE_MICRO, color=theme.BG,
                               selectable=False)]),
                on_click=lambda _e, p=position: self._click(p))
        return ft.Container(
            padding=ft.Padding(6, 3, 6, 3),
            content=ft.Row(spacing=8, controls=[
                theme.mono("%s %s" % (item["mark"] or "", item["name"]),
                           size=theme.SIZE_BODY,
                           color=theme.DIM if item["mark"] else theme.TEXT,
                           selectable=False),
                theme.mono(item["root"], size=theme.SIZE_MICRO, color=theme.DIM,
                           selectable=False, expand=True),
                theme.mono(state, size=theme.SIZE_MICRO, color=theme.DIM,
                           selectable=False)]),
            on_click=lambda _e, p=position: self._click(p))

    def _state_text(self, item: dict) -> str:
        """一条项目的运行态。同名可能**同时**有无头实例与窗口 → 并列显示。"""
        if item["mark"]:
            return item["mark"]
        labels = [self._row_state(row) for row in self.rows
                  if row.get("root")
                  and str(Path(row["root"]).resolve()) == item["root"]]
        return " + ".join(labels) if labels else "○ 未编排"

    @staticmethod
    def _row_state(row: dict) -> str:
        """一条实例的措辞。**不出现"没在跑"**——盲区里的东西根本不在账本里。"""
        kind = "本页开" if row.get("mine") \
            else ("无头" if row.get("mode") == "remote" else "窗口")
        if not row.get("alive"):
            return "✕ 无应答（%s）" % kind
        return "● 在跑（%s :%s）" % (kind, row.get("port"))

    def _render_status(self) -> None:
        running = sum(1 for row in self.rows if row.get("alive"))
        bus_port = self.society.bus_port()
        model = "调度官就绪" if (self.orchestrator is not None
                             and self.orchestrator.provider is not None) else "未配置模型"
        self.status.value = ("%s · 共 %d 项 · 在跑 %d · 总线 %s"
                            % (model, len(self.entries), running,
                               ("127.0.0.1:%d" % bus_port) if bus_port else "未上线"))
        self.model_state.value = model
        if self.orchestrator is not None:
            stats = self.orchestrator.stats()
            self.orchestrator_state.value = (
                "自主：%s · 本小时 %d/%d · allow：%s"
                % ("开" if stats["enable"] else "关", stats["steps_last_hour"],
                   stats["budget_per_hour"], "、".join(stats["allow"]) or "空"))

    def _render_bus(self) -> None:
        entries = self.society.bus_tail(BUS_TAIL_LINES)
        controls = [theme.mono("总线（谁对谁说了什么）", size=theme.SIZE_MICRO,
                               color=theme.DIM, selectable=False)]
        if not entries:
            controls.append(theme.mono("（还没有记录：app 之间还没说过话）",
                                       size=theme.SIZE_MICRO, color=theme.DIM,
                                       selectable=False))
        for entry in entries:
            if entry.get("delivery"):
                text = "%s 投递回执 %s → ok=%s failed=%s" % (
                    entry.get("time"), entry.get("from"),
                    entry.get("delivery", {}).get("ok"),
                    entry.get("delivery", {}).get("failed"))
            elif entry.get("error"):
                text = "%s %s" % (entry.get("time"), entry["error"])
            else:
                text = "%s %s [%s] %s" % (entry.get("time"), entry.get("from"),
                                          entry.get("topic"),
                                          (entry.get("text") or "")[:60])
            controls.append(theme.mono(text, size=theme.SIZE_MICRO,
                                       color=theme.DIM, selectable=False))
        self.bus_view.controls = controls

    # ------------------------------------------------------------ 周期探活

    async def _periodic(self) -> None:
        while True:
            await asyncio.sleep(PROBE_INTERVAL)
            if self._probing or self._closing:
                continue
            self._probing = True
            threading.Thread(target=self._probe_once, daemon=True,
                             name="home-probe").start()

    def _probe_once(self) -> None:
        try:
            rows = self.society.rows()
        except Exception as ex:  # noqa: BLE001 - 探活失败必须可见
            rows = []
            self._q("log", ("error", "探活失败：%s" % ex))
        self._q("reload", rows)
        self._probing = False

    # ------------------------------------------------------------ 事件泵

    def _q(self, kind: str, payload=None) -> None:
        self._events.append((kind, payload))

    async def _pump(self) -> None:
        while True:
            await asyncio.sleep(0.15)
            if self.page is None or self._closing:
                continue
            if not self._events:
                continue
            batch, self._events = self._events[:], []
            for kind, payload in batch:
                self._apply(kind, payload)
            self.page.update()

    def _apply(self, kind: str, payload) -> None:
        if kind == "reload":
            self.rows = payload or []
            self._render_list()
            self._render_status()
            self._render_bus()
        elif kind == "log":
            level, text = payload
            self._note_transcript(level, text)
        elif kind == "delta":
            self._stream += payload
            self.transcript.controls[-1].value = "   " + self._stream[-400:]
        elif kind == "turn":
            self._finish_turn(payload)
        elif kind == "after":
            self._reload()
        elif kind == "closing":
            self._destroy()

    # ------------------------------------------------------------ 转录

    def _note_transcript(self, level: str, text: str) -> None:
        color = {"error": theme.RED, "warning": theme.AMBER}.get(level, theme.DIM)
        stamp = time.strftime("%H:%M:%S")
        self.transcript.controls.append(
            theme.mono("%s │ %s" % (stamp, text), size=theme.SIZE_DATA,
                       color=color, selectable=False))

    def _finish_turn(self, result: TurnResult) -> None:
        if self.transcript.controls:
            self.transcript.controls[-1].value = ("   " + self._stream[-400:]) \
                if self._stream else ""
        self._stream = ""
        self.transcript.controls = [c for c in self.transcript.controls
                                    if (c.value or "") != ""]
        if result.error:
            self._note_transcript("error", text=result.error)
        if result.explanation:
            for line in result.explanation.splitlines():
                if line.strip():
                    self._note_transcript("assistant", "› " + line.strip())
        for item in result.actions:
            note = item.get("note") or ""
            ok = item.get("ok", True)
            self._note_transcript("info" if ok else "warning",
                                  "%s %s" % (item.get("verb"), note))
        if not result.actions and not result.error:
            self._note_transcript("info", "本轮回合没有任何动词")
        self._render_status()
        self._reload()

    # ------------------------------------------------------------ 动作

    def _click(self, position: int) -> None:
        self.index = position
        self._render_list()
        self._open_selected()

    def _move(self, delta: int) -> None:
        if not self.entries:
            return
        self.index = max(0, min(len(self.entries) - 1, self.index + delta))
        self._render_list()
        self.page.update()

    def _selected(self) -> dict | None:
        if not self.entries:
            return None
        return self.entries[self.index]

    def _open_selected(self) -> None:
        item = self._selected()
        if item is None:
            return
        self._note_transcript("info", "打开 %s（另起一个窗口进程）" % item["root"])
        self.page.update()

        def work():
            result = self.society.open_window(item["root"])
            if result.get("ok"):
                self._q("log", ("info", "已打开 %s（窗口 pid %s）"
                                % (item["name"], result.get("pid"))))
            else:
                self._q("log", ("error", "%s：%s" % (result.get("error"),
                                                    result.get("tail") or "")))
            self._q("reload", None)

        threading.Thread(target=work, daemon=True, name="home-open").start()

    def _act(self, verb: str) -> None:
        item = self._selected()
        if item is None:
            self._note_transcript("warning", "列表是空的——没有可以对它 %s 的项目" % verb)
            self.page.update()
            return

        def work():
            if verb == "forget":
                ok = recent.forget(item["root"])
                if ok:
                    self._q("log", ("info", "已从列表移除 %s" % item["root"]))
                else:
                    self._q("log", ("warning", "列表里本来就没有 %s" % item["root"]))
            else:
                getattr(self.society, verb)([item["root"]],
                                            on_event=lambda level, text:
                                            self._q("log", (level, text)))
            self._q("reload", None)

        threading.Thread(target=work, daemon=True, name="home-%s" % verb).start()

    def _toggle_autonomous(self) -> None:
        if self.orchestrator is None:
            return
        self.orchestrator.enable = not self.orchestrator.enable
        if self.orchestrator.enable:
            self.orchestrator.start_autonomous()
        state = "开" if self.orchestrator.enable else "关"
        self._note_transcript("info", "调度官自主：%s" % state)
        self._render_status()
        self.page.update()

    # ------------------------------------------------------------ 调度官

    def _submit(self, _event=None) -> None:
        if self.orchestrator is None or self.composer is None:
            return
        text = (self.composer.value or "").strip()
        if not text:
            return
        self.composer.value = ""
        self._note_transcript("user", "‹ " + text)
        self._stream = ""
        self.transcript.controls.append(theme.mono("   ", size=theme.SIZE_DATA,
                                                   color=theme.DIM, selectable=False))
        self.page.update()
        orchestrator = self.orchestrator

        def work():
            try:
                result = orchestrator.turn(text)
            except Exception as ex:  # noqa: BLE001 - 一轮崩了不能带走整个首页
                result = TurnResult(request=text, error="%s: %s" % (type(ex).__name__, ex))
            self._q("turn", result)

        threading.Thread(target=work, daemon=True, name="home-turn").start()

    def _install_autonomous_delta(self) -> None:
        """把流式回调接到事件泵（调度官的自主回路也在另一个线程里跑）。"""
        if self.orchestrator is not None:
            self.orchestrator.on_delta = lambda piece: self._q("delta", piece)
            self.orchestrator.log = lambda level, code, message: \
                self._q("log", (level, "%s %s" % (code, message)))

    # ------------------------------------------------------------ 浮层

    def _new_app(self) -> None:
        name_field = theme.field(hint="app 目录名（英文/短横线）")
        parent_field = theme.field(value=str(Path.cwd()), hint="父目录")
        title_field = theme.field(hint="窗口标题（缺省同目录名）")

        def create(_event=None) -> None:
            name = (name_field.value or "").strip()
            if not name:
                self._note_transcript("warning", "拒绝：名字要填")
                self.page.update()
                return
            theme.close_overlay(self.page)
            self._note_transcript("info", "新建 %s …" % name)
            self.page.update()
            parent = (parent_field.value or "").strip()
            title = (title_field.value or "").strip()

            def work():
                result = self.ops.new(name, parent=parent, title=title)
                if result.get("ok"):
                    self._q("log", ("info", "已创建 %s（%s）"
                                    % (result.get("root"), result.get("note") or "")))
                else:
                    detail = result.get("error") or "；".join(
                        e.get("message", "") for e in (result.get("errors") or []))
                    self._q("log", ("error", "创建失败：%s" % detail))
                self._q("reload", None)

            threading.Thread(target=work, daemon=True, name="home-new").start()

        theme.overlay_scrim(self.page, "新建 app（CLI `new`）", ft.Column(spacing=8, controls=[
            theme.mono("生成最小骨架（window + navbar + 内容容器），随后静态校验。",
                       size=theme.SIZE_DATA, color=theme.DIM, selectable=False),
            theme.labeled("名字", name_field),
            theme.labeled("父目录", parent_field),
            theme.labeled("标题", title_field),
        ]), actions=[theme.primary("创建", create),
                     theme.btn("取消", lambda _e: theme.close_overlay(self.page))])

    def _open_dir(self) -> None:
        path_field = theme.field(hint="一个含 app.puppet 的目录")

        def open_it(_event=None) -> None:
            raw = (path_field.value or "").strip().strip('"')
            if not raw:
                return
            target = Path(raw).expanduser()
            state = recent.state_of(target)
            if not state["is_app"]:
                self._note_transcript("error", "%s 不是 app 目录（缺少 app.puppet）"
                                      % state["root"])
                self.page.update()
                return
            recent.record(state["root"])
            theme.close_overlay(self.page)
            self._reload(reason="打开目录")
            self._open_selected()

        theme.overlay_scrim(self.page, "打开已有目录", ft.Column(spacing=8, controls=[
            theme.mono("必须是含 `app.puppet` 的目录；打开成功后进历史列表。",
                       size=theme.SIZE_DATA, color=theme.DIM, selectable=False),
            theme.labeled("目录", path_field),
        ]), actions=[theme.primary("打开", open_it),
                     theme.btn("取消", lambda _e: theme.close_overlay(self.page))])

    # ------------------------------------------------------------ 退出

    def _install_close_guard(self, page: ft.Page) -> None:
        try:
            page.window.prevent_close = True
            page.window.on_event = self._on_window_event
        except Exception:  # noqa: BLE001 - 不支持就说出来，不假装拦住了
            self._note_transcript("warning", "本平台不支持拦截关窗")

    def _on_window_event(self, event) -> None:
        if getattr(event, "type", None) == ft.WindowEventType.CLOSE:
            self._request_close()

    def _running_count(self) -> int:
        return sum(1 for row in self.rows if row.get("alive"))

    def _request_close(self) -> None:
        running = self._running_count()
        if not running:
            self._destroy()
            return
        text = ("社会层将**下线**：还有 %d 个实例在跑（它们仍在跑，但彼此的消息不通了）。\n"
                "要看住它们：`puppethub hub <父目录> down`。" % running)

        def confirm(_event=None) -> None:
            theme.close_overlay(self.page)
            self._destroy()

        theme.overlay_scrim(self.page, "退出首页？", ft.Column(spacing=8, controls=[
            theme.markup(text, size=theme.SIZE_DATA),
        ]), actions=[theme.btn("留下", lambda _e: theme.close_overlay(self.page)),
                     theme.primary("仍然退出", confirm)])

    def _destroy(self) -> None:
        self._closing = True
        try:
            if self.orchestrator is not None:
                self.orchestrator.stop()
            self.society.stop()
        except Exception:  # noqa: BLE001 - 下线失败也要把窗口关掉，别卡住用户
            pass
        self.page.window.prevent_close = False

        async def _close():
            try:
                await self.page.window.destroy()
            except Exception as ex:  # noqa: BLE001
                print("关窗失败：%s: %s" % (type(ex).__name__, ex))

        self.page.run_task(_close)
