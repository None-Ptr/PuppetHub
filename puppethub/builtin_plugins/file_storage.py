"""内置 storage：文件存储。

**机制**只有读写；**策略**（路径白名单、JSON 序列化、jsonl 组装、快照淘汰、脏标记）
全在宿主 `plugins.HostStorage`。所以"重置不删快照""命名快照不淘汰"这些已定不变量
对任何 storage 插件都成立——插件不可能"忘记淘汰"或"偷偷改 reset 语义"。
"""

from __future__ import annotations

import os

NAME = "file"
PROVIDES = ["storage"]


class FileStorage:
    def __init__(self, api=None):
        self.api = api

    def read(self, path: str) -> str:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def write(self, path: str, text: str) -> None:
        """**原子性义务**：先写临时文件再替换，绝不出现半份文件。"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def append(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def list(self, path: str) -> list:
        return sorted(os.listdir(path))

    def remove(self, path: str) -> None:
        if os.path.isdir(path):
            import shutil
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.isfile(path):
            os.remove(path)

    def exists(self, path: str) -> bool:
        return os.path.exists(path)


def create_storage(api) -> FileStorage:
    return FileStorage(api)
