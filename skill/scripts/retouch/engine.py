"""Traditional sRGB pixel operators. No generative models or geometry edits."""
import hashlib
import io
import math
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import numpy as np
from PIL import Image, ImageCms, ImageDraw, ImageFilter, ImageOps

from .common import canonical_hash, load_json, save_json, sha256, write_new

ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
LIMITS = {'exposure_ev': (-2., 2.), 'shadows': (-.8, 1.4), 'highlights': (-1.8, 1.8),
          'contrast': (-1.6, 1.6), 'skin_ev': (-.9, .9), 'skin_da': (-14., 14.),
          'skin_db': (-14., 14.), 'skin_smooth': (0., 1.), 'sharpen': (0., 1.5),
          'saturation':(-.9,1.),'warmth':(-16.,16.),'tint':(-16.,16.)}


def load_canonical(path, assume_srgb=False):
    with Image.open(path) as source:
        source.load()
        if source.format not in {'JPEG', 'PNG', 'TIFF', 'WEBP'}:
            raise ValueError('此解码流程尚未验证该格式')
        if source.mode not in {'RGB', 'L', 'CMYK'}:
            raise ValueError('透明／高位深图片需要单独确认，不能自动铺底或降位深')
        if source.format == 'TIFF' and any(v != 8 for v in source.tag_v2.get(258, (8,))):
            raise ValueError('当前渲染器不静默降低 TIFF 位深')
        exif = source.getexif()
        original_orientation = exif.get(274, 1)
        declared = exif.get(40961)
        if 34665 in exif: declared = exif.get_ifd(34665).get(40961, declared)
        profile = source.info.get('icc_profile')
        normalized = ImageOps.exif_transpose(source)
        if profile:
            try:
                image = ImageCms.profileToProfile(normalized, ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                                                  ImageCms.createProfile('sRGB'), outputMode='RGB')
            except Exception as exc:
                raise ValueError('嵌入 ICC 无法可靠转换，需单独处理') from exc
            input_color = 'embedded ICC converted to sRGB'
        elif declared == 1 and source.mode in {'RGB', 'L'}:
            image = normalized.convert('RGB'); input_color = 'Exif declares sRGB'
        elif assume_srgb and source.mode in {'RGB', 'L'}:
            image = normalized.convert('RGB'); input_color = 'explicitly assumed sRGB'
        else:
            raise ValueError('未确认原片颜色空间；可明确选择按 sRGB 假设后重试')
        exif[274] = 1
        if 34665 in exif: exif.get_ifd(34665)[40961] = 1
        metadata = {'input_color': input_color, 'input_icc_sha256': hashlib.sha256(profile).hexdigest() if profile else None,
                    'source_format': source.format, 'source_mode': source.mode,
                    'source_orientation': original_orientation, 'stored_size': list(source.size),
                    'working_size': list(image.size), 'working_space': 'sRGB encoded RGB float32',
                    'exif': exif.tobytes()}
    return image, metadata


def decode_srgb(a):
    return np.where(a <= .04045, a / 12.92, ((a + .055) / 1.055) ** 2.4)


def encode_srgb(a):
    return np.where(a <= .0031308, a * 12.92, 1.055 * np.maximum(a, 0) ** (1 / 2.4) - .055)


def rgb_to_lab(rgb):
    xyz = decode_srgb(rgb) @ np.array([[.4124564, .3575761, .1804375],
                                     [.2126729, .7151522, .0721750], [.0193339, .1191920, .9503041]], np.float32).T
    xyz /= np.array([.95047, 1., 1.08883], np.float32)
    delta = 6 / 29
    f = np.where(xyz > delta ** 3, np.cbrt(xyz), xyz / (3 * delta ** 2) + 4 / 29)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]),
                     200 * (f[..., 1] - f[..., 2])], axis=-1)


def lab_to_rgb(lab):
    fy = (lab[..., 0] + 16) / 116
    f = np.stack([fy + lab[..., 1] / 500, fy, fy - lab[..., 2] / 200], axis=-1)
    delta = 6 / 29
    xyz = np.where(f > delta, f ** 3, 3 * delta ** 2 * (f - 4 / 29))
    xyz *= np.array([.95047, 1., 1.08883], np.float32)
    linear = xyz @ np.array([[3.2404542, -1.5371385, -.4985314],
                              [-.9692660, 1.8760108, .0415560], [.0556434, -.2040259, 1.0572252]], np.float32).T
    return encode_srgb(linear)


