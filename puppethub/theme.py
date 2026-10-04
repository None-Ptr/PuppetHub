"""主题：**磷光终端（S2）**。唯一依据是 `docs/style-terminal.md`——那份文档里的
每一条都在这里有出处；这里没有的，不许出现在界面上。

四件事：

1. **字体**：拉丁走 Cascadia Mono，中文走显式回退 Noto Sans SC（`font_family_fallback`）
   ——不碰运气。尺寸只三级：12 / 11.5 / 10.5。
2. **颜色**：九个值，各有唯一角色。光标是**全篇最亮的**（终端本来如此）；
   琥珀只给"警告 + 主行动的反白块"；除这四色不许有别的颜色。
3. **控件纯度**：能力保留（键盘 / 滚动 / 编辑），外观一律重铸——所有按钮仍是
   `ft.Button`（保键盘焦点），只是透明底、无描边、零圆角、无阴影。
4. **零圆角、零阴影、零动效**（唯一的动画是驾驶舱里的执笔光标，且在 cockpit 里）。
"""

from __future__ import annotations

import re

import flet as ft

# ---------------------------------------------------------------- 颜色（九值）

BG = "#0A0C08"          # 底：暖黑（带绿褐；纯黑杀磷光）
RAISE = "#12140D"       # 面：抽屉 / 日志底（用面色分块，不用卡片）
RULE = "#2A2C22"        # 1px 线（看得见）
TEXT = "#E8E2D4"        # 主文（暖白）
DIM = "#9A968A"         # 标签 / 注解 / 时间戳
CURSOR = "#F7F3E6"      # 执笔光标——最亮
AMBER = "#FFB000"       # 语义：警告 ＋ 主行动反白块
GREEN = "#5CD07A"       # 语义：通过 / 自主 / 成功
RED = "#FF5C4D"         # 语义：错误

# 迁移期的旧名字（V4 前的暗色总控台）——保留指向，避免一次性重命名扯断别处。
INK = BG
SLATE = RAISE
SEAM = RULE
MIST = TEXT
BRASS = AMBER
OK = GREEN
ERR = RED
WARN = AMBER
SLATE2 = "#171A10"
ACTIVE = "#1A1D12"
STAGE = BG

# ---------------------------------------------------------------- 字体

MONO = "Cascadia Mono"
MONO_FALLBACK = ["Consolas", "Noto Sans SC"]
UI = MONO                     # 终端里没有第二种字体
UI_FALLBACK = MONO_FALLBACK

# 尺寸阶梯（只三级）
SIZE_BODY = 12
SIZE_DATA = 11.5
SIZE_MICRO = 10.5

LEVEL_MARK = {"错误": ("●", RED), "警告": ("▲", AMBER), "信息": ("·", DIM)}


# ---------------------------------------------------------------- 文本

# 内联强调：`**粗**` 与 `` `具体值` `` ——**只给界面文案与诊断用**。
#
# 为什么默认关闭：程序真源 / capabilities.py / JSON / 配置这些**内容**里，`**` 是
# Python 的幂运算符、`#` 是注释——把内容当标记解析就是渲染器侵入了它不该管的地方。
# 所以字面量一律走 `mono()`（原样），文案走 `mono(..., md=True)`。
_MD_RE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`")


def spans(text: str, size: float = SIZE_DATA) -> list:
    """文案 → `ft.TextSpan` 列表（内联标记见 `_MD_RE`）。行内复用，免得每处重写解析。"""
    source = text or ""
    out: list = []
    pos = 0
    for match in _MD_RE.finditer(source):
        if match.start() > pos:
            out.append(_span(source[pos:match.start()], size))
        strong, code = match.group(1), match.group(2)
        out.append(_span(strong if strong is not None else code, size,
                         bold=strong is not None, tone=AMBER))
        pos = match.end()
    if pos < len(source):
        out.append(_span(source[pos:], size))
    return out or [_span(source, size)]


def _span(text: str, size: float, bold: bool = False, tone=None) -> ft.TextSpan:
    return ft.TextSpan(text, style=ft.TextStyle(
        font_family=MONO, size=size, color=tone,
        font_family_fallback=list(MONO_FALLBACK),
        weight=ft.FontWeight.BOLD if bold else None))


def markup(text: str, size: float = SIZE_DATA, color: str = TEXT,
           weight=None, selectable: bool = True, **kw) -> ft.Text:
    """把界面文案里的内联标记渲染成真实字重/颜色（**不残留 `**` 字符**）。

    强调 = 加粗 + 琥珀（本项目琥珀只给"警告 + 主行动"之外的第三种合法用途：
    文案里的**关键词**——它不抢语义色的位置，只让人一眼扫到重点）。
    """
    return ft.Text(spans=spans(text, size), size=size, color=color, weight=weight,
                   font_family=MONO, font_family_fallback=list(MONO_FALLBACK),
                   selectable=selectable, **kw)


def set_md(control, text: str, size: float = SIZE_DATA) -> None:
    """给**动态更新**的 `ft.Text` 换上文案（构造时是空串，`md=` 那时判不出来）。"""
    control.spans = spans(text, size)
    control.value = None


def plain(text: str) -> str:
    """去掉内联标记（CLI 打印没有渲染器——`**` 在那里就是字面噪声）。"""
    return _MD_RE.sub(lambda m: m.group(1) if m.group(1) is not None else m.group(2),
                      text or "")


def mono(text: str, size: float = SIZE_DATA, color: str = TEXT,
         weight=None, selectable: bool = True, md: bool = False, **kw) -> ft.Text:
    """等宽文本。`md=True` 才解析内联标记——**内容一律不开**（见上面的注释）。"""
    if md and ("**" in (text or "") or "`" in (text or "")):
        return markup(text, size=size, color=color, weight=weight,
                      selectable=selectable, **kw)
    return ft.Text(text, size=size, color=color, weight=weight,
                   font_family=MONO, font_family_fallback=list(MONO_FALLBACK),
                   selectable=selectable, **kw)


