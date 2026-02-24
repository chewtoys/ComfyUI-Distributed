# Refactor Plan 2 — ComfyUI-Distributed

All paths relative to `custom_nodes/ComfyUI-Distributed/`.
Read every file listed under "Read first" before making any edits.
After every phase, run `python -c "import custom_nodes.ComfyUI-Distributed"` (or equivalent import check) to confirm nothing is broken.

---

## Phase 1 — Correctness / Data Integrity (do these first)

### Task 1.1 — Fix lock contention in USDU requeue path

**Problem:** `_check_and_requeue_timed_out_workers` in `utils/usdu_managment.py` calls
`await probe_worker(...)` (a network request) while holding `distributed_tile_jobs_lock`.
Any concurrent heartbeat, submit_tiles, or request_image that also needs the lock is blocked
for the full probe timeout.

**Read first:** `utils/usdu_managment.py` (find `_check_and_requeue_timed_out_workers`),
`utils/network.py` (confirm `probe_worker` signature).

**Changes:**

In `_check_and_requeue_timed_out_workers`:

1. **Snapshot under lock, then release.** Collect the list of workers to probe while holding
   the lock, then exit the lock context before any `await probe_worker(...)` call.

   Before (pseudocode):
   ```python
   async with prompt_server.distributed_tile_jobs_lock:
       job_data = ...
       for worker_id, last_seen in timed_out:
           result = await probe_worker(url, timeout=...)   # WRONG: network call under lock
           if not result:
               requeue(...)
   ```

   After:
   ```python
   # Step 1: snapshot under lock
   async with prompt_server.distributed_tile_jobs_lock:
       job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
       if not isinstance(job_data, BaseJobState):
           return 0
       timed_out_snapshot = [
           (wid, ts) for wid, ts in job_data.worker_status.items()
           if (now - float(ts)) > HEARTBEAT_TIMEOUT
       ]
       assigned_snapshot = dict(job_data.assigned_to_workers)

   # Step 2: probe outside lock
   workers_to_requeue = []
   for worker_id, last_seen in timed_out_snapshot:
       task_ids = assigned_snapshot.get(worker_id, [])
       wrec = _find_worker_record(worker_id)
       if wrec:
           result = await probe_worker(build_worker_url(wrec), timeout=2.0)
           if result is None:
               workers_to_requeue.append((worker_id, task_ids))
       else:
           workers_to_requeue.append((worker_id, task_ids))

   # Step 3: re-acquire lock to apply requeue decisions
   if not workers_to_requeue:
       return 0
   async with prompt_server.distributed_tile_jobs_lock:
       job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
       if not isinstance(job_data, BaseJobState):
           return 0
       requeued = 0
       for worker_id, task_ids in workers_to_requeue:
           for task_id in task_ids:
               await job_data.pending_tasks.put(task_id)
               requeued += 1
           job_data.assigned_to_workers.pop(worker_id, None)
           job_data.worker_status.pop(worker_id, None)
       return requeued
   ```

2. Add a private helper `_find_worker_record(worker_id)` that loads config and returns the
   worker dict (or `None`). This keeps the probe-outside-lock section clean.

**Verify:** The function should now only hold the lock during pure dict reads/writes.
No `await` expressions should appear inside any `async with distributed_tile_jobs_lock:` block
in this function.

---

### Task 1.2 — Fix non-idempotent client retries

**Problem:** `web/apiClient.js` retries every request unconditionally. Mutating endpoints
(`/distributed/queue`, launch, stop, update, delete, startTunnel, stopTunnel) can execute
twice on a transient network error, causing duplicate jobs or double-start/stop.

**Read first:** `web/apiClient.js` (full file).

**Changes:**

1. **Add a `retry` option to the internal `request` function.** Default `true` for safe reads,
   `false` for all mutating calls.

   ```javascript
   const request = async (endpoint, options = {}, { retries = TIMEOUTS.MAX_RETRIES, retry = true } = {}) => {
       const maxAttempts = retry ? retries : 1;
       let lastError;
       let delay = TIMEOUTS.RETRY_DELAY;
       for (let attempt = 0; attempt < maxAttempts; attempt++) {
           try {
               const response = await fetch(...);
               if (!response.ok) { ... }
               return await response.json();
           } catch (error) {
               lastError = error;
               if (attempt < maxAttempts - 1) {
                   await new Promise(resolve => setTimeout(resolve, delay));
                   delay *= 2;
               }
           }
       }
       throw lastError;
   };
   ```

2. **Set `retry: false` for all mutating endpoints:**
   - `launchWorker` — `request('/distributed/launch_worker', { method: 'POST', ... }, { retry: false })`
   - `stopWorker` — same
   - `queueDistributed` — same
   - `updateWorker` — same
   - `deleteWorker` — same
   - `updateSetting` — same
   - `updateMaster` — same
   - `startTunnel` — same
   - `stopTunnel` — same
   - `dispatchToWorker` — same

