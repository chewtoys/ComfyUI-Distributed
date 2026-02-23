import json
import asyncio
import os
import time
import platform
import subprocess

import torch
import aiohttp
from aiohttp import web
import server

from ..utils.config import load_config
from ..utils.logging import debug_log, log
from ..utils.network import handle_api_error, normalize_host, get_client_session
from ..utils.constants import CHUNK_SIZE
from ..workers import get_worker_manager
from ..workers.detection import (
    is_local_worker,
    is_same_physical_host,
    get_machine_id,
    is_docker_environment,
    is_runpod_environment,
    get_comms_channel,
)
from ..utils.async_helpers import queue_prompt_payload


@server.PromptServer.instance.routes.get("/distributed/worker_ws")
async def worker_ws_endpoint(request):
    """WebSocket endpoint for worker prompt dispatch."""
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            try:
                data = json.loads(msg.data or "{}")
            except json.JSONDecodeError:
                await ws.send_json({
                    "type": "dispatch_ack",
                    "request_id": None,
                    "ok": False,
                    "error": "Invalid JSON payload.",
                })
                continue

            if data.get("type") != "dispatch_prompt":
                await ws.send_json({
                    "type": "dispatch_ack",
                    "request_id": data.get("request_id"),
                    "ok": False,
                    "error": "Unsupported websocket message type.",
                })
                continue

            prompt = data.get("prompt")
            if not isinstance(prompt, dict):
                await ws.send_json({
                    "type": "dispatch_ack",
                    "request_id": data.get("request_id"),
                    "ok": False,
                    "error": "Field 'prompt' must be an object.",
                })
                continue

            try:
                prompt_id = await queue_prompt_payload(
                    prompt,
                    workflow_meta=data.get("workflow"),
                    client_id=data.get("client_id"),
                )
                await ws.send_json({
                    "type": "dispatch_ack",
                    "request_id": data.get("request_id"),
                    "ok": True,
                    "prompt_id": prompt_id,
                })
            except Exception as exc:
                await ws.send_json({
                    "type": "dispatch_ack",
                    "request_id": data.get("request_id"),
                    "ok": False,
                    "error": str(exc),
                })
        elif msg.type == aiohttp.WSMsgType.ERROR:
            log(f"[Distributed] Worker websocket error: {ws.exception()}")

    return ws


@server.PromptServer.instance.routes.post("/distributed/worker/clear_launching")
async def clear_launching_state(request):
    """Clear the launching flag when worker is confirmed running."""
    try:
        wm = get_worker_manager()
        data = await request.json()
        worker_id = str(data.get('worker_id'))
        
        if not worker_id:
            return await handle_api_error(request, "worker_id is required", 400)
        
        # Clear launching flag in managed processes
        if worker_id in wm.processes:
            if 'launching' in wm.processes[worker_id]:
                del wm.processes[worker_id]['launching']
                wm.save_processes()
                debug_log(f"Cleared launching state for worker {worker_id}")
        
        return web.json_response({"status": "success"})
    except Exception as e:
        return await handle_api_error(request, e, 500)

