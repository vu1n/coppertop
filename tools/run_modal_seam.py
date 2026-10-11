"""Defaults to offline preparation; provider execution requires exact approval."""
import argparse
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from coppertop import modal_seam as seam


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'execute', 'cleanup', 'internal-worker'], nargs='?', default='prepare')
    parser.add_argument('--suite', default=None)
    parser.add_argument('--workspace')
    parser.add_argument('--image-id')
    parser.add_argument('--prebuilt-base', action='store_true', help='Resolve only the fixed global Python 3.12 base inside the one owned App')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--approval', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT/'.local/modal-seam-preparation')
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        suite = args.suite or 'ct-modal-'+uuid.uuid4().hex[:20]
        plan = seam.prepare(ROOT, suite, args.workspace, args.image_id, prebuilt_base=args.prebuilt_base)
        args.output.mkdir(parents=True, exist_ok=True)
        path = args.output/'plan.json'
        with path.open('xb') as handle:
            handle.write(seam.canonical(plan))
        print(json.dumps({'plan': str(path), 'planSha256': seam.sha(seam.canonical(plan)),
                          'workspace': plan['workspace'], 'imageId': plan['imageId'],
                          'imageRecipe': plan['imageRecipe'],
                          'launchReady': False, 'cloudActionsPerformed': False}))
        return 0
    if args.plan is None:
        parser.error('--plan is required')
    plan = json.loads(args.plan.read_bytes())
    if args.command == 'cleanup':
        result = seam.cleanup_receipt(ROOT, plan, args.output)
        print(json.dumps(result))
        return 0
    if args.approval is None:
        parser.error('--approval is required')
    approval = json.loads(args.approval.read_bytes())
    if args.command == 'internal-worker':
        return seam.worker(ROOT, plan, approval, args.output)
    result = seam.supervise(ROOT, plan, approval, args.output)
    print(json.dumps(result))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps(seam.diagnostic(error, 'command')), file=sys.stderr)
        raise SystemExit(1)
