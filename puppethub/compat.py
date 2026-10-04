"""语言版本兼容：窄范围声明 + 启动硬校验。

发布之后 pip 会自动解依赖，用户装到的语言包版本不由我们控制；而语言会继续出修订。
于是"同一个 app 在不同机器上行为不同，而人看不出来"成为一个真实风险——
**看不见的差异比崩溃更糟**。

所以不匹配时**拒绝启动**并说清怎么办，而不是警告。另一个被否决的方案是
"检测到新版就自动切到兼容行为"：那会让同一个 app 在不同机器上行为不同，
正是窄范围要解决的那件事。
"""

from __future__ import annotations

import os

PACKAGE = "openpuppet-language"
LOWER = (2, 1)
UPPER = (2, 2)
SPEC_RANGE = ">=2.1,<2.2"


class SpecMismatch(RuntimeError):
    """语言版本不匹配。刻意做成异常：必须阻断启动，不得降级继续跑。"""


def _parse(text: str):
    parts = []
    for chunk in str(text).split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts[:3])


def installed_version() -> str:
    """实际安装的语言包版本。

    优先读**发行版元数据**（`pip` 解依赖的对象就是它）；退而读 `puppet.SPEC_VERSION`
    （源码树 / `-e` 安装时两者一致）。
    """
    import importlib.metadata as md
    try:
        return md.version(PACKAGE)
    except md.PackageNotFoundError:
        pass
    try:
        import puppet
    except ImportError:
        return "0.0.0"
    return getattr(puppet, "SPEC_VERSION", "0.0.0")


def check_spec_version(version: str | None = None) -> str:
    """校验版本，返回实际版本；不匹配则抛 `SpecMismatch`。"""
    raw = version or installed_version()
    actual = _parse(raw)
    if not (LOWER <= actual < UPPER):
        raise SpecMismatch(
            "语言包版本不匹配：实际装的是 %s，本版需要 %s。\n"
            "  请安装：pip install \"%s%s\"\n"
            "  为什么不继续跑：语言 2.x 冻结的承诺是\"已有 .puppet 的行为由规范定义\"，"
            "在错配的语言上继续跑，它写出的真源可能是另一种行为，而人看不出来。"
            % (raw, SPEC_RANGE, PACKAGE, SPEC_RANGE))
    return raw


class DataFilesMissing(RuntimeError):
    """语言包的数据文件（spec/ / conformance/）不在。同样是阻断启动的失败。"""


def check_data_files() -> str:
    """校验语言包的**数据文件**可达（分发定稿：版本真源 = PyPI，数据随 wheel 走）。

    `pip install openpuppet-language` 会把 `spec/` 与 `conformance/` 按 data-files
    装到 `<prefix>/share/puppet/`；但 wheel 可能被残缺安装（手动拷包、被裁剪的环境），
    而缺数据文件的故障会**推迟到**很晚才炸（上下文注入拿不到规范分节、verify 没用例可跑）
    ——那时用户看到的是"功能坏了"，不是"安装坏了"。所以启动时查一次，坏了说清怎么修。
    """
    try:
        from puppet import assets_dir
    except ImportError as ex:                      # pragma: no cover - 版本校验在前，难到达
        raise DataFilesMissing("语言包未安装：%s" % ex) from ex
    spec = os.path.join(assets_dir(), "spec")
    probe = os.path.join(spec, "04-vocabulary.md")
    if not os.path.isfile(probe):
        raise DataFilesMissing(
            "语言包的数据文件不完整（找不到 %s）。\n"
            "  请重装：pip install --force-reinstall \"%s%s\"\n"
            "  为什么不继续跑：规范分节注入与自证都依赖这些只读资产，缺了它们 "
            "LLM 会在拿不到词汇依据的情况下改程序。"
            % (probe, PACKAGE, SPEC_RANGE))
    return spec
