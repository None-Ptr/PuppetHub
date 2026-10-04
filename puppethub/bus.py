"""协作总线（V4 A）：agent 社会的邮局——app 之间的消息与能力调用路由。

**铁律的延伸，不是例外**：hub 的原则是"不制造第二写者"，总线把它贯彻到
app 协作里——

- `post`（消息）：投递给订阅者的**观察流**，并触发其自主回路。消息是**刺激**，
  不是写入——接收方的真源只有它自己的写者能改。
- `call`（跨 app 能力调用）：hub 把请求转给**被调方自己的服务端口**，执行
  发生在被调方进程内、走它自己的借出白名单（`[service] lend`，缺省空 =
  默认拒绝）。hub 不执行任何能力，只是邮局。

两端形态：
- `serve_bus`（hub 进程）：路由 + 审计（`bus.jsonl`——"谁在何时对谁说了什么"
  必须查得回来，这是 agent 社会的观察流）；
- `BusClient`（app 进程）：`post` 发消息。没有 hub 就**可见失败**——
  协作是能力不是幻觉。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Callable, Optional

BUS_FILE = "bus.jsonl"


# ------------------------------------------------------------------ hub 端


class HubBus:
    """总线核心：订阅表 + 审计 + 投递。与 TCP 壳分离（测试可直接驱动）。"""

    def __init__(self, lookup_port: Callable[[str], Optional[int]],
                 audit_path: Optional[Path] = None,
                 timeout: float = 2.5,
                 on_message: Optional[Callable[[dict], None]] = None):
        self._lookup_port = lookup_port
        self._audit_path = audit_path
        self._timeout = timeout
        # 观察者：每投递一次（含回执）调一次。**社会层（首页）用它把消息变成
        # 调度官的刺激源**；它不是订阅者，不改投递语义。
        self._on_message = on_message
        self._subs: dict[str, set] = {}      # app -> topics
        self._lock = threading.Lock()

    # ---- 订阅

    def subscribe(self, app: str, topics: list) -> dict:
        with self._lock:
            self._subs[app] = {str(t) for t in topics}
        return {"ok": True, "topics": sorted(self._subs.get(app, set()))}

    # ---- 消息

    def post(self, from_app: str, topic: str, text: str,
             title: str = "") -> dict:
        """投递给订阅了 topic 的**其他** app（不回环给发送者）。审计先行。"""
        entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "from": from_app, "topic": topic, "title": title[:120],
                 "text": text[:2000]}
        self._audit(entry)
        delivered, failed = [], []
        with self._lock:
            targets = [(app, sorted(topics)) for app, topics in self._subs.items()
                       if app != from_app and topic in topics]
        for app, _ in targets:
            port = self._lookup_port(app)
            reply = self._deliver(app, port, entry)
            (delivered if reply.get("ok") else failed).append(app)
        self._audit({"time": entry["time"], "from": from_app,
                     "topic": topic, "delivery": {"ok": delivered, "failed": failed}})
        if self._on_message is not None:
            try:
                self._on_message(dict(entry, delivered=list(delivered),
                                      failed=list(failed)))
            except Exception as ex:  # noqa: BLE001 - 观察者坏掉**可见**，但总线照常
                self._audit({"time": entry["time"], "from": "bus", "topic": "observer",
                             "error": "%s: %s" % (type(ex).__name__, ex)})
        return {"ok": True, "delivered": delivered, "failed": failed}

    def _deliver(self, app: str, port: Optional[int], entry: dict) -> dict:
        if not port:
            return {"ok": False, "error": "app %s 不在跑（hub 账本无端口）" % app}
        request = {"op": "deliver_peer_event", "from_app": entry["from"],
                   "topic": entry["topic"], "title": entry.get("title", ""),
                   "text": entry["text"]}
        return _tcp_request(port, request, timeout=self._timeout)

    # ---- 审计

    def _audit(self, entry: dict) -> None:
        if self._audit_path is None:
            return
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass                          # 审计失败：账本在 hub 侧，投递不受阻

    def recent(self, limit: int = 20) -> list:
        if self._audit_path is None or not self._audit_path.is_file():
            return []
        try:
            lines = self._audit_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


def _tcp_request(port: int, request: dict, timeout: float) -> dict:
    try:
        conn = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError as ex:
        return {"ok": False, "error": "连接失败：%s" % ex}
    try:
        conn.settimeout(timeout)
        conn.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        return json.loads(conn.makefile("r", encoding="utf-8").readline())
    except (OSError, json.JSONDecodeError) as ex:
        return {"ok": False, "error": "应答失败：%s" % ex}
    finally:
        conn.close()


def bind_bus(port: int) -> socket.socket:
    """bind 在**调用线程**做：端口冲突当场可见（OSError），不靠线程里静默死。"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(8)
    return server


def serve_bus(bus: HubBus, port: int, server: Optional[socket.socket] = None) -> None:
    """总线的 TCP 壳：一行 JSON 请求 → 一行 JSON 应答。"""
    server = server if server is not None else bind_bus(port)

    def client(conn: socket.socket) -> None:
        with conn:
            handle = conn.makefile("r", encoding="utf-8")
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except json.JSONDecodeError as ex:
                    reply = {"error": "请求不是合法 JSON（%s）" % ex}
                else:
                    op = request.get("op")
                    try:
                        if op == "post":
                            reply = bus.post(request.get("from_app", "?"),
                                             request.get("topic", ""),
                                             request.get("text", ""),
                                             request.get("title", ""))
                        elif op == "subscribe":
                            reply = bus.subscribe(request.get("app", "?"),
                                                  request.get("topics") or [])
                        elif op == "dump":
                            reply = {"ok": True, "entries": bus.recent(
                                int(request.get("limit") or 20))}
                        else:
                            reply = {"error": "未知操作 %r" % op}
                    except Exception as ex:  # noqa: BLE001 - 失败过线，不吞
                        reply = {"error": "%s: %s" % (type(ex).__name__, ex)}
                conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n")
                             .encode("utf-8"))

    while True:
        try:
            conn, addr = server.accept()
        except OSError:
            if _fileno(server) == -1:
                return                 # 监听套接字被我们自己关掉 = 正常下线
            raise                      # 别的 OSError 不吞
        threading.Thread(target=client, args=(conn,), daemon=True,
                         name="bus-%s" % (addr,)).start()


def _fileno(server) -> int:
    try:
        return server.fileno()
    except (OSError, ValueError):
        return -1


# ------------------------------------------------------------------ app 端


class BusClient:
    """app 端总线客户端。没有 hub / 连不上 = **可见失败**，不装作发了。"""

    def __init__(self, app_name: str, hub_port: int, timeout: float = 3.0):
        self.app_name = app_name
        self.hub_port = int(hub_port)
        self._timeout = timeout

    def request(self, payload: dict, timeout: Optional[float] = None) -> dict:
        return _tcp_request(self.hub_port, payload,
                            timeout=self._timeout if timeout is None else timeout)

    def post(self, topic: str, text: str, title: str = "") -> dict:
        return self.request({"op": "post", "from_app": self.app_name,
                             "topic": topic, "title": title, "text": text})

    def subscribe(self, topics: list) -> dict:
        return self.request({"op": "subscribe", "app": self.app_name,
                             "topics": list(topics)})