@server.PromptServer.instance.routes.get("/distributed/network_info")
async def get_network_info_endpoint(request):
    """Get network interfaces and recommend best IP for master."""
    import socket
    
    # Get CUDA device if available
    cuda_device = None
    cuda_device_count = 0
    physical_device_count = 0
    
    if torch.cuda.is_available():
        try:
            import os
            import subprocess
            
            # Get visible device count (what PyTorch sees)
            cuda_device_count = torch.cuda.device_count()
            
            # Try to get actual physical device info
            cuda_visible = os.environ.get('CUDA_VISIBLE_DEVICES', '')
            
            # Method 1: Parse CUDA_VISIBLE_DEVICES
            if cuda_visible and cuda_visible.strip():
                visible_devices = [int(d.strip()) for d in cuda_visible.split(',') if d.strip().isdigit()]
                if visible_devices:
                    # Get the first visible device as the actual physical device
                    cuda_device = visible_devices[0]
                    
                    # Try to get total physical device count using nvidia-smi
                    try:
                        result = subprocess.run(['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'], 
                                              capture_output=True, text=True, timeout=5)
                        if result.returncode == 0:
                            physical_device_count = len(result.stdout.strip().split('\n'))
                        else:
                            physical_device_count = max(visible_devices) + 1  # Best guess
                    except (FileNotFoundError, OSError, subprocess.SubprocessError):
                        physical_device_count = max(visible_devices) + 1  # Best guess
                else:
                    cuda_device = 0
                    physical_device_count = cuda_device_count
            else:
                # No CUDA_VISIBLE_DEVICES set, current device is actual device
                cuda_device = torch.cuda.current_device()
                physical_device_count = cuda_device_count
                
        except Exception as e:
            debug_log(f"CUDA detection error: {e}")
            cuda_device = None
            cuda_device_count = 0
            physical_device_count = 0
    
    def get_network_ips():
        """Get all network IPs, trying multiple methods."""
        ips = []
        hostname = socket.gethostname()
        
        # Method 1: Try socket.getaddrinfo
        try:
            addr_info = socket.getaddrinfo(hostname, None)
            for info in addr_info:
                ip = info[4][0]
                if ip and ip not in ips and not ip.startswith('::'):  # Skip IPv6 for now
                    ips.append(ip)
        except (socket.gaierror, OSError):
            pass
        
        # Method 2: Try to connect to external server and get local IP
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))  # Google DNS
            local_ip = s.getsockname()[0]
            s.close()
            if local_ip not in ips:
                ips.append(local_ip)
        except (OSError, socket.error):
            pass
        
        # Method 3: Platform-specific commands
        
        try:
            if platform.system() == "Windows":
                # Windows ipconfig
                result = subprocess.run(["ipconfig"], capture_output=True, text=True)
                lines = result.stdout.split('\n')
                for i, line in enumerate(lines):
                    if 'IPv4' in line and i + 1 < len(lines):
                        ip = lines[i].split(':')[-1].strip()
                        if ip and ip not in ips:
                            ips.append(ip)
            else:
                # Unix/Linux/Mac ifconfig or ip addr
                try:
                    result = subprocess.run(["ip", "addr"], capture_output=True, text=True)
                except (FileNotFoundError, OSError):
                    try:
                        result = subprocess.run(["ifconfig"], capture_output=True, text=True)
                    except (FileNotFoundError, OSError):
                        result = None
                
                import re
                ip_pattern = re.compile(r'inet\s+(\d+\.\d+\.\d+\.\d+)')
                if result is not None:
                    for match in ip_pattern.finditer(result.stdout):
                        ip = match.group(1)
                        if ip and ip not in ips:
                            ips.append(ip)
        except (OSError, subprocess.SubprocessError):
            pass
        
        return ips
    
    def get_recommended_ip(ips):
        """Choose the best IP for master-worker communication."""
        # Priority order:
        # 1. Private network ranges (192.168.x.x, 10.x.x.x, 172.16-31.x.x)
        # 2. Other non-localhost IPs
        # 3. Localhost as last resort
        
        private_ips = []
        public_ips = []
        
        for ip in ips:
            if ip.startswith('127.') or ip == 'localhost':
                continue
            elif (ip.startswith('192.168.') or 
                  ip.startswith('10.') or 
                  (ip.startswith('172.') and 16 <= int(ip.split('.')[1]) <= 31)):
                private_ips.append(ip)
            else:
                public_ips.append(ip)
        
        # Prefer private IPs
        if private_ips:
            # Prefer 192.168 range as it's most common
            for ip in private_ips:
                if ip.startswith('192.168.'):
                    return ip
            return private_ips[0]
        elif public_ips:
            return public_ips[0]
        elif ips:
            return ips[0]
        else:
            return None
    
    try:
        hostname = socket.gethostname()
        all_ips = get_network_ips()
        recommended_ip = get_recommended_ip(all_ips)
        
        return web.json_response({
            "status": "success",
            "hostname": hostname,
            "all_ips": all_ips,
            "recommended_ip": recommended_ip,
            "cuda_device": cuda_device,
            "cuda_device_count": physical_device_count if physical_device_count > 0 else cuda_device_count,
            "message": "Auto-detected network configuration"
        })
    except Exception as e:
        return web.json_response({
            "status": "error",
            "message": str(e),
            "hostname": "unknown",
            "all_ips": [],
            "recommended_ip": None
        })

