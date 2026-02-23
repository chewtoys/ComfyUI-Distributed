import asyncio
import hashlib
import json
import mimetypes
import os
import re
import time
import uuid
from collections import deque

import aiohttp

import server

from ..utils.config import load_config
from ..utils.logging import debug_log, log
from ..utils.async_helpers import queue_prompt_payload
from ..utils.network import build_worker_url, get_client_session


prompt_server = server.PromptServer.instance

# JS parity audit (web/executionUtils.js:prepareApiPromptForParticipant) vs backend orchestration:
# - Path conversion by worker OS path separator: partially implemented below (_convert_paths_for_platform).
# - Worker prompt pruning to distributed-node upstream graph + PreviewImage injection: implemented below.
# - Seed/collector/upscale hidden input overrides: implemented in _override_* helpers.
# - Delegate-master pruning and placeholder injection: implemented in _prepare_delegate_master_prompt.
# - Remote media synchronization (load/check/upload image/video inputs): implemented below (_sync_worker_media).
# - Legacy frontend-only fallback path remains in web/executionUtils.js during transition.

LIKELY_FILENAME_RE = re.compile(
    r"\.(ckpt|safetensors|pt|pth|bin|yaml|json|png|jpg|jpeg|webp|gif|bmp|latent|txt|vae|lora|embedding)"
    r"(\s*\[\w+\])?$",
    re.IGNORECASE,
)
IMAGE_OR_VIDEO_RE = re.compile(
    r"\.(png|jpg|jpeg|webp|gif|bmp|mp4|avi|mov|mkv|webm)(\s*\[\w+\])?$",
    re.IGNORECASE,
)


def ensure_distributed_state():
    """Ensure prompt_server has the state used by distributed queue orchestration."""
    if not hasattr(prompt_server, "distributed_pending_jobs"):
        prompt_server.distributed_pending_jobs = {}
    if not hasattr(prompt_server, "distributed_jobs_lock"):
        prompt_server.distributed_jobs_lock = asyncio.Lock()


class PromptIndex:
    """Cache prompt metadata for faster worker/master prompt preparation."""

    def __init__(self, prompt_obj):
        self._prompt_json = json.dumps(prompt_obj)
        self.nodes_by_class = {}
        self.class_by_node = {}
        self.inputs_by_node = {}
        for node_id, node in _iter_prompt_nodes(prompt_obj):
            class_type = node.get("class_type")
            node_id_str = str(node_id)
            if class_type:
                self.nodes_by_class.setdefault(class_type, []).append(node_id_str)
            self.class_by_node[node_id_str] = class_type
            self.inputs_by_node[node_id_str] = node.get("inputs", {})
        self._upstream_cache = {}

    def copy_prompt(self):
        return json.loads(self._prompt_json)

    def nodes_for_class(self, class_name):
        return self.nodes_by_class.get(class_name, [])

    def has_upstream(self, start_node_id, target_class):
        cache_key = (str(start_node_id), target_class)
        if cache_key in self._upstream_cache:
            return self._upstream_cache[cache_key]

        visited = set()
        stack = [str(start_node_id)]
        while stack:
            node_id = stack.pop()
            if node_id in visited:
                continue
            visited.add(node_id)
            inputs = self.inputs_by_node.get(node_id, {})
            for value in inputs.values():
                if isinstance(value, list) and len(value) == 2:
                    upstream_id = str(value[0])
                    if self.class_by_node.get(upstream_id) == target_class:
                        self._upstream_cache[cache_key] = True
                        return True
                    if upstream_id in self.inputs_by_node:
                        stack.append(upstream_id)

        self._upstream_cache[cache_key] = False
        return False


def _iter_prompt_nodes(prompt_obj):
    for node_id, node in prompt_obj.items():
        if isinstance(node, dict):
            yield str(node_id), node


def _find_nodes_by_class(prompt_obj, class_name):
    nodes = []
    for node_id, node in _iter_prompt_nodes(prompt_obj):
        if node.get("class_type") == class_name:
            nodes.append(node_id)
    return nodes


