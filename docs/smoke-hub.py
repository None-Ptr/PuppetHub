"""多 app 编排冒烟：发现 / 拉起 / 健康检查 / 停止。

要验证的不是"命令能跑"，是编排的**诚实性**：
- 幂等：已在跑的不重启；
- 健康检查 = 能应答 hello，不是"进程表里有 pid"；
- down 对已死条目如实列出，不装作停过；
- 账本外新出现的 app 如实标"未编排"。

用法：python docs/smoke-hub.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from puppethub.appdir import create_app
    from puppethub import hub

    work = Path(tempfile.mkdtemp(prefix="puppethub-hub-"))
    # 注意：create_app(parent, name) 的 app 目录 = parent/name —— 传 work 而不是 work/xxx。
    create_app(work, "alpha", "甲")
    create_app(work, "beta", "乙")
    (work / "not-an-app").mkdir()          # 干扰项：目录在，但没有 app.puppet

    failures = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print("  [%s] %s%s" % ("ok" if ok else "失败", label,
                               ("  ← " + detail) if detail and not ok else ""))
        if not ok:
            failures.append(label)

    print("1) 发现：零猜测（干扰项不算）")
    apps = hub.discover(work)
    check("只认出两个 app", [a.name for a in apps] == ["alpha", "beta"],
          str([a.name for a in apps]))

    print("\n2) 拉起：真实子进程 + 端口入账 + **等握手**（不等握手=假成功）")
    started = hub.up(work, base_port=8890)
    check("两个都拉起", {item["name"] for item in started} == {"alpha", "beta"},
          str(started))
    check("两个都等到握手", all(item["ready"] for item in started), str(started))
    rows = hub.status(work)
    check("健康检查=能应答 hello",
          all(row["alive"] is True for row in rows), str(rows))

    print("\n3) 幂等：已在跑的不重启")
    again = hub.up(work, base_port=8890)
    check("二次 up 拉起 0 个", again == [], str(again))

    print("\n4) 停止：账本清空；旧端口真的关了；对已死条目如实列出")
    stopped, dead = hub.down(work)
    check("两个都停了", {item["name"] for item in stopped} == {"alpha", "beta"},
          str((stopped, dead)))
    time.sleep(1.0)
    check("旧端口真的关了（进程死了，不是账本删了就算）",
          all(not hub.ping(item["port"])["alive"] for item in stopped),
          str([hub.ping(item["port"]) for item in stopped]))
    rows = hub.status(work)
    check("停止后未编排",
          all(row["managed"] is False for row in rows), str(rows))

    print("\n5) 账本外新出现的 app：如实标未编排")
    create_app(work, "gamma", "丙")
    rows = hub.status(work)
    gamma = next(row for row in rows if row["name"] == "gamma")
    check("gamma 未编排可见", gamma["managed"] is False and gamma["alive"] is None,
          str(gamma))

    shutil.rmtree(work, ignore_errors=True)
    print("\n%s（%d 项失败）" % ("全部通过" if not failures else "有失败", len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
