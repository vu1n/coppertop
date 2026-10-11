"""Prepare causal cases and preview one scorer call; execution is explicitly opt-in."""
import argparse
import json
import os
from pathlib import Path
import uuid

from .chords import LocalStore, canonical
from .decision_provider import DecisionProvider, ProviderError
from .decision_recording import prepare_case, save_judgment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument('--source-store', required=True, type=Path)
    prepare.add_argument('--release', required=True)
    prepare.add_argument('--episode-record', required=True)
    prepare.add_argument('--frame', required=True, type=int)
    prepare.add_argument('--output-store', required=True, type=Path)
    score = commands.add_parser('score')
    score.add_argument('--store', required=True, type=Path)
    score.add_argument('--case', required=True)
    score.add_argument('--provider', choices=('cloudflare', 'openrouter'), default='cloudflare')
    score.add_argument('--model')
    score.add_argument('--execute', action='store_true', help='Perform exactly one paid API call; requires separately approved spend/data transfer')
    args = parser.parse_args()
    if args.command == 'prepare':
        key = prepare_case(LocalStore(args.source_store), args.release, args.episode_record,
                           args.frame, LocalStore(args.output_store))
        print(json.dumps({'case_id': key, 'provider_calls': 0}))
        return
    store = LocalStore(args.store)
    # Dry preparation does not read credentials or need account setup.
    if not args.execute:
        from .decision_recording import provider_body
        from .chords import digest
        model = args.model or ('clef-flash' if args.provider == 'cloudflare' else 'typesafe/jev-1.13')
        body = provider_body(store, args.case, model, vision=args.provider == 'cloudflare')
        encoded = canonical(body)
        print(json.dumps({'status': 'prepared', 'provider_calls': 0, 'case_id': args.case,
                          'request_sha256': digest(encoded), 'request_bytes': len(encoded),
                          'provider': args.provider, 'model': model}))
        return
    token = (os.environ.get('CLOUDFLARE_API_TOKEN') or os.environ.get('CLOUDFLARE_AUTH_TOKEN', '')) if args.provider == 'cloudflare' else os.environ.get('OPENROUTER_API_KEY', '')
    provider = DecisionProvider(args.provider, token,
                                os.environ.get('CLOUDFLARE_ACCOUNT_ID'), model=args.model)
    # Reserve the immutable request before dispatch: a failed call may still be billable.
    attempt_id = uuid.uuid4().hex
    request = dict(provider.score(store, args.case), attempt_id=attempt_id)
    request_id = store.put(canonical(request))
    try:
        receipt = dict(provider.score(store, args.case, execute=True), attempt_id=attempt_id)
        overlay = save_judgment(store, args.case, receipt)
        print(json.dumps({'status': 'scored', 'request_id': request_id, 'overlay': overlay, 'provider_calls': 1}))
    except Exception as error:
        failure = store.put(canonical({'status': 'failed', 'request_id': request_id, 'attempt_id': attempt_id,
            'error_type': type(error).__name__, 'provider_calls_attempted': 1,
            'billable_status': 'unknown', 'automatic_retries': 0}))
        raise ProviderError('Scorer call failed; receipt ' + failure) from None


if __name__ == '__main__':
    main()
