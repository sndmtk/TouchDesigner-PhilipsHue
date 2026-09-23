import sys
import struct
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import discovery

def name(s):
    return b''.join(bytes([len(x)])+x.encode() for x in s.split('.'))+b'\0'
def rr(owner,kind,data):
    return name(owner)+struct.pack('!HHIH',kind,1,120,len(data))+data

class DiscoveryTests(unittest.TestCase):
    def test_mdns_resolves_only_hue_service(self):
        instance='Bridge._hue._tcp.local'
        packet=struct.pack('!6H',0,0x8400,0,4,0,0)
        packet+=rr('_hue._tcp.local',12,name(instance))
        packet+=rr(instance,33,struct.pack('!HHH',0,0,443)+name('bridge.local'))
        packet+=rr(instance,16,b'\x0dbridgeid=abc0')
        packet+=rr('bridge.local',1,bytes([192,168,1,5]))
        records=discovery.parse_packet(packet)
        bridges=discovery.bridges_from_records(records)
        self.assertEqual(bridges[0]['ip'],'192.168.1.5')
        self.assertEqual(bridges[0]['id'],'abc0')
    def test_bad_compression_pointer_rejected(self):
        with self.assertRaises(ValueError):
            discovery.read_name(b'\xc0\x00',0)
    def test_discovery_uses_only_mdns(self):
        source=(Path(__file__).resolve().parents[1]/'src/discovery.py').read_text(encoding='utf-8')
        self.assertNotIn('discovery.meethue.com',source)
        self.assertNotIn('urlopen',source)
    def test_no_hue_service_does_not_select_unrelated_device(self):
        packet=struct.pack('!6H',0,0x8400,0,1,0,0)+rr('printer.local',1,bytes([192,168,1,6]))
        self.assertEqual(discovery.bridges_from_records(discovery.parse_packet(packet)),[])
