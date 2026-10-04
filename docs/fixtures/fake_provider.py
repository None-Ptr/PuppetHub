"""脚本化 llm_provider：按队列吐出预设回复。**仅供冒烟，不进产品面。**

把它放进插件目录（或 `PUPPETHUB_PLUGINS` 指向的目录）就会被发现——走的是与真实插件
完全相同的路径：`PROVIDES` 声明槽位、`create_<槽位>(api)` 造实例、配置从 toml 的
`[plugins.fake]` 里读。**假 provider 让"机制"可被确定性地验证**（真 LLM 的输出不确定，
而这里要测的是指令块解析、诊断回灌、卡住检测、确认拦阻这类机制）。
"""

from __future__ import annotations

import json

NAME = "fake"
PROVIDES = ["llm_provider"]


class FakeProvider:
    def __init__(self, api=None):
        self.api = api
        self.replies = []
        self.index = 0
        self.seen = []
        script = api.option("script", "") if api is not None else ""
        if script:
            with open(script, "r", encoding="utf-8") as fh:
                self.replies = json.load(fh)

    def context_limit(self):
        return None

    def describe(self):
        return "fake(script=%s)" % (self.api.option("script", "") if self.api else "-")

    def stream(self, messages):
        self.seen.append(messages)
        if self.index >= len(self.replies):
            raise RuntimeError("假 provider 的脚本用完了（第 %d 次调用）" % (self.index + 1))
        text = self.replies[self.index]
        self.index += 1
        for start in range(0, len(text), 7):      # 分片吐出，模拟流式
            yield text[start:start + 7]


def create_llm_provider(api) -> FakeProvider:
    return FakeProvider(api)
