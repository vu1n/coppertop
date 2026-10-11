"""Reproduce bounded local CPU evidence; no provider or execution option."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coppertop.chords import canonical, digest
from coppertop.evaluation_audit import audit
from coppertop.evaluation_episode import run_fixture_pilot
from coppertop.evaluation_readiness import prepare
from coppertop.evaluation_roundtrip import roundtrip


def verify(directory):
    prepared = prepare()
    audited = audit()
    pilot = run_fixture_pilot()
    output = Path(directory)
    output.mkdir(parents=True, exist_ok=False)
    values = {'manifest.json': prepared, 'source-audit.json': audited,
              'pilot-fixture.json': {'accounting': pilot['accounting'],
                  'provenance': pilot['provenance'],
                  'episodes': [dict(accounting=row['accounting'], provenance=row['provenance'])
                               for row in pilot['episodes']]}}
    for name, value in values.items():
        (output/name).write_bytes(canonical(value))
    accounting = pilot['accounting']
    caps = prepared['manifest']['pilot_proposal']['four_episode_caps']
    if (accounting['status'] != 'fixture_passed'
            or accounting['controls'] != caps['controls']
            or any(accounting['totals'][key] > caps[key+'_calls']
                   for key in ('actor', 'scorer', 'input'))):
        raise ValueError('CPU fixture did not satisfy the proposed control/query caps')
    restored = roundtrip(output/'artifact-roundtrip', pilot)
    result = {'kind': 'offline_clef_readiness_verification', 'status': 'offline_fixture_passed',
        'manifest_sha256': prepared['manifest_sha256'],
        'evidence_sha256': {name: digest((output/name).read_bytes()) for name in values},
        'original_recovery_entries_verified': audited['original_entry_count'],
        'available_seed_ledger_complete': audited['inventory']['accessible_scope_complete'],
        'full_seed_history_complete': audited['seed_ledger']['full_history_complete'],
        'candidate_seeds_confirmed_unused': audited['seed_ledger']['actual_candidate_unused'],
        'fixture_accounting': accounting, 'local_artifact_roundtrip': restored,
        'selected_design': None, 'launch_ready': False, 'paid_authorized': False,
        'real_model_execution': False, 'real_simulator_execution': False,
        'provider_calls': False, 'cloud_writes': False,
        'remaining_runtime_quote_inputs': prepared['manifest']['pilot_proposal']['quote']['missing_inputs']}
    (output/'verification.json').write_bytes(canonical(result))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, help='new local evidence directory')
    args = parser.parse_args()
    print(json.dumps(verify(args.directory), indent=2, allow_nan=False))
