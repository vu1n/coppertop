"""Serial benchmark transport; not the live runner's cancellable transport."""
import http.client
import json
import ssl
import time
from urllib.parse import urlsplit

from .chords import canonical
from .decision_provider import MAX_BODY_BYTES, MAX_RESPONSE_BYTES, ProviderError


class SerialSession:
    """One fixed endpoint, no redirects/retries; explicit measurements of TLS reuse."""

    def __init__(self, endpoint):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'https' or parsed.hostname not in
                ('api.cloudflare.com', 'openrouter.ai') or parsed.port is not None
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('Fixed provider HTTPS endpoint required')
        self.endpoint = endpoint
        self.connection = http.client.HTTPSConnection(
            parsed.hostname, timeout=10, context=ssl.create_default_context())
        self.path = parsed.path
        self.measurement = None
        self.closed = False

    def close(self):
        self.closed = True
        self.connection.close()

    def __call__(self, url, token, body, *, timeout_seconds):
        if self.closed or url != self.endpoint or timeout_seconds != 10:
            raise ValueError('Open fixed-endpoint ten-second benchmark session required')
        encoded = canonical(body)
        if len(encoded) > MAX_BODY_BYTES or not isinstance(token, str) or not token or any(
                c in token for c in '\r\n'):
            raise ValueError('Bounded body and bearer credential required')
        started = time.monotonic_ns()
        reused = self.connection.sock is not None
        try:
            if not reused:
                self.connection.connect()
            connected = time.monotonic_ns()
            self.connection.request('POST', self.path, body=encoded, headers={
                'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            sent = time.monotonic_ns()
            response = self.connection.getresponse()
            headers = time.monotonic_ns()
            if response.status != 200:
                raise ProviderError('Provider HTTP status ' + str(response.status))
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            finished = time.monotonic_ns()
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ProviderError('Provider response exceeds bound')
            result = json.loads(raw)
            self.measurement = {'socket_reused': reused,
                'connect_ns': connected - started, 'send_ns': sent - connected,
                'response_headers_ns': headers - sent, 'read_ns': finished - headers,
                'transport_ns': finished - started,
                'server_closed_connection': self.connection.sock is None}
            return result
        except Exception:
            self.close()
            raise ProviderError('Benchmark transport failed; no retry') from None
