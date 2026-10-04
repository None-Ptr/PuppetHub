"""冒烟夹具：把每一次写 / 删的**相对路径**记录下来的 storage 插件。

机制完全委托内置 `file` 插件（它才是机制）；这个插件只做一件事：**记录宿主
让它写过什么**。冒烟用它证明"快照 / 引擎状态也走 storage 通道"——直接断言
文件存在只能证明位置一致，证明不了通道一致。

模块级 `WRITES` / `REMOVES`：冒烟脚本经 `sys.modules` 读取（插件由
`plugins.load_plugin_file` 以固定模块名装入）。
"""

from __future__ import annotations

import os

NAME = "spy-storage"
PROVIDES = ["storage"]

WRITES: list[str] = []
REMOVES: list[str] = []


class SpyStorage:
    def __init__(self, api=None):
        self.api = api
        self._inner = None

    def _file(self):
        if self._inner is None:
            from puppethub.builtin_plugins.file_storage import FileStorage
            self._inner = FileStorage()
        return self._inner

    def read(self, path: str) -> str:
        return self._file().read(path)

    def write(self, path: str, text: str) -> None:
        WRITES.append(path.replace(os.sep, "/"))
        self._file().write(path, text)

    def append(self, path: str, text: str) -> None:
        WRITES.append(path.replace(os.sep, "/"))
        self._file().append(path, text)

    def list(self, path: str) -> list:
        return self._file().list(path)

    def remove(self, path: str) -> None:
        REMOVES.append(path.replace(os.sep, "/"))
        self._file().remove(path)

    def exists(self, path: str) -> bool:
        return self._file().exists(path)


def create_storage(api) -> SpyStorage:
    return SpyStorage(api)
