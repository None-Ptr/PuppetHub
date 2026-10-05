"""能力目录：把能力契约压成"紧凑签名"。

给 LLM 的上下文块与 `hello.catalog` 返回的是**同一份**——一处派生、内外兼得。
签名从 `capabilities.py` 的装饰器与类型注解自动提取（`puppet.contract_of`），
所以"提供的能力"不会与代码漂移。
"""

from __future__ import annotations

from typing import Iterable


def signature(contract: dict) -> str:
    parts = []
    for param in contract.get("params", []):
        text = "%s: %s" % (param.get("name"), param.get("type", "any"))
        if not param.get("required", True):
            text += " = %r" % (param.get("default"),)
        parts.append(text)
    return "%s(%s) -> %s" % (contract.get("name"), ", ".join(parts),
                             contract.get("returns", "any"))


def compact(contracts: Iterable[dict]) -> list[dict]:
    out = []
    for contract in contracts:
        out.append({
            "name": contract.get("name"),
            "signature": signature(contract),
            "doc": (contract.get("doc") or "").strip(),
            "requires": list(contract.get("requires") or []),
        })
    return sorted(out, key=lambda item: item["name"] or "")


def prompt_block(entries: list[dict]) -> str:
    """上下文里的紧凑清单：只有签名 + 说明首行。

    "说明全文按需取"由 `context.capability_docs` 兑现：程序正在调用或请求
    点名的能力，其 docstring 全文经 `capability_docs` 键注入 prompt（触碰前
    LLM 只见首行，调错靠 `CALL_CONTRACT` 诊断回灌试错）。
    """
    if not entries:
        return "（本 app 尚未提供任何能力）"
    lines = []
    for entry in entries:
        head = (entry.get("doc") or "").splitlines() or [""]
        lines.append("- %s  # %s" % (entry.get("signature"), head[0].strip()))
    return "\n".join(lines)
