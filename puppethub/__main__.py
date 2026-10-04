"""`python -m puppethub` 入口（等价于安装后的 `puppethub` 脚本）。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
