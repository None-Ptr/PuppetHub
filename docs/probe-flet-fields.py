"""flet 0.86.5 字段面探测：渲染层依赖的类与字段名。

为什么需要它：flet 0.86 的控件签名被装饰器包装成 `(*args, **kwargs)`，
`inspect.signature` 不可靠，必须读 `__dataclass_fields__`。渲染层按字段名
装配 kwargs，因此这份清单就是渲染层的**编译期契约**——字段改名了，这里先炸。

运行：python docs/probe-flet-fields.py
"""

from __future__ import annotations

import flet as ft

CLASSES = [
    "Padding", "Margin", "Border", "BorderSide", "BoxShadow", "LinearGradient",
    "Alignment", "MainAxisAlignment", "CrossAxisAlignment", "FontWeight", "BoxFit",
    "Animation", "AnimationCurve", "Icons", "ScrollMode", "AppView", "Theme",
    "ControlState", "ButtonStyle", "Text", "Icon", "Container", "Column", "Row",
    "ListView", "NavigationBar", "Tabs", "TabBar", "Tab", "AlertDialog",
    "ProgressBar", "Image", "CircleAvatar", "Divider", "Button", "TextField",
    "Checkbox", "Switch", "Slider", "Dropdown", "DropdownOption",
]

FIELDS = {
    "Text": ["value", "color", "size", "weight", "italic", "font_family", "tooltip"],
    # 注意：flet 0.86 的 `Icon` 字段是 `icon`（不是 `name`）。
    "Icon": ["icon", "color", "size"],
    "Container": ["content", "padding", "margin", "bgcolor", "gradient", "border_radius",
                  "border", "shadow", "opacity", "alignment", "width", "height", "left",
                  "top", "offset", "scale", "rotate", "animate", "animate_opacity",
                  "animate_size", "animate_position", "animate_rotation", "animate_scale",
                  "animate_offset", "visible", "disabled", "expand", "on_hover", "tooltip"],
    "Column": ["controls", "spacing", "alignment", "horizontal_alignment", "scroll",
               "expand", "animate_opacity", "visible", "disabled"],
    "Row": ["controls", "spacing", "alignment", "vertical_alignment", "wrap", "scroll",
            "expand", "visible", "disabled"],
    "ListView": ["controls", "spacing", "expand", "auto_scroll", "padding", "visible"],
    "NavigationBar": ["destinations", "selected_index", "on_change", "bgcolor", "height"],
    "Tabs": ["length", "selected_index", "content", "on_change"],
    # 选中态与 on_change 在 `Tabs` 上，`TabBar` 只有 `tabs` / `on_click`。
    "TabBar": ["tabs", "on_click"],
    "Tab": ["label", "icon"],
    "AlertDialog": ["modal", "open", "title", "content", "actions", "on_dismiss", "bgcolor"],
    "ProgressBar": ["value", "color", "bgcolor", "bar_height", "border_radius"],
    "Image": ["src", "fit", "error_content", "border_radius", "width", "height"],
    "CircleAvatar": ["content", "radius", "bgcolor", "foreground_image_src", "color"],
    "Divider": ["height", "thickness", "color", "leading_indent"],
    "Button": ["content", "icon", "icon_color", "on_click", "style", "tooltip",
               "width", "height", "bgcolor", "color", "disabled", "visible", "expand"],
    "TextField": ["value", "hint_text", "on_change", "on_submit", "on_focus", "on_blur",
                  "read_only", "multiline", "min_lines", "max_lines", "border",
                  "border_color", "border_radius", "bgcolor", "color", "text_size",
                  "label", "expand", "content_padding", "filled"],
    "Checkbox": ["label", "value", "on_change", "active_color", "fill_color",
                 "check_color", "disabled", "visible"],
    "Switch": ["label", "value", "on_change", "active_color", "thumb_color",
               "disabled", "visible"],
    "Slider": ["min", "max", "divisions", "value", "on_change", "active_color",
               "inactive_color", "thumb_color", "label", "disabled", "visible"],
    "Dropdown": ["options", "value", "on_select", "label", "hint_text", "border_color",
                 "bgcolor", "filled", "disabled", "visible", "expand", "width"],
    "DropdownOption": ["key", "text", "content", "leading_icon", "trailing_icon"],
}

ENUMS = [
    ("AnimationCurve", "EASE_OUT"), ("AnimationCurve", "EASE_IN_OUT"),
    ("Icons", "ARROW_UPWARD"), ("Icons", "ARROW_DOWNWARD"), ("Icons", "NOTIFICATIONS"),
    ("Icons", "CALENDAR_MONTH"), ("Icons", "SHOPPING_CART"), ("Icons", "BAR_CHART"),
    ("Icons", "EXPAND_LESS"), ("Icons", "EXPAND_MORE"), ("Icons", "SCHEDULE"),
    ("Icons", "DESCRIPTION"), ("Icons", "VISIBILITY"), ("Icons", "VISIBILITY_OFF"),
    ("Icons", "INSERT_DRIVE_FILE"), ("Icons", "GRID_VIEW"), ("Icons", "PLACE"),
    ("Icons", "REMOVE"), ("Icons", "PLAY_ARROW"), ("Icons", "ADD"),
    ("Icons", "CHECK_CIRCLE"), ("Icons", "LOCK_OPEN"), ("Icons", "PERSON"),
    ("Icons", "GROUP"), ("BoxFit", "COVER"), ("FontWeight", "BOLD"),
    ("MainAxisAlignment", "SPACE_BETWEEN"), ("CrossAxisAlignment", "STRETCH"),
    ("ScrollMode", "AUTO"), ("ControlState", "HOVERED"), ("AppView", "FLET_APP"),
]


def fields_of(name: str):
    cls = getattr(ft, name, None)
    if cls is None:
        return None
    return set(getattr(cls, "__dataclass_fields__", {}) or {})


def main() -> int:
    import importlib.metadata as md
    print("flet", md.version("flet"))
    print("\n== 类存在性 ==")
    for name in CLASSES:
        print("  %-18s %s" % (name, "OK" if hasattr(ft, name) else "缺失"))

    print("\n== 枚举成员 ==")
    for owner, member in ENUMS:
        obj = getattr(ft, owner, None)
        print("  %-18s %-16s %s" % (owner, member, "OK" if hasattr(obj, member) else "缺失"))

    print("\n== 字段名（渲染层依赖的） ==")
    for name, wanted in FIELDS.items():
        present = fields_of(name)
        if present is None:
            print("  %-14s 类不存在" % name)
            continue
        missing = [f for f in wanted if f not in present]
        print("  %-14s %s" % (name, "OK" if not missing else "缺: " + ", ".join(missing)))

    print("\n== padding 辅助 ==")
    for expr in ("ft.padding.all(8)", "ft.Padding(8, 8, 8, 8)", "ft.Padding(top=8, left=4)"):
        try:
            eval(expr)
            print("  %-32s OK" % expr)
        except Exception as ex:  # noqa: BLE001
            print("  %-32s 失败: %s" % (expr, ex))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
