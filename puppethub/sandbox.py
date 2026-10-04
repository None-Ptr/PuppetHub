"""storage 插件的子进程沙箱（V2 阶段 3）。

**边界在哪**：路径白名单（`.puppet/` + `.puppethub/`）与路径解析**本来就在宿主
`HostStorage`**——插件从来只收到"白名单内已解析的绝对路径"。沙箱补的是另一半：
进程隔离。插件代码跑在子进程里，崩了带不走宿主、卡了可杀、拿不到宿主内存
（引擎、真源、审批表对它彻底不存在）。

**为什么只对"文件来源"插件有意义**：内置插件随产品分发、与产品同一信任级；
把它们也塞进子进程只会增加故障面，不会增加安全。所以对内置插件**可见地拒绝**
沙箱化并说明理由，而不是假装包了一层就更安全。

协议：一行一个 JSON 请求 `{id, method, args}` → `{id, ok, result}`。机制简单到
不需要第三方依赖，也不需要信任子进程的输出格式之外的任何东西。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

_WORKER = r'''"""沙箱 worker：加载 storage 插件文件，逐请求执行。由 puppethub.sandbox 启动。"""
import importlib.util
import json
import os
import sys

spec = importlib.util.spec_from_file_location("sandboxed_plugin", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
api = type("API", (), {"option": staticmethod(lambda k, d=None: d),
                       "secret": staticmethod(lambda n: os.environ.get(n)),
                       "log": staticmethod(lambda *a: print(json.dumps(
                           {"id": 0, "ok": True, "log": [str(a)]}), flush=True))})
plugin = module.create_storage(api)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue
    out = {"id": req.get("id")}
    try:
        func = getattr(plugin, req["method"])
        out["result"] = func(*req.get("args", []))
        out["ok"] = True
    except Exception as ex:  # noqa: BLE001 - 失败过线，由宿主标脏
        out["ok"] = False
        out["error"] = "%s: %s" % (type(ex).__name__, ex)
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()
'''


class SubprocessStorage:
    """把一个 storage 插件文件跑进子进程。接口与进程内插件完全一致（duck）。"""

    TIMEOUT = 10.0

    def __init__(self, plugin_path: str):
        self._proc = subprocess.Popen(
            [sys.executable, "-c", _WORKER, plugin_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8")
        self._id = 0
        self._lock = threading.Lock()

    def _call(self, method: str, *args):
        with self._lock:
            self._id += 1
            req = json.dumps({"id": self._id, "method": method, "args": list(args)},
                             ensure_ascii=False)
            try:
                self._proc.stdin.write(req + "\n")
                self._proc.stdin.flush()
                line = self._proc.stdout.readline()
            except (OSError, ValueError) as ex:
                raise RuntimeError("沙箱子进程通信失败：%s" % ex) from ex
            if not line:
                raise RuntimeError("沙箱子进程已退出（插件崩溃或卡死后被杀）")
            reply = json.loads(line)
            if not reply.get("ok"):
                raise RuntimeError(reply.get("error", "沙箱内失败"))
            return reply.get("result")

    def read(self, path: str) -> str:
        return self._call("read", path)

    def write(self, path: str, text: str) -> None:
        self._call("write", path, text)

    def append(self, path: str, text: str) -> None:
        self._call("append", path, text)

    def list(self, path: str) -> list:
        return self._call("list", path)

    def remove(self, path: str) -> None:
        self._call("remove", path)

    def exists(self, path: str) -> bool:
        return bool(self._call("exists", path))

    def stop(self) -> None:
        try:
            self._proc.stdin.close()
            self._proc.wait(timeout=3)
        except Exception:  # noqa: BLE001 - 收尾尽力而为
            self._proc.kill()


def sandbox_wrap(plugin_name, registry):
    """返回 `(子进程实例, 警告)`。没法沙箱化时实例为 None + 一条说清理由的警告。"""
    plugin = registry.plugins.get(str(plugin_name or ""))
    if plugin is None:
        return None, "沙箱：找不到 storage 插件 %r，按进程内运行" % plugin_name
    if plugin.builtin or not os.path.isfile(plugin.source):
        return None, ("storage 插件 %s 是内置插件（随产品分发、同一信任级），"
                      "沙箱只对文件来源的第三方插件有意义；已按进程内运行"
                      % plugin.name)
    try:
        return SubprocessStorage(plugin.source), ""
    except Exception as ex:  # noqa: BLE001 - 沙箱起不来必须可见，但不阻止启动
        return None, "沙箱子进程启动失败（%s），已按进程内运行" % ex
