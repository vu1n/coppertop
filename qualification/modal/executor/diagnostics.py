"""Bounded full stack/chain structure without exception text or auth data."""
import json
import traceback


def error_evidence(error, secrets=()):
    """No messages, source lines, locals, arbitrary URL text, or response bodies.

    Standard numeric error codes retain denial/OS/TLS evidence. The separately
    validated official endpoint is the only network hostname persisted.
    """
    chain=[];seen=set();current=error;truncated=False
    try:
        while current is not None and id(current) not in seen and len(chain)<16:
            seen.add(id(current));frames=[]
            for frame,line in traceback.walk_tb(current.__traceback__):
                if len(frames)>=128:truncated=True;break
                frames.append({'file':frame.f_code.co_filename[:1024],
                               'function':frame.f_code.co_name[:128],'line':line})
            entry={'type':type(current).__name__[:128],'frames':frames}
            for name in ('error_code','errno','verify_code'):
                value=getattr(current,name,None)
                if type(value) is int and -2147483648<=value<=2147483647:
                    entry[name]=value
            status=getattr(current,'status',None)
            if status is not None and type(status).__module__=='grpclib.const':
                entry['grpc_status']=status.name[:64]
            following=current.__cause__
            relation='cause'
            if following is None and not current.__suppress_context__:
                following=current.__context__;relation='context'
            entry['next_relation']=relation if following is not None else None
            chain.append(entry);current=following
        if current is not None:truncated=True
        result={'type':type(error).__name__[:128],'exception_chain':chain,
                'messages_omitted':True,'truncated':truncated}
        while len(json.dumps(result,separators=(',',':')).encode())>65536:
            result['truncated']=True
            if chain[-1]['frames']:chain[-1]['frames'].pop()
            else:chain.pop()
        return result
    except BaseException:
        return {'type':type(error).__name__[:128],'exception_chain':[],
                'messages_omitted':True,'truncated':True}
