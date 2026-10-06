"""Runpod network-volume staging from an external CPU worker, never a Mac relay.

Injected S3 clients require separately approved authentication. No Pod allocation or
credential discovery occurs here; the account integration remains unverified.
"""
from dataclasses import dataclass
import json
import os
import math
import time
from pathlib import Path

from .chords import canonical
from .streams import CHUNK_BYTES, ObjectRef, checked, file_chunks, file_ref

# Exact supported endpoint table, https://docs.runpod.io/storage/s3-api (2026-10-06).
DATACENTERS = frozenset(('EU-CZ-1', 'EU-RO-1', 'EUR-IS-1', 'EUR-NO-1',
                        'US-CA-2', 'US-GA-2', 'US-IL-1', 'US-KS-2',
                        'US-MD-1', 'US-MO-1', 'US-MO-2', 'US-NC-1',
                        'US-NC-2', 'US-NE-1', 'US-WA-1'))
PART_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class RunpodVolumeConfig:
    volume_id: str
    datacenter: str
    endpoint: str
    mount_path: str = '/workspace'

    def __post_init__(self):
        if (not self.volume_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in self.volume_id)
                or self.datacenter not in DATACENTERS
                or self.endpoint != f'https://s3api-{self.datacenter.lower()}.runpod.io/'):
            raise ValueError('Explicit supported Runpod volume/datacenter/endpoint required')
        if self.mount_path not in ('/workspace', '/runpod-volume'):
            raise ValueError('Explicit Pod or Serverless network-volume mount required')