def _find_downstream_nodes(prompt_obj, start_ids):
    """Return all nodes reachable downstream from the provided IDs."""
    adjacency = {}
    for node_id, node in _iter_prompt_nodes(prompt_obj):
        inputs = node.get("inputs", {})
        for value in inputs.values():
            if isinstance(value, list) and len(value) == 2:
                source_id = str(value[0])
                adjacency.setdefault(source_id, set()).add(str(node_id))

    connected = set(start_ids)
    queue = deque(start_ids)
    while queue:
        current = queue.popleft()
        for dependent in adjacency.get(current, ()):  # pragma: no branch - simple iteration
            if dependent not in connected:
                connected.add(dependent)
                queue.append(dependent)
    return connected


def _create_numeric_id_generator(prompt_obj):
    """Return a closure that yields new numeric string IDs."""
    max_id = 0
    for node_id in prompt_obj.keys():
        try:
            numeric = int(node_id)
        except (TypeError, ValueError):
            continue
        max_id = max(max_id, numeric)

    counter = max_id

    def _next_id():
        nonlocal counter
        counter += 1
        return str(counter)

    return _next_id


def _convert_paths_for_platform(obj, target_separator):
    """Recursively normalize likely file paths for the worker platform separator."""
    if target_separator not in ("/", "\\"):
        return obj

    def _convert(value):
        if isinstance(value, str):
            if ("/" in value or "\\" in value) and LIKELY_FILENAME_RE.search(value):
                trimmed = value.strip()
                has_drive = bool(re.match(r"^[A-Za-z]:(\\\\|/)", trimmed))
                is_absolute = trimmed.startswith("/") or trimmed.startswith("\\\\")
                has_protocol = bool(re.match(r"^\w+://", trimmed))

                # Keep relative media-style paths in forward-slash form (Comfy-style annotated paths).
                if not has_drive and not is_absolute and not has_protocol and IMAGE_OR_VIDEO_RE.search(trimmed):
                    return re.sub(r"[\\]+", "/", trimmed)

                if target_separator == "\\":
                    return re.sub(r"[\\/]+", r"\\", trimmed)
                return re.sub(r"[\\/]+", "/", trimmed)
            return value
        if isinstance(value, list):
            return [_convert(item) for item in value]
        if isinstance(value, dict):
            return {key: _convert(item) for key, item in value.items()}
        return value

    return _convert(obj)


def _find_upstream_nodes(prompt_obj, start_ids):
    """Return all nodes reachable upstream from start_ids, including start nodes."""
    connected = set(str(node_id) for node_id in start_ids)
    queue = deque(connected)
    while queue:
        node_id = queue.popleft()
        node = prompt_obj.get(node_id) or {}
        inputs = node.get("inputs", {})
        for value in inputs.values():
            if isinstance(value, list) and len(value) == 2:
                source_id = str(value[0])
                if source_id in prompt_obj and source_id not in connected:
                    connected.add(source_id)
                    queue.append(source_id)
    return connected


def _prune_prompt_for_worker(prompt_obj):
    """Prune worker prompt to distributed nodes and their upstream dependencies."""
    collector_ids = _find_nodes_by_class(prompt_obj, "DistributedCollector")
    upscale_ids = _find_nodes_by_class(prompt_obj, "UltimateSDUpscaleDistributed")
    distributed_ids = collector_ids + upscale_ids
    if not distributed_ids:
        return prompt_obj

    connected = _find_upstream_nodes(prompt_obj, distributed_ids)
    pruned_prompt = {}
    for node_id in connected:
        node = prompt_obj.get(node_id)
        if node is not None:
            pruned_prompt[node_id] = json.loads(json.dumps(node))

    next_id = _create_numeric_id_generator(pruned_prompt)
    for dist_id in distributed_ids:
        if dist_id not in pruned_prompt:
            continue
        downstream = _find_downstream_nodes(prompt_obj, [dist_id])
        has_removed_downstream = any(node_id != dist_id for node_id in downstream)
        if has_removed_downstream:
            preview_id = next_id()
            pruned_prompt[preview_id] = {
                "inputs": {
                    "images": [dist_id, 0],
                },
                "class_type": "PreviewImage",
                "_meta": {
                    "title": "Preview Image (auto-added)",
                },
            }

    return pruned_prompt


