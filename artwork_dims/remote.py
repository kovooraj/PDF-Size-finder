"""A seekable, read-only file object backed by HTTP Range requests, so a huge
PDF in storage can be opened without downloading it. Reads are fetched in
blocks and cached (LRU), with read-ahead for sequential access."""
from __future__ import annotations

import io
import re
import urllib.request
from collections import OrderedDict

BLOCK = 1 << 20          # 1 MiB
MAX_CACHE_BLOCKS = 96    # ~96 MiB ceiling
READ_AHEAD = 3           # extra blocks fetched on a miss


class RangeNotSupported(Exception):
    pass


class HTTPRangeFile(io.RawIOBase):
    def __init__(self, url: str, headers: dict | None = None, timeout: int = 60):
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout
        self.pos = 0
        self.cache: OrderedDict[int, bytes] = OrderedDict()
        self.bytes_fetched = 0
        self.requests = 0
        self.size = self._probe_size()

    # -- http
    def _get(self, start: int, end: int) -> bytes:
        req = urllib.request.Request(self.url, headers={**self.headers, "Range": f"bytes={start}-{end}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            if r.status != 206:
                raise RangeNotSupported(f"server answered {r.status} to a range request")
            data = r.read()
        self.requests += 1
        self.bytes_fetched += len(data)
        return data

    def _probe_size(self) -> int:
        req = urllib.request.Request(self.url, headers={**self.headers, "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            cr = r.headers.get("Content-Range", "")
            r.read()
            if r.status != 206:
                raise RangeNotSupported(f"server answered {r.status} to a range request")
        m = re.search(r"/(\d+)$", cr)
        if not m:
            raise RangeNotSupported("no Content-Range size")
        self.requests += 1
        return int(m.group(1))

    # -- blocks
    def _fetch_blocks(self, first: int, last: int):
        """Fetch blocks first..last (inclusive) in one request, skipping cached edges."""
        while first <= last and first in self.cache:
            self.cache.move_to_end(first)
            first += 1
        while last >= first and last in self.cache:
            self.cache.move_to_end(last)
            last -= 1
        if first > last:
            return
        start = first * BLOCK
        end = min(self.size, (last + 1) * BLOCK) - 1
        data = self._get(start, end)
        for b in range(first, last + 1):
            off = (b - first) * BLOCK
            self.cache[b] = data[off:off + BLOCK]
            self.cache.move_to_end(b)
        while len(self.cache) > MAX_CACHE_BLOCKS:
            self.cache.popitem(last=False)

    # -- io API
    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        elif whence == io.SEEK_END:
            self.pos = self.size + offset
        self.pos = max(0, self.pos)
        return self.pos

    def readinto(self, b):
        n = len(b)
        if self.pos >= self.size or n == 0:
            return 0
        n = min(n, self.size - self.pos)
        first, last = self.pos // BLOCK, (self.pos + n - 1) // BLOCK
        last_block = (self.size - 1) // BLOCK
        missing = [k for k in range(first, last + 1) if k not in self.cache]
        if missing:
            self._fetch_blocks(missing[0], min(last_block, max(missing[-1], missing[0] + READ_AHEAD)))
        out = bytearray()
        for k in range(first, last + 1):
            if k not in self.cache:  # evicted by a very large read
                self._fetch_blocks(k, k)
            blk = self.cache[k]
            self.cache.move_to_end(k)
            lo = self.pos - k * BLOCK if k == first else 0
            hi = (self.pos + n) - k * BLOCK if k == last else len(blk)
            out += blk[lo:hi]
        b[:len(out)] = out
        self.pos += len(out)
        return len(out)
