"""Hue Entertainment helpers, embedded in HueEntertainment.tox (no TouchDesigner access).

  * HueStream v2 packets, colour conversion and smoothing
  * Entertainment area parsing, area/mapping helpers
  * Client: kept-alive HTTPS connection for the CLIP v2 API (used from one worker thread)
  * discover(): bounded IPv4 mDNS search for Hue Bridges (used from one worker thread)
"""
import http.client
import json
import math
import socket
import ssl
import struct
import time

PROTOCOL = b'HueStream'
COLOR_RGB = 0x00
COLOR_XY = 0x01
MAX_CHANNELS = 20
WHITE_XY = (0.3127, 0.3290)  # D65; used where a pixel has no chromaticity (black)


def packet(config_id, channels, color_space=COLOR_XY, sequence=0):
    """HueStream v2 frame: 16-byte header, 36-char area id, 7 bytes per channel.

    channels: iterable of (channel_id, a, b, c) with a/b/c as floats 0..1
    (x, y, brightness in XY mode; r, g, b in RGB mode).
    """
    cid = config_id.encode('ascii')
    if len(cid) != 36:
        raise ValueError('Entertainment configuration id must be a 36-character UUID')
    out = bytearray(PROTOCOL)
    out += bytes([0x02, 0x00, sequence & 0xFF, 0x00, 0x00, color_space, 0x00])
    out += cid
    count = 0
    for channel_id, a, b, c in channels:
        if count == MAX_CHANNELS:
            break
        out += struct.pack('>BHHH', channel_id, _u16(a), _u16(b), _u16(c))
        count += 1
    return bytes(out)


def _u16(v):
    v = float(v)
    if not math.isfinite(v):
        v = 0.0
    return int(round(max(0.0, min(1.0, v)) * 65535))


def rgba_to_linear(rgba, linear=False, brightness='value'):
    """One RGBA pixel (0..1) -> (r, g, b, bri) with r/g/b in linear light.

    brightness='value': alpha x max(R,G,B) of the input, so darker pixels give
                        dimmer lights (natural for video).
    brightness='alpha': alpha only; RGB sets the colour alone.
    """
    if len(rgba) != 4:
        raise ValueError('RGBA must contain four numbers')
    vals = []
    for v in rgba:
        v = float(v)
        vals.append(max(0.0, min(1.0, v)) if math.isfinite(v) else 0.0)
    r, g, b, a = vals
    value = max(r, g, b)
    if not linear:
        r, g, b = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    bri = a * value if brightness == 'value' else a
    return r, g, b, bri


def linear_to_xyb(r, g, b, bri):
    """Linear RGB + brightness -> (x, y, brightness) for the XY stream format."""
    X = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    Y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    Z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    total = X + Y + Z
    x, y = (X / total, Y / total) if total > 1e-12 else WHITE_XY
    return x, y, bri


def rgba_to_xyb(rgba, linear=False, brightness='value'):
    """Convert one RGBA pixel (0..1) to (x, y, brightness 0..1)."""
    return linear_to_xyb(*rgba_to_linear(rgba, linear, brightness))


def smooth_step(prev, target, dt, seconds):
    """Exponential approach of `prev` toward `target` (tuples). Reaches about 95%
    of the change after `seconds`; seconds <= 0 or no history returns target."""
    if prev is None or seconds <= 0:
        return tuple(target)
    k = 1.0 - math.exp(-3.0 * max(0.0, dt) / seconds)
    return tuple(p + (t - p) * k for p, t in zip(prev, target))


def candidate_lights(entertainment, devices):
    """Entertainment services that can join an area (renderer=True), with device names."""
    names = {d['id']: d.get('metadata', {}).get('name', d['id'][:8]) for d in devices}
    out = []
    for e in entertainment:
        if not e.get('renderer'):
            continue  # e.g. the Bridge's own service, or a light without colour
        dev = e.get('owner', {}).get('rid')
        out.append({'id': e['id'], 'device': dev, 'name': names.get(dev, e['id'][:8])})
    return sorted(out, key=lambda c: c['name'].lower())


def area_locations(service_ids):
    """service_locations with the services placed left to right (x from -1 to 1)
    in the given order, so pixel order = light order."""
    n = len(service_ids)
    locations = []
    for i, sid in enumerate(service_ids):
        x = 0.0 if n == 1 else -1.0 + 2.0 * i / (n - 1)
        locations.append({'service': {'rid': sid, 'rtype': 'entertainment'},
                          'positions': [{'x': round(x, 4), 'y': 0.0, 'z': 0.0}]})
    return {'service_locations': locations}


