"""Local sealing only. No credentials, SDK, native processors or model execution."""
import ast
import json
from pathlib import Path
import re
import sys
from common import canonical, durable, read_json, sha, validate_models


def model_manifest(root):
    xiaomi=read_json(root/'source/containers/gpu-smoke-pins.json')['model']
    clef=read_json(root/'source/manifests/clef-recovery/model-release.json')
    extras={}
    upstream_path=root/'clef-upstream-tree.json'
    if upstream_path.exists():
        upstream=read_json(upstream_path,262144)
        expected={row['path']:row['bytes'] for row in clef['files']}
        for row in upstream:
            name=row.get('path')
            if name in expected and row.get('type')=='file' and row.get('size')==expected[name]:
                oid=row.get('oid')
                if isinstance(oid,str) and re.fullmatch(r'[a-f0-9]{40}',oid):
                    extras[name]={'git_blob':oid}
    # Bounded authorized parent relay is acceptable only with exact revision plus independent provenance.
    relay=root/'clef-upstream-digests.json'
    if relay.exists():
        value=read_json(relay)
        if (value['repo']!=clef['repo'] or value['revision']!=clef['revision']
                or value.get('provenance')!='independent_pinned_upstream'):
            raise ValueError('Exact independent pinned upstream relay required')
        for row in value['files']:
            if row['path'] in extras:raise ValueError('Duplicate independent digest source')
            digest={k:v for k,v in row.items() if k in ('sha256','git_blob')}
            if len(digest)!=1:raise ValueError('Independent exact digest required')
            extras[row['path']]=digest
    known={'config.json':sha((root/'source/manifests/clef-recovery/config.json').read_bytes()),
           'joint_head_config.json':sha((root/'source/manifests/clef-recovery/joint_head_config.json').read_bytes()),
           'LICENSE':'bbedc3fda3305820b977265f01b8619d87570a6739de3a5582c3464840f1e57a',
           'joint_schema_model.py':'0e304cf7c6500e8bb59bef7e2afd2c6373f82596dfb3b57d1aa93c175e2dc3a3'}
    files=[];missing=[]
    for row in clef['files']:
        if row['path']=='README.md':continue  # Explicit unverified documentation exception; never executed/read.
        entry={'path':row['path'],'bytes':row['bytes']}
        if row.get('lfs'):entry['sha256']=row['lfs']['sha256']
        elif row['path'] in known:entry['sha256']=known[row['path']]
        elif row['path'] in extras:entry.update(extras[row['path']])
        else:missing.append(row['path'])
        files.append(entry)
    value={'schema_version':1,'missing_independent_digests':missing,'releases':[
        {'repo':xiaomi['repo'],'revision':xiaomi['revision'],'root':'/work/model',
         'files':[{('path' if k=='name' else k):v for k,v in row.items()} for row in xiaomi['files']]},
        {'repo':clef['repo'],'revision':clef['revision'],'root':'/opt/coppertop-clef/clef-model','files':files}]}
    value['releases'][1]['unverified_documentation']=[{'path':'README.md','upstream_inventory_bytes':10239,
        'authenticated':False,'required_for_runtime':False,'presence':'optional'}]
    if not missing:validate_models(value)
    return value


def freeze(root):
    models=model_manifest(root);durable(root/'model-manifest.json',models)
    names=['source-index.json','input-manifest.json','model-manifest.json']
    names += ['source/'+row['path'] for row in read_json(root/'source-index.json')['files']]
    names += ['input/'+row['path'] for row in read_json(root/'input-manifest.json')['files']]
    names += ['executor/'+p.name for p in sorted((root/'executor').glob('*.py'))]
    if (root/'clef-upstream-tree.json').exists():names.append('clef-upstream-tree.json')
    if (root/'clef-upstream-digests.json').exists():names.append('clef-upstream-digests.json')
    names += ['prior-attempt-evidence.json','prior-retry-evidence.json']
    if len(names)!=len(set(names)):raise ValueError('Duplicate execution closure')
    entries=[]
    for name in sorted(names):
        path=root/name
        if path.is_symlink():raise ValueError('Regular execution bytes required')
        raw=path.read_bytes()
        if name.endswith('.py'):ast.parse(raw)
        entries.append({'path':name,'bytes':len(raw),'sha256':sha(raw)})
    execution={'schema_version':1,'files':entries,'payload_file_bytes':sum(e['bytes'] for e in entries),
               'no_paid_validation':True}
    durable(root/'execution-manifest.json',execution)
    plan=read_json(root/'plan.json');plan['execution_manifest_sha256']=sha((root/'execution-manifest.json').read_bytes())
    plan['executor_implemented']=True;plan['executable_ready']=False
    plan['approval_status']='explicit_user_approval_recorded_implementation_gates_pending'
    plan['paid_execution_authorized']=True
    plan['integrity_gates']['missing_clef_non_lfs_files']=models['missing_independent_digests']
    plan['execution_files']=len(entries)
    plan['remaining_handoff']={
        'clef_non_lfs_digests':models['missing_independent_digests'],
        'independent_final_code_review':'Pending latest exact controller/worker/observer closure review.'}
    durable(root/'plan.json',plan)
    return {'plan_sha256':sha((root/'plan.json').read_bytes()),
            'execution_manifest_sha256':plan['execution_manifest_sha256'],
            'execution_files':len(entries),'source_and_executor_bytes':sum(e['bytes'] for e in entries if not e['path'].startswith('input/')),
            'input_bytes':sum(e['bytes'] for e in entries if e['path'].startswith('input/')),
            'missing_independent_digests':models['missing_independent_digests'],'paid_actions':0}


if __name__=='__main__':
    print(json.dumps(freeze(Path(sys.argv[1]).resolve(strict=True))))