def _find_media_references(prompt_obj):
    """Find media file references in image/video inputs used by worker prompts."""
    media_refs = set()
    for _, node in _iter_prompt_nodes(prompt_obj):
        inputs = node.get("inputs", {})
        for key in ("image", "video"):
            value = inputs.get(key)
            if isinstance(value, str):
                cleaned = re.sub(r"\s*\[\w+\]$", "", value).strip().replace("\\", "/")
                if IMAGE_OR_VIDEO_RE.search(cleaned):
                    media_refs.add(cleaned)
    return sorted(media_refs)


def _load_media_file_sync(filename):
    """Load local media bytes and hash for worker upload sync."""
    import folder_paths

    full_path = folder_paths.get_annotated_filepath(filename)
    if not os.path.exists(full_path):
        raise FileNotFoundError(filename)

    with open(full_path, "rb") as f:
        file_bytes = f.read()

    file_hash = hashlib.md5(file_bytes).hexdigest()
    mime_type = mimetypes.guess_type(full_path)[0]
    if not mime_type:
        ext = os.path.splitext(full_path)[1].lower()
        if ext in {".mp4", ".avi", ".mov", ".mkv", ".webm"}:
            mime_type = "video/mp4"
        else:
            mime_type = "image/png"
    return file_bytes, file_hash, mime_type


async def _fetch_worker_path_separator(worker):
    """Best-effort fetch of a worker's path separator from /distributed/system_info."""
    url = build_worker_url(worker, "/distributed/system_info")
    session = await get_client_session()
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return None
            payload = await resp.json()
            separator = ((payload or {}).get("platform") or {}).get("path_separator")
            return separator if separator in ("/", "\\") else None
    except Exception as exc:
        debug_log(f"[Distributed] Failed to fetch worker system info ({worker.get('id')}): {exc}")
        return None


