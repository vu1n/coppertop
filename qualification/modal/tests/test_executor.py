"""Offline failure-injection evidence only; never creates provider objects or loads models."""
import asyncio
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'executor'))
import common, controller, observer, freeze
import worker


def approval():
    return {'approved':True,'plan_sha256':'a'*64,**common.IDENTITY,'modal_allowance_usd':'2.00',
            'one_attempt':True,'bounded_exposure_accepted':True,'cleanup_authorized':True,
            'rate_ceiling_confirmed':True,'r2_actions':False,'expires_at':time.time()+300}


def fixture_plan():
    return {**json.loads((ROOT/'plan.json').read_bytes()),'execution_manifest_sha256':'e'*64}


class BackendDouble:
    def __init__(self,failure=None):
        self.failure=failure;self.events=[];self.stages={};self.active=False;self.counts={}
    async def open(self):self.events.append('open');return dict(common.IDENTITY)
    async def close(self):self.events.append('close')
    async def image_lookup(self):self.events.append('image')
    async def billing_rates(self):
        self.events.append('rates')
        if self.failure=='rates':raise ValueError('Synthetic account rate exceeds bound')
        return {'verified_before_allocation':True}
    async def create_app(self,run):self.events.append('app');return 'ap-'+'A'*22
    async def create_sandbox(self,app,stage):
        self.events.append('create-'+stage);self.active=True
        if self.failure=='expired-before-upload' and stage=='cpu':self.offset+=361
        if self.failure=='lost-'+stage:raise TimeoutError('synthetic lost response')
        sid='sb-'+('C' if stage=='cpu' else 'G')*22;self.stages[sid]=stage;return sid
    async def exec(self,sid,argv,seconds,stdin=b''):
        stage=self.stages[sid];self.events.append('exec-'+stage)
        if stdin:
            if self.failure=='expired-before-stage' and stage=='cpu':self.offset+=361
            with tarfile.open(fileobj=io.BytesIO(stdin),mode='r:') as t:count=len(t.getmembers())
            return common.canonical({'installed_files':count,'archive_sha256':common.sha(stdin)})
        if self.failure=='worker-'+stage:raise ValueError('synthetic failed worker')
        run=argv[argv.index('--run-id')+1]
        phases=['fresh-restored-hash','three-interpreter-dependencies','actual-native-processors'] if stage=='cpu' else [
            'fresh-restored-hash','actor-load','clef-load','stock-actor-call','disabled-clef-actor-call',
            'official-clef-joint-head-call','exact-decoded-action-parity']
        result={'status':'passed','plan_sha256':'a'*64,'execution_manifest_sha256':'e'*64,'run_id':run,
                'stage':stage,'owned_direct_children_reaped':True,'child_cleanup_errors':[],
                'training_steps':0,'simulator_controls':0,'actor_calls':2,'clef_calls':1,
                'phases':[{'phase':n,'result':{'files':37,'finite':True},'seconds':0} for n in phases]}
        if self.failure=='bad-parity' and stage=='gpu':result['phases'][-1]['result']['finite']=False
        return common.canonical(result)
    async def stop_sandbox(self,sid):
        self.events.append('stop-sandbox');self.active=False
        if self.failure=='stop-error':raise OSError('synthetic stop failure')
    async def stop_app(self,app):self.events.append('stop-app');self.active=False
    async def heartbeat(self,app):self.events.append('heartbeat')
    async def audit(self,app,stopped):
        self.events.append('audit')
        return {'confirmed':not self.active,'workspace':common.IDENTITY['workspace'],'environment':'main',
                'app_id':app,'app_stopped':stopped,'active_containers':int(self.active),
                'active_sandboxes':int(self.active),'audited_at':time.time()}


class ObserverDouble:
    pid=987654
    def __init__(self,backend,output):
        self.backend=backend;self.output=output;self.code=None
        self.task=asyncio.create_task(self.run())
    def poll(self):return self.code
    async def run(self):
        seen=set()
        while True:
            record=common.read_json(self.output/'ownership.json');g=record['generation']
            if g not in seen:
                seen.add(g)
                ack={**{key:record[key] for key in ('plan_sha256','run_id','app_id','generation','stage','execution_deadline',*common.IDENTITY)},
                     'armed':True,'observer_pid':self.pid,'observer_pgid':self.pid}
                if self.backend.failure=='gpu-ack' and record['stage']=='gpu':ack['armed']=False
                common.durable(self.output/('ack-'+str(g)+'.json'),ack)
                if self.backend.failure=='expired-before-create-'+record['stage']:
                    self.backend.offset+=361 if record['stage']=='cpu' else 541
            if record['command']=='cpu_cleanup' and not (self.output/'cpu-cleanup.json').exists():
                self.backend.events.append('independent-cpu-audit');self.backend.active=False
                receipt={**await self.backend.audit(record['app_id'],stopped=False),
                    'run_id':record['run_id'],'plan_sha256':record['plan_sha256'],'generation':2,'observer_pid':self.pid}
                if self.backend.failure=='cpu-absence':receipt['confirmed']=False;receipt['active_sandboxes']=1
                common.durable(self.output/'cpu-cleanup.json',receipt)
            if record['command']=='final_cleanup':
                receipt=await observer.cleanup_owned(self.backend,record,final=True,seconds=2)
                receipt['observer_pid']=self.pid
                common.durable(self.output/'cleanup.json',receipt);self.code=0;return
            await asyncio.sleep(.001)


