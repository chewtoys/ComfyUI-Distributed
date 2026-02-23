import asyncio
import os
import socket
import json
import platform
import uuid
from multiprocessing import Queue

import aiohttp

from ..utils.network import normalize_host, get_client_session
from ..utils.logging import debug_log, log
from ..utils.config import load_config


async def is_local_worker(worker_config):
    """Check if a worker is running on the same machine as the master."""
    host = normalize_host(worker_config.get('host', 'localhost')) or 'localhost'
    if host in ['localhost', '127.0.0.1', '0.0.0.0', ''] or worker_config.get('type') == 'local':
        return True
    
    # For cloud workers, check if on same physical host
    if worker_config.get('type') == 'cloud':
        return await is_same_physical_host(worker_config)
    
    return False

async def is_same_physical_host(worker_config):
    """Compare machine IDs to determine if worker is on same physical host."""
    try:
        # Get master machine ID
        master_machine_id = get_machine_id()
        
        # Fetch worker's machine ID via API
        host = normalize_host(worker_config.get('host', 'localhost')) or 'localhost'
        port = worker_config.get('port', 8188)
        
        session = await get_client_session()
        async with session.get(
            f"http://{host}:{port}/distributed/system_info",
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                worker_machine_id = data.get('machine_id')
                return worker_machine_id == master_machine_id
            else:
                debug_log(f"Failed to get system info from worker: HTTP {resp.status}")
                return False
    except Exception as e:
        debug_log(f"Error checking same physical host: {e}")
        return False

def get_machine_id():
    """Get a unique identifier for this machine."""
    # Try multiple methods to get a stable machine ID
    try:
        # Method 1: MAC address-based UUID
        return str(uuid.getnode())
    except Exception:
        try:
            # Method 2: Platform + hostname
            import socket
            return f"{platform.machine()}_{socket.gethostname()}"
        except Exception:
            # Fallback
            return platform.machine()

def is_docker_environment():
    """Check if running inside Docker container."""
    return (os.path.exists('/.dockerenv') or 
            os.environ.get('DOCKER_CONTAINER', False) or
            'docker' in platform.node().lower())

def is_runpod_environment():
    """Check if running in Runpod environment."""
    return (os.environ.get('RUNPOD_POD_ID') is not None or
            os.environ.get('RUNPOD_API_KEY') is not None)

async def get_comms_channel(worker_id, worker_config):
    """Get communication channel for a worker (HTTP URL or IPC Queue)."""
    if await is_local_worker(worker_config):
        comm_mode = worker_config.get('communicationMode', 'http')
        
        if comm_mode == 'ipc':
            # Use multiprocessing Queue for IPC
            from . import get_worker_manager
            wm = get_worker_manager()
            if worker_id not in wm.queues:
                wm.queues[worker_id] = Queue()
            return wm.queues[worker_id]
        elif is_docker_environment():
            # Docker: use host.docker.internal
            return f"http://host.docker.internal:{worker_config['port']}"
        else:
            # Local: use loopback
            return f"http://127.0.0.1:{worker_config['port']}"
    elif worker_config.get('type') == 'cloud' and is_runpod_environment():
        # Runpod same-host optimization (if detected)
        host = normalize_host(worker_config.get('host', 'localhost')) or 'localhost'
        return f"http://{host}:{worker_config['port']}"
    else:
        # Remote worker: use configured endpoint
        host = normalize_host(worker_config.get('host', 'localhost')) or 'localhost'
        return f"http://{host}:{worker_config['port']}"
