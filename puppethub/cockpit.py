"""驾驶舱：磷光终端。**唯一依据**是 `docs/style-terminal.md`。

屏幕前的人只有三条路：**说**（对话，唯一能改程序的路径）、**看**（观察流 /
程序 / 能力）、**退回**（快照 / 回滚 / 重载）。终端形态让这三条都落在同一块
屏上，且不需要任何装饰：

    ▍ 执笔 │ 共作者            0/60 │ 待命        ← 签名：执笔光标
    目标 … │ 总线 … │ 借出 …                       ← 运行期状态一行
    ────────────────────────────────────────────
    12:03:41 │ › 做一个待办清单                    ← 转录：无气泡，无图标
    12:03:44 │   ‹ 好，先立窗口与清单容器：…
    ────────────────────────────────────────────
    › 描述你想要的 app…                    [发送]   ← 唯一允许填充的矩形
    [执行] 讨论        观察  程序  能力  规格  操作
    12:03:41 ● LLM_STUCK  同一诊断在最近两轮里重复…

两条不许改的纪律（写在代码里，免得半年后被"优化"掉）：
1. **闪动只在写入进行中**——闪 = 正在发生，不是装饰；待命必须静止。
2. **琥珀只给"警告 + 主行动"**——颜色只有在稀缺时才是指示。
"""

from __future__ import annotations

import asyncio
import threading

import flet as ft

from . import theme
from .gui_tools import GuiTools
from .session import Session
from .theme import (AMBER, BG, CURSOR, DIM, ERR, GREEN, LEVEL_MARK, MONO,
                    MONO_FALLBACK, RAISE, RED, RULE, SIZE_BODY, SIZE_DATA,
                    SIZE_MICRO, TEXT, box, btn, gap, mono, primary, rule)

PANEL_WIDTH = 560

# 抽屉六块牌子：点当前标签收起（把空间让给转录）。
# 「工具」= CLI 功能的图形入口（new/build/edit/hub/remote/fuse/repl），见 gui_tools.py。
VIEWS = ("观察", "程序", "能力", "规格", "操作", "工具")
DEFAULT_VIEW = "观察"

# 竖向预算（手册第 3 条：默认窗口内不滚动）。
#
# 这里踩过一次：曾经用**手算的常量**（转录 220 + 抽屉 260 + 家具）并注释"676 ≤ 692"
# ——少算了间距与状态行，真实总和 703 > 可用 665，**抽屉底部被裁掉 38px**（截图能看见
# 日志最后几行贴着窗口边缘断掉）。手算的预算会随子控件增删立刻失效，所以改成
# **可计算 + 自适应**：固定开销是一个常量、剩下全部分给转录与抽屉，且**保证不溢出**。
VERTICAL_FIXED = 250       # 顶栏 27 + 光标行 24 + 状态行 14 + 线 1 + 输入 36 + 模式 22
                           # + 标签 22 + 间距 88 + 内边距 16
TRANSCRIPT_MIN = 120
DRAWER_MIN = 130
DRAWER_MAX = 320
DRAWER_SHARE = 0.55        # 剩余空间给抽屉的比例（缺省视图 = 观察，抽屉里是日志）


def vertical_budget(window_height, drawer_open: bool) -> dict:
    """把窗口高度切成 `转录` 与 `抽屉`：**总和永不超出可用高度**（纯函数，可测）。

    空间不够时先保转录（对话是本窗口的主业），抽屉压到剩余；极小窗口允许抽屉为 0
    ——宁可让它"没有"，也不要它把输入行挤出屏幕。
    """
    free = int(window_height or 0) - VERTICAL_FIXED
    if not drawer_open:
        return {"free": max(0, free), "transcript": max(TRANSCRIPT_MIN, free),
                "drawer": 0, "fits": True}
    if free <= 0:
        return {"free": 0, "transcript": 0, "drawer": 0, "fits": False}
    if free < TRANSCRIPT_MIN + DRAWER_MIN:
        transcript = min(TRANSCRIPT_MIN, free)
        return {"free": free, "transcript": transcript,
                "drawer": free - transcript, "fits": True}
    drawer = max(DRAWER_MIN, min(DRAWER_MAX, int(round(free * DRAWER_SHARE))))
    transcript = free - drawer
    if transcript < TRANSCRIPT_MIN:
        transcript = TRANSCRIPT_MIN
        drawer = free - transcript
    return {"free": free, "transcript": transcript, "drawer": drawer,
            "fits": transcript + drawer <= free}

