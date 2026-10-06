"""内置 llm_provider：OpenAI 兼容的流式对话接口。

**以流为主**：接口是 `stream(messages) -> Iterator[str]`。非流式用法就是把结果 join 起来——
这样实时性不会在插件层丢掉（OpenAI 兼容 API 原生流式，若接口要求"返回完整字符串"，
插件就得内部攒完再给，用户界面上的逐字输出就没了）。

凭据解析：**进程环境变量 → 本机钥匙串**（`key_env` 存的是变量名；两层都在
`secrets.resolve` 里，顺序写死）。toml 只存变量名——它会进 git、进 dist、
被融合拷贝；钥匙串在 `~/.puppethub/`（app 目录之外）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Iterator, List

from ..plugins import PluginError

NAME = "openai-compat"
PROVIDES = ["llm_provider"]


class OpenAICompatProvider:
    def __init__(self, api):
        self.api = api
        self.base_url = str(api.option("base_url", "https://api.openai.com/v1")).rstrip("/")
        self.model = str(api.option("model", "") or "")
        self.key_env = str(api.option("key_env", "OPENAI_API_KEY"))
        self.timeout = float(api.option("timeout", 180))
        self.temperature = api.option("temperature")
        self._limit = api.option("context_limit")
        # **视觉能力由配置声明，保守缺省 false**（`design-aesthetics.md` §4.2）：
        # 不知道模型支不支持看图就不发图——发了会 4xx，或被服务端静默丢掉。
        # 真值只认 true/1/yes/on（字符串也算），其余一律 false。
        raw_vision = api.option("vision", False)
        self.supports_vision = str(raw_vision).strip().lower() in ("true", "1", "yes", "on")

    # -------------------------------------------------- 契约

    def context_limit(self):
        """自报上下文窗口上限（token 或字符？用**字符**口径：宿主按字符算，最保守）。

        报不出来就返回 `None`——宿主会照常统计与报告体积，只是无法判断"超限"。
        """
        try:
            return int(self._limit) if self._limit else None
        except (TypeError, ValueError):
            return None

    def describe(self) -> str:
        return "openai-compat(model=%s, base_url=%s, key_env=%s)" % (
            self.model or "（未配置）", self.base_url, self.key_env)

    def stream(self, messages: List[dict]) -> Iterator[str]:
        if not self.model:
            raise PluginError(
                "没有配置模型：二选一——\n"
                "  ① 推荐：puppethub.toml 写 [llm] profile = \"名字\"，端点/模型/凭据名\n"
                "     写在机器级 ~/.puppethub/providers.toml（app 里不留端点，可安全分享）；\n"
                "  ② 或直接写 [plugins.openai-compat] model = \"…\"")
        key = self.api.secret(self.key_env)
        if not key:
            from .. import secrets
            raise PluginError(
                "凭据 %s 没找到：进程环境变量与本机钥匙串（%s）两层都试过了。\n"
                "  录入：puppethub keys set %s\n"
                "  或改 puppethub.toml 的 [llm] profile / key_env；"
                "设了环境变量要重启（env 优先级更高）"
                % (self.key_env, secrets.secrets_path(), self.key_env))

        payload = {"model": self.model, "messages": messages, "stream": True}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + key,
                     "Accept": "text/event-stream"},
            method="POST")
        seen_done = False
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        seen_done = True
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    piece = (choices[0].get("delta") or {}).get("content")
                    if piece:
                        yield piece
        except urllib.error.HTTPError as ex:
            body = ""
            try:
                body = ex.read().decode("utf-8", "replace")[:400]
            except Exception:  # noqa: BLE001
                pass
            raise PluginError("服务端返回 %s %s：%s" % (ex.code, ex.reason, body or "（无响应体）"))
        except urllib.error.URLError as ex:
            raise PluginError("连不上 %s：%s" % (self.base_url, ex.reason))
        except Exception as ex:  # noqa: BLE001 - 中途失败必须可见（已流出的片段由宿主保留）
            raise PluginError("流读取中断：%s: %s" % (type(ex).__name__, ex))
        if not seen_done:
            # 不能静默：截断的输出看起来和完整输出一模一样，但少了半句就可能少改一段程序。
            raise PluginError("流在结束标记之前断掉，本轮输出可能不完整（已流出的片段会保留）")


def create_llm_provider(api) -> OpenAICompatProvider:
    return OpenAICompatProvider(api)