3. **Add idempotency key support for `queueDistributed`.** The caller already generates a
   `trace_execution_id`; pass it as an `X-Idempotency-Key` header so a future backend can
   deduplicate if desired.

   ```javascript
   queueDistributed: (payload) =>
       request('/distributed/queue', {
           method: 'POST',
           headers: {
               'Content-Type': 'application/json',
               ...(payload.trace_execution_id
                   ? { 'X-Idempotency-Key': payload.trace_execution_id }
                   : {}),
           },
           body: JSON.stringify(payload),
       }, { retry: false }),
   ```

4. **Leave `probeWorker`, `checkStatus`, `checkMultipleStatuses`, `getManagedWorkers`,
   `getWorkerLog`, `getTunnelStatus`, `getNetworkInfo` as `retry: true`** — these are
   read-only and safe to retry.

**Verify:** `queueDistributed`, `launchWorker`, `stopWorker`, `startTunnel`, `stopTunnel`
all pass `{ retry: false }`. No mutating call uses the default retry loop.

---

### Task 1.3 — Make config writes atomic and race-safe

**Problem:** `utils/config.py` `save_config()` writes directly to the config file with
`json.dump(..., f)`. A crash mid-write corrupts the file. Concurrent requests from
`api/config_routes.py` do independent load–modify–save cycles with no locking, creating
a race condition.

**Read first:** `utils/config.py` (full), `api/config_routes.py` (full).

**Changes in `utils/config.py`:**

1. Add a module-level `asyncio.Lock`:
   ```python
   import asyncio
   _config_lock = asyncio.Lock()
   ```

2. Rewrite `save_config` to write atomically using a temp file + `os.replace`:
   ```python
   def save_config(config):
       config_path = _get_config_path()
       tmp_path = config_path + '.tmp'
       try:
           with open(tmp_path, 'w') as f:
               json.dump(config, f, indent=2)
           os.replace(tmp_path, config_path)
       except Exception:
           try:
               os.unlink(tmp_path)
           except OSError:
               pass
           raise
   ```

3. Expose a new async context manager `config_transaction()` for use by routes:
   ```python
   from contextlib import asynccontextmanager

   @asynccontextmanager
   async def config_transaction():
       """Acquire config lock, yield loaded config, save on clean exit."""
       async with _config_lock:
           config = load_config()
           yield config
           save_config(config)
   ```

**Changes in `api/config_routes.py`:**

4. Replace every `load_config()` / `save_config(config)` pair in route handlers with
   `async with config_transaction() as config:`. Each handler becomes:
   ```python
   async with config_transaction() as config:
       # mutate config in place
       # no explicit save needed — context manager handles it
   ```
   Apply to: `update_worker_endpoint`, `delete_worker_endpoint`, `update_setting_endpoint`,
   `update_master_endpoint`, and the bulk `POST /distributed/config` handler.

5. The read-only `GET /distributed/config` route does not need the lock — leave it using
   `load_config()` directly.

**Verify:** No `save_config` call outside of the `config_transaction` context manager in
route handlers. Grep for `save_config(` in `api/config_routes.py` and confirm zero results.

---

## Phase 2 — Structural Splits

### Task 2.1 — Split `utils/usdu_managment.py` god module

**Problem:** `utils/usdu_managment.py` mixes job-state CRUD, timeout/requeue policy,
multipart payload parsing, conditioning utilities, and HTTP route registrations in one ~630-line file.

**Read first:** `utils/usdu_managment.py` (full), `upscale/job_state.py`, `upscale/job_models.py`,
`api/worker_routes.py`, `upscale/worker_comms.py` (check `_send_heartbeat_to_master` import).

**Target structure:**

```
upscale/
  job_store.py        ← job init + ensure_tile_jobs_initialized + MAX_PAYLOAD_SIZE
  job_timeout.py      ← heartbeat check, requeue logic (_check_and_requeue_timed_out_workers)
  payload_parsers.py  ← _parse_tiles_from_form, any other multipart parsers
api/
  usdu_routes.py      ← all HTTP route handlers from usdu_managment.py
utils/
  usdu_managment.py   ← thin shim: re-exports for backward compat + _send_heartbeat_to_master
```

**Step-by-step:**

1. **Create `upscale/job_store.py`.**
   Move from `usdu_managment.py`:
   - `ensure_tile_jobs_initialized()`
   - `_init_job_queue(multi_job_id, mode, batch_size, num_tiles_per_image, batched_static)`
   - `init_dynamic_job(...)`
   - `init_static_job_batched(...)`
   - `_drain_results_queue(multi_job_id)`
   - The `MAX_PAYLOAD_SIZE` constant
   Imports needed: `asyncio`, `server`, `upscale.job_models`, `utils.logging`.

2. **Create `upscale/job_timeout.py`.**
   Move from `usdu_managment.py` (after Task 1.1 changes are applied):
   - `_check_and_requeue_timed_out_workers(multi_job_id, batch_size)` (already refactored in Task 1.1)
   - `_find_worker_record(worker_id)` (new helper from Task 1.1)
   Imports needed: `utils.network`, `utils.config`, `utils.constants`, `upscale.job_models`,
   `upscale.job_store` (for `ensure_tile_jobs_initialized`).

