import butterchurn from './vendor/butterchurn/butterchurn.min.js?v=10.9.1';
/* Pulse Vinyl v10 — local Butterchurn runtime + Noisemaker shader runtime. */
const SHADER_CDN = 'https://shaders.noisedeck.app/1';
const PRESET_MANIFEST_URL = './presets/butterchurn/manifest.json';
const $ = id => document.getElementById(id);
let selected = 'vinyl', bcModule = null, bcLoadPromise = null, bcInstance = null, bcCanvas = null, bcSensitivityGain = null, bcPresetCache = new Map(), bcActivePreset = '', bcManifest = null;
let nmModule = null, nmRenderer = null, nmCanvas = null, nmMode = '', nmAudioState = null, loading = false, loadPromise = null, nmBackend = 'WebGL 2';
let lastError = '', lastWidth = 0, lastHeight = 0, bcLoadError = null, bcStage = 'ожидание';
const nmPrograms = {
 'noisemaker-flow': `search synth, filter
noise(octaves: 5, scaleX: audio(band: audioBand.low, min: 20, max: 95), scaleY: audio(band: audioBand.mid, min: 12, max: 58), speed: audio(band: audioBand.high, min: 0.4, max: 2.8))
  .bloom(intensity: 0.55)
  .write(o0)
render(o0)`,
 'noisemaker-tunnel': `search synth, filter
noise(octaves: 4, scaleX: 38, scaleY: 38, speed: audio(band: audioBand.low, min: 0.5, max: 3.2))
  .tunnel(scale: audio(band: audioBand.low, min: -0.8, max: 1.4), speed: audio(band: audioBand.mid, min: -2, max: 2))
  .bloom(intensity: 0.7)
  .write(o0)
render(o0)`,
 // The public Noisemaker CDN does not expose synth/fractal.js. Build Nebula from the known-good noise synth instead.
 'noisemaker-nebula': `search synth, filter
noise(octaves: 7, scaleX: audio(band: audioBand.low, min: 7, max: 22), scaleY: audio(band: audioBand.mid, min: 9, max: 31), speed: audio(band: audioBand.high, min: 0.08, max: 0.75))
  .bloom(intensity: audio(band: audioBand.low, min: 0.3, max: 0.9))
  .write(o0)
render(o0)`
};
function describeError(err){if(err==null)return 'неизвестная ошибка';if(typeof err==='string')return err;if(err.message)return err.message;try{return JSON.stringify(err)}catch{return String(err)}}
function setError(message, error=null) { const isNew=message!==lastError; lastError = message; if(isNew){console.warn('[Pulse Vinyl visualizers]', message); if(error){console.error('[Pulse Vinyl visualizers] Полная диагностика:', {stage:bcStage, name:error.name||'Error', message:error.message||String(error), stack:error.stack||'(stack отсутствует)', cause:error.cause||null, causeStack:error.cause?.stack||null, error});}} const hint=$('vizHint'); if(hint) hint.textContent = message; }
function resizeCanvas(canvas,w,h){const dpr=Math.min(devicePixelRatio||1,1.6);const W=Math.max(1,Math.round(w*dpr)),H=Math.max(1,Math.round(h*dpr));if(canvas.width!==W||canvas.height!==H){canvas.width=W;canvas.height=H;}return {width:W,height:H};}
async function loadButterchurn(){
 bcStage='1/6: проверка локальной библиотеки';
 if(bcModule)return bcModule;
 // Butterchurn 3 is an ES module with bundled eel-wasm support.
 // Its EEL runtime compiles native MilkDrop equations asynchronously.
 const candidate=butterchurn?.default||butterchurn;
 if(!candidate?.createVisualizer){
  throw new Error('Локальный Butterchurn 3 не найден или не экспортирует createVisualizer(). Проверь vendor/butterchurn/butterchurn.min.js.');
 }
 bcModule=candidate;
 return bcModule;
}
async function loadButterchurnPreset(id){
 bcStage='2/6: загрузка manifest.json';
 if(bcPresetCache.has(id))return bcPresetCache.get(id);
 if(!bcManifest){
  const response=await fetch(PRESET_MANIFEST_URL,{cache:'force-cache'});
  if(!response.ok)throw new Error(`Не удалось загрузить локальный список MilkDrop-пресетов (${response.status}).`);
  bcManifest=await response.json();
 }
 const item=bcManifest.find(x=>x.id===id);
 if(!item)throw new Error(`Неизвестная MilkDrop-сцена: ${id}`);
 bcStage=`3/6: загрузка JSON пресета ${item.file}`;
 const response=await fetch(`./presets/butterchurn/${item.file}`,{cache:'force-cache'});
 if(!response.ok)throw new Error(`Не удалось загрузить пресет ${item.name} (${response.status}).`);
 bcStage=`4/6: разбор JSON пресета ${item.file}`;
 const preset=await response.json();
 bcPresetCache.set(id,preset);
 return preset;
}
async function startButterchurn({ctx,source,analyser,width,height,presetId}){
 bcStage='1/6: инициализация Butterchurn';
 await loadButterchurn();
 const canvas=$('engineViz'); if(!canvas)throw new Error('Этап 1/4: не найден canvas #engineViz.');
 if(!ctx||!analyser)throw new Error('Этап 1/4: аудиоконтекст или анализатор не созданы.');
 if(!width||!height)throw new Error(`Этап 1/4: неверный размер холста ${width}×${height}.`);
 if(bcCanvas!==canvas){bcInstance=null;bcCanvas=canvas;bcActivePreset='';bcSensitivityGain=null;}
 const dims=resizeCanvas(canvas,width,height);
 if(!bcInstance){
  if(bcModule.isSupported && !bcModule.isSupported())throw new Error('Этап 2/4: Butterchurn сообщает, что WebGL 2 недоступен.');
  bcStage='5/6: создание WebGL-рендерера';
  try{bcInstance=bcModule.createVisualizer(ctx,canvas,{width:dims.width,height:dims.height,pixelRatio:1,textureRatio:0.75});}catch(e){throw new Error(`Этап 2/4: не удалось создать Butterchurn WebGL-рендерер: ${e?.message||e}`)}
  if(!bcInstance)throw new Error('Этап 2/4: createVisualizer() не вернул экземпляр движка.');
  try{
   // Branch the existing analyser output into MilkDrop; keep the player's destination path untouched.
   bcSensitivityGain=ctx.createGain();
   bcSensitivityGain.gain.value=1.3;
   analyser.connect(bcSensitivityGain);
   bcInstance.connectAudio(bcSensitivityGain);
  }catch(e){throw new Error(`Этап 3/4: Butterchurn не подключился к аудио: ${e?.message||e}`)}
 }
 bcStage='5/6: изменение размера WebGL-рендерера';
 try{bcInstance.setRendererSize(dims.width,dims.height)}catch(e){throw new Error(`setRendererSize() завершился ошибкой: ${e?.message||e}`,{cause:e})}
 if(presetId && bcActivePreset!==presetId){
  const preset=await loadButterchurnPreset(presetId);
  // A preset is a fixed scene. No timed/random preset rotation is performed.
  bcStage=`5/6: применение пресета ${presetId}`;
  try{await bcInstance.loadPreset(preset,1.25)}catch(e){throw new Error(`loadPreset() завершился ошибкой для ${presetId}: ${e?.message||e}`,{cause:e})}
  bcActivePreset=presetId;
  const hint=$('vizHint');
  const entry=bcManifest?.find(x=>x.id===presetId);
  if(hint&&entry)hint.textContent=`Butterchurn · ${entry.name}`;
 }
 bcStage='6/6: отрисовка кадра';
 try{bcInstance.render()}catch(e){throw new Error(`render() завершился ошибкой: ${e?.message||e}`,{cause:e})}
}
async function loadNoisemaker(){
 if(nmModule)return nmModule;
 if(!window.WebGL2RenderingContext && !navigator.gpu) throw new Error('Браузер не предоставляет ни WebGL 2, ни WebGPU.');
 nmModule=await import(`${SHADER_CDN}/noisemaker-shaders-core.esm.min.js`);
 if(!nmModule.CanvasRenderer)throw new Error('Модуль Noisemaker загрузился, но CanvasRenderer не найден.');
 return nmModule;
}
function checkWebGL2(){
 const probe=document.createElement('canvas');
 const gl=probe.getContext('webgl2');
 if(gl){const version=gl.getParameter(gl.VERSION);const renderer=gl.getParameter(gl.RENDERER);const vendor=gl.getParameter(gl.VENDOR);const result={version,renderer,vendor};gl.getExtension('WEBGL_lose_context')?.loseContext();return result;}
 return null;
}
async function startNoisemaker(mode,width,height){
 const canvas=$('customViz'); if(!canvas)throw new Error('Этап 1/5: не найден canvas #customViz.');
 if(!width||!height)throw new Error(`Этап 1/5: неверный размер холста ${width}×${height}.`);
 const mod=await loadNoisemaker();
 const dims=resizeCanvas(canvas,width,height);
 if(!nmRenderer||nmCanvas!==canvas){
  try{nmRenderer?.stop?.();nmRenderer?.dispose?.()}catch{}
  nmCanvas=canvas;
  const gpuInfo=checkWebGL2();
  if(!gpuInfo&&!navigator.gpu)throw new Error('Этап 2/5: WebGL 2 недоступен и WebGPU не обнаружен.');
  nmRenderer=new mod.CanvasRenderer({canvas,width:dims.width,height:dims.height,basePath:SHADER_CDN,useBundles:true,bundlePath:`${SHADER_CDN}/effects`,preferWebGPU:false,onError:(err)=>setError(`Noisemaker: ошибка GPU-рендерера: ${describeError(err)}`)});
  try{await nmRenderer.loadManifest();}catch(e){throw new Error(`Этап 3/5: не удалось загрузить каталог шейдеров с ${SHADER_CDN}: ${e?.message||e}`)}
  if(!nmRenderer.manifest)throw new Error('Этап 3/5: движок не вернул каталог шейдеров.');
  nmAudioState=nmRenderer.setAudioState?.();
  if(!nmAudioState)throw new Error('Этап 3/5: движок не создал AudioState для реакции на музыку.');
 }
 nmRenderer.resize?.(dims.width,dims.height);
 if(nmMode!==mode){
  // Stop the previous preset before any loading/compilation, so failed switches never leave the old image running.
  try{nmRenderer.stop?.()}catch{};nmMode='';
  const dsl=nmPrograms[mode]; if(!dsl)throw new Error('Неизвестный пресет Noisemaker: '+mode);
  const needed=mode==='noisemaker-tunnel' ? ['synth/noise','filter/tunnel','filter/bloom'] : mode==='noisemaker-nebula' ? ['synth/noise','filter/bloom'] : ['synth/noise','filter/bloom'];
  try{await nmRenderer.loadEffects(needed);}catch(e){try{nmRenderer.stop?.()}catch{};throw new Error(`Этап 4/5: не удалось загрузить эффекты (${needed.join(', ')}): ${describeError(e)}`)}
  try{nmRenderer.stop?.();nmMode='';await nmRenderer.compile(dsl);}catch(e){try{nmRenderer.stop?.()}catch{};nmMode='';throw new Error(`Этап 5/5: ошибка компиляции шейдера «${mode}»: ${describeError(e)}`)}
  nmRenderer.start();nmMode=mode;
  const hint=$('vizHint');if(hint)hint.textContent=`Noisemaker: WebGL 2 · ${mode.replace('noisemaker-','')} готов`;
 }
}
function sampleBins(source,count,convert){const out=new Float32Array(count);for(let i=0;i<count;i++){const at=Math.min(source.length-1,Math.floor(i*source.length/count));out[i]=convert(source[at]||0)}return out}
function updateAudio({data,active,analyser,waveData}){
 if(!nmAudioState)return;
 try{
  if(active&&analyser&&typeof nmAudioState.updateFromAnalyser==='function')nmAudioState.updateFromAnalyser(analyser);
  else {nmAudioState.low=0;nmAudioState.mid=0;nmAudioState.high=0;nmAudioState.vol=0;}
  // Sensitivity is applied only to the visualizer's analysis state, never to playback audio.
  const visualGain=selected==='noisemaker-flow'?2.21:selected==='noisemaker-tunnel'?0.5:1;
  if(active&&visualGain!==1){for(const key of ['low','mid','high','vol'])if(Number.isFinite(nmAudioState[key]))nmAudioState[key]=Math.min(1,nmAudioState[key]*visualGain);}
  if(data&&typeof nmAudioState.setSpectrum==='function')nmAudioState.setSpectrum(sampleBins(data,128,v=>Math.min(1,(v/255)*visualGain)));
  if(waveData&&typeof nmAudioState.setWaveform==='function')nmAudioState.setWaveform(sampleBins(waveData,128,v=>(v-128)/128));
 }catch(e){setError(`Noisemaker: не удалось передать аудиоданные: ${e?.message||e}`)}
}
function drawNoisemakerVisual(mode, payload){
 const canvas=$('customViz'); if(!canvas)return;
 const {data,active,width,height}=payload; const dpr=Math.min(devicePixelRatio||1,1.6);
 const W=Math.max(1,Math.round(width*dpr)),H=Math.max(1,Math.round(height*dpr));
 if(canvas.width!==W||canvas.height!==H){canvas.width=W;canvas.height=H;}
 const g=canvas.getContext('2d'); if(!g)return; g.setTransform(dpr,0,0,dpr,0,0);
 const w=width,h=height,cx=w/2,cy=h/2,t=performance.now()/1000;
 const avg=(a,b)=>{if(!active||!data)return .035;let sum=0,n=0;for(let i=a;i<Math.min(b,data.length);i++){sum+=data[i]/255;n++;}return n?sum/n:.035};
 const low=avg(0,9),mid=avg(9,65),high=avg(65,220),bass=Math.min(1,low*2.8+mid*.45);
 const bg=g.createLinearGradient(0,0,w,h);
 if(mode==='noisemaker-flow'){bg.addColorStop(0,'#031e2b');bg.addColorStop(.5,'#10204b');bg.addColorStop(1,'#250d3c');}
 else if(mode==='noisemaker-tunnel'){bg.addColorStop(0,'#02040c');bg.addColorStop(1,'#10102c');}
 else {bg.addColorStop(0,'#080a25');bg.addColorStop(.55,'#17113c');bg.addColorStop(1,'#040812');}
 g.fillStyle=bg;g.fillRect(0,0,w,h);
 const glow=(color,blur)=>{g.shadowColor=color;g.shadowBlur=blur};
 if(mode==='noisemaker-flow'){
   // Fluid contour field: independently moving, audio-deformed ribbons.
   for(let layer=10;layer>=0;layer--){const q=layer/10,base=h*(.18+q*.72),phase=t*(.55+q*.32)+layer*1.7;
    g.beginPath();g.moveTo(0,h);
    for(let x=0;x<=w;x+=3){const u=x/w,bin=active&&data?data[Math.min(data.length-1,Math.floor(u*(data.length-1)))]/255:.04;
      const y=base+Math.sin(u*7.5+phase)*h*(.045+bin*.10)+Math.sin(u*17-phase*1.3)*h*(.016+high*.035)+Math.sin(u*3.2-t*.8)*h*(.025+bass*.04);g.lineTo(x,y);}
    g.lineTo(w,h);g.closePath();const col=layer%3===0?'#40f5e8':layer%3===1?'#8a69ff':'#ed60df';g.fillStyle=col+(['20','30','18'][layer%3]);g.fill();g.beginPath();
    for(let x=0;x<=w;x+=3){const u=x/w,bin=active&&data?data[Math.min(data.length-1,Math.floor(u*(data.length-1)))]/255:.04;const y=base+Math.sin(u*7.5+phase)*h*(.045+bin*.10)+Math.sin(u*17-phase*1.3)*h*(.016+high*.035)+Math.sin(u*3.2-t*.8)*h*(.025+bass*.04);if(!x)g.moveTo(x,y);else g.lineTo(x,y);}
    g.strokeStyle=col;g.lineWidth=1.1+mid*2;glow(col,7+bass*18);g.stroke();
   }g.shadowBlur=0;
 }else if(mode==='noisemaker-tunnel'){
   // Perspective tunnel with beat-driven depth, rotation and radial spokes.
   const pulse=active?bass:0.06+Math.sin(t*2.2)*.025;
   for(let i=30;i>=1;i--){const q=i/30,depth=(q*q),rx=w*(.025+depth*.49)*(1+pulse*.15),ry=h*(.025+depth*.43)*(1+pulse*.16),rot=t*(.12+q*.24)+Math.sin(t*2+i)*.025;
    g.beginPath();for(let j=0;j<=8;j++){const a=j*Math.PI/4+rot,x=cx+Math.cos(a)*rx,y=cy+Math.sin(a)*ry;if(!j)g.moveTo(x,y);else g.lineTo(x,y);}g.closePath();const col=i%3===0?'#d5ff68':i%3===1?'#40e9ff':'#b56bff';g.strokeStyle=col;g.lineWidth=.7+q*1.7+pulse*2;glow(col,4+q*13+pulse*18);g.stroke();}
   g.shadowBlur=0;
   for(let i=0;i<72;i++){const a=i/72*Math.PI*2+t*.04,amp=active&&data?data[Math.min(data.length-1,Math.floor(i/72*(data.length-1)))]/255:.04;const r1=Math.min(w,h)*(.06+pulse*.04),r2=Math.min(w,h)*(.11+amp*.2+pulse*.14);g.beginPath();g.moveTo(cx+Math.cos(a)*r1,cy+Math.sin(a)*r1);g.lineTo(cx+Math.cos(a)*r2,cy+Math.sin(a)*r2);g.strokeStyle=i%2?'#5ceaff88':'#d5ff68aa';g.lineWidth=1;g.stroke();}
   const halo=g.createRadialGradient(cx,cy,2,cx,cy,Math.min(w,h)*(.12+pulse*.25));halo.addColorStop(0,`rgba(130,100,255,${.08+pulse*.22})`);halo.addColorStop(1,'#00000000');g.fillStyle=halo;g.fillRect(0,0,w,h);
 }else{
   // Nebula: layered particle arms plus audio-reactive cloud and central energy rings.
   const radius=Math.min(w,h)*(.1+bass*.045);
   for(let arm=0;arm<5;arm++){g.beginPath();for(let i=0;i<360;i++){const q=i/359,a=q*8.5+arm*Math.PI*2/5+t*(.13+arm*.018),r=radius+q*Math.min(w,h)*(.34+mid*.12)+Math.sin(q*26-t*1.7+arm)*h*(.012+high*.025);const x=cx+Math.cos(a)*r,y=cy+Math.sin(a)*r*.68;if(!i)g.moveTo(x,y);else g.lineTo(x,y);}const col=['#6c7dff','#e26cff','#48e6ff','#8a5cff','#ff8fda'][arm];g.strokeStyle=col;g.lineWidth=1.3+bass*2;glow(col,12+bass*18);g.stroke();}
   g.shadowBlur=0;
   for(let i=0;i<520;i++){const q=(i*.61803398875)%1,a=i*2.399+t*(.05+(i%11)*.004),r=Math.min(w,h)*(.04+q*.47)*(1+bass*.07),x=cx+Math.cos(a)*r,y=cy+Math.sin(a)*r*.7,size=.5+((i%5===0)?1.7:0)+high*2;g.globalAlpha=.25+(i%7)*.09;g.fillStyle=i%3===0?'#e8b7ff':i%3===1?'#8cf8ff':'#b3a5ff';g.beginPath();g.arc(x,y,size,0,Math.PI*2);g.fill();}g.globalAlpha=1;
   for(let r=0;r<4;r++){g.beginPath();g.ellipse(cx,cy,radius*(1.1+r*.55),radius*(.72+r*.38),t*.08+r*.3,0,Math.PI*2);g.strokeStyle=`rgba(130,150,255,${.45-r*.08})`;g.lineWidth=1+bass*2;g.stroke();}
 }
}
async function update(payload){
 if(!selected||selected==='vinyl'||(!selected.startsWith('noisemaker-')&&!selected.startsWith('butterchurn-')))return;
 const {ctx,source,analyser,width,height}=payload;
 if(loading)return;loading=true;
 try{
  if(selected.startsWith('noisemaker-')){
   if(!width||!height)throw new Error(`Этап 1/5: размер сцены ${width}×${height}; проверь CSS контейнера визуализатора.`);
   if(!nmRenderer||!nmAudioState||nmMode!==selected) await startNoisemaker(selected,width,height);
   updateAudio(payload);
   return;
  }
  if(!ctx||!analyser)return;
  await startButterchurn({ctx,source,analyser,width,height,presetId:selected});
 }catch(e){if(selected.startsWith('butterchurn-'))bcLoadError=e;const prefix=selected.startsWith('butterchurn-')?`Butterchurn [${bcStage}]`:'Noisemaker';setError(`${prefix}: ${e?.message||e}. Полная ошибка — F12 → Console (красная запись «Полная диагностика»).`,e)}
 finally{loading=false;}
}
function stopIfInactive(mode){if(!String(mode).startsWith('butterchurn-')&& !String(mode).startsWith('noisemaker-')){try{nmRenderer?.stop?.()}catch{};nmMode='';}}
function select(mode){selected=mode;lastError='';if(mode.startsWith('butterchurn-'))bcLoadError=null;if(mode.startsWith('butterchurn-')||mode.startsWith('noisemaker-')){const hint=$('vizHint');if(hint&&!lastError)hint.textContent=mode.startsWith('butterchurn-')?'Загружаю MilkDrop-сцену Butterchurn…':'Загружаю GPU-шейдер Noisemaker…';}else{try{nmRenderer?.stop?.()}catch{};nmMode='';}}
window.PulseEngines={select,update,stopIfInactive,getStatus:()=>({butterchurn:!!bcInstance,butterchurnStage:bcStage,butterchurnError:bcLoadError?{name:bcLoadError.name,message:bcLoadError.message,stack:bcLoadError.stack}:null,noisemaker:!!nmRenderer,noisemakerMode:nmMode,noisemakerBackend:nmBackend,lastError,canvasSize:{butterchurn:bcCanvas?`${bcCanvas.width}x${bcCanvas.height}`:null,noisemaker:nmCanvas?`${nmCanvas.width}x${nmCanvas.height}`:null},webgl2:checkWebGL2()})};
