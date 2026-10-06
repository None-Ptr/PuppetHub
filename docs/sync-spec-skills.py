"""从语言仓的 spec 提取知识，生成 PuppetHub 的内置技能（`builtin_skills/`）。

**为什么是"生成"而不是手抄**：`spec/*.md` 是语言的**权威规范**（住在语言仓）；
Hub 里的技能是它面向 LLM 的**可注入副本**。手抄必然漂移（语言仓一改，Hub 就过期），
所以副本由脚本产出 + 文件头标注来源与语言版本。语言仓动了 → 重跑本脚本即可。

    python docs/sync-spec-skills.py            # 生成
    python docs/sync-spec-skills.py --check    # 只校验（CI 用：副本是否已过期）

产出两份（都面向"写 puppet 时真正要查的东西"，而非照搬规范原文）：

- `诊断速查.md` ← `spec/06-diagnostics.md` 第 4 节码表（出错后怎么修）
- `控件语义.md` ← `spec/04-vocabulary.md` 第 2 节节点类型表 + 关键约束（控件怎么用）

设计取舍：**只搬"结构化、可机械提取"的部分**（表格）。散文式的设计理由留在 spec 里——
照搬进 prompt 只会占字数，LLM 要的是"这条属性什么意思、写错会怎样"。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HUB = HERE.parent
SKILLS = HUB / "puppethub" / "builtin_skills"


def spec_dir() -> Path:
    """语言仓 spec 目录。优先用已安装的 `puppet.spec_dir()`，回退到同级仓。"""
    try:
        from puppet import spec_dir as _sd
        return Path(_sd())
    except Exception:  # noqa: BLE001 - 未安装语言包时用同级目录
        return HUB.parent / "Puppet" / "spec"


def language_version() -> str:
    """语言版本号：取 `puppet.SPEC_VERSION`（规范版本真源），回退 pyproject。"""
    try:
        import puppet
        v = getattr(puppet, "SPEC_VERSION", None)
        if v:
            return str(v)
    except Exception:  # noqa: BLE001
        pass
    try:
        import tomllib
        data = tomllib.loads(
            (HUB.parent / "Puppet" / "pyproject.toml").read_text(encoding="utf-8"))
        return str(data.get("project", {}).get("version", "?"))
    except Exception:  # noqa: BLE001
        return "?"


def _tables(text: str) -> list:
    """抽出 markdown 表格，返回 [(表头行, [数据行…]), …]。"""
    out, head, rows = [], None, []
    for line in text.splitlines():
        if line.strip().startswith("|"):
            cells = line.strip()
            if head is None:
                head = cells
            elif re.match(r"^\|[\s:|-]+\|$", cells):
                continue                      # 分隔行
            else:
                rows.append(cells)
        else:
            if head is not None and rows:
                out.append((head, rows))
            head, rows = None, []
    if head is not None and rows:
        out.append((head, rows))
    return out


def _section(text: str, title: str) -> str:
    """取 `## <title>` 到下一个同级标题之间的正文。"""
    pattern = re.compile(r"^##\s+%s\s*$(.*?)(?=^##\s|\Z)" % re.escape(title),
                         re.S | re.M)
    m = pattern.search(text)
    return m.group(1) if m else ""


def build_diagnostics(spec: Path, version: str) -> str:
    """诊断速查：码表按分节汇成"出错后怎么修"的查询表。"""
    text = (spec / "06-diagnostics.md").read_text(encoding="utf-8")
    body = _section(text, "4. 码表")
    lines = ["## 诊断速查（收到诊断后先查这里）", "",
             "引擎把**带行号的诊断**回灌给你。下面的表是「这条码什么意思、怎么修」。",
             "**不要同一个错改两遍**——第二次请换一种写法。", ""]
    for head, rows in _tables(body):
        cols = [c.strip() for c in head.strip("|").split("|")]
        # 只认三列（码 / 级别 / 触发）的码表
        if len(cols) != 3 or cols[0] != "码":
            continue
        for row in rows:
            cells = [c.strip() for c in row.strip("|").split("|")]
            if len(cells) < 3:
                continue
            code, level, trigger = cells[0], cells[1], cells[2]
            code = code.strip("`").strip()
            trigger = trigger.replace("\n", " ")
            lines.append("- `%s`（%s）：%s" % (code, level, trigger))
    return "\n".join(lines) + "\n"


def build_controls(spec: Path, version: str) -> str:
    """控件语义：节点类型表 + 默认值/坑。"""
    text = (spec / "04-vocabulary.md").read_text(encoding="utf-8")
    body = _section(text, "2. 节点类型")
    lines = ["## 控件语义（每种控件怎么用、有什么坑）", "",
             "词汇清单每轮都在上下文里（【词汇边界】）。这里补的是**语义与陷阱**——",
             "表格里的「备注」列是踩过坑的地方，写之前扫一眼。", ""]
    for head, rows in _tables(body):
        cols = [c.strip() for c in head.strip("|").split("|")]
        if cols[:2] != ["类型", "语义"]:
            continue
        lines.append("| 类型 | 语义 / 备注 |")
        lines.append("|---|---|")
        for row in rows:
            cells = [c.strip() for c in row.strip("|").split("|")]
            if len(cells) < 2:
                continue
            kind = cells[0].strip("`")
            meaning = cells[1]
            if len(cells) >= 3 and cells[2] and cells[2] != "—":
                meaning = "%s —— %s" % (meaning, cells[2])
            lines.append("| `%s` | %s |" % (kind, meaning))
        lines.append("")
    return "\n".join(lines) + "\n"


_FRONT = """---
name: {name}
when: {when}
about: {about}
---

