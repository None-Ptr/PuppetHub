"""远程 / 多客户端绑定（V2 阶段 4）：跨进程驱动同一个 app。

设计依据：协议层本来就是"**外壳 + 绑定**"两条（`protocol.PlainBinding` 纯内存 /
`controlplane.AppBinding` 真源 + flet 窗口）——远程**不是新协议，是一个新绑定**。
本模块给 stdio 外壳换上 TCP 外壳：多个客户端连上来，说的还是同一套操作
（hello / send / fire / observe / dump / restart），写者仍是驱动者（origin=driver），
**单写者不因为多客户端而破例**——并发写入由 `Session._lock` 串行化。

无头（不建 flet 窗口）：远程绑定的用例是 CI / 自动化 / 跨机驱动，开一个没人看的
窗口反而要求机器有显示器。因此 `interact` / `snapshot` 这类**需要渲染器**的操作
可见地降级（`geometry/snapshot` 的诚实降级是同一个逻辑），其余操作语义不变。

**同一个绑定也给"窗口实例"当耳朵**（`session=` 传进来 + 传 renderer）：窗口实例
本来只发不收（不监听端口 ⇒ 总线的 `deliver_peer_event` 送不到、`who`/`fire`/`call`
找不到它）。现在宿主给窗口进程注入一个 `--listen-port`，窗口在自己的事件循环里挂起
这个绑定——于是窗口实例**也是社会的一员**：收得到消息、点得动按钮。
`interact` 因此对窗口实例**真的可用**（渲染器就在同一个进程里）。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
from typing import Optional

from .appdir import AppDir
from .session import Session

DEFAULT_PORT = 8765

# 处理完之后要请宿主重绘的操作（只读操作不值得每次探活都重绘一次）。
MUTATING_OPS = ("send", "fire", "call", "restart", "interact", "deliver_peer_event")


class TcpBinding:
    """AppBinding 的无头版：同一个 Session，渲染器相关操作可见降级。

    V4 B（app 即服务）与 A（协作）在这里长出两个新操作：`call` = 外部程序
    同步调用本 app **借出**的能力（白名单在被调方，默认拒绝）；`deliver_peer_event`
    = 协作总线的投递端点（hub 反向送达，进观察流 + 触发自主回路——**不写真源**）。

    两种用法：
    - `TcpBinding(AppDir(...))` —— **无头**：自己造 session（CI / 自动化 / 跨机驱动）；
    - `TcpBinding(session=..., renderer=..., after=...)` —— **给窗口实例当耳朵**：
      复用窗口进程里**那个正在跑的 session**，`interact` 也真的可用（渲染器同进程），
      `after` 用来在状态变化后请宿主在自己的事件循环里重绘。
    """

    def __init__(self, app_dir: Optional[AppDir] = None,
                 hub_port: Optional[int] = None,
                 session: Optional[Session] = None,
                 renderer=None, after=None):
        if session is None:
            if app_dir is None:
                raise ValueError("TcpBinding 需要 app_dir（自造 session）或 session（复用）")
            session = Session(app_dir)
            session.start()
        self.session = session
        self.renderer = renderer
        self._after = after
        if hub_port:
            self.session.attach_hub(hub_port)

    def handle(self, request: dict) -> dict:
        """把应答包起来：**变状态的操作用完请宿主重绘**，重绘失败**可见**但不毁应答。

        `after` 会在 TCP 线程上被调到——所以窗口宿主交过来的那个回调只该"排队"，
        真正的重绘要回到窗口自己的事件循环（跨线程直接 `page.update()` 是踩过的坑）。
        """
        reply = self._handle(request)
        if self._after is not None and request.get("op") in MUTATING_OPS:
            try:
                self._after()
            except Exception as ex:  # noqa: BLE001 - 重绘失败留痕，应答照给
                self.session.note("warning", "REPAINT",
                                  "状态已变，但请宿主重绘失败：%s（窗口会在下次本机操作时刷新）"
                                  % ex)
        return reply

    def _handle(self, request: dict) -> dict:
        op = request.get("op")
        session = self.session
        if op == "hello":
            reply = session.hello()
            reply["transport"] = "tcp"
            reply["writer"] = session.writer
            return reply
        if op == "send":
            result = session.send(request.get("batch", []), origin="driver")
            return {"diagnostics": [d.to_dict() if hasattr(d, "to_dict") else d
                                    for d in result]}
        if op == "fire":
            result = session.fire(request.get("target", ""), request.get("event", ""),
                                  request.get("row"), request.get("value"))
            return {"diagnostics": [d.to_dict() if hasattr(d, "to_dict") else d
                                    for d in result]}
        if op == "call":
            return session.call_capability(request.get("name", ""),
                                           request.get("args") or {},
                                           origin="remote")
        if op == "deliver_peer_event":
            return session.deliver_peer_event(request.get("from_app", "?"),
                                              request.get("topic", ""),
                                              request.get("text", ""),
                                              request.get("title", ""))
        if op == "observe":
            return session.engine.observe()
        if op == "dump":
            return {"program": session.engine.program_lines()}
        if op == "restart":
            return {"diagnostics": [d.to_dict() if hasattr(d, "to_dict") else d
                                    for d in session.engine.restart()]}
        if op == "interact":
            if self.renderer is None:
                return {"error": "远程绑定无渲染器：interact 需要把动作投递给真实控件"
                                 "（诚实降级，同 geometry=false 的逻辑）。用 send 改程序、"
                                 "用 fire 触发引擎事件。"}
            # 窗口实例：渲染器就在同一个进程里，动作可以真的投到部件上。
            return self.renderer.deliver(request.get("target", ""),
                                         request.get("action", ""),
                                         request.get("value"))
        if op == "snapshot":
            if self.renderer is None:
                return {"error": "远程绑定无渲染器：截图不可用（本绑定没有声明 snapshot=true，"
                                 "hello.rendering 是如实自述）。"}
            return {"error": "这条通道的截图未接：截图是异步的（要 await 窗口的事件循环），"
                             "而本通道是同步一行一应答。窗口实例请在窗口里自检。"}
        if op == "load":
            return {"error": "远程不提供整份替换：那是不可回滚的确认式动作，"
                             "而远程连接上没有人确认。请发命令批（send）。"}
        if op == "quit":
            return {"ok": True}
        return {"error": "未知操作 %r" % op}


def bind_tcp(port: int) -> socket.socket:
    """bind 在**调用线程**做：端口冲突当场可见（OSError），不靠线程里静默死。

    与 `bus.bind_bus` 同一条纪律——窗口实例的耳朵要能在**窗口里**听见"这个端口没抢到"。
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(8)
    return server


