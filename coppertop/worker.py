"""Storage/bootstrap acceptance check, not a trainer or model loader."""
import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from .chords import LocalStore, ObjectStore, object_path
from .checkpoint import checkpoint_reference
from .publication import release_objects


@dataclass(frozen=True)
class Job:
    release: str
    checkpoint: str | None = None
    run_id: str | None = None

    def __post_init__(self):
        object_path(self.release)
        if self.checkpoint:
            object_path(self.checkpoint)
            if not self.run_id:
                raise ValueError('Checkpoint requires a run identity')


def validate_job(store: ObjectStore, job: Job) -> dict:
    """Cloud callers inject R2 directly; no local replica is required."""
    result = {'release': job.release, 'objects': len(release_objects(store, job.release))}
    if job.checkpoint:
        checkpoint, payload = checkpoint_reference(store, job.checkpoint, job.release, job.run_id)
        result.update(step=checkpoint['step'], checkpoint_bytes=payload.size,
                      payload_ref=payload.to_dict())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--release', required=True)
    parser.add_argument('--checkpoint')
    parser.add_argument('--run-id')
    parser.add_argument('--require-cuda', action='store_true')
    args = parser.parse_args()
    if args.checkpoint and not args.run_id:
        parser.error('--checkpoint requires --run-id')
    store = LocalStore(args.store)
    result = validate_job(store, Job(args.release, args.checkpoint, args.run_id))
    if args.require_cuda:
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA is unavailable')
        # A tiny allocation checks runtime/device access without loading any model.
        result['cuda_device'] = torch.cuda.get_device_name()
        result['cuda_probe'] = int(torch.ones(1, device='cuda').cpu().item())
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
