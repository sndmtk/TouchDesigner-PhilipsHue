"""Executed inside TouchDesigner. Source is embedded into the saved component."""
from pathlib import Path
import traceback

ROOT=Path(r'C:/Users/sndmt/Documents/Github Projects/TouchDesigner-PhilipsHue')

LAYOUT={
    'rgba':(-600,300), 'README':(-350,300), 'mapping':(-100,300), 'errors':(150,300), 'bridges':(400,300),
    'hue_core':(-600,100), 'runtime':(-350,100),
    'lifecycle':(-600,-100), 'controls':(-350,-100), 'discovery':(-100,-100),
    'status':(-600,-300), 'out_status':(-350,-300),
    'lights':(-600,-500), 'out_lights':(-350,-500),
}

def apply_layout(component):
    for name,(x,y) in LAYOUT.items():
        child=component.op(name)
        if child is not None:
            child.nodeX=x
            child.nodeY=y

def build():
    target=op('/project1')
    old=target.op('HueBridge')
    c=old if old and not old.op('lifecycle') else target.create(baseCOMP,'HueBridge_updated' if old else 'HueBridge')
    c.nodeX=0
    c.nodeY=0
    page=c.appendCustomPage('Bridge')
    p=page.appendStrMenu('Bridgeip',label='Bridge IP')[0]
    p.menuNames=[]
    p.menuLabels=[]
    p.default=''
    p.val=''
    page.appendPulse('Discover',label='Discover Bridges')
    p=page.appendPulse('Connect',label='Connect / Retry Pairing')[0]
    p.startSection=True
    page.appendPulse('Forgetkey',label='Forget Key / Pair Again')
    for name,label in [('Apikey','API Key'),('Keyhost','Authenticated Bridge IP')]:
        p=page.appendStr(name,label=label)[0]
        p.default=''
        p.val=''
        p.readOnly=True
    page=c.appendCustomPage('Lights')
    page.appendPulse('Searchlights',label='Search New Lights')
    p=page.appendMenu('Targetlight',label='Selected Light')[0]
    p.menuNames=['none']
    p.menuLabels=['Select a light']
    p.default='none'
    p.val='none'
    page.appendPulse('Deletelight',label='Delete Selected Device')
    p=page.appendToggle('Send',label='Enable Light Output')[0]
    p.default=False
    p=page.appendToggle('On',label='Lights On')[0]
    p.default=True
    p.val=True
    for name,label,value in [('Interval','Update Interval (s)',1),('Transition','Transition (s)',1)]:
        p=page.appendFloat(name,label=label)[0]
        p.default=value
        p.min=.1 if name=='Interval' else 0
        p.clampMin=True
        p.val=value
    p=page.appendMenu('Colorspace',label='Input Color Space')[0]
    p.menuNames=['srgb','linear']
    p.menuLabels=['sRGB','Linear sRGB']
    p.default='srgb'
    p.val='srgb'
    c.create(inTOP,'rgba')
    for name in ('hue_core','runtime','discovery'):
        c.create(textDAT,name).text=(ROOT/'src'/ (name+'.py')).read_text(encoding='utf-8')
    headers={'status':['key','value'],'lights':['id','name','on','brightness','x','y','color_supported','gamut_type','zigbee_status','updated'],
             'errors':['time','message'],'mapping':['pixel','light_id','name'],'bridges':['bridge_id','ip','source']}
    for name,header in headers.items():
        table=c.create(fifoDAT if name=='errors' else tableDAT,name)
        if name=='errors':
            table.par.clamp=True
            table.par.maxlines=10
            table.par.firstrow=True
        table.clear()
        table.appendRow(header)
    for i,name in enumerate(('status','lights')):
        out=c.create(outDAT,'out_'+name)
        out.inputConnectors[0].connect(c.op(name))
        out.par.connectorder=i
    c.create(textDAT,'README').text=(ROOT/'README.md').read_text(encoding='utf-8')
    boot=c.create(executeDAT,'lifecycle')
    boot.text='''controller = None
def get_controller():
    global controller
    if controller is None:
        controller = parent().op('runtime').module.Controller(parent())
    return controller
def onFrameStart(frame):
    try:
        get_controller().tick()
    except Exception as e:
        get_controller().error(str(e))
def onExit():
    if controller is not None:
        controller.close()
'''
    boot.par.framestart=True
    boot.par.exit=True
    callback=c.create(parameterexecuteDAT,'controls')
    callback.par.op=c
    callback.par.pars='Connect Forgetkey Discover Searchlights Deletelight'
    callback.par.onpulse=True
    callback.text=(ROOT/'src/controls.py').read_text(encoding='utf-8')
    apply_layout(c)
    c.save(str(ROOT/'HueBridge.tox'))
    if globals().get('HUE_EXPORT_ONLY',False):
        return c
    # A sample TOE is intentionally disconnected from any real Bridge.
    sample=target.op('sample_rgba') or target.create(constantTOP,'sample_rgba')
    sample.par.resolutionw=1
    sample.par.resolutionh=1
    sample.par.colorr=1
    sample.par.colorg=.3
    sample.par.colorb=.05
    sample.par.alpha=1
    c.inputConnectors[0].connect(sample)
    sample.nodeX=-250
    sample.nodeY=0
    # Exclude build automation from the portable demo.
    boot_helper=target.op('boot')
    probe_helper=target.op('probe')
    if boot_helper:
        boot_helper.par.start=False
    if probe_helper:
        probe_helper.op('watch').par.framestart=False
    if globals().get('HUE_SAVE_DEMO',False):
        project.save(str(ROOT/'HueBridge_demo.toe'))
    if probe_helper:
        probe_helper.op('watch').par.framestart=True
    errors=c.errors(recurse=True)
    (ROOT/'build/verification.txt').write_text('TouchDesigner '+str(app.version)+' build '+str(app.build)+'\nErrors: '+str(errors)+'\n',encoding='utf-8')
    errorfile=ROOT/'build/build_error.txt'
    if errorfile.exists():
        errorfile.unlink()
    return c

try:
    built_component=build()
except Exception:
    (ROOT/'build/build_error.txt').write_text(traceback.format_exc(),encoding='utf-8')