<!-- 本文件由 docs/sync-spec-skills.py 生成，源 = spec/{src}（语言 {version}）。请勿手改。 -->

{body}"""


def compose(name: str, when: str, about: str, src: str,
            version: str, body: str) -> str:
    return _FRONT.format(name=name, when=when, about=about,
                         src=src, version=version, body=body)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="只校验副本是否与 spec 同步（不改文件）")
    args = parser.parse_args()

    spec = spec_dir()
    if not spec.is_dir():
        print("找不到语言仓 spec 目录：%s" % spec)
        return 2
    version = language_version()

    plan = {
        "诊断速查.md": compose(
            "诊断速查",
            "诊断 报错 错误码 出错 排查 警告 SYNTAX 级别",
            "52 个诊断码速查：这条码什么意思、怎么修（从 spec/06 生成）",
            "06-diagnostics.md", version, build_diagnostics(spec, version)),
        "控件语义.md": compose(
            "控件语义",
            # 触发词只收**英文控件名**（它们就是代码里的词汇，检索精准）+ 场景词。
            # **刻意不收中文控件名**：中文名与其它技能大面积撞词（列表/模板/图标/
            # 按钮/输入/留白…），两个技能同时被拉进来只会稀释额度。中文口语需求
            # （"加个按钮"）本就该走美学组（怎么做好看），不是"控件语义"。
            "控件 组件 节点 类型 默认值 语义 "
            "window dialog col row navbar list tabs text icon divider spacer "
            "progress image avatar button input checkbox switch slider dropdown template",
            "21 种控件的语义与坑（从 spec/04 生成）：默认值、必须配对的属性、易错点",
            "04-vocabulary.md", version, build_controls(spec, version)),
    }

    stale = []
    for filename, content in plan.items():
        path = SKILLS / filename
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if args.check:
            if old != content:
                stale.append(filename)
            continue
        if old == content:
            print("  未变  %s" % filename)
            continue
        path.write_text(content, encoding="utf-8")
        print("  写入  %s（%d 字）" % (filename, len(content)))

    if args.check:
        if stale:
            print("以下技能副本已与 spec 不同步，请重跑 docs/sync-spec-skills.py：")
            for name in stale:
                print("  · %s" % name)
            return 1
        print("技能副本与 spec 同步（%d 份）" % len(plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
