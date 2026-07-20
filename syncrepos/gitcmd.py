"""基础 git 封装: 以 GitRepo 类表示单个仓库, 承载所有 git 命令与只读查询。

所有 git 调用统一从 GitRepo.run 走, 好处:
    - 统一注入禁止交互式等待的环境变量(缺凭证/未确认 host key 直接失败, 不挂死);
    - 网络操作(fetch/push)统一支持超时, 超时返回约定码 GIT_TIMEOUT_RETURNCODE。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# fetch/push 等网络操作卡住时用的超时返回码(借用 shell 里 timeout 的惯例值 124)
GIT_TIMEOUT_RETURNCODE = 124

# fetch/push 网络操作的默认超时秒数
DEFAULT_TIMEOUT = 30

# 杀进程树后, 收尾读取管道最多再等这么久(树已被杀, 正常会立即 EOF; 只为极端情况兜底)
_KILL_DRAIN_TIMEOUT = 5


def _popen_kwargs() -> dict:
    """让子进程可被"连子孙一起杀"的启动参数。

    POSIX: start_new_session=True 让 git 自成进程组, 超时时用 killpg 杀整组。
    Windows: 不改启动参数(保留 Ctrl+C 正常传播), 超时时靠 taskkill /T 按 PID 树杀。
    """
    if os.name == "nt":
        return {}
    return {"start_new_session": True}


def _kill_process_tree(pid: int) -> None:
    """杀掉 pid 及其所有子孙进程。

    git fetch/push 会派生网络子进程(git-remote-https / ssh 等), 这些子进程继承了
    输出管道。只杀 git 主进程的话, 存活的子进程仍占着管道写端, 导致读取管道的
    communicate() 永远等不到 EOF 而挂死——这正是"超时了却还卡着"的根因。
    所以必须连子孙一起杀。
    """
    if os.name == "nt":
        # /T 连子进程树一起杀, /F 强制。进程已退出会返非 0, 忽略即可。
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True)
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


# 诊断超时用的 TCP 连通性探测超时(秒): 短一点, 只为区分"连不上" vs "连得上但慢"
PROBE_TIMEOUT = 5


def parse_remote_endpoint(url: str) -> tuple[str | None, int]:
    """从 git remote URL 解析出 (host, port), 用于超时后探测连通性。

    支持 https/http/ssh/git 协议 URL, 以及 scp 式 user@host:path。
    本地路径 / file:// / 无法解析时返回 (None, 0)。
    """
    import re
    from urllib.parse import urlparse

    url = url.strip()
    if "://" in url:
        p = urlparse(url)
        default_port = {"http": 80, "https": 443, "ssh": 22, "git": 9418}.get(p.scheme or "")
        if p.scheme == "file" or default_port is None:
            return (None, 0)
        return (p.hostname, p.port or default_port)

    # Windows 盘符(C:\...)不是 scp 语法, 排除
    if len(url) >= 2 and url[1] == ":":
        return (None, 0)
    # scp 式: [user@]host:path -> 走 SSH(22)
    m = re.match(r"^(?:[^@/]+@)?([^:/]+):", url)
    if m:
        return (m.group(1), 22)
    return (None, 0)


def tcp_probe(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    """尝试 TCP 连接 host:port。通了返回 True, 连不上(超时/拒绝/DNS 失败)返回 False。"""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run_command(cmd: list[str], cwd: Path, env: dict,
                timeout: float | None) -> subprocess.CompletedProcess:
    """执行一条命令, 支持超时后杀整个进程树。

    timeout 为 None 表示不限时(本地只读操作)。超时后杀掉进程树并返回
    GIT_TIMEOUT_RETURNCODE, 让卡住的仓库快速失败、不拖累其他仓库。
    """
    with subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", env=env,
        **_popen_kwargs(),
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc.pid)
            # 树已杀, 管道应立即 EOF; 仍给个上限, 避免极端情况下收尾再次挂死
            try:
                proc.communicate(timeout=_KILL_DRAIN_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass
            timeout_desc = f">{timeout:.0f}s" if timeout is not None else "超时"
            return subprocess.CompletedProcess(
                cmd, GIT_TIMEOUT_RETURNCODE, "", f"操作超时({timeout_desc}),已中止")


class GitRepo:
    """表示单个本地 git 仓库, 所有针对该仓库的 git 操作都是它的方法。"""

    def __init__(self, path: Path):
        self.path = path

    def __repr__(self) -> str:
        return f"GitRepo({self.path})"

    @property
    def name(self) -> str:
        return self.path.name

    # ----- 底层执行 -------------------------------------------------------

    @staticmethod
    def _env() -> dict:
        """构造禁止交互式等待的环境变量, 避免 git 在缺凭证/未确认 host key 时挂起整个进程。"""
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"                 # 需要用户名/密码时直接失败, 不弹交互提示
        env["GCM_INTERACTIVE"] = "Never"                 # 关闭 Git Credential Manager 的弹窗等待
        # 已有自定义 SSH 命令则尊重用户设置, 否则给一个非交互 + 连接超时的默认值
        env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes -oConnectTimeout=10")
        return env

    def run(self, args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
        """执行 git 命令。timeout 为 None 表示不限时(本地只读操作用);
        网络操作(fetch/push)应传入超时, 超时后进程树被杀死并返回 GIT_TIMEOUT_RETURNCODE,
        从而让卡住的仓库快速失败、不拖累其他仓库。

        超时的关键是 run_command 会连 git 派生的网络子进程(git-remote-https / ssh)
        一起杀, 否则那些子进程占着输出管道, 会让收尾读取永远等不到 EOF 而挂死。
        """
        return run_command(["git"] + args, cwd=self.path, env=self._env(), timeout=timeout)

    # ----- 只读查询 -------------------------------------------------------

    def remote_exists(self, remote: str) -> bool:
        return self.run(["remote", "get-url", remote]).returncode == 0

    def remote_url(self, remote: str) -> str | None:
        r = self.run(["remote", "get-url", remote])
        return r.stdout.strip() if r.returncode == 0 else None

    def is_clean_worktree(self) -> bool:
        result = self.run(["status", "--porcelain"])
        return result.returncode == 0 and result.stdout.strip() == ""

    def current_branch(self) -> str | None:
        result = self.run(["rev-parse", "--abbrev-ref", "HEAD"])
        if result.returncode != 0:
            return None
        branch = result.stdout.strip()
        return branch if branch and branch != "HEAD" else None

    def remote_branch_exists(self, remote: str, branch: str) -> bool:
        return self.run(["show-ref", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}"]).returncode == 0

    def ahead_behind(self, remote: str, branch: str) -> tuple[int, int] | None:
        """返回 (behind, ahead):
           behind = remote 有、本地没有的提交数 (需要拉取)
           ahead  = 本地有、remote 没有的提交数 (需要推送)
           remote 没有同名分支则返回 None
        """
        if not self.remote_branch_exists(remote, branch):
            return None
        result = self.run(["rev-list", "--left-right", "--count", f"{remote}/{branch}...HEAD"])
        if result.returncode != 0:
            return None
        try:
            behind_str, ahead_str = result.stdout.strip().split()
            return int(behind_str), int(ahead_str)
        except ValueError:
            return None

    def list_conflict_files(self) -> list[str]:
        result = self.run(["diff", "--name-only", "--diff-filter=U"])
        return [f for f in result.stdout.strip().splitlines() if f]