def area_body(name, service_ids, configuration_type='other'):
    """POST body for a new entertainment_configuration."""
    return {'type': 'entertainment_configuration', 'metadata': {'name': name},
            'configuration_type': configuration_type, 'locations': area_locations(service_ids)}


def update_body(service_ids):
    """PUT body that replaces the lights of an existing entertainment_configuration."""
    return {'locations': area_locations(service_ids)}


def channel_order(channels):
    """Left to right by position (then y, then channel id)."""
    return sorted(channels, key=lambda ch: (ch['x'], ch['y'], ch['channel_id']))


def normalize_mapping(rows, channels, rebuild=False):
    """Pixel -> light table that always covers every channel exactly once with
    pixels 0..N-1.

    rows: current table rows as (pixel_text, light_key).
    channels: the area's channels (with 'key', 'channel_id', 'name', 'x', 'y').
    Rows are matched to channels by light_key (stable across the Bridge renumbering
    channel ids). Kept lights keep their order (by the pixel value the user typed,
    so 2.5 moves a light between 2 and 3; ties keep table order), removed lights
    are dropped, new lights are appended left to right, then everything is
    renumbered 0..N-1. rebuild=True ignores the old rows and orders left to right.

    Returns rows as [pixel, channel_id, name, light_key].
    """
    by_key = {ch['key']: ch for ch in channels}
    kept = []
    if not rebuild:
        seen = set()
        for i, (pixel, key) in enumerate(rows):
            if key not in by_key or key in seen:
                continue
            seen.add(key)
            try:
                order = float(pixel)
                if not math.isfinite(order):
                    raise ValueError
            except (TypeError, ValueError):
                order = float('inf')  # unreadable number: keep, but move to the end
            kept.append((order, i, key))
        kept.sort()
    keys = [k for _, _, k in kept]
    keys += [ch['key'] for ch in channel_order(channels) if ch['key'] not in set(keys)]
    return [[i, by_key[k]['channel_id'], by_key[k]['name'], k] for i, k in enumerate(keys)]


def parse_areas(configs, entertainment, devices):
    """Combine CLIP v2 resources into areas with named channels.

    configs: data of /resource/entertainment_configuration
    entertainment: data of /resource/entertainment (service -> owner device)
    devices: data of /resource/device (device -> name)
    """
    device_names = {d['id']: d.get('metadata', {}).get('name', d['id'][:8]) for d in devices}
    service_owner = {e['id']: e.get('owner', {}).get('rid') for e in entertainment}
    areas = []
    for cfg in configs:
        raw = sorted(cfg.get('channels', []), key=lambda ch: ch.get('channel_id', 0))
        per_device = {}
        for ch in raw:
            for m in ch.get('members', []):
                dev = service_owner.get(m.get('service', {}).get('rid'))
                per_device[dev] = per_device.get(dev, 0) + 1
        channels = []
        for ch in raw:
            names = []
            keys = []
            for m in ch.get('members', []):
                # Stable identity of what the channel drives: service id + segment index.
                keys.append('%s#%d' % (m.get('service', {}).get('rid', ''), m.get('index', 0)))
                dev = service_owner.get(m.get('service', {}).get('rid'))
                name = device_names.get(dev, 'channel')
                if per_device.get(dev, 0) > 1:
                    name += ' #' + str(m.get('index', 0))  # gradient segments
                names.append(name)
            pos = ch.get('position', {})
            channels.append({'channel_id': ch.get('channel_id', 0), 'name': ' + '.join(names) or 'channel',
                             'key': '|'.join(keys) or 'channel-%d' % ch.get('channel_id', 0),
                             'services': [m.get('service', {}).get('rid') for m in ch.get('members', [])],
                             'x': pos.get('x', 0), 'y': pos.get('y', 0), 'z': pos.get('z', 0)})
        streamer = cfg.get('active_streamer', {}).get('rid', '')
        areas.append({'id': cfg['id'], 'name': cfg.get('metadata', {}).get('name', cfg['id'][:8]),
                      'status': cfg.get('status', ''), 'active_streamer': streamer, 'channels': channels})
    return sorted(areas, key=lambda a: a['name'].lower())


def candidate_devices(entertainment):
    """Map entertainment service id -> owner device id."""
    return {e['id']: e.get('owner', {}).get('rid') for e in entertainment}


def light_devices(devices):
    """Devices that have a light service (what Delete Selected Device can remove)."""
    out = [{'id': d['id'], 'name': d.get('metadata', {}).get('name', d['id'][:8])}
           for d in devices if any(s.get('rtype') == 'light' for s in d.get('services', []))]
    return sorted(out, key=lambda d: d['name'].lower())


