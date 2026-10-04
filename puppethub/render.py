"""渲染层：程序 IR + 观察面 → flet 控件树。

四条原则：

1. **只经观察面**。渲染器读程序 IR（结构、引用类属性）与 `Engine.observe()`
   （值、状态标志、模板行数据），不碰引擎的私有求值器——引擎与渲染器之间只有这一条缝。
2. **声明即实现**。`RENDERING` 由映射表**本身**派生，不手写；"声明了却做不到"
   在导入期就会炸（见文件末尾的三处一致性检查）。
3. **交互交给引擎**。用户动作 → `Session.fire`，渲染器不实现任何业务反应。
4. **降级必须可见**。取值转换不了、属性没有落点、图标解析不出——一律记一条 note，
   由宿主收进观察流，绝不静默丢弃。

`geometry` 只能声明 `false`：flet 能给出控件**尺寸**却**给不出位置**
（`LayoutSizeChangeEvent` 只有 w/h，也没有查询任意控件坐标的 API）。四分量缺两个，
所以这是**诚实降级**而不是缺陷——"声明即 oracle"的机制下，说出来了就算通过，静默才是失败。
"""

from __future__ import annotations

import asyncio
import os
from typing import Callable, Iterable, Optional

import flet as ft

from puppet import vocab

_MISSING = object()
_EATEN = object()

# ------------------------------------------------------------------ 图标

# spike 1 实测：65 个核心图标全部可用，其中 22 个需要 material 别名——material 换过名字。
ICON_ALIASES = {
    "arrow_up": "ARROW_UPWARD",
    "arrow_down": "ARROW_DOWNWARD",
    "bell": "NOTIFICATIONS",
    "calendar": "CALENDAR_MONTH",
    "cart": "SHOPPING_CART",
    "chart": "BAR_CHART",
    "chevron_up": "EXPAND_LESS",
    "chevron_down": "EXPAND_MORE",
    "clock": "SCHEDULE",
    "document": "DESCRIPTION",
    "eye": "VISIBILITY",
    "eye_off": "VISIBILITY_OFF",
    "file": "INSERT_DRIVE_FILE",
    "grid": "GRID_VIEW",
    "location": "PLACE",
    "minus": "REMOVE",
    "play": "PLAY_ARROW",
    "plus": "ADD",
    "success": "CHECK_CIRCLE",
    "unlock": "LOCK_OPEN",
    "user": "PERSON",
    "users": "GROUP",
}


def icon_value(name: str):
    """语言图标名 → `ft.Icons` 成员；解析不出返回 `None`（由调用方走可见降级）。"""
    member = ICON_ALIASES.get(name, str(name).upper())
    return getattr(ft.Icons, member, None)


# ------------------------------------------------------------------ 属性映射

# 语言属性 → flet 字段（按控件差异会在 `_field_for` 里做小范围改写）。
_SIMPLE = {
    "pad": "padding", "margin": "margin", "bgcolor": "bgcolor", "gradient": "gradient",
    "radius": "border_radius", "border": "border", "shadow": "shadow", "opacity": "opacity",
    "gap": "spacing", "wrap": "wrap", "flex": "expand", "scroll": "scroll",
    "w": "width", "h": "height", "x": "left", "y": "top", "offset": "offset",
    "scale": "scale", "rotate": "rotate",
    "fg": "color", "size": "size", "weight": "weight", "italic": "italic",
    "font": "font_family", "tooltip": "tooltip",
    "text": "value", "icon": "icon", "src": "src", "fit": "fit", "initials": None,
    "value": "value", "selected": "value", "min": "min", "max": "max",
    "step": "divisions", "placeholder": "hint_text", "title": None, "primary": None,
    "option_label": None, "option_value": None,
    "states": None, "animate": None, "duration": None, "curve": None,
}

# 盒模型：容器类控件（`col` / `row`）没有这些字段，必须由 `ft.Container` 承载。
_BOX = {"pad": "padding", "margin": "margin", "bgcolor": "bgcolor",
        "gradient": "gradient", "radius": "border_radius", "border": "border",
        "shadow": "shadow"}

# 少数属性在特定控件上的落点与默认表不同（`size` 在输入框里是文字大小）。
_FIELD_OVERRIDES = {
    "input": {"size": "text_size"},
    "avatar": {"size": "radius"},
    "divider": {"fg": "color"},
    "progress": {"fg": "color"},
    "checkbox": {"fg": "active_color"},
    "switch": {"fg": "active_color"},
    "slider": {"fg": "active_color"},
}

# 有专属 `animate_*` 字段的可动画属性；其余属性走通用 `animate`。
_ANIMATE_FIELD = {
    "opacity": "animate_opacity", "w": "animate_size", "h": "animate_size",
    "size": "animate_size", "x": "animate_position", "y": "animate_position",
    "offset": "animate_offset", "scale": "animate_scale", "rotate": "animate_rotation",
    "margin": "animate_margin", "align": "animate_align",
}

_JUSTIFY = {"start": "START", "center": "CENTER", "end": "END",
            "space_between": "SPACE_BETWEEN", "between": "SPACE_BETWEEN",
            "space_around": "SPACE_AROUND", "around": "SPACE_AROUND",
            "space_evenly": "SPACE_EVENLY", "evenly": "SPACE_EVENLY"}
_ALIGN = {"start": "START", "center": "CENTER", "end": "END",
          "stretch": "STRETCH", "baseline": "BASELINE",
          "space_between": "SPACE_BETWEEN", "space_around": "SPACE_AROUND",
          "space_evenly": "SPACE_EVENLY"}
