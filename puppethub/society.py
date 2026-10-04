"""社会层：**一个常驻进程里的总线 + 账本 + 编排**。

## 为什么需要它（它顺手修掉了一个既有缺陷）

`puppethub hub <dir> up` 是 **one-shot**（`cli.py:265-268` → `hub.main` 打印完即
返回），而 `serve_bus` 起的是 **daemon 线程**（`hub.py:184-185`）。⇒ 命令一返回，
总线就随进程消失，此后任何 `tell` 必然连不上。

**也就是说，在首页出现之前，"社会"从未真的成立过**——只有 `up` 那一瞬总线活着
（app 在启动时完成订阅注册，那是唯一的成功窗口）。既有冒烟抓不到，因为它们
**在进程内**直接驱动 `HubBus`（`bus.py:35` 明说"与 TCP 壳分离"），模块级 `_BUS`
让总线在测试进程里活着。这是"用例全绿 ≠ 改动正确"的又一例。

所以社会层必须有一个**常驻进程**当宿主：首页就是那个进程（`docs/design-home.md` §4）。
总线活在首页进程内，`up/down/status/open/tell` 全是进程内调用。

## 与 `hub` 的分工（两条路各自自洽，不互相污染）

| | 作用域 | 账本 | 总线 |
|---|---|---|---|
| `puppethub hub <dir>`（CLI） | **父目录** | `parent/.puppethub-hub/state.json` | 父目录一条 |
| 社会层（首页） | **整台机器** | `$PUPPETHUB_HOME/society.json` | 一条 |

CLI 那条路的语义**原样不动**。

## 身份、命名与两处已知不对称

- 总线按 **app 名**（= 目录名）寻址（`bus.HubBus` 的订阅表键就是名字）。首页的
  MRU 是**跨目录**的，同名会撞——**撞了就如实报冲突并拒绝**，不猜。
- **`tell` 按 topic 路由**（现状语义，`bus.post` 投给订阅了该 topic 的其他 app）。
  所以"发给谁"由**对方的订阅**决定，不由发送方指定；回执里如实报谁收到、谁没收到。
- **窗口实例也在账本里**（`windows` 段）：首页 `open` 会给它分配一个 `--listen-port`，
  窗口在**自己的进程里**挂一个复用同一 session 的 TCP 绑定 ⇒ 它同样收得到 `tell`、
  也答得了 `who`/`fire`/`call`（`interact` 因为渲染器同进程而真的可用）。
- 同名同时有"无头实例"和"窗口"时，账本**两段各存一份**（不互相覆盖）：
  总线投递优先给**无头实例**（它是社会的正式成员，窗口只是附带的观察窗）。
  两者并存 = 跨进程两个写者，所以 `open`/`up` 在这种情形下会**如实警告**（不拦，见
  `docs/design-home.md` 的已知盲区）。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import recent
from .bus import HubBus, _tcp_request, bind_bus, serve_bus
from .hub import _free_port, ping
from .secrets import home_dir

SOURCE_NAME = "app.puppet"
LEDGER_FILE = "society.json"
BUS_FILE = "bus.jsonl"
BUS_BASE_PORT = 9300          # 与 hub 的 8800/8850 错开，避免与 CLI 那条路抢端口
PORT_BASE = 9400
READY_TIMEOUT = 25.0
WINDOW_READY_TIMEOUT = 15.0   # 窗口实例还要起 flet，给它短一点（起不来会被 2s 探活继续探到）
INSTANT_EXIT_WAIT = 2.0       # `open` 之后等这么久：立刻死的子进程要被抓出来
LOG_TAIL = 40


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _drain(proc, tail: deque) -> None:
    """把子进程输出收进环形缓冲（供"立刻退出"时如实报尾巴）。"""
    try:
        for line in proc.stdout or ():
            tail.append(line.rstrip("\n"))
    except (OSError, ValueError):
        pass


def _spawn(cmd: List[str], tail: Optional[deque] = None):
    """起一个**独立的**子进程（新进程组：首页被 Ctrl+C 不该拖死别人）。

    输出走管道并有人读——不读的管道会把子进程堵死；而"立刻退出"时我们正好要它。
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    if tail is not None:
        threading.Thread(target=_drain, args=(proc, tail), daemon=True,
                         name="society-log").start()
    return proc


