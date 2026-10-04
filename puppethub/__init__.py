"""PuppetHub：搭 app 的软件（搭建 / 运行 / 运行时定制 / 未来的融合）。

它不是 app 运行时框架，也不是 UI 库：人用自然语言描述想要什么，内置 LLM 把它
翻译成**命令批**，引擎校验后写回真源并渲染。造出的 app **不含聊天框**——聊天只在
PuppetHub 自己的窗口里。

三层分工：`puppet`（语言与语义引擎，零第三方依赖）· **`puppethub`（本包）** ·
`puppetOS`（发行版，仅设计文档）。

本模块**刻意保持轻量**：不导入 flet。`puppethub new` 不该为了建目录而加载渲染栈，
`import puppethub` 也用不上面向窗口的那部分。
"""

from .compat import (SPEC_RANGE, DataFilesMissing, SpecMismatch,
                     check_data_files, check_spec_version)

__version__ = "0.1.0"
__all__ = ["SPEC_RANGE", "SpecMismatch", "DataFilesMissing",
           "check_spec_version", "check_data_files", "__version__"]
