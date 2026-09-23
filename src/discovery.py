"""Bounded IPv4 mDNS discovery for Hue Bridges."""
import socket
import struct
import time

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
