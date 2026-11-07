"""Bounded local progress frames. They carry no request data and are never receipts."""
import contextvars
import json
import math
import time

VERSION=1
MAX_FRAME_BYTES=512
MAX_WIRE_FRAMES=12
MAX_EVENTS=12
CHILD_PHASES=('CHILD_ENTERED','HTTP_STARTED','HEADERS_RECEIVED','BODY_COMPLETED','RESPONSE_VALIDATED','RESULT_READY')
HTTP_PHASES=CHILD_PHASES[1:5]
_reporter=contextvars.ContextVar('thingspeak_observer',default=None)


def encode(kind,attempt,**fields):
    frame=json.dumps({'v':VERSION,'kind':kind,'attemptId':attempt,**fields},
        separators=(',',':'),allow_nan=False).encode('utf-8')
    if len(frame)>MAX_FRAME_BYTES:raise ValueError('Observation frame too large')
    return frame


def decode(raw,attempt):
    value=json.loads(raw)
    if not isinstance(value,dict) or type(value.get('v')) is not int or value['v']!=VERSION or value.get('attemptId')!=attempt:
        raise ValueError('Invalid observation envelope')
    if value.get('kind')=='progress':
        if set(value)!= {'v','kind','attemptId','phase','elapsedMs'} or value['phase'] not in CHILD_PHASES:
            raise ValueError('Invalid progress frame')
        elapsed=value['elapsedMs']
        if type(elapsed) not in (int,float) or not 0<=elapsed<=600000 or not math.isfinite(elapsed):
            raise ValueError('Invalid progress duration')
    elif value.get('kind')=='result':
        if set(value)!= {'v','kind','attemptId','result'}:raise ValueError('Invalid result envelope')
    else:raise ValueError('Unknown observation frame')
    return value


class Reporter:
    def __init__(self,pipe,attempt):
        self.pipe,self.attempt=pipe,attempt;self.started=time.monotonic();self.last=-1

    def emit(self,phase):
        if phase not in CHILD_PHASES:return
        rank=CHILD_PHASES.index(phase)
        if rank<=self.last:return  # One event per phase; bounded even if a caller repeats.
        self.last=rank
        try:self.pipe.send_bytes(encode('progress',self.attempt,phase=phase,
            elapsedMs=round((time.monotonic()-self.started)*1000,3)))
        except (OSError,EOFError):pass


def progress(phase):
    observer=_reporter.get()
    if observer is not None:observer.emit(phase)
