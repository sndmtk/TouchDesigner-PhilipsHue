# TouchDesigner × Philips Hue (HueEntertainment)

A TouchDesigner component that drives Philips Hue lights in real time from a TOP, using the Hue **Entertainment API**. Each pixel of a one-row image is assigned to a light, and the colours of all lights are sent together in one packet up to 60 times per second, so **all lights change at the same moment** with low latency.

Drag `HueEntertainment.tox` into a network to use it. No extra Python packages are needed: the DTLS encryption the Entertainment API requires is implemented with the Python standard library.

Everything is done from the component's custom parameters: finding and pairing the Bridge, creating, updating and deleting Entertainment areas, adding new lights, removing lights, and checking their connection.

## Requirements

- A Hue Bridge with the Hue API v2 (Entertainment)
- Colour-capable Hue lights (up to 10 per Entertainment area)
- A network where the PC can reach the Bridge over **HTTPS (TCP 443) and UDP port 2100**

## Quick start

1. On the **Bridge** page, press **Discover Bridges** or type the **Bridge IP**.
2. Pairing starts automatically. When **Status** shows `Press the physical Bridge link button`, press the link button on the Bridge within 60 seconds. The **API Key** and **Client Key** (the stream encryption key) are stored on the component.
3. The Entertainment areas are loaded. If there is exactly one, it is selected. Otherwise pick one in **Entertainment Area** on the **Area** page.
   - **No area yet?** Under **Lights in Area** on the Area page, every light has a toggle and all of them start on. Turn off the lights you do not want and press **Create Area**. The new area is created and selected.
4. Connect a **N × 1 RGBA TOP** to the input. The lights are ordered by name: pixel 0 drives the first light, pixel 1 the next, and so on.
5. On the **Stream** page, turn on **Enable Streaming**. **Status** shows `Streaming N channel(s) at 50 fps`.
6. **Save the TOE (Ctrl+S)** to keep the keys.

Turning streaming off releases the area and the lights return to normal control. This also happens when TouchDesigner closes.

The component's node viewer shows the error log, so problems are visible directly in the network.

