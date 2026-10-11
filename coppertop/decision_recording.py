"""Causal scorer inputs from immutable native tapes, stored as separate overlays."""
import base64
import io
import json
import math
import struct

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .chords import canonical, digest, frame_id
from .decision_gate import QUESTIONS, SCHEMA_SHA256, validate_answers
from .official_recording import ACTION_LAYOUT, STATE_LAYOUT
from .streams import read_small

CAMERAS = ('video.robot0_agentview_left', 'video.robot0_agentview_right', 'video.robot0_eye_in_hand')


def png_rgb(image):
    # Standard-library PNG keeps exact RGB pixels without a new image dependency.
    import zlib
    if image.dtype != np.uint8 or image.shape != (256, 256, 3):
        raise ValueError('Pinned raw camera geometry required')
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    scanlines = b''.join(b'\0' + row.tobytes() for row in image)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 256, 256, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(scanlines)) + chunk(b'IEND', b''))


def array(store, ref):
    raw = read_small(store, ref['object'], limit=256 * 1024)
    source = io.BytesIO(raw)
    version = np.lib.format.read_magic(source)
    if version == (1, 0):
        shape, _fortran, dtype = np.lib.format.read_array_header_1_0(source)
    elif version == (2, 0):
        shape, _fortran, dtype = np.lib.format.read_array_header_2_0(source)
    else:
        raise ValueError('Supported native NPY version required')
    if (dtype.hasobject or dtype.kind not in 'fiu' or len(shape) > 4
            or any(type(size) is not int or size < 0 for size in shape)
            or math.prod(shape) * dtype.itemsize > 256 * 1024
            or str(dtype) != ref['dtype'] or list(shape) != ref['shape']
            or len(raw) - source.tell() != math.prod(shape) * dtype.itemsize):
        raise ValueError('Bounded primitive native array required')
    value = np.load(io.BytesIO(raw), allow_pickle=False)
    if (value.dtype.hasobject or str(value.dtype) != ref['dtype']
            or list(value.shape) != ref['shape'] or not np.isfinite(value).all()):
        raise ValueError('Native array descriptor differs')
    return value


