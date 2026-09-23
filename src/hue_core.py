"""Pure color conversion and HTTP helpers, embedded in the TOX at build time."""
import math
import json
import ssl
import urllib.request
import urllib.error

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
