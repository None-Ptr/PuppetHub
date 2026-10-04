"""无 LLM 实例的控制面：`puppethub run <app> --no-llm`，也可 `-m puppethub.controlplane <app>`。

设计里"无 LLM 实例"**不是例外，是另一类实例**：正常实例的唯一写者是内置 LLM；无 LLM 实例
（conformance / 无人值守 / 将来的 puppetOS）的写者是**驱动者**。本模块把协议外壳绑到真实
app 目录上，于是 `send` 走的是与 LLM **完全相同**的写入路径：引擎校验 → 批末整份写回真源
→ 写前自动快照。**同一条路，只是写者不同**。

与 `puppethub.protocol` 的差别只在绑定：那一个纯内存、每条例例一个新程序（为 conformance 服务）；
这一个有真源、有快照、有目录契约。

**刻意不提供 `load`（整份替换）**：那是不可回滚的**确认式**系统动作，而控制面协议没有
"人确认"这个通道。驱动者要改程序就发命令批。拒绝时说明理由——不静默忽略。
"""

from __future__ import annotations

import argparse
import base64
import sys

import flet as ft

from .appdir import AppDir
from .protocol import degrade, diags, serve
from .render import FletRenderer
from .session import Session


class AppBinding:
    """把协议绑到 app 目录：命令批写回真源、写前自动快照、能力目录从代码派生。"""

    def __init__(self, page: ft.Page, app_dir: AppDir, wipe_memory: bool = False):
        self.page = page
        self.session = Session(app_dir)
        self.wipe_memory = wipe_memory

    @property
    def renderer(self) -> FletRenderer:
        return self.session.renderer

    def start(self) -> None:
        renderer = FletRenderer(self.page, self.session.engine, on_event=self._on_event,
                                assets_dir=str(self.session.app.assets_dir))
        self.session.renderer = renderer
        self.page.add(renderer.host)
        self.page.update()
        self.session.start()            # 插件装配 + 装载真源与能力
        if self.wipe_memory:
            self.session.wipe_memory(origin="driver")
        self.render()

    def _on_event(self, node_id: str, event: str, row, value):
        return self.session.fire(node_id, event, row, value)

    def render(self) -> None:
        for code, message in self.renderer.apply(self.session.engine.render_state()):
            # 渲染器自己的降级走 stderr：协议响应是**语义**通道，别把它搅浑。
            sys.stderr.write("[render] %s: %s\n" % (code, message))
        sys.stderr.flush()
        self.page.update()

    async def handle(self, request: dict) -> dict:
        op = request.get("op")
        session = self.session
        if op == "hello":
            # 协议 + 渲染自述 + **能力目录**（从 capabilities.py 派生，不手写）
            return session.hello()
        if op == "send":
            result = session.send(request.get("batch", []), origin="driver")
            self.render()
            return {"diagnostics": diags(result)}
        if op == "fire":
            result = session.fire(request.get("target", ""), request.get("event", ""),
                                  request.get("row"), request.get("value"))
            self.render()
            return {"diagnostics": diags(result)}
        if op == "restart":
            result = session.engine.restart()
            self.render()
            return {"diagnostics": diags(result)}
        if op == "observe":
            return session.engine.observe()
        if op == "dump":
            return {"program": session.engine.program_lines()}
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
        if op == "call":
            # 服务面（V4 B）在两条外壳上一致：stdio 驱动者也能调借出的能力。
            # 借出白名单与超时校验都在 session 里（同一台机制，不因通道而变）。
            return session.call_capability(request.get("name", ""),
                                           request.get("args") or {},
                                           origin="driver")
        if op == "deliver_peer_event":
            return {"error": "控制面（stdio）不是协作总线的投递端点：总线只投给"
                             "hub 编排的远程实例（有可被反向连接的端口）。"
                             "本实例的协作入口是 send/fire。"}
        if op == "load":
            return {"error": "控制面不提供整份替换：那是不可回滚的确认式系统动作，"
                             "而本协议没有\"人确认\"这个通道。请发命令批（send）。"}
        if op == "quit":
            return {"ok": True}
        return {"error": "未知操作 %r" % op}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="puppethub.controlplane",
                                     description="无 LLM 实例的控制面（stdio 实现协议）")
    parser.add_argument("app", help="app 目录")
    parser.add_argument("--windowed", action="store_true",
                        help="显示窗口（默认隐藏：控制面是无人值守的）")
    parser.add_argument("--wipe-memory", action="store_true",
                        help="启动前清空运行期记忆（不给这个开关就一律保留）")
    args = parser.parse_args(argv)

    app = AppDir(args.app)
    if not app.exists():
        print("不是 app 目录（缺少 app.puppet）：%s" % app.root, file=sys.stderr)
        return 1
    view = ft.AppView.FLET_APP if args.windowed else ft.AppView.FLET_APP_HIDDEN
    print("控制面启动：%s（隐藏窗口；驱动者即写者）" % app.root, file=sys.stderr)
    return serve(lambda page: AppBinding(page, app, wipe_memory=args.wipe_memory),
                 view=view)


if __name__ == "__main__":
    raise SystemExit(main())