def headless_command(app_root: Path, port: int, hub_port: Optional[int]) -> List[str]:
    """无头实例：复用 `hub` 的既有拼法（冻结态 `remote` 是子命令，源码态走 `-m`）。"""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "remote", str(app_root), "--port", str(port)]
    else:
        cmd = [sys.executable, "-m", "puppethub", "remote", str(app_root),
               "--port", str(port)]
    if hub_port:
        cmd += ["--hub-port", str(hub_port)]
    return cmd


def window_command(app_root: Path, hub_port: Optional[int],
                   listen_port: Optional[int] = None) -> List[str]:
    """开窗口：与 `puppethub run` 同一条命令。

    两个端口都给：`--hub-port` 让它**能说话**，`--listen-port` 让它**能被找到**
    （否则窗口实例只发不收——总线的投递需要可反向连接的端口）。
    """
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "run", str(app_root)]
    else:
        cmd = [sys.executable, "-m", "puppethub", "run", str(app_root)]
    if hub_port:
        cmd += ["--hub-port", str(hub_port)]
    if listen_port:
        cmd += ["--listen-port", str(listen_port)]
    return cmd


class Society:
    """社会层的宿主。**所有编排动作都在这里**（薄壳 UI 只调它）。"""

    def __init__(self, log=None):
        self.log = log if log is not None else (lambda *_: None)
        self._lock = threading.RLock()
        self._bus: Optional[HubBus] = None
        self._bus_port: Optional[int] = None
        self._server = None
        self._windows: Dict[str, int] = {}      # name -> pid（**本进程**开的窗口）
        # 消息观察者：社会层用它把每次投递变成调度官的刺激源。
        self.on_peer_message: Optional[Callable[[dict], None]] = None

    # ------------------------------------------------------------ 落点

    def ledger_path(self) -> Path:
        return home_dir() / LEDGER_FILE

    def bus_audit_path(self) -> Path:
        return home_dir() / BUS_FILE

    # ------------------------------------------------------------ 账本

    def ledger(self) -> dict:
        path = self.ledger_path()
        if not path.is_file():
            return {"bus_port": None, "apps": {}, "windows": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as ex:
            self.log("warning", "SOCIETY_LEDGER",
                     "账本 %s 读不出来（%s）——按空处理，**不覆盖它**" % (path, ex))
            return {"bus_port": None, "apps": {}, "windows": {}}
        if not isinstance(data, dict):
            return {"bus_port": None, "apps": {}, "windows": {}}
        for section in ("apps", "windows"):
            if not isinstance(data.get(section), dict):
                data[section] = {}
        return data

    def _save(self, data: dict) -> None:
        path = self.ledger_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except OSError as ex:
            self.log("error", "SOCIETY_LEDGER", "账本写入失败：%s" % ex)

    # ------------------------------------------------------------ 总线

    def start(self) -> dict:
        """起总线（幂等）。返回 `{port, error}`——起不来**可见**。"""
        with self._lock:
            if self._bus is not None and self._bus_port is not None:
                return {"port": self._bus_port, "error": ""}
            taken = {entry.get("port") for entry in self.ledger()["apps"].values()}
            port = int(_free_port(BUS_BASE_PORT, {p for p in taken if p}))
            bus = HubBus(lookup_port=self._lookup_port,
                         audit_path=self.bus_audit_path(),
                         on_message=self._on_bus_message)
            try:
                server = bind_bus(port)
            except OSError as ex:
                message = "总线启动失败：%s（app 将以无协作模式运行）" % ex
                self.log("error", "SOCIETY_BUS", message)
                return {"port": None, "error": message}
            threading.Thread(target=serve_bus, args=(bus, port, server),
                             daemon=True, name="society-bus").start()
            self._bus, self._bus_port, self._server = bus, port, server
            data = self.ledger()
            data["bus_port"] = port
            self._save(data)
            self.log("info", "SOCIETY_BUS", "社会层上线：总线 127.0.0.1:%d" % port)
            return {"port": port, "error": ""}

    def stop(self) -> None:
        """下线（关首页时用）。**不杀任何实例**——只关总线。"""
        with self._lock:
            server, self._server = self._server, None
            self._bus, self._bus_port = None, None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
            self.log("info", "SOCIETY_BUS", "社会层下线：总线已关闭")

    def bus_port(self) -> Optional[int]:
        if self._bus_port is None:
            return self.ledger().get("bus_port")
        return self._bus_port

    def _lookup_port(self, name: str) -> Optional[int]:
        """总线的投递目标：**优先无头实例**（它是社会的正式成员），其次窗口。"""
        data = self.ledger()
        entry = (data["apps"].get(name) or {})
        if not entry.get("port"):
            entry = (data["windows"].get(name) or {})
        return entry.get("port")

    def _on_bus_message(self, entry: dict) -> None:
        observer = self.on_peer_message
        if observer is None:
            return
        try:
            observer(entry)
        except Exception as ex:  # noqa: BLE001 - 观察者失败必须可见，但不吞总线路由
            self.log("error", "SOCIETY_OBSERVER", "消息观察者失败：%s" % ex)

    # ------------------------------------------------------------ 查询

    def rows(self) -> List[dict]:
        """账本里每个条目的**真实健康度**（TCP hello）。

        窗口实例也在账本里（它有自己的 `--listen-port`），所以 `tell`/`who`/`fire`/`call`
        对它同样成立；`mine` = 本进程开的那个。**看不见的东西不在这里**——措辞一律
        不说"没在跑"（外部手工开的窗口对我们是盲区）。
        """
        out: List[dict] = []
        data = self.ledger()
        mine = set(self._windows.values())
        for section, mode in (("apps", "remote"), ("windows", "window")):
            for name, entry in sorted(data[section].items()):
                port = entry.get("port")
                alive = bool(port) and bool(ping(int(port))["alive"])
                out.append({"name": name, "root": entry.get("root"), "port": port,
                            "pid": entry.get("pid"), "mode": mode,
                            "mine": entry.get("pid") in mine, "alive": alive})
        return out

    def bus_tail(self, limit: int = 10) -> List[dict]:
        """总线审计尾部。**没有就空**——不编造。"""
        path = self.bus_audit_path()
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

    # ------------------------------------------------------------ 编排

    def _claim(self, root: Path, data: dict) -> tuple:
        """给一个 root 定名。同名不同根 = **如实报冲突**（总线按 app 名寻址）。"""
        resolved = root.resolve()
        name = resolved.name
        for section in ("apps", "windows"):
            existing = data[section].get(name)
            if existing is None:
                continue
            other = str(existing.get("root") or "")
            if other and str(Path(other).resolve()) != str(resolved):
                return name, ("名字冲突：`%s` 已被 %s 占用——总线按 app 名寻址，"
                              "同名会互相串线；请改目录名" % (name, other))
        return name, ""

    @staticmethod
    def _emit(on_event, level: str, text: str) -> None:
        if on_event is not None:
            on_event(level, text)

    def up(self, roots, on_event=None) -> List[dict]:
        """把所有未在跑的 app 拉成**无头实例**（幂等；已在跑的不动）。"""
        startup = self.start()
        hub_port = startup.get("port")
        started: List[dict] = []
        for root in list(roots or []):
            path = Path(root)
            if not (path / SOURCE_NAME).is_file():
                self._emit(on_event, "error", "%s 不是 app 目录，已跳过" % path)
                continue
            data = self.ledger()
            name, conflict = self._claim(path, data)
            if conflict:
                self._emit(on_event, "error", conflict)
                continue
            entry = data["apps"].get(name) or {}
            if entry.get("port") and ping(int(entry["port"]))["alive"]:
                self._emit(on_event, "info", "%s 已在跑（127.0.0.1:%s），不动它"
                           % (name, entry["port"]))
                continue
            window = data["windows"].get(name) or {}
            if window.get("port") and ping(int(window["port"]))["alive"]:
                # 能检测到就**说出来**：两个写者（无头=驱动者，窗口=LLM）会同时改真源。
                # 不拦（这是已定的选择），但绝不允许它是静默的。
                self._emit(on_event, "warning",
                           "%s 正开着窗口（127.0.0.1:%s）：再拉起无头实例，这个 app 就有"
                           "**两个写者**了（真源可能被同时改）" % (name, window["port"]))
            taken = {e.get("port") for e in data["apps"].values() if e.get("port")}
            taken |= {e.get("port") for e in data["windows"].values() if e.get("port")}
            if hub_port:
                taken.add(int(hub_port))
            port = int(_free_port(PORT_BASE, {p for p in taken if p}))
            tail: deque = deque(maxlen=LOG_TAIL)
            try:
                proc = _spawn(headless_command(path.resolve(), port, hub_port), tail)
            except OSError as ex:
                self._emit(on_event, "error", "拉起 %s 失败：%s" % (name, ex))
                continue
            data["apps"][name] = {"root": str(path.resolve()), "port": port,
                                  "pid": proc.pid}
            self._save(data)
            self._emit(on_event, "info", "已拉起 %s → 127.0.0.1:%d（pid %d），等握手…"
                       % (name, port, proc.pid))
            ready = self._wait_ready(port, proc)
            if ready:
                self._emit(on_event, "info", "%s 握手成功" % name)
            else:
                tail_text = " / ".join(list(tail)[-3:]) or "（子进程没有输出）"
                self._emit(on_event, "error",
                           "%s 等不到握手（%.0fs 超时）——条目保留（它可能只是慢）；"
                           "子进程输出：%s" % (name, READY_TIMEOUT, tail_text))
            started.append({"name": name, "root": str(path.resolve()), "port": port,
                            "pid": proc.pid, "ready": ready})
        return started

    @staticmethod
    def _wait_ready(port: int, proc, timeout: float = READY_TIMEOUT) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if ping(port, timeout=1.0)["alive"]:
                return True
            if proc.poll() is not None:
                return False          # 进程已退出：再等也不通，如实上报
            time.sleep(0.3)
        return False

    def down(self, roots=None, on_event=None) -> dict:
        """按账本结束实例（`roots=None` = 全部）。pid 已死的条目 **如实列出**。"""
        data = self.ledger()
        wanted = None
        if roots:
            wanted = {str(Path(r).resolve()) for r in roots}
        stopped, dead = [], []
        for section, mode in (("apps", "无头"), ("windows", "窗口")):
            kept = {}
            for name, entry in list(data[section].items()):
                root = str(Path(entry.get("root") or ".").resolve())
                if wanted is not None and root not in wanted:
                    kept[name] = entry
                    continue
                pid = entry.get("pid")
                if self._kill(pid):
                    stopped.append({"name": name, "pid": pid,
                                    "port": entry.get("port"), "mode": mode})
                else:
                    dead.append({"name": name, "pid": pid, "mode": mode})
                self._windows.pop(name, None)
            data[section] = kept
        self._save(data)
        for item in stopped:
            self._emit(on_event, "info", "已停止 %s（pid %s，%s）"
                       % (item["name"], item["pid"], item["mode"]))
        for item in dead:
            self._emit(on_event, "warning",
                       "%s 的 pid %s 已不存在（如实列出，不装作停过）"
                       % (item["name"], item["pid"]))
        return {"stopped": stopped, "dead": dead}

    @staticmethod
    def _kill(pid) -> bool:
        if not _pid_alive(pid):
            return False
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            return False
        return True

    def open_window(self, root, on_event=None) -> dict:
        """开一个窗口子进程，并给它一个 `--listen-port`（于是它**收得到**消息）。

        **立刻退出必须如实报**（带子进程输出尾巴）；已有一个无头实例在跑时**警告**
        （两个写者）——不拦，但不静默。
        """
        path = Path(root).resolve()
        if not (path / SOURCE_NAME).is_file():
            return {"ok": False, "error": "%s 不是 app 目录（缺少 %s）"
                    % (path, SOURCE_NAME)}
        hub_port = self.start().get("port")
        data = self.ledger()
        name, conflict = self._claim(path, data)
        if conflict:
            return {"ok": False, "error": conflict}
        remote = data["apps"].get(name) or {}
        if remote.get("port") and ping(int(remote["port"]))["alive"]:
            self._emit(on_event, "warning",
                       "%s 已经在无头运行（127.0.0.1:%s）：再开窗口就有**两个写者**了"
                       "（真源可能被同时改）；只要一个就先 down 它"
                       % (name, remote["port"]))
        taken = {e.get("port") for e in data["apps"].values() if e.get("port")}
        taken |= {e.get("port") for e in data["windows"].values() if e.get("port")}
        if hub_port:
            taken.add(int(hub_port))
        listen = int(_free_port(PORT_BASE, {p for p in taken if p}))
        tail: deque = deque(maxlen=LOG_TAIL)
        try:
            proc = _spawn(window_command(path, hub_port, listen), tail)
        except OSError as ex:
            return {"ok": False, "error": "无法启动窗口：%s" % ex}
        deadline = time.time() + INSTANT_EXIT_WAIT
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.1)
        code = proc.poll()
        if code is not None:
            time.sleep(0.3)                      # 让 drain 把尾巴读出来
            return {"ok": False,
                    "error": "窗口进程立刻退出（退出码 %s）" % code,
                    "tail": "\n".join(list(tail)[-8:]) or "（子进程没有输出）"}
        data = self.ledger()                     # 重读：等了一会儿，账本可能已被别处改
        data["windows"][name] = {"root": str(path), "port": listen, "pid": proc.pid}
        self._save(data)
        self._windows[name] = proc.pid
        self._emit(on_event, "info", "已打开 %s（窗口 pid %d，监听 127.0.0.1:%d）"
                   % (name, proc.pid, listen))
        ready = self._wait_ready(listen, proc, timeout=WINDOW_READY_TIMEOUT)
        if not ready:
            self._emit(on_event, "warning",
                       "%s 的窗口还没应答（%.0fs）——它可能还在起；等它起来就会被探到"
                       % (name, WINDOW_READY_TIMEOUT))
        return {"ok": True, "pid": proc.pid, "root": str(path), "port": listen,
                "ready": ready}

    # ------------------------------------------------------------ 说话 / 调用

    def tell(self, topic: str, text: str, title: str = "",
             sender: str = "society") -> dict:
        """经总线投递一条**刺激**（按 topic 路由；回执如实报谁收到、谁没收到）。"""
        if self._bus is None:
            return {"ok": False, "error": "社会层总线未上线（没有 hub_port 可注入）"}
        return self._bus.post(sender, topic, text, title=title)

    def request(self, name_or_root, payload: dict, timeout: float = 3.0) -> dict:
        """对一个**无头实例**发一次协议请求（`who` / `fire` / `call` 共用）。"""
        entry = self._entry_of(name_or_root)
        if entry is None:
            return {"ok": False, "error": "%s 不在账本里（先 up 它）" % name_or_root}
        port = entry.get("port")
        if not port:
            return {"ok": False,
                    "error": "%s 在账本里没有端口（它的监听没起来——看它自己的观察流）"
                             % name_or_root}
        reply = _tcp_request(int(port), payload, timeout)
        return dict(reply or {}, ok=bool((reply or {}).get("ok", "error" not in (reply or {}))))

    def known(self, token) -> Optional[dict]:
        """按名字或路径找一个**账本里**的实例。

        为什么要它：账本才是"什么在跑"的真相——一个 app 可能**从没进过 MRU**
        就被编排起来了（别的进程 `up` 的、`hub` 拉起的）。只按 MRU 解析会让
        `who`/`fire`/`call` 对这种实例说"不认识"，那是拿列表当了真相。
        """
        entry = self._entry_of(token)
        if entry is None:
            return None
        root = str(Path(entry.get("root") or "").resolve())
        name = Path(root).name or str(token)
        return {"root": root, "name": name, "mark": "",
                "exists": Path(root).is_dir(),
                "is_app": (Path(root) / SOURCE_NAME).is_file()}

    def _entry_of(self, token) -> Optional[dict]:
        """按名字或路径找一个被编排的实例（**优先无头**——总线投递同一条优先级）。"""
        data = self.ledger()
        text = str(token or "")
        for section in ("apps", "windows"):
            table = data[section]
            if text in table:
                return table[text]
            for entry in table.values():
                if str(entry.get("root")) == text:
                    return entry
        return None


