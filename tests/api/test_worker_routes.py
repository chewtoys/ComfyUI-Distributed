import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class _FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status


class _FakeRequest:
    def __init__(self, payload=None, match_info=None, query=None):
        self._payload = payload
        self.match_info = match_info or {}
        self.query = query or {}

    async def json(self):
        return self._payload


class _DummyWorkerManager:
    def __init__(self):
        self.processes = {}

    def launch_worker(self, worker):
        worker_id = str(worker["id"])
        self.processes[worker_id] = {
            "pid": 12345,
            "log_file": f"/tmp/distributed_worker_{worker_id}.log",
            "process": None,
        }
        return 12345

    def _is_process_running(self, _pid):
        return False

    def save_processes(self):
        return None

    def stop_worker(self, _worker_id):
        return True, "Stopped"

    def get_managed_workers(self):
        return []


class _ImmediateLoop:
    async def run_in_executor(self, _executor, func, *args):
        return func(*args)


def _load_worker_routes_module():
    module_path = Path(__file__).resolve().parents[2] / "api" / "worker_routes.py"
    package_name = "dist_api_worker_testpkg"

    for mod_name in list(sys.modules):
        if mod_name == package_name or mod_name.startswith(f"{package_name}."):
            del sys.modules[mod_name]

    root_pkg = types.ModuleType(package_name)
    root_pkg.__path__ = []
    sys.modules[package_name] = root_pkg

    api_pkg = types.ModuleType(f"{package_name}.api")
    api_pkg.__path__ = []
    sys.modules[f"{package_name}.api"] = api_pkg

    utils_pkg = types.ModuleType(f"{package_name}.utils")
    utils_pkg.__path__ = []
    sys.modules[f"{package_name}.utils"] = utils_pkg

    workers_pkg = types.ModuleType(f"{package_name}.workers")
    workers_pkg.__path__ = []
    workers_pkg.get_worker_manager = lambda: _DummyWorkerManager()
    sys.modules[f"{package_name}.workers"] = workers_pkg

    detection_module = types.ModuleType(f"{package_name}.workers.detection")
    detection_module.is_local_worker = lambda *_args, **_kwargs: True
    detection_module.is_same_physical_host = lambda *_args, **_kwargs: True
    detection_module.get_machine_id = lambda: "machine-id"
    detection_module.is_docker_environment = lambda: False
    detection_module.is_runpod_environment = lambda: False
    detection_module.get_comms_channel = lambda *_args, **_kwargs: "lan"
    sys.modules[f"{package_name}.workers.detection"] = detection_module

    created_aiohttp_stub = False
    if "aiohttp" not in sys.modules:
        created_aiohttp_stub = True
        aiohttp_module = types.ModuleType("aiohttp")

        class _ClientTimeout:
            def __init__(self, total=None):
                self.total = total

        class _WSMsgType:
            TEXT = "TEXT"
            ERROR = "ERROR"
            CLOSED = "CLOSED"

        class _WebSocketResponse:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

            async def prepare(self, _request):
                return None

            async def send_json(self, _payload):
                return None

            def __aiter__(self):
                async def _empty():
                    if False:
                        yield None
                return _empty()

        aiohttp_module.ClientTimeout = _ClientTimeout
        aiohttp_module.WSMsgType = _WSMsgType
        aiohttp_module.web = types.SimpleNamespace(
            json_response=lambda payload, status=200: _FakeResponse(payload, status=status),
            WebSocketResponse=_WebSocketResponse,
        )
        sys.modules["aiohttp"] = aiohttp_module

    class _Routes:
        def get(self, _path):
            def _decorator(fn):
                return fn
            return _decorator

        def post(self, _path):
            def _decorator(fn):
                return fn
            return _decorator

    server_module = types.ModuleType("server")
    server_module.PromptServer = types.SimpleNamespace(instance=types.SimpleNamespace(routes=_Routes()))
    sys.modules["server"] = server_module

    if "torch" not in sys.modules:
        torch_module = types.ModuleType("torch")
        torch_module.cuda = types.SimpleNamespace(
            is_available=lambda: False,
            empty_cache=lambda: None,
            ipc_collect=lambda: None,
            current_device=lambda: 0,
            device_count=lambda: 0,
        )
        sys.modules["torch"] = torch_module

    logging_module = types.ModuleType(f"{package_name}.utils.logging")
    logging_module.debug_log = lambda *_args, **_kwargs: None
    logging_module.log = lambda *_args, **_kwargs: None
    sys.modules[f"{package_name}.utils.logging"] = logging_module

    config_module = types.ModuleType(f"{package_name}.utils.config")
    config_module.load_config = lambda: {"workers": []}
    sys.modules[f"{package_name}.utils.config"] = config_module

    network_module = types.ModuleType(f"{package_name}.utils.network")

    async def _handle_api_error(_request, error, status=500):
        return _FakeResponse({"status": "error", "message": str(error)}, status=status)

    network_module.handle_api_error = _handle_api_error
    network_module.normalize_host = lambda value: value
    network_module.build_worker_url = lambda worker, endpoint="": f"http://localhost:{worker.get('port', 8188)}{endpoint}"

    async def _probe_worker(*_args, **_kwargs):
        return None

    network_module.probe_worker = _probe_worker

    async def _get_client_session():
        raise RuntimeError("not used in these tests")

    network_module.get_client_session = _get_client_session
    sys.modules[f"{package_name}.utils.network"] = network_module

    constants_module = types.ModuleType(f"{package_name}.utils.constants")
    constants_module.CHUNK_SIZE = 8192
    sys.modules[f"{package_name}.utils.constants"] = constants_module

    async_helpers_module = types.ModuleType(f"{package_name}.utils.async_helpers")

    async def _queue_prompt_payload(*_args, **_kwargs):
        return "prompt-id"

    async_helpers_module.queue_prompt_payload = _queue_prompt_payload
    sys.modules[f"{package_name}.utils.async_helpers"] = async_helpers_module

    schemas_module = types.ModuleType(f"{package_name}.api.schemas")

    def _require_fields(data, *fields):
        missing = []
        for field in fields:
            value = data.get(field) if isinstance(data, dict) else None
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(field)
        return missing

    def _validate_worker_id(worker_id, config):
        return any(str(worker.get("id")) == str(worker_id) for worker in config.get("workers", []))

    schemas_module.require_fields = _require_fields
    schemas_module.validate_worker_id = _validate_worker_id
    sys.modules[f"{package_name}.api.schemas"] = schemas_module

    spec = importlib.util.spec_from_file_location(f"{package_name}.api.worker_routes", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)

    if created_aiohttp_stub:
        sys.modules.pop("aiohttp", None)

    return module


