"""Reconcile CallMeIE recording failures from the public webhook's PostgreSQL ledger."""
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

UTC = timezone.utc
CHANNELS = ("mono", "stereo")
RECOVERY_DAYS = 14
ARCHIVE_DAYS = 90
ORIGIN = "callmeie-archive-retry-v1"
BATCH = 8

def utc(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp.replace(tzinfo=stamp.tzinfo or UTC).astimezone(UTC)

def expiry_at(when):
    # S3's day-based expiry rounds up to midnight UTC.
    value = when.astimezone(UTC) + timedelta(days=ARCHIVE_DAYS)
    midnight = value.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight + timedelta(days=value != midnight)).timestamp())

class Later(Exception):
    """An operational category safe to write to the retry ledger."""

class Source:
    def __init__(self):
        import requests
        self.session = requests.Session()
        self.session.headers['Authorization'] = "Bearer " + os.environ['VAPI_API_KEY']

    def get(self, path, stream=False):
        try:
            r = self.session.get("https://api.vapi.ai/call/" + path, timeout=(5,20), stream=stream)
        except Exception:
            raise Later("provider_unreachable") from None
        if r.status_code != 200:
            code = r.status_code
            r.close()
            raise Later("provider_" + str(code))

        return r

    def call(self, call_id):
        with self.get(call_id) as response:
            doc = response.json()
        if not isinstance(doc, dict) or doc.get('id') != call_id:
            raise Later('provider_call_mismatch')
        return doc

    def audio(self, call_id, channel):
        return self.get(call_id + "/" + channel + "-recording", stream=True)

class Archive:
    def __init__(self):
        import boto3
        from botocore.config import Config
        from botocore.exceptions import ClientError
        self.error = ClientError
        self.bucket = os.environ['HETZNER_OBJECT_STORAGE_BUCKET']
        cfg = Config(signature_version='s3v4', s3={'addressing_style':'path'}, connect_timeout=5,
                     read_timeout=15, retries={'total_max_attempts':2})
        self.s3 = boto3.client('s3', endpoint_url=os.environ['HETZNER_OBJECT_STORAGE_ENDPOINT'],
                               region_name=os.getenv('HETZNER_OBJECT_STORAGE_REGION') or 'eu-central-1',
                               aws_access_key_id=os.environ['HETZNER_OBJECT_STORAGE_ACCESS_KEY_ID'],
                               aws_secret_access_key=os.environ['HETZNER_OBJECT_STORAGE_SECRET_ACCESS_KEY'],
                               config=cfg)

    def existing(self, stem):
        from recording_write_guard import channel_exists
        try:
            return channel_exists(self.s3,self.bucket,stem)
        except Exception:
            raise Later('storage_unreachable_or_denied') from None

    def save_new(self, key, payload, size, digest, typ, metadata):
        from recording_write_guard import put_missing
        try:
            params={'Bucket':self.bucket,'Key':key,'Body':payload,'ContentLength':size,
                    'ContentType':typ,'Metadata':metadata}
            created=put_missing(self.s3,self.bucket,key.rsplit('.',1)[0],metadata['channel'],
                                metadata['vapi-call-id'],params)
            if not created:return
        except Exception:
            raise Later('storage_write_unavailable') from None
        try:
            stored=self.s3.head_object(Bucket=self.bucket,Key=key)
            if stored['ContentLength']!=size or stored['Metadata'].get('retry-sha256')!=digest:
                raise Later('storage_verification_failed')
        except Later:
            raise
        except Exception:
            raise Later('storage_verify_unavailable') from None

def recover(call_id, first, now, provider, archive):
    if str(UUID(call_id))!=call_id:
        raise Later('invalid_call_id')
    if now >= first+timedelta(days=RECOVERY_DAYS):
        return 'expired', 'provider_window_elapsed'
    stems={ch:'recordings/'+call_id+('.stereo' if ch=='stereo' else '') for ch in CHANNELS}
    missing=[ch for ch in CHANNELS if not archive.existing(stems[ch])]
    if not missing:
        return 'complete', 'both_present'
    doc=provider.call(call_id)
    origin=min(first,utc(doc.get('endedAt') or doc.get('startedAt') or first))
    if now >= origin+timedelta(days=RECOVERY_DAYS):
        return 'expired', 'provider_window_elapsed'
    ep=expiry_at(origin)
    for ch in missing:
        # The authenticated artifact endpoint is authoritative even while the optional URL fields lag.
        try:
            response=provider.audio(call_id,ch)
        except Later as exc:
            if str(exc) in ('provider_404','provider_409','provider_425'):
                continue
            raise
        with response, tempfile.TemporaryFile() as buf:
            ctype=response.headers.get('Content-Type','audio/wav').split(';')[0].lower()
            if not (ctype.startswith('audio/') or ctype=='application/octet-stream'):
                raise Later('unexpected_audio_type')
            size=0
            digest=hashlib.sha256()
            until=time.monotonic()+45
            for chunk in response.iter_content(128*1024):
                if time.monotonic()>until:raise Later('provider_transfer_timeout')
                if chunk:
                    size+=len(chunk);digest.update(chunk);buf.write(chunk)
            if not size: raise Later('empty_provider_audio')
            length=response.headers.get('Content-Length')
            if length and not response.headers.get('Content-Encoding') and size!=int(length):
                raise Later('incomplete_provider_audio')
            buf.seek(0)
            digest=digest.hexdigest()
            meta={'vapi-call-id':call_id,'vapi-assistant-id':str(doc.get('assistantId') or 'unknown'),
                  'channel':ch, 'mirrored-at':now.isoformat(), 'retry-origin':ORIGIN,
                  'retry-expires-at':str(ep),'retry-sha256':digest}
            ext='.mp3' if ('mp3' in ctype or 'mpeg' in ctype) else '.wav'
            archive.save_new(stems[ch]+ext, buf, size, digest, ctype, meta)
    if all(archive.existing(stems[ch]) for ch in CHANNELS):
        return 'complete', 'both_present'
    return 'pending', 'waiting_for_channel'