async def _upload_media_to_worker(worker, filename, file_bytes, file_hash, mime_type):
    """Upload one media file to worker iff missing or hash-mismatched."""
    session = await get_client_session()
    normalized = filename.replace("\\", "/")

    check_url = build_worker_url(worker, "/distributed/check_file")
    try:
        async with session.post(
            check_url,
            json={"filename": normalized, "hash": file_hash},
            timeout=aiohttp.ClientTimeout(total=6),
        ) as resp:
            if resp.status == 200:
                payload = await resp.json()
                if payload.get("exists") and payload.get("hash_matches"):
                    return False
    except Exception as exc:
        debug_log(f"[Distributed] Media check failed for '{normalized}' on worker {worker.get('id')}: {exc}")

    parts = normalized.split("/")
    clean_name = parts[-1]
    subfolder = "/".join(parts[:-1])

    form = aiohttp.FormData()
    form.add_field("image", file_bytes, filename=clean_name, content_type=mime_type)
    form.add_field("type", "input")
    form.add_field("subfolder", subfolder)
    form.add_field("overwrite", "true")

    upload_url = build_worker_url(worker, "/upload/image")
    async with session.post(
        upload_url,
        data=form,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        resp.raise_for_status()
    return True


async def _sync_worker_media(worker, prompt_obj):
    """Sync referenced media files from master to a remote worker before dispatch."""
    media_refs = _find_media_references(prompt_obj)
    if not media_refs:
        return

    loop = asyncio.get_running_loop()
    uploaded = 0
    skipped = 0
    missing = 0
    for filename in media_refs:
        try:
            file_bytes, file_hash, mime_type = await loop.run_in_executor(
                None, _load_media_file_sync, filename
            )
        except FileNotFoundError:
            missing += 1
            log(f"[Distributed] Media file '{filename}' not found on master; worker may fail to load it.")
            continue
        except Exception as exc:
            log(f"[Distributed] Failed to load media '{filename}' for worker sync: {exc}")
            continue

        try:
            changed = await _upload_media_to_worker(worker, filename, file_bytes, file_hash, mime_type)
            if changed:
                uploaded += 1
            else:
                skipped += 1
        except Exception as exc:
            log(f"[Distributed] Failed to upload media '{filename}' to worker {worker.get('id')}: {exc}")

    debug_log(
        f"[Distributed] Media sync for worker {worker.get('id')}: "
        f"uploaded={uploaded}, skipped={skipped}, missing={missing}, referenced={len(media_refs)}"
    )


def _prepare_delegate_master_prompt(prompt_obj, collector_ids):
    """Prune master prompt so it only executes post-collector nodes in delegate mode."""
    downstream = _find_downstream_nodes(prompt_obj, collector_ids)
    nodes_to_keep = set(collector_ids)
    nodes_to_keep.update(downstream)

    pruned_prompt = {}
    for node_id in nodes_to_keep:
        node = prompt_obj.get(node_id)
        if node is not None:
            pruned_prompt[node_id] = json.loads(json.dumps(node))

    pruned_ids = set(pruned_prompt.keys())
    for node_id, node in pruned_prompt.items():
        inputs = node.get("inputs")
        if not inputs:
            continue
        for input_name, input_value in list(inputs.items()):
            if isinstance(input_value, list) and len(input_value) == 2:
                source_id = str(input_value[0])
                if source_id not in pruned_ids:
                    inputs.pop(input_name, None)
                    debug_log(
                        f"Removed upstream reference '{input_name}' from node {node_id} for delegate-only master prompt."
                    )

    next_id = _create_numeric_id_generator(pruned_prompt)
    for collector_id in collector_ids:
        collector_entry = pruned_prompt.get(collector_id)
        if not collector_entry:
            continue
        placeholder_id = next_id()
        pruned_prompt[placeholder_id] = {
            "class_type": "DistributedEmptyImage",
            "inputs": {
                "height": 64,
                "width": 64,
                "channels": 3,
            },
            "_meta": {
                "title": "Distributed Empty Image (auto-added)",
            },
        }
        collector_entry.setdefault("inputs", {})["images"] = [placeholder_id, 0]
        debug_log(
            f"Inserted placeholder node {placeholder_id} for collector {collector_id} in delegate-only master prompt."
        )

    return pruned_prompt


async def _ensure_distributed_queue(job_id):
    """Ensure a queue exists for the given distributed job ID."""
    ensure_distributed_state()
    async with prompt_server.distributed_jobs_lock:
        if job_id not in prompt_server.distributed_pending_jobs:
            prompt_server.distributed_pending_jobs[job_id] = asyncio.Queue()


def _generate_job_id_map(prompt_index, prefix):
    """Create stable per-node job IDs for distributed nodes."""
    job_map = {}
    distributed_nodes = prompt_index.nodes_for_class("DistributedCollector") + prompt_index.nodes_for_class(
        "UltimateSDUpscaleDistributed"
    )
    for node_id in distributed_nodes:
        job_map[node_id] = f"{prefix}_{node_id}"
    return job_map


def _resolve_enabled_workers(config, requested_ids=None):
    """Return a list of worker configs that should participate."""
    workers = []
    for worker in config.get("workers", []):
        worker_id = str(worker.get("id") or "").strip()
        if not worker_id:
            continue

        if requested_ids is not None:
            if worker_id not in requested_ids:
                continue
        elif not worker.get("enabled", False):
            continue

        raw_port = worker.get("port", worker.get("listen_port", 8188))
        try:
            port = int(raw_port or 8188)
        except (TypeError, ValueError):
            log(f"[Distributed] Invalid port '{raw_port}' for worker {worker_id}; defaulting to 8188.")
            port = 8188

        workers.append(
            {
                "id": worker_id,
                "name": worker.get("name", worker_id),
                "host": worker.get("host"),
                "port": port,
                "type": worker.get("type", "local"),
            }
        )
    return workers


def _resolve_master_url():
    """Best-effort reconstruction of the master's public URL."""
    cfg = load_config()
    master_cfg = cfg.get("master", {}) or {}
    configured_host = (master_cfg.get("host") or "").strip()
    configured_port = master_cfg.get("port")
    default_port = getattr(prompt_server, "port", 8188) or 8188
    port = int(configured_port or default_port)

    def _needs_https(hostname):
        hostname = hostname.lower()
        https_domains = (
            ".proxy.runpod.net",
            ".ngrok-free.app",
            ".ngrok-free.dev",
            ".ngrok.io",
            ".trycloudflare.com",
            ".cloudflare.dev",
        )
        return any(hostname.endswith(suffix) for suffix in https_domains)

    if configured_host:
        if configured_host.startswith(("http://", "https://")):
            return configured_host.rstrip("/")

        host = configured_host
        scheme = "https" if _needs_https(host) or port == 443 else "http"
        default_port_for_scheme = 443 if scheme == "https" else 80
        # For ngrok/cloud domains without explicit port, default to the scheme's default
        if configured_port is None and scheme == "https" and _needs_https(host):
            port = default_port_for_scheme
        port_part = "" if port == default_port_for_scheme else f":{port}"
        return f"{scheme}://{host}{port_part}"

    address = getattr(prompt_server, "address", "127.0.0.1") or "127.0.0.1"
    if address in ("0.0.0.0", "::"):
        address = "127.0.0.1"
    scheme = "https" if port == 443 else "http"
    default_port_for_scheme = 443 if scheme == "https" else 80
    port_part = "" if port == default_port_for_scheme else f":{port}"
    return f"{scheme}://{address}{port_part}"


async def _worker_is_active(worker):
    """Ping worker's /prompt endpoint to confirm it's reachable."""
    url = build_worker_url(worker, "/prompt")
    session = await get_client_session()
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            return resp.status == 200
    except asyncio.TimeoutError:
        debug_log(f"[Distributed] Worker probe timed out: {url}")
        return False
    except aiohttp.ClientConnectorError:
        debug_log(f"[Distributed] Worker unreachable (connection refused): {url}")
        return False
    except Exception as e:
        debug_log(f"[Distributed] Worker probe unexpected error: {e}")
        return False


async def _worker_ws_is_active(worker):
    """Ping worker's websocket endpoint to confirm it's reachable."""
    session = await get_client_session()
    url = build_worker_url(worker, "/distributed/worker_ws")
    try:
        ws = await session.ws_connect(url, heartbeat=20, timeout=3)
        await ws.close()
        return True
    except asyncio.TimeoutError:
        debug_log(f"[Distributed] Worker WS probe timed out: {url}")
        return False
    except aiohttp.ClientConnectorError:
        debug_log(f"[Distributed] Worker WS unreachable: {url}")
        return False
    except Exception as e:
        debug_log(f"[Distributed] Worker WS probe unexpected error: {e}")
        return False


async def _dispatch_via_websocket(worker_url, payload, client_id, timeout=60.0):
    """Open a fresh worker websocket, dispatch one prompt, wait for ack, then close."""
    request_id = uuid.uuid4().hex
    ws_payload = {
        "type": "dispatch_prompt",
        "request_id": request_id,
        "prompt": payload.get("prompt"),
        "workflow": payload.get("workflow"),
        "client_id": client_id,
    }
    ws_url = worker_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url}/distributed/worker_ws"
    session = await get_client_session()

    async with session.ws_connect(ws_url, heartbeat=20, timeout=timeout) as ws:
        await ws.send_json(ws_payload)
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data or "{}")
                if data.get("type") == "dispatch_ack" and data.get("request_id") == request_id:
                    if data.get("ok"):
                        return
                    raise RuntimeError(data.get("error") or "Worker rejected websocket dispatch.")
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                raise RuntimeError(f"Worker websocket closed unexpectedly: {msg.type}")

    raise RuntimeError("Worker websocket closed before dispatch_ack was received.")


