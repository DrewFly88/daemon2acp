"""atomcode-daemon 子进程管理 — 自动拉起、健康检查、优雅关闭

使用方式：
    launcher = DaemonLauncher(port=13456)
    launcher.start_sync()        # 同步启动（推荐，在 web.run_app 之前调用）
    ...
    launcher.stop_sync()         # 同步关闭

启动命令：atomcode daemon --port <port> [--client <client>] [--no-telemetry]
注意：atomcode daemon 不支持 --host，始终绑定 127.0.0.1
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import time as _time
import urllib.request
from typing import Any

logger = logging.getLogger("daemon2acp.launcher")


class DaemonLauncher:
    """atomcode-daemon 子进程管理器

    使用 subprocess.Popen（而非 asyncio.create_subprocess_exec），
    避免子进程与 aiohttp 事件循环的生命周期冲突。
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 13456,
        client: str = "daemon2acp",
        idle_timeout: int = 0,
        no_telemetry: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.client = client
        self.idle_timeout = idle_timeout
        self.no_telemetry = no_telemetry

        self._process: subprocess.Popen | None = None
        self._ready = False

        # atomcode 可执行文件路径
        self._atomcode_bin = os.environ.get(
            "ATOMCODE_BIN",
            self._find_atomcode_bin(),
        )

    # ============================================================
    # 查找 atomcode 可执行文件
    # ============================================================

    @staticmethod
    def _find_atomcode_bin() -> str:
        """在 PATH 和常见安装路径中查找 atomcode"""
        # 1. PATH 中查找
        found = shutil.which("atomcode")
        if found:
            return found

        # 2. Windows 常见安装路径
        if os.name == "nt":
            common_paths = [
                os.path.expandvars(r"D:\AtomCode\atomcode.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\AtomCode\atomcode.exe"),
                os.path.expandvars(r"%PROGRAMFILES%\AtomCode\atomcode.exe"),
                os.path.expandvars(r"%USERPROFILE%\.atomcode\bin\atomcode.exe"),
            ]
            for p in common_paths:
                if os.path.isfile(p):
                    return p

        # 3. Unix 常见路径
        common_paths = [
            os.path.expanduser("~/.local/bin/atomcode"),
            os.path.expanduser("~/.atomcode/bin/atomcode"),
            "/usr/local/bin/atomcode",
        ]
        for p in common_paths:
            if os.path.isfile(p):
                return p

        return "atomcode"  # 回退到 PATH 查找

    # ============================================================
    # 属性
    # ============================================================

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def pid(self) -> int | None:
        """子进程 PID，未启动时返回 None"""
        if self._process is not None:
            return self._process.pid
        return None

    @property
    def is_running(self) -> bool:
        """子进程是否正在运行

        atomcode daemon 会 fork：父进程立即退出，子进程才是真正的 daemon。
        所以不能只看 poll()，还要看端口是否在监听。
        """
        if self._process is None:
            return False
        # 父进程可能已退出（fork），但 daemon 子进程还在
        if self._process.poll() is None:
            return True  # 父进程还活着
        # 父进程已退出（fork），检查端口
        return self._check_port()

    def _check_port(self) -> bool:
        """检查 daemon 端口是否在监听"""
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", self.port))
            s.close()
            return True
        except Exception:
            return False

    # ============================================================
    # 启动命令构建
    # ============================================================

    def _build_cmd(self) -> list[str]:
        """构建启动命令行

        atomcode daemon 支持的参数：
            --port <PORT>
            --client <CLIENT>
            --idle-timeout <SECONDS>
            --no-telemetry
        注意：atomcode daemon 没有 --host 参数，始终绑定 127.0.0.1
        """
        cmd = [
            self._atomcode_bin,
            "daemon",
            "--port", str(self.port),
            "--client", self.client,
        ]
        if self.idle_timeout > 0:
            cmd += ["--idle-timeout", str(self.idle_timeout)]
        if self.no_telemetry:
            cmd.append("--no-telemetry")
        return cmd

    # ============================================================
    # 启动
    # ============================================================

    def start_sync(self, timeout: float = 15.0) -> None:
        """启动 atomcode daemon 子进程并等待就绪

        使用 subprocess.Popen，在 web.run_app() 之前调用。
        同步阻塞，直到 daemon /health 返回 200 或超时。

        Args:
            timeout: 等待 daemon 就绪的最大秒数

        Raises:
            RuntimeError: atomcode 未找到或 daemon 启动后意外退出
            TimeoutError: daemon 在超时内未就绪
        """
        if self.is_running:
            logger.info("daemon already running (pid=%s)", self._process.pid)
            return

        cmd = self._build_cmd()
        logger.info("starting atomcode daemon: %s", " ".join(cmd))

        try:
            # Windows: 用 STARTUPINFO 隐藏控制台窗口
            # 不用 DEVNULL — atomcode daemon 可能检测 stdout 是否为 TTY
            if os.name == "nt":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE
                self._process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    startupinfo=si,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                self._process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            # 启动后台线程排空 stdout/stderr 防止管道阻塞
            self._drain_pipes()
        except FileNotFoundError:
            raise RuntimeError(
                f"atomcode not found at '{self._atomcode_bin}'. "
                f"Set ATOMCODE_BIN environment variable to the full path."
            )

    def _drain_pipes(self) -> None:
        """启动后台线程排空 stdout/stderr，防止管道缓冲区满导致 daemon 阻塞"""
        import threading
        for stream_name in ("stdout", "stderr"):
            stream = getattr(self._process, stream_name)
            if stream is not None:
                t = threading.Thread(
                    target=self._drain_stream,
                    args=(stream, stream_name),
                    daemon=True,
                )
                t.start()

    @staticmethod
    def _drain_stream(stream, name: str) -> None:
        """持续读取流直到 EOF，丢弃所有内容（防止管道缓冲区满导致 daemon 阻塞）"""
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
        except Exception:
            pass

    # ============================================================
    # 关闭
    # ============================================================

    def stop_sync(self, timeout: float = 5.0) -> None:
        """优雅关闭 daemon 子进程

        atomcode daemon 会 fork：父进程立即退出，子进程才是真正的 daemon。
        所以不能只靠 Popen.wait()，还要检查端口是否释放。

        依次尝试：
        1. POST /shutdown（graceful）
        2. 等待端口释放
        3. 按端口查找进程并 SIGTERM
        4. 按端口查找进程并 SIGKILL
        """
        if not self.is_running:
            logger.debug("stop_sync: daemon not running, nothing to do")
            self._cleanup()
            return

        pid = self.pid
        logger.info("stopping atomcode daemon (pid=%s) ...", pid)

        # 1. 尝试 graceful shutdown
        try:
            req = urllib.request.Request(
                f"{self.base_url}/shutdown",
                data=b"",
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3):
                pass
            logger.debug("POST /shutdown sent")
        except Exception:
            logger.debug("POST /shutdown failed, will wait for port release")

        # 2. 等待端口释放
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            if not self._check_port():
                logger.info("daemon stopped gracefully (port %s released)", self.port)
                self._cleanup()
                return
            _time.sleep(0.3)

        # 3. 按端口查找进程并 SIGTERM
        logger.warning("daemon port still open, trying to kill by port ...")
        self._kill_by_port(graceful=True)

        deadline = _time.time() + 3
        while _time.time() < deadline:
            if not self._check_port():
                logger.info("daemon stopped after SIGTERM (port %s released)", self.port)
                self._cleanup()
                return
            _time.sleep(0.3)

        # 4. 强制杀死
        logger.warning("daemon still running, force killing ...")
        self._kill_by_port(graceful=False)
        _time.sleep(1)
        logger.info("daemon killed (port %s status: %s)", self.port, "released" if not self._check_port() else "STILL OPEN")
        self._cleanup()

    def _kill_by_port(self, graceful: bool = True) -> None:
        """按端口号查找并杀死占用进程"""
        if os.name == "nt":
            # Windows: netstat -ano | findstr :PORT
            try:
                out = subprocess.check_output(
                    f'netstat -ano | findstr ":{self.port}"',
                    shell=True, text=True, timeout=5,
                )
                for line in out.strip().split("\n"):
                    parts = line.split()
                    if len(parts) >= 5 and parts[1].endswith(f":{self.port}"):
                        try:
                            pid = int(parts[-1])
                            if graceful:
                                subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, timeout=3)
                            else:
                                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=3)
                        except (ValueError, subprocess.TimeoutExpired):
                            pass
            except Exception:
                pass
        else:
            # Unix: lsof -ti :PORT
            try:
                out = subprocess.check_output(
                    ["lsof", "-ti", f":{self.port}"],
                    text=True, timeout=5,
                )
                for pid_str in out.strip().split("\n"):
                    try:
                        pid = int(pid_str)
                        sig = signal.SIGTERM if graceful else signal.SIGKILL
                        os.kill(pid, sig)
                    except (ValueError, ProcessLookupError):
                        pass
            except Exception:
                pass

    def _kill_process(self) -> None:
        """强制杀死子进程并清理"""
        if self._process is not None:
            try:
                self._process.kill()
                self._process.wait(timeout=3)
            except Exception:
                pass
            self._cleanup()

    def _cleanup(self) -> None:
        """重置内部状态"""
        self._process = None
        self._ready = False

    # ============================================================
    # 重启
    # ============================================================

    def restart_sync(self, timeout: float = 15.0) -> None:
        """重启 daemon"""
        self.stop_sync()
        self.start_sync(timeout)
