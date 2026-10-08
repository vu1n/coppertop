"""Read-only result recovery for the single already stopped inventory App."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from tools.run_runpod_inventory import encoded,save


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--logs-only',action='store_true');args=parser.parse_args()
    state=json.loads((args.output/'state.json').read_bytes());app=state['app_id']
    if not re.fullmatch('ap-[A-Za-z0-9]+',app):raise ValueError('Owned App ID required')
    result={'app_id':app,'new_tasks_launched':False}
    process=subprocess.run([sys.executable,'-m','modal','app','logs',app,'--env','main',
        '--tail','100','--show-function-call-id'],capture_output=True,timeout=60)
    raw=process.stdout+process.stderr
    if len(raw)>262144:raise ValueError('Existing log metadata exceeds bound')
    text=raw.decode(errors='replace')
    calls=sorted(set(re.findall(r'\bfc-[A-Za-z0-9]+\b',text)))
    result['log_command_exit_code']=process.returncode;result['function_call_ids']=calls
    result['remote_error_types']=sorted(set(re.findall(r'\b([A-Za-z][A-Za-z0-9_]*(?:Error|Exception)):',text)))
    result['remote_python_frames']=[{'path':match[1],'line':int(match[2]),'function':match[3]}
        for match in re.finditer(r'File "(/[A-Za-z0-9_./-]+\.py)", line (\d+), in ([A-Za-z0-9_<>]+)',text)][:32]
    # No raw error messages, HTTP URLs, log content or credentials are persisted.
    if len(calls)==1 and not args.logs_only:
        try:
            import modal
            receipt=modal.FunctionCall.from_id(calls[0]).get(timeout=60)
            if isinstance(receipt,bytes):receipt=json.loads(receipt)
            if receipt['plan_sha256']!=state['plan_sha256'] or len(encoded(receipt))>262144:
                raise ValueError('Recovered result scope/size differs')
            save(args.output/'inventory.json',receipt)
            result['result_recovered']=True;result['inventory_bytes']=len(encoded(receipt))
        except Exception as error:
            result['result_recovered']=False;result['error_type']=type(error).__name__
            host=getattr(error,'host',None)
            if isinstance(host,str) and re.fullmatch('[A-Za-z0-9.-]+',host):result['failed_dns_host']=host
    else:result['result_recovered']=False
    save(args.output/'recovery.json',result);print(json.dumps(result,sort_keys=True))


if __name__=='__main__':main()
