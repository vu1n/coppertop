import json
import unittest
from unittest.mock import patch
from coppertop.decision_latency import SerialSession
from coppertop.decision_provider import ProviderError

class Connection:
    def __init__(self, *args, **kwargs):
        self.sock = None
        self.connects = 0
        self.requests = 0
        self.fail = False
    def connect(self):
        self.connects += 1
        self.sock = object()
    def request(self, *args, **kwargs):
        self.requests += 1
        if self.fail:
            raise OSError('private diagnostic')
    def getresponse(self):
        return self
    status = 200
    def read(self, limit):
        return b'{"ok":true}'
    def close(self):
        self.sock = None

class LatencyTests(unittest.TestCase):
    def test_reuse_and_no_retry_on_stale_socket(self):
        with patch('coppertop.decision_latency.http.client.HTTPSConnection', Connection):
            session = SerialSession('https://openrouter.ai/api/v1/systemone')
            args = (session.endpoint, 'test-token', {})
            session(*args, timeout_seconds=10)
            self.assertFalse(session.measurement['socket_reused'])
            session(*args, timeout_seconds=10)
            self.assertTrue(session.measurement['socket_reused'])
            self.assertEqual(session.connection.connects, 1)
            session.connection.fail = True
            with self.assertRaisesRegex(ProviderError, '^Benchmark transport failed; no retry$'):
                session(*args, timeout_seconds=10)
            self.assertEqual(session.connection.requests, 3)
            self.assertTrue(session.closed)
            with self.assertRaises(ValueError):
                session(*args, timeout_seconds=10)

    def test_cross_endpoint_and_header_injection_rejected(self):
        with patch('coppertop.decision_latency.http.client.HTTPSConnection', Connection):
            session = SerialSession('https://openrouter.ai/api/v1/systemone')
            for url, token in [('https://api.cloudflare.com/wrong','test'),
                               (session.endpoint,'test\r\ninjected')]:
                with self.assertRaises(ValueError):
                    session(url,token,{},timeout_seconds=10)
            self.assertEqual(session.connection.requests,0)
            session.close()