def metrics(array):
    a = np.asarray(array, np.float32)
    if not a.size: return None
    flat = a.reshape(-1, 3)
    y = flat @ np.array([.2126, .7152, .0722], np.float32)
    p = np.percentile(y, [5, 50, 95])
    return {'p5': float(p[0]), 'p50': float(p[1]), 'p95': float(p[2]),
            'dark_fraction': float(np.mean(y < 16)), 'bright_fraction': float(np.mean(y > 245)),
            'channel_0': np.mean(flat <= 0, axis=0).tolist(),
            'channel_255': np.mean(flat >= 255, axis=0).tolist(),
            'domain': 'encoded sRGB luminance proxy, DN 0..255'}


def analyze(image, regions):
    small = image.copy(); small.thumbnail((1500, 1500))
    a = np.asarray(small)
    subjects = []
    for box in regions.get('faces', []):
        x0, y0, x1, y1 = pixel_box(box, small.size)
        value = metrics(a[y0:y1, x0:x1])
        if value: subjects.append(value)
    whole = metrics(a)
    reference = float(np.median([s['p50'] for s in subjects])) if subjects else whole['p50']
    tags = []
    if subjects and whole['p50'] > reference * 1.7: tags.append('暗主体与亮背景')
    if reference < 80: tags.append('暗部较多')
    if whole['bright_fraction'] > .08: tags.append('高亮较多')
    if len(subjects) > 1: tags.append('多人受光')
    if not subjects: tags.append('主体未可靠定位')
    if not tags: tags.append('常规光线')
    # Encoded RGB content is not a neutral reference. These conservative Lab
    # signatures detect changed scene/color conditions, never estimate CCT.
    color=rgb_to_lab(np.asarray(small.resize((90,90)),np.float32)/255)
    signature=[np.median(color[y:y+30,x:x+30,1:].reshape(-1,2),axis=0).tolist()
               for y in range(0,90,30) for x in range(0,90,30)]
    face_colors=[];full_lab=rgb_to_lab(np.asarray(small,np.float32)/255)
    for box in regions.get('faces',[]):
        x0,y0,x1,y1=pixel_box(box,small.size);fw,fh=x1-x0,y1-y0
        patch=full_lab[y0+int(.2*fh):y0+max(1,int(.85*fh)),x0+int(.2*fw):x0+max(1,int(.8*fw)),1:]
        if patch.size:face_colors.append(np.median(patch.reshape(-1,2),axis=0).tolist())
    return {'whole': whole, 'subjects': subjects, 'reference_p50': reference,
            'color_signature':signature,'face_colors':face_colors,
            'tags': tags, 'measurement_size': list(small.size), 'face_count_is_observation_only': len(subjects)}


def plan_recipe(profile, analysis):
    if profile not in {'natural', 'clear', 'portrait', 'keep'}: raise ValueError('未知方案')
    if profile == 'keep': return {k: 0. for k in LIMITS}
    reference = analysis['reference_p50']
    # This is a bounded local increment, never a universal skin brightness target.
    exposure = float(np.clip((100 - reference) / 220, -.12, .42))
    scale = {'natural': .65, 'clear': 1., 'portrait': .8}[profile]
    return {'exposure_ev': exposure * scale,
            'shadows': float(np.clip((75 - reference) / 260, 0, .22)) * scale,
            'highlights': -.18 if analysis['whole']['bright_fraction'] > .04 else -.05,
            'contrast': .14 if profile == 'clear' else .04,
            'skin_ev': .07 if profile == 'portrait' else .025 if profile == 'clear' else 0.,
            'skin_da': 0., 'skin_db': 0.,
            'skin_smooth': .12 if profile == 'portrait' else 0., 'sharpen': .08}


def complete_recipe(profile,analysis,adjustments):
    """The same complete parameter set is used for confirmation and rendering."""
    from . import tuning
    recipe={key:0. for key in LIMITS};recipe.update(plan_recipe(profile,analysis))
    if profile in {'clear','portrait'}:
        if 'skin_balance' not in analysis:raise ValueError('缺少本图逐人肤色分析，请重新分析或生成本图样片')
        recipe['skin_balance']=analysis['skin_balance']
    else:recipe['skin_balance']=[]
    return tuning.apply(recipe,adjustments)


