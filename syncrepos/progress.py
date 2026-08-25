"""通用进度条: 多行同时刷新 + 卡住时的倒计时。

从 scan 中拆出来单独成文, 因为它跟"扫 git 仓库"是不同抽象层:
进度条是可复用的展示组件, 不该和业务逻辑黏在一个类里。

两个角色:
  - ProgressBar   单个进度条的"数据 + 一行文本": done/total、在途任务的截止时间,
                  以及把这些格式化成 "prefix [####----] done/total  最长约还需 Ns" 的能力。
                  它自己不做任何终端 IO。
  - MultiProgress 多个 ProgressBar 的"渲染器": 把它们作为一个多行块渲染到终端,
                  起一个后台 ticker 线程每约 1s 重绘一次, 让卡住时倒计时持续走动。
                  这样"检查"和"同步"两个条可以同时刷新(各占一行)。

两个能力对应的诉求:
  1. 检查与同步同时进行 -> 两个条同时在两行上刷新(MultiProgress 用 ANSI 光标上移重绘整块)。
  2. 卡住时想知道还要多久 -> 网络任务登记截止时间(now + timeout), 显示最长剩余时间；
     到点后明确显示"超时收尾中"。没有硬超时的本地任务则显示仓库名和实际已用时间。

不写日志: 进度条会反复刷新, 若进日志会留下大量重复行。所以统一写 sys.__stderr__
(真实 stderr, 不受 tee_log 接管), 日志里一行进度条都不会有。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import TextIO


def _err_stream() -> TextIO:
    """真实 stderr(不被 tee_log 接管的那个), 拿不到就退回当前 sys.stderr。"""
    return sys.__stderr__ or sys.stderr


class ProgressBar:
    """单个进度条: 持有 done/total 与在途任务截止时间, 能把自己格式化成一行文本。不做终端 IO。"""

    def __init__(self, total: int, prefix: str = "检查", width: int = 30):
        self.total = total
        self.prefix = prefix
        self.width = width
        self.done = 0
        self._deadlines: dict[object, float] = {}   # key -> 截止时间(time.monotonic 值)
        self._started: dict[object, float] = {}     # key -> 开始时间, 本地长操作显示耗时
        self._lock = threading.Lock()
        self._on_change = None                       # 数据变化时通知渲染器立刻重绘

    # ----- 任务登记 / 推进(worker 线程调用) ---------------------------

    def register(self, key: object, timeout: float | None) -> None:
        """登记一个在途任务的截止时间(now + timeout), 用于倒计时。

        timeout 为 None 表示该任务不参与倒计时(比如 dry-run 不走网络)。
        应在任务"真正开始执行"时调用(而非提交排队时), 倒计时才准确。
        """
        now = time.monotonic()
        with self._lock:
            self._started[key] = now
            if timeout is not None:
                self._deadlines[key] = now + timeout
        self._changed()

    def advance(self, key: object | None = None) -> None:
        """完成一个任务: done +1, 并注销它的截止时间。"""
        with self._lock:
            self.done += 1
            if key is not None:
                self._deadlines.pop(key, None)
                self._started.pop(key, None)
        self._changed()

    def add_total(self, n: int = 1) -> None:
        """把 total 增大 n。用于"同步"条: 开跑时不知道有多少仓库需要同步,
        检查过程中每发现一个需要同步的仓库就 +1, 进度条的分母随之增长。
        """
        with self._lock:
            self.total += n
        self._changed()

    def _changed(self) -> None:
        # 在锁外调用, 避免与 format_line 里的取锁重入
        cb = self._on_change
        if cb is not None:
            cb()

    # ----- 格式化(渲染器调用) ------------------------------------------

    def format_line(self, final: bool = False) -> str:
        with self._lock:
            done = self.done
            # total 为 0(同步条起始还没发现要同步的仓库)时显示空条, 而非误导性的满条
            ratio = (done / self.total) if self.total else (1.0 if done else 0.0)
            filled = int(self.width * ratio)
            bar = "#" * filled + "-" * (self.width - filled)

            suffix = ""
            if not final and done < self.total and self._deadlines:
                deadline_key, deadline = max(self._deadlines.items(), key=lambda item: item[1])
                remaining = deadline - time.monotonic()
                if remaining <= 0.5:
                    suffix = f"  超时收尾中: {self._key_label(deadline_key)}"
                else:
                    suffix = f"  最长约还需 {remaining:4.0f}s"
            elif not final and done < self.total and self._started:
                active_key, started = min(self._started.items(), key=lambda item: item[1])
                elapsed = max(0.0, time.monotonic() - started)
                suffix = f"  正在处理: {self._key_label(active_key)} (已 {elapsed:.0f}s)"
        return f"{self.prefix} [{bar}] {done}/{self.total}{suffix}"

    @staticmethod
    def _key_label(key: object) -> str:
        name = getattr(key, "name", None)
        return str(name if name else key)


class MultiProgress:
    """把多个 ProgressBar 作为一个多行块渲染到终端, 后台 ticker 定时重绘(卡住时倒计时走动)。

    作为上下文管理器使用:
        with MultiProgress([check_bar, sync_bar]):
            ...worker 线程里调用 bar.register / bar.advance...
    """

    def __init__(self, bars: list[ProgressBar], tick_interval: float = 1.0):
        self.bars = bars
        self.tick_interval = tick_interval
        self._out = _err_stream()
        self._active = self._out.isatty()   # 非 tty(重定向)时不做 ANSI 重绘, 免得写入乱码
        self._render_lock = threading.Lock()
        self._stop = threading.Event()
        self._ticker: threading.Thread | None = None

    def __enter__(self) -> "MultiProgress":
        if os.name == "nt":
            os.system("")   # 在 Windows 10+ 控制台上启用 ANSI 转义处理
        for b in self.bars:
            b._on_change = self._redraw
        if self._active:
            # 先占好 len(bars) 行, 之后每次重绘都光标上移这么多行覆盖
            for b in self.bars:
                self._out.write(b.format_line() + "\n")
            self._out.flush()
            self._ticker = threading.Thread(target=self._tick_loop, daemon=True)
            self._ticker.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._ticker is not None:
            self._ticker.join(timeout=self.tick_interval + 0.5)
            self._ticker = None
        for b in self.bars:
            b._on_change = None
        self._redraw(final=True)

    def _tick_loop(self) -> None:
        # 每隔 tick_interval 重绘一次; __exit__ 里 set 事件后 wait 立即返回, 循环结束
        while not self._stop.wait(self.tick_interval):
            self._redraw()

    def _redraw(self, final: bool = False) -> None:
        if not self._active:
            return
        n = len(self.bars)
        with self._render_lock:
            # 光标上移 n 行到块顶, 逐行 \r 回行首 + 清到行尾 + 重写, 每行以 \n 结束回到块底
            self._out.write(f"\033[{n}A")
            for b in self.bars:
                self._out.write("\r\033[K" + b.format_line(final=final) + "\n")
            self._out.flush()
