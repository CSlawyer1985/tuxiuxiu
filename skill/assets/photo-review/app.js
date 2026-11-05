'use strict';
const $=id=>document.getElementById(id);
const labels={natural:'自然校正',clear:'明净通透',portrait:'柔和人像',keep:'保留原貌'};
const statuses={pending:'等待分析',analyzing:'正在分析',analyzed:'已分析',sample_pending:'准备样片',sample_ready:'样片已就绪',queued:'等待处理',rendering:'正在处理',needs_review:'需要复核',change_requested:'待按要求修改',error:'处理遇到问题',completed:'已完成',excluded:'已跳过'};
const runLabels={preparing:'正在准备样片',needs_selection:'样片已就绪，等你选择',running:'正在批量处理',paused:'已暂停',needs_review:'有照片需要复核',needs_changes:'修改要求已保存，等待新样片',completed:'本批处理完成'};
let compareValue=50;
let state=null,currentId=null,profile='natural',compare=true,showOverlay=false,editing=false,polling=false,renderKey='';
let strokes=[],drawing=null,editingRevision=null,toastTimer=null,lastSeq=null,reviewed={},acceptedWarnings={};
let tuningDrafts={},tuningBuildKey='',previewTimer=null,previewBusy=false,previewWanted=false,previewSerial=0,quickPreview=null,previewStatus='',applyingTuning=false;

