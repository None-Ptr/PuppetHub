# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包脚本（V2 阶段 5：独立可分发的单文件）。

产物 = 一个 puppethub.exe（或对应平台二进制）：内含 puppethub 包、puppet 语言包
及其数据文件（spec/ + conformance/）、flet 运行时。**版本真源仍是 PyPI**——
单文件产物是"离线/受限环境"的分发形态，不是另一条依赖解析路径。

**已实测**（2026-10-03，Windows/onefile）：构建成功，产物 `verify-ci` 全绿
（117/117）。实测踩掉的三颗雷，都写进了本脚本的注释与代码：
1. 语言包是可编辑安装 → PyInstaller 跟不到，必须 `pathex` 给源码根；
2. flet 的 icons.json 等数据文件不进包就启动即炸 → `collect_all("flet")`；
3. 自证的子进程链（runner → 被测实现）在无 Python 的机器上全断 →
   cli 的 `-m` / 脚本垫片让 exe 充当 python 替身；三层 onefile 嵌套的临时目录
   互相踩踏 → verify 冻结态把 runner 改为进程内执行（verify.py）。

构建命令（构建后**必须跑一遍自证**再分发）：

    pip install pyinstaller
    pyinstaller packaging/puppethub.spec
    build-dist/puppethub.exe verify-ci
"""

import os

from puppet import assets_dir
import puppet

PUPPET_ASSETS = assets_dir()
# 语言包可能是**可编辑安装**（pip install -e）：PyInstaller 跟不到 site-packages 之外的
# 包，必须把它的源码根显式交给 pathex。同时把本仓根给 puppethub（开发态未安装时）。
PUPPET_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(puppet.__file__)))
SPEC_ROOT = os.path.dirname(os.path.abspath(SPEC))

# flet 的运行时不止 .py：icons.json 等数据文件与客户端二进制都必须进包
# （实测：缺 icons.json 在启动时就炸）。collect_all 是包级全收，代价是体积。
from PyInstaller.utils.hooks import collect_all
flet_datas, flet_binaries, flet_hidden = collect_all("flet")

a = Analysis(
    ["run_puppethub.py"],
    pathex=[PUPPET_ROOT, SPEC_ROOT],
    binaries=flet_binaries,
    datas=flet_datas + [
        # 语言包数据文件：spec/（LLM 的词汇依据）与 conformance/（自证用例）。
        # 缺了它们的包会在启动时被 compat.check_data_files() 当场拒绝——
        # 那是有意的：残缺分发宁可当场炸，不可晚炸成"功能坏了"。
        (PUPPET_ASSETS + "/spec", "share/puppet/spec"),
        (PUPPET_ASSETS + "/conformance", "share/puppet/conformance"),
    ],
    hiddenimports=["puppethub", "puppet", "flet"] + flet_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="puppethub",
    debug=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
)