@server.PromptServer.instance.routes.get("/distributed/system_info")
async def get_system_info_endpoint(request):
    """Get system information including machine ID for local worker detection."""
    try:
        import socket
        
        return web.json_response({
            "status": "success",
            "hostname": socket.gethostname(),
            "machine_id": get_machine_id(),
            "platform": {
                "system": platform.system(),
                "machine": platform.machine(),
                "node": platform.node(),
                "path_separator": os.sep,  # Add path separator
                "os_name": os.name  # Add OS name (posix, nt, etc.)
            },
            "is_docker": is_docker_environment(),
            "is_runpod": is_runpod_environment(),
            "runpod_pod_id": os.environ.get('RUNPOD_POD_ID')
        })
    except Exception as e:
        return web.json_response({
            "status": "error",
            "message": str(e)
        }, status=500)

@server.PromptServer.instance.routes.post("/distributed/launch_worker")
async def launch_worker_endpoint(request):
    """Launch a worker process from the UI."""
    try:
        wm = get_worker_manager()
        data = await request.json()
        worker_id = data.get("worker_id")
        
        if not worker_id:
            return await handle_api_error(request, "Missing worker_id", 400)
        
        # Find worker config
        config = load_config()
        worker = next((w for w in config.get("workers", []) if w["id"] == worker_id), None)
        if not worker:
            return await handle_api_error(request, f"Worker {worker_id} not found", 404)
        
        # Ensure consistent string ID
        worker_id_str = str(worker_id)
        
        # Check if already running (managed by this instance)
        if worker_id_str in wm.processes:
            proc_info = wm.processes[worker_id_str]
            process = proc_info.get('process')
            
            # Check if still running
            is_running = False
            if process:
                is_running = process.poll() is None
            else:
                # Restored process without subprocess object
                is_running = wm._is_process_running(proc_info['pid'])
            
            if is_running:
                return web.json_response({
                    "status": "error",
                    "message": "Worker already running (managed by UI)",
                    "pid": proc_info['pid'],
                    "log_file": proc_info.get('log_file')
                }, status=409)
            else:
                # Process is dead, remove it
                del wm.processes[worker_id_str]
                wm.save_processes()
        
        # Launch the worker
        try:
            pid = wm.launch_worker(worker)
            log_file = wm.processes[worker_id_str].get('log_file')
            return web.json_response({
                "status": "success",
                "pid": pid,
                "message": f"Worker {worker['name']} launched",
                "log_file": log_file
            })
        except Exception as e:
            return await handle_api_error(request, f"Failed to launch worker: {str(e)}", 500)
            
    except Exception as e:
        return await handle_api_error(request, e, 400)


@server.PromptServer.instance.routes.post("/distributed/stop_worker")
async def stop_worker_endpoint(request):
    """Stop a worker process that was launched from the UI."""
    try:
        wm = get_worker_manager()
        data = await request.json()
        worker_id = data.get("worker_id")
        
        if not worker_id:
            return await handle_api_error(request, "Missing worker_id", 400)
        
        success, message = wm.stop_worker(worker_id)
        
        if success:
            return web.json_response({"status": "success", "message": message})
        else:
            return web.json_response({"status": "error", "message": message}, 
                                   status=404 if "not managed" in message else 409)
            
    except Exception as e:
        return await handle_api_error(request, e, 400)


@server.PromptServer.instance.routes.get("/distributed/managed_workers")
async def get_managed_workers_endpoint(request):
    """Get list of workers managed by this UI instance."""
    try:
        managed = get_worker_manager().get_managed_workers()
        return web.json_response({
            "status": "success",
            "managed_workers": managed
        })
    except Exception as e:
        return await handle_api_error(request, e, 500)