_FIT = {"cover": "COVER", "contain": "CONTAIN", "fill": "FILL",
        "fit_width": "FIT_WIDTH", "fit_height": "FIT_HEIGHT",
        "none": "NONE", "scale_down": "SCALE_DOWN"}
_WEIGHT = {"thin": "W_100", "light": "W_300", "normal": "NORMAL", "regular": "NORMAL",
           "medium": "W_500", "semibold": "W_600", "bold": "BOLD", "black": "W_900"}
_SCROLL = {"auto": "AUTO", "always": "ALWAYS", "hidden": "HIDDEN",
           "adaptive": "ADAPTIVE", "none": "NONE"}

_STATE_ATTRS = ("hover", "focus", "pressed", "error")

# 交互事件 → flet 部件上的回调字段名（用户动作投递要经它，而不是绕过界面进引擎）。
_EVENT_HANDLER = {"click": "on_click", "change": "on_change", "submit": "on_submit",
                  "focus": "on_focus", "blur": "on_blur"}


class _SyntheticEvent:
    """模拟部件事件的形状：渲染器的处理器只读 `control` 与 `data`。"""

    def __init__(self, control, data=None):
        self.control = control
        self.data = data


def _as_dicts(items) -> list:
    out = []
    for item in items or []:
        out.append(item.to_dict() if hasattr(item, "to_dict") else dict(item))
    return out

# 不走 `_decorate` 的通用落点、由别处专门处理的属性（不该报"没有落点"）。
_META_HANDLED = frozenset(("states", "animate", "duration", "curve"))


def _enum(owner, table: dict, value):
    member = table.get(str(value).lower().strip())
    if member is None:
        raise ValueError("未知取值 %r（可用：%s）" % (value, ", ".join(sorted(table))))
    found = getattr(owner, member, None)
    if found is None:
        raise ValueError("目标枚举没有成员 %s" % member)
    return found


def _spacing(factory):
    """`pad` / `margin` 的取值形态：单值，或"上下 左右"两值。"""
    def convert(value):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return factory.all(value)
        parts = [int(p) for p in str(value).replace(",", " ").split()]
        if len(parts) == 1:
            return factory.all(parts[0])
        top, right = parts[0], parts[1]
        return factory(top=top, right=right, bottom=top, left=right)
    return convert


def _border(value):
    text = str(value).split()
    width = float(text[0]) if text and text[0].replace(".", "", 1).isdigit() else 1
    color = text[1] if len(text) > 1 else "#000000"
    return ft.Border.all(width, color)


def _gradient(value):
    colors = value if isinstance(value, (list, tuple)) else str(value).split()
    colors = [str(c) for c in colors]
    if len(colors) < 2:
        raise ValueError("渐变需要两个颜色，得到 %r" % (value,))
    return ft.LinearGradient(colors=colors[:2],
                             begin=ft.Alignment.TOP_LEFT, end=ft.Alignment.BOTTOM_RIGHT)


def _shadow(value):
    """语言里 `shadow` 是**强度**（数字），不是完整的阴影对象。"""
    strength = float(value)
    if strength <= 0:
        return None
    return ft.BoxShadow(blur_radius=strength * 4, spread_radius=0,
                        offset=ft.Offset(0, max(1.0, strength)),
                        color="#00000033")


def _offset(value):
    if isinstance(value, (list, tuple)):
        if len(value) != 2:
            raise ValueError("偏移需要两个分量")
        return ft.Offset(float(value[0]), float(value[1]))
    text = str(value).split()
    if len(text) != 2:
        raise ValueError("偏移需要两个分量，得到 %r" % (value,))
    return ft.Offset(float(text[0]), float(text[1]))


_CONVERTERS = {
    "pad": _spacing(ft.Padding), "margin": _spacing(ft.Margin),
    "border": _border, "gradient": _gradient,
    "shadow": _shadow, "offset": _offset,
    "fit": lambda v: _enum(ft.BoxFit, _FIT, v),
    "weight": lambda v: _enum(ft.FontWeight, _WEIGHT, v),
    "scroll": lambda v: _enum(ft.ScrollMode, _SCROLL, v),
    "step": lambda v: int(v),
    "radius": lambda v: int(v),
    "duration": lambda v: int(v),
    "tooltip": lambda v: None if v is None else str(v),
}


def _supports(cls, field: str) -> bool:
    return field in (getattr(cls, "__dataclass_fields__", {}) or {})


def _field_for(node_type: str, attr: str) -> Optional[str]:
    override = _FIELD_OVERRIDES.get(node_type, {})
    if attr in override:
        return override[attr]
    return _SIMPLE.get(attr)


# ------------------------------------------------------------------ 声明

# 两个不同的数字，别混：
# - `RENDERABLE_CONTROLS` = **画出来的**控件（20 个）：`template` 是行上下文，不是控件，
#   它由 `list` 展开成行——所以它没有"画法"。
# - `DECLARED_CONTROLS` = 能力声明里的 `controls`（21 个）：声明回答的是"这个词汇我处理得了吗"。
#   `template` **必须**声明——我们确实展开它；不声明反而会让每个用了模板的程序都产生
#   一条假的 `DEGRADED_FEATURE(control:template)`。
RENDERABLE_CONTROLS = [t for t in vocab.NODE_TYPES if t != "template"]
DECLARED_CONTROLS = sorted(vocab.NODE_TYPES)

SUPPORTED_ATTRS = (set(_SIMPLE) | {"justify", "align"}
                   | set(vocab.REF_ATTRS) | set(vocab.STATE_FLAGS))

RESOLVED_ICONS = sorted(name for name in vocab.CORE_ICONS if icon_value(name) is not None)
UNRESOLVED_ICONS = sorted(name for name in vocab.CORE_ICONS if icon_value(name) is None)


