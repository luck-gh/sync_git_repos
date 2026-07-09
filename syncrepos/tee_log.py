"""把终端输出同时记进日志文件的小工具。

需求: print 到终端的所有信息都要落进日志。做法是包一层 sys.stdout / sys.stderr,
写入时一路给终端、一路给日志文件。

日志侧按行缓冲并处理 \\r 覆盖:
    - 遇 \\r 丢弃本行已写内容(模拟终端上的"回到行首覆盖")
    - 遇 \\n 才把当前行落盘
这样进度条那种反复刷新的行, 在日志里只会留下最终那一帧(例如 N/N 完成),
而不是把每一次刷新都记成一大堆重复行。
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO


class _TeeStream:
    """一路写终端、一路写日志文件的流包装。终端侧原样输出, 文件侧按行缓冲(见模块说明)。"""

    def __init__(self, terminal: TextIO, file_handle: TextIO):
        self._terminal = terminal
        self._file = file_handle
        self._line = ""

    def write(self, text: str) -> int:
        self._terminal.write(text)
        self._to_file(text)
        return len(text)

    def _to_file(self, text: str) -> None:
        for ch in text:
            if ch == "\r":
                self._line = ""
            elif ch == "\n":
                self._file.write(self._line + "\n")
                self._file.flush()
                self._line = ""
            else:
                self._line += ch

    def flush(self) -> None:
        self._terminal.flush()
        self._file.flush()

    def __getattr__(self, name):
        # isatty / encoding 等其余属性一律委托给真实终端流
        return getattr(self._terminal, name)


@contextmanager
def tee_to_file(log_file: Path):
    """上下文内, 所有写往 sys.stdout / sys.stderr 的内容都会同时写入 log_file。

    退出时恢复原来的 stdout / stderr, 并关闭日志文件。
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "w", encoding="utf-8") as fh:
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = _TeeStream(old_out, fh)  # type: ignore[assignment]
        sys.stderr = _TeeStream(old_err, fh)  # type: ignore[assignment]
        try:
            yield
        finally:
            sys.stdout, sys.stderr = old_out, old_err
