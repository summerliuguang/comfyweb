/* 批量生成页:AI 细化任务清单、清单编辑、模板 CRUD、进度轮询 */
(function () {
  const $ = id => document.getElementById(id);
  const PIPE_LABEL = { anima: 'anima(人物/图标)', zimage: 'z-image(背景)' };

  let tasks = [];          // 待生成清单(可编辑)
  let pollTimer = null;
  let wasRunning = false;

  async function toJson(r) {
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.status);
    return data;
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  /* ---------- 提示词模板 ---------- */

  async function loadTemplates(prefetched) {
    let d = prefetched;
    if (!d) {
      try { d = await toJson(await fetch('/api/batch/templates')); } catch (e) { return; }
    }
    const sel = $('tplSelect');
    window._batchTplCache = d.templates;
    sel.innerHTML = '<option value="">— 选择已保存模板 —</option>' +
      d.templates.map(t => `<option value="${esc(t.name)}">${esc(t.name)}${t.nsfw ? ' 🔒' : ''}</option>`).join('');
    const nb = $('btnTplNsfw');
    if (nb) nb.textContent = '设私密';
  }

  $('btnTplNsfw').addEventListener('click', async () => {
    const name = $('tplSelect').value;
    if (!name) { toast('先选择模板'); return; }
    const t = (window._batchTplCache || []).find(x => x.name === name);
    if (!t) return;
    try {
      const d = await toJson(await fetch('/api/batch/templates/nsfw', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, nsfw: !t.nsfw }) }));
      t.nsfw = d.nsfw;
      loadTemplates();
    } catch (e) { toast(e.message, 'bad'); }
  });

  $('tplSelect').addEventListener('change', () => {
    const name = $('tplSelect').value;
    if (!name) return;
    // 从当前下拉项取内容需要再查一次;直接用全局最近一次列表缓存
    const t = (window._batchTplCache || []).find(x => x.name === name);
    if (t) { $('tplPos').value = t.positive || ''; $('tplNeg').value = t.negative || ''; }
  });

  $('btnTplSave').addEventListener('click', async () => {
    const errBox = $('tplError');
    errBox.hidden = true;
    try {
      const d = await toJson(await fetch('/api/batch/templates', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action: 'save', name: $('tplName').value,
          positive: $('tplPos').value, negative: $('tplNeg').value,
        }),
      }));
      window._batchTplCache = d.templates;
      await loadTemplates();
      $('tplSelect').value = $('tplName').value.trim();
    } catch (e) { errBox.textContent = e.message; errBox.hidden = false; }
  });

  $('btnTplDelete').addEventListener('click', async () => {
    const name = $('tplSelect').value;
    if (!name || !confirm(`删除模板「${name}」?`)) return;
    try {
      const d = await toJson(await fetch('/api/batch/templates', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'delete', name }),
      }));
      window._batchTplCache = d.templates;
      await loadTemplates();
    } catch (e) { toast(e.message, 'bad'); }
  });

  /* ---------- AI 细化 ---------- */

  /* 角色套图每人 7 张(立绘 + 6 场景),8 人封顶才不超单批 60 张上限 */
  const modeCap = () => $('modeSelect').value === 'cast' ? 8 : 60;
  $('modeSelect').addEventListener('change', () => {
    const cap = modeCap();
    $('countInput').max = cap;
    if (parseInt($('countInput').value, 10) > cap) $('countInput').value = cap;
  });

  /* 细化模型:默认免费模型(:free),选择记忆在本机;列表来自网关,拉不到就用后端默认 */
  let batchRefineModel = '';
  (async () => {
    try {
      const d = await toJson(await fetch('/api/ai/models'));
      const models = d.models || [];
      if (!models.length) return;
      const sel = $('refineModel');
      const saved = localStorage.getItem('comfyweb.batchmodel');
      const want = models.includes(saved) ? saved
        : (models.find(m => m.includes(':free')) || d.default || models[0]);
      for (const m of models) {
        const op = document.createElement('option');
        op.value = m; op.textContent = m;
        if (m === want) op.selected = true;
        sel.appendChild(op);
      }
      batchRefineModel = sel.value;
      sel.addEventListener('change', () => {
        batchRefineModel = sel.value;
        localStorage.setItem('comfyweb.batchmodel', sel.value);
      });
    } catch (e) {
      // 网关不可达:留空,后端用默认模型——但要让用户看得见原因
      const sel = $('refineModel');
      if (sel) {
        sel.innerHTML = '<option value="">默认模型(网关不可达)</option>';
        sel.disabled = true;
      }
    }
  })();

  $('btnRefine').addEventListener('click', async () => {
    const btn = $('btnRefine'), errBox = $('refineError');
    if (!$('themeInput').value.trim()) {
      errBox.textContent = '先描述一下主题,再点 AI 细化';
      errBox.hidden = false;
      $('themeInput').focus();
      return;
    }
    errBox.hidden = true;
    btn.classList.add('is-loading');
    try {
      const d = await toJson(await fetch('/api/batch/refine', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mode: $('modeSelect').value, theme: $('themeInput').value,
          count: Math.min(parseInt($('countInput').value, 10) || 8, modeCap()),
          style: $('styleInput').value,
          template: { positive: $('tplPos').value, negative: $('tplNeg').value },
          model: batchRefineModel || undefined,
        }),
      }));
      tasks = d.tasks;
      renderTasks();
      $('tasksPanel').hidden = false;
      $('tasksPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (e) {
      errBox.textContent = e.message; errBox.hidden = false;
    } finally { btn.classList.remove('is-loading'); }
  });

  /* ---------- 任务清单编辑 ---------- */

  function taskCard(t, i) {
    const d = document.createElement('div');
    d.className = 'task-card';
    const row1 = document.createElement('div');
    row1.className = 'task-head';
    const name = document.createElement('input');
    name.className = 'input';
    name.style.cssText = 'flex:1;font-size:1rem;font-weight:600';
    name.value = t.name;
    name.dataset.idx = i; name.dataset.k = 'name';
    const del = document.createElement('button');
    del.className = 'button is-small';
    del.textContent = '×'; del.title = '删除此任务';
    del.addEventListener('click', () => {
      if (!confirm(`删除任务「${tasks[i].name || '未命名'}」?`)) return;
      tasks.splice(i, 1); renderTasks();
    });
    row1.appendChild(name); row1.appendChild(del);
    d.appendChild(row1);

    const row2 = document.createElement('div');
    row2.className = 'seed-row';
    row2.style.cssText = 'margin-top:.45rem;flex-wrap:wrap';
    const mk = (k, val, w, type) => {
      const inp = document.createElement('input');
      inp.className = 'input';
      inp.style.cssText = `width:${w};font-size:1rem;padding:.3rem .45rem`;
      inp.type = type || 'text';
      inp.value = val;
      inp.dataset.idx = i; inp.dataset.k = k;
      return inp;
    };
    const pipe = document.createElement('select');
    pipe.className = 'select';
    pipe.style.cssText = 'font-size:1rem;width:auto;flex:none';
    pipe.innerHTML = Object.keys(PIPE_LABEL).map(k =>
      `<option value="${k}"${k === t.pipeline ? ' selected' : ''}>${PIPE_LABEL[k]}</option>`).join('');
    pipe.dataset.idx = i; pipe.dataset.k = 'pipeline';
    row2.appendChild(pipe);
    row2.appendChild(mk('w', t.w, '4.2rem', 'number'));
    row2.appendChild(mk('h', t.h, '4.2rem', 'number'));
    row2.appendChild(mk('seed', t.seed == null ? '' : t.seed, '7rem', 'text'));
    d.appendChild(row2);

    const prompt = document.createElement('textarea');
    prompt.className = 'textarea';
    prompt.style.cssText = 'font-size:1rem;margin-top:.45rem';
    prompt.rows = 2; prompt.value = t.prompt;
    prompt.dataset.idx = i; prompt.dataset.k = 'prompt';
    d.appendChild(prompt);

    const neg = mk('neg', t.neg || '', '100%', 'text');
    neg.style.marginTop = '.4rem';
    neg.placeholder = '负面提示词';
    d.appendChild(neg);
    return d;
  }

  function renderTasks() {
    const list = $('taskList');
    $('taskCount').textContent = tasks.length;
    if (!tasks.length) {
      list.innerHTML = '<p class="empty">清单为空,先点「AI 细化提示词」。</p>';
      return;
    }
    list.innerHTML = '';
    tasks.forEach((t, i) => list.appendChild(taskCard(t, i)));
    saveDraft();
    list.querySelectorAll('textarea').forEach(t => comfyAutosizeFit(t));
  }

  // 输入统一走事件委托,直接改 tasks 数组,不整卡重渲染
  $('taskList').addEventListener('input', e => {
    const el = e.target;
    if (el.dataset.idx === undefined || !el.dataset.k) return;
    const t = tasks[parseInt(el.dataset.idx, 10)];
    if (!t) return;
    let v = el.value;
    if (el.dataset.k === 'w' || el.dataset.k === 'h') v = parseInt(v, 10) || 832;
    else if (el.dataset.k === 'seed') v = v === '' ? null : (parseInt(v, 10) || 0);
    t[el.dataset.k] = v;
  });
  $('taskList').addEventListener('change', e => {
    const el = e.target;
    if (el.dataset && el.dataset.idx !== undefined && el.dataset.k === 'pipeline') {
      const t = tasks[parseInt(el.dataset.idx, 10)];
      if (t) t.pipeline = el.value;
    }
  });

  $('btnClear').addEventListener('click', () => {
    if (tasks.length && !confirm(`清空全部 ${tasks.length} 个任务?`)) return;
    tasks = []; renderTasks(); localStorage.removeItem(DRAFT_KEY);
  });

  /* ---------- 草稿:主题/参数与任务清单存 localStorage,刷新/误退不丢 ---------- */
  const DRAFT_KEY = 'comfyweb.batchdraft';
  function saveDraft() {
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify({
        theme: $('themeInput').value, count: $('countInput').value,
        style: $('styleInput').value,
        tplPos: $('tplPos').value, tplNeg: $('tplNeg').value, tasks,
      }));
    } catch (e) { /* 存储满等忽略 */ }
  }
  function loadDraft() {
    try {
      const d = JSON.parse(localStorage.getItem(DRAFT_KEY) || 'null');
      if (!d) return;
      if (d.theme) $('themeInput').value = d.theme;
      if (d.count) $('countInput').value = d.count;
      if (d.style) $('styleInput').value = d.style;
      if (d.tplPos) $('tplPos').value = d.tplPos;
      if (d.tplNeg) $('tplNeg').value = d.tplNeg;
      if (Array.isArray(d.tasks) && d.tasks.length) {
        tasks = d.tasks; renderTasks(); $('tasksPanel').hidden = false;
      }
    } catch (e) { /* 草稿损坏则忽略 */ }
  }
  ['themeInput', 'countInput', 'styleInput', 'tplPos', 'tplNeg'].forEach(id =>
    document.getElementById(id).addEventListener('input', saveDraft));

  /* ---------- 开始生成 + 进度轮询 ---------- */

  $('btnStart').addEventListener('click', async () => {
    const errBox = $('startError');
    errBox.hidden = true;
    if (wasRunning) { toast('批次正在运行中,请先停止或等待完成', 'bad'); return; }
    const btn = $('btnStart');
    btn.classList.add('is-loading');
    btn.disabled = true;   // 防连点:重复提交会开两批
    const payload = tasks.map(t => ({
      name: t.name, pipeline: t.pipeline, w: t.w, h: t.h,
      pos_prompt: t.pos_prompt || '', prompt: t.prompt, neg: t.neg,
      batch: t.batch || '', category: t.category || '',
      seed: t.seed == null ? null : parseInt(t.seed, 10) || 0,
    }));
    try {
      const d = await toJson(await fetch('/api/batch/start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tasks: payload }),
      }));
      $('progPanel').hidden = false;
      $('galleryLink').hidden = true;
      $('progLog').textContent = '';
      pollStatus();
      if (!pollTimer) pollTimer = setInterval(pollStatus, 2000);
      $('progPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
      localStorage.removeItem(DRAFT_KEY);   // 清单已提交,草稿作废
      wasRunning = true;
    } catch (e) { errBox.textContent = e.message; errBox.hidden = false; }
    finally { btn.classList.remove('is-loading'); btn.disabled = false; }
  });

  $('btnStop').addEventListener('click', async () => {
    const btn = $('btnStop');
    btn.classList.add('is-loading');
    try {
      await toJson(await fetch('/api/batch/stop', { method: 'POST' }));
      toast('已发送停止指令,批次将在当前任务后结束');
    } catch (e) { window.toast && toast('停止失败: ' + e.message, 'bad'); }
    finally { btn.classList.remove('is-loading'); }
  });

  async function pollStatus() {
    let s;
    try { s = await toJson(await fetch('/api/batch/status')); } catch (e) { return; }
    const pct = s.total ? Math.round(s.done / s.total * 100) : 0;
    $('progBar').value = pct;
    $('progLine').textContent = s.running
      ? `进行中 ${s.done}/${s.total}(${pct}%)${s.temp ? ' · ' + s.temp : ''}${s.err ? ' · 失败 ' + s.err : ''}`
      : (s.finished ? `已结束:完成 ${s.done}/${s.total},失败 ${s.err}` : '空闲');
    $('progCurrent').textContent = s.running && s.current ? '当前:' + s.current : '';
    $('progLog').textContent = (s.log || []).join('\n');
    $('progLog').scrollTop = $('progLog').scrollHeight;
    if (wasRunning && !s.running) {
      $('galleryLink').hidden = false;
      clearInterval(pollTimer); pollTimer = null;
      if (navigator.vibrate) navigator.vibrate([150, 80, 150]);
    }
    // 页面打开时批次已在跑:自动展开进度面板接着轮询
    if (s.running && !pollTimer) {
      $('progPanel').hidden = false;
      pollTimer = setInterval(pollStatus, 2000);
    }
    wasRunning = s.running;
  }

  /* ---------- 初始化 ---------- */
  (async () => {
    try {
      const d = await toJson(await fetch('/api/batch/templates'));
      window._batchTplCache = d.templates;
      await loadTemplates(d);
    } catch (e) { /* 模板加载失败不阻塞页面 */ }
    loadDraft();
    pollStatus();
  })();
})();
