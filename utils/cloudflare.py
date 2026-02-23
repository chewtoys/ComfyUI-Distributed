"""
Lightweight Cloudflare Tunnel manager.

Responsibilities:
- Locate or download the cloudflared binary for the current platform.
- Start/stop a quick tunnel pointing to the local ComfyUI server.
- Track status and persist minimal state in the shared config.
"""
import asyncio
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
from urllib import request, error as urlerror

from .config import load_config, save_config
from .logging import debug_log, log
from .network import get_server_port, normalize_host
from .process import is_process_alive, terminate_process

# Regex to capture the generated public URL from cloudflared output
PUBLIC_URL_PATTERN = re.compile(r"(https?://[\w.-]+\.(?:trycloudflare\.com|cloudflare\.dev))", re.IGNORECASE)

# Default timeout (seconds) to wait for a tunnel URL before giving up
TUNNEL_START_TIMEOUT = 25

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
        self._url_event = None
        self._reader_thread = None
        self._loop = None
        self._recent_logs = []

        self.binary_path = None
        self._restore_state()

    # --- Binary discovery & download ---
    @property
    def base_dir(self):
        return os.path.dirname(os.path.dirname(__file__))

    @property
    def bin_dir(self):
        return os.path.join(self.base_dir, "bin")

    def _get_asset_name(self):
        system = platform.system().lower()
        machine = platform.machine().lower()

        if system == "windows":
            if "arm" in machine:
                return "cloudflared-windows-arm64.exe"
            return "cloudflared-windows-amd64.exe"
        if system == "darwin":
            if machine in ("arm64", "aarch64"):
                return "cloudflared-darwin-arm64"
            return "cloudflared-darwin-amd64"
        if system == "linux":
            if machine in ("arm64", "aarch64"):
                return "cloudflared-linux-arm64"
            return "cloudflared-linux-amd64"

        raise RuntimeError(f"Unsupported platform for cloudflared: {system}/{machine}")

    def _download_cloudflared(self):
        asset = self._get_asset_name()
        url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}"
        os.makedirs(self.bin_dir, exist_ok=True)
        target_path = os.path.join(self.bin_dir, "cloudflared.exe" if asset.endswith(".exe") else "cloudflared")

        debug_log(f"Downloading cloudflared from {url}")
        try:
            with request.urlopen(url, timeout=30) as resp:
                with open(target_path, "wb") as f:
                    shutil.copyfileobj(resp, f)
        except urlerror.URLError as exc:
            raise RuntimeError(f"Failed to download cloudflared: {exc}") from exc

        # Make executable
        st = os.stat(target_path)
        os.chmod(target_path, st.st_mode | stat.S_IEXEC)
        debug_log(f"Downloaded cloudflared to {target_path}")
        return target_path

    def ensure_binary(self):
        # Allow explicit override
        env_path = os.environ.get("CLOUDFLARED_PATH")
        if env_path and os.path.exists(env_path):
            self.binary_path = env_path
            return env_path

        # Check cached path
        cached = self.binary_path
        if cached and os.path.exists(cached):
            return cached

        # Local bin directory
        candidate = os.path.join(self.bin_dir, "cloudflared.exe" if platform.system().lower() == "windows" else "cloudflared")
        if os.path.exists(candidate):
            self.binary_path = candidate
            return candidate

        # System PATH
        path_binary = shutil.which("cloudflared")
        if path_binary:
            self.binary_path = path_binary
            return path_binary

        # Download on demand
        self.binary_path = self._download_cloudflared()
        return self.binary_path

    # --- State helpers ---
    def _persist_state(self, status=None, public_url=None, pid=None, log_file=None, previous_host=None, master_host=None):
        cfg = load_config()
        tunnel_cfg = cfg.get("tunnel", {}) if isinstance(cfg.get("tunnel", {}), dict) else {}

        if status is not None:
            tunnel_cfg["status"] = status
        if public_url is not None:
            tunnel_cfg["public_url"] = public_url
        if pid is not None:
            tunnel_cfg["pid"] = pid
        if log_file is not None:
            tunnel_cfg["log_file"] = log_file
        if previous_host is not None:
            tunnel_cfg["previous_master_host"] = previous_host
        if master_host is not None:
            cfg.setdefault("master", {})["host"] = master_host

        cfg["tunnel"] = tunnel_cfg
        save_config(cfg)

    def _restore_state(self):
        cfg = load_config()
        tunnel_cfg = cfg.get("tunnel", {}) if isinstance(cfg.get("tunnel", {}), dict) else {}

        self.public_url = tunnel_cfg.get("public_url") or None
        self.previous_master_host = tunnel_cfg.get("previous_master_host")
        self.log_file = tunnel_cfg.get("log_file")
        pid = tunnel_cfg.get("pid")

        if pid and is_process_alive(pid):
            self.pid = pid
            self.status = tunnel_cfg.get("status", "running")
            debug_log(f"Detected existing cloudflared process (pid={pid})")
        else:
            # Clear stale info
            self._persist_state(status="stopped", public_url="", pid=None, log_file=None)
            self.status = "stopped"
            self.pid = None

    # --- Process output handling ---
    def _append_log(self, line):
        if self.log_file:
            try:
                with open(self.log_file, "a", encoding="utf-8", errors="replace") as f:
                    f.write(line + "\n")
            except Exception as exc:  # pragma: no cover - best effort logging
                debug_log(f"Failed to write tunnel log: {exc}")

        self._recent_logs.append(line)
        # Keep last ~200 lines to avoid unbounded growth
        if len(self._recent_logs) > 200:
            self._recent_logs = self._recent_logs[-200:]

    def _reader(self):
        assert self.process is not None
        loop = self._loop
        for raw_line in iter(self.process.stdout.readline, ""):
            line = raw_line.strip()
            if not line:
                continue

            self._append_log(line)
            match = PUBLIC_URL_PATTERN.search(line)
            if match and not self.public_url:
                self.public_url = match.group(1).rstrip("/")
                self.status = "running"
                if self._url_event and loop:
                    loop.call_soon_threadsafe(self._url_event.set)

            # Capture obvious error strings
            if "error" in line.lower() and not self.last_error:
                self.last_error = line

        # Process exited
        if self.status == "starting" and not self.public_url:
            self.status = "error"
            if not self.last_error:
                self.last_error = "Cloudflare tunnel exited before becoming ready"
            if self._url_event and loop:
                loop.call_soon_threadsafe(self._url_event.set)
        elif self.status == "running":
            # Treat unexpected exit as stopped so UI can retry
            self.status = "stopped"
        elif self.status == "stopped":
            # Normal stop path, nothing to do
            pass

    # --- Public API ---
    async def start_tunnel(self):
        async with self._lock:
            if self.process and self.process.poll() is None:
                return {
                    "status": self.status,
                    "public_url": self.public_url,
                    "pid": self.process.pid,
                    "log_file": self.log_file,
                }

            # If we had a stale pid, ensure it's not running
            if self.pid and is_process_alive(self.pid):
                debug_log(f"Stopping stale cloudflared pid {self.pid} before starting a new one")
                await self.stop_tunnel()

            binary = await asyncio.to_thread(self.ensure_binary)
            port = get_server_port()
            self.status = "starting"
            self.last_error = None
            self.public_url = None
            self._recent_logs = []

            # Remember current master host so we can restore it if the tunnel stops
            config = load_config()
            master_host = (config.get("master") or {}).get("host") or ""
            tunnel_cfg = config.get("tunnel") or {}
            if tunnel_cfg.get("previous_master_host"):
                self.previous_master_host = tunnel_cfg.get("previous_master_host")
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
            self._persist_state(
                status="starting",
                pid=self.pid,
                log_file=self.log_file,
                previous_host=self.previous_master_host
            )

            # Kick off background reader
            self._loop = asyncio.get_running_loop()
            self._url_event = asyncio.Event()
            self._reader_thread = threading.Thread(target=self._reader, daemon=True)
            self._reader_thread.start()

            try:
                await asyncio.wait_for(self._url_event.wait(), timeout=TUNNEL_START_TIMEOUT)
            except asyncio.TimeoutError:
                self.last_error = "Timed out waiting for Cloudflare to assign a URL"
                await self.stop_tunnel()
                raise RuntimeError(self.last_error)

            if self.status != "running" or not self.public_url:
                error_msg = self.last_error or "Cloudflare tunnel failed to start"
                await self.stop_tunnel()
                raise RuntimeError(error_msg)

            debug_log(f"Cloudflare tunnel ready at {self.public_url}")
            master_host = normalize_host(self.public_url)
            self._persist_state(
                status="running",
                public_url=self.public_url,
                pid=self.pid,
                log_file=self.log_file,
                previous_host=self.previous_master_host or "",
                master_host=master_host
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
                self._persist_state(status="stopped", public_url=None, pid=None, log_file=None)
                self.status = "stopped"
                return {"status": "stopped"}

            debug_log(f"Stopping cloudflared (pid={pid})")
            if self.process:
                terminate_process(self.process, timeout=5)
            else:
                try:
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(0.5)
                except Exception as exc:  # pragma: no cover - best effort
                    debug_log(f"Error stopping cloudflared pid {pid}: {exc}")

            config = load_config()
            tunnel_cfg = config.get("tunnel", {}) if isinstance(config.get("tunnel", {}), dict) else {}
            self.previous_master_host = tunnel_cfg.get("previous_master_host", self.previous_master_host)
            active_url = tunnel_cfg.get("public_url")
            current_master_host = (config.get("master") or {}).get("host")
            restore_host = None
            if active_url:
                active_host = normalize_host(active_url)
                current_host = normalize_host(current_master_host)
                if current_host == active_host:
                    restore_host = self.previous_master_host or ""

            self.status = "stopped"
            self.public_url = None
            self.pid = None
            self.process = None
            self.last_error = None
            self._url_event = None
            if self._reader_thread and self._reader_thread.is_alive():
                self._reader_thread.join(timeout=1)
            self._reader_thread = None
            self._persist_state(
                status="stopped",
                public_url="",
                pid=None,
                log_file=self.log_file,
                previous_host=self.previous_master_host,
                master_host=restore_host
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
            "last_error": self.last_error,
            "binary_path": self.binary_path or shutil.which("cloudflared"),
            "recent_logs": self._recent_logs[-20:],
            "previous_master_host": self.previous_master_host,
        }


# Singleton tunnel manager
cloudflare_tunnel_manager = CloudflareTunnelManager()
