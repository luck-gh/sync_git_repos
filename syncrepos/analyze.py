"""阶段一 [检查/分析]: fetch 后对比本地与 origin/upstream 的领先落后关系。

一个类打包三件事(数据 + 自产 + 自我展示):
    - RepoAnalysis 既是单个仓库的分析结果(dataclass 数据),
      又通过 classmethod analyze() 自己产生自己,
      还通过 classmethod print_table() 打印一批结果的状态表。
只做只读操作, 不改工作区。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .gitcmd import (
    GIT_TIMEOUT_RETURNCODE,
    GitRepo,
    parse_remote_endpoint,
    tcp_probe,
)


@dataclass
class RepoAnalysis:
    """单个仓库的分析结果, 兼工厂与状态表打印。"""

    path: Path
    branch: str | None = None
    has_origin: bool = False
    has_upstream: bool = False
    clean: bool = True
    origin_behind: int | None = None
    origin_ahead: int | None = None
    upstream_behind: int | None = None
    upstream_ahead: int | None = None
    skip_reason: str | None = None      # LOCAL / NO_BRANCH / TIMEOUT(origin)
    upstream_timeout: bool = False       # upstream fetch 超时(origin 数据仍有效)
    suggestions: list[str] = field(default_factory=list)

    # ----- 工厂: fetch + 分析, 产出一个 RepoAnalysis -----------------------

    @classmethod
    def analyze(cls, repo: GitRepo, timeout: float | None = 30) -> "RepoAnalysis":
        a = cls(path=repo.path)

        if not repo.remote_exists("origin"):
            a.skip_reason = "LOCAL"
            return a
        a.has_origin = True
        a.clean = repo.is_clean_worktree()

        branch = repo.current_branch()
        if branch is None:
            a.skip_reason = "NO_BRANCH"
            return a
        a.branch = branch

        # fetch 是只读操作, 不影响工作区, 不管干不干净都可以做, 以便给出准确建议
        r = repo.run(["fetch", "origin", "--quiet"], timeout=timeout)
        if r.returncode == GIT_TIMEOUT_RETURNCODE:
            assert timeout is not None
            a.skip_reason = "TIMEOUT"
            reason = cls._diagnose_timeout(repo, "origin")
            a.suggestions = [f"fetch origin 超时(>{timeout:.0f}s),已跳过", reason]
            return a
        ob = repo.ahead_behind("origin", branch)
        if ob is not None:
            a.origin_behind, a.origin_ahead = ob

        notes: list[str] = []
        if repo.remote_exists("upstream"):
            a.has_upstream = True
            r = repo.run(["fetch", "upstream", "--quiet"], timeout=timeout)
            if r.returncode == GIT_TIMEOUT_RETURNCODE:
                assert timeout is not None
                a.upstream_timeout = True
                reason = cls._diagnose_timeout(repo, "upstream")
                notes.append(f"fetch upstream 超时(>{timeout:.0f}s),origin 数据已获取; {reason}")
            else:
                ub = repo.ahead_behind("upstream", branch)
                if ub is not None:
                    a.upstream_behind, a.upstream_ahead = ub

        a.suggestions = a._build_suggestions() + notes
        return a

    @staticmethod
    def _diagnose_timeout(repo: GitRepo, remote: str) -> str:
        """fetch 超时后, 尽力给出原因线索(非定论)。

        git 进程被超时杀掉后拿不到它的报错, 所以这里主动解析 remote URL 并做一次
        短超时 TCP 探测。注意: 若系统开了代理/VPN(透明代理), TCP 探测可能对任何
        地址都"秒连成功", 结果会失真——所以探测结果只作线索, 措辞不下死结论。
          - 地址解析不出 -> 可能是本地/SSH 别名, 或配置问题
          - 连不上         -> 大概率网络不通(断网/防火墙/地址不可达)
          - 连得上         -> 网络层可达; 慢在传输(仓库大/带宽低)或卡在凭证/host key 交互
                             (但若开了代理, 此结果不可信)
        """
        url = repo.remote_url(remote)
        if not url:
            return "原因线索: 无法获取 remote 地址"

        host, port = parse_remote_endpoint(url)
        if host is None:
            return f"原因线索: 无法从 {url} 解析主机(可能是 SSH 别名或本地路径)"

        if tcp_probe(host, port):
            return (f"原因线索: {host}:{port} 网络层可达,多半是仓库大/带宽低导致传输慢,"
                    f"或卡在凭证/host key 交互;可调大 --timeout 重试"
                    f"(若开了代理/VPN,此判断可能不准)")
        return (f"原因线索: {host}:{port} 连不上,大概率网络不通"
                f"(断网/防火墙/地址不可达);请检查网络或代理设置")

    # ----- 单实例行为: 建议 / 分类 ----------------------------------------

    def _build_suggestions(self) -> list[str]:
        s = []
        if self.origin_behind is not None:
            if self.origin_behind > 0 and (self.origin_ahead or 0) == 0:
                s.append("建议拉取 origin (落后 %d)" % self.origin_behind)
            elif (self.origin_ahead or 0) > 0 and self.origin_behind == 0:
                s.append("建议推送 origin (领先 %d)" % self.origin_ahead)
            elif (self.origin_ahead or 0) > 0 and self.origin_behind > 0:
                s.append("origin 已分叉(本地领先%d/落后%d),默认需手动处理,可用 --force 自动合并"
                          % (self.origin_ahead, self.origin_behind))
        if self.upstream_behind is not None and self.upstream_behind > 0:
            s.append("建议同步 upstream 分支 (落后 %d)" % self.upstream_behind)
        if not self.clean:
            s.append("工作区有未提交改动,默认将跳过,可用 --force 强制同步")
        if not s:
            s.append("无需操作")
        return s

    def category(self) -> tuple[int, str]:
        """分类, 用于分组排序展示。数字越小越靠前(越需要关注)。"""
        if self.skip_reason == "LOCAL":
            return (90, "无 origin,跳过")
        if self.skip_reason == "NO_BRANCH":
            return (91, "detached HEAD,跳过")
        if self.skip_reason == "TIMEOUT":
            return (92, "fetch 超时,已跳过(见下方原因,可调大 --timeout 重试)")
        if self.upstream_timeout:
            return (93, "upstream fetch 超时,已跳过(origin 数据已获取,见下方原因)")

        joined = "; ".join(self.suggestions)
        if "分叉" in joined:
            return (10, "分叉,需手动处理(或 --force)")
        if "建议推送" in joined:
            return (20, "需要推送")
        if "建议拉取" in joined or "同步 upstream" in joined:
            return (30, "需要拉取/同步 upstream")
        if "工作区有未提交改动" in joined:
            return (40, "仅工作区不干净(无远程差异)")
        return (80, "无需操作")

    def _row(self, idx: int) -> str:
        """自己这一行在状态表里怎么显示。idx 是该仓库在发现列表中的序号。"""
        label = f"[{idx:>2}] {self.path.name:<28}"
        if self.skip_reason == "TIMEOUT":
            return f"{label} {(self.branch or '-'):<12} {'-':<18} {'-':<20} {'; '.join(self.suggestions)}"
        if self.skip_reason == "LOCAL":
            return f"{label} {'-':<12} {'-':<18} {'-':<20} 无 origin,跳过"
        if self.skip_reason == "NO_BRANCH":
            return f"{label} {'-':<12} {'-':<18} {'-':<20} detached HEAD,跳过"

        origin_str = f"{self.origin_behind}/{self.origin_ahead}" if self.origin_behind is not None else "无同名分支"
        upstream_str = "无 upstream"
        if self.has_upstream:
            upstream_str = f"{self.upstream_behind}/{self.upstream_ahead}" if self.upstream_behind is not None else "无同名分支"
        return f"{label} {self.branch:<12} {origin_str:<18} {upstream_str:<20} {'; '.join(self.suggestions)}"

    # ----- 批量展示: 一批结果的状态表 -------------------------------------

    @classmethod
    def print_table(cls, results: list["RepoAnalysis"],
                    repo_index: dict) -> None:
        print("\n================ 检查结果 ================")
        header = f"{'序号+仓库':<33} {'分支':<12} {'origin(落后/领先)':<18} {'upstream(落后/领先)':<20} 建议"
        print(header)

        sorted_results = sorted(results, key=lambda a: (a.category()[0], a.path.name.lower()))

        last_category = None
        for a in sorted_results:
            _, label = a.category()
            if label != last_category:
                print(f"--- {label} ---")
                last_category = label
            idx = repo_index.get(a.path, 0)
            print(a._row(idx))
