import sys
import unittest
from pathlib import Path
from concurrent.futures import Future
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import hue_core
from runtime import Controller

class Cell:
    def __init__(self,value): self.val=str(value)

class Table:
    def __init__(self,headers): self.rows=[headers]
    @property
    def numRows(self): return len(self.rows)
    def __getitem__(self,pos): return Cell(self.rows[pos[0]][pos[1]])
    def __setitem__(self,pos,val): self.rows[pos[0]][pos[1]]=str(val)
    def appendRow(self,row): self.rows.append(list(row))
    def deleteRow(self,row): del self.rows[row]
    def clear(self): self.rows=[]

class Component:
    def __init__(self):
        self.storage={}
        self.tables={k:Table(v) for k,v in {'status':['key','value'],'errors':['time','message'],'lights':['id'],'mapping':['pixel','id','name']}.items()}
        self.par=SimpleNamespace(Apikey=Parameter(),Keyhost=Parameter())
        self.saved=None
    def op(self,name):
        return SimpleNamespace(module=hue_core) if name=='hue_core' else self.tables[name]
    def fetch(self,k,d=None): return self.storage.get(k,d)
    def store(self,k,v): self.storage[k]=v
    def save(self,path): self.saved=(path,dict(self.storage))

class Parameter:
    def __init__(self): self.val=''
    def eval(self): return self.val

