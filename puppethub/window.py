"""宿主窗口：**单窗口分区**（左 app 界面 + 右驾驶舱）+ 一键切"纯 app 模式"。

造出的 app **不含聊天框**；聊天只在 PuppetHub 自己的窗口里，即驾驶舱的对话通道。

**关窗 = app 结束（进程退出）**，唤醒靠再次 `run`——状态持久化在 `.puppet/`，重启即恢复。
早期设想的"关窗即休眠 + 挂起闸门 + 自动重开窗口"已被推翻：窗口就是 flet 窗口，
关窗即进程死，而"谁去唤醒并重开窗口"没有答案。

**关窗前先拦一次脏状态**：`storage` 写失败 → 状态标脏，而关窗是"依赖该写入的最终动作"，
且不可逆。不拦的后果很具体——**人关窗时看的是窗口、不是观察流**，那条持续报警他根本看不见。
"""

from __future__ import annotations

import flet as ft

from .cockpit import Cockpit
from .render import FletRenderer
from .session import Session


class HubWindow:
    def __init__(self, session: Session):
        self.session = session
        self.page: ft.Page | None = None
        self.cockpit: Cockpit | None = None
        self.renderer: FletRenderer | None = None
        self.app_pane: ft.Container | None = None
        self.toggle: ft.Button | None = None

    # ------------------------------------------------------------ 启动

    def run(self) -> None:
        ft.run(self._main, view=ft.AppView.FLET_APP)

    def _main(self, page: ft.Page) -> None:
        self.page = page
        page.title = self.session.app.name
        page.padding = 0
        page.spacing = 0
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

        self.app_pane = ft.Container(content=self.renderer.host, expand=True)
        self.toggle = ft.Button(content=ft.Text("纯 app 模式", size=12), height=32,
                                on_click=self._toggle_cockpit,
                                style=ft.ButtonStyle(padding=8))
        self.cockpit.set_app_mode_toggle(self._toggle_cockpit)
        topbar = ft.Container(
            padding=ft.Padding(12, 6, 12, 6), bgcolor="#0f172a",
            content=ft.Row(controls=[
                ft.Text("PuppetHub", color="#e2e8f0", size=13, weight=ft.FontWeight.BOLD),
                ft.Text(self.session.app.name, color="#94a3b8", size=12),
                ft.Container(expand=True),
                self.toggle,
            ]))
        page.add(ft.Column(expand=True, spacing=0, controls=[
            topbar,
            ft.Row(expand=True, spacing=0,
                   controls=[self.app_pane, self.cockpit.panel]),
        ]))

        self._install_close_guard(page)
        self.session.start()
        self.repaint()

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
        self.page.overlay[:] = [dialog]
        self.page.update()

    def _close_overlay(self) -> None:
        self.page.overlay[:] = []
        self.page.update()

    def _destroy(self, force: bool = False) -> None:
        self.session.note("info", "SHUTDOWN", "关窗 = app 结束；再次 run 会从 .puppet/ 恢复状态")
        if force:
            self.page.overlay[:] = []
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
        if self.cockpit is not None:
            self.cockpit.refresh_views()
        self.page.update()

    def _toggle_cockpit(self, _event=None) -> None:
        if self.cockpit is None:
            return
        visible = not self.cockpit.panel.visible
        self.cockpit.panel.visible = visible
        if self.toggle is not None:
            self.toggle.content = ft.Text("显示驾驶舱" if not visible else "纯 app 模式",
                                          size=12)
        self.page.update()
