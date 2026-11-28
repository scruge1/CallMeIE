"""Select the newest archive objects across storage pages before limiting them."""
import hashlib
import heapq
import json
import os
from datetime import datetime



def _migration_times(storage, bucket):
    key = os.environ.get('CALLMEIE_RECORDING_TIMES_KEY')
    if not key:
        return {}
    try:
        body = storage.get_object(Bucket=bucket, Key=key)['Body']
    except storage.exceptions.NoSuchKey:
        return {}
    with body:
        return json.load(body)


def latest_recording_objects(storage, bucket, limit):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('Recording limit must be between 1 and 500')

    original_times = _migration_times(storage, bucket)

    def candidates():
        paginator = storage.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket, Prefix='recordings/',
                                       PaginationConfig={'PageSize': 1000}):
            for item in page.get('Contents', []):
                key = item.get('Key')
                if isinstance(key, str) and key.startswith('recordings/') and not key.endswith('/'):
                    if original_times:
                        etag = item.get('ETag', '').strip('"')
                        fingerprint = hashlib.sha256((key + '\0' + etag).encode()).hexdigest()
                        original = original_times.get(fingerprint)
                        if original is not None:
                            item = dict(item, LastModified=datetime.fromisoformat(original))
                    yield item

    def order(item):
        modified = item.get('LastModified')
        return (modified.timestamp() if modified is not None else float('-inf'), item['Key'])

    # Storage is ordered by key, not modification time. Retain at most limit
    # objects in memory while visiting every page in the recordings prefix.
    return heapq.nlargest(limit, candidates(), key=order)
