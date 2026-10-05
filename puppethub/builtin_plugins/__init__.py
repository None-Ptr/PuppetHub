"""内置插件：四个槽位的实现（style 槽位有两个预设：plain 缺省 / phosphor）。

它们与用户插件走**同一套契约**（`PROVIDES` + `create_<槽位>(api)`），
只是随包分发、不需要放到 `~/.puppethub/plugins/`。
"""

BUILTIN = ("file", "openai-compat", "default", "plain", "phosphor")