def balance_recipe(image, regions):
    """Small, per-person blue-reflection hypotheses, never a universal skin target."""
    small=image.copy();small.thumbnail((1000,1000));lab=rgb_to_lab(np.asarray(small,np.float32)/255)
    result=[]
    for box in regions.get('faces',[]):
        x0,y0,x1,y1=pixel_box(box,small.size);fw,fh=x1-x0,y1-y0
        patch=lab[y0+int(.3*fh):y0+max(1,int(.8*fh)),x0+int(.2*fw):x0+max(1,int(.8*fw))]
        if not patch.size: continue
        median=np.median(patch.reshape(-1,3),axis=0);l,a,b=map(float,median)
        db=min(4.,-b*.15) if b < -2 and l>12 else 0.
        da=-min(1.5,max(0.,a*.04)) if db and a>4 else 0.
        result.append({'box':box,'da':da,'db':db,'reference_lab':median.tolist(),
                       'basis':'bounded blue-reflection estimate; sample review required'})
    return result


def pixel_box(box, size):
    values = np.asarray(box, float)
    if values.shape != (4,) or not np.isfinite(values).all() or np.any(values < 0) or np.any(values > 1):
        raise ValueError('选区坐标必须是本图逻辑方向 0—1 范围')
    x0, y0, x1, y1 = values
    if x1 <= x0 or y1 <= y0: raise ValueError('选区为空')
    w, h = size
    return max(0, int(x0*w)), max(0, int(y0*h)), min(w, max(1, math.ceil(x1*w))), min(h, max(1, math.ceil(y1*h)))


def make_masks(image, regions, person_mask=None, protection_override=None, skin_override=None):
    for mask in (skin_override,protection_override):
        if mask is not None and (mask.mode!='L' or mask.size!=image.size):
            raise ValueError('既有蒙版必须为本图逻辑方向、原尺寸的单通道图，不能自动缩放')
    if skin_override is not None:
        protection=protection_override.copy() if protection_override is not None else Image.new('L',image.size,0)
        return skin_override.copy(),protection,{'method':'source-bound explicit visible-skin mask; review required before smoothing',
            'skin_fraction':float(np.mean(np.asarray(skin_override)>128)),'needs_visual_review':True,
            'person_mask_used':False,'actual_parts':'explicit mask; see bound coverage review for parts and exceptions',
            'unverified_parts':'not inferred from face boxes or skin colors'}
    small = image.copy(); small.thumbnail((1500, 1500)); w, h = small.size
    rgb = np.asarray(small, np.float32) / 255
    lab = rgb_to_lab(rgb)
    spatial = Image.new('L', small.size, 0); draw = ImageDraw.Draw(spatial)
    palettes = []
    for box in regions.get('faces', []):
        x0, y0, x1, y1 = pixel_box(box, small.size)
        fw, fh = x1-x0, y1-y0
        draw.ellipse((x0, y0, x1, y1), fill=255)
        draw.ellipse((x0+fw*.22, y0+fh*.65, x1-fw*.22, y1+fh*.55), fill=255)
        crop = lab[y0+int(fh*.3):y0+max(int(fh*.8),1), x0+int(fw*.2):x0+max(int(fw*.8),1)]
        if crop.size: palettes.append(np.median(crop.reshape(-1,3),axis=0))
    for box in regions.get('hands', []):
        bounds = pixel_box(box, small.size); draw.ellipse(bounds, fill=255)
        x0,y0,x1,y1=bounds;crop=lab[y0:y1,x0:x1]
        if crop.size: palettes.append(np.median(crop.reshape(-1,3),axis=0))
    candidate = np.zeros((h,w),np.float32)
    for color in palettes:
        distance = np.sqrt(np.sum((lab[...,1:]-color[1:])**2,axis=-1))
        light_distance = np.abs(lab[...,0]-color[0])
        weight = np.clip((23-distance)/10,0,1)*np.clip((40-light_distance)/15,0,1)
        candidate = np.maximum(candidate,weight)
    spatial_array = np.asarray(spatial,np.float32)/255
    if person_mask is not None:
        person = np.asarray(person_mask.convert('L').resize(small.size,Image.Resampling.BILINEAR),np.float32)/255
        # Person segmentation is only an exclusion boundary. Matching hair or
        # clothes cannot become skin merely because their colors match a face.
        spatial_array = np.minimum(spatial_array,person)
    candidate *= spatial_array
    # Near-neutral, nearly black and clipped skin candidates are not reliable for color correction.
    candidate *= (np.linalg.norm(lab[...,1:],axis=-1)>3)&(lab[...,0]>8)&(lab[...,0]<96)
    skin=Image.fromarray(np.uint8(np.clip(candidate*255,0,255))).filter(ImageFilter.GaussianBlur(1.2))
    sd=ImageDraw.Draw(skin)
    for polygon in regions.get('features', []):
        sd.polygon([(x*w,y*h) for x,y in polygon],fill=0)
    skin=skin.resize(image.size,Image.Resampling.LANCZOS)
    protection=Image.new('L',image.size,0);pd=ImageDraw.Draw(protection)
    for polygon in regions.get('protect', []):
        pd.polygon([(x*image.width,y*image.height) for x,y in polygon],fill=255)
    if protection_override is not None:
        protection=protection_override.copy()
    return skin,protection,{'method':'person/face/hand candidates intersected with per-person color support; not semantic skin truth',
          'skin_fraction':float(np.mean(np.asarray(skin)>128)), 'needs_visual_review':True,
          'person_mask_used':person_mask is not None,'actual_parts':'localized face/neck/hand candidates only; person mask is an exclusion boundary',
          'unverified_parts':'ears, neck, arms, hands and other exposed skin require overlay review; unsupported areas stay unchanged'}


