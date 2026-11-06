"""A disposable process bounds DNS/connect/body time and keeps keys out of diagnostics."""
import multiprocessing
import copy
import json
import logging
import math
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime

import requests
from . import thingspeak_observation as observation

logger=logging.getLogger(__name__)

URL='https://api.thingspeak.com/update'


@dataclass(frozen=True)
class ThingSpeakResult:
    state: str
    code: str
    entry_id: int | None = None
    retry_after: float = 0
    http_status: int | None = None


def _retry_after(value):
    try:
        seconds=int(value) if re.fullmatch(r'[0-9]{1,9}',value or '') else (
            parsedate_to_datetime(value).astimezone(timezone.utc)-datetime.now(timezone.utc)).total_seconds()
        if seconds>31622400:return None
        return max(0,seconds)
    except (ValueError,TypeError,OverflowError,AttributeError):return 0


def http_exchange(key,payload,seconds,url=URL):
    # Only tests call this helper with a loopback double; the configured client uses URL.
    # No redirects, ambient proxy/netrc credentials, retries, or credential-bearing body.
    session=requests.Session();session.trust_env=False
    try:
        observation.progress('HTTP_STARTED')
        with session.post(url,data=payload,headers={'THINGSPEAKAPIKEY':key},
                          timeout=(min(seconds,2),min(seconds,2)),allow_redirects=False,
                          verify=True,stream=True) as response:
            observation.progress('HEADERS_RECEIVED')
            status=response.status_code
            if 300<=status<400:return ThingSpeakResult('REJECTED','REDIRECT_REFUSED',http_status=status)
            if status==429:
                delay=_retry_after(response.headers.get('Retry-After'))
                if delay is None:return ThingSpeakResult('REJECTED','RETRY_AFTER_REQUIRES_REVIEW',http_status=status)
                return ThingSpeakResult('PENDING','HTTP_429',retry_after=delay,http_status=status)
            if 400<=status<500:return ThingSpeakResult('REJECTED',f'HTTP_{status}',http_status=status)
            if status!=200:return ThingSpeakResult('UNCERTAIN',f'HTTP_{status}',http_status=status)
            body=bytearray();started=time.monotonic()
            for chunk in response.iter_content(chunk_size=1):
                body.extend(chunk)
                if len(body)>64 or time.monotonic()-started>seconds:
                    return ThingSpeakResult('UNCERTAIN','INVALID_BODY',http_status=status)
            observation.progress('BODY_COMPLETED')
            text=bytes(body).strip()
            if text==b'0':
                observation.progress('RESPONSE_VALIDATED')
                return ThingSpeakResult('PENDING','ZERO_ENTRY',http_status=status)
            if re.fullmatch(rb'[1-9][0-9]{0,18}',text):
                observation.progress('RESPONSE_VALIDATED')
                return ThingSpeakResult('CONFIRMED','ENTRY_ACCEPTED',int(text),http_status=status)
            return ThingSpeakResult('UNCERTAIN','INVALID_BODY',http_status=status)
    except requests.ConnectTimeout:
        return ThingSpeakResult('PENDING','CONNECT_TIMEOUT')
    except (requests.RequestException,ValueError):
        return ThingSpeakResult('UNCERTAIN','NETWORK_ERROR')
    finally:session.close()


_CODES=frozenset({'ENTRY_ACCEPTED','ZERO_ENTRY','REDIRECT_REFUSED','RETRY_AFTER_REQUIRES_REVIEW',
    'INVALID_BODY','CONNECT_TIMEOUT','NETWORK_ERROR','TRANSPORT_ERROR','TIMEOUT','CANCELLED','TRANSPORT_EXITED','INVALID_PROTOCOL'})


