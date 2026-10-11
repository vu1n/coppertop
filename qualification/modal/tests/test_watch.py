"""Exercises the actual observer loop with a local backend, never the SDK."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'executor'))
import common, observer, modal_backend


class WatchTests(unittest.TestCase):
    def scenario(self, mode):
        async def run(root):
            owned='ap-'+'A'*22;events=[];alive=[True];step=[0]
            record={'plan_sha256':'a'*64,'run_id':'ct-qual-'+'a'*12,'app_id':owned,
                    **common.IDENTITY,'generation':1,'stage':'cpu','command':'running',
                    'execution_deadline':time.time()+360,'outer_deadline':time.time()+1140,
                    'sandbox_id':'sb-'+'C'*22,'controller_pgid':os.getpgrp()+1,
                    'controller_pid':123,'controller_start_ticks':'1','controller_pulse':time.time()}
            common.durable(root/'ownership.json',record)
            class Backend:
                async def open(self):events.append('open');return common.IDENTITY
                async def close(self):events.append('close')
                async def heartbeat(self,app):
                    self.assert_owned(app)
                    if mode=='serial':
                        if step[0]==0:
                            record.update(generation=2,command='cpu_cleanup',cleanup_deadline=time.time()+120);step[0]=1
                        elif step[0]==1 and (root/'cpu-cleanup.json').exists():
                            record.update(generation=3,stage='gpu',command='running',
                                          execution_deadline=time.time()+540,sandbox_id='sb-'+'G'*22)
                            record.pop('cleanup_deadline',None);step[0]=2
                        elif step[0]==2 and (root/'ack-3.json').exists():
                            record.update(generation=4,command='final_cleanup',cleanup_deadline=time.time()+120);step[0]=3
                    elif mode=='lost-controller':alive[0]=False
                    elif mode=='lost-pulse':record['controller_pulse']=time.time()-6
                    elif mode=='malformed-later':record['app_id']='ap-'+'F'*22;record['workspace']='foreign'
                    common.durable(root/'ownership.json',record)
                def assert_owned(self,app):
                    if app!=owned:raise AssertionError('Attempt to act on an unowned App')
                async def stop_sandbox(self,sid):events.append(('stop-sandbox',sid))
                async def stop_app(self,app):self.assert_owned(app);events.append(('stop-app',app))
                async def audit(self,app,stopped):
                    self.assert_owned(app);events.append(('audit',stopped))
                    return {'confirmed':True,'workspace':common.IDENTITY['workspace'],'environment':'main',
                            'app_id':app,'app_stopped':stopped,'active_containers':0,'active_sandboxes':0}
            original_sleep=asyncio.sleep
            async def fast_pause(_):await original_sleep(.001)
            with patch.object(modal_backend,'ModalBackend',Backend),\
                 patch.object(observer,'verify_bundle',return_value=({},{})),\
                 patch.object(observer,'controller_alive',side_effect=lambda _:alive[0]),\
                 patch.object(observer.asyncio,'sleep',fast_pause):
                code=await asyncio.wait_for(observer.watch(root,root,'a'*64,record['run_id']),3)
            return code,events,{p.name:common.read_json(p) for p in root.glob('*.json')}
        with tempfile.TemporaryDirectory() as temp:return asyncio.run(run(Path(temp)))

    def test_real_observer_serial_cleanup_and_gpu_ack(self):
        code,events,files=self.scenario('serial')
        self.assertEqual(code,0);self.assertTrue(files['cpu-cleanup.json']['confirmed'])
        self.assertEqual(files['ack-3.json']['stage'],'gpu')
        self.assertTrue(files['cleanup.json']['app_stopped'])
        self.assertEqual(events.count(('stop-app','ap-'+'A'*22)),1)

    def test_real_observer_cleans_on_controller_loss_pulse_loss_and_malformed_record(self):
        for mode in ('lost-controller','lost-pulse','malformed-later'):
            with self.subTest(mode=mode):
                _,events,files=self.scenario(mode)
                self.assertIn(('stop-app','ap-'+'A'*22),events)
                self.assertTrue(files['cleanup.json']['confirmed'])
                self.assertEqual(files['cleanup.json']['app_id'],'ap-'+'A'*22)

    def test_slow_observer_auth_reads_current_pulse_and_arms_before_creation(self):
        async def run(root):
            now=[1000.];events=[];owned='ap-'+'A'*22
            record={'plan_sha256':'a'*64,'run_id':'ct-qual-'+'a'*12,'app_id':owned,
                    **common.IDENTITY,'generation':1,'stage':'cpu','command':'running',
                    'execution_deadline':1286.,'outer_deadline':2066.,'sandbox_id':None,
                    'controller_pgid':os.getpgrp()+1,'controller_pid':123,
                    'controller_start_ticks':'1','controller_pulse':now[0]}
            common.durable(root/'ownership.json',record)
            class Backend:
                async def open(self):
                    # Controller's independent pulse runs while observer auth waits.
                    now[0]+=6;record['controller_pulse']=now[0]
                    common.durable(root/'ownership.json',record)
                    await asyncio.sleep(.001);return common.IDENTITY
                async def heartbeat(self,app):
                    self.assert_ack()
                    events.append('armed-and-live-during-startup')
                    record.update(command='final_cleanup',generation=4,cleanup_deadline=now[0]+120)
                    common.durable(root/'ownership.json',record)
                def assert_ack(self):
                    if not (root/'ack-1.json').exists():raise AssertionError('observer was not armed')
                async def stop_app(self,app):events.append('explicit-final-stop')
                async def audit(self,app,stopped):
                    return {'confirmed':True,'workspace':common.IDENTITY['workspace'],'environment':'main',
                            'app_id':app,'app_stopped':stopped,'active_containers':0,'active_sandboxes':0}
                async def close(self):pass
            with patch.object(modal_backend,'ModalBackend',Backend),\
                 patch.object(observer,'verify_bundle',return_value=({},{})),\
                 patch.object(observer,'controller_alive',return_value=True),\
                 patch.object(observer.time,'time',side_effect=lambda:now[0]):
                code=await asyncio.wait_for(observer.watch(root,root,'a'*64,record['run_id']),2)
            return code,events
        with tempfile.TemporaryDirectory() as temp:code,events=asyncio.run(run(Path(temp)))
        self.assertEqual(code,0)
        self.assertEqual(events,['armed-and-live-during-startup','explicit-final-stop'])

if __name__=='__main__':unittest.main()
