"""驾驶舱：一条输入通道 + 一个监督通道 + 一个观察通道。

- **输入**：与 LLM 的对话窗——**唯一**能让程序改变的路径，**常驻可见**（藏起来等于没有）。
  它在后台线程里跑：LLM 是网络 I/O，占了事件循环就是界面卡死。
- **监督**：程序面板——只读呈现真源与 `capabilities.py` 源码 + 系统动作
  （命名快照 / 回滚 / 重载 / 重置 / 清记忆 / 自证 / 查看本轮 prompt）。**没有编辑入口**：
  在"仅 LLM 能写"之下，人只剩"看界面 / 看诊断 / 看懂程序并能退回"三条监督路径。
- **观察**：观察流 + 诊断，只读实时。

两处刻意不省的东西：

- **`capabilities.py` 必须一并显示**：界面逻辑在真源里（人看得见），业务逻辑住在 Python 里
  ——那是人唯一看不见、且无权干预的部分。"看得见"既然是人唯一剩下的监督手段，就不能有盲区。
- **脏状态必须一直在眼前**：storage 写失败时，人看的是窗口而不是日志，
  所以那条报警要贴在他正在看的地方。
"""

from __future__ import annotations

import threading

import flet as ft

from .session import Session

PANEL_WIDTH = 520


