"""Select the newest archive objects across storage pages before limiting them."""
import heapq


def latest_recording_objects(storage, bucket, limit):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('Recording limit must be between 1 and 500')

    def candidates():
        paginator = storage.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket, Prefix='recordings/',
                                       PaginationConfig={'PageSize': 1000}):
            for item in page.get('Contents', []):
                key = item.get('Key')
                if isinstance(key, str) and key.startswith('recordings/') and not key.endswith('/'):
                    yield item

    def order(item):
        modified = item.get('LastModified')
        return (modified.timestamp() if modified is not None else float('-inf'), item['Key'])

    # Storage is ordered by key, not modification time. Retain at most limit
    # objects in memory while visiting every page in the recordings prefix.
    return heapq.nlargest(limit, candidates(), key=order)
