"""A disposable process bounds DNS/connect/body time and keeps keys out of diagnostics."""
import multiprocessing
import re
import time
from dataclasses import dataclass
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime

import requests

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
        with session.post(url,data=payload,headers={'THINGSPEAKAPIKEY':key},
                          timeout=(min(seconds,2),min(seconds,2)),allow_redirects=False,
                          verify=True,stream=True) as response:
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
            text=bytes(body).strip()
            if text==b'0':return ThingSpeakResult('PENDING','ZERO_ENTRY',http_status=status)
            if re.fullmatch(rb'[1-9][0-9]{0,18}',text):
                return ThingSpeakResult('CONFIRMED','ENTRY_ACCEPTED',int(text),http_status=status)
            return ThingSpeakResult('UNCERTAIN','INVALID_BODY',http_status=status)
    except requests.ConnectTimeout:
        return ThingSpeakResult('PENDING','CONNECT_TIMEOUT')
    except (requests.RequestException,ValueError):
        return ThingSpeakResult('UNCERTAIN','NETWORK_ERROR')
    finally:session.close()


def _child(pipe,request,key,payload,seconds):
    try:
        try:result=request(key,payload,seconds)
        except Exception:result=ThingSpeakResult('UNCERTAIN','TRANSPORT_ERROR')
        try:pipe.send(result)
        except (OSError,EOFError):pass  # Owner cancellation/loss must not print request diagnostics.
    finally:pipe.close()


class ThingSpeakClient:
    def __init__(self,seconds=5,request=http_exchange):
        self.seconds,self.request=seconds,request

    def publish(self,key,payload,cancelled=lambda:False):
        context=multiprocessing.get_context('spawn')
        parent,child=context.Pipe(duplex=False)
        process=context.Process(target=_child,args=(child,self.request,key,payload,self.seconds),daemon=True)
        try:
            process.start();child.close();deadline=time.monotonic()+self.seconds
            while not cancelled() and time.monotonic()<deadline:
                if parent.poll(min(.05,max(0,deadline-time.monotonic()))):
                    try:return parent.recv()
                    except EOFError:return ThingSpeakResult('UNCERTAIN','TRANSPORT_EXITED')
                if not process.is_alive():return ThingSpeakResult('UNCERTAIN','TRANSPORT_EXITED')
            return ThingSpeakResult('UNCERTAIN','CANCELLED' if cancelled() else 'TIMEOUT')
        finally:
            if process.pid:
                if process.is_alive():process.terminate()
                process.join(timeout=2)
                if process.is_alive():process.kill();process.join(timeout=2)
                process.close()
            parent.close();child.close()
