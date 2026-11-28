"""Exercise ledger restart, partial-channel recovery, and concurrent non-conditional S3 writes."""
import io
import json
import os
import threading
import time
import unittest
import uuid
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from botocore.exceptions import ClientError
import recording_retry as retry
from recording_write_guard import put_missing

class Audio:
    def __init__(self,payload):
        self.payload=payload
        self.headers={'Content-Type':'audio/wav','Content-Length':str(len(payload))}
    def __enter__(self):return self
    def __exit__(self,*exc):pass
    def iter_content(self,size):yield self.payload

class Provider:
    def __init__(self,ended):
        self.ended=ended; self.fetched=[]
    def call(self,call_id):
        return {'id':call_id,'endedAt':self.ended.isoformat(),'assistantId':'synthetic-test'}
    def audio(self,call_id,channel):
        self.fetched.append((call_id,channel))
        return Audio(channel.encode()+b'-synthetic-test-bytes')

class Store:
    # Garage accepts unconditional overwrites. count every underlying mutation.
    def __init__(self):self.items={};self.writes=[];self.online=True
    def head_object(self,Bucket,Key):
        if not self.online:raise ConnectionError('fixture storage offline')
        try:return self.items[Key]
        except KeyError: raise ClientError({'ResponseMetadata':{'HTTPStatusCode':404},'Error':{'Code':'NoSuchKey'}},'HeadObject')
    def put_object(self,**params):
        time.sleep(.02)
        if not self.online: raise ConnectionError('fixture storage offline')
        content=params['Body'].read() if hasattr(params['Body'],'read') else params['Body']
        self.items[params['Key']]={'Metadata':params.get('Metadata',{}),'ContentLength':len(content),'Body':content}
        self.writes.append(params['Key'])

def recording(store):
    result=object.__new__(retry.Archive);result.s3=store;result.bucket='isolated'
    return result

class Regression(unittest.TestCase):
    def setUp(self):
        fixture=os.getenv('RETRY_TEST_DATABASE_URL','')
        if fixture and not urlsplit(fixture).path.startswith('/retry_fixture_'):
            self.fail('Expected an isolated retry fixture URL')
        scope=patch.dict(os.environ,{'DATABASE_URL':fixture})
        scope.start()
        self.addCleanup(scope.stop)

    def test_unconditional_backend_race(self):
        store=Store();call=str(uuid.uuid4()); stem='recordings/'+call;key=stem+'.wav';barrier=threading.Barrier(2)
        def write():
            barrier.wait()
            return put_missing(store,'isolated',stem,'mono',call,{'Bucket':'isolated','Key':key,'Body':b'test'})
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(lambda _:write(),range(2)))
        self.assertEqual(sorted(result),[False,True])
        self.assertEqual(store.writes,[key])

    def test_live_mirror_skips_existing_and_repeat(self):
        # Import-time legacy schema initialization is separate from this writer.
        # The canonical writer below still takes its lock against the disposable real PostgreSQL database.
        with patch.dict(os.environ,{'DATABASE_URL':'','DB_PATH':'/tmp/callmeie-retry-mirror-fixture.sqlite3'}):
            import server
        store=Store();call=str(uuid.uuid4());stem='recordings/'+call
        store.items[stem+'.wav']={'Metadata':{'original-expiry':'fixed'},'ContentLength':8,'Body':b'original'}
        class HttpAudio:
            headers={'content-type':'audio/wav'}
            content=b'stereo fixture'
            def raise_for_status(self):pass
            def close(self):pass
            def iter_content(self,size):yield self.content
        with patch.object(server,'_hetzner_s3',return_value=store), patch.object(server,'log_event') as event, patch('requests.get',return_value=HttpAudio()) as fetched, patch.dict(os.environ,{'HETZNER_OBJECT_STORAGE_BUCKET':'isolated','VAPI_API_KEY':'test-only'}):
            server._mirror_recording_to_hetzner(call,'synthetic','mono-exists','stereo-exists')
            server._mirror_recording_to_hetzner(call,'synthetic','mono-exists','stereo-exists')
        self.assertEqual(store.writes,[stem+'.stereo.wav'])
        self.assertEqual(store.items[stem+'.wav']['Body'],b'original')
        self.assertEqual(fetched.call_count,1)
        self.assertEqual(event.call_count,1)

    @unittest.skipUnless(urlsplit(os.getenv('RETRY_TEST_DATABASE_URL','')).path.startswith('/retry_fixture_'), 'requires an isolated PostgreSQL retry fixture')
    def test_durable_outage_and_partial_recovery(self):
        import psycopg
        now=datetime.now(timezone.utc)
        call,partial,old=(str(uuid.uuid4()) for _ in range(3))
        store=Store(); arc=recording(store);provider=Provider(now-timedelta(minutes=20))
        mono='recordings/'+partial+'.wav'
        store.items[mono]={'Metadata':{'original-expiry':'fixed'},'ContentLength':8,'Body':b'original'}
        original=dict(store.items[mono])
        with psycopg.connect(os.environ['DATABASE_URL'],autocommit=True) as c:
            self.assertTrue(c.execute("select current_database()").fetchone()[0].startswith('retry_fixture_'))
            c.execute("""CREATE TABLE public.call_events(id bigint generated by default as identity primary key,
                   call_id text,event_type text,created_at text,detail text)""")
            for cid,t,line in [(call,now-timedelta(minutes=10),'recording-archive-failed'),(partial,now-timedelta(minutes=10),'recording-archived'),(old,now-timedelta(days=15),'recording-archive-failed'),(partial,now-timedelta(minutes=9),'recording-archive-failed')]:
                c.execute("INSERT INTO public.call_events(call_id,created_at,event_type,detail) VALUES(%s,%s,%s,%s)",(cid,t.isoformat(),line,json.dumps({'mono':True} if line=='recording-archived' else {})))
            store.online=False
            r=retry.run(c,provider,arc,now)
            self.assertEqual((r['pending'],r['expired_unrecovered'],r['processed']),(2,1,2))
            self.assertEqual(provider.fetched,[])
        # Fresh process/database connection: persist the failed offset and retry queue.
        with psycopg.connect(os.environ['DATABASE_URL'],autocommit=True) as c:
            store.online=True
            r=retry.run(c,provider,arc,now+timedelta(minutes=6))
            self.assertEqual((r['pending'],r['expired_unrecovered'],r['recovered']),(0,1,2))
            self.assertEqual(sorted(provider.fetched),sorted([(call,'mono'),(call,'stereo'),(partial,'stereo')]))
            self.assertEqual(store.items[mono],original)
            self.assertEqual(store.items['recordings/'+partial+'.stereo.wav']['Metadata']['retry-expires-at'],str(retry.expiry_at(provider.ended)))
            writes=store.writes[:]
            r=retry.run(c,provider,arc,now+timedelta(minutes=12))
            self.assertEqual((r['processed'],store.writes),(0,writes))
            self.assertEqual(len(writes),3)

if __name__=='__main__':unittest.main()
