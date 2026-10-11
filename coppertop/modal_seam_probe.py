"""The entire remote CPU probe: standard library only, exact synthetic bytes."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

PAYLOAD = bytes(range(32))
PAYLOAD_HASH = hashlib.sha256(PAYLOAD).hexdigest()
START_PREFIX = 'COPPERTOP_MODAL_SEAM_START '


def validate_request(request):
    fields = {'schemaVersion', 'runId', 'generation', 'bodyId', 'mode',
              'payloadBase64', 'payloadSha256', 'sourceSha256', 'deadlineMs'}
    if not isinstance(request, dict) or set(request) != fields:
        raise ValueError('Exact synthetic request required')
    run = request['runId']
    if (type(request['schemaVersion']) is not int or request['schemaVersion'] != 1
        or not isinstance(run, str) or not re.fullmatch(r'ct-modal-[a-z0-9-]{1,32}', run)
        or request['generation'] != run+'-e1'
        or request['mode'] not in ('complete', 'hold')
        or request['bodyId'] != run+'-'+request['mode']
        or request['payloadBase64'] != base64.b64encode(PAYLOAD).decode()
        or request['payloadSha256'] != PAYLOAD_HASH
        or not isinstance(request['sourceSha256'], str)
        or not re.fullmatch(r'[a-f0-9]{64}', request['sourceSha256'])
        or type(request['deadlineMs']) is not int):
        raise ValueError('Synthetic request differs from fixed scope')
    return request


def runtime_container_id():
    # This is Modal's nonsecret runtime identifier; no credential is accessed.
    identity = os.environ.get('MODAL_TASK_ID')
    if not isinstance(identity, str) or not re.fullmatch(r'ta-[A-Za-z0-9]+', identity):
        raise ValueError('Modal runtime container identity missing')
    return identity


def probe(request):
    request = validate_request(request)
    now = int(time.time()*1000)
    if not now < request['deadlineMs'] <= now+180_000:
        raise ValueError('Probe authority expired or expanded')
    if sys.version_info[:2] != (3, 12):
        raise ValueError('Reviewed Python 3.12 image required')
    source = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if source != request['sourceSha256']:
        raise ValueError('Mounted probe source differs from approval')
    if request['mode'] == 'hold' and int(time.time()*1000)+20_000 >= request['deadlineMs']:
        raise ValueError('Insufficient hold authority')
    container = runtime_container_id()
    marker = {key: request[key] for key in ('runId', 'generation', 'bodyId')}
    marker.update(schemaVersion=1, event='started', containerId=container,
                  sourceSha256=source, payloadSha256=PAYLOAD_HASH)
    print(START_PREFIX+json.dumps(marker, sort_keys=True, separators=(',', ':')), flush=True)
    if request['mode'] == 'hold':
        time.sleep(20)
    if int(time.time()*1000) >= request['deadlineMs']:
        raise ValueError('Probe authority expired before receipt')
    return {'schemaVersion': 1, 'runId': request['runId'],
            'generation': request['generation'], 'bodyId': request['bodyId'],
            'sourceSha256': source, 'containerId': container,
            'payloadBase64': base64.b64encode(PAYLOAD).decode(),
            'digest': PAYLOAD_HASH, 'bytes': 32}