worker_routes = _load_worker_routes_module()


class WorkerRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def test_launch_worker_valid_id_returns_200(self):
        manager = _DummyWorkerManager()
        config = {"workers": [{"id": "worker-a", "name": "Worker A", "port": 8188}]}
        request = _FakeRequest({"worker_id": "worker-a"})

        with patch.object(worker_routes, "get_worker_manager", return_value=manager), patch.object(
            worker_routes, "load_config", return_value=config
        ), patch.object(
            worker_routes.asyncio, "get_running_loop", return_value=_ImmediateLoop()
        ):
            response = await worker_routes.launch_worker_endpoint(request)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload.get("status"), "success")
        self.assertEqual(response.payload.get("pid"), 12345)

    async def test_launch_worker_unknown_id_returns_404(self):
        manager = _DummyWorkerManager()
        config = {"workers": [{"id": "worker-a", "name": "Worker A", "port": 8188}]}
        request = _FakeRequest({"worker_id": "missing-worker"})

        with patch.object(worker_routes, "get_worker_manager", return_value=manager), patch.object(
            worker_routes, "load_config", return_value=config
        ):
            response = await worker_routes.launch_worker_endpoint(request)

        self.assertEqual(response.status, 404)
        self.assertIn("not found", response.payload.get("message", "").lower())

    async def test_worker_log_returns_content_json(self):
        manager = _DummyWorkerManager()
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
            handle.write("line-1\nline-2\nline-3\n")
            log_path = handle.name

        manager.processes["worker-a"] = {
            "pid": 9999,
            "log_file": log_path,
            "process": None,
        }

        request = _FakeRequest(match_info={"worker_id": "worker-a"}, query={"lines": "2"})
        try:
            with patch.object(worker_routes, "get_worker_manager", return_value=manager), patch.object(
                worker_routes.asyncio, "get_running_loop", return_value=_ImmediateLoop()
            ):
                response = await worker_routes.get_worker_log_endpoint(request)
        finally:
            if os.path.exists(log_path):
                os.remove(log_path)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload.get("status"), "success")
        self.assertIn("content", response.payload)
        self.assertIn("line-3", response.payload["content"])


if __name__ == "__main__":
    unittest.main()
