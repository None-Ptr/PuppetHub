"""宿主窗口：**单窗口分区**（左 app 界面 + 右驾驶舱）+ 一键切"纯 app 模式"。

造出的 app **不含聊天框**；聊天只在 PuppetHub 自己的窗口里，即驾驶舱的对话通道。

**关窗 = app 结束（进程退出）**，唤醒靠再次 `run`——状态持久化在 `.puppet/`，重启即恢复。
早期设想的"关窗即休眠 + 挂起闸门 + 自动重开窗口"已被推翻：窗口就是 flet 窗口，
关窗即进程死，而"谁去唤醒并重开窗口"没有答案。

**关窗前先拦一次脏状态**：`storage` 写失败 → 状态标脏，而关窗是"依赖该写入的最终动作"，
且不可逆。不拦的后果很具体——**人关窗时看的是窗口、不是观察流**，那条持续报警他根本看不见。
"""

from __future__ import annotations

import threading

import flet as ft

from . import theme
from .cockpit import PANEL_WIDTH, Cockpit
from .render import FletRenderer
from .session import Session

# 宿主家具占的竖向高度：顶栏 26 + 一条线 + 舞台状态行 22 + 舞台上下描边 ≈ 52。
CHROME_H = 52

# 窗口最小高度：驾驶舱竖向预算是"可算的"（`vertical_budget`），但仍要有下限——
# 否则用户把窗口拖到 300 高时，转录与抽屉两边都只剩几像素（能算不等于能用）。
COCKPIT_MIN_H = 640


