# ratelimit

A token bucket for limiting how often a caller may act.

```python
from app.ratelimit import TokenBucket

bucket = TokenBucket(capacity=10, refill_per_second=2)
if bucket.allow():
    handle_request()
```

A burst of up to `capacity` calls is allowed at once; after that, calls are
allowed at `refill_per_second` on average.
