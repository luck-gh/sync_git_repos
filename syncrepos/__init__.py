"""syncrepos - 批量同步本地 git 仓库。

模块划分:
    gitcmd    基础 git 封装 + 只读查询(分支/远程/领先落后)
    force_ops 强制模式辅助: stash / 冲突时保留一方版本的合并
    analyze   阶段一: 分析仓库状态 + 建议 + 分类 + 状态表
    execute   阶段二: 执行 pull / push + 结果表
    scan      扫描仓库 + 进度条 + 并行调度
    cli       命令行入口(参数解析 + 主流程)
"""

from __future__ import annotations

from .cli import main

__all__ = ["main"]