3. **Create `upscale/payload_parsers.py`.**
   Move from `usdu_managment.py`:
   - `_parse_tiles_from_form(request)` and any helper it uses
   Imports needed: `aiohttp`, `PIL`, `utils.image`.

4. **Create `api/usdu_routes.py`.**
   Move from `usdu_managment.py` all `@server.PromptServer.instance.routes.*` decorated
   functions:
   - `heartbeat_endpoint` (`POST /distributed/heartbeat`)
   - `submit_tiles_endpoint` (`POST /distributed/submit_tiles`)
   - `submit_image_endpoint` (`POST /distributed/submit_image`)
   - `request_image_endpoint` (`POST /distributed/request_image`)
   - `job_status_endpoint` (`GET /distributed/job_status`)
   Import from new modules above instead of `usdu_managment`.

5. **Rewrite `utils/usdu_managment.py` as a thin shim.**
   Keep only:
   - `_send_heartbeat_to_master(...)` (used by `upscale/worker_comms.py`)
   - `MAX_PAYLOAD_SIZE` re-export from `upscale.job_store`
   - Re-exports of `_check_and_requeue_timed_out_workers` and `ensure_tile_jobs_initialized`
     for any callers that import from here directly.

   ```python
   # utils/usdu_managment.py — backward-compat shim
   from ..upscale.job_store import (
       MAX_PAYLOAD_SIZE,
       ensure_tile_jobs_initialized,
       _init_job_queue,
       init_dynamic_job,
       init_static_job_batched,
   )
   from ..upscale.job_timeout import _check_and_requeue_timed_out_workers

   async def _send_heartbeat_to_master(...):
       ...  # keep this here — it's worker-side only
   ```

6. **Register `api/usdu_routes.py` in `__init__.py` or `distributed.py`.**
   Find where `usdu_managment` is currently imported to trigger route registration, and
   add an import of `api.usdu_routes` in the same place.

7. **Update all import sites.** Grep for `from ..utils.usdu_managment import` and
   `from .utils.usdu_managment import` across all files. Update each to import from the
   appropriate new module.

**Verify:** `python -m py_compile` on all modified files. Grep confirms no direct
`usdu_managment` imports remain except the shim itself and `worker_comms.py`
(which imports `_send_heartbeat_to_master`).

---

### Task 2.2 — Decompose `CloudflareTunnelManager`

**Problem:** `utils/cloudflare.py` is a 387-line class handling binary management, state
persistence, log streaming, process lifecycle, and status reporting.

**Read first:** `utils/cloudflare.py` (full), `api/tunnel_routes.py`, `distributed.py`
(check where `CloudflareTunnelManager` is imported/instantiated).

**Target structure:**

```
utils/
  cloudflare/
    __init__.py         ← re-exports CloudflareTunnelManager for backward compat
    binary.py           ← binary discovery and download
    state.py            ← config persistence, previous-host restoration
    process_reader.py   ← thread-based output reader, URL capture, log buffer
    tunnel.py           ← CloudflareTunnelManager: start/stop lifecycle, status
```

**Step-by-step:**

1. **Create `utils/cloudflare/` package.**

2. **Create `utils/cloudflare/binary.py`.**
   Move from `cloudflare.py`:
   - `_get_cloudflared_dir()`, `_get_binary_path()` (or equivalent path helpers)
   - `_get_platform_binary_name()`
   - `_download_cloudflared()` / `ensure_binary()` — everything related to finding or
     downloading the `cloudflared` binary.
   Export a single `ensure_binary() -> str` function that returns the path to the binary.

3. **Create `utils/cloudflare/state.py`.**
   Move from `cloudflare.py`:
   - `_save_tunnel_url_to_config(url)` / `_restore_previous_master_host()` /
     `_clear_tunnel_state()` — all code that reads/writes the config file to persist or
     restore tunnel URLs and master host settings.
   This module imports from `utils.config`.

4. **Create `utils/cloudflare/process_reader.py`.**
   Move from `cloudflare.py`:
   - The `_read_output` thread / `_OutputReader` thread class that reads `cloudflared`
     stdout line-by-line, extracts the tunnel URL via regex, appends to `recent_logs`,
     and sets the URL event.
   Export: `ProcessReader` class (or equivalent) with `start(process)` / `stop()` /
   `get_url()` / `get_recent_logs()` methods. Keep the `LOG_BUFFER_SIZE` constant here
   (or move to `utils/constants.py` per Task 5.2).

5. **Create `utils/cloudflare/tunnel.py`.**
   This keeps the `CloudflareTunnelManager` class but now delegates to the modules above:
   ```python
   from .binary import ensure_binary
   from .state import save_tunnel_url, restore_previous_host, clear_tunnel_state
   from .process_reader import ProcessReader

   class CloudflareTunnelManager:
       def __init__(self): ...
       async def start(self): ...    # calls ensure_binary, starts process, starts ProcessReader
       async def stop(self): ...     # stops ProcessReader, stops process, calls clear/restore
       def get_status(self): ...
       def get_recent_logs(self): ...
   ```

