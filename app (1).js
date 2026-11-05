const bridge = window.AstrBotPluginPage;
const main = document.getElementById('main');
const providerSelect = document.getElementById('providerSelect');
const statusEl = document.getElementById('status');
let currentLib = 'magic_trial_characters';
let state = null;
let cache = {};
let uploadInProgress = false;

const ALLOWED_TYPES = new Set(['image/jpeg','image/png','image/webp','image/gif','image/bmp']);
const ALLOWED_EXT = /\.(jpe?g|png|webp|gif|bmp)$/i;
const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;

function setStatus(t){ statusEl.textContent=t; }
function esc(s){ return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function errorText(err){ return String(err?.message || err || '未知错误').replace(/^Error:\s*/,''); }
async function get(path, params={}){ return bridge.apiGet(path, params); }
async function post(path, body){ return bridge.apiPost(path, body); }
async function upload(path, file){ return bridge.upload(path, file); }

function notify(message, type='info', ms=4200){
  let host=document.getElementById('toastHost');
  if(!host){ host=document.createElement('div'); host.id='toastHost'; host.className='toast-host'; document.body.appendChild(host); }
  const el=document.createElement('div'); el.className=`toast ${type}`; el.textContent=message; host.appendChild(el);
  setTimeout(()=>el.remove(),ms);
}

function confirmInPage(message, title='请确认'){
  return new Promise(resolve=>{
    const wrap=document.createElement('div'); wrap.className='modal';
    wrap.innerHTML=`<div class="modal-card confirm-card"><div class="modal-head"><div><div class="eyebrow">VISION MEMORY</div><h2>${esc(title)}</h2></div><button class="ghost" id="cancelTop">关闭</button></div><p class="confirm-message">${esc(message)}</p><div class="confirm-actions"><button class="ghost" id="cancel">取消</button><button class="danger" id="ok">确定</button></div></div>`;
    document.body.appendChild(wrap);
    const done=value=>{wrap.remove();resolve(value)};
    wrap.querySelector('#cancel').onclick=()=>done(false); wrap.querySelector('#cancelTop').onclick=()=>done(false); wrap.querySelector('#ok').onclick=()=>done(true);
  });
}

function validateImage(file){
  if(!file) throw new Error('请先选择图片。');
  if(file.size > MAX_UPLOAD_BYTES) throw new Error('图片不能超过 20 MB。');
  const typeOk = !file.type || ALLOWED_TYPES.has(file.type.toLowerCase());
  const extOk = ALLOWED_EXT.test(file.name || '');
  if(!typeOk && !extOk) throw new Error('只支持 JPG、JPEG、PNG、WebP、GIF、BMP 图片。');
}

function setUploadUiBusy(busy){
  document.body.classList.toggle('upload-busy',busy);
  main.setAttribute('aria-busy',String(busy));
  document.querySelectorAll('input[type="file"], #uploadOne, #refreshBtn, #tabs button').forEach(el=>{el.disabled=busy;});
  const uploadButton=document.getElementById('uploadOne');
  if(uploadButton) uploadButton.textContent=busy?'正在上传，请稍候…':'上传图片（可多选）';
}

async function uploadEntryImages(lib, entryId, files){
  if(uploadInProgress){notify('正在上传图片，请勿重复操作。','warning');return {skipped:true,success:0,failed:0};}
  const selected=Array.from(files||[]).filter(Boolean);
  if(!selected.length){notify('请先选择一张或多张图片。','warning');return {success:0,failed:0};}
  try{
    selected.forEach(file=>validateImage(file));
  }catch(err){
    notify(`本轮上传未开始：${errorText(err)}`,'error',7000);return {success:0,failed:0,validationError:errorText(err)};
  }

  uploadInProgress=true; setUploadUiBusy(true);
  const succeeded=[]; const failed=[]; const total=selected.length;
  if(total>1)notify(`已选择 ${total} 张图片，将按顺序上传。`,'info');
  try{
    for(let i=0;i<total;i++){
      const file=selected[i];
      setStatus(`正在上传第 ${i+1} / ${total} 张：${file.name}（已完成 ${i} 张）`);
      try{
        const endpoint=`upload/${encodeURIComponent(lib)}/${encodeURIComponent(entryId)}`;
        await upload(endpoint,file); succeeded.push(file.name);
        setStatus(`已完成 ${i+1} / ${total} 张图片上传`);
      }catch(err){
        const reason=errorText(err); failed.push({name:file.name,reason});
        console.error('[VisionMemory] image upload failed',err);
        setStatus(`第 ${i+1} / ${total} 张上传失败，${i+1<total?'继续上传下一张':'正在汇总结果'}…`);
      }
    }
    invalidate(lib);
    setStatus('图片处理结束，正在刷新图片库…');
    try{await refresh();}catch(err){notify(`图片已处理，但列表刷新失败：${errorText(err)}`,'warning',7000);}

    const summary=`上传完成：成功 ${succeeded.length} 张，失败 ${failed.length} 张。`;
    if(!failed.length)notify(`${summary}识别关键特征已自动处理。`,'success',6000);
    else{
      const details=failed.slice(0,3).map(item=>`${item.name}（${item.reason}）`).join('；');
      notify(`${summary}${details}${failed.length>3?`；另有 ${failed.length-3} 张失败`:''}`,'error',10000);
    }
    return {success:succeeded.length,failed:failed.length,results:{succeeded,failed}};
  }finally{
    uploadInProgress=false; setUploadUiBusy(false); setStatus('就绪');
  }
}

async function deleteImage(lib, entryId, imageId, filename){
  if(!await confirmInPage(`将只删除这张图片「${filename||'图片'}」，不会删除人物/事物栏目。`,`删除单张图片`)) return;
  try{
    await post(`images/delete/${encodeURIComponent(lib)}/${encodeURIComponent(entryId)}/${encodeURIComponent(imageId)}`,{});
    invalidate(lib); await refresh(); notify('这张图片已删除，栏目仍然保留。','success');
  }catch(err){ notify(`删除图片失败：${errorText(err)}`,'error',7000); }
}

async function loadImagePreview(img, lib, entryId, imageId){
  try{
    const data=await get(`image-data/${encodeURIComponent(lib)}/${encodeURIComponent(entryId)}/${encodeURIComponent(imageId)}`);
    if(data?.data_url){ img.src=data.data_url; img.classList.add('loaded'); }
    else throw new Error('未返回图片数据');
  }catch(err){ img.alt='预览失败'; img.classList.add('failed'); console.warn('[VisionMemory] image preview failed',err); }
}

async function boot(){
  try{
    await bridge.ready(); await refresh();
    document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>{currentLib=b.dataset.lib;document.querySelectorAll('#tabs button').forEach(x=>x.classList.toggle('active',x===b));render();});
    document.getElementById('refreshBtn').onclick=refresh;
    document.getElementById('saveProvider').onclick=async()=>{try{await post('settings',{vision_provider_id:providerSelect.value});notify('视觉模型已保存。','success');await refresh();}catch(err){notify(`保存模型失败：${errorText(err)}`,'error');}};
  }catch(err){ console.error('[VisionMemory] page boot failed',err); setStatus('加载失败'); notify(`页面加载失败：${errorText(err)}`,'error',8000); }
}

