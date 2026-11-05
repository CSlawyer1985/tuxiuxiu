"""Bounded user adjustments layered onto each photo's adaptive base recipe."""
import math

CONTROLS=[
    {'key':'exposure_ev','label':'亮度','min':-1.5,'max':1.5,'step':.05,'unit':'EV','mode':'delta','group':'明暗'},
    {'key':'shadows','label':'暗部','min':-.5,'max':1.,'step':.01,'unit':'%','mode':'delta','group':'明暗'},
    {'key':'highlights','label':'高光','min':-1.5,'max':1.5,'step':.01,'unit':'%','mode':'delta','group':'明暗'},
    {'key':'contrast','label':'对比','min':-1.2,'max':1.2,'step':.01,'unit':'%','mode':'delta','group':'明暗'},
    {'key':'warmth','label':'冷暖（正值偏暖）','min':-12,'max':12,'step':.25,'unit':'','mode':'delta','group':'颜色'},
    {'key':'tint','label':'绿红（正值偏红）','min':-12,'max':12,'step':.25,'unit':'','mode':'delta','group':'颜色'},
    {'key':'saturation','label':'饱和度','min':-.8,'max':.8,'step':.02,'unit':'%','mode':'delta','group':'颜色'},
    {'key':'skin_ev','label':'肤色提亮','min':-.6,'max':.6,'step':.01,'unit':'EV','mode':'delta','group':'皮肤与细节'},
    {'key':'skin_da','label':'肤色红润','min':-10,'max':10,'step':.25,'unit':'','mode':'delta','group':'皮肤与细节'},
    {'key':'skin_db','label':'肤色冷暖','min':-10,'max':10,'step':.25,'unit':'','mode':'delta','group':'皮肤与细节'},
    {'key':'skin_smooth','label':'磨皮强度','min':0,'max':1.,'step':.01,'unit':'%','mode':'absolute','group':'皮肤与细节'},
    {'key':'sharpen','label':'锐化强度','min':0,'max':1.5,'step':.01,'unit':'%','mode':'absolute','group':'皮肤与细节'},
]
BY_KEY={item['key']:item for item in CONTROLS}


def can_smooth():
    try:
        import cv2
        return all(callable(getattr(cv2,name,None)) for name in ('bilateralFilter','connectedComponentsWithStats'))
    except ImportError:return False


def validate(value):
    if not isinstance(value,dict) or not set(value)<=BY_KEY.keys():raise ValueError('微调参数包含未知项目')
    result={}
    for key,number in value.items():
        spec=BY_KEY[key]
        if type(number) not in (int,float) or not math.isfinite(number) or not spec['min']<=number<=spec['max']:
            raise ValueError('微调参数超出范围：'+spec['label'])
        result[key]=float(number)
    return result


def apply(base,adjustments):
    from .engine import LIMITS
    result=dict(base)
    for key,value in validate(adjustments).items():
        result[key]=value if BY_KEY[key]['mode']=='absolute' else base.get(key,0.)+value
        if not LIMITS[key][0]<=result[key]<=LIMITS[key][1]:raise ValueError('实际参数超过当前算子范围，请降低'+BY_KEY[key]['label'])
    return result


def defaults(recipe):
    return {item['key']:recipe.get(item['key'],0.) if item['mode']=='absolute' else 0. for item in CONTROLS}
