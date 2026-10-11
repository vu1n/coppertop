"""Single-call Clef/Jev transports; credentials stay in the calling process."""
from dataclasses import dataclass, field
import http.client
import json
import math
import re
import ssl
import socket
import threading
import time
import uuid
from urllib.parse import urlsplit

from .chords import canonical, digest
from .decision_gate import SCHEMA_SHA256, validate_answers
from .decision_recording import provider_body

MAX_BODY_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 256 * 1024


class ProviderError(RuntimeError):
    pass


def post_json(url, token, body, *, timeout_seconds, owner=None):
    """Direct TLS connection, one POST, no redirects/proxies/retries or error-body logs."""
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in ('api.cloudflare.com', 'openrouter.ai')
            or parsed.port is not None or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('Fixed provider HTTPS endpoint required')
    encoded = canonical(body)
    if len(encoded) > MAX_BODY_BYTES or not 0 < timeout_seconds <= 30:
        raise ValueError('Bounded provider request required')
    if not isinstance(token, str) or not token or '\r' in token or '\n' in token:
        raise ValueError('Existing bearer credential required')
    class CancellableConnection(http.client.HTTPSConnection):
        def connect(self):
            if owner.cancelled.is_set():
                raise ProviderError('Owned scorer cancelled')
            super().connect()
            if owner.cancelled.is_set():
                self.close()
                raise ProviderError('Owned scorer cancelled')

    connection_class = CancellableConnection if owner is not None else http.client.HTTPSConnection
    connection = connection_class(parsed.hostname, timeout=timeout_seconds,
                                  context=ssl.create_default_context())
    if owner is not None:
        owner.connection = connection
        if owner.cancelled.is_set():
            raise ProviderError('Owned scorer cancelled')
    try:
        if owner is not None:
            # Connect may outlast cancellation before a socket exists. The override
            # also prevents an implicit reconnect after cancel closes that socket.
            connection.connect()
            if owner.cancelled.is_set():
                raise ProviderError('Owned scorer cancelled')
        connection.request('POST', parsed.path, body=encoded,
                           headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        response = connection.getresponse()
        if response.status != 200:
            raise ProviderError('Provider HTTP status ' + str(response.status))
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError('Provider response exceeds bound')
        return json.loads(raw)
    except ProviderError:
        raise
    except Exception:
        # Third-party error messages may contain request headers or input bytes.
        raise ProviderError('Provider transport or JSON decoding failed') from None
    finally:
        connection.close()


class OwnedPost:
    def __init__(self):
        self.connection = None
        self.cancelled = threading.Event()

    def __call__(self, url, token, body, *, timeout_seconds):
        return post_json(url, token, body, timeout_seconds=timeout_seconds, owner=self)

    def cancel(self):
        self.cancelled.set()
        connection = self.connection
        if connection is not None:
            sock = connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # Already-closed transport is the cancellation target.
            connection.close()


@dataclass(frozen=True)
class DecisionProvider:
    provider: str
    token: str = field(repr=False)
    account_id: str | None = None
    model: str | None = None
    timeout_seconds: float = 5

    def __post_init__(self):
        if self.provider not in ('cloudflare', 'openrouter'):
            raise ValueError('Supported decision provider required')
        default = 'clef-flash' if self.provider == 'cloudflare' else 'typesafe/jev-1.13'
        object.__setattr__(self, 'model', self.model or default)
        allowed = ('clef-flash', 'clef') if self.provider == 'cloudflare' else ('typesafe/jev-1.13',)
        if self.model not in allowed:
            raise ValueError('Explicit supported model version required; latest aliases rejected')
        if self.provider == 'cloudflare' and (not isinstance(self.account_id, str)
                or not re.fullmatch(r'[a-fA-F0-9]{32}', self.account_id)):
            raise ValueError('Cloudflare account ID required')
        if (type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds)
                or not 0 < self.timeout_seconds <= 30):
            raise ValueError('Bounded transport timeout required')

    @property
    def endpoint(self):
        if self.provider == 'openrouter':
            return 'https://openrouter.ai/api/v1/systemone'
        return f'https://api.cloudflare.com/client/v4/accounts/{self.account_id}/ai/run/@cf/cloudflare/{self.model}'

    def score(self, store, case_id, *, execute=False, transport=post_json):
        body = provider_body(store, case_id, self.model, vision=self.provider == 'cloudflare')
        payload = canonical(body)
        if len(payload) > MAX_BODY_BYTES:
            raise ValueError('Scorer payload exceeds bound')
        binding = {'case_id': case_id, 'schema_sha256': SCHEMA_SHA256,
                   'provider': self.provider, 'requested_model': self.model,
                   'input_modality': body['state']['input_modality'],
                   'request_sha256': digest(payload), 'request_bytes': len(payload)}
        if not execute:
            return dict(binding, status='prepared', provider_calls=0)
        started = time.monotonic_ns()
        response = transport(self.endpoint, self.token, body, timeout_seconds=self.timeout_seconds)
        finished = time.monotonic_ns()
        if not isinstance(response, dict):
            raise ProviderError('Object provider response required')
        if self.provider == 'cloudflare':
            if response.get('success') is not True or not isinstance(response.get('result'), dict):
                raise ProviderError('Successful Cloudflare result envelope required')
            response = response['result']
        answers = validate_answers(response.get('answers'))
        served_model = response.get('model')
        if not isinstance(served_model, str) or not 0 < len(served_model) <= 128:
            raise ProviderError('Served model identity required')
        expected = (self.model, '@cf/cloudflare/' + self.model) if self.provider == 'cloudflare' else (self.model,)
        if served_model not in expected and not (self.provider == 'openrouter'
                and re.fullmatch(re.escape(self.model) + r'-[0-9]{8}', served_model)):
            raise ProviderError('Unexpected served model')
        usage = response.get('usage', {})
        if not isinstance(usage, dict):
            raise ProviderError('Object usage required')
        # Persist only typed usage, never arbitrary provider text/headers.
        checked_usage = {}
        for key in ('input_tokens', 'output_tokens'):
            value = usage.get(key)
            if value is not None:
                if type(value) is not int or value < 0:
                    raise ProviderError('Nonnegative usage count required')
                checked_usage[key] = value
        cost = usage.get('cost')
        if cost is not None:
            if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
                raise ProviderError('Finite provider cost required')
            checked_usage['cost_usd'] = cost
        return dict(binding, status='scored', provider_calls=1, served_model=served_model,
                    provider_revision_pinned=False, answers=answers, usage=checked_usage,
                    submitted_ns=started, finished_ns=finished, latency_ns=finished - started,
                    live_intervention_executed=False)


class BoundScorer:
    """Detached immutable input with one owned, cancellable HTTP operation."""
    def __init__(self, provider, store, case_id):
        self.provider, self.store, self.case_id = provider, store, case_id
        self.transport = OwnedPost()
        self.used = False
        self.receipt = None
        self.overlay = None

    def __call__(self):
        if self.used:
            raise ValueError('One scorer call per boundary')
        self.used = True
        attempt_id = uuid.uuid4().hex
        request_id = self.store.put(canonical(dict(self.provider.score(self.store, self.case_id), attempt_id=attempt_id)))
        try:
            self.receipt = dict(self.provider.score(self.store, self.case_id, execute=True,
                                                   transport=self.transport), attempt_id=attempt_id)
        except Exception as error:
            self.failure_id = self.store.put(canonical({'status': 'failed', 'request_id': request_id,
                'attempt_id': attempt_id, 'provider_calls_attempted': 1, 'automatic_retries': 0,
                'error_type': type(error).__name__, 'billable_status': 'unknown'}))
            raise
        from .decision_recording import save_judgment
        self.overlay = save_judgment(self.store, self.case_id, self.receipt)
        return self.receipt['answers']

    def cancel(self):
        self.transport.cancel()