6. **Create `utils/cloudflare/__init__.py`:**
   ```python
   from .tunnel import CloudflareTunnelManager
   __all__ = ['CloudflareTunnelManager']
   ```

7. **Delete `utils/cloudflare.py`** (the original monolithic file) after confirming all
   import sites resolve to `utils.cloudflare` (the package), which works identically due
   to `__init__.py`.

**Verify:** `from utils.cloudflare import CloudflareTunnelManager` resolves. All tunnel
routes still function. `TUNNEL_START_TIMEOUT` constant is accessible (either via
`utils.constants` after Task 5.2, or defined in `tunnel.py`).

---

## Phase 3 — Reliability / Leak Fixes

### Task 3.1 — Fix UI modal lifecycle leak

**Problem:** `web/ui.js` adds a `keydown` listener when the log modal opens but never
removes it on close or backdrop-click, leaking one listener per open.

**Read first:** `web/ui.js` (full — focus on log modal creation, open/close methods,
and the `keydown` listener).

**Changes:**

1. **Create `web/ui/logModal.js`** (file already has a `web/ui/` directory).
   Extract the log modal HTML construction, open/close logic, and auto-refresh timer into
   a module with explicit lifecycle:

   ```javascript
   export function createLogModal() {
       let _keydownHandler = null;
       let _refreshTimer = null;
       let _modalEl = null;

       function mount(container, { onClose, fetchLog }) {
           // build modal DOM
           _modalEl = buildModalDOM();
           container.appendChild(_modalEl);

           // store listener reference so we can remove it
           _keydownHandler = (e) => { if (e.key === 'Escape') unmount(); };
           document.addEventListener('keydown', _keydownHandler);

           // backdrop click
           _modalEl.querySelector('.modal-backdrop')
               .addEventListener('click', unmount);

           // auto-refresh if checkbox checked
           _refreshTimer = setInterval(() => { if (isAutoRefresh()) fetchLog(); }, 2000);
       }

       function unmount() {
           if (_keydownHandler) {
               document.removeEventListener('keydown', _keydownHandler);
               _keydownHandler = null;
           }
           if (_refreshTimer) {
               clearInterval(_refreshTimer);
               _refreshTimer = null;
           }
           _modalEl?.remove();
           _modalEl = null;
       }

       return { mount, unmount };
   }
   ```

2. **In `web/ui.js`**, replace the inline modal construction with:
   ```javascript
   import { createLogModal } from './ui/logModal.js';
   // ...
   showLogModal(workerId) {
       const modal = createLogModal();
       modal.mount(document.body, {
           onClose: () => modal.unmount(),
           fetchLog: () => this._fetchAndDisplayLog(workerId),
       });
   }
   ```

3. **Move any inline `style="..."` attributes from the modal DOM** into `distributed.css`
   class rules (e.g., `.log-modal`, `.log-modal__body`, `.log-modal__backdrop`).

**Verify:** Open log modal → close with Escape → open again. Confirm only one `keydown`
listener is registered at a time (check with DevTools → Event Listeners on `document`).

---

## Phase 4 — Deduplication

### Task 4.1 — Extract shared trace logging helpers

**Problem:** Three copies of identical `_trace_prefix` / `_trace_debug` / `_trace_info`
helpers exist in `api/orchestration/dispatch.py:11-20`,
`api/orchestration/media_sync.py:24-33`, `api/queue_orchestration.py:32-41`.

**Read first:** All three files (confirm the copies are byte-for-byte identical).

**Changes:**

1. **Create `utils/trace_logger.py`:**
   ```python
   from .logging import debug_log, log

   def trace_prefix(trace_execution_id: str) -> str:
       return f"[Distributed][exec:{trace_execution_id}]"

   def trace_debug(trace_execution_id: str, message: str) -> None:
       debug_log(f"{trace_prefix(trace_execution_id)} {message}")

   def trace_info(trace_execution_id: str, message: str) -> None:
       log(f"{trace_prefix(trace_execution_id)} {message}")
   ```

2. **In each of the three files**, delete the local definitions and add:
   ```python
   from ..utils.trace_logger import trace_prefix, trace_debug, trace_info
   ```
   Then rename call sites: `_trace_prefix` → `trace_prefix`, etc.

**Verify:** Grep for `def _trace_prefix` across the repo — should return zero results.

---

### Task 4.2 — Consolidate positive int/float parsing

**Problem:** Three near-identical implementations:
- `_parse_positive_int(value, default)` in `api/queue_orchestration.py:93-108`
- `_coerce_positive_int(value, default)` in `api/orchestration/dispatch.py:48-53`
- `validate_positive_int(value, field_name)` in `api/schemas.py:28-36`
Plus `_parse_positive_float` in `queue_orchestration.py`.

**Read first:** `api/schemas.py`, `api/queue_orchestration.py`, `api/orchestration/dispatch.py`.