def connectivity(zigbee):
    """Map device id -> Zigbee status ('connected', 'connectivity_issue', ...)."""
    return {z.get('owner', {}).get('rid'): z.get('status', '') for z in zigbee if z.get('owner', {}).get('rid')}


# --------------------------------------------------------------------------- HTTPS (CLIP v2)
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


# --------------------------------------------------------------------------- mDNS discovery
SERVICE='_hue._tcp.local'

def read_name(data,offset):
    labels=[]
    end=None
    visited=set()
    while True:
        if offset in visited or offset>=len(data):
            raise ValueError('Invalid DNS name')
        visited.add(offset)
        size=data[offset]
        if size & 0xc0 == 0xc0:
            if offset+1>=len(data):
                raise ValueError('Truncated DNS pointer')
            if end is None: end=offset+2
            offset=((size & 63)<<8)|data[offset+1]
            continue
        if size>63 or offset+1+size>len(data):
            raise ValueError('Invalid DNS label')
        offset+=1
        if not size:
            return '.'.join(labels).lower(),end if end is not None else offset
        labels.append(data[offset:offset+size].decode('utf-8',errors='replace'))
        offset+=size

def parse_packet(data):
    if len(data)<12: raise ValueError('Truncated DNS header')
    _,flags,questions,answers,authority,additional=struct.unpack_from('!6H',data)
    if not flags & 0x8000: return []
    offset=12
    for _ in range(questions):
        _,offset=read_name(data,offset)
        offset+=4
    records=[]
    for _ in range(answers+authority+additional):
        owner,offset=read_name(data,offset)
        kind,cls,ttl,length=struct.unpack_from('!HHIH',data,offset)
        offset+=10
        end=offset+length
        if end>len(data): raise ValueError('Truncated DNS record')
        value=None
        if kind==12:
            value=read_name(data,offset)[0]
        elif kind==33 and length>=7:
            value=(struct.unpack_from('!H',data,offset+4)[0],read_name(data,offset+6)[0])
        elif kind==1 and length==4:
            value=socket.inet_ntoa(data[offset:end])
        elif kind==16:
            value={}
            pos=offset
            while pos<end:
                size=data[pos];pos+=1
                if pos+size>end: raise ValueError('Truncated TXT')
                entry=data[pos:pos+size].decode('utf-8',errors='replace')
                key,sep,val=entry.partition('=')
                if sep: value[key.lower()]=val
                pos+=size
        if value is not None and ttl and cls & 0x7fff == 1:
            records.append((owner,kind,value))
        offset=end
    return records

def bridges_from_records(records):
    instances={v for n,k,v in records if n==SERVICE and k==12}
    result={}
    for instance in instances:
        info={}
        for n,k,v in records:
            if n==instance and k==16: info.update(v)
        for n,k,v in records:
            if n!=instance or k!=33 or v[0]!=443: continue
            for host,kind,ip in records:
                if host==v[1] and kind==1:
                    result[ip]={'id':info.get('bridgeid',instance.split('.')[0]),'ip':ip,'source':'mDNS'}
    return sorted(result.values(),key=lambda b:b['ip'])

def query(name,kind):
    encoded=b''.join(bytes([len(s.encode())])+s.encode() for s in name.split('.'))+b'\0'
    # Ephemeral source port requests a legacy unicast response; do not bind port 5353.
    return struct.pack('!6H',0,0,1,0,0,0)+encoded+struct.pack('!HH',kind,1)

def discover_mdns(timeout=3):
    records=[]
    deadline=time.monotonic()+timeout
    requested=set()
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.bind(('',0))
        sock.setsockopt(socket.IPPROTO_IP,socket.IP_MULTICAST_TTL,255)
        def ask(name,kind):
            if (name,kind) not in requested:
                sock.sendto(query(name,kind),('224.0.0.251',5353))
                requested.add((name,kind))
        ask(SERVICE,12)
        while time.monotonic()<deadline:
            sock.settimeout(max(.01,deadline-time.monotonic()))
            try:
                data,_=sock.recvfrom(65535)
            except socket.timeout:
                break
            try:
                records.extend(parse_packet(data))
            except (ValueError,struct.error):
                continue
            for n,k,v in list(records):
                if n==SERVICE and k==12:
                    ask(v,33);ask(v,16)
                elif k==33 and n.endswith('.'+SERVICE):
                    ask(v[1],1)
    return bridges_from_records(records)

def discover():
    try:
        return discover_mdns(),''
    except OSError as e:
        return [],'mDNS: '+str(e)
