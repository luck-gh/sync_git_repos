"""扫描 git 仓库 + 并行调度"每个仓库跑完整流程"。

fetch/push 都是网络 IO, 并行能显著缩短总等待时间; 进度条(见 progress.py)让等待有反馈。

  - RepoScanner  扫描目录找仓库(find_repos) + 并行/串行地对每个仓库调用一个 worker
                 函数(run_all)。worker 函数里做什么由调用方决定(现在是"检查 + 同步"
                 一条龙), scan 只负责"并行地把每个仓库喂给 worker"这件事。
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, TypeVar

SKIP_DIR_NAMES = {"node_modules", ".cache", "venv", ".venv", "__pycache__"}

T = TypeVar("T")


class RepoScanner:
    """扫描目录发现 git 仓库, 并行(或串行)地对每个仓库执行调用方给的 worker 函数。"""

    def __init__(self, workers: int = 8):
        self.workers = workers

    @staticmethod
    def find_repos(base_dir: Path) -> list[Path]:
        repos = []
        for root, dirs, _files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
            if ".git" in dirs:
                repos.append(Path(root))
                dirs.remove(".git")
        return repos

    def run_all(self, repos: list[Path], worker: Callable[[Path], T]) -> list[T]:
        """并行对每个仓库调用 worker(repo_path), 返回结果列表(顺序与 repos 一致)。

        workers <= 0 时退化为单线程串行执行(关闭多线程), 便于调试或规避并发问题。
        worker 应自行处理单仓库异常并返回兜底结果, 以免一个仓库拖垮整体; 这里再兜一层底,
        万一 worker 仍抛异常, 该位置留 None(由调用方过滤)。
        """
        total = len(repos)
        results: list[T] = [None] * total  # type: ignore[list-item]

        if self.workers <= 0:
            for i, repo_path in enumerate(repos):
                results[i] = worker(repo_path)
            return results

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            future_to_idx = {pool.submit(worker, repo_path): i
                             for i, repo_path in enumerate(repos)}
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception:  # worker 已尽量自兜底, 这里只防万一, 不中断整体
                    results[idx] = None  # type: ignore[assignment]
        return results
