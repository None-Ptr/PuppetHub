"""watch 块的执行者：把 LLM 声明的定时/阈值看门狗接进自主回路。

设计见 `docs/discuss-sensing.md` §4。三条不变量：

- **watch 只当看门狗**：`when` 只支持 `#地址.属性 比较符 字面量` 的简单比较，
  复杂判断交给能力（`when` 只盯能力结果）。引擎没有公开的表达式求值器，
  所以 `when` 不写成小号程序。
- **边界可见**：总数 ≤ 8 · 最小间隔 ≥ 30s · 预算池不变（只决定"何时"不决定"多少"）。
  超限 `WATCH_REJECTED`，语法错 `WATCH_INVALID`——**绝不静默丢弃**（第一原则）。
- **触发只走 `autonomous.step()`**：与既有事件源同一条路，同样计入预算、同样审计、
  同样防回声（busy / 超预算时由 `step` 自己可见拒绝）。

求值数据源 = `observe()` 快照里的 `attrs`（每帧都重算，是"到手的快照"的一部分，
不另起 `observe` 以免抢走诊断）。`#地址.属性` 的键与 `attrs` 完全一致。
"""

from __future__ import annotations

import hashlib
import re
import threading

MAX_WATCHES = 8           # watch 总数上限：防"加 100 个定时器"
MIN_INTERVAL = 30         # 最小间隔（秒）：防 every:1s 烧光步进预算

_WHEN_RE = re.compile(
    r"^#?([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*(==|!=|<=|>=|<|>)\s*(.+?)\s*$")
_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


class WatchError(Exception):
    """watch 配置/求值期的可预期错误（对应 WATCH_INVALID / WATCH_EVAL）。"""


# ------------------------------------------------------------------ 解析

def parse_duration(text: str) -> int:
    """`5m` → 300；`30s` → 30；`1h` → 3600。非法 → WatchError。"""
    text = (text or "").strip().lower()
    if not text:
        raise WatchError("every 为空")
    m = re.fullmatch(r"(\d+)\s*(s|m|h)", text)
    if not m:
        raise WatchError("every 格式不认识（要「数字+单位」，如 5m / 30s / 1h）")
    n = int(m.group(1))
    secs = n * {"s": 1, "m": 60, "h": 3600}[m.group(2)]
    if secs < 1:
        raise WatchError("every 必须 ≥ 1 秒")
    return secs


def parse_literal(text: str):
    """`when` 右侧字面量：bool / null / 数字 / 引号串 / 裸词串。"""
    t = (text or "").strip()
    low = t.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none", "nil"):
        return None
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        return t[1:-1]
    if _INT_RE.match(t):
        return int(t)
    if _FLOAT_RE.match(t):
        return float(t)
    return t


def parse_when(text: str):
    """解析 `when`：返回 (nid, attr, op, literal)。形式不对 → WatchError。

    只接受「#地址.属性 比较符 字面量」——右值必须是字面量，不能是属性引用或表达式
    （那是复杂判断，应交由能力，watch 只盯能力结果）。
    """
    text = (text or "").strip()
    if not text:
        raise WatchError("when 为空")
    m = _WHEN_RE.match(text)
    if not m:
        raise WatchError(
            "when 只支持「#地址.属性 比较符 字面量」，例如 #stock.value < 10；"
            "复杂判断请写进能力，再让 watch 盯能力结果")
    nid, attr, op, lit = m.group(1), m.group(2), m.group(3), m.group(4)
    # 复杂条件：右值是属性引用（两个地址比较）——只支持与字面量比较
    if re.fullmatch(r"#?[A-Za-z_]\w*\.[A-Za-z_]\w*", lit):
        raise WatchError(
            "when 的右值不能是一个属性引用（%s）；只支持与字面量比较，"
            "复杂判断请写进能力，再让 watch 盯能力结果" % lit)
    # 复杂条件：右值是表达式（带算术/连接运算符）
    body = lit[1:] if lit[:1] in "+-" else lit
    if re.search(r"[+\-*/%]", body):
        raise WatchError(
            "when 的右值只能是字面量（数字 / 字符串 / true / false / null），"
            "不能写表达式（%s）；复杂判断请写进能力" % lit)
    return nid, attr, op, parse_literal(lit)