def serve_tcp(binding: TcpBinding, port: int,
              server: Optional[socket.socket] = None) -> None:
    """多客户端 TCP 服务：一行一个 JSON 请求 → 一行 JSON 响应。逐连接线程。

    `quit` = **当前客户端**离开（多客户端语义下它不可能是"服务器退出"——
    别的客户端还连着）。整个实例的停止由编排者杀进程（`hub down`），这与
    "关窗 = 进程退出"是同一条规则：实例的生命周期归它的宿主管。
    """
    server = server if server is not None else bind_tcp(port)
    print("远程绑定已监听 127.0.0.1:%d（写者=驱动者；单写者由 Session 串行化）"
          % port, file=sys.stderr)

    def client(conn: socket.socket) -> None:
        with conn:
            handle = conn.makefile("r", encoding="utf-8")
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                quit_requested = False
                try:
                    request = json.loads(line)
                except json.JSONDecodeError as ex:
                    # 坏 JSON 也要给一句应答；**不能**在下面再读 request——
                    # 首个请求就坏会让 `request` 未绑定，直接炸掉整个连接线程。
                    reply = {"error": "请求不是合法 JSON（%s）" % ex}
                else:
                    try:
                        reply = binding.handle(request)
                    except Exception as ex:  # noqa: BLE001 - 失败过线，不吞
                        reply = {"error": "%s: %s" % (type(ex).__name__, ex)}
                    quit_requested = request.get("op") == "quit"
                _send(conn, reply)
                if quit_requested:
                    return

    try:
        while True:
            conn, addr = server.accept()
            threading.Thread(target=client, args=(conn,), daemon=True,
                             name="remote-%s" % (addr,)).start()
    except (KeyboardInterrupt, OSError):
        pass


def _send(conn: socket.socket, reply: dict) -> None:
    conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="puppethub.remote",
                                     description="远程/多客户端绑定（TCP，无头）")
    parser.add_argument("app", help="app 目录")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--hub-port", type=int, default=None,
                        help="协作总线端口（hub 编排时传入；缺省不接入总线）")
    args = parser.parse_args(argv)
    app = AppDir(args.app)
    if not app.exists():
        print("不是 app 目录（缺少 app.puppet）：%s" % app.root, file=sys.stderr)
        return 1
    try:
        serve_tcp(TcpBinding(app, hub_port=args.hub_port), args.port)
    except OSError as ex:
        print("监听失败：%s" % ex, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
