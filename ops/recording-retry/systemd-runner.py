#!/usr/bin/env python3
"""Launch only against the active CallMeIE receptionist deployment."""
from datetime import datetime,timezone
from pathlib import Path
import json,os,subprocess,sys,tempfile

def call(*args,timeout=20,payload=None):
    return subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout,input=payload)

def status():
    r=call('/usr/bin/docker','ps','--filter','name=^/xml9wji6109b1kergfz05665-','--format','{{.ID}}')
    ids=r.stdout.split()
    if r.returncode or len(ids)!=1:
        return {'phase':'waiting_for_single_active_deployment'}
    cid=ids[0]
    guard=call('/usr/bin/docker','exec','-w','/app',cid,'python','-B','-c',
               "from pathlib import Path;from recording_write_guard import PROTOCOL_VERSION;assert PROTOCOL_VERSION==1 and Path('/app/recording_retry.py').is_file() and 'put_missing(s3, bucket, stem, label, call_id, params)' in Path('/app/server.py').read_text()")
    armed = guard.returncode == 0
    if armed:
        r=call('/usr/bin/docker','exec','-w','/app',cid,'python','-B','/app/recording_retry.py',timeout=185)
    else:
        observer=Path(__file__).with_name('recording_retry.py').read_text()
        r=call('/usr/bin/docker','exec','-i','-w','/app',cid,'python','-B','-','--observe',payload=observer,timeout=45)
    if r.returncode:
        return {'phase':'retry_worker_error'}
    try:
        s=json.loads(r.stdout)
        if s.get('busy') is True:return {'phase':'worker_already_running'}
        expected={'processed','pending','recovered','expired_unrecovered','reasons','oldest_pending_minutes'}
        if set(s)!=expected or not all(isinstance(s[k],int) for k in expected-{'reasons'}):raise ValueError()
        if not isinstance(s['reasons'],dict) or not all(isinstance(k,str) and k.replace('_','').isalnum() and isinstance(v,int) for k,v in s['reasons'].items()):raise ValueError()
        phase='healthy'
        if s['expired_unrecovered'] or s['oldest_pending_minutes']>=120:phase='attention'
        elif s['pending']:phase='retrying'
        return {'phase':phase,'writer_guard_active':armed,**s}
    except (ValueError,TypeError,AttributeError):
        return {'phase':'invalid_worker_status'}

if __name__=='__main__':
    try: out=status()
    except (OSError,subprocess.TimeoutExpired):out={'phase':'worker_unavailable'}
    out['checked_at']=datetime.now(timezone.utc).isoformat();raw=json.dumps(out,sort_keys=True)
    base=Path('/var/lib/callmeie-recording-retry')
    with tempfile.NamedTemporaryFile(dir=base,mode='w',delete=False) as f:
        f.write(raw+'\n');f.flush();os.fsync(f.fileno())
    os.replace(f.name,base/'status.json')
    print(raw)
    sys.exit(1 if out['phase'] in ('attention','retry_worker_error','invalid_worker_status','worker_unavailable') else 0)
