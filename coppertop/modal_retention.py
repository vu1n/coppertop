"""Concrete local Modal cleanup proposal; Cron alone cannot arm finite retention."""
from datetime import datetime, timezone
import math
import re
import subprocess
import sys
import time

from .archive_retention import validate_retention
from .chords import canonical, digest
from .reload_launch import delete_owned_volume, stop_and_audit_owned_app

BACKSTOP_SPEC = {'provider':'modal.Cron', 'sdk':'1.6.1', 'offsets_seconds':[0,300,600],
    'planned_dispatches':3, 'finite_schedule_enforced':False, 'live_ready':False,
    'cpu':0.125, 'memory_mib':128, 'timeout_seconds':360,
    'startup_seconds':300, 'automatic_function_retries':0, 'new_image_builds':False}


def cleanup_plan(*, volume_name, volume_id, app_name, app_id, deadline_utc, policy, image, now=None):
    policy=validate_retention(policy)
    now=time.time() if now is None else now
    if (not re.fullmatch(r'coppertop-mechanism-[0-9a-f]{16}',app_name)
            or volume_name!=app_name+'-outputs' or not re.fullmatch(r'vo-[A-Za-z0-9]+',volume_id)
            or not re.fullmatch(r'ap-[A-Za-z0-9]+',app_id) or not re.fullmatch(r'im-[A-Za-z0-9]+',image)
            or not isinstance(deadline_utc,(int,float)) or not math.isfinite(deadline_utc)
            or not now<deadline_utc<=now+3600+policy['seconds']+60):
        raise ValueError('Bounded deadline and exact owned Modal identities required')
    first=math.ceil(deadline_utc/60)*60
    dates=[datetime.fromtimestamp(first+offset,timezone.utc) for offset in BACKSTOP_SPEC['offsets_seconds']]
    plan={'schema_version':1,'kind':'modal_owned_volume_deadline','spec':dict(BACKSTOP_SPEC),
        'volume_name':volume_name,'volume_id':volume_id,'workload_app_name':app_name,
        'workload_app_id':app_id,'deadline_utc':deadline_utc,'policy':policy,'image':image,
        'scheduled_utc':[date.isoformat() for date in dates],
        'cron':[f'{date.minute} {date.hour} {date.day} {date.month} *' for date in dates],
        'environment':'main', 'arming_blockers':[
            'Cron recurs annually; independent scheduler expiry and terminal storage disposition required',
            'Verify container authority for exact-App stop/audit and exact-Volume deletion',
            'Mount and verify reviewed cleanup module in the cached image without building a new image',
            'Approve cleanup compute, storage ceiling and disposal of unpublished data at expiry']}
    plan['sha256']=digest(canonical(plan))
    plan['deployment_name']='coppertop-retention-'+plan['sha256'][:16]
    return plan


def cleanup_due(plan, index, *, sdk, stop_workload=stop_and_audit_owned_app,
                stop_schedule, now=None):
    """Audit compute before deletion; the last scheduled call stops the scheduler too."""
    now=time.time() if now is None else now
    if not 0<=index<3:
        raise ValueError('Known finite cleanup invocation required')
    scheduled=datetime.fromisoformat(plan['scheduled_utc'][index]).timestamp()
    if now<scheduled:
        raise ValueError('Early cleanup forbidden')
    try:
        audit=stop_workload(plan['workload_app_id'])
        if (audit.get('app_stopped') is not True or audit.get('active_containers')!=0
                or audit.get('active_sandboxes')!=0):
            raise RuntimeError('Owned compute still active; retain stage')
        try:
            delete_owned_volume(sdk,plan['volume_name'],plan['volume_id'])
        except sdk.exception.NotFoundError:
            # Successful controller cleanup can precede this independently armed job.
            pass
        result={'status':'deleted_or_already_absent','volume_id':plan['volume_id'],'plan_sha256':plan['sha256']}
    except BaseException:
        if index==2:
            stop_schedule()
        raise
    stop_schedule()
    return result


def define_backstop(sdk, plan, image, *, runner=subprocess.run, stop_schedule=None):
    """Only define functions; caller must separately authorize deployment."""
    if sdk.__version__!='1.6.1':
        raise ValueError('Modal1.6.1 required')
    app=sdk.App(plan['deployment_name'],secrets=[],volumes={},include_source=False)
    def stop_self():
        # Pinned SDK supplies the actual executing App identity. Never resolve an
        # untrusted same-name deployment to stop the independent scheduler.
        from modal._runtime.container_io_manager import _ContainerIOManager
        context=_ContainerIOManager._singleton
        if context is None or not re.fullmatch(r'ap-[A-Za-z0-9]+',context.app_id):
            raise ValueError('Executing scheduler identity unavailable')
        process=runner([sys.executable,'-m','modal','app','stop',context.app_id,'--env','main','--yes'],
            capture_output=True,timeout=60)
        if process.returncode!=0:
            raise RuntimeError('Scheduler stop not acknowledged')
    def define(index):
        @app.function(image=image,serialized=True,name='expire-owned-stage-'+str(index),
            schedule=sdk.Cron(plan['cron'][index]),cpu=(0.125,0.125),memory=(128,128),
            gpu=None,secrets=[],volumes={},retries=0,min_containers=0,max_containers=1,
            buffer_containers=0,single_use_containers=True,scaledown_window=2,
            timeout=360,startup_timeout=300)
        def expire():
            return cleanup_due(plan,index,sdk=sdk,
                stop_workload=lambda identity:stop_and_audit_owned_app(identity,runner=runner),
                stop_schedule=stop_schedule or stop_self)
        return expire
    functions=[define(index) for index in range(3)]
    return app,functions
