"""Existing-key read-only RunPod control/S3 metadata checks; no writes or keys."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import re
import shlex
from urllib.request import Request,build_opener,HTTPRedirectHandler

SELECTED={'RUNPOD_API_KEY','RUNPOD_S3_ACCESS_KEY','RUNPOD_S3_ACCESS_KEY_ID','RUNPOD_S3_SECRET_ACCESS_KEY'}
ENDPOINT='https://s3api-us-wa-1.runpod.io'
REGION='US-WA-1'


def selected_env(path):
    values={}
    for line in Path(path).read_text().splitlines():
        match=re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$',line)
        if not match or match[1] not in SELECTED:continue
        if match[1] in values:raise ValueError('Duplicate selected configuration')
        parts=shlex.split(match[2],comments=True)
        if len(parts)!=1 or any(c in parts[0] for c in ('$','`','\r','\n')):raise ValueError('Unsupported selected configuration')
        values[match[1]]=parts[0]
    required={'RUNPOD_API_KEY','RUNPOD_S3_SECRET_ACCESS_KEY'}
    if not required<=values.keys() or not (values.get('RUNPOD_S3_ACCESS_KEY_ID') or values.get('RUNPOD_S3_ACCESS_KEY')):
        raise ValueError('Existing RunPod control and S3 authentication required')
    if values.get('RUNPOD_S3_ACCESS_KEY_ID') and values.get('RUNPOD_S3_ACCESS_KEY') and values['RUNPOD_S3_ACCESS_KEY_ID']!=values['RUNPOD_S3_ACCESS_KEY']:
        raise ValueError('Conflicting S3 identity aliases')
    return values


def check(values):
    import boto3
    from botocore.config import Config
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):raise ValueError('Credential redirect rejected')
    result={'checked_utc':datetime.now(timezone.utc).isoformat(),'credential_values_persisted':False,
        'writes_performed':False,'keys_created':False,'pod_created':False,
        's3_endpoint':ENDPOINT,'s3_signing_region':REGION,'bucket_semantics':'network-volume ID',
        'endpoint_selection':'documented US-WA-1 capability probe; no configured volume assumed',
        'volume_lifecycle':'persists Pod termination; explicit network-volume deletion required'}
    try:
        request=Request('https://rest.runpod.io/v1/networkvolumes',headers={
            'Authorization':'Bearer '+values['RUNPOD_API_KEY'],'User-Agent':'coppertop-readonly-preflight/1'})
        with build_opener(NoRedirect()).open(request,timeout=20) as response:raw=response.read(65537)
        if len(raw)>65536:raise ValueError('Control metadata exceeds bound')
        payload=json.loads(raw)
        if not isinstance(payload,list) or len(payload)>100:raise ValueError('Unexpected volume metadata')
        result['control']={'status':'passed','network_volume_count':len(payload),
            'volumes':[{'id':v['id'],'data_center_id':v['dataCenterId'],'size_gb':v['size']} for v in payload]}
    except Exception as error:
        result['control']={'status':'failed','error_type':type(error).__name__}
        if hasattr(error,'code'):result['control']['http_status']=error.code
    try:
        access=values.get('RUNPOD_S3_ACCESS_KEY_ID') or values['RUNPOD_S3_ACCESS_KEY']
        client=boto3.client('s3',endpoint_url=ENDPOINT,region_name=REGION,
            aws_access_key_id=access,aws_secret_access_key=values['RUNPOD_S3_SECRET_ACCESS_KEY'],
            config=Config(signature_version='s3v4',s3={'addressing_style':'path'},
                retries={'max_attempts':0},connect_timeout=10,read_timeout=20,
                user_agent_extra='coppertop-readonly-preflight/1'))
        response=client.list_buckets()
        buckets=response.get('Buckets',[])
        if len(buckets)>100:raise ValueError('Bucket metadata exceeds bound')
        result['s3']={'status':'passed','operation':'ListBuckets','http_status':response['ResponseMetadata']['HTTPStatusCode'],
                      'bucket_count':len(buckets),'bucket_ids':[v['Name'] for v in buckets]}
        client.close()
    except Exception as error:
        result['s3']={'status':'failed','operation':'ListBuckets','error_type':type(error).__name__}
        details=getattr(error,'response',{})
        if details:
            result['s3']['http_status']=details.get('ResponseMetadata',{}).get('HTTPStatusCode')
            code=details.get('Error',{}).get('Code','')
            if re.fullmatch('[A-Za-z0-9_]{1,64}',code):result['s3']['service_error_code']=code
    result['live_object_operations_verified']=False
    result['upload_multipart_and_mount_semantics_verified']=False
    result['status']='passed' if all(result[k]['status']=='passed' for k in ('control','s3')) else 'incomplete'
    if len(json.dumps(result).encode())>32768:raise ValueError('Sanitized metadata output exceeds bound')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();result=check(selected_env(args.env_file))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':main()
