import base64
import hashlib
import hmac
import io
import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from coppertop.r2_session import (SessionScope, mint, _session, BoundedClient,
                                  R2StoreFactory, ENV_NAMES)

ENDPOINT = 'https://' + 'a' * 32 + '.r2.cloudflarestorage.com'
SHA = 'ab' * 32
PATH = 'chords/objects/ab/' + SHA
SOURCE = SessionScope('source', 'coppertop', 'chords', (PATH,))
REPORT = SessionScope('report', 'coppertop', 'chords/gpu-reload/12345678-1234-1234-1234-123456789abc')


class FakeClient:
    def __init__(self):
        self.calls = []
        self.meta = SimpleNamespace(events=SimpleNamespace(register=lambda name, callback: None))
        self.failure = False

    def head_object(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise OSError('injected failure')
        return {'ContentLength': 3}

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        return {'ContentLength': 3, 'Body': io.BytesIO(b'abc')}

    def put_object(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise OSError('injected failure')
        return {}


class SessionsTest(unittest.TestCase):
    def test_real_sdk_multipart_serialization_matches_guard_offline(self):
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            self.skipTest('Existing optional R2 SDK unavailable; run in controller environment')
        prefix='chords/mechanism/mechanism-abcdef0123456789'
        scope=SessionScope('mechanism_archive','coppertop',prefix)
        # Explicit fixture credentials prevent SDK credential discovery; only
        # request serialization runs, never client API dispatch/network.
        sdk=boto3.client('s3',endpoint_url=ENDPOINT,region_name='auto',
            aws_access_key_id='fixture',aws_secret_access_key='fixture',
            config=Config(s3={'addressing_style':'path'}))
        client=BoundedClient(sdk,scope,ENDPOINT,1000,lambda:1)
        base={'Bucket':scope.bucket,'Key':prefix+'/objects/aa/'+'a'*64}
        operations={'CreateMultipartUpload':{},'UploadPart':{'UploadId':'opaque+/=','PartNumber':1,'Body':b'x'},
            'ListParts':{'UploadId':'opaque+/=','PartNumberMarker':0},
            'CompleteMultipartUpload':{'UploadId':'opaque+/=','MultipartUpload':{'Parts':[{'PartNumber':1,'ETag':'opaque'}]}},
            'AbortMultipartUpload':{'UploadId':'opaque+/='}}
        for operation,parameters in operations.items():
            model=sdk.meta.service_model.operation_model(operation)
            request=sdk._convert_to_request_dict(dict(base,**parameters),model,ENDPOINT)
            client.destination_guard(SimpleNamespace(url=request['url'],method=request['method']))

    def test_full_recording_scope_keeps_exact_prefix_and_existing_actions(self):
        from coppertop.r2_session import LIMITS
        scope=SessionScope('mechanism_full','coppertop','chords/mechanism/mechanism-'+'a'*16)
        self.assertEqual(scope.actions,('GetObject','HeadObject','PutObject'))
        self.assertEqual(LIMITS[scope.role],(30000,1024**3))
        with self.assertRaises(ValueError):SessionScope('mechanism_full','coppertop','chords')
        client=FakeClient();bounded=BoundedClient(client,scope,ENDPOINT,expires_at=1000,clock=lambda:100)
        key=scope.prefix+'/objects/ab/'+SHA
        bounded.put_object(Bucket='coppertop',Key=key,Body=b'abc',IfNoneMatch='*')
        self.assertEqual(client.calls[-1]['StorageClass'],'STANDARD')
        with self.assertRaises(ValueError):
            bounded.put_object(Bucket='coppertop',Key='chords/objects/ab/'+SHA,Body=b'abc',IfNoneMatch='*')

    def test_official_jwt_derivation_and_redaction(self):
        creds = mint('parent-id', 'dummy-secret', endpoint=ENDPOINT, scope=SOURCE, now=100)
        jwt = base64.b64decode(creds.session_token).decode()[4:]
        message, signature = jwt.rsplit('.', 1)
        actual = base64.urlsafe_b64decode(signature + '=' * (-len(signature) % 4))
        self.assertEqual(actual, hmac.new(b'dummy-secret', message.encode(), hashlib.sha256).digest())
        self.assertEqual(creds.secret_access_key, hashlib.sha256(jwt.encode()).hexdigest())
        claims = json.loads(base64.urlsafe_b64decode(message.split('.')[1] + '=='))
        self.assertEqual(claims['paths'], {'prefixPaths': [], 'objectPaths': [PATH]})
        self.assertEqual(claims['actions'], ['GetObject', 'HeadObject'])
        self.assertEqual(claims['sub'], 'a' * 32)
        self.assertEqual(claims['iat'], 100)
        self.assertEqual(claims['exp'], 7300)
        self.assertNotIn('parent-id', repr(creds))
        self.assertNotIn(creds.session_token, repr(creds))
        _session(creds.environment(), ENDPOINT, SOURCE, 101)

    def test_expired_changed_role_and_parent_env_rejected(self):
        creds = mint('parent-id', 'dummy-secret', endpoint=ENDPOINT, scope=SOURCE, now=100)
        for scope, now in ((SOURCE, 99), (SOURCE, 7300), (REPORT, 101)):
            with self.assertRaises(ValueError):
                _session(creds.environment(), ENDPOINT, scope, now)
        with self.assertRaises(ValueError):
            _session({'AWS_ACCESS_KEY_ID': 'x', 'AWS_SECRET_ACCESS_KEY': 'y'}, ENDPOINT, SOURCE, 101)
        env = creds.environment()
        env[ENV_NAMES[1]] = 'changed'
        with self.assertRaises(ValueError):
            _session(env, ENDPOINT, SOURCE, 101)

    def test_factory_serializes_public_config_and_explicit_session_only(self):
        creds = mint('parent-id', 'dummy-secret', endpoint=ENDPOINT, scope=SOURCE, now=100)
        captured = {}
        fake = FakeClient()
        def create(*args, **kwargs):
            captured.update(kwargs)
            return fake
        config = lambda **kwargs: kwargs
        modules = {'boto3': SimpleNamespace(client=create),
                   'botocore': SimpleNamespace(),
                   'botocore.config': SimpleNamespace(Config=config)}
        factory = R2StoreFactory(ENDPOINT, (SOURCE,))
        self.assertNotIn('dummy-secret', repr(factory))
        with patch.dict('sys.modules', modules), patch.dict('os.environ', creds.environment(), clear=True), patch('coppertop.r2_session.time.time', return_value=101):
            store = factory('source')
        self.assertEqual(store.prefix, 'chords')
        self.assertEqual(captured['aws_session_token'], creds.session_token)
        self.assertEqual(captured['config']['connect_timeout'], 30)
        self.assertEqual(captured['config']['read_timeout'], 60)
        self.assertEqual(captured['config']['retries'], {'total_max_attempts': 1})
        with patch.dict('os.environ', {'AWS_ACCESS_KEY_ID': 'parent', 'AWS_SECRET_ACCESS_KEY': 'parent-secret'}, clear=True):
            with self.assertRaises(ValueError):
                factory('source')

    def test_planned_report_namespace_is_bounded(self):
        prefix = 'chords/gpu-reload/reload-643845c8f57341d6'
        scope = SessionScope('report', 'coppertop', prefix)
        self.assertEqual(scope.paths, {'objectPaths': [], 'prefixPaths': [prefix + '/objects/']})
        creds = mint('parent-id', 'dummy-secret', endpoint=ENDPOINT, scope=scope, now=100)
        jwt = base64.b64decode(creds.session_token).decode()[4:]
        payload = jwt.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        self.assertEqual(claims['paths']['prefixPaths'], [prefix + '/objects/'])
        self.assertTrue(scope.allows(prefix + '/objects/ab/' + SHA))
        self.assertFalse(scope.allows('chords/gpu-reload/reload-0000000000000000/objects/ab/' + SHA))
        self.assertFalse(scope.allows(prefix + '/manifest.json'))
        for bad in ('chords/gpu-reload/', 'chords/gpu-reload/reload-deadbeef',
                    'chords/gpu-reload/reload-643845c8f57341d6-extra',
                    'chords/gpu-reload/' + '-' * 36):
            with self.assertRaises(ValueError):
                SessionScope('report', 'coppertop', bad)

    def test_invalid_scopes_and_endpoint(self):
        for endpoint in ('http://evil.test', ENDPOINT + '/wrong', ENDPOINT + ':443', ENDPOINT + '?x=1'):
            with self.assertRaises(ValueError):
                mint('id', 'secret', endpoint=endpoint, scope=SOURCE, now=100)
        for paths in ((), ('chords/',), (PATH, PATH), tuple('chords/objects/ab/' + SHA for _ in range(18))):
            with self.assertRaises(ValueError):
                SessionScope('source', 'coppertop', 'chords', paths)
        with self.assertRaises(ValueError):
            SessionScope('report', 'coppertop', 'chords')
        with self.assertRaises(ValueError):
            R2StoreFactory(ENDPOINT, (SOURCE, SOURCE))

    def test_guard_denies_wrong_host_path_method_before_send(self):
        fake = FakeClient()
        client = BoundedClient(fake, SOURCE, ENDPOINT, 1000, lambda: 101)
        good = ENDPOINT + '/coppertop/' + PATH
        client.destination_guard(SimpleNamespace(url=good, method='GET'))
        for url, method in ((good.replace('.r2.', '.evil.'), 'GET'),
                            (good + '?list-type=2', 'GET'),
                            (ENDPOINT + '/coppertop/outside', 'GET'),
                            (good, 'PUT'), (good, 'DELETE')):
            with self.assertRaises(ValueError):
                client.destination_guard(SimpleNamespace(url=url, method=method))
        self.assertEqual(fake.calls, [])

    def test_failed_requests_reserved_and_roles_enforced(self):
        fake = FakeClient()
        client = BoundedClient(fake, SOURCE, ENDPOINT, 1000, lambda: 101)
        fake.failure = True
        for _ in range(128):
            with self.assertRaises(OSError):
                client.head_object(Bucket='coppertop', Key=PATH)
        with self.assertRaises(ValueError):
            client.head_object(Bucket='coppertop', Key=PATH)
        self.assertEqual(len(fake.calls), 128)
        with self.assertRaises(ValueError):
            client.get_object(Bucket='other', Key=PATH)
        with self.assertRaises(ValueError):
            client.put_object(Bucket='coppertop', Key=PATH, Body=b'x', IfNoneMatch='*')
        with self.assertRaisesRegex(ValueError,'outside approved role'):
            client.create_multipart_upload(Bucket=SOURCE.bucket,Key=SOURCE.object_paths[0])

    def test_read_byte_reservation_and_bounded_body(self):
        fake = FakeClient()
        client = BoundedClient(fake, SOURCE, ENDPOINT, 1000, lambda: 101)
        response = client.get_object(Bucket='coppertop', Key=PATH)
        self.assertEqual(client.read_bytes, 3)
        with self.assertRaises(ValueError):
            response['Body'].read(-1)
        self.assertEqual(response['Body'].read(3), b'abc')
        self.assertEqual(response['Body'].read(1), b'')
        client.read_bytes = 3 * 1024**3
        with self.assertRaises(ValueError):
            client.get_object(Bucket='coppertop', Key=PATH)

    def test_report_write_cap_reserved_on_failure_no_retry(self):
        fake = FakeClient()
        client = BoundedClient(fake, REPORT, ENDPOINT, 1000, lambda: 101)
        key = REPORT.prefix + '/objects/ab/' + SHA
        fake.failure = True
        with self.assertRaises(OSError):
            client.put_object(Bucket='coppertop', Key=key, Body=b'x' * 65536, IfNoneMatch='*')
        with self.assertRaises(ValueError):
            client.put_object(Bucket='coppertop', Key=key, Body=b'x', IfNoneMatch='*')
        self.assertEqual(client.write_bytes, 65536)
        self.assertEqual(len(fake.calls), 1)
        client.expires_at = 100
        with self.assertRaises(ValueError):
            client.head_object(Bucket='coppertop', Key=key)


if __name__ == '__main__':
    unittest.main()