DDL="""
CREATE SCHEMA IF NOT EXISTS callmeie_ops;
CREATE TABLE IF NOT EXISTS callmeie_ops.recording_retry_cursor (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), event_id bigint NOT NULL DEFAULT 0);
INSERT INTO callmeie_ops.recording_retry_cursor (singleton) VALUES (true) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS callmeie_ops.recording_retry (
    call_id text PRIMARY KEY, first_failure timestamptz NOT NULL, next_attempt timestamptz NOT NULL,
    attempts integer NOT NULL DEFAULT 0, state text NOT NULL DEFAULT 'pending', reason text NOT NULL DEFAULT 'new');
"""

def seed(conn, now):
    with conn.transaction():
        cursor=conn.execute("SELECT event_id FROM callmeie_ops.recording_retry_cursor WHERE singleton FOR UPDATE").fetchone()[0]
        rows=conn.execute("""SELECT id,call_id,event_type,created_at,detail FROM public.call_events
          WHERE id>%s AND event_type IN ('recording-archive-failed','recording-archived') ORDER BY id LIMIT 1000""",(cursor,)).fetchall()
        for idx,call_id,kind,created,details in rows:
            try:
                if str(UUID(call_id))!=call_id:continue
                first=utc(created)
                if first<now-timedelta(days=ARCHIVE_DAYS):continue
            except (ValueError,TypeError,AttributeError):
                continue
            if kind=='recording-archived':
                try:
                    info=json.loads(details or '{}')
                except (ValueError,TypeError):
                    info={}
                if isinstance(info,dict) and all(info.get(ch) for ch in CHANNELS):continue
            conn.execute("""INSERT INTO callmeie_ops.recording_retry (call_id,first_failure,next_attempt)
                VALUES (%s,%s,%s) ON CONFLICT (call_id) DO NOTHING""",(call_id,first,first+timedelta(minutes=5)))
        if rows:
            conn.execute("UPDATE callmeie_ops.recording_retry_cursor SET event_id=%s WHERE singleton",(rows[-1][0],))
        conn.execute("DELETE FROM callmeie_ops.recording_retry WHERE first_failure<%s",(now-timedelta(days=ARCHIVE_DAYS),))
        conn.execute("""UPDATE callmeie_ops.recording_retry SET state='expired',reason='provider_window_elapsed'
                      WHERE state='pending' AND first_failure<=%s""",(now-timedelta(days=RECOVERY_DAYS),))

def run(conn,provider,archive,now,allow_writes=True):
    if not conn.execute("SELECT pg_try_advisory_lock(488775942088791::bigint)").fetchone()[0]:
        return {'busy':True}
    try:
        conn.execute(DDL)
        seed(conn,now)
        items=conn.execute("""SELECT call_id,first_failure,attempts FROM callmeie_ops.recording_retry
            WHERE state='pending' AND next_attempt<=%s ORDER BY next_attempt,first_failure LIMIT %s""",(now,BATCH)).fetchall()
        processed=0
        until=time.monotonic()+90
        for call_id,first,attempts in (items if allow_writes else []):
            if time.monotonic()>until:break
            processed+=1
            try:
                state,reason=recover(call_id, first, now, provider, archive)
            except Later as exc:
                state,reason='pending', str(exc)
            except Exception:
                state,reason='pending','unexpected_operation_failure'
            delay=min(120,5*2**min(attempts,5))
            conn.execute("""UPDATE callmeie_ops.recording_retry SET state=%s,reason=%s,
                attempts=attempts+1,next_attempt=%s WHERE call_id=%s""",(state,reason,now+timedelta(minutes=delay),call_id))
        stats=dict(conn.execute("SELECT state,count(*) FROM callmeie_ops.recording_retry GROUP BY state").fetchall())
        reasons=dict(conn.execute("""SELECT reason,count(*) FROM callmeie_ops.recording_retry WHERE state<>'complete'
                                      GROUP BY reason""").fetchall())
        due=conn.execute("SELECT min(first_failure) FROM callmeie_ops.recording_retry WHERE state='pending'").fetchone()[0]
        return {'processed':processed,'pending':stats.get('pending',0),'recovered':stats.get('complete',0),
                'expired_unrecovered':stats.get('expired',0),'reasons':reasons,
                'oldest_pending_minutes':int((now-due).total_seconds()//60) if due else 0}
    finally:
        conn.execute("SELECT pg_advisory_unlock(488775942088791::bigint)")

if __name__=='__main__':
    try:
        import psycopg
        with psycopg.connect(os.environ['DATABASE_URL'],autocommit=True,connect_timeout=5) as c:
            allow_writes='--observe' not in sys.argv
            print(json.dumps(run(c,Source() if allow_writes else None,Archive() if allow_writes else None,datetime.now(UTC),allow_writes),sort_keys=True))
    except Exception as exc:
        print(json.dumps({'failed':type(exc).__name__}),file=sys.stderr)
        raise SystemExit(1)
