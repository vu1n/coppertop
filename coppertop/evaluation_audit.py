"""Bounded, offline recovery authentication and factual DEV evidence inventory.

This module reads repository source and metadata only. Declarations, historical
reports and hash identities do not establish execution or archive payload closure.
"""
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

from .chords import canonical

ROOT = Path(__file__).resolve().parents[1]
RECOVERY_COMMIT = 'c381330c16dd43d75226af845555c795558cd78b'
PROVENANCE_PATH = 'manifests/clef-recovery/source-provenance.json'
PROVENANCE_SHA256 = '372adbebebfdfbca7cef293963c279dbf5edd702dc707d29bd3d602aad8b0364'
MAX_FILE_BYTES = 256 * 1024
MAX_INVENTORY_FILES = 256
MAX_INVENTORY_BYTES = 8 * 1024 * 1024
MAX_SEED_REFERENCES = 2048
SHA256 = re.compile(r'[0-9a-f]{64}')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _path(value):
    if (not isinstance(value, str) or not value or '\\' in value
            or PurePosixPath(value).is_absolute()
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise ValueError('Expected a bounded repository-relative path')
    return value


def _read(root, name):
    name = _path(name)
    path = root / name
    # No source inventory entry may redirect reads outside the requested repo,
    # including through a symlinked parent directory.
    if any(part.is_symlink() for part in (path, *path.parents)
           if part.is_relative_to(root)):
        raise ValueError(f'Symlink in audit source: {name}')
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f'Expected regular audit source: {name}')
    with path.open('rb') as source:
        data = source.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f'Audit source exceeds byte bound: {name}')
    return data