async function refresh(){
  setStatus('加载中…');
  state=await get('state'); const providers=await get('providers');
  providerSelect.innerHTML=(providers.providers||[]).map(p=>`<option value="${esc(p.id)}">${esc(p.name||p.id)} · ${esc(p.model||'')}</option>`).join('');
  if(state.settings?.vision_provider_id) providerSelect.value=state.settings.vision_provider_id;
  document.getElementById('countCharacters').textContent=state.libraries.magic_trial_characters.count;
  document.getElementById('countCustom').textContent=state.libraries.custom_objects.count;
  document.getElementById('countMemory').textContent=state.libraries.bot_memory.count;
  const pending=document.getElementById('pendingCount'); if(pending) pending.textContent=state.pending_count||0;
  const badge=document.getElementById('pendingTabBadge'); if(badge) badge.textContent=state.pending_count||0;
  await render(); setStatus('就绪');
}
async function loadLib(lib){ if(!cache[lib]) cache[lib]=await get(`libraries/${lib}`); return cache[lib]; }
function invalidate(lib){delete cache[lib];}

async function render(){
  if(currentLib==='logs') return renderLogs();
  if(currentLib==='settings') return renderSettings();
  if(currentLib==='pending') return renderPending();
  const data=await loadLib(currentLib);
  const threshold=currentLib==='magic_trial_characters' ? `${Math.round((state.settings?.character_threshold??.42)*100)}%` : `${Math.round((state.settings?.general_threshold??.45)*100)}%`;
  const policy=currentLib==='magic_trial_characters' ? '二创友好 · 脸部/发型优先 · 服装弱惩罚' : '二创友好 · 稳定身份特征优先 · 低像素依赖';
  const ccipState=state.ccip_available ? `CCIP：已安装 · ${esc(state.ccip_model||'默认模型')}` : 'CCIP：未安装，暂用视觉模型';
  main.innerHTML=`<div class="toolbar"><div><div class="eyebrow">LIBRARY / ${esc(currentLib)}</div><h2>${esc(data.name)}</h2><div class="muted">匹配阈值 ${threshold} · ${policy} · 多图参考 · 每张参考图独立特征 · ${ccipState}</div></div><div class="actions"><button id="newEntry">＋ 新建栏目</button><button class="ghost" id="rebuildAll">重建全部参考图特征</button><button class="ghost" id="purge">执行遗忘清理</button></div></div><div class="cards">${(data.entries||[]).map(entryCard).join('')}</div>`;
  document.getElementById('newEntry').onclick=()=>openEditor(null);
  if(document.getElementById('rebuildAll')) document.getElementById('rebuildAll').onclick=startRebuild;
  document.getElementById('purge').onclick=async()=>{try{const r=await post('purge',{});invalidate('bot_memory');await refresh();notify(`遗忘清理完成：删除 ${r.removed||0} 个栏目。`,'success');}catch(err){notify(`清理失败：${errorText(err)}`,'error')}};
  main.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>openEditor(b.dataset.edit));
  main.querySelectorAll('[data-delete-entry]').forEach(b=>b.onclick=async()=>{
    if(await confirmInPage('确定删除整个栏目吗？如果只是想删除其中一张图片，请点击图片右上角的“×”。','删除整个栏目')){
      try{await fetchDelete(currentLib,b.dataset.deleteEntry);invalidate(currentLib);await refresh();notify('栏目已删除。','success');}catch(err){notify(`删除失败：${errorText(err)}`,'error');}
    }
  });
  main.querySelectorAll('[data-upload]').forEach(input=>input.onchange=async()=>{const files=Array.from(input.files||[]);input.value='';if(!files.length)return;await uploadEntryImages(currentLib,input.dataset.upload,files);});
  main.querySelectorAll('[data-delete-image]').forEach(b=>b.onclick=()=>deleteImage(currentLib,b.dataset.entry,b.dataset.deleteImage,b.dataset.filename));
  main.querySelectorAll('img[data-preview]').forEach(img=>loadImagePreview(img,img.dataset.lib,img.dataset.entry,img.dataset.preview));
}

