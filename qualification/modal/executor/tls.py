"""Process-local official truststore bootstrap, before the first SDK import."""
from importlib import metadata
import ssl
import sys
_verified = False


def bootstrap():
    global _verified
    if _verified:
        context = ssl.create_default_context()
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise ValueError('Verified TLS was altered after bootstrap')
        return
    if 'modal' in sys.modules:
        raise ValueError('TLS bootstrap must precede the first Modal SDK import')
    pins = {'modal':'1.6.1', 'truststore':'0.10.4', 'aiohttp-socks':'0.12.0', 'python-socks':'3.1.1'}
    if any(metadata.version(name) != version for name,version in pins.items()):
        raise ValueError('Pinned official SDK/TLS/proxy dependencies required')
    import truststore
    original = ssl.get_default_verify_paths()
    truststore.inject_into_ssl()
    context = ssl.create_default_context()
    if (context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname
            or ssl.get_default_verify_paths() != original):
        raise ValueError('Verified certificate and hostname TLS required')
    _verified = True