async def _dispatch_worker_prompt(worker, prompt_obj, workflow_meta, client_id=None, use_websocket=False):
    """Send the prepared prompt to a worker ComfyUI instance."""
    worker_url = build_worker_url(worker)
    url = build_worker_url(worker, "/prompt")
    payload = {"prompt": prompt_obj}
    extra_data = {}
    if workflow_meta:
        extra_data.setdefault("extra_pnginfo", {})["workflow"] = workflow_meta
    if extra_data:
        payload["extra_data"] = extra_data

    if use_websocket:
        try:
            await _dispatch_via_websocket(worker_url, {
                "prompt": prompt_obj,
                "workflow": workflow_meta,
            }, client_id)
            return
        except Exception as exc:
            worker_id = worker.get("id")
            log(f"[Distributed] Websocket dispatch failed for worker {worker_id}: {exc}")
            raise

    session = await get_client_session()
    async with session.post(
        url,
        json=payload,
        timeout=aiohttp.ClientTimeout(total=60),
    ) as resp:
        resp.raise_for_status()


def _override_seed_nodes(prompt_copy, prompt_index, is_master, participant_id, worker_index_map):
    """Configure DistributedSeed nodes for master or worker role."""
    for node_id in prompt_index.nodes_for_class("DistributedSeed"):
        node = prompt_copy.get(node_id)
        if not isinstance(node, dict):
            continue
        inputs = node.setdefault("inputs", {})
        inputs["is_worker"] = not is_master
        if is_master:
            inputs["worker_id"] = ""
        else:
            inputs["worker_id"] = f"worker_{worker_index_map.get(participant_id, 0)}"


