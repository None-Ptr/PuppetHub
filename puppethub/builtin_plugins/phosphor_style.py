"""内置风格预设 `phosphor`（style 槽位）：**磷光终端**——一套完整配方。

与宿主自己的风格（`theme.py` / `docs/style-terminal.md`）同一血脉，但这是给
**造出来的 app** 的可选预设：写进 app 的 `puppethub.toml`（`style = "phosphor"`）
才生效。材料适合它的场景：代码 / 数据 / 状态 / 日志类工具。
"""

from __future__ import annotations

NAME = "phosphor"
PROVIDES = ["style"]

BLOCK = """## 风格：磷光终端（phosphor）

给 app 一套"天生说代码与状态"的终端观感。适合数据 / 清单 / 工具类界面。

- **底色暖黑**：`add #root window #win bgcolor=#0A0C08 primary=#5CD07A`——
  不是纯黑（纯黑杀磷光）；主文暖白 `fg=#E8E2D4`。
- **等宽字体**：`font="Cascadia Mono"`；需要纵向对齐的列（数字 / 状态 / 时间戳）
  全部等宽，中文靠系统回退，不混第二种字体。
- **颜色只有九个角色，没有之外的颜色**：底 `#0A0C08` · 面 `#12140D`（分块用面色，
  **不用卡片**）· 1px 线 `#2A2C22` · 主文 `#E8E2D4` · 暗文 `#9A968A`（标签/时间戳）·
  琥珀 `#FFB000`（**只给**警告与主行动）· 绿 `#5CD07A`（成功）· 红 `#FF5C4D`（错误）。
- **零圆角零阴影**：`radius=0`、不写 `shadow`；分块靠面色与 1px `border`，
  不靠卡片和投影。
- **层级不靠颜色**：标题只放大 `size` 与 `weight`；标签 / 注解用暗文与更小的 size。
- **一次只炫一处**：全篇唯一允许填充的矩形是**主行动**——琥珀底 + 深字反白。"""


class PhosphorStyle:
    def __init__(self, api=None):
        self.api = api

    def block(self) -> str:
        return BLOCK


def create_style(api) -> PhosphorStyle:
    return PhosphorStyle(api)