# 终端里"界面字"就是等宽字——保留别名，读代码时意图更清楚。
def ui(text: str, size: float = SIZE_BODY, color: str = TEXT,
       weight=None, **kw) -> ft.Text:
    return mono(text, size=size, color=color, weight=weight, **kw)


def micro(text: str, color: str = DIM, **kw) -> ft.Text:
    """微标签：只给拉丁大写用（中文照常写——大写中文是伪概念）。"""
    return mono(text, size=SIZE_MICRO, color=color, **kw)


# ---------------------------------------------------------------- 几何

def rule(width=None) -> ft.Container:
    """1px 线。终端的分隔是线，不是间距。

    **不要 `expand`**：`expand` 的语义随父容器而变——在 Row 里是横向伸展，在
    Column 里是**竖向伸展**。一条 1px 线放进 Column 里会变成一根灰柱（实测踩过：
    设置浮层顶部那根粗灰条）。Column 的子控件本就会横向铺满，所以这里什么都不用做。
    """
    return ft.Container(height=1, bgcolor=RULE, width=width)


def gap(height: float = 8) -> ft.Container:
    return ft.Container(height=height)


def indent(width: float) -> ft.Container:
    return ft.Container(width=width)


def box(content, tone: str = RULE, bgcolor: str | None = None,
        padding: int = 8) -> ft.Container:
    """描边块：零圆角、1px 线、可有可无的底。替代一切"卡片"。"""
    return ft.Container(content=content, padding=padding, bgcolor=bgcolor,
                        border=ft.Border.all(1, tone), border_radius=0)


# ---------------------------------------------------------------- 控件

def btn(label: str, handler=None, tone: str = DIM, bold: bool = False,
        height: int = 24, selectable: bool = False) -> ft.Button:
    """幽灵按钮：仍是 `ft.Button`（保键盘可达），但外观只是一段文字。

    零圆角、无描边、无阴影、透明底——Material 的出厂长相全被剥掉，能力留下。
    """
    return ft.Button(
        content=mono(label, size=SIZE_DATA, color=tone,
                     weight=ft.FontWeight.BOLD if bold else None,
                     selectable=selectable),
        on_click=handler, height=height, style=ft.ButtonStyle(
            bgcolor=ft.Colors.TRANSPARENT, color=tone, elevation=0,
            padding=ft.Padding(6, 2, 6, 2), overlay_color="#1A1D12",
            shape=ft.RoundedRectangleBorder(radius=0)))


def primary(label: str, handler=None, height: int = 28) -> ft.Button:
    """主行动：**琥珀反白块**——全篇唯一允许填充的矩形。"
    任务"一次只炫一处"，额度全花在这里。
    """
    return ft.Button(
        content=mono(label, size=SIZE_DATA, color=BG,
                     weight=ft.FontWeight.BOLD, selectable=False),
        on_click=handler, height=height, style=ft.ButtonStyle(
            bgcolor=AMBER, color=BG, elevation=0,
            padding=ft.Padding(12, 2, 12, 2),
            shape=ft.RoundedRectangleBorder(radius=0)))


def ghost(label, handler, height=30):        # 兼容旧调用点：等价于 btn()
    return btn(label, handler, tone=DIM, height=min(height, 24))


def soft(label, handler, tone: str = RED, height=30):   # 同上
    return btn(label, handler, tone=tone, height=min(height, 24))


def card(content, **kw):                      # 兼容旧调用点：等价于 box()
    return box(content, **kw)


def section(label: str, note: str = "") -> ft.Row:
    """小节标题：`▌ 名字` + 注解——块字符前缀替代图标。"""
    controls = [mono("▌ ", color=AMBER, selectable=False),
                mono(label, color=TEXT, weight=ft.FontWeight.BOLD)]
    if note:
        controls.append(mono(note, color=DIM))
    return ft.Row(spacing=2, controls=controls)


# ---------------------------------------------------------------- 表单原语
#
# 表单只有两种控件：**一行输入**与**多行文本域**。终端风里它们都是"一条底线 +
# 等宽字"——Material 的填充与圆角一概不要，但键盘可达性（TextField 本身）保留。

