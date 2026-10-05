"""多 app 编排（V3 提前落地的最小形态）：发现、进程生命周期、健康检查。

**先说清这不是什么**：编排**不制造第二个写者**。hub 启动的每个 app 是一个独立的
远程绑定实例（写者 = 驱动者，或该 app 自己的写者）；hub 自己从不写任何 app——
"一个写者写多个 app"是真正的融合语义，留在 V3 设计里（`design-v3-fusion.md`）。

三件事，各自诚实：
- **发现**：扫父目录下的 app 目录（含 `app.puppet` 的）。零猜测：不是 app 的不算。
- **生命周期**：`up` 把每个 app 作为 `remote` 子进程拉起（端口分配写进 hub 账本）；
  `down` 按账本里的 pid 结束它们。账本在 `<父目录>/.puppethub-hub/hub.json`——
  父目录不是 app，账本不能塞进任何一个 app 的 `.puppethub/` 里。
- **健康检查**：对每个账本条目做一次 TCP `hello`——**活着**的定义是"能应答握手"，
  不是"进程表里有个 pid"（僵尸进程会骗过后者）。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .bus import BUS_FILE, HubBus, bind_bus, serve_bus

APP_FILE = "app.puppet"
HUB_DIR = ".puppethub-hub"
STATE_FILE = "hub.json"
DEFAULT_BASE_PORT = 8800


def state_path(parent: Path) -> Path:
    return parent / HUB_DIR / STATE_FILE


def discover(parent: Path) -> list:
    """父目录下的 app 目录（含 `app.puppet` 的）。零猜测：不是 app 的不算。"""
    return sorted(
        (child for child in parent.iterdir()
         if child.is_dir() and (child / APP_FILE).is_file()),
        key=lambda p: p.name)


def _load_state(parent: Path) -> dict:
    path = state_path(parent)
    if not path.is_file():
        return {"apps": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"apps": {}}
    except (json.JSONDecodeError, OSError):
        return {"apps": {}}


def _save_state(parent: Path, state: dict) -> None:
    path = state_path(parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _spawn_command(app_root: Path, port: int, hub_port: Optional[int] = None) -> list:
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "remote", str(app_root), "--port", str(port)]
    else:
        cmd = [sys.executable, "-m", "puppethub", "remote", str(app_root),
               "--port", str(port)]
    if hub_port:
        cmd += ["--hub-port", str(hub_port)]
    return cmd


def _free_port(start: int, taken: set) -> int:
    port = start
    while port in taken:
        port += 1
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return _free_port(port + 1, taken)
    finally:
        probe.close()
    return port


def ping(port: int, timeout: float = 1.5) -> dict:
    """TCP `hello`。活着 = 能应答握手；连不上 = 死（僵尸进程骗不过这条）。"""
    try:
        conn = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError:
        return {"alive": False}
    try:
        conn.settimeout(timeout)
        conn.sendall(b'{"op": "hello"}\n')
        reply = json.loads(conn.makefile("r", encoding="utf-8").readline())
        return {"alive": reply.get("protocol") == "1", "hello": reply}
    except (OSError, json.JSONDecodeError):
        return {"alive": False}
    finally:
        conn.close()


# ------------------------------------------------------------------ 动作

READY_TIMEOUT = 25.0


def up(parent: Path, base_port: int = DEFAULT_BASE_PORT) -> list:
    """把所有未在跑的 app 拉成远程绑定子进程。已在跑的**不动**（幂等）。

    "拉起"必须**等握手**才算数：端口入账了却不通，就是"假成功"——所以每实例
    等到 hello 应答（或超时后如实标 `ready=False`，条目保留：它可能只是慢）。
    总线（V4 A）随 up 起在 hub 进程内：app 之间只经它说话（消息 / 能力借出），
    hub 仍然不写任何 app。
    """
    state = _load_state(parent)
    taken = {entry.get("port") for entry in state["apps"].values()}
    bus_port = _ensure_bus(parent, base_port, taken)
    if bus_port:
        taken.add(bus_port)
        # _ensure_bus 内部是**自己的 state 副本**落的盘——这里不同步，主循环
        # 随后的 _save_state 就会用旧副本把 bus_port 覆盖没（双副本写回冲突）。
        state["bus_port"] = bus_port
    started = []
    for app in discover(parent):
        name = app.name
        entry = state["apps"].get(name)
        if entry and ping(entry["port"])["alive"]:
            continue                       # 幂等：活着就不重启
        port = _free_port(base_port, taken)
        taken.add(port)
        proc = subprocess.Popen(_spawn_command(app, port, hub_port=bus_port),
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess,
                                                      "CREATE_NEW_PROCESS_GROUP", 0))
        state["apps"][name] = {"port": port, "pid": proc.pid, "started": True}
        _save_state(parent, state)
        deadline = time.time() + READY_TIMEOUT
        ready = False
        while time.time() < deadline:
            if ping(port, timeout=1.0)["alive"]:
                ready = True
                break
            if proc.poll() is not None:
                break                      # 进程已退出：再等也不通，如实上报
            time.sleep(0.3)
        started.append({"name": name, "port": port, "pid": proc.pid,
                        "ready": ready})
    _save_state(parent, state)
    return started


_BUS: "dict[str, object]" = {}                 # parent -> (HubBus, 线程)：幂等，不重起


def _ensure_bus(parent: Path, base_port: int, taken: set) -> Optional[int]:
    """总线随 up 起一次（幂等）。起不来**可见**——协作是能力，不是幻觉。"""
    key = str(parent)
    if key in _BUS:
        return _BUS[key]["port"]
    state = _load_state(parent)
    port = state.get("bus_port")
    if port and ping(int(port), timeout=0.6)["alive"]:
        # 上一个 hub 进程的总线还活着：沿用（账本就是跨进程的真相）。
        _BUS[key] = {"port": int(port)}
        return int(port)
    port = _free_port(base_port + 500, taken)
    audit = parent / HUB_DIR / BUS_FILE
    bus = HubBus(lookup_port=lambda name: (_load_state(parent)["apps"]
                                           .get(name, {}) or {}).get("port"),
                 audit_path=audit)
    try:
        server = bind_bus(int(port))
    except OSError as ex:
        print("总线启动失败：%s（app 将以无协作模式运行）" % ex, file=sys.stderr)
        return None
    threading.Thread(target=serve_bus, args=(bus, int(port), server), daemon=True,
                     name="hub-bus").start()
    state["bus_port"] = int(port)
    _save_state(parent, state)
    _BUS[key] = {"port": int(port)}
    return int(port)


def bus_entries(parent: Path, limit: int = 20) -> list:
    """总线审计的尾部（`hub bus` 动作）。没有就空——不编造。"""
    path = parent / HUB_DIR / BUS_FILE
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def down(parent: Path) -> list:
    """按账本结束所有实例。pid 不在/已死的**如实列出**，不装作都停干净了。"""
    state = _load_state(parent)
    stopped, dead = [], []
    for name, entry in list(state["apps"].items()):
        pid = entry.get("pid")
        try:
            os.kill(pid, signal.SIGTERM)
            stopped.append({"name": name, "port": entry.get("port"), "pid": pid})
        except (OSError, ProcessLookupError):
            dead.append({"name": name, "pid": pid})
        state["apps"].pop(name, None)
    _save_state(parent, state)
    return stopped, dead


def status(parent: Path) -> list:
    """账本里每个条目的真实健康度（TCP hello），外加账本外新出现的 app。"""
    state = _load_state(parent)
    rows = []
    for app in discover(parent):
        entry = state["apps"].get(app.name)
        if entry:
            probe = ping(entry["port"])
            rows.append({"name": app.name, "port": entry.get("port"),
                         "alive": probe["alive"], "managed": True})
        else:
            rows.append({"name": app.name, "port": None, "alive": None,
                         "managed": False})
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="puppethub.hub", description="多 app 编排（发现 / 生命周期 / 健康检查）")
    parser.add_argument("dir", help="父目录（其下含 app.puppet 的子目录视为 app）")
    parser.add_argument("action", choices=["list", "up", "down", "status", "bus"],
                        help="list=发现 · up=拉起（含协作总线）· down=停止 · "
                             "status=健康检查 · bus=协作审计尾部")
    parser.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT)
    args = parser.parse_args(argv)
    parent = Path(args.dir).resolve()
    if not parent.is_dir():
        print("父目录不存在：%s" % parent, file=sys.stderr)
        return 1

    if args.action == "list":
        apps = discover(parent)
        if not apps:
            print("（此目录下没有 app）")
            return 0
        for app in apps:
            print("  %s" % app.name)
        return 0
    if args.action == "up":
        started = up(parent, args.base_port)
        for item in started:
            print("  已拉起 %s → 127.0.0.1:%d（pid %d）"
                  % (item["name"], item["port"], item["pid"]))
        print("共 %d 个实例（已在跑的不动）。" % len(started))
        # **诚实边界**：总线是本进程里的 daemon 线程——命令一返回它就没了，
        # 此后 app 之间发消息必然连不上。要常驻的社会层请用首页（裸跑 `puppethub`）。
        print("注意：协作总线随本命令退出而**下线**（此后 app 之间消息不通）。"
              "要常驻的社会层直接裸跑 `puppethub`（首页即宿主）。", file=sys.stderr)
        return 0
    if args.action == "down":
        stopped, dead = down(parent)
        for item in stopped:
            print("  已停止 %s（pid %d）" % (item["name"], item["pid"]))
        for item in dead:
            print("  %s 的 pid %d 已不存在（如实列出，不装作停过）" % (item["name"], item["pid"]))
        return 0
    if args.action == "bus":
        entries = bus_entries(parent, limit=30)
        if not entries:
            print("（协作总线还没有审计记录：app 之间还没说过话）")
            return 0
        for entry in entries:
            if "delivery" in entry:
                print("  %s 投递回执 %s.%s → ok=%s failed=%s"
                      % (entry.get("time"), entry.get("from"), entry.get("topic"),
                         entry["delivery"].get("ok"), entry["delivery"].get("failed")))
            else:
                print("  %s %s [%s] %s：%s"
                      % (entry.get("time"), entry.get("from"), entry.get("topic"),
                         entry.get("title") or "-", (entry.get("text") or "")[:60]))
        return 0
    rows = status(parent)
    if not rows:
        print("（此目录下没有 app）")
        return 0
    for row in rows:
        state = {True: "在跑", False: "无应答", None: "未编排"}[row["alive"]]
        port = ":%d" % row["port"] if row["port"] else "-"
        print("  %-20s %s %-8s %s" % (row["name"], state, port,
                                      "（hub 管理）" if row["managed"] else "（未编排）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