class Cockpit:
    def __init__(self, session: Session, repaint, page: ft.Page):
        self.session = session
        self.repaint = repaint
        self.page = page
        self._busy = False
        self._verifying = False
        self._buffer: list[str] = []
        self._stream_text: ft.Text | None = None
        self.panel = self._build()

    # ------------------------------------------------------------ 结构

    def _build(self) -> ft.Control:
        self.status = ft.Text(size=11, color="#64748b")
        self.chain = ft.Text(size=11, color="#0f172a", selectable=True)

        self.dirty_banner = ft.Container(
            visible=False, bgcolor="#fef2f2", border_radius=6, padding=8,
            content=ft.Text("", size=12, color="#b91c1c"))

        self.hint_box = ft.Container(
            padding=12, bgcolor="#f1f5f9", border_radius=8,
            content=ft.Text("说说你想做什么——例如\"做一个待办清单\"。\n"
                            "程序**只由 LLM 改**：人用自然语言表达意图，不直接编辑程序。",
                            size=12, color="#94a3b8"))

        self.chat_log = ft.ListView(expand=True, spacing=6, auto_scroll=True)
        self.chat_input = ft.TextField(hint_text="描述你想要的 app，回车或点「发送」…",
                                       multiline=True, min_lines=1, max_lines=4,
                                       expand=True, on_submit=self._on_chat)
        self.send_btn = ft.Button(content=ft.Text("发送"), on_click=self._on_chat)

        # **确认卡（V2 阶段 0-2 细化）**：只说"待确认"不够——人要能判断才叫确认，
        # 判断需要三样：**将做什么**（原文预览）、**可逆性**、**撤销路径**。
        self.confirm_reason = ft.Text("", size=12, weight=ft.FontWeight.BOLD, color="#b45309")
        self.confirm_what = ft.Text("", size=11, color="#78350f", selectable=True)
        self.confirm_reversible = ft.Text("", size=11, color="#92400e")
        self.confirm_undo = ft.Text("", size=11, color="#92400e")
        self.confirm_row = ft.Container(
            visible=False, bgcolor="#fffbeb", border_radius=6, padding=8,
            content=ft.Column(spacing=6, controls=[
                self.confirm_reason,
                ft.Text("将做什么：", size=11, weight=ft.FontWeight.BOLD, color="#92400e"),
                self.confirm_what,
                self.confirm_reversible,
                self.confirm_undo,
                ft.Row(spacing=6, controls=[
                    ft.Button(content=ft.Text("确认执行", size=12), height=30,
                              on_click=self._on_confirm),
                    ft.Button(content=ft.Text("拒绝", size=12), height=30,
                              on_click=self._on_reject),
                ])]))

        self.takeover_row = ft.Container(
            visible=False, bgcolor="#fef2f2", border_radius=6, padding=8,
            content=ft.Column(spacing=6, controls=[
                ft.Text("", size=12, color="#b91c1c"),
                ft.Row(spacing=6, controls=[
                    ft.Button(content=ft.Text("解除停止，继续对话（人工接管）", size=12),
                              height=30, on_click=self._on_resume),
                ])]))

        self.ask_row = ft.Container(
            visible=False, bgcolor="#eff6ff", border_radius=6, padding=8,
            content=ft.Column(spacing=6, controls=[
                ft.Text("", size=12, color="#1d4ed8"),
                ft.Row(spacing=6, wrap=True, run_spacing=6, controls=[])]))

        self.mode_switch = ft.Switch(label="执行模式（关掉即讨论：不写入）", value=True,
                                     on_change=self._on_mode)

        # **自主面板（V2 M4）**：写者是谁、干了什么、怎么停它——全在一处。
        # 自主失去的是"人常驻监督"，不是"人可以看"：面板不给，监督就是空话。
        self.writer_text = ft.Text("", size=11, color="#0f172a", selectable=True)
        self.autonomy_log = ft.Text("", size=11, color="#475569", selectable=True)
        self.autonomy_box = ft.Container(
            padding=10, bgcolor="#f8fafc", border_radius=6,
            border=ft.Border.all(1, "#e2e8f0"),
            content=ft.Column(spacing=6, controls=[
                ft.Text("自主回路（写者状态机：llm / autonomous / none 互斥）",
                        size=12, weight=ft.FontWeight.BOLD),
                self.writer_text,
                ft.Row(wrap=True, spacing=6, run_spacing=6, controls=[
                    self._action("启动自主", lambda e: self._on_writer("autonomous")),
                    self._action("暂停（无人写）", lambda e: self._on_writer("none")),
                    self._action("交回共作者", lambda e: self._on_writer("llm")),
                ]),
                self.autonomy_log,
            ]))

        self.source_view = ft.TextField(read_only=True, multiline=True,
                                        min_lines=6, max_lines=14, text_size=12,
                                        border_color="#e2e8f0")
        self.caps_view = ft.TextField(read_only=True, multiline=True,
                                      min_lines=5, max_lines=12, text_size=12,
                                      border_color="#e2e8f0")
        self.snap_dd = ft.Dropdown(label="快照", expand=True)

        self.log_view = ft.ListView(expand=True, spacing=2, auto_scroll=True)
        self.verify_out = ft.Text(size=11, color="#64748b", selectable=True)
        self.app_mode_btn = self._action("纯 app 模式", None)

        actions = ft.Row(wrap=True, spacing=6, run_spacing=6, controls=[
            self._action("命名快照", self._on_named),
            self._action("回滚", self._on_restore),
            self._action("重载", self._on_reload),
            self._action("重置状态", self._on_reset),
            self._action("记忆", self._on_show_memory),
            self._action("清空记忆", self._on_wipe),
            self._action("自证", self._on_verify),
            self._action("插件热重载", self._on_reload_plugins),
            self._action("本轮 prompt", self._on_show_prompt),
        ])

        return ft.Container(
            width=PANEL_WIDTH, bgcolor="#ffffff",
            padding=12, border=ft.Border.all(1, "#e2e8f0"),
            content=ft.Column(expand=True, spacing=10, scroll=ft.ScrollMode.AUTO,
                              controls=[
                                  ft.Row([ft.Text("驾驶舱", weight=ft.FontWeight.BOLD,
                                                  size=16),
                                          ft.Container(expand=True),
                                          self.app_mode_btn]),
                                  self.status,
                                  self.chain,
                                  self.dirty_banner,
                                  self.hint_box,
                                  ft.Text("对话（唯一改程序的路径）", size=12,
                                          weight=ft.FontWeight.BOLD),
                                  ft.Container(content=self.chat_log, height=240,
                                               border=ft.Border.all(1, "#e2e8f0"),
                                               border_radius=6, padding=6),
                                  ft.Row([self.chat_input, self.send_btn]),
                                  self.confirm_row,
                                  self.takeover_row,
                                  self.ask_row,
                                  self.mode_switch,
                                  self.autonomy_box,
                                  ft.Text("观察流 / 诊断", size=12, weight=ft.FontWeight.BOLD),
                                  ft.Container(content=self.log_view, height=180,
                                               border=ft.Border.all(1, "#e2e8f0"),
                                               border_radius=6, padding=6),
                                  ft.Text("程序（只读）", size=12, weight=ft.FontWeight.BOLD),
                                  self.source_view,
                                  ft.Text("capabilities.py（只读）", size=12,
                                          weight=ft.FontWeight.BOLD),
                                  self.caps_view,
                                  actions,
                                  self.snap_dd,
                                  self.verify_out,
                              ]))

    def set_app_mode_toggle(self, handler) -> None:
        """把"纯 app 模式"按钮接到窗口上（切换的是窗口布局，不是驾驶舱内部）。"""
        self.app_mode_btn.on_click = handler

    @staticmethod
    def _action(label: str, handler) -> ft.Button:
        return ft.Button(content=ft.Text(label, size=12), on_click=handler,
                         height=32, style=ft.ButtonStyle(padding=8))

    # ------------------------------------------------------------ 刷新

    def refresh_views(self) -> None:
        session = self.session
        session.check_capabilities()       # 能力热重载检查点：界面刷新前查一次
        rendering = session.hello()["rendering"]
        self.status.value = (
            "%s · 语言 %s · 节点 %d · 能力 %d · 记忆 %d 条\n"
            "渲染：控件 %d / 属性 %d / 动效 %d / 图标 %d · geometry=%s snapshot=%s"
            % (session.app.root, session.spec_version,
               len(session.engine.program.nodes), len(session.catalog()),
               len(session.memory_entries()),
               len(rendering["controls"]), len(rendering["attributes"]),
               len(rendering["animations"]), len(rendering["icons"]),
               rendering["geometry"], rendering["snapshot"]))
        self.chain.value = "生效链：" + session.plugin_chain()
        self.source_view.value = session.program_text()
        self.caps_view.value = session.program_assets_text()

        dirty = session.dirty
        self.dirty_banner.visible = bool(dirty)
        if dirty:
            self.dirty_banner.content.value = (
                "**状态标脏**：%d 项写入没落盘，内存已变而磁盘未变。"
                "重启会丢，重新运行前请先处理。\n%s" % (len(dirty), dirty[0]))

        entries = session.app.list_snapshots()[:12]
        self.snap_dd.options = [
            ft.DropdownOption(key=entry.id,
                              text="%s · %s · %s" % (entry.time[11:], entry.kind,
                                                     entry.reason[:18]))
            for entry in entries]
        if entries and not self.snap_dd.value:
            self.snap_dd.value = entries[0].id
        if not entries:
            self.snap_dd.value = None

        self.hint_box.visible = len(session.engine.program.nodes) <= 4
        self.log_view.controls = [
            ft.Text("%s %s %s %s" % (entry["time"], entry["level"], entry["code"],
                                     entry["where"] + " " + entry["text"]),
                    size=11, selectable=True,
                    color={"错误": "#dc2626", "警告": "#b45309"}.get(entry["level"], "#334155"))
            for entry in list(session.log)[-120:]]

        chat = session.chat
        pending = chat.pending if chat is not None else None
        self.confirm_row.visible = bool(pending)
        if pending:
            body = pending.get("preview") or ""
            lines = [line for line in body.splitlines() if line.strip()]
            shown = lines[:8]
            more = "" if len(lines) <= 8 else "\n…（共 %d 行，其余看「本轮 prompt」或拒绝后细看）" % len(lines)
            calls = pending.get("calls") or []
            self.confirm_reason.value = "待确认：%s" % pending.get("reason", "")
            self.confirm_what.value = "\n".join(shown) + more or "（空批）"
            self.confirm_reversible.value = "涉及能力：%s" % "、".join(calls) if calls \
                else "涉及能力：无（纯界面改动）"
            if pending.get("kind") == "fuse":
                self.confirm_reversible.value = ("融合是**推倒重来级**的确认式变更：B 的内容全部"
                                                 "并入、B 目录归档改名（不删除），机制体检与干跑已通过。")
                self.confirm_undo.value = ("可逆性：可逆——执行前有兜底快照（rebuild，不参与淘汰）。\n"
                                           "撤销路径：下方「回滚」选 rebuild 那一份；B 的原目录在归档名下完好。")
            elif pending.get("kind") == "replace":
                self.confirm_undo.value = ("可逆性：可逆——执行前会自动快照（写前存档）。\n"
                                           "撤销路径：下方「回滚」选最近一份 auto 快照。")
        # **人工接管的运行期入口**：卡住是警告，停手是红灯——红灯必须带一个解开的开关，
        # 否则"停止自动重试"就变成"这个实例从此哑了"。
        halted = bool(chat is not None and chat.halted)
        stuck = bool(chat is not None and chat.stuck_note)
        self.takeover_row.visible = halted or stuck
        if halted:
            self.takeover_row.content.controls[0].value = (
                "已停止自动重试（连续失败超过预算）。解除后对话继续——"
                "最好先说清卡在哪；要退回上一版就用下方程序面板的「回滚」。")
        elif stuck:
            self.takeover_row.content.controls[0].value = "检测到重复失败：%s" % chat.stuck_note
        self.mode_switch.value = session.mode == "执行"

        # 自主面板：写者、预算用量、最近决策（**含被拒的**——拒了什么也是监督信息）。
        runner = session.autonomous
        if runner is not None:
            stats = runner.stats()
            self.writer_text.value = (
                "当前写者：%s · 自主步进 %d/%d 次每小时 · 自主白名单：%s · 状态：%s"
                % (session.writer, stats["steps_last_hour"], stats["budget_per_hour"],
                   "、".join(stats["allow"]) or "（空——危险能力全部默认拒绝）",
                   "步进中" if stats["busy"] else "待命"))
            recent = runner.recent(5)
            self.autonomy_log.value = ("最近自主决策：\n" + "\n".join(
                "· %s %s%s" % (item.get("time", "?"), (item.get("trigger") or "")[:40],
                               (" → " + "、".join(item.get("applied")[:1])) if item.get("applied")
                               else (" → 被拒/跳过" if item.get("skipped") else " → 无动作"))
                for item in reversed(recent))) if recent else "自主回路待命中（尚无步进）"
        else:
            self.writer_text.value = "当前写者：%s · 自主回路不可用（未装配 LLM）" % session.writer
            self.autonomy_log.value = ""
        self._render_history()

    def _render_history(self) -> None:
        """对话区渲染：**过程记忆的原文**（不做摘要），流式中的那一块挂在末尾。"""
        chat = self.session.chat
        if chat is None:
            return
        controls = []
        for entry in chat.history()[-12:]:
            role = entry.get("role")
            text = (entry.get("text") or "").strip()
            if not text:
                continue
            controls.append(ft.Text("【%s】" % ("我" if role == "user" else "共作者"),
                                    size=11, color="#64748b"))
            controls.append(ft.Text(text, size=12, selectable=True,
                                    color="#0f172a" if role == "user" else "#1e293b"))
        if self._stream_text is not None:
            controls.append(ft.Text("【共作者】", size=11, color="#64748b"))
            controls.append(self._stream_text)
        self.chat_log.controls = controls[-60:]

    # ------------------------------------------------------------ 对话

    def submit(self, text: str) -> None:
        """程序化提交一轮（等价于在输入框里打字再点发送）。"""
        self.chat_input.value = text
        self._on_chat()

    def _on_chat(self, _event=None) -> None:
        if self._busy:
            self.session.note("warning", "CHAT_BUSY", "上一轮还没结束，请稍候")
            self.repaint()
            return
        text = (self.chat_input.value or "").strip()
        if not text:
            return
        if self.session.chat is None:
            self.session.note("error", "LLM_UNAVAILABLE",
                              "对话回路不可用：%s" % (self.session.plugin_error or "插件未装配"))
            self.repaint()
            return
        self.chat_input.value = ""
        self._busy = True
        self._buffer = []
        stream = ft.Text("", size=12, selectable=True, color="#1e293b")
        self._stream_text = stream
        self.chat_log.controls = list(self.chat_log.controls) + [
            ft.Text("【我】", size=11, color="#64748b"),
            ft.Text(text, size=12, selectable=True),
            ft.Text("【共作者】", size=11, color="#64748b"),
            stream,
        ]
        self.send_btn.disabled = True
        self.page.update()
        # LLM 是网络 I/O：放后台线程，别占住事件循环。
        self.page.run_thread(self._run_turn, text)

    def _run_turn(self, text: str) -> None:
        try:
            result = self.session.chat_turn(text)
        except Exception as ex:  # noqa: BLE001 - 不能让它静默吞掉
            self.session.note("error", "CHAT_ERROR", "本轮异常：%s: %s" % (type(ex).__name__, ex))
            result = None
        finally:
            self._busy = False
            self.send_btn.disabled = False
        self._on_delta_done(result)

    def push_delta(self, piece: str) -> None:
        """流式片段（由 `Session.on_delta` 从工作线程回调进来）。"""
        self._delta(piece)

    def _delta(self, piece: str) -> None:
        self._buffer.append(piece)
        if len(self._buffer) % 12 == 0:
            self._flush_stream()

    def _flush_stream(self) -> None:
        if self._stream_text is None:
            return
        self._stream_text.value = "".join(self._buffer)
        try:
            self.page.update()
        except Exception:  # noqa: BLE001 - 界面更新失败不该打断对话
            pass

    def _on_delta_done(self, result) -> None:
        self._flush_stream()
        if result is not None:
            if result.question:
                self._show_question(result.question)
            else:
                self.ask_row.visible = False
            for note in result.applied:
                self.session.note("info", "CHAT_APPLIED", note)
            for note in result.skipped:
                self.session.note("warning", "CHAT_SKIPPED", note)
            if result.error:
                self.session.note("error", "CHAT_ERROR", result.error)
            if result.stuck:
                self.session.note("warning", "LLM_STUCK", result.stuck)
            if result.halted:
                self.session.note("error", "LLM_HALTED", "已停止自动重试：请人工接管或要求推倒重来")
        self._stream_text = None
        self.repaint()

    def _show_question(self, question: dict) -> None:
        options = question.get("options") or []
        controls = []
        for option in options:
            controls.append(ft.Button(content=ft.Text(str(option), size=12), height=30,
                                      on_click=self._answer(option)))
        controls.append(ft.Button(content=ft.Text("跳过", size=12), height=30,
                                  on_click=self._answer(None)))
        self.ask_row.visible = True
        self.ask_row.content.controls[0].value = question.get("question") or "需要你确认"
        self.ask_row.content.controls[1].controls = controls

    def _answer(self, value):
        def handler(_event=None):
            self.ask_row.visible = False
            if value:
                self.chat_input.value = str(value)
                self._on_chat()
            else:
                self.repaint()
        return handler

    def _on_confirm(self, _event=None) -> None:
        self.session.approve_pending()
        self.repaint()

    def _on_reject(self, _event=None) -> None:
        self.session.reject_pending()
        self.repaint()

    def _on_resume(self, _event=None) -> None:
        self.session.resume_chat(origin="user")
        self.repaint()

    def _on_writer(self, state: str) -> None:
        self.session.set_writer(state, origin="user")
        self.repaint()

    def _on_mode(self, event=None) -> None:
        self.session.set_mode("执行" if getattr(event.control, "value", True) else "讨论")
        self.repaint()

    # ------------------------------------------------------------ 系统动作

    def _on_named(self, _event=None) -> None:
        self.session.named_snapshot("里程碑 %s" % self.session.app.name)
        self.repaint()

    def _on_restore(self, _event=None) -> None:
        target = self.snap_dd.value
        if not target:
            self.session.note("warning", "RESTORE", "没有可回滚的快照")
        else:
            self.session.restore(target)
        self.repaint()

    def _on_reload(self, _event=None) -> None:
        self.session.reload()
        self.repaint()

    def _on_reset(self, _event=None) -> None:
        self.session.reset()
        self.repaint()

    def _on_wipe(self, _event=None) -> None:
        """清空记忆是**显式**动作，重置状态不碰它。"""
        self.session.wipe_memory(origin="user")
        self.repaint()

    def _on_show_memory(self, _event=None) -> None:
        """记忆面板：它是可读可改的纯数据，人就该能直接看。

        （与"本轮 prompt"同一套浮层做法：只读呈现，不给编辑入口——
        编辑记忆的正路是直接改 `.puppet/memory/memory.jsonl`，那是它作为**数据**的属性。）
        """
        body = ft.TextField(value=self.session.memory_text(), read_only=True,
                            multiline=True, min_lines=18, max_lines=28, text_size=11)
        self.page.overlay[:] = [ft.AlertDialog(
            modal=True, open=True, title=ft.Text("运行期记忆"),
            content=ft.Container(content=body, width=760, height=460),
            actions=[ft.Button(content=ft.Text("关闭"),
                               on_click=lambda _e: self._close_overlay())])]
        self.page.update()

    def _on_reload_plugins(self, _event=None) -> None:
        """插件热重载（V2 阶段 3）：配置与白名单即时生效；护栏在 session 端。"""
        self.session.reload_plugins()
        self.repaint()

    def _on_verify(self, _event=None) -> None:
        """自证要跑一整个 conformance（分钟级）——放后台线程，否则界面冻住。"""
        if self._verifying:
            self.session.note("warning", "VERIFY", "自证正在跑，别催")
            return
        self._verifying = True
        self.verify_out.value = "自证进行中…（结果会流进观察流）"
        self.repaint()
        self.page.run_thread(self._run_verify)

    def _run_verify(self) -> None:
        try:
            text = self.session.verify(on_line=self._flush_verify)
        finally:
            self._verifying = False
        self.verify_out.value = text
        self.repaint()

    def _flush_verify(self) -> None:
        try:
            self.refresh_views()
            self.page.update()
        except Exception:  # noqa: BLE001 - 界面刷新失败不该打断自证
            pass

    def _on_show_prompt(self, _event=None) -> None:
        """本轮实际发送的完整 prompt：不允许查看，prompt 就是系统里唯一的黑盒。"""
        chat = self.session.chat
        if chat is None or not chat.last_messages:
            self.verify_out.value = "还没有发出过请求。"
            self.repaint()
            return
        blocks = []
        for message in chat.last_messages:
            blocks.append("【%s】\n%s" % (message.get("role"), message.get("content", "")))
        chain = "、".join("%s%s" % (name, "（改写了）" if changed else "")
                         for name, changed in chat.last_chain) or "（无 prompt 插件）"
        body = ft.TextField(value="改写链：%s\n\n%s" % (chain, "\n\n".join(blocks)),
                            read_only=True, multiline=True, min_lines=20, max_lines=30,
                            text_size=11)
        self.page.overlay[:] = [ft.AlertDialog(
            modal=True, open=True, title=ft.Text("本轮实际发送的 prompt"),
            content=ft.Container(content=body, width=760, height=520),
            actions=[ft.Button(content=ft.Text("关闭"),
                               on_click=lambda _e: self._close_overlay())])]
        self.page.update()

    def _close_overlay(self) -> None:
        self.page.overlay[:] = []
        self.page.update()
