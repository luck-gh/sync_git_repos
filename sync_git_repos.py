r"""
sync_repos.py - 批量同步本地 git 仓库 (Windows/Linux/Mac 通用)

流程:
    阶段一 [检查]:
        对所有有 origin 的仓库执行 fetch (origin + upstream,如果有),
        对比本地分支与 origin/upstream 的 ahead/behind 关系,
        输出一张状态表 + 每个仓库的操作建议(拉取/推送/同步upstream/分叉需手动处理)。
        工作区是否干净也会在表中标注,但不会阻止 fetch/分析。

    阶段二 [执行] (需用户确认,或 --yes 跳过确认):
        --mode pull (默认): 对建议"拉取"的仓库执行 merge。
        --mode push        : 对建议"推送"的仓库执行 git push origin <branch>。

        默认(不加 --force):
            - 工作区不干净 -> 跳过该仓库,不动现场
            - 远程分叉(本地和远程都有对方没有的提交) -> 跳过,只提示需手动处理

        加 --force 后:
            - 工作区不干净 -> 自动 `git stash` 暂存本地改动,操作完成后再 `stash pop` 恢复;
              如果恢复时和刚拉取的内容冲突,冲突文件保留本地改动(stash 内容),其余文件正常应用。
            - 合并时遇到冲突(远程分叉) -> 冲突文件保留本地版本(跳过该文件的远程改动),
              非冲突文件正常合并,合并仍会完成并生成一个合并提交。
            - push 遇到分叉 -> 先按上述规则合并 remote 变更(冲突文件保留本地版本),
              合并完成后再推送。

        任何"跳过冲突文件"都会在结果里列出具体文件名,方便你之后手动检查这些文件是否需要
        再单独处理远程的改动(因为它们的远程变更被跳过了,不会丢在 working tree 里带冲突标记)。

用法:
    python sync_repos.py <扫描目录> [--mode pull|push] [--dry-run] [--yes] [--force]

    python sync_repos.py D:\code                        # 检查 + 确认后拉取(默认,安全模式)
    python sync_repos.py D:\code --mode push             # 检查 + 确认后推送(安全模式)
    python sync_repos.py D:\code --force                 # 强制拉取,脏工作区/冲突也会尝试处理
    python sync_repos.py D:\code --mode push --force      # 强制推送(含分叉自动合并)
    python sync_repos.py D:\code --dry-run                # 只打印会执行的命令,不实际跑
    python sync_repos.py D:\code --yes                    # 跳过确认提示,直接执行

依赖: 系统需要能在命令行直接调用 git (即 git 已在 PATH 中)
"""

from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIR_NAMES = {"node_modules", ".cache", "venv", ".venv", "__pycache__"}


# --------------------------------------------------------------------------
# 基础 git 封装
# --------------------------------------------------------------------------

