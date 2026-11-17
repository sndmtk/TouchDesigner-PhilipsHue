"""Pure color conversion and HTTP helpers, embedded in the TOX at build time."""
import math
import json
import ssl
import http.client
import socket
import urllib.request
import urllib.error

# Smallest changes worth a Zigbee command. Hue brightness has 254 steps (~0.4%),
# and an xy step of 0.002 is below what is normally visible.
XY_EPS = 0.002
BRI_EPS = 0.5

def clip_xy(point, gamut):
    if not gamut:
        return point
    a, b, c = [(gamut[k]['x'], gamut[k]['y']) for k in ('red', 'green', 'blue')]
    def cross(u, v, p):
        return (v[0]-u[0])*(p[1]-u[1])-(v[1]-u[1])*(p[0]-u[0])
    signs = [cross(u,v,point) for u,v in ((a,b),(b,c),(c,a))]
    if min(signs) >= 0 or max(signs) <= 0:
        return point
    candidates=[]
    for u,v in ((a,b),(b,c),(c,a)):
        dx,dy=v[0]-u[0],v[1]-u[1]
        t=max(0,min(1,((point[0]-u[0])*dx+(point[1]-u[1])*dy)/(dx*dx+dy*dy)))
        candidates.append((u[0]+t*dx,u[1]+t*dy))
    return min(candidates,key=lambda p:(p[0]-point[0])**2+(p[1]-point[1])**2)

def color_body(rgba, seconds, linear=False, gamut=None):
    if len(rgba)!=4 or not all(math.isfinite(float(v)) for v in rgba):
        raise ValueError('RGBA must contain four finite numbers')
    r,g,b,a=[max(0,min(1,float(v))) for v in rgba]
    if not linear:
        r,g,b=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in (r,g,b)]
    x=.4124564*r+.3575761*g+.1804375*b
    y=.2126729*r+.7151522*g+.0721750*b
    z=.0193339*r+.1191920*g+.9503041*b
    body={'dimming':{'brightness':round(100*a,4)},
          'dynamics':{'duration':max(0,round(seconds*1000))}}
    if x+y+z>1e-12:
        cx,cy=clip_xy((x/(x+y+z),y/(x+y+z)),gamut)
        body['color']={'xy':{'x':round(cx,6),'y':round(cy,6)}}
    return body

def desired_state(body, light, on=True):
    """Reduce a color_body() result to the state this light can show: on / bri / xy."""
    if not on:
        return {'on': False}
    state = {'on': True}
    if 'dimming' in light and 'dimming' in body:
        state['bri'] = body['dimming']['brightness']
    if 'color' in light and 'color' in body:
        xy = body['color']['xy']
        state['xy'] = (xy['x'], xy['y'])
    return state


def state_command(prev, want, duration_ms, refresh=False):
    """Smallest PUT body that moves a light from `prev` (last confirmed) to `want`.

    Returns (body, score, new_state). body is None when nothing visible changed.
    `on` is only sent when power changes, which saves a Zigbee message per command.
    """
    prev = prev or {}
    power = refresh or prev.get('on') != want['on']
    body = {}
    score = 0.0
    if power:
        body['on'] = {'on': want['on']}
        score = float('inf')
    new_state = dict(prev, on=want['on'])
    if want['on']:
        if 'bri' in want:
            old = prev.get('bri')
            diff = abs(want['bri'] - old) if old is not None else float('inf')
            if power or diff >= BRI_EPS:
                body['dimming'] = {'brightness': want['bri']}
                new_state['bri'] = want['bri']
                score = max(score, diff / BRI_EPS)
        if 'xy' in want:
            old = prev.get('xy')
            diff = math.dist(want['xy'], old) if old is not None else float('inf')
            if power or diff >= XY_EPS:
                body['color'] = {'xy': {'x': want['xy'][0], 'y': want['xy'][1]}}
                new_state['xy'] = want['xy']
                score = max(score, diff / XY_EPS)
    if not body:
        return None, 0.0, prev
    body['dynamics'] = {'duration': max(0, int(duration_ms))}
    return body, score, new_state


class Client:
    """One persistent HTTPS connection. Use it from a single worker thread only.

    Reusing the TLS session avoids ~100 ms of handshake per request on a Hue Bridge.
    """

    def __init__(self, timeout=5):
        self.timeout = timeout
        self.conn = None
        self.host = None

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None

    def request(self, host, key, method, path, body=None, insecure=False):
        # Only this method runs on the worker; never access TouchDesigner here.
        if host != self.host:
            self.close()
            self.host = host
        data = None if body is None else json.dumps(body).encode()
        headers = {'Content-Type': 'application/json'}
        if key:
            headers['hue-application-key'] = key
        for attempt in (0, 1):
            if self.conn is None:
                context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
                self.conn = http.client.HTTPSConnection(host, 443, timeout=self.timeout, context=context)
            try:
                if self.conn.sock is None:
                    self.conn.connect()
                    # Small request/response pairs: disable Nagle so a command is never held back.
                    self.conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.conn.request(method, path, body=data, headers=headers)
                response = self.conn.getresponse()
                raw = response.read()
            except (http.client.RemoteDisconnected, http.client.CannotSendRequest,
                    http.client.BadStatusLine, ConnectionResetError, BrokenPipeError):
                # The Bridge closed an idle kept-alive socket. GET/PUT are safe to repeat once.
                self.close()
                if attempt == 0 and method in ('GET', 'PUT'):
                    continue
                raise
            except Exception:
                self.close()
                raise
            if (response.getheader('Connection') or '').lower() == 'close':
                self.close()
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = None
            if response.status >= 400 and not (isinstance(parsed, dict) and parsed.get('errors')):
                parsed = {'errors': [{'description': 'HTTP ' + str(response.status)}]}
            elif parsed is None:
                parsed = {'errors': [{'description': 'Non-JSON response'}]}
            return response.status, parsed


def request(host, key, method, path, body=None, insecure=False):
    # Only this function runs on the worker; never access TouchDesigner here.
    headers={'Content-Type':'application/json'}
    if key:
        headers['hue-application-key']=key
    context=ssl._create_unverified_context() if insecure else ssl.create_default_context()
    req=urllib.request.Request('https://'+host+path,
        data=None if body is None else json.dumps(body).encode(),headers=headers,method=method)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context))
    try:
        with opener.open(req,timeout=5) as response:
            return response.status,json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code,{'errors':[{'description':'HTTP '+str(e.code)}]}
