"""Run in the current TouchDesigner project to update HueBridge in place."""
from pathlib import Path
import json
import traceback

ROOT=Path(r'C:/Users/sndmt/Documents/Github Projects/TouchDesigner-PhilipsHue')
# Keep in sync with OUTPUT_PARS in build_component.py: (name, label, default, min, max).
OUTPUT_PARS=[
    ('Maxrate','Max Commands/s',10,1,40),
    ('Interval','Min Interval per Light (s)',0.1,0,None),
    ('Refresh','Refresh Unchanged (s)',2,0,None),
    ('Transition','Transition (s)',1,0,None),
]

def find_component():
    """The HueBridge in /project1, whatever it was renamed to."""
    found=[o for o in op('/project1').children if o.isCOMP and o.op('lifecycle') and o.op('runtime') and o.op('hue_core')]
    if len(found)!=1:
        raise RuntimeError('Expected exactly one HueBridge component in /project1, found: '+str([o.path for o in found]))
    return found[0]

def update_output_pars(c, page):
    for name,label,default,lo,hi in OUTPUT_PARS:
        p=getattr(c.par,name,None)
        if p is None:
            p=page.appendFloat(name,label=label)[0]
            p.val=default
        elif name=='Interval' and p.eval()==1:
            # Old meaning was "resend every N s" (default 1). It is now the minimum
            # gap between commands to one light, so the old default would cap each light at 1/s.
            p.val=default
        p.label=label
        p.default=default
        p.min=lo
        p.clampMin=True
        if hi is not None:
            p.max=hi
            p.clampMax=True

try:
    c=find_component()
    lifecycle=c.op('lifecycle')
    lifecycle.par.framestart=False
    previous=lifecycle.module.controller
    if previous is not None:
        previous.close()
    if getattr(c.par,'Allowselfsigned',None) is not None:
        c.par.Allowselfsigned.destroy()
    if getattr(c.par,'Cloudfallback',None) is not None:
        c.par.Cloudfallback.destroy()
    c.par.Connect.startSection=True
    c.par.Bridgeip.menuNames=[]
    c.par.Bridgeip.menuLabels=[]
    if c.par.Bridgeip.eval()=='manual':
        c.par.Bridgeip.val=''
    page=next(p for p in c.customPages if p.name=='Lights')
    if getattr(c.par,'Searchlights',None) is None:
        page.appendPulse('Searchlights',label='Search New Lights')
        p=page.appendMenu('Targetlight',label='Selected Light')[0]
        p.menuNames=['none']
        p.menuLabels=['Select a light']
        p.default='none'
        p.val='none'
        page.appendPulse('Deletelight',label='Delete Selected Device')
    update_output_pars(c,page)
    c.op('hue_core').text=(ROOT/'src/hue_core.py').read_text(encoding='utf-8')
    c.op('runtime').text=(ROOT/'src/runtime.py').read_text(encoding='utf-8')
    status=c.op('status')
    for row in reversed(range(1,status.numRows)):
        key=status[row,0].val
        if key!='state':
            status.deleteRow(row)
    old_errors=c.op('errors')
    if old_errors.type!='fifoDAT':
        old_errors.destroy()
        errors=c.create(fifoDAT,'errors')
        errors.par.clamp=True
        errors.par.maxlines=10
        errors.par.firstrow=True
        errors.appendRow(['time','message'])
    for name in ('out_errors','out_bridges','out_devices','devices'):
        obsolete=c.op(name)
        if obsolete is not None:
            obsolete.destroy()
    controls=c.op('controls')
    controls.par.pars='Connect Forgetkey Discover Searchlights Deletelight'
    controls.par.onpulse=True
    controls.text=(ROOT/'src/controls.py').read_text(encoding='utf-8')
    c.op('README').text=(ROOT/'README.md').read_text(encoding='utf-8')
    layout={
        'rgba':(-600,300), 'README':(-350,300), 'mapping':(-100,300), 'errors':(150,300), 'bridges':(400,300),
        'hue_core':(-600,100), 'runtime':(-350,100),
        'lifecycle':(-600,-100), 'controls':(-350,-100), 'discovery':(-100,-100),
        'status':(-600,-300), 'out_status':(-350,-300),
        'lights':(-600,-500), 'out_lights':(-350,-500),
    }
    for name,(x,y) in layout.items():
        child=c.op(name)
        if child is not None:
            child.nodeX=x
            child.nodeY=y
    lifecycle.module.controller=None
    lifecycle.par.framestart=True
    # Export a separate, unpaired component; never serialize live credentials.
    namespace={'__name__':'hue_builder','HUE_EXPORT_ONLY':True}
    exec(compile((ROOT/'src/build_component.py').read_text(encoding='utf-8'),'build_component.py','exec'),namespace)
    exported=namespace.get('built_component')
    if exported is None:
        raise RuntimeError('TOX export failed; see build/build_error.txt')
    exported.destroy()
    c.openParameters()
    (ROOT/'build/management_update.json').write_text(json.dumps({
        'component':c.path,'parameters':[c.par.Searchlights.name,c.par.Targetlight.name,c.par.Deletelight.name],
        'key_readonly':c.par.Apikey.readOnly,'errors':c.errors(recurse=True),
        'error_dat_type':c.op('errors').type,'error_maxlines':c.op('errors').par.maxlines.eval()
    }),encoding='utf-8')
except Exception:
    (ROOT/'build/management_update_error.txt').write_text(traceback.format_exc(),encoding='utf-8')
    raise
