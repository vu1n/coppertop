"""Read-only code, payload, history and remaining-budget checks; no provider imports."""
import hashlib
import json
from pathlib import Path
import sys


def verify(root):
    root=Path(root).resolve(strict=True)
    def load(name):return json.loads((root/name).read_text())
    def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
    portable=load('portable-manifest.json');names=set()
    for row in portable['files']:
        name=row['path'];path=root/name
        if name in names or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Invalid portable file path')
        names.add(name)
        if path.stat().st_size!=row['bytes'] or digest(path)!=row['sha256']:
            raise ValueError('Portable file bytes differ')
    actual={p.relative_to(root).as_posix()for p in root.rglob('*')if p.is_file()
            and '__pycache__'not in p.parts and p.suffix!='.pyc'}
    if actual!=names|{'portable-manifest.json'}:raise ValueError('Portable closure differs')
    reviewed=load('reviewed-files.json')
    if len(reviewed['exact_reviewed_code_and_tests'])!=13:raise ValueError('Reviewed code closure differs')
    for row in reviewed['exact_reviewed_code_and_tests']:
        if digest(root/row['path'])!=row['sha256']:raise ValueError('Reviewed code bytes differ')
    sys.path.insert(0,str(root/'executor'))
    from common import verify_bundle
    plan,_=verify_bundle(root,digest(root/'plan.json'),require_ready=False)
    if plan['executable_ready'] or plan['portability']['launch_allowed']:
        raise ValueError('Archived plan must remain unready')
    if not (root/'consumed-attempt.json').is_file() or (root/'launch-approval.json').exists():
        raise ValueError('Archived consumed marker and no launch approval required')
    ledger=load('continuation-ledger.json')
    expected={'cpu_reserved_seconds':393,'gpu_reserved_seconds':660,'actor_calls':2,'clef_calls':1,
              'model_loads':{'xiaomi':1,'clef':1}}
    if (ledger['total_original_modal_allowance_usd']!='2.00' or ledger['remaining']!=expected
            or ledger['used']['cpu_reserved_seconds']!=87 or ledger['launch_ready']
            or ledger['model_qualification_succeeded']
            or sum(a['conservative_cpu_seconds']for a in ledger['attempts'])!=87
            or not all(a['cleanup_confirmed']for a in ledger['attempts'])):
        raise ValueError('Original allowance or remaining budget differs')
    third=load('evidence/third-attempt.json');counts=third['phase_counts']
    if (third['proxy_connect_status']!=403 or third['conservative_cpu_seconds']!=13
            or third['exec_requests']!=0 or third['upload_bytes']!=0
            or any(counts[p]!=0 for p in ('router-connect-ack','exec-start-attempt','exec-start-ack',
                                          'stdin-start','stdin-ack','exec-readback-ack'))
            or not third['cleanup']['confirmed'] or not third['fresh_readonly_audit']['confirmed']):
        raise ValueError('Terminal proof and cleanup evidence differs')
    template=next(row for row in load('model-manifest.json')['releases'][1]['files']
                  if row['path']=='chat_template.jinja')
    if template['git_blob']!='a585dec894e63da457d9440ec6aa7caa16d20860' or template['bytes']!=7756:
        raise ValueError('Pinned template identity differs')
    return {'portable_files':len(names),'exact_reviewed_code_files':13,'remaining_cpu_seconds':393,
            'remaining_gpu_seconds':660,'qualification_succeeded':False,'launch_ready':False,'provider_calls':0}


if __name__=='__main__':
    try:print(json.dumps(verify(Path(__file__).resolve().parent)))
    except Exception as error:
        print(json.dumps({'verified':False,'error_type':type(error).__name__}));raise SystemExit(1)