def describe_rendering() -> dict:
    return {
        "controls": list(DECLARED_CONTROLS),
        "attributes": sorted(SUPPORTED_ATTRS),
        "animations": sorted(vocab.ANIMATABLE),
        "icons": RESOLVED_ICONS,
        "geometry": False,
        "snapshot": True,
        "interaction": True,
        "headless": True,
        "pointer": "mouse",
        "notes": ("flet 实现：词汇四层全覆盖；geometry=false 是**诚实降级**——"
                  "flet 只给尺寸不给位置（无查询任意部件坐标的 API）；"
                  "snapshot=true（隐藏窗口下实测可用）；interaction 经部件自身的事件绑定"
                  "投递，模板内多行实例的程序化投递因缺少行上下文而**可见降级**。"),
    }


RENDERING = describe_rendering()


# ------------------------------------------------------------------ 渲染器

class FletRenderer:
    """把 IR 画成 flet 控件树。宿主（窗口 / 控制面）只需给它一个挂载点。"""

    def __init__(self, page: ft.Page, engine, on_event: Callable,
                 on_local_change: Optional[Callable[[], None]] = None,
                 assets_dir: Optional[str] = None,
                 host: Optional[ft.Control] = None):
        self.page = page
        self.engine = engine
        self.on_event = on_event
        self.on_local_change = on_local_change
        self.assets_dir = assets_dir
        self.host = host if host is not None else ft.Column(expand=True, spacing=0)
        self.notes: list[tuple[str, str]] = []
        self._snap: dict = {}
        self._hover: set[str] = set()
        self._tab_index: dict[str, int] = {}
        self._told: set[str] = set()
        # 部件表：地址 → [(行序号或 None, 部件)]。用户动作投递要找到**真实部件**，
        # 由它自己的事件绑定翻译成引擎事件——而不是绕过界面直接 shot 进引擎。
        self._controls: dict[str, list] = {}
        self._event_diags: list = []

    # ------------------------------------------------------------ 入口

    def apply(self, snap: dict) -> list[tuple[str, str]]:
        """用一次**只读**渲染状态重建整棵控件树。返回本次的降级/异常报告。"""
        self.notes = []
        self._snap = snap
        self._controls = {}
        program = self.engine.program

        roots = []
        for wid in program.nodes.get("root").children if "root" in program.nodes else []:
            node = program.nodes.get(wid)
            if node is None or node.type != "window":
                continue
            self._apply_window(node, snap)
            for child in node.children:
                control = self._node(child, snap)
                if control is not None:
                    roots.append(control)
        self.host.controls = roots
        self._apply_dialogs(snap)
        return list(self.notes)

    # ------------------------------------------------------------ 窗口 / 覆盖层

    def _apply_window(self, node, snap) -> None:
        title = self._val(node.id, "title", snap, None)
        if title is not _MISSING:
            self.page.title = str(title)
        bgcolor = self._val(node.id, "bgcolor", snap, None)
        if bgcolor is not _MISSING and hasattr(self.page, "bgcolor"):
            try:
                self.page.bgcolor = bgcolor
            except Exception as ex:  # noqa: BLE001
                self._report("RENDER_VALUE", "#%s 的 bgcolor 无法应用：%s" % (node.id, ex))
        theme_kw = {}
        seed = self._val(node.id, "primary", snap, None)
        if seed is not _MISSING:
            theme_kw["color_scheme_seed"] = str(seed)
        family = self._val(node.id, "font", snap, None)
        if family is not _MISSING:
            theme_kw["font_family"] = str(family)
        if theme_kw:
            self.page.theme = ft.Theme(**theme_kw)
        self._size_window(node, snap)
        self.host.spacing = self._gap(node.id, snap, None, 8)

    def _size_window(self, node, snap) -> None:
        for attr, field in (("w", "width"), ("h", "height"), ("x", "left"), ("y", "top")):
            value = self._val(node.id, attr, snap, None)
            if value is not _MISSING:
                setattr(self.page.window, field, int(value))

    def _apply_dialogs(self, snap: dict) -> None:
        """`dialog` 是覆盖层：层叠（后声明在上）、可见时阻断下层交互。

        flet 的 `AlertDialog(modal=True)` 提供的正是"阻断下层交互"——模态的实质是
        **行为**而不是视觉，所以这里不能退化成"手写一个 visible 覆盖层"（那样点击会
        穿透到下层）。层叠顺序 = overlay 列表顺序，后声明的在后 → 绘制在上。

        **一处诚实降级**：flet 的 `AlertDialog` 由内容撑开尺寸，做不到"铺满父的内容区"。
        两条被断言的契约（阻断 / 层叠）成立，"铺满"降级——所以要说出来。
        `page.overlay` 是只读属性（没有 setter），只能**就地改**。
        """
        overlays = []
        for nid, node in self.engine.program.nodes.items():
            if node.type != "dialog":
                continue
            self._warn_once("dialog:fill", "DEGRADED_FEATURE",
                            "dialog 由内容撑开尺寸：flet 的 AlertDialog 做不到\"铺满父内容区\""
                            "（阻断与层叠已按契约实现）")
            visible = bool(snap["flags"].get("#%s.visible" % nid, True))
            content = ft.Column(
                controls=list(self._children(nid, snap)),
                spacing=self._gap(nid, snap, None, 8),
                tight=True, scroll=ft.ScrollMode.AUTO,
            )
            kwargs = {}
            bgcolor = self._val(nid, "bgcolor", snap, None)
            if bgcolor is not _MISSING:
                kwargs["bgcolor"] = bgcolor
            dialog = ft.AlertDialog(modal=True, open=visible, content=content, **kwargs)
            title = self._val(nid, "title", snap, None)
            if title is not _MISSING:
                dialog.title = ft.Text(str(title))
            overlays.append(dialog)
        self.page.overlay[:] = overlays

    # ------------------------------------------------------------ 值 / 标志

    def _val(self, node_id: str, attr: str, snap: dict, row):
        if row is not None:
            return (row.get("#" + node_id) or {}).get(attr, _MISSING)
        return snap.get("attrs", {}).get("#%s.%s" % (node_id, attr), _MISSING)

    def _flag(self, node_id: str, name: str, snap: dict) -> bool:
        return bool(snap.get("flags", {}).get("#%s.%s" % (node_id, name), False))

    def _gap(self, node_id: str, snap: dict, row, default: int) -> int:
        value = self._val(node_id, "gap", snap, row)
        if value is _MISSING:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            self._report("RENDER_VALUE", "#%s 的 gap 不是数字（%r），用默认 %d"
                         % (node_id, value, default))
            return default

    def _ref(self, node_id: str, attr: str) -> Optional[str]:
        expr = self.engine.program.nodes[node_id].attrs.get(attr)
        return getattr(expr, "addr", None)

    def _report(self, code: str, message: str) -> None:
        self.notes.append((code, message))

    def _warn_once(self, key: str, code: str, message: str) -> None:
        if key in self._told:
            return
        self._told.add(key)
        self._report(code, message)

    # ------------------------------------------------------------ 节点

    def _children(self, node_id: str, snap: dict, row=None, row_index=None) -> list:
        node = self.engine.program.nodes.get(node_id)
        if node is None:
            return []
        out = []
        for child in node.children:
            control = self._node(child, snap, row, row_index)
            if control is not None:
                out.append(control)
        return out

    def _node(self, node_id: str, snap: dict, row=None, row_index=None):
        node = self.engine.program.nodes.get(node_id)
        if node is None:
            return None
        if node.type in ("dialog", "template"):
            return None                     # 覆盖层单独处理；模板由 `list` 展开
        builder = getattr(self, "_build_" + node.type, None)
        if builder is None:
            self._warn_once("type:" + node.type, "DEGRADED_FEATURE",
                            "渲染器不认识控件 %s（#%s），已跳过" % (node.type, node_id))
            return None
        control = builder(node, snap, row, row_index)
        if control is not None:
            self._controls.setdefault(node_id, []).append((row_index, control))
        return control

    # ------------------------------------------------------------ 装饰（盒模型 / 状态 / 交互动效）

    def _decorate(self, node, inner, snap: dict, row, row_index, eaten: Iterable[str] = ()):
        """把通用属性落到控件上；容器类控件没有的盒模型字段交给 `ft.Container` 承载。"""
        node_id = node.id
        eaten = set(eaten)
        inner_kw: dict = {}
        box_kw: dict = {}
        flex = _MISSING

        for attr in sorted(SUPPORTED_ATTRS - eaten):
            if attr in vocab.REF_ATTRS or attr in vocab.STATE_FLAGS:
                continue
            value = self._val(node_id, attr, snap, row)
            if value is _MISSING:
                continue
            if attr in ("align", "justify"):
                if node.type not in ("col", "row"):
                    self._warn_once("axis:%s:%s" % (node_id, attr), "RENDER_NO_LANDING",
                                    "#%s 的属性 %s 只对 col / row 有意义，已忽略"
                                    % (node_id, attr))
                continue
            field = _field_for(node.type, attr)
            if field is None:
                if attr not in _META_HANDLED:
                    self._warn_once("land:%s:%s" % (node_id, attr), "RENDER_NO_LANDING",
                                    "#%s 的属性 %s 在 flet 的 %s 上没有落点，已忽略"
                                    % (node_id, attr, type(inner).__name__))
                continue
            try:
                converted = _CONVERTERS[attr](value) if attr in _CONVERTERS else value
            except Exception as ex:  # noqa: BLE001 - 取值转换失败必须可见
                self._warn_once("conv:%s:%s" % (node_id, attr), "RENDER_VALUE",
                                "#%s 的属性 %s 取值 %r 无法转换，已忽略：%s"
                                % (node_id, attr, value, ex))
                continue
            if attr == "flex":
                flex = converted
            elif _supports(type(inner), field):
                inner_kw[field] = converted
            elif attr in _BOX:
                box_kw[_BOX[attr]] = converted
            else:
                self._warn_once("land:%s:%s" % (node_id, attr), "RENDER_NO_LANDING",
                                "#%s 的属性 %s 在 flet 的 %s 上没有落点，已忽略"
                                % (node_id, attr, type(inner).__name__))

        states = self._val(node_id, "states", snap, row)
        if isinstance(states, dict):
            if node.type == "button":
                inner_kw["style"] = self._button_style(inner_kw.get("style"), states)
            else:
                self._merge_states(node_id, inner, inner_kw, states, snap)

        for field, value in inner_kw.items():
            setattr(inner, field, value)

        outer = inner
        if box_kw:
            outer = ft.Container(content=inner, **box_kw)
        if flex is not _MISSING and _supports(type(outer), "expand"):
            outer.expand = int(flex)
        if _supports(type(outer), "visible"):
            outer.visible = self._flag(node_id, "visible", snap)
        if _supports(type(outer), "disabled"):
            outer.disabled = self._flag(node_id, "disabled", snap)
        self._apply_animation(node, outer, snap, row)
        self._wire(node, inner, row_index)
        return outer

    def _button_style(self, base, states: dict):
        """`states` 在按钮上交给 `ButtonStyle`（flet 原生按状态取样式）。"""
        mapping = {"hover": ft.ControlState.HOVERED, "focus": ft.ControlState.FOCUSED,
                   "pressed": ft.ControlState.PRESSED, "error": ft.ControlState.ERROR}
        bgcolor, color = {}, {}
        for state, attrs in states.items():
            key = mapping.get(state)
            if key is None or not isinstance(attrs, dict):
                continue
            for attr, value in attrs.items():
                if attr == "bgcolor":
                    bgcolor[key] = value
                elif attr == "fg":
                    color[key] = value
                else:
                    self._warn_once("state:%s:%s:%s" % (id(states), state, attr),
                                    "RENDER_NO_LANDING",
                                    "按钮状态 %s 的外观属性 %s 暂无落点，已忽略" % (state, attr))
        kwargs = {}
        if bgcolor:
            kwargs["bgcolor"] = bgcolor
        if color:
            kwargs["color"] = color
        return ft.ButtonStyle(**kwargs) if kwargs else base

    def _merge_states(self, node_id: str, inner, kw: dict, states: dict, snap: dict) -> None:
        """非按钮控件：flet 没有逐状态样式表，故**当前处于哪个状态就套哪套外观**。"""
        active = [state for state in _STATE_ATTRS if self._flag(node_id, state, snap)]
        if node_id in self._hover:
            active.append("hover")
        for state in active:
            attrs = states.get(state)
            if not isinstance(attrs, dict):
                continue
            for attr, value in attrs.items():
                field = _SIMPLE.get(attr)
                if field is None or not _supports(type(inner), field):
                    self._warn_once("state:%s:%s:%s" % (node_id, state, attr),
                                    "RENDER_NO_LANDING",
                                    "#%s 状态 %s 的外观属性 %s 在 flet 的 %s 上没有落点"
                                    % (node_id, state, attr, type(inner).__name__))
                    continue
                try:
                    kw[field] = _CONVERTERS[attr](value) if attr in _CONVERTERS else value
                except Exception as ex:  # noqa: BLE001
                    self._warn_once("stateconv:%s:%s:%s" % (node_id, state, attr),
                                    "RENDER_VALUE",
                                    "#%s 状态 %s 的 %s 取值无法转换：%s"
                                    % (node_id, state, attr, ex))

    def _apply_animation(self, node, control, snap: dict, row) -> None:
        names = self._val(node.id, "animate", snap, row)
        if names is _MISSING:
            return
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, (list, tuple)):
            return
        duration = self._val(node.id, "duration", snap, row)
        curve = self._val(node.id, "curve", snap, row)
        curve_name = "EASE_OUT" if curve is _MISSING else str(curve).upper()
        curve_value = getattr(ft.AnimationCurve, curve_name, None)
        if curve_value is None:
            self._warn_once("curve:%s" % curve_name, "RENDER_VALUE",
                            "未知缓动 %r，改用默认 ease_out" % curve)
            curve_value = ft.AnimationCurve.EASE_OUT
        animation = ft.Animation(200 if duration is _MISSING else int(duration), curve_value)
        if _supports(type(control), "animate"):
            control.animate = animation
        for name in names:
            field = _ANIMATE_FIELD.get(str(name))
            if field and _supports(type(control), field):
                setattr(control, field, animation)

    # ------------------------------------------------------------ 交互

    def _wire(self, node, control, row_index) -> None:
        node_id = node.id
        if node.type == "button":
            control.on_click = self._handler(node_id, "click", row_index)
        elif node.type in ("checkbox", "switch", "slider", "dropdown"):
            control.on_change = self._handler(node_id, "change", row_index, takes_value=True)
        elif node.type == "input":
            # 打字本身不是"需要被响应的动作"（规范 4 节），故不接 `change`；
            # 提交才是动作，失焦/聚焦是状态。
            control.on_submit = self._handler(node_id, "submit", row_index, takes_value=True)
            control.on_focus = self._handler(node_id, "focus", row_index)
            control.on_blur = self._handler(node_id, "blur", row_index)
        if _supports(type(control), "on_hover"):
            control.on_hover = self._hover_handler(node_id)

    def _handler(self, node_id: str, event: str, row_index, takes_value: bool = False):
        def handler(event_obj=None):
            value = self._event_value(event_obj) if takes_value else None
            result = self.on_event(node_id, event, row_index, value)
            if result:
                # 引擎产生的诊断随事件回传；`deliver` 会把它们一并交给驱动者。
                self._event_diags.extend(_as_dicts(result))
        return handler

    # ------------------------------------------------------------ 用户动作投递

    def deliver(self, target: str, action: str, value=None) -> dict:
        """把**用户动作**投递给真实部件，由部件自己的事件绑定翻成引擎事件。

        与"直接往引擎里灌事件"不是一回事：只有部件真的绑定了该事件，动作才送得到。
        送不到就**明说**（`delivered: false` + `DEGRADED_FEATURE(feature=interaction)`），
        绝不假装送达。
        """
        node_id = (target or "").lstrip("#")
        node = self.engine.program.nodes.get(node_id)
        if node is None:
            return {"delivered": False, "diagnostics": [
                {"code": "TARGET_MISSING", "level": "error",
                 "message": "节点 #%s 不存在" % node_id}]}

        region = self._interactive_root()
        if region is not None and not self._in_subtree(node_id, region):
            # 模态的实质就是"阻断下层交互"：动作被覆盖层吃掉——这正是规范要求的行为。
            return {"delivered": True, "blocked_by": "#" + region,
                    "note": "动作被模态覆盖层阻断：下层收不到它"}
        if not self._visible(node_id):
            return {"delivered": False, "diagnostics": [
                {"code": "TARGET_MISSING", "level": "error",
                 "message": "节点 #%s 不可见，用户点不到它" % node_id}]}

        entries = self._controls.get(node_id) or []
        if not entries:
            return self._undeliverable(node_id, "该节点没有被渲染成可交互部件")
        if len(entries) > 1:
            # 模板里的部件有多份实例，而 `interact` 请求里没有行上下文——不能瞎挑一个。
            return self._undeliverable(
                node_id, "该节点在模板内且有多行实例，程序化投递缺少行上下文")
        _row_index, control = entries[0]
        handler = getattr(control, _EVENT_HANDLER.get(action, "on_click"), None)
        if not callable(handler):
            return self._undeliverable(node_id, "部件没有绑定 %s" % action)
        if value is not None and hasattr(control, "value"):
            # 真实用户动作会先改部件的值，再触发事件——submit（键盘完成键）与
            # change 同理：动作带着值来，部件值先同步，`takes_value` 的处理器
            # 才能从部件读到它。只对 change 做会让 input 的 submit 投递空值。
            control.value = value
        self._event_diags = []
        try:
            handler(_SyntheticEvent(control, value))
        except Exception as ex:  # noqa: BLE001 - 投递失败必须可见
            return {"delivered": False, "diagnostics": [
                {"code": "RENDER_VALUE", "level": "error",
                 "message": "投递 %s 时部件处理失败：%s: %s"
                            % (action, type(ex).__name__, ex)}]}
        diags, self._event_diags = self._event_diags, []
        return {"delivered": True, "diagnostics": diags}

    def _undeliverable(self, node_id: str, reason: str) -> dict:
        return {"delivered": False, "diagnostics": [
            {"code": "DEGRADED_FEATURE", "level": "info",
             "message": "#%s 的用户动作无法投递：%s" % (node_id, reason),
             "feature": "interaction"}]}

    def _interactive_root(self):
        """当前可交互区域的根：最上层**可见**的 `dialog`；没有则整棵树都可交互。"""
        region = None
        for nid, node in self.engine.program.nodes.items():
            if node.type == "dialog" and self._flag(nid, "visible", self._snap):
                region = nid                        # 后声明的在上
        return region

    def _in_subtree(self, node_id: str, root: str) -> bool:
        cursor = node_id
        while cursor:
            if cursor == root:
                return True
            node = self.engine.program.nodes.get(cursor)
            cursor = node.parent if node is not None else None
        return False

    def _visible(self, node_id: str) -> bool:
        cursor = node_id
        while cursor:
            if not self._flag(cursor, "visible", self._snap):
                return False
            node = self.engine.program.nodes.get(cursor)
            cursor = node.parent if node is not None else None
        return True

    async def capture(self) -> Optional[bytes]:
        """视觉快照（可选观察面，仅供驱动者自检）。

        两个坑都是实测出来的，不是推演：① 刚 `update()` 完立刻截图会返回**空字节**；
        ② 页面没开 `page.enable_screenshots` 时**永远**返回空——那就不是"无头下截不了"，
        而是"忘了开开关"（这正是它被写进这里的理由）。
        """
        for attempt in range(6):
            image = await self.page.take_screenshot()
            if image:
                return image
            await asyncio.sleep(0.15 * (attempt + 1))
        return None

    @staticmethod
    def _event_value(event_obj):
        control = getattr(event_obj, "control", None)
        if control is not None and hasattr(control, "value"):
            return control.value
        return getattr(event_obj, "data", None)

    def _hover_handler(self, node_id: str):
        def handler(event_obj=None):
            data = str(getattr(event_obj, "data", "")).lower()
            entered = data in ("true", "1", "yes")
            was = node_id in self._hover
            if entered:
                self._hover.add(node_id)
            else:
                self._hover.discard(node_id)
            # 只在真的变化时重绘，否则鼠标一动就重画整棵树。
            if was != entered and self.on_local_change is not None:
                self.on_local_change()
        return handler

    # ------------------------------------------------------------ 各控件

    def _build_col(self, node, snap, row, row_index):
        inner = ft.Column(controls=self._children(node.id, snap, row, row_index),
                          spacing=self._gap(node.id, snap, row, 8),
                          scroll=self._scroll(node, snap, row))
        # 交叉轴缺省拉伸：否则容器宽度由内容决定，整页会缩成一条。
        inner.horizontal_alignment = ft.CrossAxisAlignment.STRETCH
        self._axis(node, inner, snap, row, "col")
        return self._decorate(node, inner, snap, row, row_index,
                              eaten=("align", "justify", "gap"))

    def _build_row(self, node, snap, row, row_index):
        inner = ft.Row(controls=self._children(node.id, snap, row, row_index),
                       spacing=self._gap(node.id, snap, row, 8),
                       scroll=self._scroll(node, snap, row))
        self._axis(node, inner, snap, row, "row")
        return self._decorate(node, inner, snap, row, row_index,
                              eaten=("align", "justify", "gap"))

    def _axis(self, node, control, snap, row, kind: str) -> None:
        justify = self._val(node.id, "justify", snap, row)
        if justify is not _MISSING:
            try:
                control.alignment = _enum(ft.MainAxisAlignment, _JUSTIFY, justify)
            except ValueError as ex:
                self._warn_once("justify:%s" % node.id, "RENDER_VALUE",
                                "#%s 的 justify 取值无效：%s" % (node.id, ex))
        align = self._val(node.id, "align", snap, row)
        if align is not _MISSING:
            field = "horizontal_alignment" if kind == "col" else "vertical_alignment"
            try:
                setattr(control, field, _enum(ft.CrossAxisAlignment, _ALIGN, align))
            except ValueError as ex:
                self._warn_once("align:%s" % node.id, "RENDER_VALUE",
                                "#%s 的 align 取值无效：%s" % (node.id, ex))

    def _scroll(self, node, snap, row):
        value = self._val(node.id, "scroll", snap, row)
        if value is _MISSING:
            return None
        try:
            return _enum(ft.ScrollMode, _SCROLL, value)
        except ValueError:
            return None

    def _build_text(self, node, snap, row, row_index):
        value = self._val(node.id, "text", snap, row)
        inner = ft.Text(value="" if value is _MISSING else str(value))
        return self._decorate(node, inner, snap, row, row_index, eaten=("text",))

    def _build_icon(self, node, snap, row, row_index):
        name = self._val(node.id, "icon", snap, row)
        member = None if name is _MISSING else icon_value(str(name))
        if member is None:
            self._warn_once("icon:%s" % node.id, "DEGRADED_FEATURE",
                            "#%s 的图标 %r 在本渲染器里没有对应成员，已用占位符"
                            % (node.id, name))
            inner = ft.Text("?")
        else:
            inner = ft.Icon(icon=member)
        return self._decorate(node, inner, snap, row, row_index, eaten=("icon",))

    def _build_divider(self, node, snap, row, row_index):
        inner = ft.Divider()
        height = self._val(node.id, "h", snap, row)
        if height is not _MISSING:
            inner.height = int(height)
        return self._decorate(node, inner, snap, row, row_index, eaten=("h",))

    def _build_spacer(self, node, snap, row, row_index):
        # 语言里 `spacer` 是"弹性留白"，flet 没有对应控件 → 等价配方。
        inner = ft.Container(expand=True)
        return self._decorate(node, inner, snap, row, row_index)

    def _build_progress(self, node, snap, row, row_index):
        value = self._val(node.id, "value", snap, row)
        maximum = self._val(node.id, "max", snap, row)
        ratio = None
        if value is not _MISSING and value is not None:
            try:
                number = float(value)
                top = float(maximum) if maximum is not _MISSING and maximum else 1.0
                ratio = max(0.0, min(1.0, number / top if top else 0.0))
            except (TypeError, ValueError) as ex:
                self._warn_once("progress:%s" % node.id, "RENDER_VALUE",
                                "#%s 的进度值无法计算：%s" % (node.id, ex))
        inner = ft.ProgressBar(value=ratio)
        return self._decorate(node, inner, snap, row, row_index, eaten=("value", "max"))

    def _build_image(self, node, snap, row, row_index):
        src = self._val(node.id, "src", snap, row)
        inner = ft.Image(src="")
        if src is not _MISSING and src:
            path = self._asset_path(str(src))
            if path is None:
                # 资源在渲染期消失 → **兜底可见，不留白**（与 avatar 同一套规则）。
                self._warn_once("asset:%s" % node.id, "ASSET_MISSING",
                                "#%s 的资源 %r 在渲染期不可用，已显示占位提示"
                                % (node.id, src))
                inner.error_content = ft.Text("资源缺失：%s" % src)
            inner.src = path
        return self._decorate(node, inner, snap, row, row_index, eaten=("src",))

    def _build_avatar(self, node, snap, row, row_index):
        inner = ft.CircleAvatar()
        initials = self._val(node.id, "initials", snap, row)
        name = self._val(node.id, "icon", snap, row)
        src = self._val(node.id, "src", snap, row)
        if src is not _MISSING and src:
            path = self._asset_path(str(src))
            if path is None:
                self._warn_once("asset:%s" % node.id, "ASSET_MISSING",
                                "#%s 的头像资源 %r 在渲染期不可用，已回退到文字兜底"
                                % (node.id, src))
                inner.content = ft.Text(str(initials) if initials is not _MISSING else "?")
            else:
                inner.foreground_image_src = path
        elif initials is not _MISSING:
            inner.content = ft.Text(str(initials))
        elif name is not _MISSING and icon_value(str(name)) is not None:
            inner.content = ft.Icon(icon=icon_value(str(name)))
        size = self._val(node.id, "size", snap, row)
        if size is not _MISSING:
            try:
                inner.radius = float(size) / 2.0
            except (TypeError, ValueError):
                pass
        return self._decorate(node, inner, snap, row, row_index,
                              eaten=("src", "initials", "icon", "size"))

    def _build_button(self, node, snap, row, row_index):
        text = self._val(node.id, "text", snap, row)
        name = self._val(node.id, "icon", snap, row)
        content = ft.Text("" if text is _MISSING else str(text))
        kwargs = {"content": content}
        if name is not _MISSING:
            member = icon_value(str(name))
            if member is None:
                self._warn_once("icon:%s" % node.id, "DEGRADED_FEATURE",
                                "#%s 的图标 %r 没有对应成员，按钮只显示文字" % (node.id, name))
            else:
                kwargs["icon"] = member
        inner = ft.Button(**kwargs)
        return self._decorate(node, inner, snap, row, row_index, eaten=("text", "icon"))

    def _build_input(self, node, snap, row, row_index):
        value = self._val(node.id, "value", snap, row)
        inner = ft.TextField(value="" if value is _MISSING else value)
        return self._decorate(node, inner, snap, row, row_index, eaten=("value",))

    def _build_checkbox(self, node, snap, row, row_index):
        value = self._val(node.id, "value", snap, row)
        inner = ft.Checkbox(value=bool(value) if value is not _MISSING else False)
        return self._decorate(node, inner, snap, row, row_index, eaten=("value",))

    def _build_switch(self, node, snap, row, row_index):
        value = self._val(node.id, "value", snap, row)
        inner = ft.Switch(value=bool(value) if value is not _MISSING else False)
        return self._decorate(node, inner, snap, row, row_index, eaten=("value",))

    def _build_slider(self, node, snap, row, row_index):
        value = self._val(node.id, "value", snap, row)
        inner = ft.Slider(value=None if value is _MISSING else float(value))
        return self._decorate(node, inner, snap, row, row_index, eaten=("value",))

    def _build_dropdown(self, node, snap, row, row_index):
        source = self._ref(node.id, "options")
        rows = snap.get("data", {}).get("#" + source, []) if source else []
        label_field = self._val(node.id, "option_label", snap, row)
        value_field = self._val(node.id, "option_value", snap, row)
        options = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            label = item.get(label_field) if label_field is not _MISSING else None
            key = item.get(value_field) if value_field is not _MISSING else None
            options.append(ft.DropdownOption(key=str(key if key is not None else label),
                                             text="" if label is None else str(label)))
        selected = self._val(node.id, "selected", snap, row)
        inner = ft.Dropdown(options=options,
                            value=None if selected is _MISSING else str(selected))
        return self._decorate(node, inner, snap, row, row_index,
                              eaten=("selected", "option_label", "option_value"))

    def _build_list(self, node, snap, row, row_index):
        template_id = self._ref(node.id, "template")
        template = self.engine.program.nodes.get(template_id) if template_id else None
        items = []
        if template is not None:
            for index, item_row in enumerate(snap.get("rows", {}).get("#" + node.id, [])):
                items.append(ft.Column(
                    controls=self._children(template.id, snap, item_row, index),
                    spacing=self._gap(node.id, snap, row, 8), tight=True))
        inner = ft.ListView(controls=items, spacing=self._gap(node.id, snap, row, 8),
                            padding=0, auto_scroll=False)
        return self._decorate(node, inner, snap, row, row_index, eaten=("gap", "source", "template"))

    def _build_navbar(self, node, snap, row, row_index):
        title = self._val(node.id, "title", snap, row)
        head = ft.Text("" if title is _MISSING else str(title),
                       weight=ft.FontWeight.BOLD, size=18)
        children = self._children(node.id, snap, row, row_index)
        inner = ft.Row(controls=[head, ft.Container(expand=True)] + children,
                       spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        return self._decorate(node, inner, snap, row, row_index, eaten=("title",))

    def _build_tabs(self, node, snap, row, row_index):
        pages = [cid for cid in node.children
                 if self.engine.program.nodes.get(cid) is not None
                 and self.engine.program.nodes[cid].type not in ("template", "dialog")]
        labels = []
        for cid in pages:
            label = self._val(cid, "title", snap, None)
            if label is _MISSING:
                label = self._val(cid, "text", snap, None)
            labels.append(cid if label is _MISSING else str(label))
        declared = self._val(node.id, "selected", snap, row)
        if node.id not in self._tab_index:
            try:
                self._tab_index[node.id] = int(declared) if declared is not _MISSING else 0
            except (TypeError, ValueError):
                self._tab_index[node.id] = 0
        index = max(0, min(self._tab_index[node.id], max(len(pages) - 1, 0)))

        bar = ft.TabBar(tabs=[ft.Tab(label=labels[i]) for i in range(len(pages))])
        tabs = ft.Tabs(length=len(pages), selected_index=index, content=bar)

        def on_change(event_obj=None):
            control = getattr(event_obj, "control", None)
            new_index = getattr(control, "selected_index", index)
            self._tab_index[node.id] = int(new_index or 0)
            if self.on_local_change is not None:
                self.on_local_change()

        tabs.on_change = on_change

        body = []
        for position, cid in enumerate(pages):
            page_control = self._node(cid, snap)
            if page_control is None:
                continue
            # 非选中页**不占位**：靠 `visible` 切换，而不是假装它在别处。
            page_control.visible = position == index and page_control.visible
            body.append(page_control)
        inner = ft.Column(controls=[tabs, ft.Column(controls=body, spacing=8, expand=True)],
                          spacing=8, expand=True)
        return self._decorate(node, inner, snap, row, row_index, eaten=("selected",))

    def _build_window(self, node, snap, row, row_index):   # pragma: no cover
        return None

    def _build_dialog(self, node, snap, row, row_index):   # pragma: no cover
        return None

    def _build_template(self, node, snap, row, row_index):  # pragma: no cover
        return None

    # ------------------------------------------------------------ 工具

    def _asset_path(self, src: str) -> Optional[str]:
        if not self.assets_dir:
            return src
        path = os.path.join(self.assets_dir, src)
        return path if os.path.isfile(path) else None


def make_renderer(page: ft.Page, engine, on_event: Callable, **kwargs) -> FletRenderer:
    return FletRenderer(page, engine, on_event, **kwargs)


# ------------------------------------------------------------------ 一致性检查
#
# 声明不能靠自觉：下面三处在**导入期**就把"声明了却做不到"炸出来。
# 语言新增词汇时这里会先失败，而不是让用户看到一个悄悄降级的界面。

_BUILDERS = {name[7:] for name in dir(FletRenderer) if name.startswith("_build_")}
_IMPLEMENTED = {t for t in _BUILDERS if t in RENDERABLE_CONTROLS}

assert _IMPLEMENTED == set(RENDERABLE_CONTROLS), (
    "渲染器未覆盖全部可渲染控件：缺 %s" % sorted(set(RENDERABLE_CONTROLS) - _IMPLEMENTED))

assert SUPPORTED_ATTRS == set(vocab.ALL_ATTRS), (
    "渲染器的属性映射与词汇表不一致：缺 %s，多 %s"
    % (sorted(set(vocab.ALL_ATTRS) - SUPPORTED_ATTRS),
       sorted(SUPPORTED_ATTRS - set(vocab.ALL_ATTRS))))

assert not UNRESOLVED_ICONS, "核心图标无法解析：%s" % UNRESOLVED_ICONS
