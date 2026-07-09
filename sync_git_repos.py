r"""sync_git_repos.py - 批量同步本地 git 仓库 (Windows/Linux/Mac 通用)

本文件现在只是一个入口薄壳,真正的逻辑已按模块拆分到同目录的 syncrepos/ 包里:
    syncrepos/gitcmd.py    基础 git 封装 + 只读查询
    syncrepos/force_ops.py 强制模式辅助(stash / 冲突保留一方的合并)
    syncrepos/analyze.py   阶段一: 分析 + 建议 + 分类 + 状态表
    syncrepos/execute.py   阶段二: pull / push + 结果表
    syncrepos/scan.py      扫描仓库 + 进度条 + 并行调度
    syncrepos/cli.py       命令行入口(参数解析 + 主流程)

用法保持不变:
    python sync_git_repos.py <扫描目录> [--mode pull|push] [--dry-run] [--yes] [--force]

    python sync_git_repos.py D:\code                        # 检查 + 确认后拉取(默认,安全模式)
    python sync_git_repos.py D:\code --mode push             # 检查 + 确认后推送(安全模式)
    python sync_git_repos.py D:\code --force                 # 强制拉取,脏工作区/冲突也会尝试处理
    python sync_git_repos.py D:\code --mode push --force      # 强制推送(含分叉自动合并)
    python sync_git_repos.py D:\code --dry-run                # 只打印会执行的命令,不实际跑
    python sync_git_repos.py D:\code --yes                    # 跳过确认提示,直接执行

依赖: 系统需要能在命令行直接调用 git (即 git 已在 PATH 中)
"""

from __future__ import annotations

from syncrepos.cli import main

if __name__ == "__main__":
    main()