**Changes:**

1. **Add to `api/schemas.py`** (after existing `validate_positive_int`):
   ```python
   def parse_positive_int(value, default: int) -> int:
       """Parse value as positive int, returning default on failure."""
       try:
           parsed = int(value)
       except (TypeError, ValueError):
           return max(1, int(default))
       return max(1, parsed)

   def parse_positive_float(value, default: float) -> float:
       """Parse value as positive float, returning default on failure."""
       try:
           parsed = float(value)
       except (TypeError, ValueError):
           return max(0.0, float(default))
       return max(0.0, parsed)
   ```

2. **In `api/queue_orchestration.py`:**
   - Delete `_parse_positive_int` and `_parse_positive_float`.
   - Add `from .schemas import parse_positive_int, parse_positive_float`.
   - Replace all call sites.

3. **In `api/orchestration/dispatch.py`:**
   - Delete `_coerce_positive_int`.
   - Add `from ..api.schemas import parse_positive_int`.
   - Replace call sites.

**Verify:** `grep -r "_parse_positive\|_coerce_positive"` returns zero results outside of
any test files.

---

### Task 4.3 — Deduplicate conditioning slicing

**Problem:** `upscale/modes/dynamic.py` contains conditioning slicing logic already
present in `upscale/tile_ops.py`.

**Read first:** `upscale/modes/dynamic.py` (full), `upscale/tile_ops.py` (full).
Identify the exact duplicated slicing pattern in each file (look for code that slices
`positive` / `negative` conditioning tensors by batch index).

**Changes:**

1. **Identify** the shared logic. It will look like:
   ```python
   # Slicing positive/negative conditioning for a specific batch index
   pos_sliced = [[t[:, idx:idx+1, ...] for t in cond_pair] for cond_pair in positive]
   neg_sliced = [[t[:, idx:idx+1, ...] for t in cond_pair] for cond_pair in negative]
   ```

2. **If the helper already exists in `tile_ops.py`**, find it and confirm its signature.
   If not, extract the logic from `dynamic.py` into a new function in `tile_ops.py`:
   ```python
   def slice_conditioning(positive, negative, batch_idx: int):
       """Return conditioning sliced to a single batch index."""
       ...
   ```

3. **In `upscale/modes/dynamic.py`**, delete the local implementation and replace with:
   ```python
   from ..tile_ops import slice_conditioning
   ```
   Update all call sites to use `slice_conditioning(positive, negative, idx)`.

**Verify:** The slicing logic exists in exactly one place. `grep -n "idx:idx+1\|batch_idx:batch_idx"`
in `dynamic.py` returns zero results.

---

### Task 4.4 — Unify worker probe/status logic in JavaScript

**Problem:** `web/workerLifecycle.js` and `web/executionUtils.js` both implement probe +
parse flows. `executionUtils.js` already uses `extension.api.probeWorker()` but
`workerLifecycle.js` still has raw `fetch` calls for `checkMasterStatus` and
`checkWorkerStatus`.

**Read first:** `web/workerLifecycle.js` (full), `web/executionUtils.js` (full),
`web/apiClient.js` (confirm `probeWorker` signature).

**Changes:**

1. **In `web/workerLifecycle.js`**, find `checkMasterStatus` and `checkWorkerStatus`.
   Note: These two functions have legitimate reasons for raw `fetch`:
   - `checkMasterStatus` uses a same-origin call (no CORS) to the local server
   - `checkWorkerStatus` may use `AbortSignal.any()` to combine timeout + cancellation

   **Assessment before changing:** If `probeWorker` in `apiClient.js` supports an
   `AbortSignal` parameter, migrate both to use `extension.api.probeWorker()`.
   If it does not, add an optional `signal` parameter to `probeWorker`:
   ```javascript
   probeWorker: async (workerUrl, timeout, signal = null) => {
       const controller = new AbortController();
       const timeoutId = setTimeout(() => controller.abort(), timeout);
       const effectiveSignal = signal
           ? AbortSignal.any([controller.signal, signal])
           : controller.signal;
       try {
           const response = await fetch(`${workerUrl}/prompt`, { signal: effectiveSignal });
           ...
       } finally {
           clearTimeout(timeoutId);
       }
   }
   ```

2. **Create a shared UI mapper** in `web/workerUtils.js` (already exists):
   ```javascript
   export function applyProbeResultToWorkerDot(workerId, probeResult) {
       const dot = document.getElementById(`status-${workerId}`);
       if (!dot) return;
       dot.classList.remove('worker-status--online', 'worker-status--offline',
                           'worker-status--processing', 'worker-status--unknown',
                           'status-pulsing');
       if (!probeResult || !probeResult.ok) {
           dot.classList.add('worker-status--offline');
           dot.title = 'Offline';
       } else if (probeResult.queueRemaining > 0) {
           dot.classList.add('worker-status--processing');
           dot.title = `Processing (${probeResult.queueRemaining} queued)`;
       } else {
           dot.classList.add('worker-status--online');
           dot.title = 'Online';
       }
   }
   ```

