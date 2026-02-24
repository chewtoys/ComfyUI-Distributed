"""Cloudflare tunnel lifecycle manager."""

import asyncio
import os
import shutil
import signal
import subprocess
import time

from ..constants import TUNNEL_START_TIMEOUT
from ..logging import debug_log
from ..network import get_server_port, normalize_host
from ..process import is_process_alive, terminate_process
from .binary import ensure_binary
from .process_reader import ProcessReader
from .state import clear_tunnel_state, load_tunnel_state, persist_tunnel_state, resolve_restore_master_host


class CloudflareTunnelManager:
    def __init__(self):
        self.process = None
        self.pid = None
        self.public_url = None
        self.last_error = None
        self.log_file = None
        self.status = "stopped"
        self.previous_master_host = None

        self._lock = asyncio.Lock()
        self._reader = ProcessReader()
        self.binary_path = None

        self._restore_state()

    @property
    def base_dir(self):
        return os.path.dirname(os.path.dirname(os.path.dirname(__file__)))

    def _restore_state(self):
        state = load_tunnel_state()

        self.public_url = state.get("public_url") or None
        self.previous_master_host = state.get("previous_master_host")
        self.log_file = state.get("log_file")
        pid = state.get("pid")

        if pid and is_process_alive(pid):
            self.pid = pid
            self.status = state.get("status") or "running"
            debug_log(f"Detected existing cloudflared process (pid={pid})")
        else:
            clear_tunnel_state(log_file=self.log_file, previous_host=self.previous_master_host)
            self.status = "stopped"
            self.pid = None

    async def start_tunnel(self):
        async with self._lock:
            if self.process and self.process.poll() is None:
                return {
                    "status": self.status,
                    "public_url": self.public_url,
                    "pid": self.process.pid,
                    "log_file": self.log_file,
                }

            if self.pid and is_process_alive(self.pid):
                debug_log(f"Stopping stale cloudflared pid {self.pid} before starting a new one")
                await self.stop_tunnel()

            binary = await asyncio.to_thread(ensure_binary)
            self.binary_path = binary
            port = get_server_port()
            self.status = "starting"
            self.last_error = None
            self.public_url = None

            state = load_tunnel_state()
            master_host = state.get("master_host") or ""
            if state.get("previous_master_host"):
                self.previous_master_host = state.get("previous_master_host")
            else:
                self.previous_master_host = master_host

            os.makedirs(os.path.join(self.base_dir, "logs"), exist_ok=True)
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            self.log_file = os.path.join(self.base_dir, "logs", f"cloudflare-{timestamp}.log")

            cmd = [
                binary,
                "tunnel",
                "--no-autoupdate",
                "--url",
                f"http://127.0.0.1:{port}",
            ]

            debug_log(f"Starting cloudflared: {' '.join(cmd)}")
            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except FileNotFoundError:
                self.status = "error"
                raise RuntimeError("cloudflared binary not found")
            except Exception as exc:
                self.status = "error"
                raise RuntimeError(f"Failed to start cloudflared: {exc}") from exc

            self.pid = self.process.pid
            persist_tunnel_state(
                status="starting",
                pid=self.pid,
                log_file=self.log_file,
                previous_host=self.previous_master_host,
            )

            loop = asyncio.get_running_loop()
            self._reader.set_log_file(self.log_file)
            self._reader.start(self.process, loop)

            try:
                await self._reader.wait_for_url(timeout=TUNNEL_START_TIMEOUT)
            except asyncio.TimeoutError:
                self.last_error = "Timed out waiting for Cloudflare to assign a URL"
                await self.stop_tunnel()
                raise RuntimeError(self.last_error)

            public_url = self._reader.get_url()
            if not public_url:
                self.last_error = self._reader.get_last_error() or "Cloudflare tunnel failed to start"
                await self.stop_tunnel()
                raise RuntimeError(self.last_error)

            self.public_url = public_url
            self.status = "running"
            debug_log(f"Cloudflare tunnel ready at {self.public_url}")

            persist_tunnel_state(
                status="running",
                public_url=self.public_url,
                pid=self.pid,
                log_file=self.log_file,
                previous_host=self.previous_master_host or "",
                master_host=normalize_host(self.public_url),
            )
            return {
                "status": self.status,
                "public_url": self.public_url,
                "pid": self.pid,
                "log_file": self.log_file,
            }

    async def stop_tunnel(self):
        async with self._lock:
            pid = self.process.pid if self.process else self.pid
            if not pid:
                clear_tunnel_state(log_file=self.log_file, previous_host=self.previous_master_host)
                self.status = "stopped"
                return {"status": "stopped"}

            debug_log(f"Stopping cloudflared (pid={pid})")
            if self.process:
                terminate_process(self.process, timeout=5)
            else:
                try:
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(0.5)
                except Exception as exc:  # pragma: no cover
                    debug_log(f"Error stopping cloudflared pid {pid}: {exc}")

            restore_host = resolve_restore_master_host(self.previous_master_host)

            self.status = "stopped"
            self.public_url = None
            self.pid = None
            self.process = None
            self.last_error = None
            self._reader.stop()

            clear_tunnel_state(
                log_file=self.log_file,
                previous_host=self.previous_master_host,
                master_host=restore_host,
            )
            return {"status": "stopped"}

    def get_status(self):
        alive = False
        pid = self.process.pid if self.process else self.pid
        if pid:
            alive = is_process_alive(pid)
            if not alive and self.status == "running":
                self.status = "stopped"

        return {
            "status": self.status,
            "public_url": self.public_url,
            "pid": pid,
            "log_file": self.log_file,
            "last_error": self.last_error or self._reader.get_last_error(),
            "binary_path": self.binary_path or shutil.which("cloudflared"),
            "recent_logs": self._reader.get_recent_logs()[-20:],
            "previous_master_host": self.previous_master_host,
        }
