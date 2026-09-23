"""Per-component runtime. Network work is asynchronous; all OP access is on TD's thread."""
import time
import ipaddress
from concurrent.futures import ThreadPoolExecutor

class Controller:
    def __init__(self, c):
        self.c=c
        self.core=c.op('hue_core').module
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='HueHTTP')
        self.control_pool=ThreadPoolExecutor(max_workers=16,thread_name_prefix='HueControl')
        self.pending=None
        self.control_pending={}
        self.config=None
        self.generation=0
        self.ready=False
        self.next_auth=0
        self.deadline=0
        self.last_poll=0
        self.last_telemetry_poll=0
        self.telemetry_queue=[]
        self.last_send={}
        self.sent={}
        self.unavailable_until={}
        self.lights={}
        self.zigbee_connectivity={}
        self.cursor=0
        self.auth_stopped=False
        self.last_error=''
        self.last_network=0
        self.discovery_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='HueDiscovery')
        self.discovery_future=None
        self.discovery_ip=''
        self.bridges=[]
        self.management=None
        self.search_id=None
        self.search_deadline=0
        self.search_next=0
        self.status('state','Enter Bridge IP')

    def status(self,k,v):
        # The public status DAT intentionally exposes one current message only.
        if k in ('light_count','last_send','last_updated'):
            return
        k='state'
        d=self.c.op('status')
        for row in range(1,d.numRows):
            if d[row,0].val==k:
                d[row,1]=str(v)
                return
        d.appendRow([k,str(v)])

    def error(self,message):
        message=str(message)
        key=self.c.par.Apikey.eval()
        if key:
            message=message.replace(key,'[redacted]')
        if message!=self.last_error:
            self.c.op('errors').appendRow([time.strftime('%Y-%m-%d %H:%M:%S'),message])
            self.last_error=message

    def light_ready_for_send(self, light_id, now):
        return now>=self.unavailable_until.get(light_id,0)

    def should_send(self, light_id, body, now):
        previous=self.sent.get(light_id)
        power_change=previous is None or previous.get('on')!=body['on']
        return power_change or now-self.last_send.get(light_id,0)>=self.c.par.Interval.eval()

    def control_ready(self, light_id):
        return light_id not in self.control_pending

    def render_resources(self):
        """Render light state together with its Zigbee connectivity resource."""
        d=self.c.op('lights')
        d.clear()
        d.appendRow(['id','name','on','brightness','x','y','color_supported','gamut_type','zigbee_status','updated'])
        updated=time.strftime('%H:%M:%S')
        for lid,l in self.lights.items():
            xy=l.get('color',{}).get('xy',{})
            device_id=l.get('owner',{}).get('rid','')
            connectivity=self.zigbee_connectivity.get(device_id,{})
            dimming=l.get('dimming',{})
            d.appendRow([lid,l.get('metadata',{}).get('name',''),l.get('on',{}).get('on',''),dimming.get('brightness',''),xy.get('x',''),xy.get('y',''),'color' in l,l.get('color',{}).get('gamut_type',''),connectivity.get('status',''),updated])

    def submit(self,kind,method,path,body=None,light_id=None):
        host,insecure=self.config
        key=self.c.par.Apikey.eval() if self.c.par.Keyhost.eval()==host else ''
        f=self.pool.submit(self.core.request,host,key,method,path,body,insecure)
        self.pending=(f,kind,(self.config,self.generation),light_id,body)
        self.last_network=time.monotonic()

    def submit_control(self, light_id, body):
        host,insecure=self.config
        key=self.c.par.Apikey.eval() if self.c.par.Keyhost.eval()==host else ''
        future=self.control_pool.submit(self.core.request,host,key,'PUT','/clip/v2/resource/light/'+light_id,body,insecure)
        self.control_pending[light_id]=(future,(self.config,self.generation),body)

    def receive_controls(self, now):
        for light_id,item in list(self.control_pending.items()):
            future,config,body=item
            if not future.done():
                continue
            self.control_pending.pop(light_id,None)
            if config!=(self.config,self.generation):
                continue
            try:
                code,result=future.result()
                if code in (401,403):
                    self.ready=False
                    self.auth_stopped=True
                    raise ValueError('API key rejected. Use Forget Key to pair again.')
                if not isinstance(result,dict) or code>=400 or result.get('errors'):
                    raise ValueError(str(result))
                self.sent[light_id]=body
                self.unavailable_until.pop(light_id,None)
                self.status('last_send',time.strftime('%H:%M:%S'))
                self.last_error=''
            except Exception as e:
                # Isolate a failed light. Other lights continue on their own schedule.
                self.sent.pop(light_id,None)
                self.unavailable_until[light_id]=now+5
                self.status('unavailable_light',str(light_id)+' retrying in 5 seconds')
                self.error(str(e))

    def pulse(self,name,confirmed=False):
        if name in ('Searchlights','Deletelight'):
            if not self.ready or not self.config or self.bridge_ip()!=self.config[0] or self.c.par.Keyhost.eval()!=self.config[0]:
                self.error('Connect and authenticate this Bridge first')
                return
            if self.management or self.search_id or (isinstance(self.pending,tuple) and self.pending[1] in ('search_resources','search_start','search_poll','delete_device')):
                self.error('A light management operation is already running')
                return
            if name=='Searchlights':
                self.management=('search_resources','GET','/clip/v2/resource/zigbee_device_discovery',None,None)
                self.status('light_search','Queued')
            elif confirmed:
                light=self.lights.get(self.c.par.Targetlight.eval(),{})
                owner=light.get('owner',{})
                if owner.get('rtype')!='device' or not owner.get('rid'):
                    self.error('Select an available light with a device owner')
                    return
                self.management=('delete_device','DELETE','/clip/v2/resource/device/'+owner['rid'],None,owner['rid'])
                self.status('light_delete','Queued: '+light.get('metadata',{}).get('name',light['id']))
            return
        if name=='Discover':
            if self.discovery_future:
                return
            self.discovery_ip=self.bridge_ip()
            self.status('discovery','Searching...')
            self.discovery_future=self.discovery_pool.submit(self.c.op('discovery').module.discover)
            return
        if name in ('Connect','Forgetkey'):
            self.generation+=1
            self.management=None
            self.search_id=None
        if name=='Connect':
            self.ready=False
            self.auth_stopped=False
            self.deadline=0
            self.next_auth=0
            self.sent.clear()
        elif name=='Forgetkey':
            self.c.par.Apikey.val=''
            self.c.par.Keyhost.val=''
            self.ready=False
            self.auth_stopped=False
            self.deadline=0
            self.next_auth=0
            self.status('credentials','API Key cleared; save your TOE to persist')

    def receive(self,now):
        f,kind,config,light_id,body=self.pending
        if not f.done():
            return False
        self.pending=None
        if config!=(self.config,self.generation):
            return True
        try:
            code,result=f.result()
            if kind=='auth':
                if isinstance(result,list) and result and 'success' in result[0]:
                    self.c.par.Apikey.val=result[0]['success']['username']
                    self.c.par.Keyhost.val=self.config[0]
                    self.status('state','Authenticated')
                    self.status('credentials','API Key updated; save your TOE to persist')
                    self.status('last_updated',time.strftime('%H:%M:%S'))
                    self.deadline=0
                elif isinstance(result,list) and result and result[0].get('error',{}).get('type')==101:
                    self.status('state','Press the physical Bridge link button')
                else:
                    raise ValueError('Pairing rejected: '+str(result))
                self.next_auth=now+2
                return True
            if code in (401,403):
                self.ready=False
                self.auth_stopped=True
                raise ValueError('API key rejected. Use Forget Key to pair again.')
            if not isinstance(result,dict) or code>=400 or result.get('errors'):
                raise ValueError(str(result))
            self.status('last_updated',time.strftime('%H:%M:%S'))
            if kind=='lights':
                self.lights={v['id']:v for v in result['data']}
                self.render_resources()
                mapping=self.c.op('mapping')
                if self.c.fetch('mapping_host',self.config[0])!=self.config[0]:
                    mapping.clear()
                    mapping.appendRow(['pixel','light_id','name'])
                self.c.store('mapping_host',self.config[0])
                mapped={mapping[r,1].val for r in range(1,mapping.numRows)}
                pixels=[]
                for r in range(1,mapping.numRows):
                    try:
                        pixels.append(int(mapping[r,0].val))
                    except ValueError:
                        pass
                pixel=max(pixels,default=-1)+1
                for lid,l in self.lights.items():
                    if lid not in mapped:
                        mapping.appendRow([pixel,lid,l.get('metadata',{}).get('name','')])
                        pixel+=1
                menu=getattr(self.c.par,'Targetlight',None)
                if menu is not None:
                    previous=menu.eval()
                    menu.menuNames=['none']+list(self.lights)
                    menu.menuLabels=['Select a light']+[l.get('metadata',{}).get('name',lid)+' ['+lid[:8]+']' for lid,l in self.lights.items()]
                    menu.val=previous if previous in self.lights else 'none'
                self.ready=True
                self.last_poll=now
                self.status('state','Ready')
                self.status('light_count',len(self.lights))
            elif kind=='zigbee_connectivity':
                self.zigbee_connectivity={v.get('owner',{}).get('rid'):v for v in result['data'] if v.get('owner',{}).get('rid')}
                self.render_resources()
            elif kind=='search_resources':
                resources=result.get('data',[])
                if not resources:
                    raise ValueError('Bridge exposes no Zigbee discovery resource')
                self.search_id=resources[0]['id']
                self.search_deadline=now+120
                if resources[0].get('status')=='active':
                    self.search_next=now+2
                    self.status('light_search','Already searching; monitoring')
                else:
                    self.management=('search_start','PUT','/clip/v2/resource/zigbee_device_discovery/'+self.search_id,{'action':{'action_type':'search'}},None)
            elif kind=='search_start':
                self.search_next=now+2
                self.status('light_search','Searching')
            elif kind=='search_poll':
                resources=result.get('data',[])
                if not resources:
                    raise ValueError('Discovery status unavailable')
                state=resources[0].get('status')
                self.status('light_search','Searching' if state=='active' else 'Complete' if state=='ready' else str(state))
                self.search_next=now+2
                if state=='ready':
                    self.search_id=None
                    self.last_poll=0
            elif kind=='delete_device':
                removed={lid for lid,l in self.lights.items() if l.get('owner',{}).get('rid')==light_id}
                mapping=self.c.op('mapping')
                for r in reversed(range(1,mapping.numRows)):
                    if mapping[r,1].val in removed:
                        mapping.deleteRow(r)
                for lid in removed:
                    self.lights.pop(lid,None)
                    self.sent.pop(lid,None)
                    self.last_send.pop(lid,None)
                self.c.par.Targetlight.val='none'
                self.ready=False
                self.last_poll=0
                self.status('light_delete','Deleted device '+str(light_id))
            elif kind=='put':
                self.sent[light_id]=body
                self.unavailable_until.pop(light_id,None)
                self.status('last_send',time.strftime('%H:%M:%S'))
            self.last_error=''
        except Exception as e:
            if kind.startswith('search_'):
                self.search_id=None
                self.management=None
                self.status('light_search','Failed; see errors DAT')
            if kind=='delete_device':
                self.status('light_delete','Failed or unconfirmed; refresh before retrying')
                self.ready=False
                self.last_poll=0
            if kind=='put':
                # The Bridge may have applied a request even when its response was lost.
                self.sent.pop(light_id,None)
                self.unavailable_until[light_id]=now+5
                self.status('unavailable_light',str(light_id)+' retrying in 5 seconds')
            self.error(str(e))
            if kind!='put':
                self.next_auth=now+5
            if kind=='lights':
                self.last_poll=now
        return True

    def tick(self):
        now=time.monotonic()
        self.receive_discovery()
        host=self.bridge_ip()
        # Hue Bridges are addressed by their local IPv4 address and commonly use
        # a certificate that cannot be verified through the standard CA store.
        config=(host,True)
        if config!=self.config:
            self.generation+=1
            self.management=None
            self.search_id=None
            self.config=config
            self.ready=False
            self.sent.clear()
            self.last_send.clear()
            self.control_pending.clear()
            self.auth_stopped=False
            self.deadline=0
            self.next_auth=0
        self.receive_controls(now)
        if self.pending and not self.receive(now):
            return
        if not host:
            return
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            self.error('Bridge IP must be an IPv4 address')
            return
        if now<self.next_auth or self.auth_stopped:
            return
        key=self.c.par.Apikey.eval() if self.c.par.Keyhost.eval()==host else ''
        if not key:
            if not self.deadline:
                self.deadline=now+60
            if now>self.deadline:
                self.status('state','Pairing timed out; press Connect to retry')
                self.auth_stopped=True
                return
            self.submit('auth','POST','/api',{'devicetype':'touchdesigner#hue_tox'})
            return
        if self.management:
            command=self.management
            self.management=None
            self.submit(*command)
            return
        if self.search_id and now>=self.search_deadline:
            self.search_id=None
            self.status('light_search','Monitoring timed out; Bridge may still be searching')
        if self.search_id and now>=self.search_next:
            self.submit('search_poll','GET','/clip/v2/resource/zigbee_device_discovery/'+self.search_id)
            return
        if not self.ready or now-self.last_poll>=5:
            self.submit('lights','GET','/clip/v2/resource/light')
            return
        # Health and device capability data change slowly; keep it off the control path.
        if now-self.last_telemetry_poll>=15:
            self.last_telemetry_poll=now
            self.telemetry_queue=['zigbee_connectivity']
        if self.telemetry_queue:
            kind=self.telemetry_queue.pop(0)
            self.submit(kind,'GET','/clip/v2/resource/'+kind)
            return
        if not self.c.par.Send.eval():
            self.sent.clear()
            return
        mapping=self.c.op('mapping')
        rows=list(range(1,mapping.numRows))
        if not rows:
            return
        pixels=None
        if self.c.par.On.eval():
            top=self.c.op('rgba')
            if not self.c.inputs or top.height!=1:
                self.error('Connect an N x 1 RGBA TOP before enabling output')
                return
            pixels=top.numpyArray(delayed=True)
            if pixels is None:
                return
        for offset in range(len(rows)):
            idx=(self.cursor+offset)%len(rows)
            row=rows[idx]
            lid=mapping[row,1].val
            if lid not in self.lights:
                self.error('Mapping contains an unknown light ID')
                continue
            if not self.light_ready_for_send(lid,now):
                continue
            if not self.control_ready(lid):
                continue
            try:
                if not self.c.par.On.eval():
                    body={'on':{'on':False},'dynamics':{'duration':round(self.c.par.Transition.eval()*1000)}}
                else:
                    pixel=int(mapping[row,0].val)
                    if pixel<0 or pixel>=pixels.shape[1]:
                        raise ValueError('Mapping pixel index outside TOP width')
                    l=self.lights[lid]
                    body=self.core.color_body(pixels[0,pixel],self.c.par.Transition.eval(),self.c.par.Colorspace.eval()=='linear',l.get('color',{}).get('gamut'))
                    if 'color' not in l:
                        body.pop('color',None)
                    if 'dimming' not in l:
                        body.pop('dimming',None)
                    body['on']={'on':True}
                if not self.should_send(lid,body,now):
                    continue
                self.last_send[lid]=now
                self.cursor=(idx+1)%len(rows)
                self.submit_control(lid,body)
            except Exception as e:
                self.error(str(e))

    def close(self):
        self.pool.shutdown(wait=False,cancel_futures=True)
        self.control_pool.shutdown(wait=False,cancel_futures=True)
        self.discovery_pool.shutdown(wait=False,cancel_futures=True)

    def bridge_ip(self):
        value=self.c.par.Bridgeip.eval().strip()
        return value

    def receive_discovery(self):
        if not self.discovery_future or not self.discovery_future.done():
            return
        future=self.discovery_future
        self.discovery_future=None
        try:
            found,warning=future.result()
            self.bridges=found
            d=self.c.op('bridges')
            d.clear()
            d.appendRow(['bridge_id','ip','source'])
            for b in found:
                d.appendRow([b['id'],b['ip'],b['source']])
            menu=self.c.par.Bridgeip
            previous=menu.eval()
            names=[b['ip'] for b in found]
            labels=[b['ip']+' — '+b['id'] for b in found]
            if previous and previous not in names:
                names.append(previous)
                labels.append(previous+' (previous selection)')
            menu.menuNames=names
            menu.menuLabels=labels
            menu.val=previous
            self.status('discovery',str(len(found))+' Bridge(s) found' if found else 'No Bridge found; retry or enter IP manually')
            if warning:
                self.error(warning)
            # Never replace manual edits or an existing connection during discovery.
            if len(found)==1 and not self.discovery_ip and not self.bridge_ip():
                self.c.par.Bridgeip.val=found[0]['ip']
        except Exception as e:
            self.status('discovery','Discovery failed')
            self.error(str(e))