When the component is placed, a callbacks DAT `<name>_callbacks` is created below it and **docked** to it (it moves, copies and is deleted together with the component). It holds Python callbacks for events, like an Execute DAT (see [Callbacks](#callbacks)).

## Parameters

### Bridge

| Parameter | Description |
| --- | --- |
| Bridge IP | IPv4 address of the Bridge. Discovered Bridges appear in the menu as `IP - Bridge ID` |
| Discover Bridges | Searches the local network for Bridges (about 3 s). If exactly one is found and Bridge IP is empty, it is filled in |
| Connect / Retry Pairing | Reconnects and restarts pairing |
| Forget Key / Pair Again | Clears the stored keys and pairs again |
| API Key / Client Key (PSK) / Authenticated Bridge IP | Values from pairing (read-only) |
| Status | The current message: connection, pairing, area and light operations, streaming (read-only) |

### Area

| Parameter | Default | Description |
| --- | --- | --- |
| Entertainment Area | - | The area to stream to |
| Refresh Areas | - | Reloads the areas and resets **Lights in Area** to the selected area |
| New Area Name | TouchDesigner | Name used by Create Area |
| Create Area | - | Creates a new area from the lights switched on under **Lights in Area** and selects it |
| Update Selected Area | - | Replaces the lights of the selected area with **Lights in Area**. The area keeps its ID. If streaming, it stops, updates, and restarts by itself |
| Delete Selected Area | - | After confirmation, deletes the selected area from the Bridge (the lights stay registered) |
| Lights in Area | - | One toggle per light: on = in the area. Lights are placed by name, evenly from left (x = -1) to right (x = 1), so the name order is also the pixel order. The toggles show the selected area; your changes are kept until you select another area or press Refresh Areas. Lights newly added to the Bridge start switched on (they join the area when you press Update Selected Area). Without any area, all lights start on |

### Lights

| Parameter | Description |
| --- | --- |
| Search New Lights | Registers new lights with the Bridge. Power the lights on first. New lights appear in the list below and, switched on, in **Lights in Area**. The search is followed for up to about 2 minutes |
| Registered Lights | One read-only row per light on the Bridge: on/off (as last read from the Bridge), Zigbee status (`connected`, `connectivity_issue`, `disconnected`, ...) and, for lights in the selected area, its pixel. Refreshed about every 10 s. To switch lights on or off, use the extension (`LightOn` / `LightOff`, see [Python extension](#python-extension)) |
| Device | The light to delete |
| Delete Selected Device | After confirmation, removes the light from the Bridge. It also leaves its areas, and the pixels are renumbered. To use it again, search for it again |

### Stream

| Parameter | Default | Description |
| --- | --- | --- |
| Enable Streaming | Off | Starts and stops streaming |
| Send Rate (fps) | 50 | Packets per second (10-60). The Bridge updates the lights at about 25 Hz; sending more often covers lost UDP packets |
| Smoothing (s) | 0 | Eases colour and brightness changes. About 95% of a change is reached after this many seconds. Blending is done in linear light, so fades to black keep their hue. 0 = off (send the input as is). Leave at 0 if the input is already smoothed in TouchDesigner |
| Stream Color Format | XY + Brightness | How colours are sent. `XY + Brightness`: the component converts each pixel to a CIE xy colour and a brightness (Brightness From applies directly). `RGB`: 16-bit R, G, B (sRGB-encoded, scaled by brightness) and the Bridge converts them for each light. Compare both on your lights |
| Input Color Space | sRGB | Colour space of the input TOP (sRGB / Linear sRGB) |
| Brightness From | Alpha x RGB Value | `Alpha x RGB Value`: brightness is alpha times the largest of R, G, B, so darker pixels give dimmer lights (good for video). `Alpha Only`: alpha sets brightness and RGB only sets the colour |

### Setup

| Parameter | Description |
| --- | --- |
| Callbacks DAT | The DAT whose callback functions are called. Created once when the component is placed; if you delete it or clear this parameter, callbacks are off and it is not created again. Point it at another Text DAT to share callbacks |
| Create Callbacks DAT | Creates a new docked callbacks DAT from the template and selects it |

## Inside the component

| Operator | Purpose |
| --- | --- |
| `rgba` (input TOP) | The N × 1 RGBA image |
| `errors` | The last 10 errors (keys are redacted). Shown in the component's node viewer. Also reports when a light of the selected area loses its Zigbee connection |
| `mapping` | Pixel to light table (`pixel`, `channel_id`, `name`, `light_key`). Maintained automatically; can be edited to change the pixel order (see below) |
| `dtls_psk`, `ent_core`, `ent_runtime` | The code |
| `ent_ext` | The Python extension (see [Python extension](#python-extension)) |
| `lifecycle`, `controls` | Per-frame update and button callbacks |
| `README` | This document |

## Pixel numbering

`mapping` (inside the component) always holds **every light of the area exactly once**, and `pixel` is always **0 to (number of lights - 1), without gaps or duplicates**. It is renumbered automatically:

- Lights are identified by `light_key` (a stable ID of the light). If the Bridge renumbers its channels, each pixel stays with the same light (`channel_id` is updated for you).
- A light that leaves the area is removed and the rest are renumbered in their current order.
- A light that joins the area is added at the end and everything is renumbered.
- To reorder by hand, change a `pixel` value. For example `1.5` moves a light between 1 and 2, and the table is immediately renumbered 0, 1, 2, ... If two rows share a number, the upper row comes first.
- When an area is selected, created or updated, the order is rebuilt from left to right (for areas made by this component: by light name).
- Deleting a row has no lasting effect (it comes back). To stop controlling a light, switch it off under **Lights in Area** and press **Update Selected Area**.

## Adding and removing lights

| Task | Steps |
| --- | --- |
| **Add a new light** | Power the light on and press **Search New Lights**. It appears under **Lights in Area** already switched on; press **Update Selected Area** |
| **Take a light out of the area** | Switch it off under **Lights in Area** and press **Update Selected Area**. It stays registered on the Bridge |
| **Remove a light from the Bridge** | Select it in **Device** and press **Delete Selected Device** |
| **Change the order** | The order follows the light names: rename lights in the Hue app, or edit `pixel` in `mapping` |

An area holds at most 10 lights.

## Python extension

The component has a promoted extension, so its methods are called on the component itself:

```python
hue = op('HueEntertainment')
hue.LightOff('Hue Go')          # one light, a list, or nothing for all lights
hue.StartStream('Stage')        # select an area and turn on Enable Streaming
print(hue.Lights)
```

Lights are given by name (as on the Lights page, case-insensitive), entertainment service id, or 1-based index. Areas by name or id. Calls return immediately: Bridge requests run in the background and their results appear in Status, `errors` and the callbacks.

| Member | Description |
| --- | --- |
| `Status`, `Ready`, `Streaming` | Current Status message; paired and lists read; stream running |
| `Areas`, `SelectedArea` | `[{'id', 'name', 'lights'}, ...]` (lights left to right) / the selected one or `None` |
| `Lights` | `[{'id', 'name', 'status', 'on'}, ...]`; `on` is the Bridge's last known state |
| `Mapping` | `[{'pixel', 'name', 'channel_id'}, ...]` in pixel order |
| `Discover()`, `Connect(bridgeIp=None)`, `ForgetKey()` | Bridge discovery, connection and pairing |
| `RefreshAreas()`, `SelectArea(area)` | Reload areas; select one |
| `CreateArea(name=None, lights=None)`, `UpdateArea(lights=None)` | Create / update from these lights (default: the Lights in Area toggles) |
| `DeleteArea(area=None)`, `DeleteLight(light)` | Delete without a confirmation dialog |
| `SearchLights()` | Search for new lights |
| `LightOn(lights=None)`, `LightOff(lights=None)`, `SetLight(lights, on)` | Switch lights over HTTPS whatever their current state. Lights of a streaming area ignore it (the stream owns them, and the stream itself cannot switch a light off) |
| `StartStream(area=None)`, `StopStream()` | Enable Streaming on / off |
| `SetColorFormat('xy' or 'rgb')` | Stream Color Format |
| `CreateCallbacks()` | Same as Create Callbacks DAT |

## Callbacks

`<name>_callbacks` (a Text DAT, Content Language: Python) works like the callbacks of an Execute DAT: define the functions you need and they are called on TouchDesigner's main thread. Every function receives the component as `comp`. Remove the ones you do not use. If a callback raises an error, it is written to `errors` and the component keeps running.

| Callback | When |
| --- | --- |
| `onBridgesDiscovered(comp, bridges)` | Discover Bridges finished. `bridges`: `[{'id', 'ip'}, ...]` |
| `onPaired(comp, bridgeIp)` | Pairing succeeded (new API key and client key) |
| `onConnected(comp, bridgeIp)` | Areas and lights were read for the first time after connecting |
| `onAreasChanged(comp, areas)` | After connecting, and whenever the areas on the Bridge change. `areas`: `[{'id', 'name', 'lights'}, ...]`, `lights` in left-to-right order |
| `onLightsChanged(comp, lights)` | After connecting, and whenever lights are added, removed or renamed. `lights`: `[{'id', 'name', 'status'}, ...]` |
| `onLightStatusChanged(comp, light, status, previous)` | A light's Zigbee status changed, e.g. `connected` to `connectivity_issue` |
| `onSearchComplete(comp, newLights)` | Search New Lights finished. `newLights`: the lights that were not there before |
| `onAreaCreated(comp, area)` / `onAreaUpdated(comp, area)` | Create Area / Update Selected Area succeeded (called once the new state has been read) |
| `onAreaDeleted(comp, areaName)` / `onLightDeleted(comp, lightName)` | Delete Selected Area / Delete Selected Device succeeded |
| `onStreamStart(comp, area)` | The stream is running; the lights follow the input TOP |
| `onStreamStop(comp, area, reason)` | The stream stopped. `reason`: `disabled`, `area changed`, `area update`, `area deleted`, `light deleted`, `reconnect`, or `lost: <error>` (it retries after 3 s) |
| `onMappingChanged(comp, mapping)` | The pixel order changed (lights added or removed, area updated, or a pixel edited). `mapping`: `[{'pixel', 'name', 'channel_id'}, ...]` in pixel order |
| `onError(comp, message)` | A message was added to `errors` |

There is no per-frame callback: use a CHOP or an Execute DAT for that.

Example: show the stream state in a Text TOP.

```python
def onStreamStart(comp, area):
	op('stream_state').par.text = 'Streaming ' + area['name']

def onStreamStop(comp, area, reason):
	op('stream_state').par.text = 'Stopped: ' + reason
```

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| No Bridge found | Is the PC on the same network as the Bridge? Type the Bridge's IPv4 address into Bridge IP |
| Pairing times out | Press Connect / Retry Pairing and press the Bridge's link button within 60 s |
| Discovered, but cannot connect | With several network adapters (e.g. Wi-Fi and Ethernet), traffic to the Bridge may take the wrong route. Check with `Find-NetRoute -RemoteIPAddress <Bridge IP>` in PowerShell |
| `No Entertainment areas yet` | Check the toggles under Lights in Area and press Create Area |
| No lights under Lights in Area | Are colour-capable lights registered on the Bridge? Register them with Search New Lights, then press Refresh Areas |
| `No light is selected` | Switch on at least one light under Lights in Area |
| `Could not create/update/delete ...` | Check `errors`. An area holds at most 10 lights (switch some off) |
| `Could not start the area` | Is another app (Hue Sync, another TouchDesigner) streaming on the same Bridge? Only one area per Bridge can stream at a time |
| `Stream lost: Handshake timed out` | Is UDP 2100 blocked by the PC's firewall? Is the Bridge IP correct? |
| `Stream lost: No Finished from Bridge` | The Client Key may not match. Use Forget Key / Pair Again |
| Some lights do not react | Check their row under Registered Lights (should be `connected`) and `errors` for `not reachable`. Check power and distance to the Bridge |
| The callbacks DAT is missing | A deleted callbacks DAT is not created again: press **Create Callbacks DAT** on the Setup page |
| A light keeps an old colour | Is the input TOP at least as wide as the number of lights in the area? |

## Communication and limits

- Management (pairing, reading areas and lights, area and light operations) uses the Hue API v2 over HTTPS on one kept-alive connection. Light data uses the Entertainment API over DTLS on UDP 2100, sent by a dedicated thread at a steady rate. The stream keeps running even if TouchDesigner stalls, so the Bridge never times it out.
- Areas, lights and their Zigbee status are refreshed about every 10 s, also while streaming.
- Nothing waits on TouchDesigner's thread: HTTPS requests and Bridge discovery run on worker threads, the stream (including its encryption) on its own thread, and releasing the area on exit or rebuild on a short background thread. Per frame, the component only reads the input TOP and builds one small packet. Your callbacks run on TouchDesigner's thread, so keep them short.
- Only one area per Bridge can stream at a time. While streaming, lights in the area cannot be controlled by other apps.
- HTTPS certificate verification for the Bridge is currently disabled. Use the component on a trusted local network.

## Saving and sharing

The API Key and Client Key are stored in custom parameters, so **saving the TOE saves the keys**. Read-only only prevents editing in the UI; it is not encryption. Do not publish a paired TOE or a TOX you exported by hand. The `HueEntertainment.tox` in this repository is exported unpaired by the builder.

## Development

| File | Purpose |
| --- | --- |
| HueEntertainment.tox | The component (unpaired) |
| src/dtls_psk.py | DTLS 1.2 client (PSK, TLS_PSK_WITH_AES_128_GCM_SHA256 only) with AES-128, GCM and the TLS 1.2 PRF in pure Python |
| src/ent_core.py | HueStream v2 packets, colour conversion and smoothing, area and mapping helpers, the HTTPS client, mDNS Bridge discovery |
| src/ent_runtime.py | Pairing, area and light management, the parameter-based light lists, stream start/stop and the sender thread |
| src/ent_ext.py | The promoted Python extension (`LightOn`, `StartStream`, ...) |
| src/build_entertainment.py | Creates or updates the component inside TouchDesigner and exports the unpaired TOX |
| tests/ | Unit tests. With the `openssl` command available, a real DTLS handshake is tested too |

### Building or updating the TOX

Run this in TouchDesigner's Textport (Alt+T). Set `ROOT` in `src/build_entertainment.py` to where the repository is.

```python
exec(open(r'C:/Users/sndmt/Documents/Github Projects/TouchDesigner-PhilipsHue/src/build_entertainment.py', encoding='utf-8').read())
```

If `/project1/HueEntertainment` does not exist it is created. Otherwise its code, parameters and operators are updated in place, keeping the keys, settings, area and mapping. The callbacks DAT is created next to the component if it has never had one. An unpaired `HueEntertainment.tox` is also written to the repository root. The result is written to `build/entertainment_build.json`, or `build/entertainment_build_error.txt` on failure.

### Tests

Run from the repository root (TouchDesigner's bundled Python works too):

```sh
python -m unittest discover -s tests
```
