"""A disposable process bounds DNS/connect/body time and keeps keys out of diagnostics."""
import multiprocessing
import copy
import json
import logging
import queue
import threading
import time
import uuid

from thingspeak_child import observation
# The spawn child runs thingspeak_child.worker, which must not import edge_api. Names below stay importable from here.
from thingspeak_child.worker import (URL,ThingSpeakResult,_child,_read_result,_retry_after,http_exchange)  # noqa: F401

logger=logging.getLogger(__name__)


def _read_frames(connection,incoming):
    # recv_bytes may block on a truncated IPC write. Keep it off the deadline thread.
    try:
        for _ in range(observation.MAX_WIRE_FRAMES):
            raw=connection.recv_bytes(observation.MAX_FRAME_BYTES)
            incoming.put_nowait(('frame',raw))
        incoming.put_nowait(('limit',None))
    except EOFError:incoming.put_nowait(('closed',None))
    except OSError:incoming.put_nowait(('invalid',None))


def _timeout_diagnostic(process,last_child,marker_at):
    # Snapshot taken before cleanup terminates the child. No payload, header, URL, key or exception text.
    marked=last_child!='UNKNOWN'
    return {'childAlive':bool(process.is_alive()),'exitCode':process.exitcode,
        'lastMarker':last_child if marked else None,'markerBasis':'CHILD' if marked else 'START_RETURNED',
        'msSinceLastMarker':round((time.monotonic()-marker_at)*1000,3)}


class ThingSpeakClient:
    def __init__(self,seconds=5,request=http_exchange):
        self.seconds,self.request=seconds,request
        self.last_observation=None

    def diagnostics(self):
        return copy.deepcopy(self.last_observation)

    def publish(self,key,payload,cancelled=lambda:False):
        context=multiprocessing.get_context('spawn')
        parent,child=context.Pipe(duplex=False)
        attempt=uuid.uuid4().hex;began=time.monotonic();events=[];last_child='UNKNOWN';last_http='UNKNOWN'
        rank=-1;last_child_ms=0;limited=False;reader=None;marker_at=None;timeout=None
        incoming=queue.Queue(maxsize=observation.MAX_WIRE_FRAMES+1)
        process=context.Process(target=_child,args=(child,self.request,key,payload,self.seconds,attempt),daemon=True)
        result=ThingSpeakResult('UNCERTAIN','TRANSPORT_ERROR')
        def parent_event(phase):
            events.append({'phase':phase,'scope':'PARENT','elapsedMs':round((time.monotonic()-began)*1000,3)})
        parent_event('START_REQUESTED')
        try:
            process.start();child.close();deadline=time.monotonic()+self.seconds
            parent_event('START_RETURNED');marker_at=time.monotonic()
            reader=threading.Thread(target=_read_frames,args=(parent,incoming),name='thingspeak-pipe-'+attempt[:8],daemon=True)
            reader.start()
            while not cancelled() and time.monotonic()<deadline:
                try:kind,raw=incoming.get(timeout=min(.05,max(0,deadline-time.monotonic())))
                except queue.Empty:
                    continue
                if kind!='frame':
                    limited=kind in ('limit','invalid');result=ThingSpeakResult('UNCERTAIN','INVALID_PROTOCOL' if limited else 'TRANSPORT_EXITED');break
                try:
                    message=observation.decode(raw,attempt)
                    if message['kind']=='result':
                        result=_read_result(message['result']);parent_event('RESULT_RECEIVED');break
                    phase=message['phase'];phase_rank=observation.CHILD_PHASES.index(phase)
                    if phase_rank<=rank or message['elapsedMs']<last_child_ms:raise ValueError('Out of order progress')
                    rank=phase_rank;last_child_ms=message['elapsedMs'];last_child=phase;marker_at=time.monotonic()
                    if phase in observation.HTTP_PHASES:last_http=phase
                    events.append({'phase':phase,'scope':'CHILD','elapsedMs':message['elapsedMs'],
                        'observedAfterMs':round((time.monotonic()-began)*1000,3)})
                except (ValueError,TypeError,KeyError,UnicodeError,RecursionError):
                    limited=True;result=ThingSpeakResult('UNCERTAIN','INVALID_PROTOCOL');break
            else:
                result=ThingSpeakResult('UNCERTAIN','CANCELLED' if cancelled() else 'TIMEOUT')
                if result.code=='TIMEOUT':timeout=_timeout_diagnostic(process,last_child,marker_at)
            return result
        finally:
            cleanup_deadline=time.monotonic()+4  # Keep the previous two 2 s process-cleanup budgets.
            if process.pid:
                if process.is_alive():process.terminate()
                process.join(timeout=2)
                if process.is_alive():process.kill();process.join(timeout=2)
                process.close()
            parent.close();child.close()
            if reader is not None:reader.join(timeout=min(1,max(0,cleanup_deadline-time.monotonic())))
            parent_event('FINAL_RESULT')
            self.last_observation={'attemptId':attempt,'phaseAttribution':'OBSERVED_ONLY',
                'lastChildPhase':last_child,'lastHttpPhase':last_http,'resultCode':result.code,
                'timeout':timeout,'events':events[:observation.MAX_EVENTS],'diagnosticsLimited':limited,
                'readerStopped':reader is None or not reader.is_alive()}
            text=json.dumps(self.last_observation,separators=(',',':'),allow_nan=False)
            try:logger.log(logging.WARNING if result.state=='UNCERTAIN' else logging.INFO,'ThingSpeak diagnostic %s',text)
            except Exception:pass  # Observability failure must not replace a validated receipt.
