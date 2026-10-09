"""Bounded publication observations; callbacks decide how to persist them."""
from contextlib import contextmanager
import re
import time


def response_details(response):
    """Allowlist SDK response facts; never copy headers, payloads or credentials."""
    result={}
    if not isinstance(response,dict):return result
    failure=response.get('Error',{})
    metadata=response.get('ResponseMetadata',{})
    code=failure.get('Code') if isinstance(failure,dict) else None
    if not isinstance(metadata,dict):metadata={}
    if isinstance(code,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',code):result['error_code']=code
    status=metadata.get('HTTPStatusCode')
    if type(status) is int and 100<=status<=599:result['http_status']=status
    request_id=metadata.get('RequestId')
    if isinstance(request_id,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}',request_id):
        result['request_id']=request_id
    return result


def error_details(error):
    result={'error_type':type(error).__name__}
    current=error
    for _ in range(5):
        response=getattr(current,'response',None)
        if isinstance(response,dict):
            result.update(response_details(response))
            break
        current=getattr(current,'__cause__',None)
        if current is None:break
    return result


@contextmanager
def phase(observe,name):
    started=time.monotonic()
    facts={}
    observe({'stage':name,'status':'started'})
    try:yield facts
    except Exception as error:
        observe({'stage':name,'status':'failed','duration_seconds':time.monotonic()-started,
                 **error_details(error)})
        raise
    else:
        observe({'stage':name,'status':'completed','duration_seconds':time.monotonic()-started,**facts})