def _override_collector_nodes(
    prompt_copy,
    prompt_index,
    is_master,
    participant_id,
    job_id_map,
    master_url,
    enabled_json,
    delegate_master,
):
    """Configure DistributedCollector nodes for master or worker role."""
    for node_id in prompt_index.nodes_for_class("DistributedCollector"):
        node = prompt_copy.get(node_id)
        if not isinstance(node, dict):
            continue

        if prompt_index.has_upstream(node_id, "UltimateSDUpscaleDistributed"):
            node.setdefault("inputs", {})["pass_through"] = True
            continue

        inputs = node.setdefault("inputs", {})
        inputs["multi_job_id"] = job_id_map.get(node_id, node_id)
        inputs["is_worker"] = not is_master
        inputs["enabled_worker_ids"] = enabled_json
        if is_master:
            inputs["delegate_only"] = bool(delegate_master)
            inputs.pop("master_url", None)
            inputs.pop("worker_id", None)
        else:
            inputs["master_url"] = master_url
            inputs["worker_id"] = participant_id
            inputs["delegate_only"] = False


def _override_upscale_nodes(
    prompt_copy,
    prompt_index,
    is_master,
    participant_id,
    job_id_map,
    master_url,
    enabled_json,
):
    """Configure UltimateSDUpscaleDistributed nodes for master or worker role."""
    for node_id in prompt_index.nodes_for_class("UltimateSDUpscaleDistributed"):
        node = prompt_copy.get(node_id)
        if not isinstance(node, dict):
            continue
        inputs = node.setdefault("inputs", {})
        inputs["multi_job_id"] = job_id_map.get(node_id, node_id)
        inputs["is_worker"] = not is_master
        inputs["enabled_worker_ids"] = enabled_json
        if is_master:
            inputs.pop("master_url", None)
            inputs.pop("worker_id", None)
        else:
            inputs["master_url"] = master_url
            inputs["worker_id"] = participant_id


def _apply_participant_overrides(
    prompt_copy,
    participant_id,
    enabled_worker_ids,
    job_id_map,
    master_url,
    delegate_master,
    prompt_index,
):
    """Return a prompt copy with hidden inputs configured for master/worker."""
    is_master = participant_id == "master"
    worker_index_map = {wid: idx for idx, wid in enumerate(enabled_worker_ids)}
    enabled_json = json.dumps(enabled_worker_ids)

    _override_seed_nodes(prompt_copy, prompt_index, is_master, participant_id, worker_index_map)
    _override_collector_nodes(
        prompt_copy,
        prompt_index,
        is_master,
        participant_id,
        job_id_map,
        master_url,
        enabled_json,
        delegate_master,
    )
    _override_upscale_nodes(
        prompt_copy,
        prompt_index,
        is_master,
        participant_id,
        job_id_map,
        master_url,
        enabled_json,
    )

    return prompt_copy