def git(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def remote_exists(repo: Path, remote: str) -> bool:
    return git(repo, ["remote", "get-url", remote]).returncode == 0


def is_clean_worktree(repo: Path) -> bool:
    result = git(repo, ["status", "--porcelain"])
    return result.returncode == 0 and result.stdout.strip() == ""


def current_branch(repo: Path) -> str | None:
    result = git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch if branch and branch != "HEAD" else None


def remote_branch_exists(repo: Path, remote: str, branch: str) -> bool:
    return git(repo, ["show-ref", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}"]).returncode == 0


def ahead_behind(repo: Path, remote: str, branch: str) -> tuple[int, int] | None:
    """返回 (behind, ahead):
       behind = remote 有、本地没有的提交数 (需要拉取)
       ahead  = 本地有、remote 没有的提交数 (需要推送)
       remote 没有同名分支则返回 None
    """
    if not remote_branch_exists(repo, remote, branch):
        return None
    result = git(repo, ["rev-list", "--left-right", "--count", f"{remote}/{branch}...HEAD"])
    if result.returncode != 0:
        return None
    try:
        behind_str, ahead_str = result.stdout.strip().split()
        return int(behind_str), int(ahead_str)
    except ValueError:
        return None


def list_conflict_files(repo: Path) -> list[str]:
    result = git(repo, ["diff", "--name-only", "--diff-filter=U"])
    return [f for f in result.stdout.strip().splitlines() if f]


# --------------------------------------------------------------------------
# 强制模式辅助: stash / 冲突时保留一方版本的合并
# --------------------------------------------------------------------------

def stash_changes(repo: Path, dry_run: bool) -> tuple[bool, str]:
    """暂存本地未提交改动。返回 (是否真的 stash 了东西, 说明)"""
    if dry_run:
        return True, "[dry-run] git stash push -u"
    r = git(repo, ["stash", "push", "-u", "-m", "sync_repos-force-autostash"])
    if r.returncode != 0:
        return False, f"stash 失败: {r.stderr.strip()[:150]}"
    if "No local changes to save" in r.stdout:
        return False, "无本地改动需要暂存"
    return True, "已暂存本地未提交改动"


def pop_stash(repo: Path, dry_run: bool) -> tuple[str, str, list[str]]:
    """恢复之前 stash 的改动。冲突时保留 stash 内容(本地改动),跳过冲突文件里合并结果带来的变化。
    返回 (status, message, skipped_files)   status: OK / CONFLICT_RESOLVED / FAILED
    """
    if dry_run:
        return "OK", "[dry-run] git stash pop", []

    r = git(repo, ["stash", "pop"])
    if r.returncode == 0:
        return "OK", "已恢复本地未提交改动", []

    conflict_files = list_conflict_files(repo)
    if not conflict_files:
        return "FAILED", f"恢复本地改动失败: {r.stderr.strip()[:150]}", []

    # stash pop 的冲突里, --theirs 对应 stash 的内容(即本地改动),保留它
    for f in conflict_files:
        git(repo, ["checkout", "--theirs", "--", f])
        git(repo, ["add", "--", f])
    git(repo, ["stash", "drop"])
    return "CONFLICT_RESOLVED", f"恢复本地改动时 {len(conflict_files)} 个文件冲突,已保留本地版本", conflict_files


def merge_with_conflict_skip(repo: Path, remote: str, branch: str, dry_run: bool) -> tuple[str, str, list[str]]:
    """合并 remote/branch。冲突时对冲突文件保留本地版本(跳过该文件的远程改动),
    其余文件正常合并,最终仍完成一次合并提交。
    返回 (status, message, skipped_files)   status: OK / NOTHING / CONFLICT_RESOLVED / FAILED
    """
    if dry_run:
        return "OK", f"[dry-run] git merge {remote}/{branch} (强制,冲突文件将保留本地版本)", []

    r = git(repo, ["merge", "--no-edit", f"{remote}/{branch}"])
    if r.returncode == 0:
        if "Already up to date" in r.stdout or "up to date" in r.stdout.lower():
            return "NOTHING", f"{remote} 已是最新", []
        return "OK", f"{remote} 合并完成", []

    conflict_files = list_conflict_files(repo)
    if not conflict_files:
        # 不是冲突导致的失败(可能是别的错误),放弃合并,不留烂摊子
        git(repo, ["merge", "--abort"])
        return "FAILED", f"{remote} 合并失败(非冲突原因): {r.stderr.strip()[:150]}", []

    # merge 冲突里, --ours 对应本地当前分支的内容,保留它、跳过远程对这些文件的改动
    for f in conflict_files:
        git(repo, ["checkout", "--ours", "--", f])
        git(repo, ["add", "--", f])

    commit_r = git(repo, ["commit", "--no-edit"])
    if commit_r.returncode == 0:
        preview = ", ".join(conflict_files[:5]) + ("..." if len(conflict_files) > 5 else "")
        return ("CONFLICT_RESOLVED",
                f"{remote} 合并完成,{len(conflict_files)} 个冲突文件已跳过远程改动(保留本地版本): {preview}",
                conflict_files)
    else:
        git(repo, ["merge", "--abort"])
        return "FAILED", f"{remote} 冲突解决后提交失败,已放弃合并: {commit_r.stderr.strip()[:150]}", conflict_files


# --------------------------------------------------------------------------
# 阶段一: 分析
# --------------------------------------------------------------------------

@dataclass
class RepoAnalysis:
    path: Path
    branch: str | None = None
    has_origin: bool = False
    has_upstream: bool = False
    clean: bool = True
    origin_behind: int | None = None
    origin_ahead: int | None = None
    upstream_behind: int | None = None
    upstream_ahead: int | None = None
    skip_reason: str | None = None   # LOCAL / NO_BRANCH (DIRTY 不再在这里硬跳过,交给执行阶段处理)
    suggestions: list[str] = field(default_factory=list)


def analyze_repo(repo: Path) -> RepoAnalysis:
    a = RepoAnalysis(path=repo)

    if not remote_exists(repo, "origin"):
        a.skip_reason = "LOCAL"
        return a
    a.has_origin = True
    a.clean = is_clean_worktree(repo)

    branch = current_branch(repo)
    if branch is None:
        a.skip_reason = "NO_BRANCH"
        return a
    a.branch = branch

    # fetch 是只读操作,不影响工作区,不管干不干净都可以做,以便给出准确建议
    git(repo, ["fetch", "origin", "--quiet"])
    ob = ahead_behind(repo, "origin", branch)
    if ob is not None:
        a.origin_behind, a.origin_ahead = ob

    if remote_exists(repo, "upstream"):
        a.has_upstream = True
        git(repo, ["fetch", "upstream", "--quiet"])
        ub = ahead_behind(repo, "upstream", branch)
        if ub is not None:
            a.upstream_behind, a.upstream_ahead = ub

    a.suggestions = build_suggestions(a)
    return a


def build_suggestions(a: RepoAnalysis) -> list[str]:
    s = []
    if a.origin_behind is not None:
        if a.origin_behind > 0 and (a.origin_ahead or 0) == 0:
            s.append("建议拉取 origin (落后 %d)" % a.origin_behind)
        elif (a.origin_ahead or 0) > 0 and a.origin_behind == 0:
            s.append("建议推送 origin (领先 %d)" % a.origin_ahead)
        elif (a.origin_ahead or 0) > 0 and a.origin_behind > 0:
            s.append("origin 已分叉(本地领先%d/落后%d),默认需手动处理,可用 --force 自动合并"
                      % (a.origin_ahead, a.origin_behind))
    if a.upstream_behind is not None and a.upstream_behind > 0:
        s.append("建议同步 upstream 分支 (落后 %d)" % a.upstream_behind)
    if not a.clean:
        s.append("工作区有未提交改动,默认将跳过,可用 --force 强制同步")
    if not s:
        s.append("无需操作")
    return s


def categorize(a: RepoAnalysis) -> tuple[int, str]:
    """给每个仓库分类,用于分组排序展示。数字越小越靠前(越需要关注)。"""
    if a.skip_reason == "LOCAL":
        return (90, "无 origin,跳过")
    if a.skip_reason == "NO_BRANCH":
        return (91, "detached HEAD,跳过")

    joined = "; ".join(a.suggestions)
    if "分叉" in joined:
        return (10, "分叉,需手动处理(或 --force)")
    if "建议推送" in joined:
        return (20, "需要推送")
    if "建议拉取" in joined or "同步 upstream" in joined:
        return (30, "需要拉取/同步 upstream")
    if "工作区有未提交改动" in joined:
        return (40, "仅工作区不干净(无远程差异)")
    return (80, "无需操作")


def print_analysis_table(results: list[RepoAnalysis]) -> None:
    print("\n================ 检查结果 ================")
    header = f"{'仓库':<30} {'分支':<12} {'origin(落后/领先)':<18} {'upstream(落后/领先)':<20} 建议"
    print(header)

    sorted_results = sorted(results, key=lambda a: (categorize(a)[0], a.path.name.lower()))

    last_category = None
    for a in sorted_results:
        weight, label = categorize(a)
        if label != last_category:
            print(f"--- {label} ---")
            last_category = label

        if a.skip_reason == "LOCAL":
            print(f"{a.path.name:<30} {'-':<12} {'-':<18} {'-':<20} 无 origin,跳过")
            continue
        if a.skip_reason == "NO_BRANCH":
            print(f"{a.path.name:<30} {'-':<12} {'-':<18} {'-':<20} detached HEAD,跳过")
            continue

        origin_str = f"{a.origin_behind}/{a.origin_ahead}" if a.origin_behind is not None else "无同名分支"
        upstream_str = "无 upstream"
        if a.has_upstream:
            upstream_str = f"{a.upstream_behind}/{a.upstream_ahead}" if a.upstream_behind is not None else "无同名分支"

        print(f"{a.path.name:<30} {a.branch:<12} {origin_str:<18} {upstream_str:<20} {'; '.join(a.suggestions)}")


# --------------------------------------------------------------------------
# 阶段二: 执行 (pull / push)
# --------------------------------------------------------------------------

@dataclass
class ExecResult:
    path: Path
    status: str   # OK / CONFLICT_RESOLVED / SKIP / NOTHING / FAILED
    detail: str


def maybe_push_after_upstream_sync(repo: Path, branch: str, dry_run: bool) -> str | None:
    """upstream 合并完成后,如果本地相对 origin 变成领先(无落后),就推回 origin,让 GitHub 上的 fork 也同步。
    返回一句说明,没有需要推送的情况返回 None。
    """
    if dry_run:
        return f"[dry-run] 若本地领先 origin,将执行 git push origin {branch} (同步 fork)"

    ob = ahead_behind(repo, "origin", branch)
    if ob is None:
        return None
    behind, ahead = ob
    if ahead > 0 and behind == 0:
        r = git(repo, ["push", "origin", branch])
        if r.returncode == 0:
            return f"已将 upstream 同步结果推送到 origin (领先 {ahead} 个提交,fork 已更新)"
        else:
            return "推送到 origin 失败: " + r.stderr.strip()[:150]
    return None


def do_pull(a: RepoAnalysis, dry_run: bool, force: bool, push_after_upstream_sync: bool) -> ExecResult:
    branch = a.branch
    msgs: list[str] = []
    final = "OK"
    stashed = False

    if not a.clean:
        if not force:
            return ExecResult(a.path, "SKIP", "工作区不干净,使用 --force 可强制同步")
        stashed, stash_msg = stash_changes(a.path, dry_run)
        msgs.append(stash_msg)

    # 1. origin 先同步
    if a.origin_behind is not None and a.origin_behind > 0:
        diverged = (a.origin_ahead or 0) > 0
        if diverged and not force:
            msgs.append("origin 已分叉,跳过合并(可用 --force 自动处理)")
        else:
            status, msg, _skipped = merge_with_conflict_skip(a.path, "origin", branch, dry_run)
            msgs.append(msg)
            if status == "FAILED":
                final = "FAILED"
            elif status == "CONFLICT_RESOLVED":
                final = "CONFLICT_RESOLVED" if final == "OK" else final
    else:
        msgs.append("origin 无需拉取")

    # 2. upstream 最后同步
    if a.has_upstream and a.upstream_behind is not None and a.upstream_behind > 0:
        diverged_up = (a.upstream_ahead or 0) > 0
        if diverged_up and not force:
            msgs.append("upstream 已分叉,跳过合并(可用 --force 自动处理)")
        else:
            status, msg, _skipped = merge_with_conflict_skip(a.path, "upstream", branch, dry_run)
            msgs.append(msg)
            if status == "FAILED":
                final = "FAILED"
            elif status == "CONFLICT_RESOLVED":
                final = "CONFLICT_RESOLVED" if final == "OK" else final

            if push_after_upstream_sync and status in ("OK", "CONFLICT_RESOLVED"):
                push_msg = maybe_push_after_upstream_sync(a.path, branch, dry_run)
                if push_msg:
                    msgs.append(push_msg)

    # 3. 恢复之前 stash 的本地改动
    if stashed:
        pop_status, pop_msg, _skipped = pop_stash(a.path, dry_run)
        msgs.append(pop_msg)
        if pop_status == "FAILED":
            final = "FAILED"
        elif pop_status == "CONFLICT_RESOLVED" and final == "OK":
            final = "CONFLICT_RESOLVED"

    return ExecResult(a.path, final, "; ".join(msgs))


def do_push(a: RepoAnalysis, dry_run: bool, force: bool) -> ExecResult:
    branch = a.branch

    if a.origin_ahead is None:
        return ExecResult(a.path, "SKIP", "origin 无同名分支,无法推送")

    diverged = (a.origin_behind or 0) > 0

    if (a.origin_ahead or 0) == 0 and not diverged:
        return ExecResult(a.path, "NOTHING", "没有本地领先的提交,无需推送")

    msgs: list[str] = []
    stashed = False

    if diverged:
        if not force:
            return ExecResult(a.path, "SKIP", "origin 已分叉,拒绝推送(需先手动处理,或使用 --force 自动合并后推送)")
        if not a.clean:
            stashed, stash_msg = stash_changes(a.path, dry_run)
            msgs.append(stash_msg)
        status, msg, _skipped = merge_with_conflict_skip(a.path, "origin", branch, dry_run)
        msgs.append(msg)
        if stashed:
            pop_status, pop_msg, _skipped2 = pop_stash(a.path, dry_run)
            msgs.append(pop_msg)
            if pop_status == "FAILED":
                return ExecResult(a.path, "FAILED", "; ".join(msgs))
        if status == "FAILED":
            return ExecResult(a.path, "FAILED", "; ".join(msgs))
    else:
        # 未分叉但工作区不干净:push 本身不需要干净的工作区,直接推送即可
        pass

    if dry_run:
        msgs.append(f"[dry-run] git push origin {branch}")
        return ExecResult(a.path, "OK", "; ".join(msgs))

    push_r = git(a.path, ["push", "origin", branch])
    if push_r.returncode == 0:
        msgs.append(f"已推送到 origin/{branch}")
        final = "CONFLICT_RESOLVED" if diverged else "OK"
        return ExecResult(a.path, final, "; ".join(msgs))
    else:
        msgs.append("推送失败: " + push_r.stderr.strip()[:150])
        return ExecResult(a.path, "FAILED", "; ".join(msgs))


def print_exec_table(results: list[ExecResult]) -> list[str]:
    print("\n================ 执行结果 ================")
    lines = [f"{'仓库':<30} {'状态':<18} 详情"]
    print(lines[0])
    for r in results:
        line = f"{r.path.name:<30} {r.status:<18} {r.detail}"
        print(line)
        lines.append(line)
    return lines


# --------------------------------------------------------------------------
# 扫描仓库
# --------------------------------------------------------------------------

def render_progress(done: int, total: int, prefix: str = "检查", width: int = 30) -> None:
    """在同一行刷新一个简单进度条。输出到 stderr,避免和正常结果混在一起被重定向。"""
    ratio = done / total if total else 1.0
    filled = int(width * ratio)
    bar = "#" * filled + "-" * (width - filled)
    end = "\n" if done >= total else ""
    print(f"\r{prefix} [{bar}] {done}/{total}", end=end, file=sys.stderr, flush=True)


def analyze_repos_parallel(repos: list[Path], workers: int) -> list[RepoAnalysis]:
    """并行 fetch/分析所有仓库,实时刷新进度条。fetch 是网络 IO,并行能显著缩短总等待时间。
    workers <= 0 时退化为单线程串行执行(关闭多线程),便于调试或规避并发问题。
    """
    total = len(repos)
    results: list[RepoAnalysis] = [None] * total  # type: ignore[list-item]
    done = 0
    render_progress(done, total)

    if workers <= 0:
        for i, repo in enumerate(repos):
            try:
                results[i] = analyze_repo(repo)
            except Exception as exc:  # 单个仓库分析异常不应中断整体
                a = RepoAnalysis(path=repo)
                a.skip_reason = "LOCAL"
                a.suggestions = [f"分析出错,已跳过: {str(exc)[:120]}"]
                results[i] = a
            done += 1
            render_progress(done, total)
        return results

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_idx = {pool.submit(analyze_repo, repo): i for i, repo in enumerate(repos)}
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as exc:  # 单个仓库分析异常不应中断整体
                a = RepoAnalysis(path=repos[idx])
                a.skip_reason = "LOCAL"
                a.suggestions = [f"分析出错,已跳过: {str(exc)[:120]}"]
                results[idx] = a
            done += 1
            render_progress(done, total)
    return results


def find_git_repos(base_dir: Path) -> list[Path]:
    repos = []
    for root, dirs, _files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
        if ".git" in dirs:
            repos.append(Path(root))
            dirs.remove(".git")
    return repos


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="批量同步本地 git 仓库(检查 + 拉取/推送,支持强制模式)")
    parser.add_argument("base_dir", nargs="?", default=".",
                         help="要扫描的根目录,例如 D:\\code;不传则默认当前目录 ./")
    parser.add_argument("--mode", choices=["pull", "push"], default="pull",
                         help="集体拉取(pull,默认)还是集体推送(push)")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的操作,不实际运行 merge/push/stash")
    parser.add_argument("--yes", "-y", action="store_true", help="跳过确认提示,检查完直接执行")
    parser.add_argument("--force", "-f", action="store_true",
                         help="强制模式: 工作区不干净也会尝试同步(自动stash/pop),"
                              "分叉/冲突时对冲突文件保留本地版本并跳过远程改动,而不是整体放弃")
    parser.add_argument("--log-dir", default=None,
                         help="日志输出目录,默认是脚本所在目录下的 logs 文件夹")
    parser.add_argument("--push-after-upstream-sync", action="store_true",
                         help="pull 模式下,合并完 upstream 后如果本地领先 origin,自动推回 origin"
                              "(让 GitHub 上的 fork 也同步到最新,而不只是本地领先)")
    parser.add_argument("--workers", type=int, default=8,
                         help="并行 fetch 的线程数(默认 8),仓库多时可适当调大;设为 0 则关闭多线程,改为单线程串行执行")
    args = parser.parse_args()

    base_dir = Path(args.base_dir).expanduser().resolve()
    if not base_dir.exists():
        print(f"目录不存在: {base_dir}")
        sys.exit(1)

    print(f"扫描目录: {base_dir}")
    if args.dry_run:
        print("[DRY-RUN 模式,不会实际执行 merge/push/stash]")
    print(f"模式: {'集体拉取 pull' if args.mode == 'pull' else '集体推送 push'}")
    print(f"多线程模式: {f'开启 {args.workers} 线程' if args.workers > 1 else '关闭多线程 (--workers 设置多线程数量)'}")
    print(f"强制模式: {'开启 (--force)' if args.force else '关闭 (未加 --force,遇脏工作区/分叉将跳过)'}")
    if args.mode == "pull":
        print(f"upstream 同步后推回 origin: {'开启' if args.push_after_upstream_sync else '关闭'}")

    repos = find_git_repos(base_dir)
    if not repos:
        print("未发现任何 git 仓库")
        return

    print(f"发现 {len(repos)} 个仓库,开始检查(fetch)...\n")

    analyses = analyze_repos_parallel(repos, workers=args.workers)
    print_analysis_table(analyses)

    actionable = [a for a in analyses if a.skip_reason is None]
    if not actionable:
        print("\n没有可操作的仓库(均为本地仓库/无有效分支),结束。")
        return

    if args.mode == "pull":
        need_action = [a for a in actionable
                        if (a.origin_behind or 0) > 0 or (a.upstream_behind or 0) > 0]
    else:
        need_action = [a for a in actionable if (a.origin_ahead or 0) > 0]

    if not need_action:
        print(f"\n检查完毕: 没有仓库需要 {args.mode},结束。")
        return

    print(f"\n以下 {len(need_action)} 个仓库需要执行 {args.mode}:")
    for a in need_action:
        print(f"  - {a.path.name}: {'; '.join(a.suggestions)}")

    if not args.yes:
        answer = input(f"\n是否继续执行 {args.mode}? 输入 y 确认,其他任意键取消: ").strip().lower()
        if answer != "y":
            print("已取消,未做任何改动。")
            return

    print(f"\n开始执行 {args.mode}...\n")
    exec_results = []
    for a in need_action:
        result = do_pull(a, args.dry_run, args.force, args.push_after_upstream_sync) if args.mode == "pull" else do_push(a, args.dry_run, args.force)
        exec_results.append(result)

    log_lines = print_exec_table(exec_results)

    if args.log_dir:
        log_dir = Path(args.log_dir).expanduser().resolve()
    else:
        log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"sync-repos-{args.mode}-{datetime.datetime.now():%Y%m%d-%H%M%S}.log"
    log_file.write_text("\n".join(log_lines), encoding="utf-8")
    print(f"\n完整日志: {log_file}")


if __name__ == "__main__":
    main()