const media=(asset,download=false)=>asset?(asset.data_url||`/media/${encodeURIComponent(asset.id)}${download?'?download=1':''}`):'';
const photo=()=>state?.photos.find(p=>p.id===currentId);
const maskKey=p=>p?.masks?`${p.masks.skin.sha256}:${p.masks.protection.sha256}:${p.masks.texture?.sha256||''}`:'';
const humanReviewed=p=>reviewed[p.id]===maskKey(p);
const isReviewed=p=>humanReviewed(p)||Boolean(p.mask_review_current);
function saveDecisionDraft(){if(!state)return;try{localStorage.setItem(`photo-review-choice:${state.run_id}`,JSON.stringify({revision:state.revision,currentId,profile,scope:$('applyScope').value,watermark:$('watermarkEnabled').checked,note:$('decisionNote').value,noteIntent:$('noteIntent').value,tuningDrafts,reviewed,acceptedWarnings}));}catch(error){notify('选择草稿未能保存在浏览器，请保持页面开启。',true);}}
function restoreDecisionDraft(){try{const draft=JSON.parse(localStorage.getItem(`photo-review-choice:${state.run_id}`)||'null');if(!draft)return;if(Object.hasOwn(labels,draft.profile))profile=draft.profile;if(state.photos.some(p=>p.id===draft.currentId))currentId=draft.currentId;$('applyScope').value=draft.scope==='current'?'current':'batch';$('watermarkEnabled').checked=Boolean(draft.watermark);$('decisionNote').value=draft.note||'';$('noteIntent').value=draft.noteIntent==='delivery_note'?'delivery_note':'change_request';tuningDrafts=draft.tuningDrafts||{};if(draft.revision===state.revision){reviewed=draft.reviewed||{};acceptedWarnings=draft.acceptedWarnings||{};}else notify('已恢复选择草稿；样片版本更新，请重新核对选区和效果。');}catch(error){notify('选择草稿读取失败，请重新核对应用范围。',true);}}
function savedTuning(p,selectedProfile=profile){const values={...(state?.tuning_defaults?.[selectedProfile]||{}),...(p?.tuning?.[selectedProfile]||{})};if(state?.capabilities?.skin_smooth===false)values.skin_smooth=0;return values;}
function tuningKey(p=photo(),selectedProfile=profile){return p?`${p.id}:${selectedProfile}`:'';}
function tuningValues(){return tuningDrafts[tuningKey()]?.values||savedTuning(photo());}
function comparableTuning(values){return JSON.stringify((state?.tuning_controls||[]).map(s=>Number(values[s.key]??0)));}
function tuningNeedsSave(){
  if(!state||!photo()||profile==='keep')return false;
  let targets=$('applyScope').value==='current'?[photo()]:state.photos.filter(p=>!['completed','excluded','error'].includes(p.status));
  if(!targets.length&&$('applyScope').value!=='current')targets=state.photos.filter(p=>!['excluded','error'].includes(p.status));
  return targets.some(p=>comparableTuning(savedTuning(p))!==comparableTuning(tuningValues()));
}
function hasUnsubmittedChangeNote(){
  const text=$('decisionNote').value.trim();if(!text||$('noteIntent').value==='delivery_note')return false;
  const pending=state?.pending_request;
  if(pending&&pending.text===text&&['pending','samples_queued'].includes(pending.status))return false;
  return !(pending?.status==='awaiting_confirmation'&&pending.text===text&&pending.sample_binding===state.sample_binding);
}
function confirmationBlocker(){
  if(state?.config?.watermark_options?.length&&$('watermarkEnabled').checked&&!state.config.watermark_selection)return '请先选择一版活动水印，核对更新后的样片。';
  if(hasUnsubmittedChangeNote())return '请先提交修改要求并更新样片，或将这段文字标为交付备注。';
  if(tuningNeedsSave())return '微调尚未保存为原尺寸样片，请先应用微调并更新样片。';
  if(state?.pending_request?.status==='pending')return '修改要求正在等待处理，旧效果暂不确认。';
  return '';
}
function hasWatermarkComparison(){return Boolean(photo()?.size&&state?.config?.watermark_options?.length&&$('watermarkEnabled').checked&&!showOverlay&&!editing);}
function previewMarked(){return $('watermarkEnabled').checked&&!hasWatermarkComparison();}
function matchingQuickPreview(p){return tuningNeedsSave()&&quickPreview&&quickPreview.photo_id===p.id&&quickPreview.profile===profile&&quickPreview.revision===state.revision&&quickPreview.marked===previewMarked();}
function formatTuning(spec,value){const sign=value>0&&spec.mode==='delta'?'+':'';return spec.unit==='%'?`${sign}${Math.round(value*100)}%`:`${sign}${Number(value.toFixed(2))}${spec.unit?' '+spec.unit:''}`;}
function renderTuning(){
  const p=photo(),specs=state?.tuning_controls||[];visible($('tuningPanel'),Boolean(p?.analysis)&&specs.length>0);
  if(!p?.analysis||!specs.length)return;
  const context=[p.id,profile,p.smoothing_review_current,comparableTuning(savedTuning(p)),specs.map(s=>s.key).join(':')].join('|');
  const available=profile!=='keep'&&['needs_selection','needs_review','needs_changes','completed'].includes(state.status)&&!applyingTuning;
  if(context!==tuningBuildKey){
    $('tuningPrimary').replaceChildren();$('tuningAdvanced').replaceChildren();
    for(const spec of specs){
      const label=document.createElement('label');label.className='tuning-control';
      const heading=document.createElement('span'),name=document.createElement('span'),output=document.createElement('output'),input=document.createElement('input');
      name.textContent=spec.label;input.type='range';input.min=spec.min;input.max=spec.max;input.step=spec.step;input.id='tune-'+spec.key;input.dataset.tuningKey=spec.key;input.setAttribute('aria-label',spec.label);output.htmlFor=input.id;
      heading.append(name,output);label.append(heading,input);
      if(spec.key==='skin_smooth'&&!state.capabilities?.skin_smooth){const hint=document.createElement('small');hint.textContent='当前环境不支持保边磨皮，保持停用';label.append(hint);}
      else if(spec.key==='skin_smooth'&&!p.smoothing_review_current){const hint=document.createElement('small');hint.textContent='可见皮肤覆盖尚未核对通过，磨皮暂不执行；保留当前强度草稿。';label.append(hint);}
      input.addEventListener('input',()=>{
        const values={...tuningValues(),[spec.key]:Number(input.value)};
        tuningDrafts[tuningKey()]={values,revision:state.revision};previewStatus='快速预览更新中…';saveDecisionDraft();render();schedulePreview();
      });
      (['exposure_ev','contrast','saturation','skin_smooth'].includes(spec.key)?$('tuningPrimary'):$('tuningAdvanced')).append(label);
    }
    tuningBuildKey=context;
  }
  const values=tuningValues();
  for(const spec of specs){const input=$('tune-'+spec.key);const value=Number(values[spec.key]??0);input.value=value;input.disabled=!available||(spec.key==='skin_smooth'&&(!state.capabilities?.skin_smooth||!p.smoothing_review_current));input.previousElementSibling.querySelector('output').textContent=spec.key==='skin_smooth'&&!p.smoothing_review_current?'待核对 · '+formatTuning(spec,value):formatTuning(spec,value);}
  $('resetTuning').disabled=!available;$('applyTuning').disabled=!available;
  $('applyTuning').textContent=hasUnsubmittedChangeNote()?'提交参数与修改要求':'应用微调并更新样片';
  const dirty=tuningNeedsSave();$('tuningStatus').classList.toggle('tuning-dirty',dirty);
  $('tuningStatus').textContent=profile==='keep'?'保留原貌不叠加调色，请先选择其他方案。':previewStatus||(dirty?'当前微调尚未保存，确认前请更新原尺寸样片。':'参数已与当前样片一致；可继续微调，或确认效果。');
}
function schedulePreview(){clearTimeout(previewTimer);previewTimer=setTimeout(requestQuickPreview,350);}
async function requestQuickPreview(){
  if(previewBusy){previewWanted=true;return;}
  const p=photo();if(!p?.analysis||!p.masks||profile==='keep')return;
  previewBusy=true;previewWanted=false;const serial=++previewSerial;
  const context={photo_id:p.id,profile,revision:state.revision,adjustments:{...tuningValues()},watermark:previewMarked()};
  try{
    const result=await post('/api/tuning-preview',context);
    if(serial===previewSerial&&photo()?.id===context.photo_id&&profile===context.profile&&state.revision===context.revision&&comparableTuning(tuningValues())===comparableTuning(context.adjustments)&&previewMarked()===context.watermark){
      quickPreview={photo_id:p.id,profile,revision:state.revision,marked:context.watermark,id:'quick-'+serial,data_url:result.image_url};
      previewStatus='快速预览已更新；确认前请更新原尺寸样片。';renderKey='';render();
    }
  }catch(error){if(photo()?.id===context.photo_id&&state.revision===context.revision){previewStatus=error.message;renderTuning();}}
  finally{previewBusy=false;if(previewWanted)requestQuickPreview();}
}
async function submitChangeRequest(){
  const text=$('decisionNote').value.trim();if(!text){notify('先填写需要修改的地方');return;}
  try{
    await post('/api/revision-request',{revision:state.revision,scope:$('applyScope').value,photo_id:currentId,text,
      parameter_context:{profile,adjustments:tuningValues(),watermark:$('watermarkEnabled').checked}});
    quickPreview=null;previewSerial++;saveDecisionDraft();notify('修改要求和当前参数已保存并回传，先更新样片，再由你确认。');await refresh();
  }catch(error){notify(error.message,true);}
}
async function applyTuning(){
  if(hasUnsubmittedChangeNote()){await submitChangeRequest();return;}
  const values={...tuningValues()},selectedProfile=profile,snapshot=comparableTuning(values);applyingTuning=true;render();
  try{
    const result=await post('/api/tuning',{revision:state.revision,profile:selectedProfile,adjustments:values,scope:$('applyScope').value,photo_id:currentId});
    for(const id of result.photo_ids){const key=`${id}:${selectedProfile}`;if(tuningDrafts[key]&&comparableTuning(tuningDrafts[key].values)===snapshot)delete tuningDrafts[key];}
    quickPreview=null;previewSerial++;previewStatus='正在生成原尺寸微调样片，完成后请核对并确认。';tuningBuildKey='';saveDecisionDraft();notify('微调已保存，原有成品保留，正在更新样片');await refresh();
  }catch(error){notify(error.message,true);}
  finally{applyingTuning=false;render();}
}
function visible(node,on){node.classList.toggle('hidden',!on);}
function notify(text,error=false){$('toast').textContent=text;$('toast').classList.toggle('error',error);visible($('toast'),true);clearTimeout(toastTimer);toastTimer=setTimeout(()=>visible($('toast'),false),5000);}
function alertMessage(text){$('alert').textContent=text||'';visible($('alert'),Boolean(text));}
async function post(path,value){
  const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':state.csrf},body:JSON.stringify(value)});
  const result=await response.json();if(!response.ok)throw new Error(result.error||'操作未完成');return result;
}
async function control(action,extra={}){try{await post('/api/control',{action,...extra});await refresh();return true;}catch(error){notify(error.message,true);return false;}}
function updateCallback(){
  let message='当前对话已绑定';
  if(!state.config.conversation_bound)message='尚未绑定回传对话';
  else if(!state.config.callback_enabled)message='回传通道尚未启用';
  else if(state.outbox?.length){
    const last=state.outbox[0];
    message=({pending:'等待回传',sending:'正在回传',queued:'已排队回传 · 继续待核对',observed:'当前对话已收到回传',failed:'回传失败，可检查后重试',delivery_unknown:'回传结果待核对'})[last.status]||message;
  }
  $('callbackStatus').querySelector('span').textContent=message;
  visible($('callbackRetry'),Boolean(state.outbox?.some(e=>['failed','delivery_unknown'].includes(e.status))));
}
function renderList(){
  const box=$('photoList'),scroll=box.scrollTop,left=box.scrollLeft;box.replaceChildren();
  const filter=$('photoFilter').value;
  const list=state.photos.filter(p=>filter==='all'||(filter==='completed'?p.status==='completed':['sample_ready','needs_review','change_requested','error'].includes(p.status)));
  for(const p of list){
    const button=document.createElement('button');button.className='photo-item'+(p.id===currentId?' active':'');
    button.setAttribute('aria-label',`${p.name}，${statuses[p.status]||p.status}`);button.setAttribute('aria-current',String(p.id===currentId));
    if(p.original_preview){const image=document.createElement('img');image.src=media(p.original_preview);image.alt='';image.loading='lazy';button.append(image);}
    else{const wait=document.createElement('span');wait.className='thumb-wait';wait.textContent='…';button.append(wait);}
    const text=document.createElement('span'),name=document.createElement('strong'),status=document.createElement('small');
    name.textContent=p.name;name.title=p.name;status.textContent=statuses[p.status]||p.status;
    if(['needs_review','error'].includes(p.status))status.className='problem';
    text.append(name,status);button.append(text);button.addEventListener('click',()=>selectPhoto(p.id));box.append(button);
  }
  if(!list.length){const empty=document.createElement('p');empty.className='subtle';empty.textContent=state.photos.length?'此分类暂无照片':'尚未添加照片';box.append(empty);}
  box.scrollTop=scroll;box.scrollLeft=left;
}
function selectPhoto(id){saveDraft();currentId=id;editing=false;showOverlay=false;strokes=[];drawing=null;renderKey='';previewStatus='';tuningBuildKey='';loadDraft();render();saveDecisionDraft();if(tuningNeedsSave())schedulePreview();}
function selectedAsset(p){
  if(showOverlay||editing)return p.overlay;
  if(hasWatermarkComparison())return comparisonSource(p);
  if(matchingQuickPreview(p))return quickPreview;
  const candidate=p.candidates?.[profile];
  if(!candidate)return p.original_preview;
  return $('watermarkEnabled').checked&&candidate.watermarked_preview?candidate.watermarked_preview:candidate.preview;
}
function updateCompare(){
  const p=photo(),on=compare&&!showOverlay&&!editing&&profile!=='keep'&&Boolean(p&&(p.candidates?.[profile]||matchingQuickPreview(p)));
  visible($('beforeClip'),on);visible($('compareLine'),on);visible($('beforeLabel'),on);
  const value=compareValue;$('beforeClip').style.clipPath=`inset(0 ${100-value}% 0 0)`;$('compareLine').style.left=`${value}%`;
  $('compareLine').setAttribute('aria-valuenow',String(value));
  $('compareLine').setAttribute('aria-valuetext',`原片显示 ${value}%`);
  $('compareToggle').classList.toggle('active',compare);$('compareToggle').setAttribute('aria-pressed',String(compare));
  for(const stage of document.querySelectorAll('.watermark-photo')){
    if(!stage.querySelector('.before-clip'))continue;
    const enabled=compare&&Boolean(photo()?.original_preview);
    visible(stage.querySelector('.before-clip'),enabled);visible(stage.querySelector('.compare-line'),enabled);visible(stage.querySelector('.before-label'),enabled);
    stage.querySelector('.before-clip').style.clipPath=`inset(0 ${100-compareValue}% 0 0)`;
    const line=stage.querySelector('.compare-line');line.style.left=`${compareValue}%`;line.setAttribute('aria-valuenow',String(compareValue));
  }
}
function renderPhoto(){
  const p=photo();visible($('emptyState'),!p);visible($('photoStage'),Boolean(p));if(!p)return;
  $('photoName').textContent=p.name;$('photoStatus').textContent=statuses[p.status]||p.status;
  const parts=[];if(p.size)parts.push(`${p.size[0]} × ${p.size[1]}`);if(p.analysis?.tags)parts.push(p.analysis.tags.join(' · '));
  $('photoMeta').textContent=parts.join('  /  ');
  const asset=selectedAsset(p),key=[p.id,asset?.id,p.original_preview?.id,editing].join(':');
  if(key!==renderKey){
    if(asset)$('afterImage').src=media(asset);else $('afterImage').removeAttribute('src');
    if(p.original_preview)$('beforeImage').src=media(p.original_preview);else $('beforeImage').removeAttribute('src');
    if(p.size)$('viewer').style.setProperty('--ratio',p.size[0]/p.size[1]);renderKey=key;
  }
  visible($('preparingOverlay'),!asset);visible($('preparingOverlay').querySelector('.spinner'),!p.error);
  const displayError=p.error?.includes('cannot identify image')?'无法读取此照片，请更换可打开的原图，或说明原因后跳过。':p.error;
  $('preparingOverlay').querySelector('span:last-child').textContent=displayError||(tuningNeedsSave()?'正在更新微调预览':'正在准备当前方案样片');
  $('afterLabel').textContent=editing?'正在编辑选区':showOverlay?'选区覆盖':p.status==='change_requested'?'旧样片 · 修改要求待处理':matchingQuickPreview(p)?'快速预览 · 微调未确认':profile==='keep'?'原片预览':p.candidates?.[profile]?`候选 · ${labels[profile]}`:'方案正在更新 · 暂示原片';
  $('overlayToggle').classList.toggle('active',showOverlay);$('overlayToggle').setAttribute('aria-pressed',String(showOverlay));
  $('editToggle').classList.toggle('active',editing);$('editToggle').setAttribute('aria-pressed',String(editing));
  visible($('drawCanvas'),editing);visible($('maskEditor'),editing);visible($('coverageLegend'),editing||showOverlay);
  $('detailsOpen').disabled=tuningNeedsSave()||!p.candidates?.[profile]?.details?.length;
  $('overlayToggle').disabled=!p.overlay;$('editToggle').disabled=!p.masks||p.status==='analyzing';
  $('skinReviewed').checked=humanReviewed(p);$('skinReviewed').disabled=!p.masks;
  const automaticNote=p.coverage?.deferred_texture_faces?`自动定位中有 ${p.coverage.deferred_texture_faces} 处小脸或五官不可靠，已暂缓这些范围的磨皮。`:'';
  $('photoNotes').textContent=displayError||automaticNote||(p.smoothing_review_current?'已核对可见皮肤覆盖、五官保护与过渡；请继续比较强度和纹理。':p.coverage?.skin_fraction>0?'皮肤选区仍待完整核对，磨皮暂不执行；可继续比较调色。':p.analysis?'未得到可靠皮肤范围，磨皮暂不执行。':'正在分析原片。');
  visible($('repreparePhoto'),Boolean(p.analysis)&&!['analyzing','rendering','sample_pending'].includes(p.status));
  visible($('excludePhoto'),['error','needs_review','sample_ready'].includes(p.status));
  visible($('assumeSrgb'),Boolean(state.photos.some(p=>p.error?.includes('颜色空间'))));
  visible($('downloadPhoto'),p.status==='completed'&&Boolean(p.output));if(p.output)$('downloadPhoto').href=media(p.output,true);
  const history=$('previousOutputs');history.replaceChildren();
  for(const [i,output] of (p.previous_outputs||[]).slice(-3).entries()){const link=document.createElement('a');link.href=media(output,true);link.textContent=`此前成品 ${i+1}`;history.append(link);}
  visible($('warningAcceptLabel'),Boolean(p.error?.includes('高亮')));$('warningAccept').checked=acceptedWarnings[p.id]===true;
  renderWatermarkComparison();updateCompare();if(editing)resizeCanvas();
}
let watermarkComparisonKey='';
function watermarkLayout(size,asset){
  const [w,h]=size,short=Math.min(w,h),width=Math.round(short*(asset.width_fraction??.15)),margin=Math.round(short*(asset.margin_fraction??.025));
  return {width:`${100*width/w}%`,right:`${100*margin/w}%`,bottom:`${100*margin/h}%`};
}
function appendWatermark(stage,asset,size){
  const mark=document.createElement('img');mark.className='comparison-watermark';mark.src=media(asset);mark.alt='';
  Object.assign(mark.style,watermarkLayout(size,asset));stage.append(mark);
}
function bindComparisonDivider(line,stage){
  let pointer=null;
  const move=event=>{const rect=stage.getBoundingClientRect();if(rect.width){compareValue=Math.round(Math.max(0,Math.min(100,(event.clientX-rect.left)/rect.width*100)));updateCompare();}};
  line.addEventListener('pointerdown',event=>{if(!event.isPrimary||event.button!==0)return;event.preventDefault();pointer=event.pointerId;line.setPointerCapture(pointer);line.focus({preventScroll:true});move(event);});
  line.addEventListener('pointermove',event=>{if(event.pointerId===pointer)move(event);});
  const end=event=>{if(event.pointerId!==pointer)return;pointer=null;if(line.hasPointerCapture(event.pointerId))line.releasePointerCapture(event.pointerId);};
  line.addEventListener('pointerup',end);line.addEventListener('pointercancel',end);line.addEventListener('lostpointercapture',()=>{pointer=null;});
  line.addEventListener('keydown',event=>{const step=event.shiftKey?10:1;const next={ArrowLeft:compareValue-step,ArrowRight:compareValue+step,ArrowUp:compareValue+step,ArrowDown:compareValue-step,Home:0,End:100}[event.key];if(next===undefined)return;event.preventDefault();event.stopPropagation();compareValue=Math.max(0,Math.min(100,next));updateCompare();});
}
function comparisonSource(p){
  if(tuningNeedsSave())return matchingQuickPreview(p)?quickPreview:null;
  return p.candidates?.[profile]?.preview||null;
}
function renderWatermarkComparison(){
  const active=hasWatermarkComparison(),p=photo();visible($('watermarkComparison'),active);visible($('singleViewer'),true);
  if(!active){watermarkComparisonKey='';return;}
  const source=comparisonSource(p),dirty=tuningNeedsSave(),options=state.config.watermark_options;
  $('watermarkComparisonStatus').textContent=`${labels[profile]} · ${dirty?(source?'快速预览，确认前请更新样片':'正在更新微调预览'):source?'同一张照片 · 已保存样片':'正在准备当前方案'}${state.config.watermark_selection?'':' · 请在右侧选择水印'}`;
  const key=JSON.stringify([p.id,state.revision,profile,source?.id,dirty,state.config.watermark_selection,p.status]);
  if(key===watermarkComparisonKey)return;watermarkComparisonKey=key;
  const grid=$('watermarkComparisonGrid');grid.replaceChildren();
  for(const option of options){
    const card=document.createElement('figure'),heading=document.createElement('figcaption'),title=document.createElement('strong'),badge=document.createElement('span');
    const selected=option.option_id===state.config.watermark_selection;
    card.className='watermark-comparison-card'+(selected?' selected':'');card.dataset.optionId=option.option_id;card.dataset.photoId=p.id;
    title.textContent=option.label;badge.textContent=selected?'已选':'候选';heading.append(title,badge);card.append(heading);
    const stage=document.createElement('div');stage.className='watermark-photo';stage.style.aspectRatio=p.size[0]/p.size[1];
    if(source){
      const image=document.createElement('img');image.className='comparison-base';image.src=media(source);image.alt=`${p.name} · ${labels[profile]} · ${option.label}叠加预览`;stage.append(image);appendWatermark(stage,option.asset,p.size);
      const clip=document.createElement('div');clip.className='before-clip';const original=document.createElement('img');original.src=media(p.original_preview);original.alt='原片对照';clip.append(original);stage.append(clip);
      const label=document.createElement('span');label.className='image-label before-label';label.textContent='原片';stage.append(label);
      const line=document.createElement('div');line.className='compare-line';line.tabIndex=0;line.setAttribute('role','slider');line.setAttribute('aria-label',`${option.label}原片对比分割线`);line.setAttribute('aria-valuemin','0');line.setAttribute('aria-valuemax','100');bindComparisonDivider(line,stage);stage.append(line);
    }else{const pending=document.createElement('p');pending.className='comparison-pending';pending.textContent=dirty?'正在更新微调预览…':'正在生成当前方案…';stage.append(pending);}
    card.append(stage);
    const zoom=document.createElement('button');zoom.type='button';zoom.className='text-button comparison-zoom';zoom.textContent='放大检查';zoom.setAttribute('aria-label',`放大检查：${option.label}`);zoom.disabled=!source||dirty||!p.candidates?.[profile]?.master;
    zoom.addEventListener('click',()=>openWatermarkComparison(option));stage.classList.toggle('zoomable',!zoom.disabled);
    stage.addEventListener('click',event=>{if(!event.target.closest('.compare-line')&&!zoom.disabled)openWatermarkComparison(option);});
    card.append(zoom);grid.append(card);
  }
}
let watermarkDialogSize=null;
function fitPhotoToArea(size,area){
  const scale=Math.min(area.width/size[0],area.height/size[1]);
  return {width:size[0]*scale,height:size[1]*scale};
}
function fitWatermarkDialog(){
  if(!$('watermarkDialog').open||!watermarkDialogSize)return;
  const area=$('watermarkFitArea').getBoundingClientRect();if(!area.width||!area.height)return;
  const size=fitPhotoToArea(watermarkDialogSize,area);
  Object.assign($('watermarkFitStage').style,{width:`${size.width}px`,height:`${size.height}px`});
}
function openWatermarkComparison(option){
  const p=photo(),candidate=p?.candidates?.[profile];if(!candidate||tuningNeedsSave())return;
  $('watermarkDialogTitle').textContent=`${p.name} · ${option.label}`;
  const stage=$('watermarkFitStage');stage.replaceChildren();
  const image=document.createElement('img');image.className='watermark-fit-base';image.src=media(candidate.master);image.alt=`${labels[profile]} · ${option.label} · 适合窗口`;stage.append(image);
  appendWatermark(stage,option.asset,p.size);watermarkDialogSize=p.size.slice();
  $('watermarkDialog').showModal();fitWatermarkDialog();
}
let choosingWatermark=false;
function renderWatermarks(){
  const config=state.config,mark=config.watermark,options=config.watermark_options||[];
  visible($('watermarkSection'),Boolean(mark||options.length));visible($('watermarkCurrent'),Boolean(mark)&&!options.length);
  if(mark)$('watermarkImage').src=media(mark);
  visible($('watermarkOptions'),options.length>0);visible($('watermarkChoiceHint'),options.length>0);
  $('watermarkOptions').replaceChildren();
  for(const option of options){
    const button=document.createElement('button'),image=document.createElement('img'),title=document.createElement('strong'),caption=document.createElement('small');
    const selected=option.option_id===config.watermark_selection;
    button.className='watermark-option'+(selected?' selected':'');button.type='button';button.setAttribute('aria-pressed',String(selected));button.setAttribute('aria-label',`选择水印：${option.label}`);
    image.src=media(option.asset);image.alt=`${option.label}水印素材`;image.loading='lazy';
    title.textContent=option.label;caption.textContent=selected?'已选 · 照片效果见中间':'水印素材';
    button.append(image,title,caption);button.disabled=choosingWatermark||!['needs_selection','needs_review','needs_changes'].includes(state.status)||Boolean(state.choice)||state.photos.some(p=>p.output||p.approval);
    button.addEventListener('click',async()=>{
      choosingWatermark=true;renderWatermarks();
      try{
        await post('/api/watermark-selection',{revision:state.revision,option_id:option.option_id});
        quickPreview=null;previewSerial++;renderKey='';reviewed={};$('watermarkEnabled').checked=true;saveDecisionDraft();
        notify('已选水印，正在更新含水印的原尺寸样片；核对后再确认批量。');await refresh();
      }catch(error){notify(error.message,true);}
      finally{choosingWatermark=false;render();}
    });
    $('watermarkOptions').append(button);
  }
  $('watermarkChoiceHint').textContent=config.watermark_selection?'中间可比较三版叠加效果；更换后需重新核对样片。':'在右侧选水印，在中间比较三版照片效果；选定后更新原尺寸样片。';
}
function render(){
  if(!state)return;
  if(!currentId||!state.photos.some(p=>p.id===currentId))currentId=state.samples?.[0]||state.photos[0]?.id||null;
  $('activityName').textContent=state.config.activity_name;$('photoCount').textContent=state.photos.length;
  $('applyScope').options[0].textContent=state.status==='completed'?'本批照片（另存新版本）':'本批待处理照片';
  $('runStatus').textContent=state.photos.length?runLabels[state.status]||state.status:'等待添加照片';
  const done=state.photos.filter(p=>p.status==='completed').length,issues=state.photos.filter(p=>['needs_review','error'].includes(p.status)).length;
  const prepared=state.photos.filter(p=>p.candidates&&Object.keys(p.candidates).length>=4).length;
  $('runSummary').textContent=state.status==='preparing'?`共 ${state.photos.length} 张 · 已准备 ${prepared} 张代表样片`:`共 ${state.photos.length} 张 · 完成 ${done} 张${issues?` · ${issues} 张待复核`:''}`;
  visible($('pauseRun'),['running','preparing'].includes(state.status)&&state.photos.length>0);visible($('resumeRun'),state.status==='paused');visible($('retryRun'),issues>0);visible($('downloadAll'),done>0);
  updateCallback();renderList();renderPhoto();renderTuning();
  for(const button of document.querySelectorAll('[data-profile]')){const on=button.dataset.profile===profile;button.classList.toggle('selected',on);button.setAttribute('aria-checked',String(on));
    const retired=photo()?.retained_selected&&photo()?.status==='completed'&&!photo()?.candidates?.[button.dataset.profile];
    button.disabled=Boolean(retired);button.title=retired?'未选方案已归档；需要重新比较时，先为这张另试一版。':'';}
  renderWatermarks();
  const reviewedCount=state.photos.filter(isReviewed).length;
  $('maskReviewSummary').textContent=`${reviewedCount} / ${state.photos.length} 张选区已有检查记录，可沿用或自行复看。修改选区后需重新检查。`;
  $('approveButton').disabled=!['needs_selection','needs_review'].includes(state.status)||!state.sample_binding||!photo()?.analysis;
  if($('applyScope').value==='current'&&!photo()?.candidates?.[profile])$('approveButton').disabled=true;
  if($('applyScope').value==='current'&&['completed','excluded','error'].includes(photo()?.status))$('approveButton').disabled=true;
  const blocker=confirmationBlocker();if(blocker||applyingTuning)$('approveButton').disabled=true;
  $('approveButton').querySelector('span').textContent=state.status==='needs_review'?'确认并继续待处理照片':'确认效果并开始批量';
  $('decisionHint').textContent=['needs_selection','needs_review'].includes(state.status)?'确认会保存本次选择，并自动继续处理。':state.status==='completed'?'本批成品已保存，可下载查看。':'正在处理的任务会自动更新，无需重复确认。';
  if(blocker)$('decisionHint').textContent=blocker;
  const pending=state.pending_request;
  $('revisionStatus').textContent=pending&&pending.status!=='approved'?`已保存要求：${pending.text}（${pending.status==='awaiting_confirmation'?'新样片已就绪，请核对效果':pending.status==='samples_queued'?'正在更新样片':'已回传当前对话，等待处理'}）`:'';
  $('submitChanges').disabled=!$('decisionNote').value.trim()||$('noteIntent').value==='delivery_note'||pending?.status==='pending';
  if(state.updated_at)$('lastSaved').textContent='状态已保存 '+new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:'Asia/Shanghai'}).format(state.updated_at*1000);
  if(state.error)alertMessage(state.error);
  if(state.retention_error)alertMessage('归档尚未完成，原片与所选方案已保留：'+state.retention_error);
}
async function refresh(){
  const response=await fetch('/api/state',{cache:'no-store'});if(!response.ok)throw new Error('无法读取活动状态');
  const next=await response.json(),initial=!state,previewMayNeedRefresh=initial||state.revision!==next.revision||state.status==='preparing';
  if(initial)profile=next.choice?.profile||next.photos.find(p=>p.selected_profile)?.selected_profile||'natural';
  if(state&&['preparing','running'].includes(state.status)&&['needs_selection','needs_review'].includes(next.status)){previewStatus='原尺寸样片已更新，请核对后确认。';quickPreview=null;tuningBuildKey='';}
  state=next;if(initial)restoreDecisionDraft();
  if(lastSeq!==state.seq){lastSeq=state.seq;render();}else updateCallback();
  if(previewMayNeedRefresh&&['needs_selection','needs_review'].includes(state.status)&&tuningNeedsSave())schedulePreview();
}
async function poll(){
  if(polling)return;polling=true;
  try{await refresh();$('callbackStatus').classList.remove('connect-error');}
  catch(error){$('callbackStatus').classList.add('connect-error');$('callbackStatus').querySelector('span').textContent='连接断开，草稿仍保留';}
  finally{polling=false;setTimeout(poll,state&&['running','preparing'].includes(state.status)?1200:2500);}
}
function changeProfile(value){profile=value;previewStatus='';tuningBuildKey='';previewSerial++;saveDecisionDraft();renderKey='';render();if(tuningNeedsSave())schedulePreview();}
for(const button of document.querySelectorAll('[data-profile]'))button.addEventListener('click',()=>changeProfile(button.dataset.profile));
$('applyScope').addEventListener('change',()=>{saveDecisionDraft();render();});
$('decisionNote').addEventListener('input',()=>{saveDecisionDraft();render();});
$('noteIntent').addEventListener('change',()=>{saveDecisionDraft();render();});
$('submitChanges').addEventListener('click',submitChangeRequest);
$('applyTuning').addEventListener('click',applyTuning);
$('resetTuning').addEventListener('click',()=>{tuningDrafts[tuningKey()]={values:{...(state.tuning_defaults?.[profile]||{})},revision:state.revision};quickPreview=null;previewSerial++;previewStatus='';saveDecisionDraft();render();if(tuningNeedsSave())schedulePreview();});
$('photoFilter').addEventListener('change',renderList);
$('compareToggle').addEventListener('click',()=>{compare=!compare;updateCompare();});
// Capture only the divider so photo scrolling and mask editing keep their own gestures.
let comparePointer=null;
function moveCompare(event){
  const bounds=$('viewer').getBoundingClientRect();
  if(bounds.width<=0)return;
  compareValue=Math.round(Math.max(0,Math.min(100,(event.clientX-bounds.left)/bounds.width*100)));
  updateCompare();
}
$('compareLine').addEventListener('pointerdown',event=>{
  if(!event.isPrimary||event.button!==0||$('compareLine').classList.contains('hidden'))return;
  event.preventDefault();comparePointer=event.pointerId;
  $('compareLine').setPointerCapture(event.pointerId);$('compareLine').focus({preventScroll:true});moveCompare(event);
});
$('compareLine').addEventListener('pointermove',event=>{if(event.pointerId===comparePointer)moveCompare(event);});
function endCompare(event){
  if(event.pointerId!==comparePointer)return;
  comparePointer=null;
  if($('compareLine').hasPointerCapture(event.pointerId))$('compareLine').releasePointerCapture(event.pointerId);
}
$('compareLine').addEventListener('pointerup',endCompare);
$('compareLine').addEventListener('pointercancel',endCompare);
$('compareLine').addEventListener('lostpointercapture',()=>{comparePointer=null;});
$('compareLine').addEventListener('keydown',event=>{
  const step=event.shiftKey?10:1,value=compareValue;
  const next={ArrowLeft:value-step,ArrowDown:value-step,ArrowRight:value+step,ArrowUp:value+step,Home:0,End:100}[event.key];
  if(next===undefined)return;
  event.preventDefault();event.stopPropagation();compareValue=Math.max(0,Math.min(100,next));updateCompare();
});
$('overlayToggle').addEventListener('click',()=>{showOverlay=!showOverlay;editing=false;renderKey='';renderPhoto();if(tuningNeedsSave())schedulePreview();});
$('editToggle').addEventListener('click',()=>{editing=!editing;showOverlay=editing;editingRevision=state.revision;loadDraft();renderKey='';renderPhoto();if(tuningNeedsSave())schedulePreview();});
$('watermarkEnabled').addEventListener('change',()=>{saveDecisionDraft();renderKey='';render();if(tuningNeedsSave())schedulePreview();});
$('skinReviewed').addEventListener('change',()=>{const p=photo();if(!p)return;reviewed[p.id]=$('skinReviewed').checked?maskKey(p):null;saveDecisionDraft();render();});
$('reviewAll').addEventListener('click',()=>{for(const p of state.photos)if(p.masks)reviewed[p.id]=maskKey(p);saveDecisionDraft();render();notify('已记录本批展示选区的核对结果');});
$('warningAccept').addEventListener('change',()=>{if(photo())acceptedWarnings[photo().id]=$('warningAccept').checked;saveDecisionDraft();});
$('pauseRun').addEventListener('click',()=>control('pause'));
$('resumeRun').addEventListener('click',()=>control('resume'));
$('retryRun').addEventListener('click',()=>control('retry'));
$('callbackTest').addEventListener('click',async()=>{if(await control('callback-test'))notify('回传检查已排队，状态将在收到后更新');});
$('callbackRetry').addEventListener('click',async()=>{const event=state.outbox.find(e=>['failed','delivery_unknown'].includes(e.status));if(event)await control('callback-retry',{event_id:event.event_id});});
$('repreparePhoto').addEventListener('click',()=>control('reprepare',{photo_id:currentId}));
$('assumeSrgb').addEventListener('click',()=>control('assume-srgb'));
$('excludePhoto').addEventListener('click',()=>{const reason=window.prompt('请简短说明跳过原因（原片会保留）');if(reason?.trim())control('exclude',{photo_id:currentId,reason:reason.trim()});});
$('approveButton').addEventListener('click',async()=>{
  const button=$('approveButton');button.disabled=true;
  try{
    const blocker=confirmationBlocker();if(blocker)throw new Error(blocker);
    const choice={profile,watermark:Boolean(state.config.watermark&&$('watermarkEnabled').checked),overrides:{},
      skin_reviewed:state.photos.filter(humanReviewed).map(p=>p.id),accept_warnings:Object.keys(acceptedWarnings).filter(id=>acceptedWarnings[id]),note:$('decisionNote').value.trim(),note_intent:$('noteIntent').value};
    if(choice.watermark&&state.config.watermark_selection)choice.watermark_option_id=state.config.watermark_selection;
    if($('applyScope').value==='current')choice.approved_photo_ids=[currentId];
    await post('/api/decision',{revision:state.revision,binding:state.sample_binding,choice,request_id:crypto.randomUUID()});
    $('decisionNote').value='';saveDecisionDraft();notify('效果与完整参数已保存，开始继续处理');await refresh();
  }catch(error){notify(error.message,true);button.disabled=false;}
});
async function upload(files){
  if(!state)return;let done=0;const errors=[];const usable=[...files].filter(f=>!f.name.startsWith('.'));
  $('addPhotos').disabled=true;$('addFolder').disabled=true;
  try{
    for(const file of usable){
      if(file.size>256*1024*1024){errors.push(`${file.name}：超过当前接收大小`);continue;}
      try{const response=await fetch('/api/import',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-CSRF-Token':state.csrf,'X-File-Name':encodeURIComponent(file.name)},body:file});const result=await response.json();if(!response.ok)throw new Error(result.error);done++;$('runSummary').textContent=`正在归档 ${done} / ${usable.length} 张`;}
      catch(error){errors.push(`${file.name}：${error.message}`);}
    }
    if(done)await post('/api/import-done',{received:done});
    if(errors.length)alertMessage(errors.join('；'));
    notify(`已归档 ${done} 张${errors.length?`，${errors.length} 张未接收`:''}`,errors.length>0);await refresh();
  }catch(error){notify(error.message,true);}
  finally{$('addPhotos').disabled=false;$('addFolder').disabled=false;}
}
$('addPhotos').addEventListener('click',()=>$('photoInput').click());$('emptyAdd').addEventListener('click',()=>$('photoInput').click());$('addFolder').addEventListener('click',()=>$('folderInput').click());
for(const id of ['photoInput','folderInput'])$(id).addEventListener('change',event=>{upload(event.target.files);event.target.value='';});

function draftKey(){return state&&currentId?`photo-review-draft:${state.run_id}:${currentId}`:null;}
function saveDraft(){const key=draftKey();if(!key||!photo())return;try{localStorage.setItem(key,JSON.stringify({revision:editingRevision,source:photo().input.sha256,strokes}));}catch(error){/* Server state remains authoritative. */}}
function loadDraft(){
  const p=photo(),key=draftKey();if(!key||!p)return;
  try{const saved=JSON.parse(localStorage.getItem(key)||'null');if(saved?.source===p.input.sha256&&saved.revision===state.revision){strokes=saved.strokes||[];editingRevision=saved.revision;}else if(saved?.strokes?.length){notify('已有旧版选区草稿，当前样片已更新，请重新核对后绘制。',true);strokes=[];}}
  catch(error){strokes=[];}
}
function resizeCanvas(){const canvas=$('drawCanvas'),rect=canvas.getBoundingClientRect();if(!rect.width)return;canvas.width=Math.round(rect.width*devicePixelRatio);canvas.height=Math.round(rect.height*devicePixelRatio);drawStrokes();}
function point(event){const r=$('drawCanvas').getBoundingClientRect();return [Math.max(0,Math.min(1,(event.clientX-r.left)/r.width)),Math.max(0,Math.min(1,(event.clientY-r.top)/r.height))];}
function drawStrokes(){
  const canvas=$('drawCanvas'),ctx=canvas.getContext('2d');ctx.clearRect(0,0,canvas.width,canvas.height);
  for(const stroke of [...strokes,...(drawing?[drawing]:[])]){
    const color=stroke.erase?'rgba(244,247,241,.8)':stroke.layer==='protect'?'rgba(35,122,235,.6)':'rgba(241,119,28,.6)';
    const points=stroke.points.map(([x,y])=>[x*canvas.width,y*canvas.height]);ctx.strokeStyle=color;ctx.fillStyle=color;ctx.lineWidth=stroke.radius*Math.min(canvas.width,canvas.height)*2;ctx.lineCap='round';ctx.lineJoin='round';
    if(stroke.tool==='rect'){const [a,b]=[points[0],points.at(-1)];ctx.fillRect(Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.abs(b[0]-a[0]),Math.abs(b[1]-a[1]));}
    else{ctx.beginPath();ctx.moveTo(...points[0]);for(const p of points.slice(1))ctx.lineTo(...p);ctx.stroke();}
  }
}
$('drawCanvas').addEventListener('pointerdown',event=>{if(!editing)return;event.preventDefault();$('drawCanvas').setPointerCapture(event.pointerId);editingRevision=editingRevision??state.revision;drawing={layer:$('maskLayer').value,tool:$('maskTool').value,erase:$('maskErase').checked,radius:Number($('brushSize').value)/100,points:[point(event)]};drawStrokes();});
$('drawCanvas').addEventListener('pointermove',event=>{if(!drawing)return;const p=point(event);if(drawing.tool==='rect')drawing.points=[drawing.points[0],p];else if(drawing.points.length<1900)drawing.points.push(p);drawStrokes();});
function endStroke(){if(!drawing)return;if(drawing.tool==='rect'&&drawing.points.length===1)drawing.points.push(drawing.points[0]);strokes.push(drawing);drawing=null;saveDraft();drawStrokes();}
$('drawCanvas').addEventListener('pointerup',endStroke);$('drawCanvas').addEventListener('pointercancel',endStroke);
$('undoStroke').addEventListener('click',()=>{strokes.pop();saveDraft();drawStrokes();});
$('saveMask').addEventListener('click',async()=>{
  if(!strokes.length){notify('先绘制需要修改的选区');return;}
  const p=photo(),button=$('saveMask'),submitted=strokes.slice(),submittedLength=strokes.length,key=draftKey(),submittedRevision=editingRevision??state.revision;
  const prefix=JSON.stringify(submitted);saveDraft();button.disabled=true;$('approveButton').disabled=true;
  try{const reply=await post('/api/mask',{photo_id:p.id,revision:submittedRevision,input_sha256:p.input.sha256,strokes:submitted});
    const revision=reply.revision??submittedRevision+1;
    try{const saved=JSON.parse(localStorage.getItem(key)||'null');if(saved?.source===p.input.sha256&&JSON.stringify(saved.strokes.slice(0,submittedLength))===prefix)localStorage.setItem(key,JSON.stringify({revision,source:p.input.sha256,strokes:saved.strokes.slice(submittedLength)}));}catch(error){/* Keep any unrecognized draft rather than deleting it. */}
    if(currentId===p.id&&JSON.stringify(strokes.slice(0,submittedLength))===prefix){strokes=strokes.slice(submittedLength);editing=strokes.length>0;showOverlay=editing;renderKey='';editingRevision=revision;saveDraft();}
    reviewed[p.id]=null;saveDecisionDraft();notify(`${p.name} 选区已保存，正在更新样片`);await refresh();}
  catch(error){notify(error.message,true);}finally{button.disabled=false;}
});
window.addEventListener('resize',()=>{if(editing)resizeCanvas();fitWatermarkDialog();});
if(typeof ResizeObserver!=='undefined')new ResizeObserver(fitWatermarkDialog).observe($('watermarkFitArea'));
$('watermarkDialogClose').addEventListener('click',()=>$('watermarkDialog').close());
window.addEventListener('beforeunload',()=>saveDraft());
$('detailsOpen').addEventListener('click',()=>{
  const candidate=photo()?.candidates?.[profile];if(!candidate)return;const list=$('detailList');list.replaceChildren();
  for(const detail of candidate.details){const section=document.createElement('section');section.className='detail-item';const title=document.createElement('h3');title.textContent=detail.label;section.append(title);const pair=document.createElement('div');pair.className='detail-pair';for(const [caption,ref] of [['原片 · 原像素',detail.original],[`${labels[profile]} · 原像素`,detail.result]]){const figure=document.createElement('figure'),label=document.createElement('figcaption'),image=document.createElement('img');label.textContent=caption;image.src=media(ref);image.alt=`${detail.label} ${caption}`;figure.append(label,image);pair.append(figure);}section.append(pair);list.append(section);}
  $('detailsDialog').showModal();
});
$('detailsClose').addEventListener('click',()=>$('detailsDialog').close());
$('fullImageOpen').addEventListener('click',()=>{
  const p=photo(),candidate=p?.candidates?.[profile];if(!candidate||!p.baseline)return;
  const list=$('detailList');list.replaceChildren();const title=document.createElement('h3');title.textContent='整幅原像素 · 可横向、纵向滚动检查文字与边角';list.append(title);
  const pair=document.createElement('div');pair.className='detail-pair';
  const chosen=$('watermarkEnabled').checked&&candidate.watermarked_inspection?candidate.watermarked_inspection:candidate.master;
  for(const [caption,ref] of [['原片色管基准',p.baseline],[`${labels[profile]}${chosen===candidate.watermarked_inspection?' · 含水印':''}`,chosen]]){const figure=document.createElement('figure'),label=document.createElement('figcaption'),image=document.createElement('img');label.textContent=caption;image.src=media(ref);image.alt=caption+' 整幅原像素';figure.append(label,image);pair.append(figure);}list.append(pair);
});
document.addEventListener('keydown',event=>{if(!state||['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)||$('detailsDialog').open||$('watermarkDialog').open)return;const index=state.photos.findIndex(p=>p.id===currentId);if(event.key==='ArrowRight'&&index<state.photos.length-1)selectPhoto(state.photos[index+1].id);if(event.key==='ArrowLeft'&&index>0)selectPhoto(state.photos[index-1].id);if(['1','2','3'].includes(event.key)){profile=['natural','clear','portrait'][Number(event.key)-1];renderKey='';render();}});
poll();
