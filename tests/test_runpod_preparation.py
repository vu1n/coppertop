"""Tiny real tar/file/SDK controls; never proof of cloud restoration or throughput."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import gzip
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from coppertop import runpod_bundle as b,runpod_bootstrap as boot,runpod_prepare as prep
from coppertop.runpod_relay import Relay,copy_shards,feasibility,PAYLOAD
from coppertop.runpod_runtime import contract
from coppertop.runpod_staging import RunpodVolumeConfig
from coppertop.runpod_cloud import save,RestAPI
from coppertop.chords import canonical,digest
from coppertop.streams import file_ref
from coppertop.multipart import UploadInterrupted
from coppertop.reload_launch import read_json
from test_record_archive import DiskS3
from test_storage import S3Error
from test_runpod_cloud import inputs,API


def source(root):
    for name in [*contract()['required_trees'],'debs','app']:(root/name).mkdir(parents=True)
    for name in contract()['required_files']:
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture-native-bytes\n')
    (root/'work/sim/bin/python').unlink();(root/'work/sim/bin/python').symlink_to('/opt/conda/bin/python')
    (root/'debs/lock.json').write_bytes(b'{}')
    (root/'app/containers').mkdir();(root/'app/containers/gpu-smoke-pins.json').write_bytes(b'{}')
    (root/'work/model/model.bin').write_bytes(bytes(range(256))*10)
    return root


def samples(seconds=.001):return [{'bytes':8*1024**2,'seconds':seconds,'checked_utc':datetime.now(timezone.utc).isoformat()}]*2


def config():
    return {'attempt':'preparation-test','cleanup_owner':'test operator',
        'CPU_quote':{'type_id':'cpu3g-2-4','flavor_id':'cpu3g','vcpus':4,'ram_gib':'20','hour_usd':'0.40','datacenter':'US-WA-1',
            'availability':'HIGH','checked_utc':datetime.now(timezone.utc).isoformat()},
        'base_metadata':{'image':contract()['base'],'platform':'linux/amd64','compressed_bytes':1024},
        'package_admission':{'offline_bytes_hashes_locked':True,'signature_verification_stage':'source CPU before export','architecture':'amd64','release':'jammy','bytes':1024,'lock_sha256':'d'*64}}


def approval(p):
    return dict(plan_sha256=p['plan_sha256'],additional_cap_usd='12.00',**dict.fromkeys(
        ('execute_CPU_preparation','transfer_frozen_bytes','create_one_volume_and_CPU_Pod',
         'retain_up_to14days_then_delete_owned_volume','accept_failure_storage_until_cleanup'),True))


class RP(DiskS3):
    def __init__(self,path):
        super().__init__(path);self.upload_keys={};self.lose_create=False;self.lose_complete=False
        self.meta=SimpleNamespace(endpoint_url='https://s3api-us-wa-1.runpod.io/',region_name='US-WA-1',
            config=SimpleNamespace(connect_timeout=10,read_timeout=20,retries={'total_max_attempts':1}))
    def put_object(self,**kwargs):return super().put_object(**dict(kwargs,IfNoneMatch='*'))
    def get_object(self,**kwargs):
        if not self.object(kwargs['Key']).exists():raise S3Error('NoSuchKey')
        return super().get_object(**kwargs)
    def create_multipart_upload(self,**kwargs):
        if (self.root/'upload').exists():shutil.rmtree(self.root/'upload')
        value=super().create_multipart_upload(**kwargs);self.upload_keys[kwargs['Key']]=value['UploadId']
        if self.lose_create:self.lose_create=False;raise OSError('lost create')
        return value
    def complete_multipart_upload(self,**kwargs):
        value=super().complete_multipart_upload(**kwargs);self.upload_keys.pop(kwargs['Key'],None)
        if self.lose_complete:self.lose_complete=False;raise OSError('lost complete')
        return value
    def list_multipart_uploads(self,**kwargs):return {'Uploads':[{'Key':k,'UploadId':v} for k,v in self.upload_keys.items()]}
    def abort_multipart_upload(self,**kwargs):
        if kwargs['Key'] not in self.upload_keys:raise S3Error('NoSuchUpload')
        self.upload_keys.pop(kwargs['Key']);return super().abort_multipart_upload(**kwargs)
    def list_parts(self,**kwargs):
        if kwargs['Key'] not in self.upload_keys:raise S3Error('NoSuchUpload')
        return super().list_parts(**kwargs)
    def delete_object(self,**kwargs):self.object(kwargs['Key']).unlink(missing_ok=True)


class CapacityFixture(unittest.TestCase):
    def setUp(self):
        # Tiny real tar fixtures need no production-size disk reservation. Test
        # the capacity admission separately; never relax the runtime guard for CI.
        capacity=patch('coppertop.runpod_bundle.shutil.disk_usage',
            return_value=SimpleNamespace(free=64*b.GIB))
        capacity.start();self.addCleanup(capacity.stop)


class BundleTest(CapacityFixture):
    def test_insufficient_source_scratch_blocks_without_complete_export(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);src=source(root/'source');original=(src/'work/model/model.bin').read_bytes()
            with patch('coppertop.runpod_bundle.shutil.disk_usage',return_value=SimpleNamespace(free=52*b.GIB-1)):
                with self.assertRaisesRegex(ValueError,'needs52GiB'):
                    b.export(src,root/'bundle')
            self.assertEqual((src/'work/model/model.bin').read_bytes(),original)
            self.assertFalse((root/'bundle/bundle.json').exists());self.assertFalse(list((root/'bundle').iterdir()))

    def test_real_split_roundtrip_modes_absolute_links_and_no_volume_extraction(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);src=source(root/'source');(src/'work/model/model.bin').chmod(0o640)
            d=b.export(src,root/'bundle',shard_bytes=8192);self.assertGreater(len(d['shards']),1)
            b.restore(root/'bundle',root/'container/restore',volume=root/'bundle')
            restored=root/'container/restore'
            self.assertEqual((restored/'work/model/model.bin').read_bytes(),(src/'work/model/model.bin').read_bytes())
            self.assertEqual((restored/'work/model/model.bin').stat().st_mode&0o777,0o640)
            self.assertEqual(os.readlink(restored/'work/sim/bin/python'),'/opt/conda/bin/python')
            self.assertTrue((restored/'.restore-complete.json').exists())
            self.assertEqual((src/'work/model/model.bin').read_bytes(),bytes(range(256))*10)
            with self.assertRaises(ValueError):b.restore(root/'bundle',root/'bundle/extracted',volume=root/'bundle')

    def test_source_and_partial_exports_survive_failure(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);src=source(root/'source')
            with patch.dict(b.BOUNDS,tar=100),self.assertRaises(ValueError):b.export(src,root/'failed')
            self.assertTrue(src.exists());self.assertTrue((root/'failed').exists());self.assertFalse((root/'failed/bundle.json').exists())
            with self.assertRaises(ValueError):b.export(src,root/'failed')

    def test_tamper_and_insufficient_container_capacity_fail_before_ready(self):
        for mutation in ('shard','manifest','free','contract'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as root:
                root=Path(root);b.export(source(root/'source'),root/'bundle')
                if mutation=='shard':(root/'bundle/shard-00.tar').write_bytes(b'bad')
                if mutation=='manifest':(root/'bundle/manifest.json').write_bytes(b'{}')
                if mutation=='contract':
                    m=read_json(root/'bundle/manifest.json',b.BOUNDS['manifest']);m['runtime_contract_sha256']='0'*64
                    save(root/'bundle/manifest.json',m);d=read_json(root/'bundle/bundle.json');d['manifest_sha256']=digest(canonical(m));save(root/'bundle/bundle.json',d)
                with self.assertRaises((ValueError,KeyError)):
                    b.restore(root/'bundle',root/'restore',volume=root/'bundle',**({'free_bytes':1} if mutation=='free' else {}))
                self.assertFalse((root/'restore/.restore-complete.json').exists())

    def test_escaping_links_special_files_privileged_modes_and_missing_prefix_rejected(self):
        for mutation in ('link','fifo','setuid','prefix'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as root:
                root=Path(root);src=source(root/'source')
                if mutation=='link':(src/'work/model/escape').symlink_to('/etc/passwd')
                if mutation=='fifo':os.mkfifo(src/'work/model/fifo')
                if mutation=='setuid':
                    value=b.manifest(src,deadline=time.monotonic()+10)
                    value['entries']['work/model/model.bin']['mode']=0o4755
                    with self.assertRaises(ValueError):b.validate_manifest(value)
                    continue # macOS managed filesystem strips setuid on chmod.
                if mutation=='prefix':shutil.rmtree(src/'work/robosuite')
                with self.assertRaises(ValueError):b.export(src,root/'bundle')

    def test_member_under_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            src=source(Path(root)/'source');value=b.manifest(src,deadline=time.monotonic()+10)
            value['entries']['work/sim/bin/python/evil']={'kind':'directory','mode':0o755}
            with self.assertRaisesRegex(ValueError,'under a link'):b.validate_manifest(value)

    def test_hostile_tar_never_extracts_or_marks_complete(self):
        for kind in ('parent','hardlink','device','duplicate','wrong_size'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as root:
                root=Path(root);b.export(source(root/'source'),root/'bundle')
                d=read_json(root/'bundle/bundle.json');path=root/'bundle/shard-00.tar'
                with tarfile.open(path,'w') as tar:
                    m=tarfile.TarInfo('../escaped' if kind=='parent' else 'work/model/model.bin')
                    if kind=='hardlink':m.type=tarfile.LNKTYPE;m.linkname='opt/conda/bin/python'
                    if kind=='device':m.type=tarfile.CHRTYPE
                    tar.addfile(m)
                    if kind=='duplicate':tar.addfile(m)
                ref=file_ref(path);d.update(tar_sha256=ref.sha256,tar_bytes=ref.size,shards=[dict(ref.to_dict(),name=path.name)])
                save(root/'bundle/bundle.json',d)
                with self.assertRaises(ValueError):b.restore(root/'bundle',root/'restore',volume=root/'bundle')
                self.assertFalse((root/'escaped').exists());self.assertFalse((root/'restore/.restore-complete.json').exists())


class RelayTest(CapacityFixture):
    def fixture(self,root,large=False):
        src=source(root/'source')
        if large:
            with (src/'work/model/model.bin').open('wb') as out:out.truncate(17*1024**2)
        b.export(src,root/'bundle');control=root/'control.tar';control.write_bytes(b'control-fixture')
        s3=RP(root/'remote');cfg=RunpodVolumeConfig('owned','US-WA-1','https://s3api-us-wa-1.runpod.io/')
        owner={'attempt':'preparation-test','plan_sha256':'e'*64,'volume_id':'owned','allocated_gb':60,'datacenter':'US-WA-1'}
        relay=Relay(cfg,s3,owner=owner,journal=root/'relay.json',memory_check=lambda:0)
        return relay,s3,control

    def test_staged_marker_is_last_and_full_readback_required(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);r,s3,c=self.fixture(root);ready=r.stage(root/'bundle',c,samples=samples())
            self.assertTrue(ready['all_staged_bytes_verified']);self.assertFalse(ready['extracted_on_volume'])
            self.assertEqual(r.state['status'],'staged');self.assertTrue(s3.object(r.prefix+'/ready.json').exists())
            self.assertLessEqual(r.state['written_bytes'],PAYLOAD)
            with self.assertRaises(ValueError):Relay(r.config,s3,owner=r.owner,journal=r.journal)

    def test_interrupted_multipart_resumes_same_owned_ID_and_cumulative_budget(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);r,s3,c=self.fixture(root,True);s3.fail_part=2
            with self.assertRaises(UploadInterrupted):r.stage(root/'bundle',c,samples=samples())
            self.assertFalse(s3.object(r.prefix+'/ready.json').exists());self.assertTrue((root/'bundle/shard-00.tar').exists())
            before=r.state['written_bytes'];created=s3.calls.count('create');s3.fail_part=None
            resumed=Relay(r.config,s3,owner=r.owner,journal=r.journal,resume=True,memory_check=r.memory_check)
            resumed.stage(root/'bundle',c,samples=samples())
            self.assertEqual(s3.calls.count('create'),created);self.assertGreater(resumed.state['written_bytes'],before)

    def test_lost_create_cannot_blindly_retry_and_exact_reconcile_is_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);r,s3,c=self.fixture(root,True);s3.lose_create=True
            with self.assertRaises(OSError):r.stage(root/'bundle',c,samples=samples())
            resumed=Relay(r.config,s3,owner=r.owner,journal=r.journal,resume=True,memory_check=r.memory_check)
            with self.assertRaisesRegex(ValueError,'Lost multipart'):resumed.stage(root/'bundle',c,samples=samples())
            resumed.reconcile_lost_create();resumed.stage(root/'bundle',c,samples=samples())
            self.assertEqual(s3.calls.count('create'),1)

    def test_lost_complete_reconciles_full_object_without_duplicate_upload(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);r,s3,c=self.fixture(root,True);s3.lose_complete=True
            with self.assertRaises(UploadInterrupted):r.stage(root/'bundle',c,samples=samples())
            calls=s3.calls.count('part');resumed=Relay(r.config,s3,owner=r.owner,journal=r.journal,resume=True,memory_check=r.memory_check)
            resumed.stage(root/'bundle',c,samples=samples());self.assertEqual(s3.calls.count('part'),calls)

    def test_resumed_wrong_owner_source_tamper_and_budgets_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);r,s3,c=self.fixture(root)
            with self.assertRaises(ValueError):Relay(r.config,s3,owner=dict(r.owner,volume_id='wrong'),journal=r.journal,resume=True)
            r.state['requests']=4096;r.persist()
            with self.assertRaisesRegex(ValueError,'request budget'):r.stage(root/'bundle',c,samples=samples())
            self.assertFalse(s3.object(r.prefix+'/ready.json').exists())

    def test_corrupt_full_readback_and_expired_resume_keep_sources(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);r,s3,c=self.fixture(root,True);s3.corrupt=True
            with self.assertRaises(UploadInterrupted):r.stage(root/'bundle',c,samples=samples())
            self.assertTrue((root/'bundle/shard-00.tar').exists());self.assertFalse(s3.object(r.prefix+'/ready.json').exists())
            r.state['started_utc']=(datetime.now(timezone.utc)-timedelta(hours=3)).isoformat();r.persist()
            with self.assertRaises(TimeoutError):r.stage(root/'bundle',c,samples=samples())

    def test_expired_transfer_can_abort_under_separate_bounded_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);r,s3,c=self.fixture(root,True);s3.fail_part=2
            with self.assertRaises(UploadInterrupted):r.stage(root/'bundle',c,samples=samples())
            r.state['started_utc']=(datetime.now(timezone.utc)-timedelta(hours=3)).isoformat();r.state['requests']=4096;r.persist()
            r.state['written_bytes']=PAYLOAD+1 # A failed write admission cannot block explicit abort.
            r.abort_owned();self.assertTrue(r.state['uploads_absent']);self.assertFalse(s3.upload_keys)
            self.assertLessEqual(r.state['cleanup_requests'],64);self.assertTrue((root/'bundle/shard-00.tar').exists())

    def test_resource_usage_failure_stops_before_remote_requests(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);r,s3,c=self.fixture(root)
            from coppertop.runpod_relay import check_memory
            r.memory_check=check_memory
            with patch('coppertop.runpod_relay.resource.getrusage',return_value=SimpleNamespace(ru_maxrss=600*1024**2)),patch('coppertop.runpod_relay.sys.platform','darwin'):
                with self.assertRaises(MemoryError):r.stage(root/'bundle',c,samples=samples())
            self.assertFalse(s3.calls)

    def test_public_copy_API_partial_and_verified_resume(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);d=b.export(source(root/'source'),root/'bundle',shard_bytes=8192)
            fs=Mock();fs.copy_to_local.side_effect=lambda remote,local:shutil.copyfile(root/'bundle'/Path(remote).name,local)
            copy_shards(fs,'/remote',root/'local',d,journal=root/'copy.json',sample=samples()[0],seconds_left=100)
            calls=fs.copy_to_local.call_count
            copy_shards(fs,'/remote',root/'local',d,journal=root/'copy.json',sample=samples()[0],seconds_left=100)
            self.assertEqual(fs.copy_to_local.call_count,calls)
            (root/'local/shard-00.tar').write_bytes(b'bad')
            with self.assertRaises(ValueError):copy_shards(fs,'/remote',root/'local',d,journal=root/'copy.json',sample=samples()[0],seconds_left=100)

    def test_partial_copy_requires_explicit_reconciliation_and_cumulative_bytes_are_not_reset(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);d=b.export(source(root/'source'),root/'bundle')
            def interrupted(remote,target):Path(target).write_bytes(b'partial');raise OSError('lost connection')
            fs=Mock();fs.copy_to_local.side_effect=interrupted
            with self.assertRaises(OSError):copy_shards(fs,'/remote',root/'local',d,journal=root/'copy.json',sample=samples()[0],seconds_left=100)
            reserved=read_json(root/'copy.json')['bytes_reserved']
            with self.assertRaisesRegex(ValueError,'Partial'):copy_shards(fs,'/remote',root/'local',d,journal=root/'copy.json',sample=samples()[0],seconds_left=100)
            self.assertEqual(read_json(root/'copy.json')['bytes_reserved'],reserved)

    def test_slow_stale_and_nan_network_samples_fail_closed(self):
        for mutation in ('slow','stale','nan','short'):
            value=samples(30)
            if mutation=='stale':value[0]['checked_utc']=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
            if mutation=='nan':value[0]['seconds']=float('nan')
            if mutation=='short':value[0]['bytes']=1
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):feasibility(value,bytes_each=50*b.GIB,seconds_left=7200)


class PackageAndBootstrapTest(CapacityFixture):
    def packages(self,directory):
        directory.mkdir();data=b'fake deb bytes';(directory/'opengl.deb').write_bytes(data)
        seed=read_json(Path(boot.__file__).resolve().parents[1]/'containers/runpod-seed-inventory.json',65536)
        versions={v['package'].split(':')[0]:v['version'] for v in seed['system_packages']}
        required={n:('1.4.0-1' if n=='libopengl0' else versions[n]) for n in contract()['system_packages']}
        package={'file':'opengl.deb','package':'libopengl0','version':'1.4.0-1','architecture':'amd64',
                 'sha256':digest(data),'size':len(data),'ubuntu_filename':'pool/main/libopengl0.deb'}
        text=('Package: libopengl0\nVersion: 1.4.0-1\nArchitecture: amd64\nSHA256: '+digest(data)+
            '\nSize: '+str(len(data))+'\nInstalled-Size: 32\nFilename: pool/main/libopengl0.deb\n\n').encode()
        (directory/'Packages.gz').write_bytes(gzip.compress(text));ref=file_ref(directory/'Packages.gz')
        (directory/'InRelease').write_text('-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\nSHA256:\n '+ref.sha256+' '+str(ref.size)+' main/binary-amd64/Packages.gz\n-----BEGIN PGP SIGNATURE-----\nfixture only\n')
        lock={'schema_version':1,'release':'jammy','architecture':'amd64','required':required,
            'base':{n:v for n,v in required.items() if n!='libopengl0'},'packages':[package],
            'indexes':[{'origin':'https://archive.ubuntu.com/ubuntu/','inrelease':'InRelease','file':'Packages.gz',
                'release_name':'main/binary-amd64/Packages.gz'}]}
        save(directory/'lock.json',lock)
        def runner(args,**kwargs):
            stdout=b''
            if args[0]=='dpkg-deb':stdout=b'Package: libopengl0\nVersion: 1.4.0-1\nArchitecture: amd64\n'
            if args[0]=='dpkg-query':stdout=(required[args[-1]]+'\tinstall ok installed').encode()
            return SimpleNamespace(returncode=0,stdout=stdout,stderr=b'')
        return lock,runner

    def test_signed_index_to_deb_chain_with_exact_seed_required_versions(self):
        with tempfile.TemporaryDirectory() as root:
            lock,runner=self.packages(Path(root)/'debs');actual,installed=boot.verify_packages(Path(root)/'debs',runner=runner)
            self.assertEqual(lock,actual);self.assertEqual(installed,32*1024)

    def test_Mac_input_gate_matches_exact_closure_bytes_before_SDK_or_credentials(self):
        from coppertop.runpod_source import ModalSource
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);lock,_=self.packages(root/'debs');c=config()
            c['package_admission'].update(lock_sha256=digest(canonical(lock)),
                bytes=sum(p.stat().st_size for p in (root/'debs').rglob('*') if p.is_file()))
            p=prep.prepare(c);control=root/'control.tar';prep.pack_control(control)
            ModalSource().validate_inputs(p,root/'debs',control)
            c['package_admission']['bytes']+=1;p=prep.prepare(c)
            with self.assertRaisesRegex(ValueError,'closure bytes'):ModalSource().validate_inputs(p,root/'debs',control)

    def test_corrupt_deb_index_signature_or_missing_opengl_fail(self):
        for mutation in ('deb','index','signature','pin','duplicate','architecture'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as root:
                directory=Path(root)/'debs';lock,runner=self.packages(directory)
                if mutation=='deb':(directory/'opengl.deb').write_bytes(b'changed')
                if mutation=='index':(directory/'Packages.gz').write_bytes(b'changed')
                if mutation=='signature':runner=Mock(return_value=SimpleNamespace(returncode=1))
                if mutation=='pin':lock['required']['libopengl0']='new';save(directory/'lock.json',lock)
                if mutation=='duplicate':lock['packages']*=2;save(directory/'lock.json',lock)
                if mutation=='architecture':lock['packages'][0]['architecture']='arm64';save(directory/'lock.json',lock)
                with self.assertRaises(ValueError):boot.verify_packages(directory,runner=runner)

    def test_real_restore_then_mock_package_and_library_admission(self):
        for outcome in ('pass','closure','library','space'):
            with self.subTest(outcome=outcome),tempfile.TemporaryDirectory() as root:
                root=Path(root);src=source(root/'source');shutil.rmtree(src/'debs');lock,runner=self.packages(src/'debs')
                b.export(src,root/'volume/bundle')
                admit=Mock(return_value={'status':'passed','system_libraries_loaded':True,'cuda_executed':False,'render_reset_executed':False})
                if outcome=='library':admit.return_value['system_libraries_loaded']=False
                if outcome=='closure':
                    original=runner
                    def runner(args,**kwargs):
                        if args[0]=='dpkg':return SimpleNamespace(returncode=1)
                        return original(args,**kwargs)
                kwargs=dict(volume=root/'volume',root=root/'container',scratch=root/'container/tmp/restore',
                    runner=runner,admit=admit,root_bytes=70*b.GIB if outcome=='space' else 1024)
                if outcome=='pass':
                    result=boot.bootstrap(root/'volume/bundle',**kwargs)
                    self.assertTrue(result['absolute_paths_preserved']);self.assertFalse(result['extracted_on_network_volume'])
                    self.assertEqual(os.readlink(root/'container/opt/model'),'/work/model')
                    self.assertTrue((root/'volume/bundle/cpu-receipt.json').exists())
                else:
                    with self.assertRaises(ValueError):boot.bootstrap(root/'volume/bundle',**kwargs)
                    self.assertFalse((root/'volume/bundle/cpu-receipt.json').exists())
                self.assertTrue((root/'volume/bundle/shard-00.tar').exists())


class ControllerTest(unittest.TestCase):
    def test_control_archive_exact_membership_bound_to_reviewed_code(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);p=prep.prepare(config());path=root/'control.tar';prep.pack_control(path)
            ref=prep.verify_control(path,p['plan']['code']);self.assertEqual(ref,file_ref(path).to_dict())
            wrong=deepcopy(p['plan']['code']);next(iter(wrong.values()))['sha256']='0'*64
            with self.assertRaises(ValueError):prep.verify_control(path,wrong)

    def fixture(self,root):
        p=prep.prepare(config());api=API();api.cpu_quote=Mock(return_value=p['plan']['config']['CPU_quote'])
        src=Mock();bundle=root/'bundle';bundle.mkdir();src.collect.return_value=bundle
        control=root/'control.tar';control.write_bytes(b'control')
        relay=Mock();relay.stage.return_value={'bundle':{'manifest_sha256':'b'*64,'tar_sha256':'c'*64}}
        transport=Mock(return_value=relay);transport.probe.return_value=samples()
        transport.wait_CPU.return_value={'status':'passed','manifest_sha256':'b'*64,'tar_sha256':'c'*64,
            'extracted_on_network_volume':False}
        # The existing fake API's GPU price is patched to this CPU quote.
        original=api.request
        def request(method,resource,payload=None):
            value=original(method,resource,payload)
            if method=='POST' and resource=='pods':value.update(costPerHr='0.40',cpuFlavorId='cpu3g',memoryInGb=21.47483648)
            return value
        api.request=request
        return p,api,src,transport,relay,control

    def run_fixture(self,root,f):
        p,api,src,t,r,c=f
        return prep.execute(p,approval(p),root/'controller',api=api,source=src,transport=t,debs=root/'debs',control=c)

    def test_owned_CPU_pod_deleted_cache_retained_and_no_keys_in_payload(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);f=self.fixture(root);result=self.run_fixture(root,f)
            self.assertEqual(result['status'],'passed');self.assertTrue(result['pod_absent']);self.assertTrue(result['volume_retained'])
            self.assertEqual(result['ongoing_volume_month_usd'],'4.20');self.assertFalse(result['holds_released'])
            pod=next(payload for method,resource,payload in f[1].calls if method=='POST' and resource=='pods')
            self.assertEqual(pod['computeType'],'CPU');self.assertEqual(pod['ports'],[]);self.assertNotIn('KEY',str(pod['env']))
            compile(pod['dockerStartCmd'][-1],'CPU start','exec')
            f[2].close.assert_called();self.assertTrue((root/'controller/owned-cache.json').exists())

    def test_failures_preserve_source_and_reconcile_lost_CPU_create_without_retry(self):
        for outcome in ('transfer','CPU','lost_pod','delete'):
            with self.subTest(outcome=outcome),tempfile.TemporaryDirectory() as root:
                root=Path(root);f=self.fixture(root)
                if outcome=='transfer':f[4].stage.side_effect=TimeoutError('slow')
                if outcome=='CPU':f[3].wait_CPU.side_effect=TimeoutError('bootstrap')
                if outcome=='lost_pod':f[1].lose='pods'
                if outcome=='delete':f[1].refuse_delete=True
                result=self.run_fixture(root,f)
                self.assertEqual(result['status'],'failed');self.assertTrue(result['local_source_preserved']);self.assertTrue(result['volume_retained'])
                self.assertLessEqual(sum(c[:2]==('POST','pods') for c in f[1].calls),1)
                if outcome!='delete':self.assertFalse(f[1].values['pods'])

    def test_missing_quote_closure_base_and_wrong_approval_block_before_provider(self):
        for missing in ('CPU_quote','package_admission','base_metadata','approval'):
            with self.subTest(missing=missing),tempfile.TemporaryDirectory() as root:
                c=config()
                if missing!='approval':c[missing]=None
                p=prep.prepare(c);api=Mock();src=Mock()
                with self.assertRaises(ValueError):prep.execute(p,{} if missing=='approval' else approval(p),Path(root)/'out',api=api,source=src,transport=Mock(),debs=None,control=None)
                self.assertFalse(api.mock_calls);self.assertFalse(src.mock_calls)

    def test_quote_RAM_GB_conversion_and_403_no_fallback(self):
        q=config()['CPU_quote'];api=RestAPI('fixture-token')
        api._send=Mock(return_value={'cpus':[{'id':q['type_id'],'vcpu':{'min':2,'max':32},'ramGbPerVcpu':5,
            'price':{'securePerVcpu':.1},'dataCenters':[{'id':'US-WA-1','availability':'HIGH'}]}]})
        actual=api.cpu_quote(q);self.assertLess(float(actual['ram_gib']),20);self.assertGreater(float(actual['ram_gib']),16)
        api._send=Mock(side_effect=RuntimeError('HTTP403'))
        with self.assertRaises(RuntimeError):api.cpu_quote(q)
        self.assertEqual(api._send.call_count,1)

    def test_cleanup_requires_source_and_checks_other_Pod_cache_attachment(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);f=self.fixture(root);state=self.run_fixture(root,f);p=f[0];api=f[1]
            with self.assertRaises(ValueError):prep.delete_cache(api,f[4],state,approved_plan_sha256=p['plan_sha256'],surviving_source_verified=False)
            api.values['pods']['other']={'id':'other','networkVolumeId':state['volume_id']}
            with self.assertRaises(ValueError):prep.delete_cache(api,f[4],state,approved_plan_sha256=p['plan_sha256'],surviving_source_verified=True)
            api.values['pods'].clear()
            result=prep.delete_cache(api,f[4],state,approved_plan_sha256=p['plan_sha256'],surviving_source_verified=True)
            self.assertTrue(result['volume_absent']);f[4].abort_owned.assert_called_once()

    def test_changed_price_or_unverified_package_bytes_prevent_allocation(self):
        for kind in ('price','package'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as root:
                root=Path(root);f=self.fixture(root)
                if kind=='package':f[2].validate_inputs.side_effect=ValueError('closure changed')
                else:f[1].cpu_quote.return_value=dict(f[0]['plan']['config']['CPU_quote'],hour_usd='0.45')
                if kind=='package':
                    with self.assertRaises(ValueError):self.run_fixture(root,f)
                else:self.assertEqual(self.run_fixture(root,f)['status'],'failed')
                self.assertFalse(any(v[0]=='POST' for v in f[1].calls))

    def test_lost_volume_recovery_adopts_only_exact_shape_without_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);f=self.fixture(root);f[1].lose='networkvolumes';state=self.run_fixture(root,f)
            self.assertEqual(state['pending'],'networkvolumes');self.assertNotIn('volume_id',state)
            prep.recover(f[1],state,root/'controller/state.json',approved_plan_sha256=f[0]['plan_sha256'])
            self.assertEqual(state['volume_id'],'volume-1');self.assertTrue(state['pod_absent'])
            self.assertEqual(sum(v[:2]==('POST','networkvolumes') for v in f[1].calls),1)

    def test_full_controller_state_is_durable_before_source_work(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);f=self.fixture(root)
            def collect(*args,**kwargs):
                initial=read_json(root/'controller/state.json')
                self.assertEqual(initial['prepared'],f[0]);self.assertIsNone(initial['pending'])
                raise TimeoutError('source interruption')
            f[2].collect.side_effect=collect
            state=self.run_fixture(root,f)
            self.assertEqual(state['status'],'failed');self.assertFalse(any(v[0]=='POST' for v in f[1].calls))

    def test_source_crash_recovery_stops_only_hash_bound_App(self):
        for outcome in ('known','changed','lost_id'):
            with self.subTest(outcome=outcome),tempfile.TemporaryDirectory() as root:
                root=Path(root);p=prep.prepare(config());journal=root/'state.json'
                state={'prepared':p,'pending':None};source_path=root/'bundle/source-state.json'
                value={'plan_sha256':p['plan_sha256'],'app_name':'coppertop-preparation-test-export','app_id':'ap-Fixture'}
                if outcome=='changed':value['plan_sha256']='0'*64
                if outcome=='lost_id':value.pop('app_id')
                save(source_path,value);api=Mock()
                with patch('coppertop.runpod_prepare.stop_and_audit_owned_app',return_value={'app_stopped':True,'active_containers':0,'active_sandboxes':0}) as stop:
                    if outcome=='known':
                        prep.recover(api,state,journal,approved_plan_sha256=p['plan_sha256'])
                        stop.assert_called_once_with('ap-Fixture');self.assertTrue(state['source_stopped'])
                    else:
                        with self.assertRaises(RuntimeError):prep.recover(api,state,journal,approved_plan_sha256=p['plan_sha256'])
                        stop.assert_not_called();self.assertIn('source_cleanup_error',state)
                self.assertTrue(state['pod_absent']);api.request.assert_not_called()

    def test_documented_recovery_CLI_needs_no_new_output_or_resource(self):
        with tempfile.TemporaryDirectory() as root:
            p=prep.prepare(config());journal=Path(root)/'state.json'
            save(journal,{'prepared':p,'pending':None,'source_stopped':True})
            api=Mock()
            with patch('sys.argv',['runpod_prepare','--recover',str(journal),'--approved-plan-sha256',p['plan_sha256']]),patch.dict(os.environ,{'RUNPOD_API_KEY':'local-fixture'}),patch.object(prep,'RestAPI',return_value=api):
                prep.main()
            api.request.assert_not_called();self.assertTrue(read_json(journal)['pod_absent'])


class CacheReuseTest(unittest.TestCase):
    def fixture(self,root):
        from test_runpod_cloud import ControllerTests,NOW
        from coppertop import runpod_cloud as cloud
        fixture=ControllerTests().fixture(root);old,cpu,api,e,factory,publisher=fixture
        c=deepcopy(old['plan']['config']);cache={'attempt':'prepared-test','plan_sha256':'e'*64,'volume_id':'cache-volume',
            'volume_name':'coppertop-prepared-test-cache','allocated_gb':60,'datacenter':'US-WA-1','cleanup_owner':'operator',
            'manifest_sha256':c['runtime_export_sha256'],'tar_sha256':'d'*64,'CPU_receipt_sha256':'a'*64,
            'control_sha256':'f'*64,'retention_until_utc':datetime.now(timezone.utc).timestamp()+13*86400}
        c.update(image=contract()['base'],runtime_cache=cache);p=cloud.prepare(c)
        api.values['networkvolumes']['cache-volume']={'id':'cache-volume','name':cache['volume_name'],'size':60,'dataCenterId':'US-WA-1'}
        return cloud,p,cpu,api,e,factory,publisher,NOW

    def test_reuse_allocates_one_GPU_no_volume_and_preserves_input_cache(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);cloud,p,cpu,api,e,factory,publisher,now=self.fixture(root)
            result=cloud.execute(p,p['plan_sha256'],root/'out',api=api,evidence=factory,publisher=publisher,cpu_receipt=cpu,now=now)
            self.assertEqual(result['status'],'passed');self.assertTrue(result['cleanup']['cache_retained'])
            self.assertFalse(any(v[:2]==('POST','networkvolumes') for v in api.calls))
            self.assertFalse(any(v[:2]==('DELETE','networkvolumes/cache-volume') for v in api.calls))
            self.assertFalse(api.values['pods']);self.assertTrue(api.values['networkvolumes'])
            e.require_cache.assert_called_once_with(p,cpu);e.wait_bootstrap.assert_called_once()
            payload=next(v[2] for v in api.calls if v[:2]==('POST','pods'))
            self.assertEqual(payload['env']['COPPERTOP_BOOTSTRAP_MODE'],'rollout')
            self.assertEqual(payload['networkVolumeId'],'cache-volume')
            self.assertEqual(p['plan']['limits']['setup_seconds'],6300);self.assertEqual(p['plan']['limits']['network_volume_gb'],60)

    def test_long_cache_bootstrap_defers_publication_until_fresh_session_without_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);cloud,p,cpu,api,e,factory,publisher,now=self.fixture(root)
            result=cloud.execute(p,p['plan_sha256'],root/'out',api=api,evidence=factory,publisher=None,cpu_receipt=cpu,now=now)
            self.assertEqual(result['status'],'awaiting_publication');self.assertTrue(result['publication_deferred'])
            self.assertTrue(result['pod_absent']);self.assertEqual(result['runtime_status'],'passed');e.publish.assert_not_called()
            self.assertTrue(result['cleanup']['stage_retained']);self.assertTrue(api.values['networkvolumes'])

    def test_changed_ownership_incomplete_inputs_or_other_Pod_block_GPU(self):
        for mutation in ('ownership','incomplete','occupied'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as root:
                root=Path(root);cloud,p,cpu,api,e,factory,publisher,now=self.fixture(root)
                if mutation=='ownership':api.values['networkvolumes']['cache-volume']['name']='unrelated'
                if mutation=='incomplete':e.require_cache.side_effect=ValueError('input multipart incomplete')
                if mutation=='occupied':api.values['pods']['other']={'id':'other','networkVolumeId':'cache-volume'}
                result=cloud.execute(p,p['plan_sha256'],root/'out',api=api,evidence=factory,publisher=None,cpu_receipt=cpu,now=now)
                self.assertEqual(result['status'],'failed');self.assertFalse(any(v[0]=='POST' for v in api.calls))
                self.assertTrue(api.values['networkvolumes'])

    def test_archive_live_evidence_matches_exact_shared_code_without_runtime_claim(self):
        from coppertop.runpod_cloud import live_archive_verified
        self.assertTrue(live_archive_verified())
        proof=read_json(Path(prep.__file__).resolve().parents[1]/'containers/archive-live-admission.json',4096)
        self.assertFalse(proof['Modal_runtime_verified']);self.assertFalse(proof['RunPod_runtime_verified'])


if __name__=='__main__':unittest.main()
