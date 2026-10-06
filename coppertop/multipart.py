"""Serializable multipart receipts, persisted by the caller outside disposable compute."""
from dataclasses import dataclass, field
from .streams import ObjectRef

MIN_PART_BYTES = 5 * 1024 * 1024
MAX_PART_BYTES = 64 * 1024 * 1024  # Bounded worker memory, below R2's per-part maximum.
MAX_PARTS = 10_000


@dataclass
class UploadState:
    destination: str
    upload_id: str
    ref: ObjectRef
    part_size: int = MIN_PART_BYTES
    parts: list[dict] = field(default_factory=list)

    def to_dict(self):
        return {'schema_version': 1, 'destination': self.destination, 'upload_id': self.upload_id,
                'ref': self.ref.to_dict(), 'part_size': self.part_size,
                'parts': [dict(part) for part in self.parts]}

    @classmethod
    def from_dict(cls, value):
        if value['schema_version'] != 1:
            raise ValueError('Unsupported upload state')
        state = cls(value['destination'], value['upload_id'], ObjectRef(**value['ref']),
                    value['part_size'], value['parts'])
        state.validate()
        return state

    def validate(self):
        if type(self.part_size) is not int or not MIN_PART_BYTES <= self.part_size <= MAX_PART_BYTES:
            raise ValueError('Invalid multipart part size')
        if not self.upload_id or len(self.parts) > MAX_PARTS or self.ref.size > self.part_size * MAX_PARTS:
            raise ValueError('Invalid multipart state size')
        for number, part in enumerate(self.parts, 1):
            ObjectRef(part['sha256'], part['size'])
            expected_size = min(self.part_size, self.ref.size - (number - 1) * self.part_size)
            if part['number'] != number or part['size'] != expected_size or not part['etag']:
                raise ValueError('Invalid multipart receipt')


class UploadInterrupted(RuntimeError):
    def __init__(self, state: UploadState):
        super().__init__('Multipart transfer interrupted; persist state and retry with the same immutable source')
        self.state = state
