"""Local scheduler definitions and fake owned-resource cleanup only."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from coppertop import modal_retention as m
from test_record_archive import POLICY


class Missing(Exception): pass


def plan():
    return m.cleanup_plan(volume_name='coppertop-mechanism-abcdef0123456789-outputs',volume_id='vo-owned',
        app_name='coppertop-mechanism-abcdef0123456789',app_id='ap-workload',deadline_utc=2000,
        policy=POLICY,image='im-existing',now=1000)


class App:
    def __init__(self,name,**kwargs): self.name=name;self.app_id=None;self.definitions=[]
    def function(self,**kwargs):
        def decorate(function):
            function.hydrate=Mock();self.definitions.append(kwargs);return function
        return decorate


class RetentionTests(unittest.TestCase):
    def test_plan_is_pure_exact_finite_and_rejects_unowned_targets(self):
        value=plan();self.assertEqual(len(value['cron']),3)
        self.assertEqual(value,plan());self.assertEqual(value['spec']['planned_dispatches'],3)
        self.assertFalse(value['spec']['finite_schedule_enforced']);self.assertFalse(value['spec']['live_ready'])
        self.assertTrue(value['arming_blockers'])
        self.assertEqual(value['scheduled_utc'],['1970-01-01T00:34:00+00:00',
            '1970-01-01T00:39:00+00:00','1970-01-01T00:44:00+00:00'])
        for field,bad in [('volume_name','unrelated'),('app_id','unrelated'),('volume_id','unrelated'),('deadline_utc',float('nan'))]:
            ownership={key:value[key] for key in ('volume_name','volume_id','deadline_utc','policy','image')}
            ownership.update(app_name=value['workload_app_name'],app_id=value['workload_app_id'],now=1000)
            ownership[field]=bad
            with self.assertRaises(ValueError):m.cleanup_plan(**ownership)

    def test_defining_schedule_never_deploys_or_mounts_data(self):
        sdk=SimpleNamespace(__version__='1.6.1',App=App,Cron=lambda value:value)
        app,functions=m.define_backstop(sdk,plan(),object())
        self.assertIsNone(app.app_id);self.assertEqual(len(functions),3)
        self.assertTrue(all(row['gpu'] is None and row['volumes']=={} and row['secrets']==[] and row['retries']==0 for row in app.definitions))
        self.assertEqual([row['schedule'] for row in app.definitions],plan()['cron'])

    def test_due_orders_zero_active_audit_delete_then_scheduler_stop(self):
        events=[];sdk=SimpleNamespace(exception=SimpleNamespace(NotFoundError=Missing))
        def audit(identity):
            self.assertEqual(identity,'ap-workload');events.append('audit')
            return {'app_stopped':True,'active_containers':0,'active_sandboxes':0}
        with patch.object(m,'delete_owned_volume',side_effect=lambda *args:events.append('delete')):
            result=m.cleanup_due(plan(),0,sdk=sdk,stop_workload=audit,
                stop_schedule=lambda:events.append('stop-scheduler'),now=2040)
        self.assertEqual(events,['audit','delete','stop-scheduler']);self.assertEqual(result['status'],'deleted_or_already_absent')

    def test_early_active_changed_identity_and_absent_cases(self):
        sdk=SimpleNamespace(exception=SimpleNamespace(NotFoundError=Missing))
        stopped=Mock();audit=Mock(return_value={'app_stopped':True,'active_containers':0,'active_sandboxes':0})
        with patch.object(m,'delete_owned_volume') as delete:
            with self.assertRaises(ValueError):m.cleanup_due(plan(),0,sdk=sdk,stop_workload=audit,stop_schedule=stopped,now=1000)
            audit.assert_not_called();delete.assert_not_called()
            with self.assertRaises(RuntimeError):
                m.cleanup_due(plan(),0,sdk=sdk,stop_workload=lambda identity:{'app_stopped':True,'active_containers':1,'active_sandboxes':0},stop_schedule=stopped,now=2040)
            delete.assert_not_called();stopped.assert_not_called()
        with patch.object(m,'delete_owned_volume',side_effect=ValueError('Owned Volume identity changed')):
            with self.assertRaises(ValueError):m.cleanup_due(plan(),2,sdk=sdk,stop_workload=audit,stop_schedule=stopped,now=2640)
        stopped.assert_called_once()
        with patch.object(m,'delete_owned_volume',side_effect=Missing):
            self.assertEqual(m.cleanup_due(plan(),0,sdk=sdk,stop_workload=audit,stop_schedule=lambda:None,now=2040)['status'],'deleted_or_already_absent')

    def test_actual_sdk_definition_offline(self):
        try: import modal
        except ImportError:self.skipTest('Existing optional Modal SDK unavailable; run in controller env')
        app,functions=m.define_backstop(modal,plan(),modal.Image.from_id('im-existing'))
        self.assertIsNone(app.app_id);self.assertEqual(len(functions),3)
