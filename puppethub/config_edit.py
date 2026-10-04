"""外科式配置写入：只改指定小节里的指定键，**其余内容原样保留**。

## 为什么不用"读进来整份重写"

`tomllib` 只能读；而"读进来再整份写回"会**吃掉注释与顺序**——配置文件是给人
读的，注释是它的一半。GUI 的设置面板改两个键（`base_url` / `model`），不该把
用户写在旁边的解释清掉。

所以这里做的是**按行手术**：定位小节 → 改/插/删那几行键，别的一律不碰。
写不进去（只读、目录不存在）如实抛错——静默丢失配置是最坏的一种失败。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .secrets import _read_table, _toml_escape


def _header_of(line: str) -> Optional[str]:
    """`[a.b]` → `"a.b"`；不是小节头返回 None。"""
    text = line.strip()
    if not text.startswith("[") or not text.endswith("]"):
        return None
    if text.startswith("[["):          # 数组表不支持（我们的配置不用）
        return None
    return text[1:-1].strip()


def _render(key: str, value) -> str:
    if isinstance(value, bool):
        return "%s = %s" % (key, "true" if value else "false")
    if isinstance(value, (int, float)):
        return "%s = %s" % (key, value)
    if isinstance(value, (list, tuple)):
        return "%s = [%s]" % (key, ", ".join('"%s"' % _toml_escape(str(v)) for v in value))
    return '%s = "%s"' % (key, _toml_escape(str(value)))


def set_options(path: Path, section: str, values: dict) -> Path:
    """在 `path` 的 `[section]` 里写入 `values`（`None` = 删除该键）。

    小节不存在就**追加**到文件末尾（不覆盖任何现有内容）；键被删空的小节
    （只剩小节头、没有键也没有注释）会一并清掉——**空小节是噪声**。
    更新已有的键时**保留行内注释**：人写在 `model = "x"  # 为什么` 后面的
    那句话，不该因为改个值就消失。

    返回写入的路径。
    """
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []

    def locate(name: str):
        for index, line in enumerate(lines):
            if _header_of(line) == name:
                end = len(lines)
                for probe in range(index + 1, len(lines)):
                    if _header_of(lines[probe]) is not None:
                        end = probe
                        break
                return index, end
        return None, None

    def inline_comment(line: str) -> str:
        """把 `key = value  # 注释` 的尾巴摘出来（值里的 # 由引号保护，不摘）。"""
        if "#" not in line:
            return ""
        head, _, tail = line.partition("#")
        if head.count('"') % 2:              # # 落在引号内 → 不是注释
            return ""
        return "  #" + tail

    pending = dict(values)
    start, end = locate(section)
    if start is None:
        block = ["", "[%s]" % section]
        for key, value in pending.items():
            if value is not None:
                block.append(_render(key, value))
        lines.extend(block)
    else:
        for index in range(start + 1, end):
            if "=" not in lines[index]:
                continue
            key = lines[index].split("=", 1)[0].strip()
            if key in pending:
                value = pending.pop(key)
                if value is None:
                    lines[index] = None
                else:
                    lines[index] = _render(key, value) + inline_comment(lines[index])
        lines = [line for line in lines if line is not None]
        start, end = locate(section)
        tail = end
        while tail > start + 1 and not lines[tail - 1].strip():
            tail -= 1
        for key, value in pending.items():
            if value is not None:
                lines.insert(tail, _render(key, value))
                tail += 1
        # 键被删空、且没留下注释的小节：连头一起清掉（避免留下一个空壳）
        start, end = locate(section)
        block = [line for line in lines[start + 1:end] if line.strip()]
        if start is not None and not block:
            keep = [line for line in lines[:start]
                    if not (line.strip() == "[%s]" % section)]
            keep += lines[end:]
            lines = keep

    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines).rstrip("\n") + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def read_options(path: Path, section: str) -> dict:
    """读回某个小节的当前值（`tomllib` 为准；读不到就是空）。"""
    data = _read_table(Path(path))
    node = data
    for part in section.split("."):
        node = (node or {}).get(part) if isinstance(node, dict) else None
    return dict(node) if isinstance(node, dict) else {}
