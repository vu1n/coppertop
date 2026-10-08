"""Explicit finite retention contract; a provider adapter must arm a durable backstop."""
from decimal import Decimal


def validate_retention(value):
    if not isinstance(value, dict) or set(value) != {'seconds', 'max_bytes', 'max_storage_usd', 'cleanup_owner', 'expiry_action'}:
        raise ValueError('Explicit bounded archive retention policy required')
    if type(value['seconds']) is not int or not 60 <= value['seconds'] <= 86400:
        raise ValueError('Retention duration must be 60..86400 seconds')
    if type(value['max_bytes']) is not int or value['max_bytes'] != 2*1024**3:
        raise ValueError('Retention must cover the bounded 2GiB owned stage')
    cost = Decimal(str(value['max_storage_usd']))
    if not cost.is_finite() or not Decimal('0') < cost <= Decimal('1'):
        raise ValueError('Explicit finite retention storage ceiling required')
    if not isinstance(value['cleanup_owner'], str) or not 1 <= len(value['cleanup_owner']) <= 128:
        raise ValueError('Named deadline cleanup owner required')
    if value['expiry_action'] != 'delete_owned_stage_even_if_unpublished':
        raise ValueError('Explicit disposition of unpublished data at expiry required')
    return dict(value)


def cleanup_disposition(*, gpu_started, publication_verified, retention, backstop):
    if not gpu_started or publication_verified:
        return 'delete'
    validate_retention(retention)
    if not isinstance(backstop, dict) or backstop.get('armed') is not True:
        raise ValueError('Durable retention deadline backstop must be armed')
    return 'retain_until_deadline'