async def _select_active_workers(workers, use_websocket, delegate_master):
    """Probe workers and return (active_workers, updated_delegate_master).

    If all workers are offline and delegate_master was True, flips delegate_master to False.
    """
    active_workers = []
    for worker in workers:
        is_active = (
            await _worker_ws_is_active(worker)
            if use_websocket
            else await _worker_is_active(worker)
        )
        if is_active:
            active_workers.append(worker)
        else:
            log(f"[Distributed] Worker {worker['name']} ({worker['id']}) is offline, skipping.")

    if not active_workers and delegate_master:
        debug_log("All workers offline while delegate-only requested; enabling master participation.")
        delegate_master = False

    return active_workers, delegate_master


async def orchestrate_distributed_execution(
    prompt_obj,
    workflow_meta,
    client_id,
    enabled_worker_ids=None,
    delegate_master=None,
):
    """Core orchestration logic for the /distributed/queue endpoint.

    Returns:
        tuple[str, int]: (prompt_id, worker_count)
    """
    ensure_distributed_state()

    config = load_config()
    use_websocket = bool(config.get("settings", {}).get("websocket_orchestration", False))
    requested_ids = enabled_worker_ids if enabled_worker_ids is not None else None
    workers = _resolve_enabled_workers(config, requested_ids)
    prompt_index = PromptIndex(prompt_obj)

    # Respect master delegate-only configuration
    if delegate_master is None:
        delegate_master = bool(config.get("settings", {}).get("master_delegate_only", False))

    if not workers and delegate_master:
        debug_log("Delegate-only requested but no workers are enabled. Falling back to master execution.")
        delegate_master = False

    active_workers, delegate_master = await _select_active_workers(workers, use_websocket, delegate_master)

    enabled_ids = [worker["id"] for worker in active_workers]

    discovery_prefix = f"exec_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    job_id_map = _generate_job_id_map(prompt_index, discovery_prefix)

    if not job_id_map:
        prompt_id = await queue_prompt_payload(prompt_obj, workflow_meta, client_id)
        return prompt_id, 0

    for job_id in job_id_map.values():
        await _ensure_distributed_queue(job_id)

    master_url = _resolve_master_url()
    master_prompt = prompt_index.copy_prompt()
    master_prompt = _apply_participant_overrides(
        master_prompt,
        "master",
        enabled_ids,
        job_id_map,
        master_url,
        delegate_master,
        prompt_index,
    )

    if delegate_master:
        collector_ids = _find_nodes_by_class(master_prompt, "DistributedCollector")
        upscale_nodes = _find_nodes_by_class(master_prompt, "UltimateSDUpscaleDistributed")
        if upscale_nodes:
            debug_log(
                "Delegate-only master mode currently does not support UltimateSDUpscaleDistributed nodes; running full prompt on master."
            )
        elif not collector_ids:
            debug_log(
                "Delegate-only master mode requested but no collectors found in master prompt. Running full prompt on master."
            )
        else:
            master_prompt = _prepare_delegate_master_prompt(master_prompt, collector_ids)

    if active_workers:
        debug_log(
            "Active distributed workers: "
            + ", ".join(f"{worker['name']} ({worker['id']})" for worker in active_workers)
        )
    worker_payloads = []
    for worker in active_workers:
        worker_prompt = prompt_index.copy_prompt()

        is_remote_like = bool(worker.get("host"))
        if is_remote_like:
            path_separator = await _fetch_worker_path_separator(worker)
            if path_separator:
                worker_prompt = _convert_paths_for_platform(worker_prompt, path_separator)

        worker_prompt = _prune_prompt_for_worker(worker_prompt)
        worker_prompt = _apply_participant_overrides(
            worker_prompt,
            worker["id"],
            enabled_ids,
            job_id_map,
            master_url,
            delegate_master,
            prompt_index,
        )

        if is_remote_like:
            await _sync_worker_media(worker, worker_prompt)

        worker_payloads.append((worker, worker_prompt))

    if worker_payloads:
        await asyncio.gather(
            *[
                _dispatch_worker_prompt(worker, wprompt, workflow_meta, client_id, use_websocket=use_websocket)
                for worker, wprompt in worker_payloads
            ]
        )

    prompt_id = await queue_prompt_payload(master_prompt, workflow_meta, client_id)
    return prompt_id, len(worker_payloads)
