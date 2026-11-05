"""`python -m datahub.intake <source> <raw 文件>` 的入口。

真正实现在 `datahub.fin.intake`；这个包只是为了让文档和命令行里
少打一层 `.fin`（历史上就是这样写的）。
"""

from datahub.fin.intake.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
