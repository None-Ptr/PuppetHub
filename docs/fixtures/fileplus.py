"""无外部依赖的 storage 插件（冒烟/沙箱夹具用）。

代码与内置 file 插件相同，但**独立成文件**：沙箱 worker 在子进程里只加载这一个文件，
验证的正是"第三方插件以文件形态被沙箱化"的真实路径。NAME 必须唯一（插件名即身份）。
"""

from __future__ import annotations

import os

NAME = "fileplus"
PROVIDES = ["storage"]


class FilePlus:
    def __init__(self, api=None):
        self.api = api

    def read(self, path: str) -> str:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def write(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)

    def append(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def list(self, path: str) -> list:
        return sorted(os.listdir(path))

    def remove(self, path: str) -> None:
        import shutil
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.isfile(path):
            os.remove(path)

    def exists(self, path: str) -> bool:
        return os.path.exists(path)


def create_storage(api) -> FilePlus:
    return FilePlus(api)
