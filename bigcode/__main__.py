"""命令行模块入口。

学习思路：python -m bigcode 会先运行这个文件，再转到 cli.main() 做真正的参数解析和会话启动。
"""
from .cli import main


if __name__ == "__main__":
    main()