def _read_result(data):
    if not isinstance(data,dict) or set(data)!= {'state','code','entryId','retryAfter','httpStatus'}:raise ValueError('Invalid final result')
    state,code,entry,delay,status=(data[k] for k in ('state','code','entryId','retryAfter','httpStatus'))
    if state not in ('CONFIRMED','PENDING','UNCERTAIN','REJECTED') or not isinstance(code,str):raise ValueError('Invalid outcome')
    if code not in _CODES and re.fullmatch(r'HTTP_[1-5][0-9]{2}',code) is None:raise ValueError('Invalid outcome code')
    if entry is not None and (type(entry) is not int or not 1<=entry<10**19):raise ValueError('Invalid entry ID')
    if state=='CONFIRMED' and (code!='ENTRY_ACCEPTED' or entry is None or status!=200):raise ValueError('Missing receipt')
    if state!='CONFIRMED' and entry is not None:raise ValueError('Contradictory entry ID')
    if type(delay) not in (int,float) or not 0<=delay<=31622400 or not math.isfinite(delay):raise ValueError('Invalid delay')
    if status is not None and (type(status) is not int or not 100<=status<=599):raise ValueError('Invalid status')
    return ThingSpeakResult(state,code,entry,delay,status)


def _child(pipe,request,key,payload,seconds,attempt=None):
    attempt=attempt or uuid.uuid4().hex
    reporter=observation.Reporter(pipe,attempt);token=observation._reporter.set(reporter)
    try:
        reporter.emit('CHILD_ENTERED')
        try:result=request(key,payload,seconds)
        except Exception:result=ThingSpeakResult('UNCERTAIN','TRANSPORT_ERROR')
        try:
            if not isinstance(result,ThingSpeakResult):raise ValueError('Invalid request result')
            data={'state':result.state,'code':result.code,'entryId':result.entry_id,
                'retryAfter':result.retry_after,'httpStatus':result.http_status}
            _read_result(data)
        except (ValueError,TypeError):
            data={'state':'UNCERTAIN','code':'TRANSPORT_ERROR','entryId':None,'retryAfter':0,'httpStatus':None}
        reporter.emit('RESULT_READY')
        try:pipe.send_bytes(observation.encode('result',attempt,result=data))
        except (OSError,EOFError):pass
    finally:
        observation._reporter.reset(token);pipe.close()


def _read_frames(connection,incoming):
    # recv_bytes may block on a truncated IPC write. Keep it off the deadline thread.
    try:
        for _ in range(observation.MAX_WIRE_FRAMES):
            raw=connection.recv_bytes(observation.MAX_FRAME_BYTES)
            incoming.put_nowait(('frame',raw))
        incoming.put_nowait(('limit',None))
    except EOFError:incoming.put_nowait(('closed',None))
    except OSError:incoming.put_nowait(('invalid',None))


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
        rank=-1;last_child_ms=0;limited=False;reader=None
        incoming=queue.Queue(maxsize=observation.MAX_WIRE_FRAMES+1)
        process=context.Process(target=_child,args=(child,self.request,key,payload,self.seconds,attempt),daemon=True)
        result=ThingSpeakResult('UNCERTAIN','TRANSPORT_ERROR')
        def parent_event(phase):
            events.append({'phase':phase,'scope':'PARENT','elapsedMs':round((time.monotonic()-began)*1000,3)})
        parent_event('START_REQUESTED')
        try:
            process.start();child.close();deadline=time.monotonic()+self.seconds
            parent_event('START_RETURNED')
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
                    rank=phase_rank;last_child_ms=message['elapsedMs'];last_child=phase
                    if phase in observation.HTTP_PHASES:last_http=phase
                    events.append({'phase':phase,'scope':'CHILD','elapsedMs':message['elapsedMs'],
                        'observedAfterMs':round((time.monotonic()-began)*1000,3)})
                except (ValueError,TypeError,KeyError,UnicodeError,RecursionError):
                    limited=True;result=ThingSpeakResult('UNCERTAIN','INVALID_PROTOCOL');break
            else:result=ThingSpeakResult('UNCERTAIN','CANCELLED' if cancelled() else 'TIMEOUT')
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
                'events':events[:observation.MAX_EVENTS],'diagnosticsLimited':limited,
                'readerStopped':reader is None or not reader.is_alive()}
            text=json.dumps(self.last_observation,separators=(',',':'),allow_nan=False)
            try:logger.log(logging.WARNING if result.state=='UNCERTAIN' else logging.INFO,'ThingSpeak diagnostic %s',text)
            except Exception:pass  # Observability failure must not replace a validated receipt.
