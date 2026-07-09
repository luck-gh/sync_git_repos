"""强制模式下的写操作: 暂存/恢复本地改动, 以及冲突时保留本地版本的合并。

这些操作都会改动仓库工作区或提交历史, 只在 --force 模式下才会被调用。
以 ForceOps 类持有一个 GitRepo, 所有写操作都是它的方法。
"""

from __future__ import annotations

from .gitcmd import GitRepo


class ForceOps:
    """针对单个仓库的强制模式写操作 (stash / pop / 冲突保留本地的合并)。"""

    def __init__(self, repo: GitRepo):
        self.repo = repo

    def stash_changes(self, dry_run: bool) -> tuple[bool, str]:
        """暂存本地未提交改动。返回 (是否真的 stash 了东西, 说明)"""
        if dry_run:
            return True, "[dry-run] git stash push -u"
        r = self.repo.run(["stash", "push", "-u", "-m", "sync_repos-force-autostash"])
        if r.returncode != 0:
            return False, f"stash 失败: {r.stderr.strip()[:150]}"
        if "No local changes to save" in r.stdout:
            return False, "无本地改动需要暂存"
        return True, "已暂存本地未提交改动"

    def pop_stash(self, dry_run: bool) -> tuple[str, str, list[str]]:
        """恢复之前 stash 的改动。冲突时保留 stash 内容(本地改动), 跳过冲突文件里合并结果带来的变化。
        返回 (status, message, skipped_files)   status: OK / CONFLICT_RESOLVED / FAILED
        """
        if dry_run:
            return "OK", "[dry-run] git stash pop", []

        r = self.repo.run(["stash", "pop"])
        if r.returncode == 0:
            return "OK", "已恢复本地未提交改动", []

        conflict_files = self.repo.list_conflict_files()
        if not conflict_files:
            return "FAILED", f"恢复本地改动失败: {r.stderr.strip()[:150]}", []

        # stash pop 的冲突里, --theirs 对应 stash 的内容(即本地改动), 保留它
        for f in conflict_files:
            self.repo.run(["checkout", "--theirs", "--", f])
            self.repo.run(["add", "--", f])
        self.repo.run(["stash", "drop"])
        return "CONFLICT_RESOLVED", f"恢复本地改动时 {len(conflict_files)} 个文件冲突,已保留本地版本", conflict_files

    def merge_with_conflict_skip(self, remote: str, branch: str, dry_run: bool) -> tuple[str, str, list[str]]:
        """合并 remote/branch。冲突时对冲突文件保留本地版本(跳过该文件的远程改动),
        其余文件正常合并, 最终仍完成一次合并提交。
        返回 (status, message, skipped_files)   status: OK / NOTHING / CONFLICT_RESOLVED / FAILED
        """
        if dry_run:
            return "OK", f"[dry-run] git merge {remote}/{branch} (强制,冲突文件将保留本地版本)", []

        r = self.repo.run(["merge", "--no-edit", f"{remote}/{branch}"])
        if r.returncode == 0:
            if "Already up to date" in r.stdout or "up to date" in r.stdout.lower():
                return "NOTHING", f"{remote} 已是最新", []
            return "OK", f"{remote} 合并完成", []

        conflict_files = self.repo.list_conflict_files()
        if not conflict_files:
            # 不是冲突导致的失败(可能是别的错误), 放弃合并, 不留烂摊子
            self.repo.run(["merge", "--abort"])
            return "FAILED", f"{remote} 合并失败(非冲突原因): {r.stderr.strip()[:150]}", []

        # merge 冲突里, --ours 对应本地当前分支的内容, 保留它、跳过远程对这些文件的改动
        for f in conflict_files:
            self.repo.run(["checkout", "--ours", "--", f])
            self.repo.run(["add", "--", f])

        commit_r = self.repo.run(["commit", "--no-edit"])
        if commit_r.returncode == 0:
            preview = ", ".join(conflict_files[:5]) + ("..." if len(conflict_files) > 5 else "")
            return ("CONFLICT_RESOLVED",
                    f"{remote} 合并完成,{len(conflict_files)} 个冲突文件已跳过远程改动(保留本地版本): {preview}",
                    conflict_files)
        else:
            self.repo.run(["merge", "--abort"])
            return "FAILED", f"{remote} 冲突解决后提交失败,已放弃合并: {commit_r.stderr.strip()[:150]}", conflict_files
