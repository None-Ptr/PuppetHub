"""PyInstaller 的入口脚本（打成 exe 后等价于 `puppethub` 命令）。"""
import sys

from puppethub.cli import main

if __name__ == "__main__":
    sys.exit(main())