class ExecutorTests(unittest.TestCase):
    def test_actual_unready_bundle_prevents_sdk_auth_or_creation(self):
        backend=BackendDouble()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            names=['plan.json','execution-manifest.json']+[x['path'] for x in common.read_json(ROOT/'execution-manifest.json')['files']]
            for name in names:
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,p)
            plan=common.read_json(root/'plan.json');plan['executable_ready']=False;common.durable(root/'plan.json',plan)
            value=approval();value['plan_sha256']=common.sha((root/'plan.json').read_bytes())
            with self.assertRaises(ValueError):
                asyncio.run(controller.execute(root,value,backend=backend))
        self.assertEqual(backend.events,[])

    def test_frozen_source_and_input_hashes_match(self):
        manifest=common.read_json(ROOT/'execution-manifest.json')
        for row in manifest['files']:common.hash_file(ROOT,row)
        self.assertLess(sum(r['bytes']for r in manifest['files']),10485760)

    def test_authenticated_model_inventory_counts_and_missing_gate(self):
        value=freeze.model_manifest(ROOT)
        self.assertEqual(value['missing_independent_digests'],[])
        self.assertEqual(sum(len(r['files'])for r in value['releases']),37)
        self.assertEqual(sum(f['bytes']for r in value['releases']for f in r['files']),29205952741)
        common.validate_models(value)
        template=next(x for x in value['releases'][1]['files'] if x['path']=='chat_template.jinja')
        self.assertEqual(template['git_blob'],'a585dec894e63da457d9440ec6aa7caa16d20860')
        missing=copy.deepcopy(value);missing['missing_independent_digests']=['chat_template.jinja']
        with self.assertRaises(ValueError):common.validate_models(missing)

    def test_hash_rejects_escape_length_mutation_and_invalid_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'good').write_bytes(b'abc')
            good={'path':'good','bytes':3,'sha256':common.sha(b'abc')}
            common.hash_file(root,good)
            for bad in ({**good,'path':'../good'},{**good,'bytes':4},{**good,'sha256':'0'*64}):
                with self.assertRaises(ValueError):common.hash_file(root,bad)
            (root/'link').symlink_to('/etc/hostname')
            with self.assertRaises(ValueError):common.hash_file(root,{**good,'path':'link'},internal_symlinks=True)

    def test_durable_exclusive_receipt_cannot_be_reconsumed(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'owned.json';common.durable(p,{'attempt':1},exclusive=True)
            with self.assertRaises(FileExistsError):common.durable(p,{'attempt':2},exclusive=True)
            self.assertEqual(common.read_json(p),{'attempt':1})

    def test_linked_retry_requires_exact_prior_absence_and_conservative_deduction(self):
        plan=common.read_json(ROOT/'plan.json')
        evidence=common.read_json(ROOT/'prior-attempt-evidence.json')
        common.validate_prior_attempt(evidence,plan,common.read_json(ROOT/'prior-retry-evidence.json'))
        for key,value in [('cpu_seconds_reserved',0),('aggregate_cpu_reserved_seconds',491),
                          ('aggregate_actor_calls_max',3),('retry_index',3),('first_reserve_usd','0.00')]:
            bad=copy.deepcopy(plan);bad['retry_link'][key]=value
            with self.assertRaises(ValueError):common.validate_prior_attempt(evidence,bad,common.read_json(ROOT/'prior-retry-evidence.json'))
        bad=copy.deepcopy(evidence);bad['actor_calls']=1
        with self.assertRaises(ValueError):common.validate_prior_attempt(bad,plan,common.read_json(ROOT/'prior-retry-evidence.json'))
        bad=copy.deepcopy(evidence);row=bad['records']['cleanup.json'];row['data']['confirmed']=False
        row['sha256']=common.sha(common.canonical(row['data']))
        with self.assertRaises(ValueError):common.validate_prior_attempt(bad,plan,common.read_json(ROOT/'prior-retry-evidence.json'))
        bad=copy.deepcopy(evidence);bad['records']['ownership.json']['data']['execution_deadline']+=11
        with self.assertRaises(ValueError):common.validate_prior_attempt(bad,plan,common.read_json(ROOT/'prior-retry-evidence.json'))

    def test_native_action_guard_rejects_dtype_nan_inf_shape_order_and_difference(self):
        import numpy as np
        good=np.zeros((16,12),dtype=np.float32);common.checked_actions(good,good.copy())
        bads=[good.astype(np.float64),np.zeros((15,12),dtype=np.float32),np.asfortranarray(good),good+np.float32(1)]
        for value in (float('nan'),float('inf')):
            bad=good.copy();bad[0,0]=value;bads.append(bad)
            with self.assertRaises(ValueError):common.checked_actions(bad,bad.copy())
        for bad in bads:
            with self.assertRaises(ValueError):common.checked_actions(good,bad)

    def test_stage_plan_cannot_increase_gpu_cpu_ram_timeout_or_calls(self):
        original=fixture_plan();controller.fixed_scope(original)
        mutations=[('resources','max_active_containers',2),('workload','actor_calls',3),
                   ('workload','clef_calls',2),('deadlines','outer_absolute_deadline_seconds',1141)]
        for section,key,value in mutations:
            bad=copy.deepcopy(original);bad[section][key]=value
            with self.assertRaises(ValueError):controller.fixed_scope(bad)
        for key in ('cpu_limit_cores','memory_request_mib','provider_timeout_seconds'):
            bad=copy.deepcopy(original);bad['resources']['gpu_stage'][key]+=1
            with self.assertRaises(ValueError):controller.fixed_scope(bad)

    def test_stop_failure_still_attempts_app_stop_and_independent_audit(self):
        backend=BackendDouble('stop-error')
        record={'app_id':'ap-'+'A'*22,'sandbox_id':'sb-'+'C'*22,'plan_sha256':'a'*64,
                'run_id':'ct-qual-'+'a'*12,'generation':4}
        receipt=asyncio.run(observer.cleanup_owned(backend,record,final=True,seconds=2))
        self.assertTrue(receipt['confirmed']);self.assertIn('stop-app',backend.events)
        self.assertIn('audit',backend.events);self.assertEqual(receipt['cleanup_errors'][0]['operation'],'SandboxTerminate')

    def scenario(self,failure=None):
        async def run(root):
            backend=BackendDouble(failure)
            backend.offset=0
            real_time=time.time
            class ControllerClock:
                gmtime=staticmethod(time.gmtime)
                strftime=staticmethod(time.strftime)
                monotonic=staticmethod(time.monotonic)
                time=staticmethod(lambda:real_time()+backend.offset)
            with patch.object(controller,'require_approval',return_value=(fixture_plan(),{'files':[]})),\
                 patch.object(controller,'verify_bundle',return_value=(fixture_plan(),{})),\
                 patch.object(controller,'time',ControllerClock):
                state,out=await controller.execute(root,approval(),backend=backend,
                    observer_factory=lambda sealed,output,record:ObserverDouble(backend,output))
            return state,backend.events,(common.read_json(out/'ownership.json') if (out/'ownership.json').exists() else {})
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);common.durable(root/'plan.json',fixture_plan());common.durable(root/'execution-manifest.json',{'files':[]})
            return asyncio.run(run(root))

    def test_expired_authority_never_starts_allocation_upload_or_native_command(self):
        expectations={'expired-before-create-cpu':(0,0),'expired-before-upload':(1,0),
                      'expired-before-stage':(1,1),'expired-before-create-gpu':(1,2)}
        for failure,(creates,execs) in expectations.items():
            with self.subTest(failure=failure):
                state,events,_=self.scenario(failure)
                self.assertEqual(events.count('create-cpu')+events.count('create-gpu'),creates)
                self.assertEqual(events.count('exec-cpu')+events.count('exec-gpu'),execs)
                self.assertEqual(state['status'],'failed')
                self.assertTrue(state['cleanup']['confirmed'])

    def test_failed_account_rate_gate_never_creates_app_or_sandbox(self):
        state,events,_=self.scenario('rates')
        self.assertNotIn('app',events);self.assertNotIn('create-cpu',events)
        self.assertEqual(state['provider_creates_attempted'],0)

    def test_release_inventory_rejects_extras_symlink_dirs_and_missing_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'config.json').write_bytes(b'{}')
            files=[{'path':'config.json'}];worker.verify_release_inventory(root,files)
            (root/'extra.py').write_bytes(b'raise SystemExit')
            with self.assertRaises(ValueError):worker.verify_release_inventory(root,files)
            (root/'extra.py').unlink();(root/'alias').symlink_to(root,target_is_directory=True)
            with self.assertRaises(ValueError):worker.verify_release_inventory(root,files)
            (root/'alias').unlink();(root/'config.json').unlink()
            with self.assertRaises(ValueError):worker.verify_release_inventory(root,files)

    def test_only_explicit_root_readme_is_optional_unverified_documentation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'config.json').write_bytes(b'{}')
            files=[{'path':'config.json'}];docs=[{'path':'README.md'}]
            receipt=worker.verify_release_inventory(root,files,docs)
            self.assertFalse(receipt['unverified_documentation'][0]['present'])
            (root/'README.md').write_text('Unverified documentation')
            receipt=worker.verify_release_inventory(root,files,docs)
            self.assertEqual(receipt['files'],1);self.assertTrue(receipt['unverified_documentation'][0]['present'])
            self.assertFalse(receipt['unverified_documentation'][0]['authenticated'])
            (root/'README.md').unlink();(root/'README.md').mkdir()
            with self.assertRaises(ValueError):worker.verify_release_inventory(root,files,docs)
            (root/'README.md').rmdir();(root/'README.md').symlink_to('/etc/hostname')
            with self.assertRaises(ValueError):worker.verify_release_inventory(root,files,docs)
            (root/'README.md').unlink();(root/'extra.jinja').write_text('runtime relevant')
            with self.assertRaises(ValueError):worker.verify_release_inventory(root,files,docs)

    def test_success_is_serial_and_cpu_absence_precedes_gpu_create(self):
        state,events,record=self.scenario()
        self.assertEqual(state['status'],'passed');self.assertTrue(state['cleanup']['confirmed'])
        self.assertLess(events.index('independent-cpu-audit'),events.index('create-gpu'))
        self.assertEqual(events.count('create-cpu'),1);self.assertEqual(events.count('create-gpu'),1)
        self.assertEqual(record['command'],'final_cleanup')

    def test_cpu_failures_never_allocate_gpu(self):
        for reason in ('lost-cpu','worker-cpu','cpu-absence'):
            with self.subTest(reason=reason):
                state,events,_=self.scenario(reason)
                self.assertNotIn('create-gpu',events);self.assertEqual(state['status'],'failed')
                self.assertTrue(state['cleanup']['confirmed']);self.assertIn('stop-app',events)

    def test_gpu_ack_failure_stops_before_allocation(self):
        state,events,_=self.scenario('gpu-ack')
        self.assertNotIn('create-gpu',events);self.assertEqual(state['status'],'failed')

    def test_lost_gpu_response_and_failed_parity_are_not_retried(self):
        for reason in ('lost-gpu','worker-gpu','bad-parity'):
            with self.subTest(reason=reason):
                state,events,_=self.scenario(reason)
                self.assertEqual(events.count('create-gpu'),1);self.assertEqual(state['status'],'failed')
                self.assertIn('stop-app',events);self.assertTrue(state['cleanup']['confirmed'])

    def test_record_binds_exact_ownership_and_existing_account(self):
        record={'plan_sha256':'a'*64,'run_id':'ct-qual-'+'a'*12,'app_id':'ap-'+'A'*22,
                **common.IDENTITY,'generation':1,'stage':'cpu','command':'running',
                'execution_deadline':time.time()+360,'outer_deadline':time.time()+1140,
                'controller_pulse':time.time(),'sandbox_id':None}
        observer.validate_record(record,'a'*64,'ct-qual-'+'a'*12)
        for field,value in [('workspace','vuluan'),('app_id','bad'),('environment','other'),('stage','larger')]:
            with self.assertRaises(ValueError):observer.validate_record({**record,field:value},'a'*64,'ct-qual-'+'a'*12)
        for field in ('execution_deadline','outer_deadline','controller_pulse'):
            for value in (float('nan'),float('inf')):
                with self.assertRaises(ValueError):observer.validate_record({**record,field:value},'a'*64,'ct-qual-'+'a'*12)
        for command in ('cpu_cleanup','final_cleanup'):
            for value in (None,float('nan'),float('inf')):
                with self.assertRaises(ValueError):observer.validate_record(
                    {**record,'command':command,'cleanup_deadline':value},'a'*64,'ct-qual-'+'a'*12)

    def test_cleanup_unconfirmed_never_becomes_success(self):
        backend=BackendDouble();backend.active=True
        async def refuse(*_):raise OSError('synthetic unavailable stop')
        backend.stop_app=refuse;backend.stop_sandbox=refuse
        record={'app_id':'ap-'+'A'*22,'sandbox_id':'sb-'+'C'*22,'plan_sha256':'a'*64,
                'run_id':'ct-qual-'+'a'*12,'generation':4}
        result=asyncio.run(observer.cleanup_owned(backend,record,final=True,seconds=.02))
        self.assertFalse(result['confirmed']);self.assertEqual(result['active_containers'],1)


if __name__=='__main__':unittest.main()