3. **Use `applyProbeResultToWorkerDot`** in both `workerLifecycle.js` and
   `executionUtils.js` (the `performPreflightCheck` inactive-worker update block at
   lines 915-922 of `executionUtils.js`).

**Verify:** `grep -n "style\." web/workerLifecycle.js` returns zero results. DOM status
updates flow exclusively through class manipulation helpers.

---

### Task 4.5 — Extract timeout handler duplication in result_collector

**Problem:** `upscale/result_collector.py` lines 116-161 contain nearly identical
worker-status logging blocks for static (`TileJobState`) vs dynamic (`ImageJobState`) modes
inside the `asyncio.TimeoutError` handler.

**Read first:** `upscale/result_collector.py` (full).

**Changes:**

1. **Extract a helper method** on `ResultCollectorMixin`:
   ```python
   def _log_worker_timeout_status(self, job_data, current_time: float, multi_job_id: str) -> None:
       """Log which workers have timed out and for how long."""
       if not isinstance(job_data, BaseJobState):
           return
       worker_status = dict(job_data.worker_status)
       for worker_id, last_seen in worker_status.items():
           elapsed = max(0.0, current_time - float(last_seen))
           log(
               f"[USDU] Timeout: job={multi_job_id} worker={worker_id[:8]} "
               f"elapsed={elapsed:.1f}s"
           )
   ```

2. **In the `asyncio.TimeoutError` block**, replace both the static-mode and dynamic-mode
   worker-status logging sections with a single call:
   ```python
   except asyncio.TimeoutError:
       current_time = time.time()
       self._log_worker_timeout_status(job_data_snapshot, current_time, multi_job_id)
       if mode == 'dynamic':
           if current_time - last_heartbeat_check >= HEARTBEAT_INTERVAL:
               requeued = await self._check_and_requeue_timed_out_workers(...)
               ...
   ```
   The `job_data_snapshot` should be obtained under the lock before the `wait_for` call
   (snapshot pattern from Task 1.1).

**Verify:** The timeout handler block has one code path for status logging, not two.

---

### Task 4.6 — Eliminate config update endpoint boilerplate

**Problem:** `api/config_routes.py` `update_worker_endpoint`, `update_master_endpoint`,
and `update_setting_endpoint` all repeat the same `if "field" in data: target[field] = ...`
pattern with special handling for `None` (pop vs assign) and `host` normalization.

**Read first:** `api/config_routes.py` (full, especially lines 107-275).

**Changes:**

1. **Define a field rule schema** near the top of `config_routes.py`:
   ```python
   # Each entry: (key, normalizer_or_None, remove_on_none: bool)
   _WORKER_FIELDS = [
       ('enabled',     None,           False),
       ('name',        None,           False),
       ('host',        normalize_host, True),   # pop key when value is None
       ('cuda_device', None,           True),
       ('extra_args',  None,           True),
       ('type',        None,           False),
   ]
   _MASTER_FIELDS = [
       ('name',        None,           False),
       ('host',        normalize_host, True),
   ]
   ```

2. **Add a helper function:**
   ```python
   def _apply_field_patch(target: dict, data: dict, field_rules: list) -> None:
       """Apply a partial update to target dict based on field rules."""
       for key, normalizer, remove_on_none in field_rules:
           if key not in data:
               continue
           value = data[key]
           if value is None and remove_on_none:
               target.pop(key, None)
           else:
               target[key] = normalizer(value) if (normalizer and value is not None) else value
   ```

3. **Replace the explicit if-blocks** in `update_worker_endpoint` with:
   ```python
   _apply_field_patch(worker, data, _WORKER_FIELDS)
   ```
   And in `update_master_endpoint`:
   ```python
   _apply_field_patch(config['master'], data, _MASTER_FIELDS)
   ```

**Verify:** `update_worker_endpoint` body no longer contains any `if "enabled" in data:` style
blocks. Behavior is identical — test with a PATCH that sets `host` to `None` (should remove
the key) and one that sets it to a URL string (should normalize it).

---

## Phase 5 — Medium-Low

### Task 5.1 — Centralize distributed state initialization

**Problem:** The `ensure_distributed_state()` / job queue init pattern is repeated in
`api/queue_orchestration.py`, `distributed.py`, `api/job_routes.py` (prepare_job endpoint),
and `nodes/collector.py`. Each touches `prompt_server.distributed_pending_jobs` and the
associated lock independently.

**Read first:** `api/queue_orchestration.py` (`ensure_distributed_state` function),
`distributed.py` (find where state attributes are set on `prompt_server`),
`api/job_routes.py` (`prepare_job_endpoint`, lines 131-146),
`nodes/collector.py` (lines 198-204).

**Changes:**

