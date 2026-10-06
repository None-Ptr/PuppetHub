"""plan 块：把 `goal`（一句方向）展开成**可核验的步骤序列**。

`discuss-sensing.md` §7 与 `review-agent-gap.md` §2.4 指出：`goal` 只是字符串，
自主每步只做"最小的一步"，**看不到通向目标的路径**。本模块补这一格。

## 最重要的一条：完成判据必须机制可观测

一个"LLM 写步骤、LLM 自己勾完成"的 plan 是**自我报告装置**——它会一律勾"完成"，
那是自我肯定，不是进展。所以每步可带 `done:` **受限比较判据**（复用 `watch` 的
`#地址.属性 OP 字面量`），由**宿主求值**决定它是否完成：

- 有 `done:` 且求值为真 → 机制认定完成（`PLAN_STEP_DONE`）
- 有 `done:` 但求值为假 → **未完成**，且要**说出当前值**（不是"没做到"，是"现在是多少"）
- 没有 `done:` → **不判完成**（诚实：这条只能由人或后续机制确认，不假装）

判据用不了（节点不存在 / 非标量）→ `PLAN_EVAL` 可见告警，**不退化成"算完成"**。

## 不变量
- **plan 不增预算**：只决定"做什么"，不决定"能做多少"（预算池仍归 `steps_per_hour`）。
- **写 plan 仍是当值写者的动作**（readonly 分支已挡非当值者），照 `goal`/`watch` 的路。
- **一次只推进一步**：本模块只提供"当前该做哪一步"的判定，不做调度器。
- 步骤**可撤销**：`drop:` 删掉做不了的步骤（记流水，不静默消失）。
"""

from __future__ import annotations

import re

from . import watch as _watch

MAX_STEPS = 12           # 步骤上限：plan 是"一条路径"，不是待办清单
MAX_GOAL_CHARS = 400


def parse_block(text: str):
    """解析 ```plan 块 → (plan, diag)。diag 非 None = `PLAN_INVALID`。

    块形（行式 `key: value`，与 watch 同风格）::

        goal: 让待办能归档
        step: 给 #list 加归档按钮 | done: #arch.visible == true
        step: 归档后从列表移除 | done: #list.count == 2
        drop: 做不了的步骤
    """
    fields: dict = {"steps": [], "drop": []}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            return None, {"code": "PLAN_INVALID", "level": "error",
                          "message": "plan 块每行应是「key: value」，看不懂：%r" % line}
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "goal":
            fields["goal"] = value
        elif key in ("step", "drop"):
            bucket = "steps" if key == "step" else "drop"
            # 判据写法：`... | done: #a.b < 1`。注意 `done:` 标签要被剥掉——
            # 上面的 partition(':') 只切了第一个冒号，剩下 `done: 判据` 整段在这里拆。
            what, sep, done = value.partition("|")
            what = what.strip()
            done = done.strip()
            if sep and done.lower().startswith("done:"):
                done = done[len("done:"):].strip()
            elif sep:
                return None, {"code": "PLAN_INVALID", "level": "error",
                              "message": "第 %d 步的判据前缺 `done:`（写法是 "
                                         "`... | done: #地址.属性 比较符 字面量`）"
                                         % (len(fields["steps"]) + 1)}
            if not what:
                return None, {"code": "PLAN_INVALID", "level": "error",
                              "message": "%s 行没写内容" % key}
            if done:
                # 判据只接受受限比较——复杂判断写进能力（与 watch 同一哲学）。
                try:
                    _watch.parse_when(done)
                except _watch.WatchError as ex:
                    return None, {"code": "PLAN_INVALID", "level": "error",
                                  "message": "第 %d 步的 done 判据非法：%s"
                                             % (len(fields["steps"]) + 1, ex)}
            fields[bucket].append({"what": what, "done": done or ""})
        elif key in ("step", "drop", "goal"):
            pass
        else:
            return None, {"code": "PLAN_INVALID", "level": "error",
                          "message": "plan 块不认识字段 %r（可用：goal / step / drop）" % key}

    goal = fields.get("goal", "")
    steps = fields["steps"]
    if not goal and not steps and not fields["drop"]:
        return None, {"code": "PLAN_INVALID", "level": "error",
                      "message": "plan 块是空的（至少要有 goal 或一条 step）"}
    if not steps and not fields["drop"]:
        return None, {"code": "PLAN_INVALID", "level": "error",
                      "message": "plan 只有 goal 没有 step——那不是计划，是许愿"}
    if len(steps) > MAX_STEPS:
        return None, {"code": "PLAN_REJECTED", "level": "error",
                      "message": "plan 有 %d 步，超过上限 %d——拆成几个 plan，"
                                 "或者先想清楚哪几步真必要" % (len(steps), MAX_STEPS)}
    if len(goal) > MAX_GOAL_CHARS:
        return None, {"code": "PLAN_REJECTED", "level": "error",
                      "message": "goal 超过 %d 字" % MAX_GOAL_CHARS}
    return {"goal": goal[:MAX_GOAL_CHARS], "steps": steps, "drop": fields["drop"]}, None


