"""MRU：**打开过的 app**（首页项目列表的真源）。

落点 `$PUPPETHUB_HOME/recent.toml`，形状是**一层字符串表**（复用 `secrets.py` 那套
零依赖 TOML 读写——本项目零第三方依赖是硬约束，stdlib 的 `tomllib` 只能读）：

```toml
[projects]
"/path/to/demo" = "2026-10-04T12:00:00"
```

两条纪律：

1. **只在"成功打开"之后入册**。打错的路径、不是 app 的目录都不进历史——
   历史记的是"我确实打开过"，不是"我敲过什么"。
2. **保留全部历史、无上限，且失效条目不自删**。列表自己偷偷少一行就是静默失败；
   要移除得人来（`forget`）。失效只**如实标记**（`(已不在)` / `(不是 app)`）。

为什么不做"扫描项目根"：那需要用户先声明根目录，而"零配置就能用"与"列出所有"
只能靠"见过就记住"同时成立——代价是换机器/新 clone 时首页是空的，这一点在
空态里明说，不假装。
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import List, Optional

from .secrets import _read_table, _write_table, home_dir

RECENT_FILE = "recent.toml"
SECTION = "projects"
SOURCE_NAME = "app.puppet"

HEADER = ("# 首页的项目列表（MRU）——**打开过的 app**，按最后打开时间倒序。\n"
          "# 手改也有效：删掉一行 = 从列表里移除它。")


def path() -> Path:
    return home_dir() / RECENT_FILE


def _stamp() -> str:
    """**毫秒精度**：同一秒里连开两个 app 是常事，秒级时间戳会让"最近"排不出来。"""
    return datetime.datetime.now().isoformat(timespec="milliseconds")


# 读不出来的原因**不吞**：交到这里，由调用方（首页/CLI）说出来。
_PROBLEMS: List[str] = []


def _on_error(ex) -> None:
    message = ("项目列表 %s 读不出来（%s: %s）——按空处理，**不覆盖它**"
               % (path(), type(ex).__name__, ex))
    if message not in _PROBLEMS:
        _PROBLEMS.append(message)


def problems() -> List[str]:
    """读文件时攒下的问题（调用方负责显示；显示完调 `clear_problems`）。"""
    return list(_PROBLEMS)


def clear_problems() -> None:
    _PROBLEMS.clear()


def _table() -> dict:
    data = _read_table(path(), on_error=_on_error)
    flat = data.get(SECTION) or {}
    return {str(k): str(v) for k, v in flat.items()}


def entries() -> List[dict]:
    """按最后打开时间**倒序**的全部历史（无上限）。"""
    items = [{"root": root, "opened": opened} for root, opened in _table().items()]
    items.sort(key=lambda item: item["opened"], reverse=True)
    return items


def record(root) -> str:
    """把一个**成功打开过**的 app 记进历史（已存在则只刷新时间）。"""
    key = str(Path(root).resolve())
    table = _table()
    table[key] = _stamp()
    _write_table(path(), {SECTION: table}, header=HEADER)
    return key


def forget(root) -> bool:
    """从历史里移除一条。返回是否真的删掉了（没这条 = False，不装删过）。"""
    key = str(Path(root).resolve())
    table = _table()
    if key not in table:
        return False
    table.pop(key)
    if table:
        _write_table(path(), {SECTION: table}, header=HEADER)
    else:
        try:
            path().unlink()
        except OSError:
            pass
    return True


def state_of(root) -> dict:
    """一条历史在**磁盘上的真实状态**——失效不隐藏，只如实标记。

    `name` 取目录名（app 名就是它的目录名；首页不读任何 app 的真源，
    所以列表里显示的永远是"路径说了什么"，不是"app 自己声明了什么"）。
    """
    resolved = Path(root).resolve()
    is_dir = resolved.is_dir()
    is_app = is_dir and (resolved / SOURCE_NAME).is_file()
    return {"root": str(resolved), "name": resolved.name,
            "exists": is_dir, "is_app": is_app,
            "mark": "" if is_app else ("(不是 app)" if is_dir else "(已不在)")}


def known(root) -> Optional[dict]:
    """历史里的这一条（没有则 None）。"""
    key = str(Path(root).resolve())
    for item in entries():
        if item["root"] == key:
            return item
    return None
