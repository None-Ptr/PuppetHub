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

# 版本号**真源是 `pyproject.toml`**，这里**从它读**，不再硬编码，也不用安装元数据。
#
# 之前这里是 `"0.1.0"`，而 pyproject 已经是 `0.2.0` —— **落后两个发行版**。
# 两处都写死意味着谁也不会注意到，而 `puppethub --version` 会拿它报给用户。
#
# 为什么不用 `importlib.metadata`：editable 安装的元数据在改 pyproject 后是**旧的**
# （实测：pyproject 写 0.3.0，`__version__` 仍报 0.2.0）。发行元数据是 pip 的缓存，
# pyproject 才是真源。
try:
    from pathlib import Path
    import re as _re
    _toml = Path(__file__).resolve().parent.parent / "pyproject.toml"
    _m = _re.search(r'^version\s*=\s*"([^"]+)"', _toml.read_text(encoding="utf-8"), _re.M)
    __version__ = _m.group(1) if _m else "0.0.0.dev0"
except Exception:  # noqa: BLE001 - 装成 wheel 后 pyproject 不在包内，退回元数据
    try:
        from importlib.metadata import version as _v
        __version__ = _v("puppethub")
    except Exception:  # noqa: BLE001
        __version__ = "0.0.0.dev0"
__all__ = ["SPEC_RANGE", "SpecMismatch", "DataFilesMissing",
           "check_spec_version", "check_data_files", "__version__"]