# ------------------------------------------------------------------ 执行

def plan_id(goal: str) -> str:
    """计划 id：同 goal 即同计划（重设即替换，不叠加）。"""
    import hashlib
    return "p" + hashlib.md5((goal or "").encode("utf-8")).hexdigest()[:8]


def evaluate(steps: list, attrs: dict) -> tuple:
    """逐条求值步骤判据 → (完成下标集合, 描述行列表, 告警列表)。

    `attrs` = `observe()` 快照的属性表（与 watch 同一数据源、**不另起 observe**）。
    告警 = 判据用不了（节点不存在 / 非标量）——**必须可见，不退化成"算完成"**。
    """
    done, lines, warns = set(), [], []
    for index, step in enumerate(steps):
        what = step.get("what", "")
        expr = step.get("done") or ""
        if not expr:
            # 诚实：没有判据 = 无法自动核验。**不假装完成，也不假装失败**。
            lines.append((index, what, "无判据（需人或后续机制确认）", None))
            continue
        try:
            nid, attr, op, literal = _watch.parse_when(expr)
        except _watch.WatchError as ex:
            warns.append("第 %d 步判据不可用：%s" % (index + 1, ex))
            lines.append((index, what, "判据不可用（见告警）", None))
            continue
        key = "#%s.%s" % (nid, attr)
        if key not in attrs:
            warns.append("第 %d 步判据取不到 %s（节点/属性不存在或不可序列化）"
                         % (index + 1, key))
            lines.append((index, what, "判据取不到 %s" % key, None))
            continue
        value = attrs[key]
        try:
            ok = _watch.compare(value, op, literal)
        except Exception as ex:  # noqa: BLE001 - 求值炸了要说，不当成"完成"
            warns.append("第 %d 步判据求值出错：%s" % (index + 1, ex))
            lines.append((index, what, "判据求值出错", None))
            continue
        if ok:
            done.add(index)
            lines.append((index, what, "已达成（%s %s %s）" % (key, op, literal), value))
        else:
            lines.append((index, what, "未达成（当前 %s = %s，需 %s %s）"
                          % (key, _brief(value), op, literal), value))
    return done, lines, warns


def current_step(lines: list, done: set) -> Optional[dict]:
    """当前该做的那一步 = **第一条未达成**的。返回 None 表示全达成或无可做。"""
    for index, what, status, value in lines:
        if index not in done:
            return {"index": index, "what": what, "status": status, "value": value}
    return None


def _brief(value, limit: int = 30) -> str:
    try:
        import json
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = repr(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def plan_block(plan: dict, lines: list, done: set) -> str:
    """给下一轮注入的「当前计划」块。空计划 → 空串（不注废话）。"""
    steps = (plan or {}).get("steps") or []
    if not steps:
        return ""
    finished = len(done & set(range(len(steps))))
    rows = []
    for index, what, status, _value in lines:
        mark = "x" if index in done else " "
        rows.append("  [%s] %d. %s —— %s" % (mark, index + 1, what, status))
    nxt = current_step(lines, done)
    tail = ("\n**当前该做的是第 %d 步**（先做它，别跳着做）：%s"
            % (nxt["index"] + 1, nxt["what"])) if nxt else \
           "\n**所有带判据的步骤都已达成**——别再做多余的事，考虑收尾或重定目标。"
    return ("【当前计划（%d/%d 已由机制核验达成）】%s\n%s%s"
            % (finished, len(steps),
               ("目标：%s" % plan["goal"]) if plan.get("goal") else "", "\n".join(rows), tail))
