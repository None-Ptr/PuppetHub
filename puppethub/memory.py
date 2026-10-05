"""运行期记忆：`.puppet/memory/` 独立成区，**重置保留**，清空要显式。

先厘清一件事，否则会把它做成"又一个真源"：

- **记忆是状态，不是程序**。`spec/02-ir.md` 把状态归入"可丢、可从声明重建"那一类；
  §7 也把它列在四类记忆里、与 `app.puppet` 并列而不是包含。所以写记忆**不违反**
  "改变程序必须走命令批"——那条铁律管的是程序。
- 但记忆比普通状态**更该被看见**：它是跨会话留下来的东西，一条错的记忆会一直影响后面每一轮。
  于是四条硬规矩：**每次写入留诊断**（谁 / 何时 / 改了什么）· **可读可改**（人直接编辑文件即可，
  宿主按 id 增量合并，不整体覆盖）· **上限 + 语义化裁剪**（最不重要、最久未访问的先掉，
  不按时间一刀切）· **丢弃可见**且与"人主动清空"在观察流里**可区分**。

V1 的写者 = **共作者 LLM**（指令块 `remember` / `forget`，标 `origin: llm`）+ 人（驾驶舱清空）。
运行期自主 LLM（V1 开关只留"关"）将来接同一个 `remember()`——那就是插入点。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Callable, List, Optional

REL = ".puppet/memory/memory.jsonl"

DEFAULT_MAX_ENTRIES = 200
DEFAULT_MAX_BYTES = 64 * 1024
DEFAULT_CONTEXT_CHARS = 1200
IMPORTANCE_RANGE = (1, 3)


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Memory:
    """`.puppet/memory/memory.jsonl` 的读写与裁剪。**所有写都经 storage 槽位**。"""

    def __init__(self, storage, log: Callable[[str, str, str], None],
                 options: Optional[dict] = None, rel: Optional[str] = None):
        self.storage = storage
        self.log = log
        # `rel` 可换：app 侧是 `.puppet/memory/memory.jsonl`；社会层笔记（调度官）
        # 复用这一整套策略，只是落在 `$PUPPETHUB_HOME/orchestrator/` 下。
        self.rel = rel or REL
        options = options or {}
        self.max_entries = int(options.get("max_entries", DEFAULT_MAX_ENTRIES))
        self.max_bytes = int(options.get("max_bytes", DEFAULT_MAX_BYTES))
        self.context_chars = int(options.get("context_chars", DEFAULT_CONTEXT_CHARS))
        self._bad_lines = False
        self._injected: List[str] = []          # 本轮注入过的 id（回合末统一记一次"被访问"）
        self.uses = 0                            # 上次渲染用了多少条（供驾驶舱显示）

    # ------------------------------------------------------------ 读

    def entries(self) -> List[dict]:
        raw = self.storage.read_text(self.rel, "")
        out: List[dict] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                if not self._bad_lines:
                    self._bad_lines = True
                    # 人手工编辑过、写坏了：**不覆盖它**，但要说出来（否则人改的会被悄悄丢掉）
                    self.log("warning", "MEMORY_BAD_LINE",
                             "%s 里有无法解析的行：宿主会跳过它、**不会覆盖**，"
                             "但每轮都会少读一条" % self.rel)
                continue
            if isinstance(item, dict) and item.get("text"):
                out.append(item)
        return out

    def count(self) -> int:
        return len(self.entries())

    # ------------------------------------------------------------ 写

    def remember(self, text: str, *, importance: int = 1, tags=None,
                 origin: str = "llm") -> Optional[dict]:
        text = (text or "").strip()
        if not text:
            return None
        entries = self.entries()             # 先读：人可能刚改过文件
        entry = {
            "id": self._next_id(entries),
            "text": text if len(text) <= 500 else text[:500] + "…",
            "importance": max(IMPORTANCE_RANGE[0], min(IMPORTANCE_RANGE[1], int(importance))),
            "tags": [str(tag) for tag in (tags or [])][:6],
            "created": _now(),
            "accessed": _now(),
            "uses": 0,
            "origin": origin,
        }
        entries.append(entry)
        dropped = self._prune(entries)
        self._write(entries)
        self.log("info", "MEMORY_WRITE",
                 "记下一条（origin=%s, importance=%d）：%s"
                 % (origin, entry["importance"], _short(entry["text"])))
        for item in dropped:
            self.log("info", "MEMORY_TRIMMED",
                     "超出上限，丢弃了最不重要/最久未访问的一条（不是人主动删的）：%s"
                     % _short(item.get("text", "")))
        return entry

    def forget(self, key: str, origin: str = "llm") -> int:
        """按 id 或**完全匹配的文本**删除。不做模糊匹配——猜错了就是静默删数据。"""
        key = (key or "").strip()
        if not key:
            return 0
        entries = self.entries()
        keep, removed = [], []
        for item in entries:
            if item.get("id") == key or item.get("text") == key:
                removed.append(item)
            else:
                keep.append(item)
        if not removed:
            self.log("warning", "MEMORY_FORGET_MISS",
                     "要忘掉的那条不存在（按 id 或完整文本匹配）：%s" % _short(key))
            return 0
        self._write(keep)
        for item in removed:
            self.log("info", "MEMORY_FORGET",
                     "忘掉一条（origin=%s, id=%s）：%s"
                     % (origin, item.get("id"), _short(item.get("text", ""))))
        return len(removed)

    def touch(self, ids: List[str]) -> None:
        """标记"被访问过"——裁剪按"最久未访问"排序，不记访问就没有依据。"""
        ids = [i for i in (ids or []) if i]
        if not ids:
            return
        entries = self.entries()
        hit = False
        for item in entries:
            if item.get("id") in ids:
                item["accessed"] = _now()
                item["uses"] = int(item.get("uses", 0)) + 1
                hit = True
        if hit:
            self._write(entries)

    def wipe(self, origin: str = "user") -> int:
        """清空。**与自动裁剪在观察流里必须能区分**——一个是人的指令，一个是系统自保。"""
        n = self.count()
        self._write([])
        self.log("info", "MEMORY_WIPED",
                 "已清空运行期记忆（origin=%s，共 %d 条）。"
                 "注意：这与「超出上限自动裁剪」是两回事——一个是人的指令，一个是系统自保"
                 % (origin, n))
        return n

    # ------------------------------------------------------------ 注入上下文

    def context_block(self) -> str:
        """给 LLM 的紧凑块：**排序 = 重要性优先，其次最近访问**（与裁剪口径一致）。"""
        # 排序口径与裁剪口径一致：重要性优先，其次最近访问。
        entries = sorted(self.entries(),
                         key=lambda e: (-int(e.get("importance", 1)),
                                        str(e.get("accessed", ""))))
        lines, used, taken = [], 0, []
        for item in entries:
            line = "- %s%s: %s" % (item.get("id"), _importance_tag(item), item.get("text"))
            if used + len(line) > self.context_chars:
                lines.append("- （其余 %d 条未注入：超出本轮记忆预算）"
                             % (len(entries) - len(taken)))
                break
            lines.append(line)
            used += len(line)
            if item.get("id"):
                taken.append(item["id"])
        self._injected = taken
        self.uses = len(taken)
        return "\n".join(lines) if lines else "（空）"

    def flush_access(self) -> None:
        """回合末统一记一次访问：每轮注入都写盘会让文件抖。"""
        ids, self._injected = self._injected, []
        self.touch(ids)

    def export_text(self) -> str:
        entries = self.entries()
        if not entries:
            return "（运行期记忆为空）"
        lines = ["# 运行期记忆（%d 条，来自 %s）" % (len(entries), self.rel)]
        for item in sorted(entries, key=lambda e: -int(e.get("importance", 1))):
            tags = item.get("tags") or []
            lines.append("%s  [重要度 %s]  %s%s  (访问 %s 次, 最后 %s)  [%s]"
                         % (item.get("id"), item.get("importance", 1), item.get("text"),
                            ("  标签:" + ",".join(tags)) if tags else "",
                            item.get("uses", 0), item.get("accessed", "?"),
                            item.get("created", "?")))
        return "\n".join(lines)

    # ------------------------------------------------------------ 内部

    def _write(self, entries: List[dict]) -> None:
        text = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in entries)
        # 经 storage 槽位：路径白名单、原子写、写失败标脏，一条都不少。
        self.storage.write_text(self.rel, text)

    @staticmethod
    def _next_id(entries: List[dict]) -> str:
        highest = 0
        for item in entries:
            ident = str(item.get("id", ""))
            if ident.startswith("m") and ident[1:].isdigit():
                highest = max(highest, int(ident[1:]))
        return "m%d" % (highest + 1)

    def _prune(self, entries: List[dict]) -> List[dict]:
        """按**重要性升序、其次访问时间升序**丢最旧的，直到同时满足条目数与字节数上限。

        刻意**不按创建时间一刀切**：每条经历价值不同，"最近记的"不等于"最重要的"。
        """
        dropped = []
        while len(entries) > self.max_entries or _bytes(entries) > self.max_bytes:
            victim = min(entries, key=lambda e: (int(e.get("importance", 1)),
                                                 str(e.get("accessed", ""))))
            entries.remove(victim)
            dropped.append(victim)
        return dropped


def _bytes(entries: List[dict]) -> int:
    return sum(len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + 1
               for item in entries)


def _importance_tag(item: dict) -> str:
    level = int(item.get("importance", 1))
    return "" if level <= 1 else "（重要度 %d）" % level


def _short(text: str, limit: int = 60) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"
