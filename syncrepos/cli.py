"""命令行入口: 解析参数 -> 扫描仓库 -> 确认 -> 并行(检查+同步一条龙) -> 打印结果 + 写日志。

  - SyncApp  组织整个主流程的应用类
  - main     命令行入口函数, 供 __init__ 和入口薄壳调用

与旧版的区别: 检查与同步不再是"先全部检查完、确认、再全部同步"两段, 而是每个仓库在
自己的线程里跑完整条龙(检查 -> 需要则同步)。确认改到开跑前一次性完成; "检查"与"同步"
两个进度条同时刷新(见 progress.py 的 MultiProgress)。
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

from .analyze import RepoAnalysis
from .execute import ExecResult
from .gitcmd import PUSH_TIMEOUT, GitRepo
from .progress import MultiProgress, ProgressBar
from .scan import RepoScanner
from .tee_log import tee_to_file


class SyncApp:
    """把参数解析后的一次运行组织成一个对象: 扫描 -> 确认 -> 并行检查+同步 -> 打印 -> 写日志。"""

    def __init__(self, args: argparse.Namespace):
        self.args = args

    # ----- 入口 ------------------------------------------------

    def run(self) -> None:
        args = self.args
        base_dir = Path(args.base_dir).expanduser().resolve()
        if not base_dir.exists():
            print(f"目录不存在: {base_dir}")
            sys.exit(1)

        # 先确定日志文件, 再用 tee 把流程里的终端输出写进日志。
        # 注意: 进度条走的是真实 stderr(见 progress.py), 不经 tee, 所以不会污染日志。
        log_file = self._log_file_path()
        print(f"日志文件: {log_file}\n")
        with tee_to_file(log_file):
            self._run_body(base_dir)

    def _run_body(self, base_dir: Path) -> None:
        args = self.args
        self._print_banner(base_dir)

        repos = RepoScanner.find_repos(base_dir)
        if not repos:
            print("未发现任何 git 仓库")
            return

        print(f"\n发现 {len(repos)} 个仓库:")
        for repo in sorted(repos, key=lambda p: str(p).lower()):
            print(f"  - {repo.name:<30} {repo}")

        # 检查与同步合并成一条龙, 无法先展示差异表再确认, 所以确认前移到开跑前一次
        if not self._confirm_start(len(repos)):
            return

        print(f"\n开始检查 + 同步 {len(repos)} 个仓库(检查完立即同步该仓库)...\n")

        check_bar = ProgressBar(len(repos), prefix="检查")
        # 同步条分母固定为仓库总数: 不需要同步的仓库检查完即算"同步完成", 分母不再跳变,
        # 卡住时也能看清"同步还差几个到 N"。同步条自然紧跟检查条、略微滞后。
        sync_bar = ProgressBar(len(repos), prefix="同步")
        scanner = RepoScanner(workers=args.workers)
        with MultiProgress([check_bar, sync_bar]):
            results = scanner.run_all(
                repos, lambda p: self._process_one(p, check_bar, sync_bar))

        analyses = [r[0] for r in results if r is not None]
        exec_results = [r[1] for r in results if r is not None and r[1] is not None]

        RepoAnalysis.print_table(analyses)
        if exec_results:
            ExecResult.print_table(exec_results)
        else:
            print("\n没有仓库需要执行同步(均无远程差异或被跳过)。")

    # ----- 单仓库一条龙: 检查 -> 需要则同步 -----------------------------

    def _process_one(self, repo_path: Path, check_bar: ProgressBar,
                     sync_bar: ProgressBar) -> tuple[RepoAnalysis, ExecResult | None]:
        """在 worker 线程里对单个仓库跑完整流程, 返回 (分析结果, 执行结果或 None)。

        检查阶段登记 fetch 超时(单次网络操作), 同步阶段登记 push 超时, 用于进度条倒计时。
        单仓库异常在此兜底, 不抛给上层, 以免拖垮其他仓库。
        """
        args = self.args

        # 检查阶段: 卡住时最坏就是单次 fetch 超时, 故倒计时上界用 fetch_timeout(而非 2 倍)
        check_bar.register(repo_path, args.fetch_timeout)
        try:
            a = RepoAnalysis.analyze(GitRepo(repo_path), fetch_timeout=args.fetch_timeout)
        except Exception as exc:  # 单个仓库分析异常不应中断整体
            a = self._fallback_analysis(repo_path, exc)
        check_bar.advance(repo_path)

        # 不需要同步的仓库(无差异/被跳过/无 origin): 直接算作"同步完成"推进一格,
        # 让同步条分母保持仓库总数、稳步推进到满, 不需要真的执行任何同步。
        if not self._repo_needs_action(a):
            sync_bar.advance(repo_path)
            return (a, None)

        # 需要同步: 登记 push 超时用于卡住时倒计时(dry-run 不走网络, 不登记)
        sync_bar.register(repo_path, None if args.dry_run else PUSH_TIMEOUT)
        try:
            if args.mode == "pull":
                result = ExecResult.pull(a, args.dry_run, args.force, args.push_after_upstream_sync)
            else:
                result = ExecResult.push(a, args.dry_run, args.force)
        except Exception as exc:  # 单个仓库执行异常不应中断整体
            result = ExecResult(repo_path, "FAILED", f"执行出错: {str(exc)[:120]}")
        sync_bar.advance(repo_path)
        return (a, result)

    def _repo_needs_action(self, a: RepoAnalysis) -> bool:
        """该仓库在当前模式下是否需要同步(与旧版 _select_need_action 的筛选口径一致)。"""
        if a.skip_reason is not None:
            return False
        if self.args.mode == "pull":
            return (a.origin_behind or 0) > 0 or (a.upstream_behind or 0) > 0
        return (a.origin_ahead or 0) > 0

    @staticmethod
    def _fallback_analysis(repo_path: Path, exc: Exception) -> RepoAnalysis:
        """单个仓库分析异常时的兜底结果, 标记为本地库(不参与同步)。"""
        a = RepoAnalysis(path=repo_path)
        a.skip_reason = "LOCAL"
        a.suggestions = [f"分析出错,已跳过: {str(exc)[:120]}"]
        return a

    # ----- 各步骤 ---------------------------------------------

    def _print_banner(self, base_dir: Path) -> None:
        args = self.args
        print(f"扫描目录: {base_dir}")
        if args.dry_run:
            print("[DRY-RUN 模式,不会实际执行 merge/push/stash]")
        print(f"模式: {'集体拉取 pull' if args.mode == 'pull' else '集体推送 push'}")
        print(f"多线程模式: {f'开启 {args.workers} 线程' if args.workers > 1 else '关闭多线程 (--workers 设置多线程数量)'}")
        print(f"强制模式: {'开启 (--force)' if args.force else '关闭 (未加 --force,遇脏工作区/分叉将跳过)'}")
        print(f"fetch 超时: {args.fetch_timeout:.0f}s (超时的仓库将跳过,不影响其他仓库)")
        if args.mode == "pull":
            print(f"upstream 同步后推回 origin: {'开启' if args.push_after_upstream_sync else '关闭'}")

    def _confirm_start(self, repo_count: int) -> bool:
        """开跑前确认。检查与同步合并进行, 故在真正动手前一次性征得同意。

        安全兜底仍在: 未加 --force 时, 工作区不干净/分叉的仓库会在同步阶段被自动跳过。
        """
        args = self.args
        if args.dry_run:
            return True   # dry-run 不改动任何东西, 无需确认
        if args.yes:
            return True
        action = "拉取" if args.mode == "pull" else "推送"
        answer = input(
            f"\n将对 {repo_count} 个仓库检查并按需{action}(未加 --force 时脏工作区/分叉会跳过)。"
            f"\n输入 y 确认开始,其他任意键取消: ").strip().lower()
        if answer != "y":
            print("已取消,未做任何改动。")
            return False
        return True

    def _log_file_path(self) -> Path:
        """算出本次运行的日志文件路径(不创建文件, 交给 tee 负责写入)。"""
        args = self.args
        if args.log_dir:
            log_dir = Path(args.log_dir).expanduser().resolve()
        else:
            # cli.py 位于 sync_git_repos/syncrepos/ 下, logs 要落在上一层的 sync_git_repos/logs
            log_dir = Path(__file__).resolve().parent.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / f"sync-repos-{args.mode}-{datetime.datetime.now():%Y%m%d-%H%M%S}.log"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量同步本地 git 仓库(检查 + 拉取/推送,支持强制模式)")
    parser.add_argument("base_dir", nargs="?", default=".",
                        help="要扫描的根目录,例如 D:\\code;不传则默认当前目录 ./")
    parser.add_argument("--mode", choices=["pull", "push"], default="pull",
                        help="集体拉取(pull,默认)还是集体推送(push)")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的操作,不实际运行 merge/push/stash")
    parser.add_argument("--yes", "-y", action="store_true", help="跳过确认提示,直接开始检查+同步")
    parser.add_argument("--force", "-f", action="store_true",
                        help="强制模式: 工作区不干净也会尝试同步(自动stash/pop),"
                             "分叉/冲突时对冲突文件保留本地版本并跳过远程改动,而不是整体放弃")
    parser.add_argument("--log-dir", default=None,
                        help="日志输出目录,默认是脚本所在目录下的 logs 文件夹")
    parser.add_argument("--push-after-upstream-sync", action="store_true",
                        help="pull 模式下,合并完 upstream 后如果本地领先 origin,自动推回 origin"
                             "(让 GitHub 上的 fork 也同步到最新,而不只是本地领先)")
    parser.add_argument("--workers", type=int, default=8,
                        help="并行处理的线程数(默认 8),每个线程负责一个仓库的检查+同步整条流程;"
                             "仓库多时可适当调大;设为 0 则关闭多线程,改为单线程串行执行")
    parser.add_argument("--fetch-timeout", type=float, default=30,
                        help="单个仓库 fetch 的超时秒数(默认 30)。超时的仓库会被跳过并标记,不影响其他仓库;"
                             "网络慢或仓库大时可调大。配合 GIT_TERMINAL_PROMPT=0 一起避免因等待凭证/host key 而卡死")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    SyncApp(args).run()
