"""Local R2 session signing and CPU-only, bounded S3 access.

No credential discovery: signing accepts the operator's already authorized parent
credentials; factories consume only explicit ephemeral COPPERTOP_R2_* values.
See https://developers.cloudflare.com/r2/api/s3/temporary-credentials/.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import time
from threading import RLock
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit, parse_qs

from .s3 import S3Store

LIMITS = {'source': (128, 3 * 1024**3), 'checkpoint': (512, 3 * 1024**3),
          'report': (8, 65536), 'mechanism': (256, 24*1024**2), 'mechanism_full': (30000, 1024**3),
          'mechanism_archive': (256, 1024**3+32*1024**2)}
READ_ACTIONS = ('GetObject', 'HeadObject')
WRITE_ROLES = ('report', 'mechanism', 'mechanism_full', 'mechanism_archive')
MULTIPART_ACTIONS = ('CreateMultipartUpload', 'UploadPart', 'ListParts',
                     'CompleteMultipartUpload', 'AbortMultipartUpload')
ENV_NAMES = ('COPPERTOP_R2_ACCESS_KEY_ID', 'COPPERTOP_R2_SECRET_ACCESS_KEY',
             'COPPERTOP_R2_SESSION_TOKEN')


def _endpoint(value):
    url = urlsplit(value)
    if (url.scheme != 'https' or not re.fullmatch(r'[0-9a-f]{32}\.r2\.cloudflarestorage\.com', url.netloc)
            or url.path not in ('', '/') or url.query or url.fragment):
        raise ValueError('Exact account R2 HTTPS origin required')
    return url.netloc, url.netloc.split('.')[0]


def _path(value):
    if (not isinstance(value, str) or not value or len(value) > 1024
            or any(p in ('', '.', '..') for p in value.split('/'))
            or any(c in value for c in ('\\', '%', '?', '#'))):
        raise ValueError('Invalid scoped object path')
    return value


@dataclass(frozen=True)
class SessionScope:
    role: str
    bucket: str
    prefix: str
    object_paths: tuple[str, ...] = ()

    def __post_init__(self):
        if self.role not in LIMITS or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', self.bucket):
            raise ValueError('Explicit role and bucket required')
        _path(self.prefix)
        object.__setattr__(self, 'object_paths', tuple(self.object_paths))
        if self.role in WRITE_ROLES:
            pattern = (r'chords/mechanism/mechanism-[0-9a-f]{16}' if self.role != 'report' else
                r'chords/gpu-reload/(?:reload-[0-9a-f]{16}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})')
            if self.object_paths or not re.fullmatch(pattern, self.prefix):
                raise ValueError('Report requires one dedicated reload prefix')
        else:
            maximum = 17 if self.role == 'source' else 5
            if not 1 <= len(self.object_paths) <= maximum or len(set(self.object_paths)) != len(self.object_paths):
                raise ValueError('Bounded exact read objects required')
            for path in self.object_paths:
                _path(path)
                suffix = path.removeprefix(self.prefix + '/objects/')
                if (path != self.prefix + '/objects/' + suffix
                        or not re.fullmatch(r'[0-9a-f]{2}/[0-9a-f]{64}', suffix)
                        or suffix[:2] != suffix[3:5]):
                    raise ValueError('Exact content-addressed object path required')

    @property
    def actions(self):
        return READ_ACTIONS + (('PutObject',) if self.role in WRITE_ROLES else ()) + (MULTIPART_ACTIONS if self.role == 'mechanism_archive' else ())

    @property
    def paths(self):
        return {'objectPaths': list(self.object_paths),
                'prefixPaths': [self.prefix + '/objects/'] if self.role in WRITE_ROLES else []}

    def allows(self, key):
        _path(key)
        if self.role not in WRITE_ROLES:
            return key in self.object_paths
        suffix = key.removeprefix(self.prefix + '/objects/')
        return (key == self.prefix + '/objects/' + suffix
                and bool(re.fullmatch(r'[0-9a-f]{2}/[0-9a-f]{64}', suffix))
                and suffix[:2] == suffix[3:5])


def _encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def _json(value):
    return json.dumps(value, separators=(',', ':'), sort_keys=True).encode('utf8')


@dataclass(frozen=True, repr=False)
class TemporaryCredentials:
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str = field(repr=False)
    expires_at: int

    def __repr__(self):
        return f'TemporaryCredentials(<redacted>, expires_at={self.expires_at})'

    def environment(self):
        return dict(zip(ENV_NAMES, (self.access_key_id, self.secret_access_key, self.session_token)))


def mint(parent_access_id, parent_secret, *, endpoint, scope, now=None, ttl_seconds=7200):
    """No API calls. Caller must have action-time authorization before signing."""
    host, account = _endpoint(endpoint)
    if not isinstance(scope, SessionScope) or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 7200:
        raise ValueError('Bounded session scope and TTL required')
    if not all(isinstance(v, str) and v for v in (parent_access_id, parent_secret)):
        raise ValueError('Explicit parent credentials required')
    issued = int(time.time()) if now is None else now
    if type(issued) is not int or issued < 0:
        raise ValueError('Integer issue time required')
    claims = {'bucket': scope.bucket, 'actions': list(scope.actions), 'paths': scope.paths,
              'sub': account, 'iss': parent_access_id, 'aud': host, 'iat': issued,
              'exp': issued + ttl_seconds}
    signing = _encode(_json({'alg': 'HS256', 'typ': 'JWT'})) + '.' + _encode(_json(claims))
    jwt = signing + '.' + _encode(hmac.new(parent_secret.encode(), signing.encode(), hashlib.sha256).digest())
    return TemporaryCredentials(parent_access_id, hashlib.sha256(jwt.encode()).hexdigest(),
                                base64.b64encode(('jwt/' + jwt).encode()).decode(), claims['exp'])


def _session(environment, endpoint, scope, now):
    """Validate public JWT claims and derived key; parent signature is checked by R2."""
    host, account = _endpoint(endpoint)
    values = [environment.get(name, '') for name in ENV_NAMES]
    if any(not isinstance(v, str) or not v or len(v) > 16384 for v in values):
        raise ValueError('Explicit temporary CPU credentials required')
    try:
        decoded = base64.b64decode(values[2], validate=True).decode('ascii')
        if not decoded.startswith('jwt/'):
            raise ValueError()
        jwt = decoded[4:]
        header, payload, signature = jwt.split('.')
        load = lambda v: json.loads(base64.urlsafe_b64decode(v + '=' * (-len(v) % 4)))
        claims = load(payload)
        if load(header) != {'alg': 'HS256', 'typ': 'JWT'} or len(base64.urlsafe_b64decode(signature + '=' * (-len(signature) % 4))) != 32:
            raise ValueError()
        expected = {'bucket': scope.bucket, 'actions': list(scope.actions), 'paths': scope.paths,
                    'sub': account, 'iss': values[0], 'aud': host}
        if set(claims) != set(expected) | {'iat', 'exp'} or any(claims[k] != v for k, v in expected.items()):
            raise ValueError()
        if (type(claims['iat']) is not int or type(claims['exp']) is not int
                or not claims['iat'] <= now < claims['exp'] or not 1 <= claims['exp'] - claims['iat'] <= 7200
                or not hmac.compare_digest(hashlib.sha256(jwt.encode()).hexdigest(), values[1])):
            raise ValueError()
    except Exception:
        raise ValueError('Temporary session does not match approved CPU role') from None
    return values, claims['exp']


class _Body:
    def __init__(self, body, client, count):
        self.body, self.client, self.remaining = body, client, count

    def read(self, count):
        if type(count) is not int or not 0 <= count <= 8 * 1024**2:
            raise ValueError('Bounded stream reads required')
        self.client.live()
        data = self.body.read(count)
        if len(data) > count or len(data) > self.remaining:
            raise ValueError('Response exceeds declared object size')
        self.remaining -= len(data)
        return data

    def close(self):
        self.body.close()


class BoundedClient:
    """Deny unsupported operations before SDK dispatch, charging failed attempts."""
    def __init__(self, client, scope, endpoint, expires_at, clock=time.time):
        self.client, self.scope, self.endpoint = client, scope, endpoint
        self.expires_at, self.clock = expires_at, clock
        self.requests = self.read_bytes = self.write_bytes = 0
        self._budget_lock = RLock()
        client.meta.events.register('before-send.s3', self.destination_guard)

    def live(self):
        if self.clock() >= self.expires_at:
            raise ValueError('Temporary CPU session expired')

    def destination_guard(self, request, **unused):
        host, _ = _endpoint(self.endpoint)
        url = urlsplit(request.url)
        key = unquote(url.path).removeprefix('/' + self.scope.bucket + '/')
        multipart = False
        if self.scope.role == 'mechanism_archive' and url.query:
            pairs = parse_qs(url.query, keep_blank_values=True)
            if all(len(values)==1 for values in pairs.values()):
                fields = set(pairs)
                multipart = (request.method == 'POST' and pairs == {'uploads':['']}) or (
                    'uploadId' in fields and 0 < len(pairs['uploadId'][0]) <= 2048 and (
                    (request.method in ('POST','DELETE') and fields == {'uploadId'}) or
                    (request.method == 'PUT' and fields == {'uploadId','partNumber'} and
                        pairs['partNumber'][0].isdigit() and 1<=int(pairs['partNumber'][0])<=10000) or
                    (request.method == 'GET' and fields <= {'uploadId','part-number-marker','max-parts'} and
                        all(pairs[field][0].isdigit() and 0<=int(pairs[field][0])<=10000 for field in fields-{'uploadId'}))))
        if (url.scheme != 'https' or url.netloc != host or (url.query and not multipart) or url.fragment
                or url.path == '/' + self.scope.bucket + '/' or not self.scope.allows(key)
                or (not multipart and request.method not in (('GET', 'HEAD', 'PUT') if self.scope.role in WRITE_ROLES else ('GET', 'HEAD')))):
            raise ValueError('Unexpected R2 destination or operation')
        self.live()

    def _call(self, action, kwargs):
        self.live()
        if (action not in self.scope.actions or kwargs.get('Bucket') != self.scope.bucket
                or not self.scope.allows(kwargs.get('Key'))):
            raise ValueError('R2 operation outside approved role')
        with self._budget_lock:
            if self.requests >= LIMITS[self.scope.role][0]:
                raise ValueError('R2 request cap exhausted')
            self.requests += 1
        methods = {'GetObject':'get_object','HeadObject':'head_object','PutObject':'put_object',
            'CreateMultipartUpload':'create_multipart_upload','UploadPart':'upload_part','ListParts':'list_parts',
            'CompleteMultipartUpload':'complete_multipart_upload','AbortMultipartUpload':'abort_multipart_upload'}
        return getattr(self.client, methods[action])(**kwargs)

    def head_object(self, **kwargs):
        return self._call('HeadObject', kwargs)

    def get_object(self, **kwargs):
        response = self._call('GetObject', kwargs)
        size = response.get('ContentLength')
        with self._budget_lock:
            if type(size) is not int or size < 0 or self.read_bytes + size > LIMITS[self.scope.role][1]:
                response['Body'].close()
                raise ValueError('R2 response byte cap exhausted')
            self.read_bytes += size  # Reserve entire body, including interrupted reads.
        response['Body'] = _Body(response['Body'], self, size)
        return response

    def put_object(self, **kwargs):
        body = kwargs.get('Body')
        if type(body) is not bytes or kwargs.get('IfNoneMatch') != '*':
            raise ValueError('Only bounded immutable report bytes can be written')
        if self.scope.role=='mechanism_full' and len(body)>64*1024**2:
            raise ValueError('Full recording single PUT exceeds buffer bound')
        if self.scope.role=='mechanism_archive' and len(body)>65536:
            raise ValueError('Archive single PUT is reserved for a bounded completion receipt')
        self._reserve_write(len(body))
        if self.scope.role in ('mechanism','mechanism_full','mechanism_archive'):
            kwargs = dict(kwargs, StorageClass='STANDARD')
        return self._call('PutObject', kwargs)

    def _reserve_write(self, size):
        with self._budget_lock:
            if self.write_bytes+size > LIMITS[self.scope.role][1]:
                raise ValueError('R2 write byte cap exhausted')
            self.write_bytes += size

    def create_multipart_upload(self, **kwargs):
        return self._call('CreateMultipartUpload', dict(kwargs, StorageClass='STANDARD'))

    def upload_part(self, **kwargs):
        body, number = kwargs.get('Body'), kwargs.get('PartNumber')
        if type(body) is not bytes or not 0<len(body)<=16*1024**2 or type(number) is not int or not 1<=number<=10000:
            raise ValueError('Archive multipart part exceeds bound')
        self._reserve_write(len(body))
        return self._call('UploadPart', kwargs)

    def list_parts(self, **kwargs):
        return self._call('ListParts', kwargs)

    def complete_multipart_upload(self, **kwargs):
        parts = kwargs.get('MultipartUpload',{}).get('Parts',[])
        if not 1<=len(parts)<=66 or [p.get('PartNumber') for p in parts] != list(range(1,len(parts)+1)):
            raise ValueError('Archive completion part list outside bound')
        return self._call('CompleteMultipartUpload', kwargs)

    def abort_multipart_upload(self, **kwargs):
        return self._call('AbortMultipartUpload', kwargs)


@dataclass(frozen=True)
class R2StoreFactory:
    endpoint: str
    scopes: tuple[SessionScope, ...]

    def __post_init__(self):
        _endpoint(self.endpoint)
        object.__setattr__(self, 'scopes', tuple(self.scopes))
        if not self.scopes or len({s.role for s in self.scopes}) != len(self.scopes):
            raise ValueError('One public configuration per role required')

    def __call__(self, role):
        scope = next((s for s in self.scopes if s.role == role), None)
        if scope is None:
            raise ValueError('Unknown CPU store role')
        values, expiry = _session(os.environ, self.endpoint, scope, time.time())
        import boto3
        from botocore.config import Config
        client = boto3.client('s3', endpoint_url=self.endpoint, region_name='auto',
                              aws_access_key_id=values[0], aws_secret_access_key=values[1],
                              aws_session_token=values[2], config=Config(connect_timeout=30,
                              read_timeout=60, retries={'total_max_attempts': 1},
                              s3={'addressing_style': 'path'}))
        bounded = BoundedClient(client, scope, self.endpoint, expiry)
        return S3Store(bounded, scope.bucket, prefix=scope.prefix)
