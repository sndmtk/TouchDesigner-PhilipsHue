"""HueEntertainment extension: control the component from Python.

Promoted, so call the methods on the component itself, e.g.

    hue = op('HueEntertainment')
    hue.LightOff('Hue Go')
    hue.StartStream()

Lights can be given by name (as shown on the Lights page), by entertainment
service id, or by 1-based index on the Lights page. Areas by name or id.
Everything returns immediately: Bridge requests run in the background, and
their results show up in Status, the errors DAT and the callbacks.
"""


class HueEntertainmentExt:
    def __init__(self, ownerComp):
        self.ownerComp = ownerComp

    # ------------------------------------------------------------ internals
    @property
    def ctl(self):
        return self.ownerComp.op('lifecycle').module.get_controller()

    def _area(self, ref):
        if ref is None:
            return self.ctl.selected_area()
        return next((a for a in self.ctl.areas if ref in (a['id'], a['name'])), None)

    def _use(self, lights):
        """Set the Lights in Area toggles to exactly these lights."""
        ctl = self.ctl
        wanted = set()
        for ref in lights:
            cand = ctl.find_light(ref)
            if cand is None:
                raise ValueError('Unknown light: %s' % (ref,))
            wanted.add(cand['id'])
        for i, sid in enumerate(ctl.light_ids):
            p = getattr(self.ownerComp.par, 'Use%d' % (i + 1), None)
            if p is not None:
                p.val = sid in wanted

    # ------------------------------------------------------------ state
    @property
    def Status(self):
        """The current Status message."""
        return self.ownerComp.par.Status.eval()

    @property
    def Ready(self):
        """True once paired and the areas and lights have been read."""
        return self.ctl.ready

    @property
    def Streaming(self):
        """True while the stream is running."""
        return self.ctl.sender is not None and self.ctl.stream_announced

    @property
    def Areas(self):
        """[{'id', 'name', 'lights'}, ...] with lights in left-to-right order."""
        return [self.ctl.area_info(a) for a in self.ctl.areas]

    @property
    def SelectedArea(self):
        """The selected area ({'id', 'name', 'lights'}) or None."""
        area = self.ctl.selected_area()
        return self.ctl.area_info(area) if area else None

    @property
    def Lights(self):
        """[{'id', 'name', 'status', 'on'}, ...] for every light on the Bridge.
        'on' is the Bridge's last known state (None if unknown)."""
        ctl = self.ctl
        power = ctl.bridge_power()
        out = []
        for c in ctl.candidates():
            info = ctl.light_info(c)
            info['on'] = power.get(ctl.light_rid(c))
            out.append(info)
        return out

    @property
    def Mapping(self):
        """[{'pixel', 'name', 'channel_id'}, ...] in pixel order."""
        m = self.ownerComp.op('mapping')
        out = []
        for r in range(1, m.numRows):
            try:
                out.append({'pixel': int(m[r, 0].val), 'channel_id': int(m[r, 1].val), 'name': m[r, 2].val})
            except ValueError:
                pass
        return sorted(out, key=lambda x: x['pixel'])

    # ------------------------------------------------------------ Bridge
    def Discover(self):
        """Search the network for Bridges (see onBridgesDiscovered)."""
        self.ctl.pulse('Discover')

    def Connect(self, bridgeIp=None):
        """Connect (and pair if needed), optionally to another Bridge IP."""
        if bridgeIp:
            self.ownerComp.par.Bridgeip.val = bridgeIp
        self.ctl.pulse('Connect')

    def ForgetKey(self):
        """Clear the stored keys and pair again."""
        self.ctl.pulse('Forgetkey')

    # ------------------------------------------------------------ areas
    def RefreshAreas(self):
        self.ctl.pulse('Refreshareas')

    def SelectArea(self, area):
        """Select an area by name or id."""
        a = self._area(area)
        if a is None:
            raise ValueError('Unknown area: %s' % (area,))
        self.ownerComp.par.Area.val = a['id']

    def CreateArea(self, name=None, lights=None):
        """Create an area from these lights (default: the Lights in Area toggles).
        The new area is selected when the Bridge answers (see onAreaCreated)."""
        if name:
            self.ownerComp.par.Newareaname.val = name
        if lights is not None:
            self._use(lights)
        self.ctl.pulse('Createarea')

    def UpdateArea(self, lights=None):
        """Replace the lights of the selected area (default: the Lights in Area toggles)."""
        if lights is not None:
            self._use(lights)
        self.ctl.pulse('Updatearea')

    def DeleteArea(self, area=None):
        """Delete an area (default: the selected one). No confirmation dialog."""
        if area is not None:
            self.SelectArea(area)
        self.ctl.pulse('Deletearea', confirmed=True)

    # ------------------------------------------------------------ lights
    def SearchLights(self):
        """Search for new lights (see onSearchComplete)."""
        self.ctl.pulse('Searchlights')

    def DeleteLight(self, light):
        """Remove a light from the Bridge. No confirmation dialog."""
        cand = self.ctl.find_light(light)
        if cand is None:
            raise ValueError('Unknown light: %s' % (light,))
        self.ctl.light_command('Deletedevice', True, device_id=cand['device'])

    def LightOn(self, lights=None):
        """Switch lights on over HTTPS: one light, a list, or None for all.
        Lights of a streaming area ignore it. Returns the names sent."""
        return self.ctl.set_power(lights, True)

    def LightOff(self, lights=None):
        """Switch lights off over HTTPS: one light, a list, or None for all.
        Lights of a streaming area ignore it (the stream cannot switch a light off)."""
        return self.ctl.set_power(lights, False)

    def SetLight(self, lights, on):
        """LightOn / LightOff with the state as an argument."""
        return self.ctl.set_power(lights, bool(on))

    # ------------------------------------------------------------ stream
    def StartStream(self, area=None):
        """Turn on Enable Streaming (optionally selecting an area first)."""
        if area is not None:
            self.SelectArea(area)
        self.ownerComp.par.Stream.val = True

    def StopStream(self):
        """Turn off Enable Streaming; the area is released and lights return to normal."""
        self.ownerComp.par.Stream.val = False

    def SetColorFormat(self, fmt):
        """'xy' (XY + Brightness) or 'rgb'."""
        if fmt not in ('xy', 'rgb'):
            raise ValueError("Color format must be 'xy' or 'rgb'")
        self.ownerComp.par.Colorformat.val = fmt

    # ------------------------------------------------------------ callbacks
    def CreateCallbacks(self):
        """Create a new docked callbacks DAT from the template."""
        self.ctl.pulse('Newcallbacks')