def _coerce(left, right):
    """为一次比较选公共类型：都是数→数；否则都转 str（避免 Python 不可比异常）。

    阈值常见形态是「字符串里的数字 vs 数字字面量」（如 `#count.text < 10`）——
    走 float 强制转换即可正确比较；类型彻底不可比时退回字符串比较。
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return (str(left), str(right))
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return (left, right)
    try:
        return (float(left), float(right))
    except (TypeError, ValueError):
        return (str(left), str(right))


def compare(left, op, right) -> bool:
    a, b = _coerce(left, right)
    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    try:
        if op == "<":
            return a < b
        if op == "<=":
            return a <= b
        if op == ">":
            return a > b
        if op == ">=":
            return a >= b
    except TypeError:
        return False
    return False


def parse_block(text: str):
    """解析 ```watch 块正文（行式 `key: value`）→ (fields, diag)。

    diag 非 None 表示非法配置，应转成 `WATCH_INVALID` 返回给声明者。
    """
    fields: dict = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            return None, {"code": "WATCH_INVALID", "level": "error",
                          "message": "watch 块每行应是「key: value」，看不懂：%r" % line}
        key, val = line.split(":", 1)
        fields[key.strip().lower()] = val.strip()

    every_raw = fields.get("every")
    when = fields.get("when")
    why = fields.get("why", "")
    if not every_raw and not when:
        return None, {"code": "WATCH_INVALID", "level": "error",
                      "message": "watch 块需要 every 或 when 至少一个"}
    every_sec = None
    if every_raw:
        try:
            every_sec = parse_duration(every_raw)
        except WatchError as ex:
            return None, {"code": "WATCH_INVALID", "level": "error",
                          "message": "every 非法：%s" % ex}
    if when:
        try:
            parse_when(when)
        except WatchError as ex:
            return None, {"code": "WATCH_INVALID", "level": "error",
                          "message": str(ex)}
    return {"every_raw": every_raw, "every_sec": every_sec,
            "when": when, "why": why}, None


def watch_id(every_raw, when, why=None) -> str:
    """稳定 id：相同触发条件（every + when）→ 相同 id，重设即更新，不占新槽位。

    `why` 只是人类备注，不参与身份——改 why 不算新 watch。
    """
    seed = "%s|%s" % (every_raw or "", when or "")
    return "w" + hashlib.md5(seed.encode("utf-8")).hexdigest()[:8]


# ------------------------------------------------------------------ 执行者

class Watcher:
    """宿主侧看门狗：every 用定时器，when 在每次 refresh 后求值（边沿触发）。"""

    def __init__(self, session):
        self.session = session
        self.watches: list = []
        self._by_id: dict = {}
        self._timers: dict = {}
        self._last: dict = {}          # id -> 上次 when 求值结果（边沿检测）
        self._warned: set = set()      # 求值失败的去重告警（避免每帧刷屏）
        self._last_attrs: dict = {}    # 最近一次快照的 attrs（定时器路径用）
        self._lock = threading.Lock()

    # -------------------------------------------------------- 装载 / 装配

    def load(self) -> None:
        """从 storage 读 watches.json 并武装定时器。重装载会取消旧定时器。"""
        with self._lock:
            self._stop_timers_locked()
            raw = []
            try:
                raw = self.session.storage.read_json(
                    ".puppethub/watches.json", []) or []
            except Exception:            # 读不出当没有——但落点可见性由 set_watch 负责
                raw = []
            self.watches = [w for w in (raw or []) if isinstance(w, dict)]
            self._by_id = {w.get("id"): w for w in self.watches
                           if w.get("id") is not None}
            self._last = {}
            self._warned = set()
            for w in self.watches:
                self._arm_locked(w)

    def _stop_timers_locked(self) -> None:
        for timer in self._timers.values():
            try:
                timer.cancel()
            except Exception:           # noqa: BLE001 - 取消失败不致命
                pass
        self._timers = {}

    def _arm_locked(self, entry) -> None:
        secs = entry.get("every_sec")
        if not secs:
            return
        wid = entry.get("id")
        timer = threading.Timer(secs, self._tick, args=(wid,))
        timer.daemon = True
        self._timers[wid] = timer
        timer.start()

    def stop(self) -> None:
        """卸下所有定时器（插件热重载时用）。"""
        with self._lock:
            self._stop_timers_locked()

    # -------------------------------------------------------- 求值入口

    def note_snapshot(self, snap: dict) -> None:
        """由 session 在每帧 observe 后调用：缓存最新 attrs 供定时器路径使用。"""
        self._last_attrs = snap.get("attrs") or {}

    def evaluate_conditions(self, attrs: dict) -> None:
        """when-only 看门狗：每次 refresh 后跑一次，做上升沿检测。

        多个 watch 同帧同时触发 → 合并成一次唤醒（防连点式唤醒风暴）。
        """
        if self.session.writer != "autonomous" or self.session.autonomous is None:
            return                      # 人在驱动时 agent 不插嘴
        with self._lock:
            targets = [w for w in self.watches
                       if w.get("when") and not w.get("every_sec")]
        fired = [w for w in targets if self._eval_edge(w, attrs)]
        if fired:
            self._fire(fired)

    def _tick(self, wid) -> None:
        """定时器到点：先重新武装，再检查（有 when 就上升沿，没 when 直接触发）。

        写者不是自主时只武装不触发——定时器继续走，但绝不白烧预算。
        """
        with self._lock:
            entry = self._by_id.get(wid)
            if entry is not None:
                self._arm_locked(entry)
        if self.session.writer != "autonomous" or self.session.autonomous is None:
            return
        entry = self._by_id.get(wid)
        if entry is None:
            return
        if entry.get("when"):
            if not self._eval_edge(entry, self._last_attrs):
                return
        self._fire([entry])

    def _eval_edge(self, entry, attrs) -> bool:
        """对单个 watch 求值；只在 false→true 上升沿返回 True。

        首评建基线（不触发）：避免"重启时条件本来就为真"立刻炸一次唤醒。
        """
        try:
            ok = self._eval_when(entry, attrs)
        except WatchError as ex:
            self._warn_once(entry.get("id"), str(ex))
            return False
        wid = entry.get("id")
        prev = self._last.get(wid)
        self._last[wid] = ok
        if prev is None:
            return False
        return (not prev) and ok

    def _eval_when(self, entry, attrs) -> bool:
        nid, attr, op, lit = parse_when(entry["when"])
        key = "#%s.%s" % (nid, attr)
        if key not in attrs:
            raise WatchError("快照里没有 %s（节点/属性未渲染或不可序列化）" % key)
        return compare(attrs[key], op, lit)

    def _warn_once(self, wid, msg) -> None:
        with self._lock:
            if wid in self._warned:
                return
            self._warned.add(wid)
        self.session.note("warning", "WATCH_EVAL",
                          "watch %s 求值失败：%s" % (wid, msg))

    def _fire(self, entries: list) -> None:
        """把攒下的触发合成一条 trigger，走 `autonomous.step`（预算/熔断/审计全复用）。"""
        if not entries:
            return
        if len(entries) == 1:
            e = entries[0]
            head = ("阈值：%s" % e["when"]) if e.get("when") else "定时到点"
            why = e.get("why") or e.get("when") or "定时"
            trigger = "【watch】%s（%s）" % (head, why)
        else:
            parts = []
            for e in entries:
                head = ("阈值：%s" % e["when"]) if e.get("when") else "定时到点"
                parts.append(head + (("（%s）" % e["why"]) if e.get("why") else ""))
            trigger = "【watch】多处同时触发：%s" % "；".join(parts)
        runner = self.session.autonomous
        threading.Thread(target=runner.step, args=(trigger,), daemon=True).start()