def _git_bytes(root, name):
    result = subprocess.run(
        ['git', '--no-replace-objects', '--no-pager', 'show',
         f'{RECOVERY_COMMIT}:{_path(name)}'], cwd=root, check=False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    if result.returncode or len(result.stdout) > MAX_FILE_BYTES:
        raise ValueError(f'Cannot authenticate original recovery source: {name}')
    return result.stdout


def _json(data, name):
    try:
        value = json.loads(data)
    except (ValueError, TypeError, UnicodeError) as error:
        raise ValueError(f'Invalid audit JSON: {name}') from error
    if not isinstance(value, dict):
        raise ValueError(f'Expected audit JSON object: {name}')
    return value


def _recovery(root):
    original = _git_bytes(root, PROVENANCE_PATH)
    if _sha(original) != PROVENANCE_SHA256:
        raise ValueError('Original recovery provenance differs from exact pin')
    if _read(root, PROVENANCE_PATH) != original:
        raise ValueError('Immutable recovery provenance was changed or removed')
    provenance = _json(original, PROVENANCE_PATH)
    if (len(provenance.get('copied_files', [])) != 16
            or len(provenance.get('packaging_files', [])) != 7):
        raise ValueError('Expected all 23 original recovery entries')
    originals, rows = {}, []
    for kind in ('copied_files', 'packaging_files'):
        for entry in provenance[kind]:
            name = _path(entry['path'])
            if (name in originals or not SHA256.fullmatch(entry['sha256'])
                    or type(entry['bytes']) is not int
                    or not 0 <= entry['bytes'] <= MAX_FILE_BYTES):
                raise ValueError('Invalid original recovery entry')
            data = _git_bytes(root, name)
            if len(data) != entry['bytes'] or _sha(data) != entry['sha256']:
                raise ValueError(f'Original recovery authentication failed: {name}')
            originals[name] = data
            current = _read(root, name)
            if name.startswith('manifests/clef-recovery/') and current != data:
                raise ValueError(f'Immutable recovery metadata differs: {name}')
            rows.append({'path': name, 'kind': kind, 'original_bytes': len(data),
                         'original_sha256': _sha(data), 'original_verified': True,
                         'current_bytes': len(current) if current is not None else None,
                         'current_sha256': _sha(current) if current is not None else None,
                         'current_status': 'missing' if current is None else
                         'unchanged' if current == data else 'derivative'})
    return provenance, originals, rows


def _current_identity(root):
    """Authenticate immutable archive identities without historical git objects.

    This proves only metadata bytes. The full audit still authenticates all 23
    original files through the exact recovery commit independently.
    """
    raw = _read(root, PROVENANCE_PATH)
    if raw is None or _sha(raw) != PROVENANCE_SHA256:
        raise ValueError('Pinned current recovery provenance differs or is missing')
    provenance = _json(raw, PROVENANCE_PATH)
    name = 'manifests/clef-recovery/dev-archive-identities.json'
    entries = [entry for entry in provenance['packaging_files'] if entry['path'] == name]
    if len(entries) != 1:
        raise ValueError('Expected pinned DEV archive identity entry')
    entry = entries[0]
    identity = _read(root, name)
    if (identity is None or len(identity) != entry['bytes']
            or _sha(identity) != entry['sha256']):
        raise ValueError('Pinned current DEV archive identity differs or is missing')
    return {name: identity}


def _inventory(root):
    """No tests, generated histories, credential files or model assets are read."""
    inventory, total = {}, 0
    for directory, suffixes in (('coppertop', {'.py'}), ('tools', {'.py'}),
                                ('docs', {'.md', '.json'}),
                                ('manifests/clef-recovery', {'.md', '.json'})):
        base = root / directory
        if base.is_symlink():
            raise ValueError(f'Symlink in audit inventory: {directory}')
        if not base.is_dir():
            raise ValueError(f'Missing audit inventory directory: {directory}')
        for path in sorted(base.rglob('*')):
            if path.is_symlink():
                raise ValueError(f'Symlink in audit inventory: {path.relative_to(root)}')
            if path.suffix not in suffixes:
                continue
            name = path.relative_to(root).as_posix()
            data = _read(root, name)
            if data is None:
                raise ValueError(f'Audit source disappeared: {name}')
            inventory[name] = data
            total += len(data)
            if len(inventory) > MAX_INVENTORY_FILES or total > MAX_INVENTORY_BYTES:
                raise ValueError('Audit inventory exceeds bounded source scope')
    return inventory


def _literal(data, name, constant):
    tree = ast.parse(data, filename=name)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == constant
                for target in node.targets):
            return ast.literal_eval(node.value), node.lineno
    raise ValueError(f'Missing literal {constant} in {name}')


def _seeds(values):
    if not isinstance(values, (list, tuple, set)) or any(
            type(value) is not int or value < 0 for value in values):
        raise ValueError('Expected declared nonnegative integer seeds')
    return sorted(set(values))


