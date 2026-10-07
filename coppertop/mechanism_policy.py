"""Seeded pinned official policy, trusted loopback transport, bounded cancellation."""
import socket
import threading
from types import SimpleNamespace

from .performance_runtime import MODEL, PORT, load_official


class SeededModel:
    def __init__(self, model):
        self.model = model
        self.device, self.dtype = model.device, model.dtype

    def __call__(self, **inputs):
        import torch
        seed = inputs.pop('_coppertop_seed')
        if type(seed) is not int or not 0 <= seed < 2**63:
            raise ValueError('Explicit bounded policy seed required')
        # The server is serial and single-device. Restore ambient RNG after each call.
        with torch.random.fork_rng(devices=[self.device.index or 0]):
            torch.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)
            return self.model(**inputs)


class OfficialQuery:
    def __init__(self):
        from transformers import AutoProcessor
        entry = load_official('entry')
        self.native = entry.Client.__new__(entry.Client)
        self.native.processor = AutoProcessor.from_pretrained(str(MODEL),
            trust_remote_code=True, use_fast=False, local_files_only=True)
        self.native.client_socket = socket.create_connection(('127.0.0.1', PORT), timeout=5)
        self.native.client_socket.settimeout(5)
        self.seed = None
        self.lock = threading.Lock()
        self.client = entry.EvalClient.__new__(entry.EvalClient)
        self.client.client, self.client.processor = self, self.native.processor
        self.client.robot_type, self.client.crop_ratio = 'robocasa365', .95
        if self.client.robot_type not in self.native.processor.list_robot_types():
            self.close()
            raise ValueError('Pinned robot processor unavailable')

    def __call__(self, *args, **request):
        if request:
            # Called only by pinned EvalClient.infer within our serialized outer call.
            request['_coppertop_seed'] = self.seed
            return self.native(**request)
        inputs, seed = args
        with self.lock:
            self.seed = seed
            return self.client.infer(*inputs)

    def cancel(self):
        try:
            self.native.client_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # An already closed owned transport is successfully cancelled.
        self.close()

    def close(self):
        self.native.client_socket.close()


def serve(output):
    """Runs only on an already allocated CUDA worker; provisions nothing."""
    import signal
    import torch
    import transformers
    from .image_check import require_native_interpreter, require_dependencies, pinned_dependencies
    from .performance_runtime import source_gate, save, read_json
    from .smoke_runtime import verify_snapshot
    from pathlib import Path
    require_native_interpreter()
    require_dependencies(pinned_dependencies(Path('/app/requirements-gpu-smoke.lock'), Path('/app/requirements-r2.txt')))
    source_gate()
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise ValueError('Single CUDA device required')
    signal.alarm(240)
    pins = read_json(Path('/app/containers/gpu-smoke-pins.json'))
    verify_snapshot(MODEL, pins['model'])
    torch.set_grad_enabled(False)
    model, info = transformers.AutoModel.from_pretrained(MODEL, local_files_only=True,
        trust_remote_code=True, attn_implementation='flash_attention_2', dtype=torch.bfloat16,
        low_cpu_mem_usage=True, output_loading_info=True)
    if any(info.get(k) for k in ('missing_keys', 'unexpected_keys', 'mismatched_keys', 'error_msgs')):
        raise ValueError('Official frozen model mapping incomplete')
    model = model.cuda().eval()
    official = load_official('server')
    class ReadySocket(socket.socket):
        def listen(self, backlog):
            super().listen(backlog)
            save(output/'ready.json', {'frozen': True, 'cuda_devices': 1, 'seeded_wrapper': True})
    # Publish readiness only after our own socket binds/listens, never before.
    # This scoped module shim leaves the pinned wire/forward code unchanged.
    official.socket = SimpleNamespace(socket=ReadySocket, AF_INET=socket.AF_INET,
        SOCK_STREAM=socket.SOCK_STREAM, SOL_SOCKET=socket.SOL_SOCKET, SO_REUSEADDR=socket.SO_REUSEADDR)
    server_class = official.Server
    server = server_class.__new__(server_class)
    server.host, server.port, server.model = '127.0.0.1', PORT, SeededModel(model)
    torch.cuda.synchronize()
    server.serve()
