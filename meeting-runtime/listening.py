"""Listening in: copies of a call's audio, both sides, for the owner to hear live.

A call's line pushes each side's audio here as it passes (base64, the codec and rate the tap
names). Every listener gets its own bounded queue: a slow listener loses audio, and never
slows the call down.
"""
import asyncio
import json

# Chunks a listener may fall behind by before its oldest audio is dropped (about 4 s of 20 ms
# phone frames).
LISTENER_QUEUE = 200
SIDES = frozenset({'agent', 'other', 'meeting'})


class AudioTap:
    def __init__(self, codec, rate):
        self.codec = codec
        self.rate = rate
        self.closed = False
        self._listeners = set()

    @property
    def listening(self):
        return len(self._listeners)

    def push(self, side, audio):
        if self.closed or not self._listeners or side not in SIDES or not audio:
            return
        chunk = {'side': side, 'audio': audio}
        for queue in list(self._listeners):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(chunk)

    def close(self):
        self.closed = True
        for queue in list(self._listeners):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(None)

    def format(self):
        return {'type': 'format', 'codec': self.codec, 'rate': self.rate}

    async def chunks(self):
        """The format first, then each chunk until the call ends (or the listener goes)."""
        queue = asyncio.Queue(maxsize=LISTENER_QUEUE)
        self._listeners.add(queue)
        try:
            yield self.format()
            while not self.closed or not queue.empty():
                chunk = await queue.get()
                if chunk is None:
                    return
                yield chunk
        finally:
            self._listeners.discard(queue)


def ndjson(item):
    return (json.dumps(item, separators=(',', ':')) + '\n').encode('utf-8')