def _seed_ledger(originals, inventory):
    archive_path = 'manifests/clef-recovery/dev-archive-identities.json'
    archives = _json(originals[archive_path], archive_path)
    used = _seeds(archives['development_seeds'])
    reserved = _seeds(archives['additional_holdout_exclusion'])
    facts = [{'category': 'used', 'seeds': used, 'path': archive_path,
              'json_pointer': '/development_seeds', 'status': 'declared_prior_dev'},
             {'category': 'reserved', 'seeds': reserved, 'path': archive_path,
              'json_pointer': '/additional_holdout_exclusion', 'status': 'declared_exclusion'}]
    name = 'coppertop/performance_eval.py'
    prior, line = _literal(inventory[name], name, 'DEV_SEEDS')
    prior = _seeds(prior)
    used = sorted(set(used) | set(prior))
    facts.append({'category': 'used', 'seeds': prior, 'path': name,
                  'line': line, 'status': 'declared_prior_dev'})
    name = 'coppertop/episode.py'
    explored, line = _literal(inventory[name], name, 'EXPLORED_DEV_SEEDS')
    explored = _seeds(explored)
    facts.append({'category': 'explored', 'seeds': explored, 'path': name,
                  'line': line, 'status': 'declared_explored_dev_not_task_execution'})
    reported = []
    for index, archive in enumerate(archives['archives']):
        seed = _seeds([archive['seed']])[0]
        reported.append(seed)
        facts.append({'category': 'reported_execution', 'seeds': [seed],
                      'path': archive_path, 'json_pointer': f'/archives/{index}',
                      'attempt': archive['attempt'], 'cadence': archive['cadence'],
                      'status': 'historical_report_without_payload_verification_here'})
    references = []
    for name, data in inventory.items():
        for line, text in enumerate(data.decode('utf-8').splitlines(), 1):
            if re.search(r'\b(?:seeds?|DEV\d+)\b|DEV_SEEDS', text, re.IGNORECASE):
                references.append({'path': name, 'line': line,
                                   'text': text.strip()[:240], 'status': 'reference_only'})
                if len(references) > MAX_SEED_REFERENCES:
                    raise ValueError('Seed references exceed bounded source scope')
    exclusions = sorted(set(used) | set(reserved) | set(explored) | set(reported))
    return {'kind': 'bounded_available_dev_seed_ledger', 'used': used,
            'reserved': reserved, 'explored': explored, 'exclusions': exclusions,
            'reported_execution_seeds': sorted(set(reported)),
            'proven_execution_seeds': [], 'facts': facts, 'references': references,
            'accessible_inventory_complete': True, 'full_history_complete': False,
            'actual_candidate_unused': False,
            'candidate_checks': [{'seed': seed, 'seen_in_declared_exclusions': seed in exclusions,
                                  'actual_candidate_unused': False, 'status': 'unconfirmed'}
                                 for seed in (1152, 1153)],
            'excluded_evidence': ['tests', 'generated CPU fixtures', 'proposed runs',
                                  'policy RNG seeds', 'constructor/base seeds'],
            'limits': ['Mac histories and other missing execution histories are unavailable',
                       'Declarations do not prove task-specific execution',
                       '1152 and 1153 require reconciliation before selection'],
            'evidence_hashes': {name: _sha(data) for name, data in inventory.items()}}


def _receipt_audit(originals, inventory):
    name = 'manifests/clef-recovery/dev-archive-identities.json'
    identity = _json(originals[name], name)
    # Search actual complete metadata bytes by digest, never hash strings inside
    # an identity manifest. Large archives are deliberately outside this scope.
    by_hash = {}
    for path, data in inventory.items():
        if path.endswith('.json'):
            by_hash.setdefault(_sha(data), []).append(path)
    archives = []
    for entry in identity['archives']:
        receipts = by_hash.get(entry['completion_receipt_sha256'], [])
        manifests = by_hash.get(entry['manifest_sha256'], [])
        archives.append({**entry, 'completion_receipt_paths': receipts,
                         'completion_receipt_bytes_verified_here': bool(receipts),
                         'manifest_paths': manifests,
                         'manifest_bytes_verified_here': bool(manifests),
                         'archive_bytes_available_in_scope': False,
                         'archive_sha256_verified_here': False,
                         'release_closure_verified_here': False,
                         'remote_readback_verified_here': False,
                         'status': 'authenticated_identity_metadata_only'})
    return {'kind': 'bounded_archive_receipt_audit',
            'identity_manifest_sha256': _sha(originals[name]),
            'identity_metadata_authenticated': True, 'archives': archives,
            'separate_failed_attempt': identity['separate_failed_attempt'],
            'raw_receipts_available_in_scope': all(
                row['completion_receipt_bytes_verified_here'] for row in archives),
            'archive_payloads_read': False, 'complete_archive_closure_verified': False,
            'remote_calls': False,
            'limits': ['Repository code/docs/recovery metadata only; no external or generated payloads read',
                       'Recorded Mac archive rehashes are historical claims, not fresh readback here',
                       'Missing raw receipts, input/query bytes and archive closures require later reconciliation'],
            'evidence_hashes': {path: _sha(data) for path, data in inventory.items()
                                if path.endswith('.json') or path.startswith('docs/')}}


