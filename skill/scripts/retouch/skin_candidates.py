"""Four-model local candidate masks; no user decisions or quality approvals."""
import time
import cv2
import numpy as np
from PIL import Image,ImageDraw
mp=None

def segment_options(path):
 return mp.tasks.vision.ImageSegmenterOptions(base_options=mp.tasks.BaseOptions(model_asset_path=str(path),delegate=mp.tasks.BaseOptions.Delegate.CPU),output_category_mask=False,output_confidence_masks=True)
def mpimage(im):return mp.Image(image_format=mp.ImageFormat.SRGB,data=np.ascontiguousarray(np.asarray(im.convert('RGB'))))
def infer(seg,im):
 result=np.stack([np.squeeze(m.numpy_view().copy()) for m in seg.segment(mpimage(im)).confidence_masks],axis=-1)
 if result.shape[:2]!=(im.height,im.width) or result.shape[-1] not in (2,6) or not np.isfinite(result).all():raise ValueError('模型输出无效')
 return result
def resize(a,size):return cv2.resize(a,tuple(size),interpolation=cv2.INTER_LINEAR)
def iou(a,b):
 x=max(a[0],b[0]);y=max(a[1],b[1]);xx=min(a[2],b[2]);yy=min(a[3],b[3]);v=max(0,xx-x)*max(0,yy-y)
 return v/max(1e-9,(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-v)
def detection(detector,im):
 # YuNet can use native multi-scale frames; original dimensions retained in mapping.
 work=im.copy();work.thumbnail((1500,1500));sx=im.width/work.width;sy=im.height/work.height
 array=cv2.cvtColor(np.asarray(work),cv2.COLOR_RGB2BGR);found=[];timings=[]
 for scale in (1.,1.5):
  test=cv2.resize(array,None,fx=scale,fy=scale);detector.setInputSize((test.shape[1],test.shape[0]));t=time.perf_counter();_,faces=detector.detect(test);timings.append(time.perf_counter()-t)
  if faces is None:continue
  for f in faces:
   x,y,w,h=f[:4]/scale
   box=[float(x*sx),float(y*sy),float((x+w)*sx),float((y+h)*sy)]
   points=np.asarray(f[4:14]).reshape(5,2)/scale*np.array([sx,sy])
   found.append({'box':box,'score':float(f[-1]),'five_points':points.tolist(),'analysis_scale':scale})
 keep=[]
 for f in sorted(found,key=lambda f:f['score'],reverse=True):
  if not any(iou(f['box'],g['box'])>.35 for g in keep):keep.append(f)
 return keep,timings
def cropbox(box,size,pad):
 x0,y0,x1,y1=box;w,h=size;fw=x1-x0;fh=y1-y0
 return [max(0,int(x0-pad[0]*fw)),max(0,int(y0-pad[1]*fh)),min(w,int(np.ceil(x1+pad[2]*fw))),min(h,int(np.ceil(y1+pad[3]*fh)))]
def feature_polygons(points):
 c=mp.tasks.vision.FaceLandmarksConnections;polys=[]
 for group in [c.FACE_LANDMARKS_LEFT_EYE,c.FACE_LANDMARKS_RIGHT_EYE,c.FACE_LANDMARKS_LEFT_EYEBROW,c.FACE_LANDMARKS_RIGHT_EYEBROW,c.FACE_LANDMARKS_LIPS]:
  indices=sorted({v for edge in group for v in [edge.start,edge.end]});polys.append(cv2.convexHull(np.float32(points[indices,:2])).reshape(-1,2))
 return polys

def texture_candidate(color,deferred_boxes):
 # Defer a whole connected visible-skin area; never cut smoothing at a face rectangle.
 support=(color>0).astype(np.uint8)
 if not deferred_boxes:return color.copy(),0
 _,labels=cv2.connectedComponents(support,connectivity=8)
 blocked=set()
 for x0,y0,x1,y1 in deferred_boxes:blocked.update(int(v) for v in np.unique(labels[y0:y1,x0:x1]) if v)
 texture=color.copy()
 if blocked:texture[np.isin(labels,list(blocked))]=0
 return texture,len(blocked)

class CandidateModels:
 def __init__(self,paths):
  global mp
  import mediapipe as mp
  self.skin=mp.tasks.vision.ImageSegmenter.create_from_options(segment_options(paths['skin']))
  self.hair=mp.tasks.vision.ImageSegmenter.create_from_options(segment_options(paths['hair']))
  options=mp.tasks.vision.FaceLandmarkerOptions(base_options=mp.tasks.BaseOptions(model_asset_path=str(paths['landmarks']),delegate=mp.tasks.BaseOptions.Delegate.CPU),running_mode=mp.tasks.vision.RunningMode.IMAGE,num_faces=4,min_face_detection_confidence=.5,min_face_presence_confidence=.5,output_face_blendshapes=False,output_facial_transformation_matrixes=False)
  self.landmarker=mp.tasks.vision.FaceLandmarker.create_from_options(options)
  self.detector=cv2.FaceDetectorYN.create(str(paths['yunet']),'',(320,320),.75,.3,5000,cv2.dnn.DNN_BACKEND_DEFAULT,cv2.dnn.DNN_TARGET_CPU)
 def close(self):
  for item in (self.skin,self.hair,self.landmarker):item.close()
 def smoke(self):
  image=Image.new('RGB',(48,32),(150,110,90))
  skin=infer(self.skin,image);hair=infer(self.hair,image)
  if skin.shape!=(32,48,6) or hair.shape!=(32,48,2) or not np.isfinite(skin).all() or not np.isfinite(hair).all():raise ValueError('模型输出尺寸／通道／数值异常')
  self.landmarker.detect(mpimage(image));detection(self.detector,image)
  return {'smoke_passed':True,'models_checked':['skin','hair','landmarks','yunet'],'shape':list(skin.shape),'quality':'candidate_requires_review'}
 def propose(self,im,regions):
  t_total=time.perf_counter();skin_seg=self.skin;hair_seg=self.hair;landmarker=self.landmarker;detector=self.detector
  w,h=im.size;work=im.copy();work.thumbnail((1500,1500))
  t=time.perf_counter();global_probs=infer(skin_seg,work);global_s=time.perf_counter()-t
  non_skin=resize(np.maximum(global_probs[...,1],global_probs[...,4]),im.size)
  global_labels=resize(np.isin(global_probs.argmax(-1),[2,3]).astype(np.uint8),im.size)>.5
  color=np.float32(global_labels);hair_exclusion=np.zeros((h,w),np.float32);feature_core=Image.new('L',im.size);fd=ImageDraw.Draw(feature_core)
  for poly in regions.get('features',[]):
   points=np.asarray(poly,dtype=float)
   if points.ndim!=2 or points.shape[1]!=2 or len(points)<3 or not np.isfinite(points).all() or np.any(points<0) or np.any(points>1):raise ValueError('五官观察点无效')
   fd.polygon([(float(x*w),float(y*h)) for x,y in points],fill=255)
  deferred_boxes=[];records=[];detected,detector_times=detection(detector,im)
  # High scores first; per-face observations never directly become skin pixels.
  for face_index,face in enumerate(detected):
   bt=time.perf_counter();box=face['box'];fw=box[2]-box[0];fh=box[3]-box[1]
   roi=cropbox(box,im.size,(.6,.6,.6,.6));x0,y0,x1,y1=roi;source_crop=im.crop(roi);cw,ch=source_crop.size
   if min(cw,ch)<12:continue
   analysis=source_crop.copy();analysis.thumbnail((768,768));lp=infer(skin_seg,analysis)
   local_skin=resize(np.isin(lp.argmax(-1),[2,3]).astype(np.float32),source_crop.size)
   own=cropbox(box,im.size,(.15,.4,.15,.18));ox0,oy0,ox1,oy1=own
   yy,xx=np.mgrid[y0:y1,x0:x1];edge=np.minimum.reduce([xx-ox0+1,oy1-yy,ox1-xx,yy-oy0+1]).astype(np.float32)
   alpha=np.clip(edge/max(2.,min(fw,fh)*.1),0,1)
   # Strong local skin can replace coarse global labels inside a verified face vicinity.
   # Disagreement remains a QA signal; no automatic approval follows from this override.
   conflict=(alpha>.5)&(local_skin>.5)&(non_skin[y0:y1,x0:x1]>.8)
   target=color[y0:y1,x0:x1];target[:]=target*(1-alpha)+local_skin*alpha
   mesh_points=None;mesh_s=0.;hair_s=0.;nose_pixels=0;decision='texture_deferred_small_or_unreliable_face'
   if min(fw,fh)>=32:
    mt=time.perf_counter();meshes=landmarker.detect(mpimage(analysis)).face_landmarks;mesh_s=time.perf_counter()-mt
    candidates=[]
    for mesh in meshes:
     points=np.array([[x0+p.x*cw,y0+p.y*ch] for p in mesh]);b=[*points.min(0),*points.max(0)]
     if iou(b,box)>.25:candidates.append((iou(b,box),points))
    if candidates:mesh_points=max(candidates,key=lambda item:item[0])[1]
    ht=time.perf_counter();hp=infer(hair_seg,analysis)[...,1];hair_s=time.perf_counter()-ht
    hp=resize(hp,source_crop.size)
    gray=cv2.cvtColor(np.asarray(source_crop),cv2.COLOR_RGB2GRAY).astype(np.float32)/255
    if hasattr(cv2,'ximgproc'):hp=cv2.ximgproc.guidedFilter(gray,hp,4,1e-3)
    veto=np.clip((hp-.35)/.5,0,1)*alpha
    hair_exclusion[y0:y1,x0:x1]=np.maximum(hair_exclusion[y0:y1,x0:x1],veto)
    if mesh_points is not None:
     for poly in feature_polygons(mesh_points):fd.polygon([tuple(v) for v in poly],fill=255)
     # Generic alar topology plus relative darkness; no per-photo tracing.
     groups=[[98,64,48,115,220,60,99,97],[327,294,278,344,440,290,328,326]]
     region=Image.new('L',source_crop.size);nd=ImageDraw.Draw(region)
     for group in groups:nd.polygon([tuple(v-np.array([x0,y0])) for v in mesh_points[group]],fill=255)
     gray255=gray*255;context=cv2.GaussianBlur(gray255,(0,0),max(2.,fw*.025))
     dark=((gray255-context)<-8)&(np.asarray(region)>0);nose=cv2.dilate(dark.astype(np.uint8),np.ones((3,3),np.uint8));nose_pixels=int(nose.sum())
     full=np.asarray(feature_core).copy();full[y0:y1,x0:x1]=np.maximum(full[y0:y1,x0:x1],nose*255);feature_core=Image.fromarray(full);fd=ImageDraw.Draw(feature_core)
     decision='candidate_with_mesh_features_still_requires_quality_review'
   if mesh_points is None:
    deferred_boxes.append(own)
   records.append({'index':face_index,'box':box,'score':face['score'],'source_face_min_side_px':min(fw,fh),'source_crop_size':[cw,ch],
    'mesh_found':mesh_points is not None,'nostril_protection_pixels':nose_pixels,'skin_global_conflict_pixels':int(conflict.sum()),
    'mesh_s':mesh_s,'hair_s':hair_s,'face_elapsed_s':time.perf_counter()-bt,'decision':decision})
  # Derive transition only along real candidate edges, not skin-color confidence internally.
  support=(color>.5).astype(np.uint8);distance=cv2.distanceTransform(support,cv2.DIST_L2,5);color=np.minimum(distance/3.,1.)*(1-hair_exclusion)
  feature=np.asarray(feature_core);feature_distance=cv2.distanceTransform((feature==0).astype(np.uint8),cv2.DIST_L2,5)
  color*=np.minimum(feature_distance/2.,1.)
  texture,blocked_components=texture_candidate(color,deferred_boxes)

  color_mask=Image.fromarray(np.uint8(np.clip(color,0,1)*255));texture_mask=Image.fromarray(np.uint8(np.clip(texture,0,1)*255))
  deferred=sum(not r['mesh_found'] for r in records)
  report={'provider':'MediaPipe skin + hair + FaceLandmarker / OpenCV YuNet','release':'candidate-only','source_size':list(im.size),'analysis_size':list(work.size),'original_size':list(im.size),'face_candidates':len(detected),'mesh_faces':sum(r['mesh_found'] for r in records),'deferred_texture_faces':deferred,'deferred_skin_components':blocked_components,'faces':records,'crops_used':len(records),'rejected_crops':[],
   'needs_visual_review':True,'automatic_quality_approved':False,'quality_reasons':['visible_skin_and_texture_not_independently_validated']+(['small_or_unreliable_face_texture_deferred'] if deferred else []),
   'unverified_parts':['all visible skin completeness','occluded or undetected faces','fine hairs and skin texture'],
   'source_detail':'face crops extracted from original oriented pixels','elapsed_s':time.perf_counter()-t_total}
  return color_mask,texture_mask,feature_core,report