class SocietyOps:
    """调度官能拿到的**全部**能力（白名单）。

    这里**没有 `send`、没有 `load`、没有任何能改 app 真源的入口**——不是靠提示词
    自律，是**代码里不可达**（与"插件不拿引擎句柄、只产出值"同一条设计律）。
    `docs/smoke-home.py` 用 `inspect` 断言这条边界，否则它会悄悄长回来。

    **每个方法都返回 dict**，并且**拒绝要如实**（对端拒绝 / 连不上 / 不在账本里），
    绝不把失败说成成功——拒绝本身就是回灌给 LLM 的诊断。
    """

    VERBS = ("ls", "status", "bus", "who", "check", "open", "up", "down",
             "fire", "call", "tell", "new", "forget")

    # 自主时**默认允许**的动词：只读 + 消息刺激（其余默认拒绝，需人手动进 allow）
    FREE_WHEN_AUTONOMOUS = ("ls", "status", "bus", "who", "check", "tell")

    # 黑名单：**刻意不实现**。留成常量只为让"边界是什么"可以被测试断言。
    FORBIDDEN = ("send", "load")

    def __init__(self, society: Society, *, log=None, config: Optional[dict] = None):
        self.society = society
        self.log = log if log is not None else (lambda *_: None)
        self.config = dict(config or {})

    def _note(self, level: str, code: str, message: str) -> None:
        self.log(level, code, message)

    # ------------------------------------------------------------ 解析

    def resolve(self, token) -> dict:
        """把 `<app>` 解析成一个条目：**先 MRU，再账本**（账本才是"什么在跑"）。

        歧义与找不到都如实说，不猜。
        """
        text = str(token or "").strip()
        if not text:
            return {"error": "缺少 app 名"}
        known = [recent.state_of(item["root"]) for item in recent.entries()]
        by_name = [item for item in known if item["name"] == text]
        if not by_name:
            by_name = [item for item in known if item["root"] == str(Path(text).resolve())]
        if not by_name and Path(text).is_dir():
            by_name = [recent.state_of(text)]
        if len(by_name) > 1:
            return {"error": "`%s` 有 %d 个同名项目：%s——用完整路径指定"
                    % (text, len(by_name), "、".join(i["root"] for i in by_name))}
        if by_name:
            return by_name[0]
        # 没在列表里？看看它是不是**正在跑**（从没进过 MRU 的实例也算认识）
        found = self.society.known(text)
        if found is not None:
            return found
        names = "、".join(sorted(item["name"] for item in known)) or "（列表是空的）"
        return {"error": "不认识 `%s`：项目列表与账本里都没有它（列表现有：%s）"
                % (text, names)}

    # ------------------------------------------------------------ 只读

    def ls(self) -> dict:
        return {"ok": True, "items": [recent.state_of(item["root"])
                                      for item in recent.entries()],
                "problems": recent.problems()}

    def status(self) -> dict:
        return {"ok": True, "rows": self.society.rows(),
                "bus_port": self.society.bus_port()}

    def bus(self, limit: int = 10) -> dict:
        return {"ok": True, "entries": self.society.bus_tail(int(limit or 10))}

    def who(self, app: str) -> dict:
        found = self.resolve(app)
        if "error" in found:
            return found
        reply = self.society.request(found["name"], {"op": "hello"})
        if reply.get("ok") is False:
            return {"ok": False, "error": reply.get("error")}
        return {"ok": True, "name": found["name"], "hello": reply}

    def check(self) -> dict:
        """只读探活（先 `/models` 再退化 1-token 对话）——模型通不通。"""
        from . import keys
        settings = self.provider_settings()
        if settings.get("error"):
            return settings
        result = keys.probe(settings)
        return {"ok": bool(result.get("ok")), "message": result.get("message"),
                "detail": result}

    def provider_settings(self) -> dict:
        """调度官当前生效的 provider 设置（**不含明文**时会由 secrets 解析）。"""
        from . import secrets
        options = self.config.get("plugins", {}).get("openai-compat", {}) or {}
        key_env = str(options.get("key_env") or "OPENAI_API_KEY")
        key, source = secrets.resolve(key_env)
        return {"base_url": options.get("base_url"), "model": options.get("model"),
                "key_env": key_env, "key": key, "key_source": source,
                "error": "" if options.get("model") else
                         "没有配置模型：在 %s 里写 [llm] profile 或 [plugins.openai-compat]"
                         % (secrets.home_dir() / "orchestrator.toml")}

    # ------------------------------------------------------------ 生命周期

    def open(self, app: str) -> dict:
        found = self.resolve(app)
        if "error" in found:
            return found
        result = self.society.open_window(found["root"])
        return result

    def up(self, apps=None) -> dict:
        """`up`（不带参数）= 拉起列表里所有 app。"""
        if apps:
            roots = []
            for token in apps:
                found = self.resolve(token)
                if "error" in found:
                    return found
                roots.append(found["root"])
        else:
            roots = [item["root"] for item in recent.entries()]
        if not roots:
            return {"ok": True, "started": [], "note": "项目列表是空的——没有可拉起的 app"}
        started = self.society.up(roots)
        return {"ok": True, "started": started}

    def down(self, apps=None) -> dict:
        if apps:
            roots = []
            for token in apps:
                found = self.resolve(token)
                if "error" in found:
                    return found
                roots.append(found["root"])
        else:
            roots = None
        result = self.society.down(roots)
        return {"ok": True, **result}

    # ------------------------------------------------------------ 交互 / 调用

    def fire(self, app: str, target: str, event: str, row=None, value=None) -> dict:
        """触发**对方自己声明的** handler——等价于"在它的窗口里点了一下"。"""
        found = self.resolve(app)
        if "error" in found:
            return found
        if not target or not event:
            return {"ok": False, "error": "`fire` 需要 `<目标> <事件>`（如 `#btn click`）"}
        reply = self.society.request(found["name"],
                                     {"op": "fire", "target": target, "event": event,
                                      "row": row, "value": value})
        if reply.pop("ok", True) is False:
            return {"ok": False, "error": reply.get("error") or "触发失败"}
        diagnostics = reply.get("diagnostics") or []
        errors = [d.get("message") for d in diagnostics
                  if isinstance(d, dict) and d.get("level") == "error"]
        return {"ok": not errors, "diagnostics": diagnostics,
                "error": "；".join(errors) if errors else ""}

    def call(self, app: str, name: str, args=None) -> dict:
        """借出能力调用。**调用权在被调方**（`[service] lend` 缺省空 = 默认拒绝）。"""
        found = self.resolve(app)
        if "error" in found:
            return found
        if not name:
            return {"ok": False, "error": "`call` 需要能力名"}
        reply = self.society.request(found["name"],
                                     {"op": "call", "name": name, "args": args or {}},
                                     timeout=float(self.config.get("call_timeout", 30)))
        if reply.get("ok") is False:
            return {"ok": False, "error": reply.get("error")}
        return {"ok": True, "value": reply.get("value")}

    def tell(self, app: str, topic: str, text: str) -> dict:
        """经总线发消息（**刺激，不写入**）。路由按 topic——回执如实报谁收到。"""
        if not topic:
            return {"ok": False, "error": "`tell` 需要 `<topic>`"}
        reply = self.society.tell(topic, text, sender="society")
        if reply.get("ok") is False:
            return {"ok": False, "error": reply.get("error")}
        delivered = reply.get("delivered") or []
        failed = reply.get("failed") or []
        return {"ok": True, "delivered": delivered, "failed": failed,
                "note": "投递给 topic=%s 的订阅者；`%s` 只是意图标注"
                        % (topic, app)}

    # ------------------------------------------------------------ 磁盘

    def new(self, name: str, parent: str = "", title: str = "") -> dict:
        """生成最小骨架 + **静态校验**（骨架零诊断是它的契约）——照 `GuiTools` 同一条。"""
        from .appdir import create_app
        from .fusion import _dry_run
        if not name or "/" in name or "\\" in name:
            return {"ok": False, "error": "`new` 需要一个目录名（不含路径分隔符）"}
        parent_dir = parent or os.getcwd()
        try:
            app = create_app(Path(parent_dir), name, title or None)
        except (FileExistsError, OSError) as ex:
            return {"ok": False, "error": "创建失败：%s" % ex}
        errors = _dry_run(app.read_source())
        return {"ok": not errors, "root": str(app.root),
                "errors": [{"level": d.level, "code": d.code, "message": d.message}
                           for d in errors],
                "note": "下一步：`open %s`" % app.name}

    def forget(self, app: str) -> dict:
        found = self.resolve(app)
        if "error" in found:
            return found
        removed = recent.forget(found["root"])
        return {"ok": removed, "root": found["root"],
                "note": "" if removed else "列表里本来就没有它"}