def smooth_skin_regions(source,skin,protection,strength):
    """Scale a bilateral filter to each connected candidate; never expand the mask."""
    import cv2
    count,labels,stats,_=cv2.connectedComponentsWithStats((skin>0).astype(np.uint8),connectivity=8)
    output=source.copy();settings=[]
    for label in range(1,count):
        x,y,w,h,pixels=map(int,stats[label])
        if pixels<9:continue
        radius=int(np.clip(round(min(w,h)*(.008+.018*strength)),2,14))
        diameter=radius*2+1;sigma_color=16+64*strength;sigma_space=max(2.,radius*.7)
        x0=max(0,x-radius);y0=max(0,y-radius);x1=min(source.shape[1],x+w+radius);y1=min(source.shape[0],y+h+radius)
        original=source[y0:y1,x0:x1];component=labels[y0:y1,x0:x1]==label
        filtered=cv2.bilateralFilter(original,diameter,sigma_color,sigma_space)
        weight=strength*(skin[y0:y1,x0:x1].astype(np.float32)/255)*(1-protection[y0:y1,x0:x1].astype(np.float32)/255)*component
        mixed=np.uint8(np.floor(np.clip(original.astype(np.float32)+(filtered.astype(np.float32)-original)*weight[...,None],0,255)+.5))
        patch=output[y0:y1,x0:x1];patch[component]=mixed[component]
        settings.append({'box':[x,y,w,h],'diameter_px':diameter,'sigma_color_dn':sigma_color,
                         'sigma_space_px':sigma_space,'mix':strength,'affected_pixels':int(np.count_nonzero(weight))})
    return output,settings


