"""内置插件：三个槽位各一个实现。

它们与用户插件走**同一套契约**（`PROVIDES` + `create_<槽位>(api)`），
只是随包分发、不需要放到 `~/.puppethub/plugins/`。
"""

BUILTIN = ("file", "openai-compat", "default")
