"""TouchDesigner Parameter Execute callbacks."""
def onPulse(par):
    controller=parent().op('lifecycle').module.get_controller()
    if par.name=='Deletelight':
        selected=parent().par.Targetlight.eval()
        light=controller.lights.get(selected)
        if not light:
            controller.error('Select an available light first')
            return
        device=light.get('owner',{}).get('rid','')
        names=[l.get('metadata',{}).get('name',lid) for lid,l in controller.lights.items()
               if l.get('owner',{}).get('rid')==device]
        message='Remove this device from the Hue Bridge?\n\n'+ '\n'.join(names)
        message+='\n\nDevice: '+device+'\nAll lights on this device will be removed. Re-pair to use again.'
        if ui.messageBox('Delete Hue device',message,buttons=['Cancel','Delete Device'])!=1:
            controller.status('light_delete','Cancelled')
            return
        controller.pulse(par.name,confirmed=True)
    else:
        controller.pulse(par.name)
