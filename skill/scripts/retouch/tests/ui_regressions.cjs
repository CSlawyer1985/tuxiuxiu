// Exercise the shipped UI script, including async handlers, without a browser.
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.resolve(__dirname,'../../../assets/photo-review/app.js'),'utf8').replace(/poll\(\);\s*$/,'');
function fixture(){
  const elements=new Map(),saved=new Map();
  function element(id){if(!elements.has(id))elements.set(id,{value:'',checked:false,handlers:{},addEventListener(name,fn){this.handlers[name]=fn;},classList:{toggle(){},add(){},remove(){}},disabled:false});return elements.get(id);}
  const context=vm.createContext({document:{getElementById:element,querySelectorAll:()=>[],addEventListener(){}},window:{addEventListener(){}},localStorage:{getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v)},setTimeout,clearTimeout,Intl,Map,JSON,Boolean,Number,Object});
  vm.runInContext(source,context);
  vm.runInContext(`state={run_id:'synthetic-ui',revision:1,photos:[{id:'A',input:{sha256:'a'}},{id:'B',input:{sha256:'b'}}]};currentId='A';editing=true;editingRevision=1;strokes=[{layer:'protect',points:[[0,0],[.1,.1]]}];render=()=>{};renderPhoto=()=>{};notify=()=>{};refresh=async()=>{};var resolveSave;post=()=>new Promise(resolve=>resolveSave=resolve);`,context);
  return {context,saved,element};
}
function galleryFixture(){
  const roots=new Map(),nodes=[];
  function node(tag='div'){
    const value={tagName:tag.toUpperCase(),children:[],dataset:{},style:{},handlers:{},attributes:{},value:'',checked:false,className:'',
      append(...items){this.children.push(...items);},replaceChildren(...items){this.children=[...items];},
      addEventListener(name,fn){this.handlers[name]=fn;},setAttribute(k,v){this.attributes[k]=v;},getAttribute(k){return this.attributes[k];},
      querySelector(selector){return this.querySelectorAll(selector)[0];},
      querySelectorAll(selector){const found=[];for(const child of this.children){if(selector.startsWith('.')&&child.className.split(' ').includes(selector.slice(1)))found.push(child);found.push(...child.querySelectorAll(selector));}return found;},
      getBoundingClientRect(){return {left:0,width:400};},focus(){},setPointerCapture(){},hasPointerCapture(){return false;}};
    value.classList={contains:name=>value.className.split(' ').includes(name),toggle(name,on){const names=new Set(value.className.split(' ').filter(Boolean));if(on)names.add(name);else names.delete(name);value.className=[...names].join(' ');},add(name){this.toggle(name,true);},remove(name){this.toggle(name,false);}};
    nodes.push(value);return value;
  }
  const element=id=>{if(!roots.has(id))roots.set(id,node());return roots.get(id);};
  const context=vm.createContext({document:{getElementById:element,createElement:node,querySelectorAll:selector=>[...roots.values()].flatMap(root=>root.querySelectorAll(selector)),addEventListener(){}},window:{addEventListener(){}},localStorage:{getItem:()=>null,setItem(){}},setTimeout,clearTimeout,Intl,Map,JSON,Boolean,Number,Object});
  vm.runInContext(source,context);
  vm.runInContext(`
    state={run_id:'synthetic-gallery',revision:1,status:'needs_selection',photos:[
      {id:'A',name:'A.jpg',size:[1000,500],status:'sample_ready',original_preview:{id:'a-original'},candidates:{natural:{preview:{id:'a-natural'},master:{id:'a-master'}}}},
      {id:'B',name:'B.jpg',size:[400,800],status:'sample_pending',original_preview:{id:'b-original'},candidates:{}}
    ],config:{watermark:{id:'m2'},watermark_selection:'two',watermark_options:[
      {option_id:'one',label:'纯文字',asset:{id:'m1',width_fraction:.2,margin_fraction:.02},previews:[{photo_id:'A',image:{id:'old-a-one'}}]},
      {option_id:'two',label:'主题标志',asset:{id:'m2',width_fraction:.2,margin_fraction:.02},previews:[]},
      {option_id:'three',label:'自由设计',asset:{id:'m3',width_fraction:.2,margin_fraction:.02},previews:[]}
    ]},tuning_controls:[{key:'exposure_ev',mode:'delta'}],tuning_defaults:{natural:{exposure_ev:0}}};
    currentId='A';profile='natural';render=()=>{};
  `,context);
  element('watermarkEnabled').checked=true;element('applyScope').value='current';
  return {context,element};
}
(async()=>{
  for(const addOnA of [false,true]){
    const f=fixture(),saving=f.element('saveMask').handlers.click();
    if(addOnA)vm.runInContext(`strokes.push({layer:'skin',points:[[.2,.2],[.3,.3]]});saveDraft();`,f.context);
    vm.runInContext(`selectPhoto('B');editing=true;strokes=[{layer:'skin',points:[[.5,.5],[.7,.7]]}];saveDraft();resolveSave({accepted:true,revision:2});`,f.context);
    await saving;
    assert.equal(JSON.parse(f.saved.get('photo-review-draft:synthetic-ui:B')).strokes.length,1);
    assert.equal(JSON.parse(f.saved.get('photo-review-draft:synthetic-ui:A')).strokes.length,addOnA?1:0);
    assert.equal(vm.runInContext('editing',f.context),true);
  }
  const f=fixture();f.element('applyScope').value='current';f.element('watermarkEnabled').checked=false;f.element('decisionNote').value='仅此张保持自然';
  vm.runInContext(`profile='portrait';saveDecisionDraft();profile='natural';currentId='B';`,f.context);
  f.element('applyScope').value='batch';f.element('watermarkEnabled').checked=true;f.element('decisionNote').value='';
  vm.runInContext('restoreDecisionDraft()',f.context);
  assert.equal(f.element('applyScope').value,'current');assert.equal(f.element('watermarkEnabled').checked,false);
  assert.equal(f.element('decisionNote').value,'仅此张保持自然');assert.equal(vm.runInContext('profile',f.context),'portrait');
  const pending=fixture();pending.element('decisionNote').value='脸部再亮一点';pending.element('noteIntent').value='change_request';pending.element('applyScope').value='current';
  vm.runInContext(`profile='clear';state.pending_request={status:'pending',text:'脸部再亮一点'};state.tuning_controls=[{key:'exposure_ev',mode:'delta'}];state.tuning_defaults={clear:{exposure_ev:0}};tuningDrafts['A:clear']={values:{exposure_ev:.2}};var posted=[];post=async(path)=>{posted.push(path);return {accepted:true,photo_ids:['A'],revision:2}};`,pending.context);
  await vm.runInContext('applyTuning()',pending.context);
  assert.equal(vm.runInContext('posted[0]',pending.context),'/api/tuning','submitted pending note must permit updating tuning without re-submitting the note');
  const completed=fixture();completed.element('applyScope').value='batch';
  vm.runInContext(`profile='clear';state.photos.forEach(p=>p.status='completed');state.tuning_controls=[{key:'exposure_ev',mode:'delta'}];state.tuning_defaults={clear:{exposure_ev:0}};tuningDrafts['A:clear']={values:{exposure_ev:.2}};`,completed.context);
  assert.equal(vm.runInContext('tuningNeedsSave()',completed.context),true,'completed activity can preview a proposed new version without overwriting old files');
  const gallery=galleryFixture(),run=code=>vm.runInContext(code,gallery.context),grid=gallery.element('watermarkComparisonGrid');
  run('renderWatermarkComparison();renderWatermarks();');
  assert.equal(grid.children.length,3);
  assert.equal(gallery.element('singleViewer').classList.contains('hidden'),false,'watermark comparisons cannot hide the primary retouch viewer');
  assert.equal(run('selectedAsset(photo()).id'),'a-natural','primary retouch comparison uses the same unmarked effect as the watermark candidates');
  assert.deepEqual(grid.children.map(c=>c.dataset.photoId),['A','A','A']);
  assert.deepEqual(grid.children.map(c=>c.querySelector('.comparison-base').src),['/media/a-natural','/media/a-natural','/media/a-natural']);
  assert.deepEqual(gallery.element('watermarkOptions').children.map(c=>c.children[0].src),['/media/m1','/media/m2','/media/m3'],'sidebar must show independent watermark assets, never precomposited photographs');
  assert.equal(gallery.element('watermarkCurrent').classList.contains('hidden'),true,'do not duplicate the selected asset below the options');
  assert.deepEqual(JSON.parse(run('JSON.stringify(watermarkLayout([1000,500],{width_fraction:.2,margin_fraction:.02}))')),{width:'10%',right:'1%',bottom:'2%'});
  for(const [size,area] of [[[6345,4230],{width:900,height:500}],[[500,724],{width:900,height:500}],[[500,724],{width:300,height:600}]]){
    const fit=JSON.parse(run(`JSON.stringify(fitPhotoToArea(${JSON.stringify(size)},${JSON.stringify(area)}))`));
    assert.ok(fit.width<=area.width+1e-8&&fit.height<=area.height+1e-8,'complete photo must fit both dimensions of the popup');
    assert.ok(Math.abs(fit.width/fit.height-size[0]/size[1])<1e-8,'popup must preserve photograph and watermark proportions');
  }
  assert.deepEqual(JSON.parse(run('JSON.stringify(fitPhotoToArea([6345,4230],{width:900,height:500}))')),{width:750,height:500});
  run("currentId='B';renderWatermarkComparison();updateCompare();");
  assert.equal(grid.children.length,3);assert.ok(grid.children.every(c=>!c.querySelector('.comparison-base')),'missing current candidate must not reuse a different photo');
  run("state.photos[1].candidates.natural={preview:{id:'b-natural'},master:{id:'b-master'}};renderWatermarkComparison();");
  assert.ok(grid.children.every(c=>c.querySelector('.comparison-base').src==='/media/b-natural'));
  run("tuningDrafts['B:natural']={values:{exposure_ev:.2}};quickPreview={id:'wrong',photo_id:'A',profile:'natural',revision:1,marked:false};renderWatermarkComparison();");
  assert.ok(grid.children.every(c=>!c.querySelector('.comparison-base')),'stale quick responses must not appear in another photo');
  run("quickPreview={id:'already-marked',photo_id:'B',profile:'natural',revision:1,marked:true};renderWatermarkComparison();");
  assert.ok(grid.children.every(c=>!c.querySelector('.comparison-base')),'do not stack a second watermark on a marked quick preview');
  run("quickPreview={id:'fresh-base',photo_id:'B',profile:'natural',revision:1,marked:false};renderWatermarkComparison();compareValue=37;updateCompare();");
  assert.ok(grid.children.every(c=>c.querySelector('.comparison-base').src==='/media/fresh-base'));
  assert.ok(grid.children.every(c=>c.querySelector('.before-clip').style.clipPath==='inset(0 63% 0 0)'));
  assert.equal(run('state.config.watermark_selection'),'two','rendering comparisons cannot choose or approve a watermark');
  const html=fs.readFileSync(path.resolve(__dirname,'../../../assets/photo-review/index.html'),'utf8');
  assert.ok(html.indexOf('id="singleViewer"')<html.indexOf('id="watermarkComparison"'),'primary retouch viewer must come before watermark comparisons');
  assert.ok(html.indexOf('id="profiles"')<html.indexOf('id="watermarkSection"'),'retouch choices must come before watermark choices');
  assert.ok(html.indexOf('id="tuningPanel"')<html.indexOf('id="watermarkSection"'),'retouch parameters must come before watermark choices');
  console.log('UI async save and decision draft regressions passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