1. **Ensure `ensure_distributed_state()` in `api/queue_orchestration.py` is the sole place**
   that creates `prompt_server.distributed_pending_jobs`, `distributed_jobs_lock`, and
   any other top-level distributed state attributes. It should be idempotent:
   ```python
   def ensure_distributed_state(server_instance=None):
       ps = server_instance or prompt_server
       if not hasattr(ps, 'distributed_pending_jobs'):
           ps.distributed_pending_jobs = {}
       if not hasattr(ps, 'distributed_jobs_lock'):
           ps.distributed_jobs_lock = asyncio.Lock()
   ```

2. **In `api/job_routes.py` `prepare_job_endpoint`**, replace the inline init:
   ```python
   # Before:
   async with prompt_server.distributed_jobs_lock:
       if multi_job_id not in prompt_server.distributed_pending_jobs:
           prompt_server.distributed_pending_jobs[multi_job_id] = asyncio.Queue()
   ```
   with a call to `ensure_distributed_state()` at startup (it's idempotent) and keep the
   per-job queue init in place (that's correct per-job logic, not state init).

3. **In `nodes/collector.py`** (lines 198-204), the early queue creation is correct
   behavior (race prevention) — leave it. But ensure `ensure_distributed_state()` has been
   called before the node can ever run (it's called at startup in `queue_orchestration.py`
   module import time).

4. **In `distributed.py`**, remove any direct `prompt_server.distributed_* = ...`
   assignments and replace with a call to `ensure_distributed_state()` at startup.

**Verify:** Grep for `distributed_pending_jobs = {}` and `distributed_jobs_lock = ` — both
should appear only in `ensure_distributed_state`. All other sites just call that function.

---

### Task 5.2 — Move all magic numbers to `utils/constants.py`

**Problem:** Several timeout and size values are hard-coded in files rather than defined in
`utils/constants.py`.

**Read first:** `utils/constants.py` (full), then check each file listed below.

**Changes:**

1. **In `utils/constants.py`**, add:
   ```python
   # Cloudflare tunnel
   TUNNEL_START_TIMEOUT: float = float(os.environ.get("TUNNEL_START_TIMEOUT", "25"))
   CLOUDFLARE_LOG_BUFFER_SIZE: int = 200

   # USDU result collection
   DYNAMIC_MODE_MAX_POLL_TIMEOUT: float = 10.0

   # Static mode job poll loop
   JOB_POLL_INTERVAL: float = 1.0
   JOB_POLL_MAX_ATTEMPTS: int = 20
   ```

2. **In `utils/cloudflare.py`** (or `utils/cloudflare/tunnel.py` after Task 2.2):
   - Replace `TUNNEL_START_TIMEOUT = 25` (line ~30) with import from constants.
   - Replace `> 200` log buffer check (line ~178) with `CLOUDFLARE_LOG_BUFFER_SIZE`.

3. **In `upscale/result_collector.py`** (line ~65):
   - Replace `min(10.0, timeout)` with `min(DYNAMIC_MODE_MAX_POLL_TIMEOUT, timeout)`.
   - Add import: `from ..utils.constants import DYNAMIC_MODE_MAX_POLL_TIMEOUT`.

4. **In `upscale/modes/static.py`**:
   - Replace `time.sleep(1.0)` with `time.sleep(JOB_POLL_INTERVAL)`.
   - Replace `max_attempts=20` with `max_attempts=JOB_POLL_MAX_ATTEMPTS`.
   - Add import: `from ...utils.constants import JOB_POLL_INTERVAL, JOB_POLL_MAX_ATTEMPTS`.

**Verify:** `grep -rn "= 25\b\|> 200\b\|10\.0\b\|max_attempts=20\b" utils/ upscale/modes/`
shows no magic number remnants in these files.

---

## Phase 6 — Low / Cleanup

### Task 6.1 — Remove dead code and import drift

**Problem:** `api/worker_routes.py` imports helpers from `workers/detection.py` that
are not used within that file. `workers/detection.py` `get_comms_channel()` may be dormant.

**Read first:** `api/worker_routes.py` (all imports at top of file), `workers/detection.py`
(full). Grep for every symbol imported in `worker_routes.py` to confirm usage.

**Changes:**

1. **In `api/worker_routes.py`**, identify every imported name from `workers.detection`
   (and any other module). For each, search the file body for usage. Remove any import
   line whose symbols are not referenced.

2. **For `workers/detection.py` `get_comms_channel()`**: grep the full codebase for
   `get_comms_channel`. If found only in `nodes/collector.py` imports but never called
   (check `collector.py` body), remove the import in `collector.py` and the function
   in `detection.py`. If it is called, leave it.

3. **Also in `nodes/collector.py`** (line 20): `from ..workers.detection import is_local_worker, get_comms_channel`.
   Check whether `is_local_worker` and `get_comms_channel` are referenced in the file body.
   Remove unused ones.

**Verify:** `python -m py_compile` on each modified file. No `ImportError` on module load.

---

### Task 6.2 — Standardize error response format across API routes

**Problem:** Some endpoints use `await handle_api_error(request, msg, status)`,
others call `web.json_response({"error": ...}, status=400)` directly, creating
inconsistent client-facing error shapes.

**Read first:** `utils/network.py` (`handle_api_error` implementation),
`api/job_routes.py` (lines 276-288 — list-of-errors response),
`api/config_routes.py` (find all direct `web.json_response` error calls).

**Changes:**

1. **Extend `handle_api_error`** (if needed) to accept a list of error strings:
   ```python
   async def handle_api_error(request, error, status: int = 500):
       if isinstance(error, list):
           return web.json_response({"errors": error}, status=status)
       msg = str(error)
       debug_log(f"API error [{status}]: {msg}")
       return web.json_response({"error": msg}, status=status)
   ```

2. **In `api/job_routes.py`** (lines ~287-288):
   ```python
   # Before:
   return web.json_response({"error": errors}, status=400)
   # After:
   return await handle_api_error(request, errors, 400)
   ```

3. **In `api/config_routes.py`**, replace all `return web.json_response({"error": ...}, status=...)`
   calls with `return await handle_api_error(request, ..., status)`.

4. **In `api/worker_routes.py`**, check for any direct `web.json_response` error patterns
   and standardize them.

**Verify:** `grep -rn 'json_response.*"error"' api/` returns zero results (all error
responses now flow through `handle_api_error`).

---

### Task 6.3 — Fix redundant server import alias

**Problem:** `api/job_routes.py` lines 10-11 have both:
```python
import server
import server as _server
```
Only one is needed.

**Read first:** `api/job_routes.py` (check which alias is used throughout the file).

**Changes:**

1. Find all usages of `server` and `_server` in `api/job_routes.py`.
2. Pick one (the dominant one, or `server` for consistency with other files).
3. Remove the unused import and update any references to the removed alias.

---

### Task 6.4 — Document mixin inheritance chain

**Problem:** `ResultCollectorMixin`, `StaticModeMixin`, `DynamicModeMixin` in
`upscale/result_collector.py` and `upscale/modes/` have no documentation of the required
method interface, making them hard to extend.

**Read first:** `upscale/result_collector.py`, `upscale/modes/static.py`,
`upscale/modes/dynamic.py`, `upscale/worker_comms.py`.

**Changes:**

1. **In `upscale/result_collector.py`**, add a docstring to `ResultCollectorMixin`
   listing abstract methods / expected attributes:
   ```python
   class ResultCollectorMixin:
       """
       Mixin for master-side result collection in USDU distributed jobs.

       Requires the consuming class to also mix in:
       - JobStateMixin (upscale.job_state) for job state access
       - WorkerCommsMixin (upscale.worker_comms) is NOT required here

       Expected to be used via multiple inheritance in DistributedUSDU node class.
       """
   ```

2. **In `upscale/modes/static.py`** and `upscale/modes/dynamic.py`**, add docstrings
   to `StaticModeMixin` and `DynamicModeMixin` listing what they call on `self` and what
   they require from co-mixins.

3. **No behavior changes** — documentation only.

---

## Verification Checklist (run after all phases)

```bash
# Python syntax check all modified files
find custom_nodes/ComfyUI-Distributed -name "*.py" | xargs python -m py_compile

# Confirm no bare usdu_managment god-module imports remain
grep -r "from.*usdu_managment import" custom_nodes/ComfyUI-Distributed \
  --include="*.py" | grep -v "usdu_managment.py"

# Confirm trace helpers are in one place
grep -rn "def _trace_prefix\|def _trace_debug\|def _trace_info" \
  custom_nodes/ComfyUI-Distributed --include="*.py"
# Expected: 0 results (all moved to utils/trace_logger.py as trace_prefix etc.)

# Confirm no lock-held network calls remain
grep -A5 "distributed_tile_jobs_lock" custom_nodes/ComfyUI-Distributed/utils/usdu_managment.py \
  | grep "await probe_worker"
# Expected: 0 results

# Confirm mutating JS calls don't retry
grep -A3 "queueDistributed\|launchWorker\|stopWorker\|startTunnel\|stopTunnel" \
  custom_nodes/ComfyUI-Distributed/web/apiClient.js | grep "retry: false"
# Expected: one hit per mutating call

# Confirm no raw web.json_response error patterns remain
grep -rn 'web\.json_response.*"error"' custom_nodes/ComfyUI-Distributed/api/ --include="*.py"
# Expected: 0 results
```

---

## Notes for the implementing agent

- **Do not change any HTTP endpoint paths or request/response JSON schemas.** All changes
  are internal to Python modules and JS modules — the public API surface is frozen.
- **Do not change `upscale/job_models.py`** — the typed dataclasses are already correct.
- **Phase 1 tasks are prerequisites for Phase 2.** Specifically, Task 1.1 must be done
  before Task 2.1 since the requeued function moves in 2.1.
- **Task 2.1 is the most complex.** Read all import sites before moving any code. Use
  re-exports in the shim `usdu_managment.py` generously to avoid breaking callers.
- **Tasks within the same phase are independent** and can be done in any order or in
  parallel.
- **After each task**, run `python -m py_compile` on all touched files before moving on.