function entryCard(e){
  const images=e.images||[];
  const thumbs=images.map((img,i)=>{
    const hasVision=!!(img.features||''); const hasCcip=!!(img.ccip_feature&&img.ccip_feature.length);
    return `<div class="thumb-wrap"><div class="thumb"><img data-preview="${esc(img.id)}" data-lib="${esc(currentLib)}" data-entry="${esc(e.id)}" alt="正在加载…"><span class="image-index">${i+1}</span><button class="image-delete" title="删除这张图片" data-delete-image="${esc(img.id)}" data-entry="${esc(e.id)}" data-filename="${esc(img.filename||'图片')}">×</button></div><div class="image-name">${esc(img.filename||'图片')}</div><div class="feature-badges"><span class="badge ${hasVision?'ok':''}">视觉${hasVision?'✓':'×'}</span><span class="badge ${hasCcip?'ok':''}">CCIP${hasCcip?'✓':'×'}</span></div></div>`;
  }).join('');
  const detail=(images.length?`<details class="ref-details"><summary>查看逐张参考图特征 / CCIP状态</summary>${images.map((img,i)=>`<div class="ref-row"><div><b>参考图 #${i+1}</b> · ${esc(img.filename||'图片')}</div><div class="ref-status">视觉特征：${img.features?'已生成':'缺失'} · CCIP：${img.ccip_feature?.length?'已缓存':'未缓存'}</div><div class="ref-feature">${esc(img.features||'尚未生成这张图片的独立视觉特征')}</div>${img.ccip_feature?.length?`<div class="muted">CCIP模型：${esc(img.ccip_model||'')} · 尺寸：${esc(img.ccip_size||'')}</div>`:''}</div>`).join('')}</details>`:'');
  return `<article class="entry"><div class="entry-head"><div><h3>${esc(e.name)}</h3><div class="entry-meta">${e.builtin?'预置角色槽位 · ':''}${images.length} 张参考图</div></div>${e.builtin?'<span class="builtin">魔裁</span>':''}</div><div class="images">${thumbs||'<div class="empty-images">暂无图片<br><span>点击下方“上传图片（可多选）”添加参考图</span></div>'}</div>${detail}<div class="features-title">人物/栏目综合特征（兼容摘要）</div><div class="features">${esc(e.features||'尚未生成综合特征；建议点击“重建全部参考图特征”。')}</div><div class="entry-footer"><span class="muted">最近识别 ${e.last_seen_at?new Date(e.last_seen_at*1000).toLocaleString():'—'}</span><div class="entry-actions"><button class="small" data-edit="${esc(e.id)}">编辑</button><label class="small ghost upload-label"><input type="file" multiple accept="image/jpeg,image/png,image/webp,image/gif,image/bmp" data-upload="${esc(e.id)}">上传图片（可多选）</label><button class="small danger" data-delete-entry="${esc(e.id)}">删除栏目</button></div></div></article>`;
}

async function startRebuild(){
  if(!await confirmInPage('这会重新读取三个图片库中的每张参考图，生成视觉特征，并在 CCIP 可用时生成或缓存对应特征。参考图片本身不会被删除。首次运行 CCIP 可能需要下载模型。','重建全部参考图特征')) return;
  try{
    const r=await post('rebuild-features',{}); if(!r?.started){notify('已有重建任务正在运行。','warning');return;}
    const jobId=r.job.id; notify('重建任务已开始，请等待完成。','info',5000); await pollRebuild(jobId);
  }catch(err){notify(`启动重建失败：${errorText(err)}`,'error',7000);}
}
async function pollRebuild(jobId){
  for(let i=0;i<600;i++){
    await new Promise(r=>setTimeout(r,1200));
    try{
      const d=await get(`rebuild-features/status/${encodeURIComponent(jobId)}`); const j=d.job||{};
      setStatus(j.status==='running'?`重建参考图特征：${j.done||0}/${j.total||'?'}…`:'就绪');
      if(j.status==='done'){Object.keys(cache).forEach(k=>delete cache[k]);await refresh();notify('全部参考图特征重建完成。','success',6000);return;}
      if(j.status==='error'){notify(`重建失败：${j.message||'未知错误'}`,'error',8000);return;}
    }catch(err){console.warn('[VisionMemory] rebuild poll failed',err);}
  }
  notify('重建任务运行时间较长，请稍后刷新页面查看。','warning',6000);
}

async function openEditor(id){
  const data=await loadLib(currentLib); const e=(data.entries||[]).find(x=>x.id===id);
  const images=(e?.images||[]).map((img,i)=>`<div class="editor-image"><img data-preview="${esc(img.id)}" data-lib="${esc(currentLib)}" data-entry="${esc(e.id)}" alt="加载中…"><div><strong>图片 ${i+1}</strong><div class="muted">${esc(img.filename||'图片')}</div></div><button class="small danger" data-editor-delete-image="${esc(img.id)}" data-entry="${esc(e.id)}" data-filename="${esc(img.filename||'图片')}">删除此图</button></div>`).join('');
  const wrap=document.createElement('div');wrap.className='modal';
  wrap.innerHTML=`<div class="modal-card editor-card"><div class="modal-head"><div><div class="eyebrow">${e?'EDIT ENTRY':'NEW ENTRY'}</div><h2>${e?'编辑栏目':'新建栏目'}</h2></div><button class="ghost" id="close">关闭</button></div><div class="form"><label>图片事物名称<input id="name" value="${esc(e?.name||'')}" placeholder="例如：夏目安安 / 某个物品" /></label><label>识别关键特征<textarea id="features" placeholder="上传图片后会自动生成，也可以手动修正。">${esc(e?.features||'')}</textarea></label><div class="notice">${currentLib==='magic_trial_characters'?'预设库（魔裁）采用“身份特征优先”：二创、换装、不同画师、姿势和背景变化不会被当作强否定证据。':'人工视觉添加库与 Bot 图片库按综合视觉结构进行匹配。'} </div>${e?`<section class="editor-gallery"><div class="section-title">参考图片 <span>${images?e.images?.length||0:0}</span></div><div class="editor-images">${images||'<div class="muted">暂无参考图</div>'}</div><div class="upload-box"><input id="newImg" type="file" multiple accept="image/jpeg,image/png,image/webp,image/gif,image/bmp"><button id="uploadOne">上传图片（可多选）</button></div></section>`:''}<button id="save">保存栏目</button></div></div>`;
  document.body.appendChild(wrap); wrap.querySelector('#close').onclick=()=>wrap.remove();
  wrap.querySelectorAll('img[data-preview]').forEach(img=>loadImagePreview(img,img.dataset.lib,img.dataset.entry,img.dataset.preview));
  wrap.querySelectorAll('[data-editor-delete-image]').forEach(b=>b.onclick=async()=>{await deleteImage(currentLib,b.dataset.entry,b.dataset.editorDeleteImage,b.dataset.filename);wrap.remove();});
  wrap.querySelector('#save').onclick=async()=>{const body={name:wrap.querySelector('#name').value,features:wrap.querySelector('#features').value};try{if(e)await post(`entries/${currentLib}/${e.id}`,body);else await post('entries',{library:currentLib,...body});invalidate(currentLib);wrap.remove();await refresh();notify('栏目已保存。','success');}catch(err){notify(`保存失败：${errorText(err)}`,'error');}};
  if(e)wrap.querySelector('#uploadOne').onclick=async()=>{const files=Array.from(wrap.querySelector('#newImg').files||[]);if(!files.length){notify('请先选择一张或多张图片。','warning');return;}const result=await uploadEntryImages(currentLib,e.id,files);if(result.success>0)wrap.remove();};
}

async function renderPending(){
  const d=await get('pending');
  const items=d.pending||[];
  main.innerHTML=`<div class="toolbar"><div><div class="eyebrow">CONTEXT / PENDING</div><h2>待管理员命名</h2><div class="muted">未知图片默认保留 7 天；每个会话最多 20 张，超限清理最旧图片。插件不会从聊天上下文、用户后续发言或 Bot 自己的回复中学习名称；只有管理员在本页面手动输入名称，图片才会进入 Bot 图片库。</div></div><div class="actions"><button class="ghost" id="refreshPending">刷新待确认</button></div></div><div class="pending-grid">${items.map(pendingCard).join('')||'<div class="panel empty-pending">目前没有等待确认的图片。</div>'}</div>`;
  document.getElementById('refreshPending').onclick=refresh;
  main.querySelectorAll('[data-promote]').forEach(b=>b.onclick=async()=>{const name=await askNameInPage();if(!name)return;try{await post(`pending/promote/${encodeURIComponent(b.dataset.promote)}`,{name});await refresh();notify(`已将图片按「${name}」加入 Bot 图片库。`,'success');}catch(err){notify(`入库失败：${errorText(err)}`,'error');}});
  main.querySelectorAll('[data-pending-delete]').forEach(b=>b.onclick=async()=>{if(!await confirmInPage('删除后这张未知图片不会进入 Bot 图片库。','删除待确认图片'))return;try{await post(`pending/delete/${encodeURIComponent(b.dataset.pendingDelete)}`,{});await refresh();notify('待确认图片已删除。','success');}catch(err){notify(`删除失败：${errorText(err)}`,'error');}});
  main.querySelectorAll('img[data-pending-preview]').forEach(img=>loadPendingPreview(img,img.dataset.id));
}
function askNameInPage(){
  return new Promise(resolve=>{
    const wrap=document.createElement('div');wrap.className='modal';
    wrap.innerHTML=`<div class="modal-card confirm-card"><div class="modal-head"><div><div class="eyebrow">ADMIN MEMORY</div><h2>管理员手动命名</h2></div><button class="ghost" id="cancelTop">关闭</button></div><p class="confirm-message">输入这张图片最终确认的人物/事物名称。名称只由管理员手动指定；插件不会从聊天上下文自行猜名。</p><input id="pendingName" class="confirm-input" placeholder="例如：夏目安安 / 小明 / 那只猫"><div class="confirm-actions"><button class="ghost" id="cancel">取消</button><button id="ok">确认入库</button></div></div>`;
    document.body.appendChild(wrap);const input=wrap.querySelector('#pendingName');input.focus();
    const done=v=>{wrap.remove();resolve(v)};wrap.querySelector('#cancel').onclick=()=>done('');wrap.querySelector('#cancelTop').onclick=()=>done('');wrap.querySelector('#ok').onclick=()=>done((input.value||'').trim().slice(0,80));input.onkeydown=e=>{if(e.key==='Enter')done((input.value||'').trim().slice(0,80));if(e.key==='Escape')done('')};
  });
}

async function loadPendingPreview(img,id){
  try{const d=await get(`pending/image-data/${encodeURIComponent(id)}`);if(d?.data_url){img.src=d.data_url;img.classList.add('loaded');}else throw new Error('未返回图片数据');}catch(e){img.alt='预览失败';img.classList.add('failed');}
}
function pendingCard(p){
  const best=p.result?.best_match; const age=new Date((p.created_at||0)*1000).toLocaleString();
  return `<article class="pending-card panel"><div class="pending-image"><img data-pending-preview="1" data-id="${esc(p.id)}" alt="加载中…"></div><div class="pending-body"><div class="eyebrow">待确认 · ${esc(age)}</div><h3>${esc(p.image_filename||'未知图片')}</h3><div class="pending-source">触发文本：${esc(p.source_name||'（图片消息）')}</div><div class="pending-score">${best?`当前最高候选：<b>${esc(best.name)}</b> · ${Math.round((best.score||0)*100)}%`:'当前没有达到库内阈值'}</div><div class="pending-actions"><button data-promote="${esc(p.id)}">管理员命名并入库</button><button class="small danger" data-pending-delete="${esc(p.id)}">删除</button></div></div></article>`;
}

async function fetchDelete(lib,id){ await post(`entries/${lib}/${id}`,{_delete:true}); }
async function renderLogs(){const d=await get('logs');main.innerHTML=`<div class="toolbar"><div><div class="eyebrow">AUDIT / RECOGNITION</div><h2>识别日志</h2><div class="muted">记录 Provider、候选、Vision 核验、逐张 CCIP 距离、最终判定与 Bot 自身判定。</div></div></div><div class="panel log-panel">${(d.logs||[]).map(x=>`<div class="log"><div class="log-top"><strong>${esc(x.image)}</strong><span>${new Date(x.timestamp*1000).toLocaleString()}</span></div><div>${x.best_match?`最佳：<span class="score">${esc(x.best_match.name)} ${Math.round((x.best_match.score||0)*100)}%</span>`:'未达到阈值'} ${x.is_self?'<b> · Bot自身</b>':''}</div><div class="muted">${(x.matches||[]).slice(0,6).map(m=>{const c=m.ccip_distances?.length?` · CCIP：${m.ccip_distances.map(r=>`#${r.reference_index} ${Number(r.distance).toFixed(4)}`).join(', ')}`:'';return `${esc(m.name)} ${Math.round((m.score||0)*100)}%${c}`}).join('<br>')}</div></div>`).join('')||'<div class="log muted">暂无识别记录</div>'}</div>`;}
async function renderSettings(){const s=state.settings||{};main.innerHTML=`<div class="toolbar"><div><div class="eyebrow">CONTROL / SETTINGS</div><h2>运行设置</h2><div class="muted">v1.9 起 Bot 图片库采用“管理员手动命名”模式：未知图片只保留原图，不读取聊天上下文给图片起名；待命名图片默认保留 7 天、每个会话最多 20 张。</div></div></div><div class="settings-grid"><div class="panel setting-card"><div class="section-title">识别策略</div><label>预设库（魔裁）阈值<input id="charT" type="number" min="0.20" max="1" step="0.01" value="${s.character_threshold??.42}"><small>推荐 0.35～0.50；二创图建议 0.42。</small></label><label>人工视觉添加库与 Bot 图片库阈值<input id="genT" type="number" min="0.30" max="1" step="0.01" value="${s.general_threshold??.45}"></label><button id="saveSettings">保存运行设置</button></div><div class="panel setting-card"><div class="section-title">Bot 图片库策略</div><div class="policy"><div><b>未知图片</b><span>原图进入“待管理员命名”，默认保留 7 天；每个会话最多 20 张，超限清理最旧图片</span></div><div><b>管理员</b><span>在 WebUI 查看图片后手动输入最终名称，再加入 Bot 图片库</span></div><div><b>聊天上下文</b><span>完全不参与 Bot 图片库命名，不会因“这个是XX”而自动创建栏目</span></div><div><b>Bot 回复</b><span>完全不作为图片身份来源</span></div></div></div></div>`;document.getElementById('saveSettings').onclick=async()=>{try{await post('settings',{character_threshold:+document.getElementById('charT').value,general_threshold:+document.getElementById('genT').value,auto_memory_enabled:false,pending_memory_retention_hours:168,context_learning_max_pending:20});await refresh();notify('运行设置已保存；管理员手动命名模式保持开启。','success');}catch(err){notify(`保存设置失败：${errorText(err)}`,'error');}};}
boot();
