"""
Network and API utilities for ComfyUI-Distributed.
"""
import aiohttp
import re
import server
from aiohttp import web
from .logging import debug_log

# Shared session for connection pooling
_client_session = None

async def get_client_session():
    """Get or create a shared aiohttp client session."""
    global _client_session
    if _client_session is None or _client_session.closed:
        connector = aiohttp.TCPConnector(limit=100, limit_per_host=30)
        # Don't set timeout here - set it per request
        _client_session = aiohttp.ClientSession(connector=connector)
    return _client_session

async def cleanup_client_session():
    """Clean up the shared client session."""
    global _client_session
    if _client_session and not _client_session.closed:
        await _client_session.close()
        _client_session = None

async def handle_api_error(request, error, status=500):
    """Standardized error response handler."""
    debug_log(f"API Error: {error}")
    return web.json_response({"status": "error", "message": str(error)}, status=status)

def get_server_port():
    """Get the ComfyUI server port."""
    import server
    return server.PromptServer.instance.port

def get_server_loop():
    """Get the ComfyUI server event loop."""
    import server
    return server.PromptServer.instance.loop


def normalize_host(value):
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    host = value.strip()
    if not host:
        return host
    host = re.sub(r"^https?://", "", host, flags=re.IGNORECASE)
    return host.split("/")[0]


def build_worker_url(worker, endpoint=""):
    """Construct the worker base URL with optional endpoint."""
    host = (worker.get("host") or "").strip()
    port = int(worker.get("port", worker.get("listen_port", 8188)) or 8188)

    if not host:
        host = getattr(server.PromptServer.instance, "address", "127.0.0.1") or "127.0.0.1"

    if host.startswith(("http://", "https://")):
        base = host.rstrip("/")
    else:
        is_cloud = worker.get("type") == "cloud" or host.endswith(".proxy.runpod.net") or port == 443
        scheme = "https" if is_cloud else "http"
        default_port = 443 if scheme == "https" else 80
        port_part = "" if port == default_port else f":{port}"
        base = f"{scheme}://{host}{port_part}"

    return f"{base}{endpoint}"
