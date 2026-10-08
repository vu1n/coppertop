"""Explicit size-bounded paid retention; release requires publication or an operator decision."""
from decimal import Decimal


def validate_retention(value):
    if not isinstance(value, dict) or set(value) != {'max_bytes', 'accept_ongoing_storage_charges', 'cleanup_owner', 'release_condition'}:
        raise ValueError('Explicit staging-storage retention policy required')
    if type(value['max_bytes']) is not int or value['max_bytes'] != 2*1024**3:
        raise ValueError('Retention must cover the bounded 2GiB owned stage')
    if value['accept_ongoing_storage_charges'] is not True:
        raise ValueError('Ongoing staging-storage charges must be explicitly accepted')
    if not isinstance(value['cleanup_owner'], str) or not 1 <= len(value['cleanup_owner']) <= 128:
        raise ValueError('Named recovery/cleanup owner required')
    if value['release_condition'] != 'verified_publication_or_explicit_recovery_or_deletion':
        raise ValueError('Explicit recovery/deletion release condition required')
    return dict(value)


def cleanup_disposition(*, gpu_started, publication_verified, retention):
    if not gpu_started or publication_verified:
        return 'delete'
    validate_retention(retention)
    return 'retain_for_recovery'


def retention_report(resource, policy, *, rate_usd_per_gib_month, size_bytes=None, size_error=None):
    policy=validate_retention(policy)
    rate=Decimal(str(rate_usd_per_gib_month))
    if not rate.is_finite() or rate<0:
        raise ValueError('Finite storage rate required')
    if size_bytes is not None and (type(size_bytes) is not int or size_bytes<0):
        raise ValueError('Measured retained size must be nonnegative bytes')
    return {'resource':dict(resource),'size_bytes':size_bytes,'size_error_type':size_error,
        'max_authorized_bytes':policy['max_bytes'],
        'within_size_authorization':size_bytes<=policy['max_bytes'] if size_bytes is not None else None,
        'rate_usd_per_gib_month':str(rate),
        'estimated_monthly_usd':str(rate*Decimal(size_bytes)/Decimal(1024**3)) if size_bytes is not None else None,
        'maximum_authorized_monthly_usd':str(rate*Decimal(policy['max_bytes'])/Decimal(1024**3)),
        'ongoing_charges_accepted':True,'cleanup_owner':policy['cleanup_owner'],
        'release_condition':policy['release_condition'],'automatic_expiry':False}
