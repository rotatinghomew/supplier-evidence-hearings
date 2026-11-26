"""便利入口：``python3 app.py`` 默认载入合成样例并启动服务。"""

from __future__ import annotations

import sys

from appeals_service.__main__ import main

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        args = ["serve", "--seed", "--state", "var/state.json"]
    raise SystemExit(main(args))
