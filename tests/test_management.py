import test_auth
from test_auth import Parameter
import unittest
from concurrent.futures import Future


class ManagementTests(unittest.TestCase):
    response=test_auth.AuthTests.response
    tearDown=test_auth.AuthTests.tearDown
    def setUp(self):
        test_auth.AuthTests.setUp(self)
        self.c.par.Targetlight=Parameter()
        self.c.par.Bridgeip=Parameter()
        self.c.par.Bridgeip.val=self.ctrl.config[0]
        self.c.par.Apikey.val='secret'
        self.c.par.Keyhost.val=self.ctrl.config[0]
        self.ctrl.ready=True

    def light(self, lid='a', device='device-a'):
        return {'id':lid,'metadata':{'name':lid},'owner':{'rid':device,'rtype':'device'}}

    def test_light_refresh_has_no_error_and_updates_menu(self):
        self.response('lights',{'data':[self.light()],'errors':[]})
        self.assertEqual(self.c.tables['errors'].numRows,1)
        self.assertIn('a',self.c.par.Targetlight.menuNames)

    def test_search_queues_v2_request_without_overwriting_inflight(self):
        pending=object()
        self.ctrl.pending=pending
        self.ctrl.pulse('Searchlights')
        self.assertIs(self.ctrl.pending,pending)
        self.assertEqual(self.ctrl.management[:3],('search_resources','GET','/clip/v2/resource/zigbee_device_discovery'))

    def test_search_uses_returned_resource_id(self):
        self.response('search_resources',{'data':[{'id':'discovery-id','status':'ready'}],'errors':[]})
        self.assertEqual(self.ctrl.management,('search_start','PUT','/clip/v2/resource/zigbee_device_discovery/discovery-id',{'action':{'action_type':'search'}},None))

    def test_no_discovery_resource_reports_error(self):
        self.response('search_resources',{'data':[],'errors':[]})
        self.assertGreater(self.c.tables['errors'].numRows,1)

    def test_delete_requires_confirmation_and_uses_device_owner(self):
        self.ctrl.lights={'a':self.light()}
        self.c.par.Targetlight.val='a'
        self.ctrl.pulse('Deletelight')
        self.assertIsNone(self.ctrl.management)
        self.ctrl.pulse('Deletelight',confirmed=True)
        self.assertEqual(self.ctrl.management,('delete_device','DELETE','/clip/v2/resource/device/device-a',None,'device-a'))

    def test_changed_bridge_cannot_delete_old_selection(self):
        self.ctrl.lights={'a':self.light()}
        self.c.par.Targetlight.val='a'
        self.c.par.Bridgeip.val='192.0.2.2'
        self.ctrl.pulse('Deletelight',confirmed=True)
        self.assertIsNone(self.ctrl.management)

    def test_delete_removes_all_device_mappings_preserving_other_pixels(self):
        self.ctrl.lights={'a':self.light(),'b':self.light('b'),'c':self.light('c','device-c')}
        for i,lid in enumerate(self.ctrl.lights): self.c.tables['mapping'].appendRow([i,lid,lid])
        f=Future(); f.set_result((200,{'data':[],'errors':[]}))
        self.ctrl.pending=(f,'delete_device',(self.ctrl.config,self.ctrl.generation),'device-a',None)
        self.ctrl.receive(100)
        self.assertEqual(self.c.tables['mapping'].rows[1:],[ [2,'c','c'] ])
        self.assertFalse(self.ctrl.ready)

    def test_new_lights_append_without_changing_existing_pixels(self):
        self.c.tables['mapping'].appendRow([7,'a','a'])
        self.response('lights',{'data':[self.light(),self.light('b')],'errors':[]})
        self.assertEqual(self.c.tables['mapping'].rows[1:],[ [7,'a','a'],[8,'b','b'] ])

    def test_reconnect_cancels_queued_delete(self):
        self.ctrl.management=('delete_device','DELETE','bad',None,'bad')
        self.ctrl.pulse('Connect')
        self.assertIsNone(self.ctrl.management)
