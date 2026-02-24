import asyncio
import hashlib
import mimetypes
import os
import re

import aiohttp

from ...utils.logging import debug_log, log
from ...utils.network import build_worker_url, get_client_session
from ...utils.trace_logger import trace_debug, trace_info


LIKELY_FILENAME_RE = re.compile(
    r"\.(ckpt|safetensors|pt|pth|bin|yaml|json|png|jpg|jpeg|webp|gif|bmp|latent|txt|vae|lora|embedding)"
    r"(\s*\[\w+\])?$",
    re.IGNORECASE,
)
IMAGE_OR_VIDEO_RE = re.compile(
    r"\.(png|jpg|jpeg|webp|gif|bmp|mp4|avi|mov|mkv|webm)(\s*\[\w+\])?$",
    re.IGNORECASE,
)

def convert_paths_for_platform(obj, target_separator):
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


def _find_media_references(prompt_obj):
    """Find media file references in image/video inputs used by worker prompts."""
    media_refs = set()
    for node in prompt_obj.values():
        if not isinstance(node, dict):
            continue
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


async def fetch_worker_path_separator(worker, trace_execution_id=None):
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
        if trace_execution_id:
            trace_debug(trace_execution_id, f"Failed to fetch worker system info ({worker.get('id')}): {exc}")
        else:
            debug_log(f"[Distributed] Failed to fetch worker system info ({worker.get('id')}): {exc}")
        return None


async def _upload_media_to_worker(worker, filename, file_bytes, file_hash, mime_type, trace_execution_id=None):
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
        if trace_execution_id:
            trace_debug(trace_execution_id, f"Media check failed for '{normalized}' on worker {worker.get('id')}: {exc}")
        else:
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


async def sync_worker_media(worker, prompt_obj, trace_execution_id=None):
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
            if trace_execution_id:
                trace_info(trace_execution_id, f"Media file '{filename}' not found on master; worker may fail to load it.")
            else:
                log(f"[Distributed] Media file '{filename}' not found on master; worker may fail to load it.")
            continue
        except Exception as exc:
            if trace_execution_id:
                trace_info(trace_execution_id, f"Failed to load media '{filename}' for worker sync: {exc}")
            else:
                log(f"[Distributed] Failed to load media '{filename}' for worker sync: {exc}")
            continue

        try:
            changed = await _upload_media_to_worker(
                worker,
                filename,
                file_bytes,
                file_hash,
                mime_type,
                trace_execution_id=trace_execution_id,
            )
            if changed:
                uploaded += 1
            else:
                skipped += 1
        except Exception as exc:
            if trace_execution_id:
                trace_info(trace_execution_id, f"Failed to upload media '{filename}' to worker {worker.get('id')}: {exc}")
            else:
                log(f"[Distributed] Failed to upload media '{filename}' to worker {worker.get('id')}: {exc}")

    summary = (
        f"Media sync for worker {worker.get('id')}: "
        f"uploaded={uploaded}, skipped={skipped}, missing={missing}, referenced={len(media_refs)}"
    )
    if trace_execution_id:
        trace_debug(trace_execution_id, summary)
    else:
        debug_log(f"[Distributed] {summary}")
