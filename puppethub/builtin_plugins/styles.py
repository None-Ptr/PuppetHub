"""内置风格预设 `plain`（style 槽位）：给共作者的**观感纪律**。

风格是**内容**不是机制：它经 prompt 槽位进上下文，**碰不到任何渲染路径**——
界面永远由程序（真源）决定，风格插件只教 LLM 怎么把词汇表用得体面。
选择：app 的 `puppethub.toml` 写 `style = "名字"`；缺省 `plain`（零配置能跑）。
"""

from __future__ import annotations

NAME = "plain"
PROVIDES = ["style"]

BLOCK = """## 观感（怎么把界面做体面）

词汇表给了能力，这一段给用法。大多数"难看"不是缺能力，是缺纪律：

- **一处定色**：`add #root window #win primary=#4A6CF7`——按钮、开关、滑块、聚焦环
  **全部自动跟随**这一个种子色。逐个控件上色既贵又必然不和谐；换主题只改这一处。
- **颜色纪律**：一个主色 + 深浅中性色；绿/红只给成功/错误，别当装饰。深底配浅字、
  浅底配深字——对比度不够不是"高级灰"，是看不清。
- **留白用档**：`pad`/`gap` 从 4 / 8 / 12 / 16 / 24 里挑，全篇两三档就够——
  间距忽大忽小比没有间距更乱。
- **层级三件套**：标题 = 大 `size` + `weight` 粗 + `fg` 深；正文常规；辅助说明 =
  小 `size` + 浅 `fg`。层级靠这三件套表达，不靠增加颜色数量。
- **图标与反馈**：按钮配 `icon=`（图标见下方清单）；主按钮给
  `states hover={bgcolor: "...", fg: "..."}`——hover 有反馈，界面才是"活"的。
- **克制**：`gradient` 只给头图/英雄区一处；`shadow` 轻用（大模糊、低透明度）；
  全篇"一次只炫一处"，炫的额度花在主行动上。"""


class PlainStyle:
    """契约：`block()` 返回进 prompt 的风格段（含标题）。失败可见，不返回半截。"""

    def __init__(self, api=None):
        self.api = api

    def block(self) -> str:
        return BLOCK


def create_style(api) -> PlainStyle:
    return PlainStyle(api)