@server.PromptServer.instance.routes.get("/distributed/local-worker-status")
async def get_local_worker_status_endpoint(request):
    """Check status of all local workers (localhost/no host specified)."""
    try:
        config = load_config()
        worker_statuses = {}
        
        for worker in config.get("workers", []):
            # Only check local workers
            host = normalize_host(worker.get("host")) or ""
            if not host or host in ["localhost", "127.0.0.1"]:
                worker_id = worker["id"]
                port = worker["port"]
                
                # Check if worker is enabled
                if not worker.get("enabled", False):
                    worker_statuses[worker_id] = {
                        "online": False,
                        "enabled": False,
                        "processing": False,
                        "queue_count": 0
                    }
                    continue
                
                # Try to connect to worker
                try:
                    session = await get_client_session()
                    async with session.get(
                        f"http://localhost:{port}/prompt",
                        timeout=aiohttp.ClientTimeout(total=2)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            queue_remaining = data.get("exec_info", {}).get("queue_remaining", 0)
                            worker_statuses[worker_id] = {
                                "online": True,
                                "enabled": True,
                                "processing": queue_remaining > 0,
                                "queue_count": queue_remaining
                            }
                        else:
                            worker_statuses[worker_id] = {
                                "online": False,
                                "enabled": True,
                                "processing": False,
                                "queue_count": 0,
                                "error": f"HTTP {resp.status}"
                            }
                except asyncio.TimeoutError:
                    worker_statuses[worker_id] = {
                        "online": False,
                        "enabled": True,
                        "processing": False,
                        "queue_count": 0,
                        "error": "Timeout"
                    }
                except Exception as e:
                    worker_statuses[worker_id] = {
                        "online": False,
                        "enabled": True,
                        "processing": False,
                        "queue_count": 0,
                        "error": str(e)
                    }
        
        return web.json_response({
            "status": "success",
            "worker_statuses": worker_statuses
        })
    except Exception as e:
        debug_log(f"Error checking local worker status: {e}")
        return await handle_api_error(request, e, 500)


@server.PromptServer.instance.routes.get("/distributed/worker_log/{worker_id}")
async def get_worker_log_endpoint(request):
    """Get log content for a specific worker."""
    try:
        wm = get_worker_manager()
        worker_id = request.match_info['worker_id']
        
        # Ensure worker_id is string
        worker_id = str(worker_id)
        
        # Check if we manage this worker
        if worker_id not in wm.processes:
            return await handle_api_error(request, f"Worker {worker_id} not managed by UI", 404)
        
        proc_info = wm.processes[worker_id]
        log_file = proc_info.get('log_file')
        
        if not log_file or not os.path.exists(log_file):
            return await handle_api_error(request, "Log file not found", 404)
        
        # Read last N lines (or full file if small)
        lines_to_read = int(request.query.get('lines', 1000))
        
        try:
            # Get file size
            file_size = os.path.getsize(log_file)
            
            with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                if lines_to_read > 0 and file_size > 1024 * 1024:  # If file > 1MB and limited lines requested
                    # Read last N lines efficiently
                    lines = []
                    # Start from end and work backwards
                    f.seek(0, 2)  # Go to end
                    file_length = f.tell()
                    
                    # Read chunks from end
                    chunk_size = min(CHUNK_SIZE, file_length)
                    while len(lines) < lines_to_read and f.tell() > 0:
                        # Move back and read chunk
                        current_pos = max(0, f.tell() - chunk_size)
                        f.seek(current_pos)
                        chunk = f.read(chunk_size)
                        
                        # Process chunk
                        chunk_lines = chunk.splitlines()
                        if current_pos > 0:
                            # Partial line at beginning, combine with next chunk
                            chunk_lines = chunk_lines[1:]
                        
                        lines = chunk_lines + lines
                        
                        # Move back for next chunk
                        f.seek(current_pos)
                    
                    # Take only last N lines
                    content = '\n'.join(lines[-lines_to_read:])
                    truncated = len(lines) > lines_to_read
                else:
                    # Read entire file
                    content = f.read()
                    truncated = False
            
            return web.json_response({
                "status": "success",
                "content": content,
                "log_file": log_file,
                "file_size": file_size,
                "truncated": truncated,
                "lines_shown": lines_to_read if truncated else content.count('\n') + 1
            })
            
        except Exception as e:
            return await handle_api_error(request, f"Error reading log file: {str(e)}", 500)
            
    except Exception as e:
        return await handle_api_error(request, e, 500)