def render(image, recipe, skin, protection, texture_skin=None):
    params = {k: 0. for k in LIMITS}
    balance=recipe.get('skin_balance',[])
    if not isinstance(balance,list) or len(balance)>128: raise ValueError('肤色分区记录无效')
    for item in balance:
        pixel_box(item['box'],image.size)
        if not all(type(item.get(k)) in (int,float) and math.isfinite(item[k]) and abs(item[k])<=6 for k in ('da','db')):
            raise ValueError('分区肤色增量无效')
    for key, value in recipe.items():
        if key=='skin_balance': continue
        if key not in LIMITS: raise ValueError('未知调色参数：'+key)
        if type(value) not in (int,float) or not math.isfinite(value) or not LIMITS[key][0] <= value <= LIMITS[key][1]:
            raise ValueError('调色参数无效或超出本地范围：'+key)
        params[key]=float(value)
    for mask in (skin,protection,texture_skin):
        if mask is not None and (mask.size!=image.size or mask.mode!='L'): raise ValueError('蒙版尺寸或通道与本图不符')
    source=np.asarray(image.convert('RGB'));out=source.copy()
    skin_a=np.asarray(skin) if skin is not None else np.zeros(source.shape[:2],np.uint8)
    texture_a=skin_a if texture_skin is None else np.minimum(skin_a,np.asarray(texture_skin))
    protect=np.asarray(protection) if protection is not None else np.zeros(source.shape[:2],np.uint8)
    outside=0;count=source.size;notes=[]
    has_base=any(params[k] for k in ('exposure_ev','shadows','highlights','contrast'))
    has_skin=any(params[k] for k in ('skin_ev','skin_da','skin_db')) or any(item['da'] or item['db'] for item in balance)
    for y in range(0,image.height,256):
        base=source[y:y+256].astype(np.float32)/255;a=base.copy()
        if has_base:
            if params['exposure_ev']: a=encode_srgb(decode_srgb(a)*2**params['exposure_ev'])
            lum=a@np.array([.2126,.7152,.0722],np.float32)
            target=lum+params['shadows']*lum*(1-lum)**2
            target+=params['highlights']*np.maximum(lum-.55,0)*(1-lum)
            target+=params['contrast']*lum*(1-lum)*(lum-.5)
            a*=np.divide(target,np.maximum(lum,1e-6))[...,None]
        if params['saturation'] or params['warmth'] or params['tint']:
            lab=rgb_to_lab(np.clip(a,0,1));lab[...,1:]*=1+params['saturation']
            lab[...,1]+=params['tint'];lab[...,2]+=params['warmth'];a=lab_to_rgb(lab)
        if has_skin:
            corrected=a.copy()
            if params['skin_ev']: corrected=encode_srgb(decode_srgb(np.maximum(corrected,0))*2**params['skin_ev'])
            if params['skin_da'] or params['skin_db'] or balance:
                lab=rgb_to_lab(np.clip(corrected,0,1));da=np.full(a.shape[:2],params['skin_da'],np.float32);db=np.full(a.shape[:2],params['skin_db'],np.float32)
                if balance:
                    best=np.full(a.shape[:2],np.inf,np.float32);xx=np.arange(image.width,dtype=np.float32)[None,:]/image.width
                    yy=np.arange(y,y+a.shape[0],dtype=np.float32)[:,None]/image.height
                    for item in balance:
                        x0,y0,x1,y1=item['box'];distance=((xx-(x0+x1)/2)/max(x1-x0,.02))**2+((yy-(y0+y1)/2)/max((y1-y0)*5,.1))**2
                        distance=np.where((xx>=x0)&(xx<x1)&(yy>=y0)&(yy<y1),distance-10000,distance)
                        selected=distance<best;da[selected]=params['skin_da']+item['da'];db[selected]=params['skin_db']+item['db'];best=np.minimum(best,distance)
                lab[...,1]+=da;lab[...,2]+=db
                corrected=lab_to_rgb(lab)
            a+=(corrected-a)*(skin_a[y:y+256].astype(np.float32)/255)[...,None]
        weight=(1-protect[y:y+256].astype(np.float32)/255)[...,None]
        a=base+(a-base)*weight
        outside+=int(np.count_nonzero((a<0)|(a>1)))
        out[y:y+256]=np.uint8(np.floor(np.clip(a,0,1)*255+.5))
    smoothing_regions=[]
    if params['skin_smooth'] and texture_a.any():
        try:
            out,smoothing_regions=smooth_skin_regions(out,texture_a,protect,params['skin_smooth'])
        except (ImportError,AttributeError): notes.append('保边平滑不可调用，已跳过该模块')
    if params['sharpen']:
        blurred=np.asarray(Image.fromarray(out).filter(ImageFilter.GaussianBlur(1)),np.float32)
        for y in range(0,image.height,256):
            a=out[y:y+256].astype(np.float32)
            weight=params['sharpen']*(1-.85*skin_a[y:y+256]/255)*(1-protect[y:y+256]/255)
            value=a+(a-blurred[y:y+256])*weight[...,None]
            outside+=int(np.count_nonzero((value<0)|(value>255)))
            out[y:y+256]=np.uint8(np.floor(np.clip(value,0,255)+.5))
    core=protect==255;out[core]=source[core]
    report={'operator_version':'local-pixel-2','recipe':dict(params,skin_balance=balance),'units':{'exposure_ev':'linear RGB local EV increment','skin_ev':'linear RGB local EV increment',
            'skin_da':'CIELab D65 a* delta','skin_db':'CIELab D65 b* delta','skin_smooth':'8-bit RGB adaptive bilateral per connected skin candidate; diameter/sigma recorded; 0..1 result mix ratio','sharpen':'encoded RGB unsharp radius=1 output pixel, mix ratio',
            'saturation':'CIELab chroma scale delta; 0.1 means multiply a*,b* by 1.1','warmth':'global CIELab D65 b* delta; not Kelvin','tint':'global CIELab D65 a* delta'},
            'preclip_channel_out_of_range_fraction':outside/max(count,1),
            'protected_core_exact':bool(np.array_equal(out[core],source[core])),
            'skin_pixels_affected':int(np.count_nonzero((skin_a if has_skin else texture_a if params['skin_smooth'] else np.zeros_like(skin_a))*(protect<255))),
            'texture_candidate_pixels':int(np.count_nonzero(texture_a)),'skin_smoothing_regions':smoothing_regions,'skin_smoothing_pixels_affected':sum(r['affected_pixels'] for r in smoothing_regions),
            'before':metrics(source[::8,::8]),'after':metrics(out[::8,::8]),'notes':notes,
            'geometry_unchanged':True,'generative_operations':False}
    return Image.fromarray(out),report