class RunpodStager:
    """One exclusive writer per manifest path; S3 ACL/conditional locks are unavailable.

    Caller supplies an S3 client bound to config.endpoint/datacenter with finite
    connect/read timeouts and retries disabled. Its parent secret stays outside
    compute; the client must live in an explicitly approved CPU transfer worker.
    """
    def __init__(self, config: RunpodVolumeConfig, client):
        meta = client.meta
        options = meta.config
        retries = options.retries or {}
        if (meta.endpoint_url.rstrip('/') != config.endpoint.rstrip('/')
                or meta.region_name != config.datacenter
                or not 0 < options.connect_timeout <= 30
                or not 0 < options.read_timeout <= 60
                or retries.get('total_max_attempts') != 1):
            raise ValueError('Runpod client must bind endpoint/region and bounded no-retry transport')
        self.config = config
        self.client = client

    @staticmethod
    def _deadline(max_seconds):
        if isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)) or not math.isfinite(max_seconds) or not 0 < max_seconds <= 3600:
            raise ValueError('Finite transfer deadline within 3600 seconds required')
        return time.monotonic() + max_seconds

    @staticmethod
    def _check(deadline):
        if time.monotonic() >= deadline:
            raise TimeoutError('Runpod CPU transfer deadline exceeded')

    def _read(self, key, deadline):
        self._check(deadline)
        response = self.client.get_object(Bucket=self.config.volume_id, Key=key)
        body = response['Body']
        try:
            while True:
                self._check(deadline)
                chunk = body.read(CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk
        finally:
            body.close()

    def _verify(self, key, ref, deadline):
        for _ in checked(self._read(key, deadline), ref):
            pass
        self._check(deadline)

    def _upload(self, key, ref, chunks, deadline):
        if ref.size > PART_BYTES * 10000:
            raise ValueError('Runpod multipart object limit exceeded')
        source = checked(chunks, ref)
        if ref.size <= PART_BYTES:
            data = b''.join(source)
            self._check(deadline)
            self.client.put_object(Bucket=self.config.volume_id, Key=key, Body=data)
        else:
            # Runpod requires multipart above 500 MB. Keep every part at 8 MiB.
            self._check(deadline)
            upload = self.client.create_multipart_upload(Bucket=self.config.volume_id, Key=key)['UploadId']
            try:
                parts = []
                buffer = bytearray()
                for chunk in source:
                    self._check(deadline)
                    buffer.extend(chunk)
                    if len(buffer) >= PART_BYTES:
                        parts.append(self._part(key, upload, len(parts) + 1, bytes(buffer), deadline))
                        buffer.clear()
                if buffer:
                    parts.append(self._part(key, upload, len(parts) + 1, bytes(buffer), deadline))
                self._check(deadline)
                self.client.complete_multipart_upload(Bucket=self.config.volume_id, Key=key, UploadId=upload,
                                                      MultipartUpload={'Parts': parts})
            except BaseException:
                # Abort only this worker's upload; never delete completed volume data.
                self.client.abort_multipart_upload(Bucket=self.config.volume_id, Key=key, UploadId=upload)
                raise
        self._verify(key, ref, deadline)

    def _part(self, key, upload, number, data, deadline):
        self._check(deadline)
        if number > 10000:
            raise ValueError('Runpod multipart part limit exceeded')
        result = self.client.upload_part(Bucket=self.config.volume_id, Key=key, UploadId=upload,
                                        PartNumber=number, Body=data)
        return {'PartNumber': number, 'ETag': result['ETag']}

    @staticmethod
    def _directory(manifest):
        manifest.validate()
        return f'coppertop/stages/{manifest.key}'

    def stage(self, source, manifest, directory: Path, *, max_seconds):
        """External CPU scratch -> volume. Shared ready marker uploads last."""
        from .cloud_stage import stage_objects
        deadline = self._deadline(max_seconds)
        directory = Path(directory)
        receipt = stage_objects(source, manifest, directory, max_seconds=max_seconds,
                                commit=lambda: self._publish_directory(manifest, directory, deadline))
        return self._stage_result(manifest, receipt)

    def _publish_directory(self, manifest, directory, deadline):
        remote = self._directory(manifest)
        for name, ref in sorted(manifest.objects.items()):
            self._upload(f'{remote}/{name}', ref, file_chunks(directory / name), deadline)
        marker = directory / 'stage.json'
        self._upload(f'{remote}/stage.json', file_ref(marker), file_chunks(marker), deadline)

    def _stage_result(self, manifest, receipt):
        return dict(receipt, provider='runpod', volume_id=self.config.volume_id,
                    datacenter=self.config.datacenter, manifest=manifest.key,
                    directory=self._directory(manifest), stage=receipt)

    def require_ready(self, manifest, receipt, *, pod_datacenter, max_seconds):
        """CPU gate before allocation: same datacenter and full marker/payload checks."""
        from .cloud_stage import stage_receipt
        directory = self._directory(manifest)
        expected = dict(stage_receipt(manifest), provider='runpod', volume_id=self.config.volume_id,
                        datacenter=self.config.datacenter, manifest=manifest.key,
                        directory=directory, stage=stage_receipt(manifest))
        if receipt != expected or pod_datacenter != self.config.datacenter:
            raise ValueError('Runpod readiness or datacenter mismatch')
        deadline = self._deadline(max_seconds)
        from .chords import digest
        data = canonical(receipt['stage'])
        self._verify(f'{directory}/stage.json', ObjectRef(digest(data), len(data)), deadline)
        for name, ref in sorted(manifest.objects.items()):
            self._verify(f'{directory}/{name}', ref, deadline)
        return f'{self.config.mount_path}/{directory}'


    def publish_output(self, r2, manifest, directory: Path, *, run_id: str, max_seconds: int):
        """External CPU only: checksum-fetch mounted GPU output, then publish to R2."""
        from .cloud_stage import DeadlineStore, stage_objects, stage_receipt
        from .gpu_smoke import RELEASE
        from .smoke_artifacts import publish_smoke_checkpoint
        _checkpoint_manifest(manifest, run_id)
        deadline = self._deadline(max_seconds)
        remote = f'coppertop/runs/{run_id}/train'
        marker = canonical(stage_receipt(manifest))
        from .chords import digest
        self._verify(f'{remote}/stage.json', ObjectRef(digest(marker), len(marker)), deadline)
        source = _VolumeSource(self, manifest, remote, deadline)
        stage_objects(source, manifest, Path(directory), max_seconds=max_seconds,
                      commit=lambda: self._check(deadline))
        self._check(deadline)
        # Share the phase's remaining time instead of resetting a fresh full cap.
        bounded = DeadlineStore(r2, max_seconds)
        bounded.deadline = deadline
        key = publish_smoke_checkpoint(bounded, Path(directory),
            json.loads((Path(directory)/'result.json').read_bytes()),
            json.loads((Path(directory)/'head-manifest.json').read_bytes()), run_id)
        return {'run_id': run_id, 'release': RELEASE, 'checkpoint_key': key, 'phase': 'publish'}

    def restage_checkpoint(self, r2, checkpoint_key: str, manifest, directory: Path, *,
                           run_id: str, max_seconds: int):
        """External CPU only: verify R2 receipt, then stage a separate reload path."""
        from .cloud_stage import DeadlineStore, commit_directory
        from .smoke_artifacts import fetch_smoke_checkpoint
        _checkpoint_manifest(manifest, run_id)
        directory = Path(directory)
        deadline = self._deadline(max_seconds)
        bounded = DeadlineStore(r2, max_seconds)
        bounded.deadline = deadline
        fetched = fetch_smoke_checkpoint(bounded, checkpoint_key, directory/'fetch', run_id)
        if fetched['artifacts'] != {name: ref.to_dict() for name, ref in manifest.objects.items()}:
            raise ValueError('Published R2 checkpoint differs from requested output manifest')
        self._check(deadline)
        # fetch_smoke_checkpoint already verified the bytes. Reuse CPU scratch;
        # fetching them through stage_objects again would repeat the R2 download.
        receipt = commit_directory(manifest, directory/'fetch',
            lambda: self._publish_directory(manifest, directory/'fetch', deadline))
        self._check(deadline)
        return self._stage_result(manifest, receipt)


    def publish_reload(self, r2, report_manifest, checkpoint_manifest, directory: Path, *,
                       container_id: str, training_container_id: str, max_seconds: int):
        """External CPU: publish the sealed fresh verifier report after verified stop."""
        from .cloud_stage import DeadlineStore, MAX_MANIFEST_BYTES, stage_objects, stage_receipt
        from .cloud_smoke import validate_reload_result
        from .streams import read_small
        from .chords import digest
        report_manifest.validate()
        _checkpoint_manifest(checkpoint_manifest, report_manifest.run_id)
        if (report_manifest.release != checkpoint_manifest.release
                or set(report_manifest.objects) != {'result.json'}
                or report_manifest.objects['result.json'].size > MAX_MANIFEST_BYTES
                or not isinstance(container_id, str) or not 0 < len(container_id) <= 128
                or not isinstance(training_container_id, str) or not 0 < len(training_container_id) <= 128
                or container_id == training_container_id):
            raise ValueError('Bounded distinct verifier identities/report required')
        deadline = self._deadline(max_seconds)
        remote = f'coppertop/runs/{report_manifest.run_id}/reload'
        marker = canonical(stage_receipt(report_manifest))
        self._verify(f'{remote}/stage.json', ObjectRef(digest(marker), len(marker)), deadline)
        stage_objects(_VolumeSource(self, report_manifest, remote, deadline), report_manifest,
                      Path(directory), max_seconds=max_seconds, commit=lambda: self._check(deadline))
        raw = (Path(directory)/'result.json').read_bytes()
        result = json.loads(raw)
        if (result.get('run_id'), result.get('container_id'), result.get('training_container_id')) != (
                report_manifest.run_id, container_id, training_container_id):
            raise ValueError('Sealed verifier report identities differ')
        validate_reload_result(result, checkpoint_manifest)
        bounded = DeadlineStore(r2, max_seconds)
        bounded.deadline = deadline
        key = bounded.put(raw)
        if key != report_manifest.objects['result.json'].sha256 or read_small(bounded, key, MAX_MANIFEST_BYTES) != raw:
            raise ValueError('Durable verifier report read-back differs')
        return {'run_id': report_manifest.run_id, 'release': report_manifest.release,
                'container_id': container_id, 'training_container_id': training_container_id,
                'report_key': key, 'phase': 'publish_reload'}


def _checkpoint_manifest(manifest, run_id):
    from .cloud_smoke import validate_checkpoint_manifest
    validate_checkpoint_manifest(manifest)
    if manifest.run_id != run_id:
        raise ValueError('Pinned checkpoint run identity differs')


class _VolumeSource:
    """Read-only named-volume view of checksum-addressed output references."""
    def __init__(self, stager, manifest, directory, deadline):
        self.stager = stager
        self.directory = directory
        self.deadline = deadline
        self.names = {ref.sha256: name for name, ref in manifest.objects.items()}

    def stat(self, key):
        self.stager._check(self.deadline)
        result = self.stager.client.head_object(Bucket=self.stager.config.volume_id,
            Key=f'{self.directory}/{self.names[key]}')
        self.stager._check(self.deadline)
        return ObjectRef(key, result['ContentLength'])

    def iter_bytes(self, key):
        return self.stager._read(f'{self.directory}/{self.names[key]}', self.deadline)


def seal_runpod_output(manifest, directory: Path):
    """GPU-side local fsync barrier; external S3 verification establishes handoff.

    This makes no claim about undocumented distributed-filesystem commit semantics.
    No S3 client or parent credentials are available to this operation.
    """
    from .cloud_smoke import output_manifest
    _checkpoint_manifest(manifest, manifest.run_id)
    directory = Path(directory)
    if output_manifest(manifest, directory).to_dict() != manifest.to_dict():
        raise ValueError('Native checkpoint differs from sealed manifest')
    return seal_runpod_stage(manifest, directory)


def seal_runpod_stage(manifest, directory: Path):
    """Local filesystem seal for checkpoint/report stages; no identity inference."""
    from .cloud_stage import commit_directory
    directory = Path(directory)

    def flush():
        for name in (*manifest.objects, 'stage.json'):
            with (directory/name).open('rb') as source:
                os.fsync(source.fileno())
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    return commit_directory(manifest, directory, flush)