def field(value: str = "", hint: str = "", password: bool = False,
          width=None, on_submit=None) -> ft.TextField:
    return ft.TextField(
        value=value or "", hint_text=hint or "", password=password,
        width=width, on_submit=on_submit, filled=False,
        border=ft.InputBorder.UNDERLINE, border_color=RULE,
        focused_border_color=AMBER, cursor_color=CURSOR, color=TEXT,
        text_size=SIZE_DATA,
        text_style=ft.TextStyle(size=SIZE_DATA, font_family=MONO,
                                font_family_fallback=list(MONO_FALLBACK),
                                color=TEXT),
        hint_style=ft.TextStyle(size=SIZE_DATA, font_family=MONO,
                                font_family_fallback=list(MONO_FALLBACK),
                                color=DIM),
        content_padding=ft.Padding(0, 4, 0, 4))


def area(value: str = "", lines: int = 14, hint: str = "") -> ft.TextField:
    """多行文本域（程序真源 / 命令批 / 构建输出）。"""
    return ft.TextField(
        value=value or "", hint_text=hint or "", multiline=True,
        min_lines=lines, max_lines=lines, filled=False,
        border=ft.InputBorder.NONE, border_color=ft.Colors.TRANSPARENT,
        focused_border_color=ft.Colors.TRANSPARENT, cursor_color=CURSOR,
        color=TEXT, text_size=SIZE_DATA,
        text_style=ft.TextStyle(size=SIZE_DATA, font_family=MONO,
                                font_family_fallback=list(MONO_FALLBACK),
                                color=TEXT),
        hint_style=ft.TextStyle(size=SIZE_DATA, font_family=MONO,
                                font_family_fallback=list(MONO_FALLBACK),
                                color=DIM),
        content_padding=ft.Padding(2, 4, 2, 4))


def labeled(label: str, control, label_width: int = 84) -> ft.Row:
    """`标签  控件`：终端里的表单是**对齐的列**，标签宽度固定。"""
    return ft.Row(spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER, controls=[
        mono(label, size=SIZE_DATA, color=DIM, selectable=False, width=label_width),
        ft.Container(content=control, expand=True)])


# ---------------------------------------------------------------- 自绘浮层

def close_overlay(page) -> None:
    page.overlay[:] = []
    page.update()


def overlay_scrim(page, title: str, body, actions=None, width: int = 760) -> None:
    """自绘浮层：全屏 scrim + 1px 描边框（零圆角、零阴影）。

    CLI 的功能搬进 GUI 后，参数表单与运行输出都长在这里——与 `AlertDialog`
    无关（圆角 + 阴影是终端风的重灾区）。
    """
    if actions is None:
        actions = [btn("关闭", lambda _e: close_overlay(page))]
    scrim = ft.Container(
        bgcolor=ft.Colors.with_opacity(0.86, BG), expand=True,
        alignment=ft.Alignment.CENTER,
        content=ft.Container(
            width=width, padding=12, bgcolor=BG,
            border=ft.Border.all(1, RULE),
            content=ft.Column(expand=True, spacing=8, controls=[
                # 标题是**文案**：走标记解析，否则 `` `new` `` 这种反引号会字面上屏
                mono(title, size=SIZE_BODY, color=AMBER,
                     weight=ft.FontWeight.BOLD, selectable=False, md=True),
                rule(),
                ft.Container(content=body, expand=True),
                ft.Row(spacing=8, wrap=True, run_spacing=6, controls=list(actions)),
            ])))
    page.overlay[:] = [ft.Container(content=scrim, expand=True,
                                    top=0, bottom=0, left=0, right=0)]
    page.update()


# ---------------------------------------------------------------- 页面主题

def color_scheme() -> ft.ColorScheme:
    """Material 色彩槽位 ← 手册的九色。

    TextField / ListView 这些底层控件**不听 bgcolor、只读 color_scheme**，
    所以主题必须铸成这台仪器的样子——同一个真相只留一处。
    """
    return ft.ColorScheme(
        primary=AMBER, on_primary=BG,
        primary_container="#3A2A00", on_primary_container=AMBER,
        secondary=GREEN, on_secondary=BG, secondary_container="#0F2A16",
        error=RED, on_error=BG, error_container="#2A0F0C",
        surface=BG, on_surface=TEXT, on_surface_variant=DIM,
        surface_dim=BG, surface_bright=RAISE,
        surface_container_lowest="#070805", surface_container_low=BG,
        surface_container=RAISE, surface_container_high="#171A10",
        surface_container_highest="#1E2214",
        outline=RULE, outline_variant="#1C1E16",
        surface_tint=AMBER, inverse_surface=TEXT, inverse_primary="#3A2A00")


def page_theme() -> ft.Theme:
    # `Theme` 没有 font_family_fallback（只有 Text/TextStyle 有）——中文回退
    # 在每个 Text 上显式给（见 mono()），这里只钉主字族。
    return ft.Theme(color_scheme=color_scheme(), font_family=MONO,
                    use_material3=True,
                    divider_color=RULE, splash_color="#1A1D12",
                    highlight_color="#171A10", hover_color="#14170E",
                    focus_color="#1A1D12", unselected_control_color=DIM,
                    disabled_color="#4A4A42")
