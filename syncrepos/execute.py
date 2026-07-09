"""阶段二 [执行]: 对需要操作的仓库执行 pull(merge) 或 push。

一个类打包三件事(数据 + 自产 + 自我展示):
    - ExecResult 既是单个仓库的执行结果(dataclass 数据),
      又通过 classmethod pull()/push() 自己产生自己,
      还通过 classmethod print_table() 打印一批结果的结果表。

安全模式(默认)遇脏工作区/分叉会跳过; 强制模式(--force)自动 stash + 冲突保留本地版本合并。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .analyze import RepoAnalysis
from .force_ops import ForceOps
from .gitcmd import GIT_TIMEOUT_RETURNCODE, PUSH_TIMEOUT, GitRepo


@dataclass
class ExecResult:
    """单个仓库的执行结果, 兼 pull/push 工厂与结果表打印。"""

    path: Path
    status: str   # OK / CONFLICT_RESOLVED / SKIP / NOTHING / FAILED
    detail: str

    # ----- 工厂: pull -----------------------------------------------------

    @classmethod
    def pull(cls, a: RepoAnalysis, dry_run: bool, force: bool,
             push_after_upstream_sync: bool) -> "ExecResult":
        repo = GitRepo(a.path)
        ops = ForceOps(repo)
        branch = a.branch
        msgs: list[str] = []
        final = "OK"
        stashed = False

        if not a.clean:
            if not force:
                return cls(a.path, "SKIP", "工作区不干净,使用 --force 可强制同步")
            stashed, stash_msg = ops.stash_changes(dry_run)
            msgs.append(stash_msg)

        # 1. origin 先同步
        if a.origin_behind is not None and a.origin_behind > 0:
            diverged = (a.origin_ahead or 0) > 0
            if diverged and not force:
                msgs.append("origin 已分叉,跳过合并(可用 --force 自动处理)")
            else:
                status, msg, _skipped = ops.merge_with_conflict_skip("origin", branch, dry_run)
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
                status, msg, _skipped = ops.merge_with_conflict_skip("upstream", branch, dry_run)
                msgs.append(msg)
                if status == "FAILED":
                    final = "FAILED"
                elif status == "CONFLICT_RESOLVED":
                    final = "CONFLICT_RESOLVED" if final == "OK" else final

                if push_after_upstream_sync and status in ("OK", "CONFLICT_RESOLVED"):
                    push_msg = cls._maybe_push_after_upstream_sync(repo, branch, dry_run)
                    if push_msg:
                        msgs.append(push_msg)

        # 3. 恢复之前 stash 的本地改动
        if stashed:
            pop_status, pop_msg, _skipped = ops.pop_stash(dry_run)
            msgs.append(pop_msg)
            if pop_status == "FAILED":
                final = "FAILED"
            elif pop_status == "CONFLICT_RESOLVED" and final == "OK":
                final = "CONFLICT_RESOLVED"

        return cls(a.path, final, "; ".join(msgs))

    @staticmethod
    def _maybe_push_after_upstream_sync(repo: GitRepo, branch: str, dry_run: bool) -> str | None:
        """upstream 合并完成后, 如果本地相对 origin 变成领先(无落后), 就推回 origin, 让 GitHub 上的 fork 也同步。
        返回一句说明, 没有需要推送的情况返回 None。
        """
        if dry_run:
            return f"[dry-run] 若本地领先 origin,将执行 git push origin {branch} (同步 fork)"

        ob = repo.ahead_behind("origin", branch)
        if ob is None:
            return None
        behind, ahead = ob
        if ahead > 0 and behind == 0:
            r = repo.run(["push", "origin", branch], timeout=PUSH_TIMEOUT)
            if r.returncode == 0:
                return f"已将 upstream 同步结果推送到 origin (领先 {ahead} 个提交,fork 已更新)"
            elif r.returncode == GIT_TIMEOUT_RETURNCODE:
                return f"推送到 origin 超时(>{PUSH_TIMEOUT}s),已跳过"
            else:
                return "推送到 origin 失败: " + r.stderr.strip()[:150]
        return None

    # ----- 工厂: push -----------------------------------------------------

    @classmethod
    def push(cls, a: RepoAnalysis, dry_run: bool, force: bool) -> "ExecResult":
        repo = GitRepo(a.path)
        ops = ForceOps(repo)
        branch = a.branch

        if a.origin_ahead is None:
            return cls(a.path, "SKIP", "origin 无同名分支,无法推送")

        diverged = (a.origin_behind or 0) > 0

        if (a.origin_ahead or 0) == 0 and not diverged:
            return cls(a.path, "NOTHING", "没有本地领先的提交,无需推送")

        msgs: list[str] = []
        stashed = False

        if diverged:
            if not force:
                return cls(a.path, "SKIP", "origin 已分叉,拒绝推送(需先手动处理,或使用 --force 自动合并后推送)")
            if not a.clean:
                stashed, stash_msg = ops.stash_changes(dry_run)
                msgs.append(stash_msg)
            status, msg, _skipped = ops.merge_with_conflict_skip("origin", branch, dry_run)
            msgs.append(msg)
            if stashed:
                pop_status, pop_msg, _skipped2 = ops.pop_stash(dry_run)
                msgs.append(pop_msg)
                if pop_status == "FAILED":
                    return cls(a.path, "FAILED", "; ".join(msgs))
            if status == "FAILED":
                return cls(a.path, "FAILED", "; ".join(msgs))
        else:
            # 未分叉但工作区不干净: push 本身不需要干净的工作区, 直接推送即可
            pass

        if dry_run:
            msgs.append(f"[dry-run] git push origin {branch}")
            return cls(a.path, "OK", "; ".join(msgs))

        push_r = repo.run(["push", "origin", branch], timeout=PUSH_TIMEOUT)
        if push_r.returncode == 0:
            msgs.append(f"已推送到 origin/{branch}")
            final = "CONFLICT_RESOLVED" if diverged else "OK"
            return cls(a.path, final, "; ".join(msgs))
        elif push_r.returncode == GIT_TIMEOUT_RETURNCODE:
            msgs.append(f"推送超时(>{PUSH_TIMEOUT}s),已跳过")
            return cls(a.path, "FAILED", "; ".join(msgs))
        else:
            msgs.append("推送失败: " + push_r.stderr.strip()[:150])
            return cls(a.path, "FAILED", "; ".join(msgs))

    # ----- 批量展示: 一批结果的结果表 -------------------------------------

    @classmethod
    def print_table(cls, results: list["ExecResult"]) -> list[str]:
        """打印结果表, 返回可写入日志的文本行。"""
        print("\n================ 执行结果 ================")
        lines = [f"{'仓库':<30} {'状态':<18} 详情"]
        print(lines[0])
        for r in results:
            line = f"{r.path.name:<30} {r.status:<18} {r.detail}"
            print(line)
            lines.append(line)
        return lines
