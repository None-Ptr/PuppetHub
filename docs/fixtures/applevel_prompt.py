"""app 级插件目录的夹具：无操作的 prompt 插件（只出现在 app 自带插件目录里）。"""

NAME = "applevel"
PROVIDES = ["prompt"]


class AppLevelPrompt:
    def __init__(self, api=None):
        self.api = api

    def rewrite(self, context: dict) -> dict:
        # 纯透传：链里看得见它就行，不改内容。
        return {"system": context.get("system", ""),
                "messages": context.get("messages") or []}


def create_prompt(api) -> AppLevelPrompt:
    return AppLevelPrompt(api)