def prepare_case(source, release_id, episode_record, at_frame, output):
    """No terminal outcomes, future observations, or post-boundary executed actions enter state."""
    manifest = json.loads(read_small(source, release_id))
    if episode_record not in manifest['episodes']:
        raise ValueError('Episode must belong to the bound release')
    record = json.loads(read_small(source, episode_record))
    if (record['kind'] != 'generated_official_rollout' or record['state_layout'] != STATE_LAYOUT
            or record['action_layout'] != ACTION_LAYOUT or record['provenance']['split'] != 'development'):
        raise ValueError('Native development recording required')
    parquet = pq.ParquetFile(io.BytesIO(read_small(source, record['signals'])),
                            thrift_string_size_limit=65536, thrift_container_size_limit=1024)
    metadata = parquet.metadata
    scalar_types = {'step': pa.int64(), 'time_ns': pa.int64(), 'frame_id': pa.string(),
                    'observation': pa.string(), 'query_id': pa.int64(), 'chunk_offset': pa.int64(),
                    'reward': pa.float64(), 'success': pa.bool_(), 'terminated': pa.bool_(), 'truncated': pa.bool_()}
    schema = parquet.schema_arrow
    state_type, action_type = schema.field('state').type, schema.field('action').type
    if (set(schema.names) != set(scalar_types) | {'state', 'action'}
            or any(schema.field(name).type != kind for name, kind in scalar_types.items())
            or not pa.types.is_fixed_size_list(state_type) or state_type.list_size != 16
            or state_type.value_type != pa.float64()
            or not pa.types.is_list(action_type) or action_type.value_type != pa.float32()):
        raise ValueError('Pinned native signals schema required')
    if (metadata.num_rows > 601 or metadata.num_columns != 12
            or sum(metadata.row_group(i).total_byte_size for i in range(metadata.num_row_groups)) > 2 * 1024 * 1024
            or any(metadata.row_group(i).column(j).total_uncompressed_size > 2 * 1024 * 1024
                   for i in range(metadata.num_row_groups) for j in range(metadata.num_columns))):
        raise ValueError('Bounded native signals metadata required')
    for group in range(metadata.num_row_groups):
        for column_index in range(metadata.num_columns):
            column = metadata.row_group(group).column(column_index)
            name = column.path_in_schema.split('.')[0]
            width = 16 if name == 'state' else 12 if name == 'action' else 1
            if (column.num_values > 601 * width
                    or name in ('frame_id', 'observation') and column.total_uncompressed_size > 65536):
                raise ValueError('Bounded native column expansion required')
    rows = []
    for batch in parquet.iter_batches(batch_size=32):
        for name in ('frame_id', 'observation'):
            if not pc.all(pc.equal(pc.utf8_length(batch.column(name)), 64)).as_py():
                raise ValueError('Bounded signal identities required')
        lengths = pc.list_value_length(batch.column('action'))
        if not pc.all(pc.fill_null(pc.equal(lengths, 12), True)).as_py():
            raise ValueError('Native action width required')
        for item in batch.to_pylist():
            if (len(item['frame_id']) != 64 or len(item['observation']) != 64
                    or item['action'] is not None and len(item['action']) != 12):
                raise ValueError('Bounded signal identities and native action width required')
            rows.append(item)
    if type(at_frame) is not int or not 0 < at_frame < len(rows) - 1:
        raise ValueError('Interior completed control boundary required')
    row = rows[at_frame]
    if any(row[key] for key in ('success', 'terminated', 'truncated')):
        raise ValueError('Stopped episode cannot request intervention')
    queries = json.loads(read_small(source, record['queries']))['queries']
    # At a normal query boundary use the OLD chunk; the newly generated chunk would leak
    # a future decision into the early-replan question.
    preceding = [item for item in queries if item['at_frame'] < at_frame]
    if not preceding:
        raise ValueError('Already-issued chunk required')
    query = max(preceding, key=lambda item: item['at_frame'])
    offset = at_frame - query['at_frame']
    if not 0 < offset < 16:
        raise ValueError('Boundary must have pending controls in the old chunk')
    chunk = array(source, query['chunk'])
    if chunk.shape != (16, 12) or chunk.dtype != np.float32:
        raise ValueError('Native 16x12 predictions required')
    indices = sorted(set((max(0, at_frame - 8), max(0, at_frame - 4), at_frame)))
    prefix = []
    for index in indices:
        item = rows[index]
        if (item['step'] != index or item['frame_id'] != frame_id(record['episode_id'], index)
                or item['time_ns'] != index * 50_000_000 or len(item['state']) != 16
                or not np.isfinite(item['state']).all()):
            raise ValueError('Native frame identity, state, or clock differs')
        prefix.append({key: item[key] for key in ('step', 'time_ns', 'frame_id', 'state')})
    observation = json.loads(read_small(source, row['observation']))
    images = []
    for name in CAMERAS:
        ref = observation['native'][name]
        image = png_rgb(array(source, ref))
        images.append({'camera': name, 'source_array': ref['object'], 'png': output.put(image)})
    past = []
    for item in rows[max(0, at_frame - 8):at_frame]:
        action = np.asarray(item['action'])
        if action.shape != (12,) or not np.isfinite(action).all():
            raise ValueError('Finite native executed action prefix required')
        past.append({'frame_id': item['frame_id'], 'action': item['action']})
    state = {'instruction': record['instruction'], 'state_layout': STATE_LAYOUT,
             'action_layout': ACTION_LAYOUT, 'coordinate_conventions': record['coordinate_conventions'],
             'observed_prefix': prefix, 'past_executed_actions': past,
             'queued_predictions': chunk[offset:].tolist(), 'observed_frame': at_frame,
             'queued_until': query['at_frame'] + 16,
             'camera_order': list(CAMERAS), 'camera_frame': at_frame,
             'evidence_limits': 'Images are current-frame only. Native state/action history contains no privileged object poses. Predictions are not executed outcomes; uncertainty is allowed.'}
    case = {'schema_version': 1, 'kind': 'decision_case', 'schema_sha256': SCHEMA_SHA256,
            'release': release_id, 'episode_record': episode_record, 'episode_id': record['episode_id'],
            'frame_id': row['frame_id'], 'at_frame': at_frame, 'state': state, 'images': images,
            'questions': QUESTIONS, 'split': 'development', 'initialization_group': record['initialization_group'],
            'source_provenance_sha256': digest(canonical(record['provenance']))}
    return output.put(canonical(case))


def provider_body(store, case_id, model, *, vision):
    case = json.loads(read_small(store, case_id, limit=65536))
    if (case['kind'] != 'decision_case' or case['schema_sha256'] != SCHEMA_SHA256
            or case['questions'] != QUESTIONS):
        raise ValueError('Pinned decision schema required')
    state = dict(case['state'], input_modality='native_state_and_current_images' if vision else 'native_state_only')
    body = {'model': model, 'state': state, 'questions': QUESTIONS}
    if vision:
        if (not isinstance(case['images'], list) or len(case['images']) != 3
                or [item['camera'] for item in case['images']] != list(CAMERAS)):
            raise ValueError('Exact three-camera decision view required')
        images = [read_small(store, item['png'], limit=256 * 1024) for item in case['images']]
        if any(raw[:8] != b'\x89PNG\r\n\x1a\n' or len(raw) < 24
               or struct.unpack('>II', raw[16:24]) != (256, 256) for raw in images):
            raise ValueError('Pinned bounded PNG geometry required')
        body['images'] = [{'content_type': 'image/png',
                           'base64': base64.b64encode(raw).decode('ascii')} for raw in images]
    if len(canonical(body)) > 1024 * 1024:
        raise ValueError('Scorer payload exceeds bound')
    return body


def save_judgment(store, case_id, receipt):
    case = json.loads(read_small(store, case_id))
    answers = validate_answers(receipt['answers'])
    if receipt['case_id'] != case_id or receipt['schema_sha256'] != case['schema_sha256']:
        raise ValueError('Judgment input/schema binding differs')
    return store.put(canonical({'schema_version': 1, 'kind': 'decision_overlay',
        'case_id': case_id, 'episode_id': case['episode_id'], 'frame_id': case['frame_id'],
        'schema_sha256': case['schema_sha256'], 'answers': answers, 'receipt': receipt,
        'label_status': 'model_prediction_not_ground_truth', 'live_intervention_executed': False}))
