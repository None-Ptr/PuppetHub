"""控制面外壳：把**产品渲染器**包成可被 stdio 协议驱动的子进程。

为什么需要它：`自证`要能证明"这个渲染器按规范行事"，而 conformance 的驱动方式是
"行分隔 JSON over stdio 的子进程"。本模块负责那层**桥接**——渲染是真的（flet 隐藏窗口，
走真部件的事件绑定），不是替身；所以它全绿才说明问题。

**外壳与绑定分开**：外壳只管线程、stdout 纪律、JSON 进出；"收到某个 op 该做什么"由
**绑定**决定。于是同一条桥接被两处复用：

| 绑定 | 谁在用 | 落盘吗 |
|---|---|---|
| `PlainBinding`（本模块） | `-m puppethub.protocol`：conformance / `自证` | 不落盘，纯内存（每条例例一个新程序） |
| `AppBinding`（`controlplane.py`） | `puppethub run <app> --no-llm`：无 LLM 实例，写者是驱动者 | **真源写回 + 写前自动快照** |

三条工程要点（spike 2b 实测出来，不是推演）：

1. **flet 占主线程**。`ft.run(..., view=FLET_APP_HIDDEN)` 阻塞；stdin 由后台守护线程同步读。
2. **请求要回到事件循环**：`asyncio.run_coroutine_threadsafe(handler, loop)` + `future.result()`，
   否则在后台线程里碰部件是未定义行为。
3. **stdout 只能有协议**。任何库的 `print` 一律改道 stderr，否则协议流被污染；
   另外 stdin 必须容忍 **BOM**（Windows 管道会给首行带 BOM）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import threading
from typing import Optional

import flet as ft

from puppet import Engine

from .render import RENDERING, FletRenderer


def emit(payload: dict) -> None:
    """把一条响应写到**协议出口**（真 stdout）。

    注意不是 `sys.stdout`——那个已经被改道到 stderr 了，好让库的 print 污染不了协议。
    """
    line = json.dumps(payload, ensure_ascii=True) + "\n"
    with _write_lock:
        _stdout.write(line)
        _stdout.flush()


def diags(items) -> list:
    return [item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in items or []]


def degrade(feature: str, message: str) -> dict:
    return {"code": "DEGRADED_FEATURE", "level": "info", "message": message,
            "feature": feature}


_stdout = None
_write_lock = threading.Lock()
_ready = threading.Event()
_stop = threading.Event()
_loop = None
_binding = None


# ------------------------------------------------------------------ 外壳

def serve(make_binding, view=ft.AppView.FLET_APP_HIDDEN) -> int:
    """跑外壳。`make_binding(page)` 在 flet 主线程里造绑定；`binding.handle(req)` 是异步的。"""
    global _stdout, _loop, _binding
    _stdout = sys.stdout
    # 把 stdout 让给协议：任何库的 print 一律改道 stderr，协议流不能被污染。
    sys.stdout = sys.stderr
    try:
        sys.stdin.reconfigure(encoding="utf-8-sig")      # 容忍 BOM
    except Exception:  # noqa: BLE001 - 老环境没有 reconfigure 也不致命
        pass
    threading.Thread(target=_reader, daemon=True).start()

    async def gui(page: ft.Page):
        global _loop, _binding
        _loop = asyncio.get_running_loop()
        page.title = "puppethub"
        page.padding = 0
        page.enable_screenshots = True   # 不开这个，截图永远是空——不是"无头下截不了"
        _binding = make_binding(page)
        _binding.start()
        _ready.set()
        while not _stop.is_set():
            await asyncio.sleep(0.05)

    try:
        ft.run(gui, view=view)
    except Exception as ex:  # noqa: BLE001 - 起不来必须看见（驱动者会读到 stderr）
        sys.stderr.write("渲染器启动失败：%s: %s\n" % (type(ex).__name__, ex))
        _stop.set()
        return 1
    return 0


def _reader() -> None:
    _ready.wait()
    for raw in sys.stdin:
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as ex:
            emit({"error": "请求不是合法 JSON：%s" % ex})
            continue
        try:
            future = asyncio.run_coroutine_threadsafe(_binding.handle(request), _loop)
            response = future.result(timeout=120)
        except Exception as ex:  # noqa: BLE001 - 适配器绝不静默：错误原样回传
            response = {"error": "%s: %s" % (type(ex).__name__, ex)}
        emit(response)
        if request.get("op") == "quit":
            break
    _stop.set()


# ------------------------------------------------------------------ 绑定：纯内存（conformance）

class PlainBinding:
    """为 conformance / `自证` 服务：一个纯内存的引擎 + 真渲染器，不落盘。

    每个用例都是一次新的 `load`，所以这里没有"真源"这个概念——写回是 `AppBinding` 的事。
    """

    def __init__(self, page: ft.Page):
        self.page = page
        self.engine: Optional[Engine] = None
        self.renderer: Optional[FletRenderer] = None

    def start(self) -> None:
        self.engine = Engine(rendering=dict(RENDERING))
        self.renderer = FletRenderer(self.page, self.engine, on_event=self._on_event)
        self.page.add(self.renderer.host)
        self.page.update()

    def _on_event(self, node_id: str, event: str, row, value):
        return self.engine.fire(node_id, event, row, value)

    def render(self) -> None:
        """重建控件树并上屏。

        渲染器自己的降级 / 异常**走 stderr**：conformance 的每步诊断是**语义**通道
        （`noDiagnostics` 只看它），往那里塞渲染器内部说明会把"声明即 oracle"搅浑。
        渲染器做不到什么，应当由**能力声明**（`hello.rendering`，含 `notes`）承担。
        """
        for code, message in self.renderer.apply(self.engine.render_state()):
            sys.stderr.write("[render] %s: %s\n" % (code, message))
        sys.stderr.flush()
        self.page.update()

    async def handle(self, request: dict) -> dict:
        op = request.get("op")
        engine = self.engine
        if op == "hello":
            return {"protocol": "1", "rendering": dict(RENDERING), "catalog": []}
        if op == "load":
            self.renderer.assets_dir = request.get("assetsDir") or None
            result = engine.load(
                request.get("program", []),
                capabilities=request.get("capabilities"),
                limits=request.get("limits"),
                seed_state=request.get("seedState"),
                render_geometry=request.get("renderGeometry"),
                rendering=request.get("rendering"),
                capability_modules=request.get("capabilityModules"),
                assets_dir=request.get("assetsDir"))
            self.render()
            return {"diagnostics": diags(result)}
        if op == "send":
            result = engine.apply_batch(request.get("batch", []))
            self.render()
            return {"diagnostics": diags(result)}
        if op == "fire":
            result = engine.fire(request.get("target", ""), request.get("event", ""),
                                 request.get("row"), request.get("value"))
            self.render()
            return {"diagnostics": diags(result)}
        if op == "restart":
            result = engine.restart()
            self.render()
            return {"diagnostics": diags(result)}
        if op == "observe":
            return engine.observe()
        if op == "dump":
            return {"program": engine.program_lines()}
        if op == "interact":
            result = self.renderer.deliver(request.get("target", ""),
                                           request.get("action", ""),
                                           request.get("value"))
            self.render()
            return result
        if op == "snapshot":
            image = await self.renderer.capture()
            if not image:
                return {"image": None, "format": "png",
                        "diagnostics": [degrade("snapshot", "本次截图未产出图像（重试后仍为空）")]}
            return {"image": base64.b64encode(image).decode("ascii"), "format": "png"}
        if op == "quit":
            return {"ok": True}
        return {"error": "未知操作 %r" % op}


def main() -> int:
    return serve(lambda page: PlainBinding(page))


if __name__ == "__main__":
    raise SystemExit(main())