def seed_ledger(root=ROOT):
    """Return declared DEV categories and bounded references, never unseen-seed proof."""
    root = Path(root).resolve()
    return _seed_ledger(_current_identity(root), _inventory(root))


def receipt_audit(root=ROOT):
    """Authenticate identities and identify actual receipt bytes within source scope."""
    root = Path(root).resolve()
    return _receipt_audit(_current_identity(root), _inventory(root))


def audit(root=ROOT):
    """Authenticate 23 exact-commit originals; report derivatives and evidence limits."""
    root = Path(root).resolve()
    provenance, originals, rows = _recovery(root)
    inventory = _inventory(root)
    questions, _ = _literal(originals['coppertop/decision_gate.py'],
                            'coppertop/decision_gate.py', 'QUESTIONS')
    schema = _sha(canonical(questions))
    if schema != provenance['schema_sha256']:
        raise ValueError('Original recovery decision schema differs from provenance')
    current_questions, _ = _literal(inventory['coppertop/decision_gate.py'],
                                    'coppertop/decision_gate.py', 'QUESTIONS')
    model_path = 'manifests/clef-recovery/model-release.json'
    model = _json(originals[model_path], model_path)
    model_files = model['files']
    for item in model_files:
        _path(item['path'])
        if type(item['bytes']) is not int or item['bytes'] < 0:
            raise ValueError('Invalid pinned model file byte metadata')
        lfs = item['lfs']
        if lfs is not None and (not SHA256.fullmatch(lfs['sha256'])
                                or lfs['size'] != item['bytes']):
            raise ValueError('Invalid pinned model LFS metadata')
    reference = model['reference_implementation']
    if not SHA256.fullmatch(reference['sha256']) or not any(
            item['path'] == reference['path'] for item in model_files):
        raise ValueError('Missing pinned reference-module metadata')
    return {'schema_version': 1, 'kind': 'offline_evaluation_evidence_audit',
            'recovery_commit': RECOVERY_COMMIT, 'recovery_provenance_sha256': PROVENANCE_SHA256,
            'original_entry_count': len(rows), 'original_entries_verified': True,
            'immutable_recovery_metadata_verified': True, 'entries': rows,
            'decision_schema': {'original_sha256': schema, 'original_verified': True,
                                'current_sha256': _sha(canonical(current_questions)),
                                'current_matches_original': _sha(canonical(current_questions)) == schema,
                                'runtime_parity_verified': False},
            'model_metadata': {'manifest_sha256': _sha(originals[model_path]),
                               'repo': model['repo'], 'revision': model['revision'],
                               'license': model['license'], 'files': model_files,
                               'file_count': len(model_files),
                               'total_declared_bytes': sum(item['bytes'] for item in model_files),
                               'reference_implementation': reference,
                               'config': _json(originals['manifests/clef-recovery/config.json'], 'config'),
                               'joint_head_config': _json(originals['manifests/clef-recovery/joint_head_config.json'], 'joint config'),
                               'weights_read': False, 'reference_module_read': False,
                               'runtime_verified': False,
                               'historical_cache_image': model['local_cache_image'],
                               'cache_image_rechecked': False},
            'seed_ledger': _seed_ledger(originals, inventory),
            'receipt_audit': _receipt_audit(originals, inventory),
            'inventory': {'files': len(inventory), 'bytes': sum(map(len, inventory.values())),
                          'max_files': MAX_INVENTORY_FILES, 'max_file_bytes': MAX_FILE_BYTES,
                          'max_total_bytes': MAX_INVENTORY_BYTES, 'accessible_scope_complete': True,
                          'full_history_complete': False},
            'evidence_hashes': {PROVENANCE_PATH: PROVENANCE_SHA256,
                                **{name: _sha(data) for name, data in inventory.items()}},
            'paid_authorized': False, 'provider_calls': False, 'cloud_writes': False}
