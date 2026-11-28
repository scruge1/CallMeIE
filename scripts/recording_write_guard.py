"""Serialize creation of canonical recording channels across the app and retry worker."""
import hashlib
import io
import os
import tempfile
import threading
from contextlib import contextmanager

PROTOCOL_VERSION = 1
MULTIPART_THRESHOLD = 32 * 1024 * 1024
PART_BYTES = 8 * 1024 * 1024
_LOCAL_LOCK = threading.Lock()

def channel_exists(s3, bucket, stem):
    """Check both formats used by the app; distinguish absence from a failed HEAD."""
    from botocore.exceptions import ClientError
    for ext in ('.wav', '.mp3'):
        try:
            header = s3.head_object(Bucket=bucket, Key=stem + ext)
        except ClientError as exc:
            if exc.response.get('ResponseMetadata', {}).get('HTTPStatusCode') == 404:
                continue
            raise
        if header['ContentLength'] <= 0:
            raise ValueError('Existing recording channel is empty')
        return True
    return False

@contextmanager
def channel_lock(call_id, channel):
    token = hashlib.sha256(('callmeie-recording:' + call_id + ':' + channel).encode()).digest()
    url = os.environ.get('DATABASE_URL', '')
    if url.startswith(('postgres://', 'postgresql://')):
        import psycopg
        with psycopg.connect(url, connect_timeout=5) as conn:
            conn.execute("SET LOCAL lock_timeout = '5s'")
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (int.from_bytes(token[:8], 'big', signed=True),))
            yield
        return
    # SQLite development deployments have no shared database coordinator. Writers
    # to a single SQLite workspace share one lock file (with independent byte ranges).
    import fcntl
    lock_path = os.path.join(tempfile.gettempdir(), 'callmeie-recording-channel.lock')
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with _LOCAL_LOCK, os.fdopen(fd, 'r+b') as lock:
        position = int.from_bytes(token[:4], 'big')
        fcntl.lockf(lock, fcntl.LOCK_EX, 1, position)
        try:
            yield
        finally:
            fcntl.lockf(lock, fcntl.LOCK_UN, 1, position)

@contextmanager
def downloaded_recording(response):
    """Finish the provider download before taking a lock; large streams spill to an anonymous temp file."""
    try:
        with tempfile.SpooledTemporaryFile(max_size=PART_BYTES) as body:
            for chunk in response.iter_content(128*1024):
                if chunk:
                    body.write(chunk)
            size = body.tell()
            advertised = response.headers.get('Content-Length')
            if not size or (advertised and not response.headers.get('Content-Encoding') and size != int(advertised)):
                raise ValueError('Incomplete recording download')
            body.seek(0)
            yield body
    finally:
        response.close()

def put_missing(s3, bucket, stem, channel, call_id, params):
    body = params['Body']
    if isinstance(body, (bytes,bytearray)):
        body = io.BytesIO(body)
    body.seek(0, os.SEEK_END)
    size = body.tell()
    body.seek(0)
    with channel_lock(call_id, channel):
        if channel_exists(s3, bucket, stem):
            return False
        if size < MULTIPART_THRESHOLD:
            s3.put_object(**dict(params, Body=body, ContentLength=size))
        else:
            from boto3.s3.transfer import TransferConfig
            config = TransferConfig(multipart_threshold=MULTIPART_THRESHOLD, multipart_chunksize=PART_BYTES,
                                    max_concurrency=2, use_threads=True)
            extras = {k:params[k] for k in ('ContentType','Metadata') if k in params}
            s3.upload_fileobj(body, bucket, params['Key'], ExtraArgs=extras, Config=config)
        return True