class HubWindow:
    def __init__(self, session: Session, listen_port: int | None = None):
        self.session = session
        # 协作端口（首页 `open` / `run --listen-port` 注入）：给了就给窗口实例装一只
        # **耳朵**——没有它，窗口实例只发不收（总线的投递需要可反向连接的端口）。
        self.listen_port = listen_port
        self.page: ft.Page | None = None
        self.cockpit: Cockpit | None = None
        self.renderer: FletRenderer | None = None
        self.app_pane: ft.Container | None = None
        self.toggle: ft.Button | None = None
        self.stage_meta: ft.Text | None = None
        # 舞台（app 声明的）尺寸 + "上次据以调整外框的声明"：判据见 _fit_window。
        self._stage = (0, 0)
        self._applied_declared = None
        self._close_dialog: ft.Control | None = None   # 关窗拦截框（共享 page.overlay）

    # ------------------------------------------------------------ 启动

    def run(self) -> None:
        ft.run(self._main, view=ft.AppView.FLET_APP)

    def _main(self, page: ft.Page) -> None:
        self.page = page
        page.title = self.session.app.name
        page.padding = 0
        page.spacing = 0
        page.bgcolor = theme.BG
        # **主题是我们铸的**（不是跟随系统）：Material 控件（输入框 / 列表 /
        # 下拉 / 开关）读页面的 color_scheme，而"跟随系统"会让同一个程序在
        # 每台机器上长得不一样——那与"可核对"背道而驰。
        page.theme_mode = ft.ThemeMode.DARK
        page.theme = theme.page_theme()
        # 供多模态自检：截图要先用 `enable_screenshots` 开闸，否则返回空（不是"无头下截不了"）。
        page.enable_screenshots = True

        self.renderer = FletRenderer(page, self.session.engine,
                                     on_event=self._on_event,
                                     on_local_change=self.repaint,
                                     assets_dir=str(self.session.app.assets_dir))
        self.session.renderer = self.renderer
        self.cockpit = Cockpit(self.session, self.repaint, page)
        # 流式输出：LLM 逐字吐，界面逐字上屏（以流为主，实时性不在插件层丢）。
        self.session.on_delta = self.cockpit.push_delta
        self._start_society_listener()

        # **舞台**（左）= 设备框：app 自己的界面是一台"放在暗房里的设备"，
        # 它保持自己的样子（那是别人的作品），我们只负责框住它、并说明它的规格。
        self.stage_meta = theme.mono("", size=theme.SIZE_MICRO, color=theme.DIM,
                                     selectable=False)
        self.stage_empty = ft.Container(
            expand=True, bgcolor=theme.BG, padding=24,
            content=ft.Column(expand=True, spacing=6,
                              alignment=ft.MainAxisAlignment.CENTER,
                              horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                              controls=[
                                  theme.mono("（这台设备还没有内容）", size=theme.SIZE_BODY,
                                             color=theme.DIM, selectable=False),
                                  theme.mono("在右边说一句话，它就从头开始长界面。",
                                             size=theme.SIZE_DATA, color=theme.DIM,
                                             selectable=False),
                              ]))
        self.app_pane = ft.Container(
            expand=True, bgcolor=theme.BG,
            content=ft.Column(expand=True, spacing=0, controls=[
                ft.Container(padding=ft.Padding(12, 4, 12, 4),
                             content=self.stage_meta),
                ft.Container(content=ft.Container(
                    content=ft.Stack(expand=True, controls=[
                        # 空态：**垫在 app 后面**——app 自己画了底色就盖住它，
                        # 没画（骨架 app）就露出这一句。宿主只提示，不侵入。
                        self.stage_empty,
                        self.renderer.host,
                    ]), expand=True,
                    border=ft.Border.all(1, theme.RULE)),
                    expand=True, padding=ft.Padding(12, 0, 12, 12)),
            ]))
        self.toggle = theme.btn("隐藏控制台", self._toggle_cockpit, tone=theme.DIM)
        topbar = ft.Container(
            height=26, bgcolor=theme.BG, padding=ft.Padding(10, 0, 10, 0),
            content=ft.Row(spacing=8, controls=[
                theme.mono("PUPPETHUB", size=theme.SIZE_MICRO, color=theme.AMBER,
                           selectable=False),
                theme.mono("/", size=theme.SIZE_MICRO, color=theme.RULE,
                           selectable=False),
                theme.mono(self.session.app.name, size=theme.SIZE_BODY,
                           color=theme.TEXT, selectable=False),
                ft.Container(expand=True),
                self.toggle,
            ]))
        page.add(ft.Column(expand=True, spacing=0, controls=[
            topbar,
            ft.Container(height=1, bgcolor=theme.RULE),
            ft.Row(expand=True, spacing=0,
                   controls=[self.app_pane, self.cockpit.panel]),
        ]))
        if self.cockpit is not None:
            page.run_task(self.cockpit.cursor_loop)
            # 拖窗口 = 比例跟着变：竖向预算按**当前高度**重算（转录 / 抽屉的分配
            # 只有一个来源，见 `cockpit.vertical_budget`）。
            try:
                page.on_resize = self._on_resize
            except Exception:  # noqa: BLE001 - 平台不支持就算了（启动时仍会算一次）
                pass

        self._install_close_guard(page)
        self.session.start()
        self.repaint()

    # ------------------------------------------------------------ 协作耳朵（V4 社会）

    def _start_society_listener(self) -> None:
        """给窗口实例装一只**耳朵**：复用**本进程这个 session** 的 TCP 绑定。

        没有它，窗口实例只发不收——总线的投递需要对方有可反向连接的端口。
        端口由宿主分配（首页 `open` / `run --listen-port`）。bind 在**调用线程**做：
        端口没抢到要能在窗口里看见，而不是静默死在线程里。
        """
        if not self.listen_port:
            return
        from .remote import TcpBinding, bind_tcp, serve_tcp
        port = int(self.listen_port)
        try:
            server = bind_tcp(port)
        except OSError as ex:
            self.session.note("error", "SOCIETY_LISTEN",
                              "协作端口 %d 没抢到：%s（本实例只发不收——社会层对它"
                              "发的消息会投递失败）" % (port, ex))
            return
        binding = TcpBinding(session=self.session, renderer=self.renderer,
                             after=self._schedule_repaint)
        threading.Thread(target=serve_tcp, args=(binding, port, server),
                         daemon=True, name="society-listen").start()
        self.session.note("info", "SOCIETY_LISTEN",
                          "窗口实例已监听 127.0.0.1:%d（可被 tell / who / fire / call）"
                          % port)

    def _schedule_repaint(self) -> None:
        """TCP 线程上被调到：**只排队**，真正的重绘回到窗口自己的事件循环。

        跨线程直接 `page.update()` 是踩过的坑；`run_task` 这条退路也不可用时**说出来**
        （界面会在下次本机操作时刷新）——不装作已经刷新了。
        """
        try:
            self.page.run_task(self._repaint_on_loop)
        except Exception as ex:  # noqa: BLE001
            self.session.note("warning", "REPAINT",
                              "跨线程重绘不可用：%s（界面会在下次本机操作时刷新）" % ex)

    async def _repaint_on_loop(self) -> None:
        try:
            self.repaint()
        except Exception as ex:  # noqa: BLE001 - 重绘失败不能吞
            self.session.note("warning", "REPAINT", "重绘失败：%s" % ex)

    # ------------------------------------------------------------ 关窗前的脏状态拦截

    def _install_close_guard(self, page: ft.Page) -> None:
        try:
            page.window.prevent_close = True
            page.window.on_event = self._on_window_event
        except Exception:  # noqa: BLE001 - 平台不支持时**说出来**，而不是假装拦住了
            self.session.note("warning", "CLOSE_GUARD",
                              "本平台不支持拦截关窗：脏状态只会在驾驶舱里报警，"
                              "关窗可能直接丢掉未落盘的改动")

    def _on_window_event(self, event) -> None:
        if getattr(event, "type", None) == ft.WindowEventType.CLOSE:
            self._request_close()

    def _request_close(self) -> None:
        remaining = self.session.retry_storage()
        if not remaining:
            self._destroy()
            return
        dialog = ft.AlertDialog(
            modal=True, open=True, title=ft.Text("还有改动没写入磁盘"),
            content=ft.Text("有 %d 项改动未写入磁盘，退出将丢失；仍要退出？\n\n%s"
                            % (len(remaining), "\n".join(remaining[:3])), size=12),
            actions=[
                ft.Button(content=ft.Text("留下"),
                          on_click=lambda _e: self._close_overlay()),
                ft.Button(content=ft.Text("仍要退出"),
                          on_click=lambda _e: self._destroy(force=True)),
            ])
        # 只增删**自己这一个**：app 的 `dialog` 节点与驾驶舱的「记忆 / 本轮 prompt」
        # 浮层共用 `page.overlay`，整表覆盖会把它们一起抹掉。
        self._close_dialog = dialog
        self.page.overlay[:] = list(self.page.overlay) + [dialog]
        self.page.update()

    def _close_overlay(self) -> None:
        dialog, self._close_dialog = self._close_dialog, None
        if dialog is not None:
            self.page.overlay[:] = [item for item in self.page.overlay
                                    if item is not dialog]
        self.page.update()

    def _destroy(self, force: bool = False) -> None:
        self.session.note("info", "SHUTDOWN", "关窗 = app 结束；再次 run 会从 .puppet/ 恢复状态")
        if force:
            self._close_overlay()
        self.page.window.prevent_close = False

        async def _close():
            # `destroy()` 是协程：只能在事件循环里 await，而关窗回调是同步的。
            try:
                await self.page.window.destroy()
            except Exception as ex:  # noqa: BLE001 - 关不掉也要说出来
                self.session.note("error", "SHUTDOWN", "关窗失败：%s: %s"
                                  % (type(ex).__name__, ex))
        self.page.run_task(_close)

    # ------------------------------------------------------------ 回路

    def _on_event(self, node_id: str, event: str, row, value) -> None:
        self.session.fire(node_id, event, row, value)
        self.repaint()

    def repaint(self) -> None:
        """拉一次观察面 → 重画 app → 刷新驾驶舱 → 上屏。"""
        if self.page is None:
            return
        self.session.refresh()
        if self.stage_meta is not None:
            # 用**记录的舞台尺寸**，不拿当前窗口宽去减——窗口宽在"舞台↔外框"
            # 之间跳，减出来的数会漂（实测见过 348 这种假值）。
            rendering = self.session.hello()["rendering"]
            if self.stage_empty is not None:
                self.stage_empty.visible = len(self.session.engine.program.nodes) <= 4
            stage = self._stage if self._stage[0] else (0, 0)
            self.stage_meta.value = (
                "%s · %s×%s · %d 节点 · geometry %s"
                % (self.session.app.name, stage[0] or "?", stage[1] or "?",
                   len(self.session.engine.program.nodes),
                   "✗ 报不出（对齐无法核对；x/y 仍生效）"
                   if not rendering["geometry"] else "✓"))
        if self.cockpit is not None:
            self.cockpit.refresh_views()
        self._fit_window()
        self.page.update()

    def _on_resize(self, _event=None) -> None:
        """窗口尺寸变化 → 重算竖向预算（用户拖窗口也要保持比例可用）。"""
        if self.cockpit is None:
            return
        self.cockpit.apply_budget()
        self.page.update()

    def _fit_window(self) -> None:
        """**只在 app 改了声明尺寸时**调整外框；其余时间窗口归用户。

        渲染器把 `window` 节点的 w/h 写给真实窗口（那是**舞台**的尺寸——app 就该
        在它声明的视口里跑），并且改为"只在声明变化时写"，同时把声明尺寸报给宿主
        （`renderer.applied_window`）。宿主据此把控制台与顶栏补上：**补一次**，
        之后用户想怎么拖都行——每帧强制尺寸等于用户拖不动窗口，那是 bug。
        高度至少 640，免得控制台被挤扁。
        """
        declared = getattr(self.renderer, "applied_window", None)
        if not declared or declared == self._applied_declared:
            return
        self._applied_declared = declared
        win = self.page.window
        width = declared[0] or int(win.width or 0)
        height = declared[1] or int(win.height or 0)
        if not width or not height:
            return
        self._stage = (width, height)
        extra = PANEL_WIDTH if (self.cockpit is not None
                                and self.cockpit.panel.visible) else 0
        win.width = width + extra
        win.height = max(height + CHROME_H, COCKPIT_MIN_H)
        if self.cockpit is not None:
            self.cockpit.apply_budget()          # 外框变了，比例重算

    def _toggle_cockpit(self, _event=None) -> None:
        """纯 app 模式：整块控制台收走，只剩舞台（截图 / 给最终用户试跑用）。"""
        if self.cockpit is None:
            return
        visible = not self.cockpit.panel.visible
        self.cockpit.panel.visible = visible
        if self.toggle is not None:
            self.toggle.content.value = "显示控制台" if not visible else "隐藏控制台"
        # 外框随控制台的去留变：纯 app 模式 = 正好舞台大小。这里**显式**算，
        # 不走 `_fit_window`（那个只在 app 改声明时动作，此刻声明没变）。
        stage_w = self._stage[0] or int(self.page.window.width or 0)
        stage_h = self._stage[1] or int(self.page.window.height or 0)
        self.page.window.width = stage_w + (PANEL_WIDTH if visible else 0)
        self.page.window.height = max(stage_h + CHROME_H, 640)
        self.page.update()