PROMPT_MINE = "›"
PROMPT_PEER = "‹"
CURSOR_BLOCK = "▍"          # 实心：有写者
CURSOR_HOLLOW = "▏"         # 空心：光标躺着（无人执笔）


class Cockpit:
    def __init__(self, session: Session, repaint, page: ft.Page):
        self.session = session
        self.repaint = repaint
        self.page = page
        self._busy = False
        self._verifying = False
        self._budget: dict = {}
        self._buffer: list[str] = []
        self._stream_text: ft.Text | None = None
        self._view = DEFAULT_VIEW
        self._cursor_on = True
        self._cursor_task = None
        self._snapshots: list = []
        self._snap_value: str | None = None
        # CLI 功能的图形入口（new/build/edit/hub/remote/fuse/repl + 模型设置）。
        self.tools = GuiTools(session, page, repaint)
        self.panel = self._build()

    # ------------------------------------------------------------ 结构

    def _build(self) -> ft.Control:
        # ---- 执笔光标行（签名）
        self.cursor_text = mono(CURSOR_BLOCK, size=13, color=CURSOR, selectable=False)
        self.writer_text = mono("执笔 │ 未知", size=SIZE_BODY, color=TEXT,
                                weight=ft.FontWeight.BOLD, selectable=False)
        self.lamp_meta = mono("", size=SIZE_MICRO, color=DIM, selectable=False)
        self.prompt_row = ft.Row(spacing=6, controls=[
            self.cursor_text, self.writer_text,
            ft.Container(expand=True), self.lamp_meta])
        self.collab_text = mono("", size=SIZE_MICRO, color=DIM, selectable=False)

        # ---- 告警（描边块；不填色——填充是主行动的专利）
        self.dirty_banner = ft.Container(
            visible=False, padding=8, border=ft.Border.all(1, ERR),
            content=mono("", size=SIZE_DATA, color=RED, selectable=False))
        self.confirm_reason = mono("", size=SIZE_BODY, color=AMBER,
                                   weight=ft.FontWeight.BOLD, selectable=False)
        self.confirm_what = mono("", size=SIZE_DATA, color=TEXT)
        self.confirm_reversible = mono("", size=SIZE_MICRO, color=DIM)
        self.confirm_undo = mono("", size=SIZE_MICRO, color=DIM)
        self.confirm_row = ft.Container(
            visible=False, padding=8, border=ft.Border.all(1, AMBER),
            content=ft.Column(spacing=4, controls=[
                self.confirm_reason,
                mono("将做什么", size=SIZE_MICRO, color=DIM, selectable=False),
                self.confirm_what,
                self.confirm_reversible,
                self.confirm_undo,
                ft.Row(spacing=8, controls=[
                    primary("确认执行", self._on_confirm, height=24),
                    btn("拒绝", self._on_reject, tone=RED)]),
            ]))
        self.takeover_text = mono("", size=SIZE_DATA, color=RED, selectable=False)
        self.takeover_row = ft.Container(
            visible=False, padding=8, border=ft.Border.all(1, ERR),
            content=ft.Column(spacing=6, controls=[
                self.takeover_text,
                ft.Row(spacing=8, controls=[
                    btn("解除停止，继续对话", self._on_resume, tone=RED)])]))
        self.ask_text = mono("", size=SIZE_DATA, color=TEXT, selectable=False)
        self.ask_options = ft.Row(spacing=8, wrap=True, run_spacing=6, controls=[])
        self.ask_row = ft.Container(
            visible=False, padding=8, border=ft.Border.all(1, RULE),
            content=ft.Column(spacing=6, controls=[self.ask_text, self.ask_options]))

        # ---- 转录（无气泡：`时间 │ 提示符 文本`）
        self.chat_log = ft.ListView(expand=True, spacing=2, auto_scroll=True,
                                    padding=ft.Padding(0, 4, 0, 4))
        self.empty_hint = ft.Container(
            visible=False,
            content=ft.Column(spacing=4, controls=[
                mono("说一句话就开始。",
                     size=SIZE_DATA, color=DIM, selectable=False),
                ft.Row(spacing=8, wrap=True, run_spacing=4, controls=[
                    btn("› 做一个待办清单", self._example("做一个待办清单")),
                    btn("› 做一个记账小工具", self._example("做一个记账小工具")),
                    btn("› 先把窗口改小一点", self._example("先把窗口改小一点")),
                ]),
            ]))
        self.chat_box = ft.Container(
            height=TRANSCRIPT_MIN, bgcolor=BG,
            content=ft.Column(expand=True, spacing=0,
                              controls=[self.empty_hint, self.chat_log]))

        # ---- 输入行：`›` + 无壳输入 + 主行动
        self.chat_input = ft.TextField(
            hint_text="描述你想要的 app，回车或点 [发送]…",
            multiline=True, min_lines=1, max_lines=3, expand=True,
            on_submit=self._on_chat, filled=False, border=ft.InputBorder.NONE,
            border_color=ft.Colors.TRANSPARENT,
            focused_border_color=ft.Colors.TRANSPARENT,
            cursor_color=CURSOR, bgcolor=ft.Colors.TRANSPARENT,
            color=TEXT, text_size=SIZE_BODY,
            text_style=ft.TextStyle(size=SIZE_BODY, font_family=MONO,
                                    font_family_fallback=list(MONO_FALLBACK),
                                    color=TEXT),
            hint_style=ft.TextStyle(size=SIZE_BODY, font_family=MONO,
                                    font_family_fallback=list(MONO_FALLBACK),
                                    color=DIM),
            content_padding=ft.Padding(0, 6, 0, 6))
        self.send_btn = primary("发送", self._on_chat, height=26)
        self.input_row = ft.Row(spacing=8, controls=[
            mono(PROMPT_MINE, size=13, color=CURSOR, selectable=False),
            self.chat_input, self.send_btn])

        # ---- 模式（文本开关）+ 标签行
        self.mode_buttons = {name: btn(name, self._make_mode_handler(name),
                                       tone=TEXT, height=22)
                             for name in ("执行", "讨论")}
        self.mode_row = ft.Row(spacing=10, controls=[
            self.mode_buttons["执行"], self.mode_buttons["讨论"]])

        # ---- 抽屉（五块牌子，固定高度；不靠 flex）
        self.source_view = mono("", size=SIZE_DATA, color=TEXT)
        self.caps_view = mono("", size=SIZE_DATA, color=TEXT)
        self.log_view = ft.ListView(expand=True, spacing=1, auto_scroll=True)
        self.spec_view = ft.Column(spacing=2, scroll=ft.ScrollMode.AUTO)
        self.ops_view = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO)
        self.tools_view = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO)
        self.view_box = ft.Container(
            height=DRAWER_MIN, bgcolor=RAISE,
            content=ft.Column(expand=True, spacing=0, controls=[
                ft.Container(content=self.log_view, expand=True),
                ft.Container(expand=True, visible=False,
                             content=ft.Column(scroll=ft.ScrollMode.AUTO,
                                               controls=[self.source_view])),
                ft.Container(expand=True, visible=False,
                             content=ft.Column(scroll=ft.ScrollMode.AUTO,
                                               controls=[self.caps_view])),
                ft.Container(expand=True, visible=False, content=self.spec_view),
                ft.Container(expand=True, visible=False, content=self.ops_view),
                ft.Container(expand=True, visible=False, content=self.tools_view),
            ]))
        self.view_buttons = {name: btn(name, self._make_view_handler(name),
                                       tone=TEXT, height=22)
                             for name in VIEWS}
        self.tab_row = ft.Row(spacing=10, controls=[
            *(self.view_buttons[name] for name in VIEWS),
            ft.Container(expand=True)])
        self.verify_out = mono("", size=SIZE_MICRO, color=DIM, selectable=False,
                               visible=False)

        self._build_ops()
        self._build_tools()
        self.apply_budget()       # 启动就按窗口高度定比例（此刻可能还不知道，
        #                            真正的高度由 window._fit_window / on_resize 补算）

        return ft.Container(
            width=PANEL_WIDTH, bgcolor=BG,
            padding=ft.Padding(12, 8, 12, 8),
            content=ft.Column(expand=True, spacing=8, controls=[
                self.prompt_row,
                self.collab_text,
                rule(),
                self.dirty_banner,
                self.confirm_row,
                self.takeover_row,
                self.ask_row,
                self.chat_box,
                self.input_row,
                self.mode_row,
                self.tab_row,
                self.view_box,
            ]))

    def _build_ops(self) -> None:
        """「操作」抽屉：写者任职 + 系统动作 + 快照 + 自证输出。"""
        self.ops_writer = mono("", size=SIZE_DATA, color=TEXT)
        self.autonomy_log = mono("", size=SIZE_MICRO, color=DIM, selectable=False)
        self.snap_btn = btn("快照 » （无）", self._on_show_snapshots, tone=TEXT)
        self.ops_view.controls = [
            mono("▌ 写者", size=SIZE_DATA, color=TEXT, selectable=False,
                 weight=ft.FontWeight.BOLD),
            self.ops_writer,
            ft.Row(spacing=8, wrap=True, run_spacing=4, controls=[
                btn("启动自主", lambda e: self._on_writer("autonomous")),
                btn("暂停（无人写）", lambda e: self._on_writer("none")),
                btn("交回共作者", lambda e: self._on_writer("llm")),
            ]),
            mono("▌ 操作", size=SIZE_DATA, color=TEXT, selectable=False,
                 weight=ft.FontWeight.BOLD),
            ft.Row(spacing=8, wrap=True, run_spacing=4, controls=[
                btn("命名快照", self._on_named),
                btn("回滚", self._on_restore),
                btn("重载", self._on_reload),
                btn("重置状态", self._on_reset),
                btn("记忆", self._on_show_memory),
                btn("清空记忆", self._on_wipe),
                btn("自证", self._on_verify),
                btn("插件热重载", self._on_reload_plugins),
                btn("本轮 prompt", self._on_show_prompt),
            ]),
            self.snap_btn,
            ft.Row(spacing=10, wrap=True, run_spacing=4, controls=[
                mono("▌ 凭据", size=SIZE_DATA, color=TEXT, selectable=False,
                     weight=ft.FontWeight.BOLD),
                btn("模型设置", self._on_settings),
                btn("探活", self._on_probe),
            ]),
            self.verify_out,
            self.autonomy_log,
        ]

    def _build_tools(self) -> None:
        """「工具」抽屉：CLI 功能的图形入口（一条命令一个按钮，语义在 service 层）。"""
        rows = [
            ("模型设置", "base_url / 模型 / 凭据名 / 凭据值（写本机 profile 或本 app）",
             self._on_settings),
            ("人的写入", "整份替换真源（CLI edit）——干跑 + 兜底快照 + 拒稿留底",
             self.tools.human_edit),
            ("命令批", "人作驱动者的命令批（CLI repl）", self.tools.command_batch),
            ("打包", "生成独立 flet 工程 / 连跑 flet build（CLI build）", self.tools.build),
            ("编排", "多 app 拉起/停止/健康检查/协作审计（CLI hub）", self.tools.hub_tools),
            ("服务化", "把 app 暴露成可编程接口（CLI remote）", self.tools.service),
            ("融合", "把 B 并入 A（CLI fuse）——先体检干跑", self.tools.fuse),
            ("新建 app", "生成最小骨架（CLI new）", self.tools.new_app),
        ]
        self.tools_view.controls = [
            ft.Row(spacing=10, wrap=True, run_spacing=4,
                   controls=[btn(name, handler) for name, _note, handler in rows]),
        ]

    # ------------------------------------------------------------ 视图

    def _example(self, text: str):
        def handler(_event=None):
            self.chat_input.value = text
            self.repaint()
        return handler

    def _make_mode_handler(self, name: str):
        def handler(_event=None):
            self.session.set_mode(name)
            self.repaint()
        return handler

    def _make_view_handler(self, name: str):
        def handler(_event=None):
            self._set_view(None if self._view == name else name)
        return handler

    def _set_view(self, name) -> None:
        self._view = name or ""
        self._sync_views()
        self.repaint()

    def apply_budget(self) -> dict:
        """按**当前窗口高度**切分转录与抽屉（拖窗口 / 切视图 / 启动都走这里）。

        这是"比例"的唯一来源：不再有一堆手算常量各自写死高度。
        """
        name = self._view or None
        height = None
        try:
            height = self.page.window.height
        except Exception:  # noqa: BLE001 - 无窗口（冒烟/截图）时用设计稿高度
            height = None
        budget = vertical_budget(height or 692, name is not None)
        self.chat_box.height = max(TRANSCRIPT_MIN, budget["transcript"])
        self.view_box.height = budget["drawer"]
        self.view_box.visible = name is not None and budget["drawer"] > 0
        self._budget = budget
        return budget

    def _sync_views(self) -> None:
        name = self._view or None
        self.apply_budget()
        for index, label in enumerate(VIEWS):
            self.view_box.content.controls[index].visible = (label == name)
        for label, button in self.view_buttons.items():
            active = label == name
            button.content.value = "[%s]" % label if active else label
            button.content.color = TEXT if active else DIM

    # ------------------------------------------------------------ 刷新

    def refresh_views(self) -> None:
        session = self.session
        self.apply_budget()       # 每轮刷新都对一次比例（on_resize 不可靠时的兜底）
        session.check_capabilities()
        rendering = session.hello()["rendering"]
        chat = session.chat
        runner = session.autonomous
        halted = bool(chat is not None and chat.halted)
        stuck = bool(chat is not None and chat.stuck_note)

        # ---- 执笔光标（签名）：闪动只在写入进行中
        self._apply_cursor(halted)
        budget = ""
        if runner is not None:
            stats = runner.stats()
            budget = "%d/%d │ %s%s" % (stats["steps_last_hour"],
                                       stats["budget_per_hour"],
                                       "运行中" if stats["busy"] else "待命",
                                       " │ 已停手" if halted else "")
            self.autonomy_log.value = ("\n".join(
                "│ %s %s%s" % (item.get("time", "?")[11:],
                               (item.get("trigger") or "")[:30],
                               (" → " + "、".join(item.get("applied")[:1]))
                               if item.get("applied")
                               else (" → 被拒/跳过" if item.get("skipped") else ""))
                for item in reversed(runner.recent(3)))) if runner.recent(3) else ""
            self.ops_writer.value = ("自主白名单：%s"
                                     % ("、".join(stats["allow"]) or "空"))
        else:
            budget = "自主回路不可用"
            self.ops_writer.value = ""
            self.autonomy_log.value = ""
        self.lamp_meta.value = budget
        goal = session.current_goal()
        bus = getattr(session, "bus", None)
        self.collab_text.value = ("目标 %s │ 总线 %s │ 借出 %s"
                                  % (goal or "（未设）",
                                     ("127.0.0.1:%d" % bus.hub_port) if bus is not None
                                     else "未接入",
                                     "、".join(session.service_lend) or "空"))

        # ---- 告警
        dirty = session.dirty
        self.dirty_banner.visible = bool(dirty)
        if dirty:
            self.dirty_banner.content.value = (
                "! 状态标脏：%d 项没落盘，重启会丢：%s"
                % (len(dirty), dirty[0]))
        pending = chat.pending if chat is not None else None
        self.confirm_row.visible = bool(pending)
        if pending:
            body = pending.get("preview") or ""
            lines = [line for line in body.splitlines() if line.strip()]
            shown = lines[:6]
            more = "" if len(lines) <= 6 else "\n…（共 %d 行）" % len(lines)
            calls = pending.get("calls") or []
            self.confirm_reason.value = "待确认：%s" % pending.get("reason", "")
            self.confirm_what.value = "\n".join(shown) + more or "（空批）"
            self.confirm_reversible.value = ("涉及能力：%s" % "、".join(calls)) if calls \
                else "涉及能力：无"
            if pending.get("kind") == "fuse":
                self.confirm_reversible.value = "融合：B 并入 A，B 归档改名（不删）"
                self.confirm_undo.value = "可逆：执行前自动快照（操作 » 回滚）"
            elif pending.get("kind") == "replace":
                self.confirm_undo.value = "可逆：执行前自动快照（操作 » 回滚）"
        self.takeover_row.visible = halted or stuck
        if halted:
            self.takeover_text.value = "! 已停止自动重试（连续失败超过预算）。"
        elif stuck:
            self.takeover_text.value = "△ 检测到重复失败：%s" % chat.stuck_note

        # ---- 模式（文本开关：方括号标激活）
        mode = session.mode
        for name, button in self.mode_buttons.items():
            active = name == mode
            button.content.value = "[%s]" % name if active else name
            button.content.color = TEXT if active else DIM

        # ---- 规格（读数表：标签与数值分列，靠等宽对齐）
        readings = [
            ("app", session.app.name),
            ("语言", session.spec_version),
            ("节点", str(len(session.engine.program.nodes))),
            ("能力", str(len(session.catalog()))),
            ("记忆", "%d 条" % len(session.memory_entries())),
            ("渲染", "控件%d 属性%d 动效%d 图标%d"
             % (len(rendering["controls"]), len(rendering["attributes"]),
                len(rendering["animations"]), len(rendering["icons"]))),
            ("geometry", "报不出"
             if not rendering["geometry"] else "正常"),
            ("snapshot", "正常" if rendering["snapshot"] else "不支持"),
            ("链", session.plugin_chain()),
        ]
        self.spec_view.controls = [
            ft.Row(spacing=8, controls=[
                mono(label, size=SIZE_DATA, color=DIM, selectable=False, width=80),
                mono(value, size=SIZE_DATA,
                     color=AMBER if (label == "geometry" and value == "报不出")
                     else TEXT),
            ]) for label, value in readings]

        # ---- 抽屉内容
        self.source_view.value = session.program_text()
        self.caps_view.value = session.program_assets_text()
        self.log_view.controls = self._log_rows(session)
        entries = session.app.list_snapshots()[:12]
        self._snapshots = entries
        if entries and not self._snap_value:
            self._snap_value = entries[0].id
        self.snap_btn.content.value = ("快照 » %s" % self._snapshots_label()
                                       if entries else "快照 » （无）")
        self._render_history()
        self._sync_views()
        self.empty_hint.visible = not any(
            (entry.get("text") or "").strip() for entry in
            (chat.history()[-3:] if chat is not None else []))

    def _snapshots_label(self) -> str:
        for entry in self._snapshots:
            if entry.id == self._snap_value:
                return "%s %s %s" % (entry.time[11:], entry.kind, entry.reason[:14])
        return "（无）"

    def _apply_cursor(self, halted: bool) -> None:
        """光标形态 = 写者。**闪动只在写入进行中**（见模块头第 1 条纪律）。"""
        writer = self.session.writer
        if writer == "llm":
            self.cursor_text.color = CURSOR
            self.writer_text.value = "执笔 │ 共作者"
        elif writer == "autonomous":
            self.cursor_text.color = GREEN
            self.writer_text.value = "执笔 │ 自主回路"
        else:
            self.cursor_text.color = DIM
            self.writer_text.value = "无人执笔（暂停）"
        writing = self._writing_now()
        if writer == "none" or not writing:
            self.cursor_text.value = CURSOR_HOLLOW if writer == "none" else CURSOR_BLOCK
            self._cursor_on = True

    def _writing_now(self) -> bool:
        """正在写 = 流式输出中 / 自主步进中 / 有未落盘的改动。"""
        chat = self.session.chat
        if chat is not None and getattr(chat, "streaming", False):
            return True
        runner = self.session.autonomous
        if runner is not None and getattr(runner, "busy", False):
            return True
        return self._busy

    async def cursor_loop(self) -> None:
        """唯一的动画：写时闪、待命静止（500ms）。"""
        while True:
            await asyncio.sleep(0.5)
            try:
                if self._writing_now() and self.session.writer != "none":
                    self._cursor_on = not self._cursor_on
                    self.cursor_text.value = CURSOR_BLOCK if self._cursor_on \
                        else " "
                    self.page.update()
                elif self.cursor_text.value == " ":
                    self.cursor_text.value = CURSOR_BLOCK
                    self.page.update()
            except Exception:  # noqa: BLE001 - 关窗竞态：静默退出即可
                return

    def _log_rows(self, session) -> list:
        rows = []
        for entry in list(session.log)[-200:]:
            mark, tone = LEVEL_MARK.get(entry["level"], ("│", DIM))
            where = (entry.get("where") or "").strip()
            text = entry["text"] if not where else "%s %s" % (where, entry["text"])
            rows.append(ft.Text(spans=[
                ft.TextSpan("%s " % entry["time"],
                            style=ft.TextStyle(font_family=MONO, size=SIZE_MICRO,
                                               color="#5C5C50",
                                               font_family_fallback=list(MONO_FALLBACK))),
                ft.TextSpan("%s " % mark,
                            style=ft.TextStyle(font_family=MONO, size=SIZE_MICRO,
                                               color=tone,
                                               font_family_fallback=list(MONO_FALLBACK))),
                ft.TextSpan("%-15s" % entry["code"],
                            style=ft.TextStyle(font_family=MONO, size=SIZE_MICRO,
                                               color=tone,
                                               font_family_fallback=list(MONO_FALLBACK))),
                # 诊断消息走**内联标记**：`**密钥被拒（401）**` 这类强调渲染成
                # 加粗琥珀，而不是把星号当字面量打出来（实测踩过）。
                *theme.spans(text, size=SIZE_DATA),
            ], selectable=True, no_wrap=False))
        return rows

    # ------------------------------------------------------------ 转录

    @staticmethod
    def _line(when: str, glyph: str, text: str, glyph_tone: str,
              body_tone: str = TEXT) -> ft.Row:
        """一行转录：`时间 │ 提示符 文本`——没有气泡，没有头像，只有栅格。"""
        return ft.Row(spacing=6, vertical_alignment=ft.CrossAxisAlignment.START,
                      controls=[
                          mono(when or "        ", size=SIZE_MICRO, color="#5C5C50",
                               selectable=False),
                          mono("│", size=SIZE_MICRO, color=RULE, selectable=False),
                          mono(glyph, size=SIZE_BODY, color=glyph_tone,
                               selectable=False),
                          mono(text, size=SIZE_BODY, color=body_tone, expand=True),
                      ])

    def _render_history(self) -> None:
        """转录 = 过程记忆的原文（不摘要），最新的在下方。"""
        chat = self.session.chat
        if chat is None:
            return
        controls = []
        for entry in chat.history()[-14:]:
            role = entry.get("role")
            text = (entry.get("text") or "").strip()
            if not text:
                continue
            mine = role == "user"
            controls.append(self._line(entry.get("time", "")[11:],
                                       PROMPT_MINE if mine else PROMPT_PEER,
                                       text, CURSOR if mine else DIM))
        if self._stream_text is not None:
            controls.append(self._line("", PROMPT_PEER, "", DIM))
            controls[-1].controls[3] = self._stream_text
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
        stream = mono("", size=SIZE_BODY, color=TEXT, expand=True)
        self._stream_text = stream
        self.chat_log.controls = list(self.chat_log.controls) + [
            self._line("", PROMPT_MINE, text, CURSOR),
            self._line("", PROMPT_PEER, "", DIM),
        ]
        self.chat_log.controls[-1].controls[3] = stream
        self.send_btn.disabled = True
        self.page.update()
        self.page.run_thread(self._run_turn, text)

    def _run_turn(self, text: str) -> None:
        try:
            result = self.session.chat_turn(text)
        except Exception as ex:  # noqa: BLE001 - 不能让它静默吞掉
            self.session.note("error", "CHAT_ERROR",
                              "本轮异常：%s: %s" % (type(ex).__name__, ex))
            result = None
        finally:
            self._busy = False
            self.send_btn.disabled = False
        self._on_delta_done(result)

    def push_delta(self, piece: str) -> None:
        """流式片段（由 `Session.on_delta` 从工作线程回调进来）。"""
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
                self.session.note("error", "LLM_HALTED",
                                  "已停止自动重试：请人工接管或要求推倒重来")
        self._stream_text = None
        self.repaint()

    def _show_question(self, question: dict) -> None:
        options = question.get("options") or []
        controls = [btn(str(option), self._answer(option)) for option in options]
        controls.append(btn("跳过", self._answer(None), tone=DIM))
        self.ask_row.visible = True
        self.ask_text.value = question.get("question") or "需要你确认"
        self.ask_options.controls = controls

    def _answer(self, value):
        def handler(_event=None):
            self.ask_row.visible = False
            if value:
                self.chat_input.value = str(value)
                self._on_chat()
            else:
                self.repaint()
        return handler

    # ------------------------------------------------------------ 动作

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

    def _on_named(self, _event=None) -> None:
        self.session.named_snapshot("里程碑 %s" % self.session.app.name)
        self.repaint()

    def _on_restore(self, _event=None) -> None:
        if not self._snap_value:
            self.session.note("warning", "RESTORE", "没有可回滚的快照")
        else:
            self.session.restore(self._snap_value)
        self.repaint()

    def _on_show_snapshots(self, _event=None) -> None:
        """快照选择 = 自绘浮层（零圆角、一列文本行）。"""
        if not self._snapshots:
            self.session.note("warning", "RESTORE", "还没有快照")
            self.repaint()
            return
        rows = []
        for entry in self._snapshots:
            label = "%s  %-8s %s" % (entry.time[11:], entry.kind, entry.reason[:24])
            active = entry.id == self._snap_value
            rows.append(btn("[%s]" % label if active else " %s" % label,
                            self._pick_snapshot(entry.id), tone=TEXT if active else DIM))

        def confirm(_event=None):
            self._close_overlay()
            self._on_restore()

        self._overlay("快照", ft.Column(spacing=2, controls=[
            mono("选中后按 [回滚] 落到那一份。",
                 size=SIZE_MICRO, color=DIM, selectable=False),
            *rows]), actions=[primary("回滚到选中", confirm, height=24),
                              btn("关闭", lambda _e: self._close_overlay())])

    def _pick_snapshot(self, sid: str):
        def handler(_event=None):
            self._snap_value = sid
            self.snap_btn.content.value = "快照 » %s" % self._snapshots_label()
            self._close_overlay()
            self.repaint()
        return handler

    def _on_reload(self, _event=None) -> None:
        self.session.reload()
        self.repaint()

    def _on_reset(self, _event=None) -> None:
        self.session.reset()
        self.repaint()

    def _on_wipe(self, _event=None) -> None:
        self.session.wipe_memory(origin="user")
        self.repaint()

    def _on_show_memory(self, _event=None) -> None:
        body = ft.Column(scroll=ft.ScrollMode.AUTO, controls=[
            mono(line, size=SIZE_DATA, color=TEXT)
            for line in (self.session.memory_text() or "（空）").splitlines()])
        self._overlay("运行期记忆", body)

    def _on_settings(self, _event=None) -> None:
        """模型设置浮层（base_url / 模型 / 凭据名 / 凭据值）——见 `GuiTools.settings`。"""
        self.tools.settings()

    def _on_probe(self, _event=None) -> None:
        """探活 + 泄漏自检：401 / 403 / 404 / 429 / 超时 / 连不上分开说清。

        没有探活，这六件事全都只在一次真实对话里撞出来——代价是一整轮上下文。
        """
        self.verify_out.value = "探活中…（一次最小请求）"
        self.repaint()
        self.page.run_thread(self._run_probe)

    def _run_probe(self) -> None:
        try:
            result = self.session.check_credentials()
        except Exception as ex:  # noqa: BLE001 - 探活自己失败也要可见
            self.verify_out.value = "探活失败：%s: %s" % (type(ex).__name__, ex)
        else:
            self.verify_out.value = result["text"]
        self.verify_out.visible = bool(self.verify_out.value)
        self.repaint()

    def _on_reload_plugins(self, _event=None) -> None:
        self.session.reload_plugins()
        self.repaint()

    def _on_verify(self, _event=None) -> None:
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
        body = ft.Column(scroll=ft.ScrollMode.AUTO, controls=[
            mono("改写链：%s" % chain, size=SIZE_MICRO, color=DIM, selectable=False),
            mono("\n\n".join(blocks), size=SIZE_DATA, color=TEXT)])
        self._overlay("本轮实际发送的 prompt", body)

    # ------------------------------------------------------------ 浮层（自绘）

    def _overlay(self, title: str, body: ft.Control, actions=None) -> None:
        """自绘浮层：全屏 scrim + 1px 描边框。不用 AlertDialog（圆角+阴影是重灾区）。"""
        scrim = ft.Container(
            bgcolor=ft.Colors.with_opacity(0.86, BG), expand=True,
            alignment=ft.Alignment.CENTER,
            content=ft.Container(
                width=720, padding=12, bgcolor=BG,
                border=ft.Border.all(1, RULE),
                content=ft.Column(expand=True, spacing=8, controls=[
                    mono(title, size=SIZE_BODY, color=AMBER,
                         weight=ft.FontWeight.BOLD, selectable=False),
                    rule(),
                    ft.Container(content=body, expand=True),
                    ft.Row(spacing=8, controls=(
                        list(actions) if actions else
                        [btn("关闭", lambda _e: self._close_overlay())])),
                ])))
        self.page.overlay[:] = [ft.Container(
            content=scrim, expand=True,
            bottom=0, top=0, left=0, right=0)]
        self.page.update()

    def _close_overlay(self) -> None:
        self.page.overlay[:] = []
        self.page.update()