def save_lossless(image,path,metadata):
    buf=io.BytesIO();image.save(buf,format='PNG',icc_profile=ICC,exif=metadata.get('exif',b''),compress_level=2)
    write_new(path,buf.getvalue())


def export_image(image,path,metadata,watermark=None,layout=None):
    path=Path(path)
    if path.exists(): raise ValueError('成品版本已存在，不覆盖')
    result=image.convert('RGBA')
    if watermark is not None:
        layout=layout or {};width=round(min(image.size)*layout.get('width_fraction',.15))
        mark=watermark.convert('RGBA');mark=mark.resize((width,max(1,round(mark.height*width/mark.width))),Image.Resampling.LANCZOS)
        margin=round(min(image.size)*layout.get('margin_fraction',.025))
        pos=(image.width-mark.width-margin,image.height-mark.height-margin)
        if min(pos)<0: raise ValueError('水印超出照片边界')
        result.alpha_composite(mark,pos)
    buf=io.BytesIO();result.convert('RGB').save(buf,format='JPEG',quality=96,subsampling=0,icc_profile=ICC,exif=metadata.get('exif',b''))
    write_new(path,buf.getvalue())
    with Image.open(path) as check:
        check.load()
        if check.size!=image.size or not check.info.get('icc_profile') or check.getexif().get(274,1)!=1:
            raise ValueError('导出核验未通过')
    return {'path':str(path),'sha256':sha256(path),'size':list(image.size),'format':'JPEG',
            'quality':96,'subsampling':0,'icc':'embedded sRGB','orientation':1,'watermark':watermark is not None}


def overlay(image,skin,protection):
    base=image.copy();base.thumbnail((1600,1600));a=np.asarray(base).astype(np.float32)
    for mask,color in ((protection,[30,130,240]),(skin,[240,120,35])):
        weight=np.asarray(mask.resize(base.size),np.float32)/255*.42
        a=a*(1-weight[...,None])+np.array(color)*weight[...,None]
    return Image.fromarray(np.uint8(np.clip(a,0,255)))


def vision_regions(image, directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    if sys.platform!='darwin' or not shutil.which('swiftc'):
        return {},None,{'available':False,'reason':'系统 Vision 不可用，需在网页定位主体与选区'}
    source=Path(__file__).with_name('vision.swift')
    binary=directory/('vision-'+sha256(source)[:12])
    stamp=binary.with_suffix('.json')
    try:
        if not (binary.is_file() and stamp.is_file() and load_json(stamp).get('binary_sha256')==sha256(binary)):
            if binary.exists(): binary=directory/('vision-'+uuid.uuid4().hex[:12]);stamp=binary.with_suffix('.json')
            result=subprocess.run(['swiftc','-O',str(source),'-o',str(binary)],capture_output=True,text=True,timeout=90)
            if result.returncode: raise ValueError('系统 Vision 辅助程序编译失败：'+result.stderr[-300:])
            save_json(stamp,{'binary_sha256':sha256(binary),'source_sha256':sha256(source)})
        token=uuid.uuid4().hex[:12];input_path=directory/(token+'.png');mask_path=directory/(token+'-people.png')
        small=image.copy();small.thumbnail((1500,1500));save_lossless(small,input_path,{})
        result=subprocess.run([str(binary),str(input_path),str(mask_path)],capture_output=True,text=True,timeout=90)
        if result.returncode: raise ValueError('系统 Vision 定位失败：'+result.stderr[-200:])
        import json
        regions=json.loads(result.stdout);person=Image.open(mask_path).convert('L') if mask_path.is_file() else None
        return regions,person,{'available':True,'provider':'Apple Vision','source_sha256':sha256(source),
                               'faces_observed':len(regions.get('faces',[])),'skin_segmentation_proven':False}
    except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
        return {},None,{'available':False,'reason':str(exc)}