class AuthTests(unittest.TestCase):
    def setUp(self):
        self.c=Component()
        self.c.par.Interval=Parameter()
        self.c.par.Interval.val=1
        self.ctrl=Controller(self.c)
        self.ctrl.config=('192.168.1.2',False)
    def tearDown(self): self.ctrl.close()
    def response(self,kind,value,code=200,token=None):
        f=Future()
        f.set_result((code,value))
        self.ctrl.pending=(f,kind,token or (self.ctrl.config,self.ctrl.generation),None,None)
        self.ctrl.receive(100)
    def test_button_wait_does_not_save_key(self):
        self.response('auth',[{'error':{'type':101,'description':'link button not pressed'}}])
        self.assertNotIn('application_key',self.c.storage)
        self.assertTrue(any('Press the physical' in str(r) for r in self.c.tables['status'].rows))
    def test_success_persists_key(self):
        self.response('auth',[{'success':{'username':'secret'}}])
        self.assertEqual(self.c.par.Apikey.eval(),'secret')
        self.assertEqual(self.c.par.Keyhost.eval(),'192.168.1.2')
        self.assertIsNone(self.c.saved)
        self.assertNotIn('application_key',self.c.storage)
    def test_old_auth_response_after_reset_is_ignored(self):
        token=(self.ctrl.config,self.ctrl.generation)
        self.ctrl.pulse('Forgetkey')
        self.response('auth',[{'success':{'username':'stale'}}],token=token)
        self.assertEqual(self.c.par.Apikey.eval(),'')
    def test_api_errors_do_not_mark_ready(self):
        self.response('lights',{'errors':[{'description':'unavailable'}],'data':[]})
        self.assertFalse(self.ctrl.ready)
        self.assertEqual(self.c.tables['errors'].numRows,2)
    def test_ambiguous_put_failure_invalidates_cached_power(self):
        self.ctrl.sent['abc']={'on':{'on':True}}
        f=Future()
        f.set_exception(TimeoutError('response lost after Bridge applied OFF'))
        self.ctrl.pending=(f,'put',(self.ctrl.config,self.ctrl.generation),'abc',{'on':{'on':False}})
        self.ctrl.receive(100)
        self.assertNotIn('abc',self.ctrl.sent)

    def test_unreachable_light_does_not_pause_other_lights(self):
        body={'on':{'on':True}}
        f=Future()
        f.set_result((200,{'errors':[{'description':'communication_error'}],'data':[]}))
        self.ctrl.pending=(f,'put',(self.ctrl.config,self.ctrl.generation),'offline',body)
        self.ctrl.receive(100)
        self.assertEqual(self.ctrl.next_auth,0)
        self.assertFalse(self.ctrl.light_ready_for_send('offline',104.9))
        self.assertTrue(self.ctrl.light_ready_for_send('offline',105))

    def test_error_is_not_copied_to_status(self):
        self.ctrl.error('network unavailable')
        self.assertFalse(any(row[0]=='last_error' for row in self.c.tables['status'].rows))
        self.assertEqual(self.c.tables['errors'].rows[-1][1],'network unavailable')

    def test_status_contains_only_current_message(self):
        self.ctrl.status('credentials','API Key updated')
        self.ctrl.status('light_count',2)
        self.ctrl.status('last_send','12:34:56')
        rows=dict(self.c.tables['status'].rows[1:])
        self.assertEqual(set(rows),{'state'})
        self.assertEqual(rows['state'],'API Key updated')

    def test_bridge_requests_always_allow_the_bridge_certificate(self):
        root=Path(__file__).resolve().parents[1]
        runtime_source=(root/'src/runtime.py').read_text(encoding='utf-8')
        builder_source=(root/'src/build_component.py').read_text(encoding='utf-8')
        self.assertNotIn('Allowselfsigned',runtime_source)
        self.assertNotIn('Allowselfsigned',builder_source)
        self.assertIn('config=(host,True)',runtime_source)

    def test_bridge_ui_has_no_cloud_fallback_or_manual_menu_item(self):
        root=Path(__file__).resolve().parents[1]
        builder_source=(root/'src/build_component.py').read_text(encoding='utf-8')
        runtime_source=(root/'src/runtime.py').read_text(encoding='utf-8')
        self.assertNotIn('Cloudfallback',builder_source)
        self.assertNotIn("['manual']",builder_source)
        self.assertNotIn('Cloudfallback',runtime_source)
        self.assertNotIn("value=='manual'",runtime_source)
        self.assertIn("p.startSection=True",builder_source)

    def test_unchanged_light_is_resent_after_its_update_interval(self):
        body={'on':{'on':True}}
        self.ctrl.sent['a']=body
        self.ctrl.last_send['a']=99
        self.assertFalse(self.ctrl.should_send('a',body,99.9))
        self.assertTrue(self.ctrl.should_send('a',body,100))

    def test_inflight_light_does_not_block_another_light(self):
        self.ctrl.control_pending['a']=object()
        self.assertFalse(self.ctrl.control_ready('a'))
        self.assertTrue(self.ctrl.control_ready('b'))
    def test_unauthorized_stops_retry_until_explicit_action(self):
        self.response('lights',{'errors':[]},403)
        self.assertTrue(self.ctrl.auth_stopped)
    def test_light_list_initializes_mapping_once(self):
        light={'id':'abc','metadata':{'name':'Desk'},'on':{'on':True}}
        self.response('lights',{'errors':[],'data':[light]})
        self.assertTrue(self.ctrl.ready)
        self.assertEqual(self.c.tables['mapping'].rows[1],[0,'abc','Desk'])
        self.response('lights',{'errors':[],'data':[]})
        self.assertEqual(self.c.tables['mapping'].rows[1],[0,'abc','Desk'])

    def test_zigbee_status_is_joined_without_device_or_battery_columns(self):
        light={'id':'abc','metadata':{'name':'Desk'},'owner':{'rid':'device-1'},'on':{'on':True},'dimming':{'brightness':50},'color':{'gamut_type':'C'}}
        self.response('lights',{'errors':[],'data':[light]})
        self.response('zigbee_connectivity',{'errors':[],'data':[{'owner':{'rid':'device-1'},'status':'connected'}]})
        header=self.c.tables['lights'].rows[0]
        row=dict(zip(header,self.c.tables['lights'].rows[1]))
        self.assertEqual(row['zigbee_status'],'connected')
        self.assertNotIn('mirek',header)
        self.assertNotIn('battery_level',header)
    def test_new_bridge_rebuilds_mapping(self):
        self.c.store('mapping_host','192.168.1.99')
        self.c.tables['mapping'].appendRow([0,'old','Old'])
        self.response('lights',{'errors':[],'data':[{'id':'new','metadata':{'name':'New'}}]})
        self.assertEqual(self.c.tables['mapping'].rows[1],[0,'new','New'])

    def discovery_response(self, found):
        self.c.tables['bridges']=Table(['bridge_id','ip','source'])
        f=Future()
        f.set_result((found,''))
        self.ctrl.discovery_future=f
        self.ctrl.receive_discovery()

    def test_discovery_preserves_manual_edit_during_search(self):
        self.c.par.Bridgeip=Parameter()
        self.c.par.Bridgeip.val='192.0.2.99'
        self.ctrl.discovery_ip=''
        self.discovery_response([{'id':'a','ip':'192.0.2.10','source':'mDNS'}])
        self.assertEqual(self.ctrl.bridge_ip(),'192.0.2.99')
        self.assertIn('192.0.2.10',self.c.par.Bridgeip.menuNames)

    def test_single_bridge_populates_empty_strmenu(self):
        self.c.par.Bridgeip=Parameter()
        self.discovery_response([{'id':'a','ip':'192.0.2.10','source':'mDNS'}])
        self.assertEqual(self.ctrl.bridge_ip(),'192.0.2.10')

    def test_multiple_bridges_wait_for_selection(self):
        self.c.par.Bridgeip=Parameter()
        self.discovery_response([{'id':'a','ip':'192.0.2.10','source':'mDNS'}, {'id':'b','ip':'192.0.2.11','source':'mDNS'}])
        self.assertEqual(self.ctrl.bridge_ip(),'')
        self.assertEqual(self.c.par.Bridgeip.menuNames,['192.0.2.10','192.0.2.11'])

if __name__=='__main__': unittest.main()
