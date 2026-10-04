"""**人的写入**：整份替换真源（origin=driver）——CLI `edit` 与驾驶舱「人的写入」
共用这一份实现。

它不绕过单写者，绕过的是"写者必须是 LLM"：这里写者是人自己，走的还是同一条
纪律链——**干跑校验 → 确认 → 兜底快照 → 整份替换 → 重载 → 决策流水**。

两条不许省的东西：
- **干跑在前**：编辑器/输入框里写出的东西必须先证明是合法程序，否则人会以为
  自己改成功了。校验不过 → **真源恢复原样，拒稿留在 `*.rejected`**（人的工作
  不能丢，真源也不能坏，两头都要说清楚）。
- **兜底快照**：人的整份替换与"推倒重来"同级，可回滚是它的前提。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional  # noqa: F401 - restore_to 的类型标注

REJECTED_SUFFIX = ".puppet.rejected"


def diff_summary(before: list, after: list) -> dict:
    """给人看的差异摘要：行数变化 + 前几处不同（不猜语义，只报事实）。"""
    before_set = set(line for line in before if line.strip())
    after_set = set(line for line in after if line.strip())
    added = [line for line in after if line.strip() and line not in before_set]
    removed = [line for line in before if line.strip() and line not in after_set]
    return {"before_lines": len(before), "after_lines": len(after),
            "added": added[:5], "removed": removed[:5],
            "fresh": added[:12]}


def validate_text(text: str) -> list:
    """干跑：解析 + IR 校验 + 执行路径干跑（与融合同一判据）。"""
    from .fusion import _dry_run
    return _dry_run(str(text).splitlines())


def apply_text(app, session, text: str, note: str = "",
               restore_to: Optional[list] = None) -> dict:
    """把 `text` 作为新真源整份替换。**调用方负责拿到人的确认**。

    `restore_to` = 拒绝采用时要恢复到的基线行（缺省 = 现读真源）。

    **编辑器路径必须显式传它**：那条路上真源已经被编辑器写脏了，"恢复原样"
    再现读只会把脏内容写回去——基线只能由"动真源之前"的人给（实测踩过：
    `smoke-edit` 第 3 节逮住）。

    返回 `{"ok", "stage", "errors", "rejected", "diagnostics"}`——每一步都
    说得清停在哪，不吞。
    """
    after_lines = str(text).splitlines()
    before_lines = list(restore_to) if restore_to is not None else app.read_source()
    errors = validate_text("\n".join(after_lines))
    if errors:
        rejected = app.source_path.with_suffix(REJECTED_SUFFIX)
        rejected.write_text("\n".join(after_lines).rstrip("\n") + "\n", encoding="utf-8")
        app.write_source(before_lines, origin="system")
        session.note("error", "EDIT_REJECTED",
                     "编辑后的程序没通过静态校验（%d 项），真源已恢复原样；"
                     "拒稿留在 %s" % (len(errors), rejected.name))
        return {"ok": False, "stage": "dry-run", "rejected": str(rejected),
                "errors": [{"level": d.level, "code": d.code, "message": d.message}
                           for d in errors], "diagnostics": []}
    app.push_snapshot("rebuild", "手写程序模式编辑前兜底存档", "driver")
    diags = session.load_source(after_lines, origin="driver")
    app.append_decision("手写程序模式编辑真源",
                        note or "整份替换（人确认），%d 行" % len(after_lines), "全程序")
    session.note("info", "EDIT_APPLIED",
                 "真源已整份替换并重载（%d 行；写前有兜底快照，可回滚）" % len(after_lines))
    return {"ok": True, "stage": "applied", "rejected": None,
            "errors": [], "diagnostics": diags}


def rejected_path(app) -> Optional[Path]:
    path = app.source_path.with_suffix(REJECTED_SUFFIX)
    return path if path.is_file() else None
