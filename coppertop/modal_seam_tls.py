"""Explicit application bootstrap for the local Modal seam processes only."""
from importlib import metadata
import ssl
import sys

PINNED = {'modal': '1.6.1', 'truststore': '0.10.4',
          'aiohttp-socks': '0.12.0', 'python-socks': '3.1.1'}
_BOOTSTRAPPED = False


def bootstrap():
    # No injection on module import: this is called only by the owned worker
    # application or its independent cleanup/observer subprocess applications.
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    if 'modal' in sys.modules:
        raise ValueError('TLS bootstrap must precede the Modal SDK import')
    if any(metadata.version(name) != version for name, version in PINNED.items()):
        raise ValueError('Pinned Modal proxy and trust-store dependencies required')
    import truststore
    original_paths = ssl.get_default_verify_paths()
    truststore.inject_into_ssl()
    context = ssl.create_default_context()
    if (context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname
        or ssl.get_default_verify_paths() != original_paths):
        raise ValueError('Verified platform trust-store configuration required')
    _BOOTSTRAPPED = True
