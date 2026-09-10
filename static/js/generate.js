/* 生成页:加载模板表单、提交、轮询任务状态;草稿自动保存、再次生成、完成提醒 */
(function () {
  const $ = id => document.getElementById(id);
  const wfSelect = $('wfSelect'), formArea = $('formArea'), submitRow = $('submitRow');
  const taskList = $('taskList'), tasksPanel = $('tasksPanel');
  let tpl = null;
  let pollIds = new Set(window.ACTIVE_IDS || []);
  let pollTimer = null;
  const lastStatus = {};  // taskId -> 上次状态,用于完成提醒

  const draftKey = wid => 'comfyweb.draft.' + wid;

  // fetch 一律用字面量路径 + parseInt 过的数值 id(路径段强制数值);
  // 响应处理统一走 toJson,不再有"URL 参数进 fetch"的封装
  const numId = v => parseInt(v, 10);

  async function toJson(r) {
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.status);
    return data;
  }

  let draftTimer = null;
  function saveDraft() {          // 输入事件高频触发,300ms 防抖后再写 localStorage
    if (!tpl) return;
    clearTimeout(draftTimer);
    draftTimer = setTimeout(_saveDraft, 300);
  }

  function _saveDraft() {
    if (!tpl) return;
    const values = {};
    for (const el of formArea.querySelectorAll('[data-pname]')) {
      if (el.type === 'hidden') continue;
      values[el.dataset.pname] = controlValue(el);
    }
    localStorage.setItem(draftKey(tpl.id), JSON.stringify({
      values, count: $('countInput').value, random: $('seedRandom') ? $('seedRandom').checked : true,
    }));
  }

  function applyDraft(values) {
    for (const el of formArea.querySelectorAll('[data-pname]')) {
      if (el.type === 'hidden' || !(el.dataset.pname in values)) continue;
      const v = values[el.dataset.pname];
      const real = el.dataset && el.dataset.select ? el.querySelector('select') : el;
      if (real.type === 'checkbox') real.checked = !!v;
      else if ([...real.options || []].some(o => o.value === String(v))) real.value = v;
      else if (!real.options) real.value = v == null ? '' : v;
    }
    formArea.querySelectorAll('textarea').forEach(t => comfyAutosizeFit(t));
  }

  async function loadTemplate() {
    formArea.innerHTML = '';
    submitRow.hidden = true;
    const wid = wfSelect.value;
    if (!wid) return;
    try {
      tpl = await toJson(await fetch('/api/workflows/' + numId(wid)));
      renderForm(formArea, tpl);
      const draft = localStorage.getItem(draftKey(tpl.id));
      if (draft) {
        try {
          const d = JSON.parse(draft);
          applyDraft(d.values || {});
          $('countInput').value = d.count || 1;
          if ($('seedRandom')) $('seedRandom').checked = d.random !== false;
        } catch (e) { /* 草稿损坏则忽略 */ }
      }
      submitRow.hidden = false;
      localStorage.setItem('comfyweb.lastwf', String(tpl.id));
      loadPromptChips();
    } catch (e) {
      formArea.innerHTML = `<p class="task-err">模板加载失败: ${esc(e.message)}</p>`;
    }
  }

  /* 最近用过的提示词,点击回填 */
  async function loadPromptChips() {
    if (!tpl) return;
    const params = tpl.params || [];
    // 与后端 build_prompt 的兜底一致:无 positive 角色时取第一个可见文本域
    const pos = params.find(p => p.role === 'positive') ||
                params.find(p => p.visible && p.widget === 'textarea');
    if (!pos) return;
    let d;
    try {
      d = await toJson(await fetch('/api/prompts?workflow_id=' + numId(tpl.id)));
    } catch (e) { return; }
    if (!d.prompts || !d.prompts.length) return;
    const el = [...formArea.querySelectorAll('[data-pname]')]
      .find(x => x.dataset.pname === pos.name);
    if (!el) return;
    const old = document.getElementById('promptChips');
    if (old) old.remove();
    const box = document.createElement('div');
    box.id = 'promptChips';
    box.style.cssText = 'display:flex;flex-wrap:wrap;gap:.35rem;align-items:center;margin:-.2rem 0 .8rem';
    const lab = document.createElement('span');
    lab.className = 'hint';
    lab.textContent = '最近使用:';
    box.appendChild(lab);
    for (const p of d.prompts.slice(0, 5)) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'button is-small';
      chip.textContent = p.length > 26 ? p.slice(0, 26) + '…' : p;
      chip.title = p;
      chip.addEventListener('click', () => { el.value = p; saveDraft(); });
      box.appendChild(chip);
    }
    el.closest('.field').after(box);
  }

  wfSelect.addEventListener('change', () => loadTemplate());
  formArea.addEventListener('input', () => saveDraft());
  formArea.addEventListener('change', () => saveDraft());

  $('btnGenerate').addEventListener('click', async () => {
    const btn = $('btnGenerate');
    const errBox = $('genError');
    errBox.hidden = true;
    const values = {};
    for (const el of formArea.querySelectorAll('[data-pname]')) {
      values[el.dataset.pname] = controlValue(el);
    }
    btn.classList.add('is-loading');
    try {
      const res = await toJson(await fetch('/api/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          workflow_id: parseInt(wfSelect.value, 10),
          values,
          count: Math.max(1, parseInt($('countInput').value, 10) || 1),
          random_seed: $('seedRandom') ? $('seedRandom').checked : true,
        }),
      }));
      for (const id of res.task_ids) pollIds.add(id);
      if (res.error) { errBox.textContent = res.error; errBox.hidden = false; }
      saveDraft();
      startPoll();
      refreshTasks();
      // 手机上把进度送到眼前
      setTimeout(() => {
        const card = taskList.querySelector('.task-card');
        if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }, 150);
    } catch (e) {
      errBox.textContent = e.message;
      errBox.hidden = false;
    } finally {
      btn.classList.remove('is-loading');
    }
  });

  /* ---------- 再次生成:把任务参数回填到表单 ---------- */

  let refillTaskId = null;  // 待回填的任务 id(赋值处已 numId 强转)

  async function refillFromTask() {
    if (!tpl || refillTaskId == null) return;
    let t;
    try {
      t = (await toJson(await fetch('/api/tasks/' + numId(refillTaskId)))).task;
    } catch (e) { alert(e.message); return; }
    const byLabel = {};
    for (const p of t.params || []) byLabel[p.label] = p.value;
    let matched = 0;
    for (const el of formArea.querySelectorAll('[data-pname]')) {
      if (el.type === 'hidden') continue;
      const def = (tpl.params || []).find(p => p.name === el.dataset.pname);
      if (!def || !(def.label in byLabel)) continue;
      const v = byLabel[def.label];
      const real = el.dataset && el.dataset.select ? el.querySelector('select') : el;
      if (real.type === 'checkbox') real.checked = !!v;
      else if (real.options && ![...real.options].some(o => o.value === String(v))) continue;
      else real.value = v == null ? '' : v;
      matched++;
    }
    if (t.seed != null) {
      const el = [...formArea.querySelectorAll('[data-pname]')].find(e => {
        const real = e.dataset && e.dataset.select ? e.querySelector('select') : e;
        return real.type === 'number' && real.closest('.seed-row');
      });
      if (el) {
        const real = el.dataset && el.dataset.select ? el.querySelector('select') : el;
        real.value = t.seed;
        const cb = $('seedRandom');
        if (cb) cb.checked = false;
        matched++;
      }
    }
    saveDraft();
    formArea.querySelectorAll('textarea').forEach(t => comfyAutosizeFit(t));
    formArea.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  /* ---------- 任务列表 ---------- */

  function taskCard(t) {
    const d = document.createElement('div');
    d.className = 'task-card';
    const head = document.createElement('div');
    head.className = 'task-head';
    const left = document.createElement('div');
    left.appendChild(statusDot(t.status));
    const name = document.createElement('span');
    name.style.cssText = 'font-weight:600;font-size:.88rem;margin-left:.5rem';
    name.textContent = t.workflow_name;
    left.appendChild(name);
    head.appendChild(left);
    const right = document.createElement('div');
    right.className = 'task-meta';
    right.textContent = `#${t.id} · ${t.created_at}`;
    head.appendChild(right);
    d.appendChild(head);

    if (t.status === 'queued' && t.queue_pos) {
      const q = document.createElement('p');
      q.className = 'task-meta';
      q.textContent = `排队第 ${t.queue_pos} 位`;
      d.appendChild(q);
    }
    if (t.status === 'running') {
      const p = document.createElement('progress');
      p.className = 'progress is-link is-small';
      p.max = 100;
      p.value = t.progress || 0;
      d.appendChild(p);
    }
    if (t.error) {
      const e = document.createElement('p');
      e.className = 'task-err';
      e.textContent = t.error;
      d.appendChild(e);
    }
    if (t.prompt_text) {
      const pr = document.createElement('p');
      pr.className = 'task-prompt';
      pr.textContent = t.prompt_text;
      pr.title = t.prompt_text;
      d.appendChild(pr);
    }
    if (t.images && t.images.length) {
      const g = document.createElement('div');
      g.className = 'task-imgs';
      const list = t.images.map(im => ({ url: im.url, thumb: im.thumb }));
      t.images.slice(0, 8).forEach((im, i) => {
        const a = document.createElement('a');
        a.href = im.url;
        a.addEventListener('click', (ev) => {
          ev.preventDefault();
          Lightbox.open(list, i, { caption: t.prompt_text || '' });
        });
        const img = document.createElement('img');
        img.src = im.thumb;
        img.loading = 'lazy';
        a.appendChild(img);
        g.appendChild(a);
      });
      d.appendChild(g);
    }
    const row = document.createElement('div');
    row.style.cssText = 'margin-top:.5rem;display:flex;gap:.5rem';
    if (t.status === 'queued' || t.status === 'running') {
      const b = document.createElement('button');
      b.className = 'button is-small';
      b.textContent = '取消';
      b.addEventListener('click', async () => {
        b.classList.add('is-loading');
        try { await toJson(await fetch(`/api/tasks/${numId(t.id)}/cancel`, { method: 'POST' })); refreshTasks(); }
        catch (e) { b.classList.remove('is-loading'); alert(e.message); }
      });
      row.appendChild(b);
    } else if (t.status === 'done' && t.workflow_id) {
      const b = document.createElement('button');
      b.className = 'button is-small is-link is-light';
      b.textContent = '再来一次';
      b.addEventListener('click', () => { refillTaskId = numId(t.id); refillFromTask(); });
      row.appendChild(b);
      const a = document.createElement('a');
      a.className = 'button is-small';
      a.href = '/gallery';
      a.textContent = '在画廊查看';
      row.appendChild(a);
    }
    if (row.children.length) d.appendChild(row);
    return d;
  }

  async function refreshTasks() {
    let ids = [...pollIds];
    if (!ids.length) {
      const recent = await toJson(await fetch('/api/tasks/recent?limit=8')).catch(() => null);
      if (!recent) return;
      renderCards(recent.tasks);
      pollIds = new Set(recent.tasks.filter(t => t.status === 'queued' || t.status === 'running').map(t => t.id));
      return;
    }
    const res = await toJson(await fetch('/api/tasks?ids=' + ids.map(numId).join(','))).catch(() => null);
    if (!res) return;
    renderCards(res.tasks);
    pollIds = new Set(res.tasks.filter(t => t.status === 'queued' || t.status === 'running').map(t => t.id));
  }

  function renderCards(tasks) {
    tasksPanel.hidden = false;
    if (!tasks.length) { taskList.innerHTML = '<p class="empty">暂无任务</p>'; return; }
    taskList.innerHTML = '';
    for (const t of tasks) {
      taskList.appendChild(taskCard(t));
      if (lastStatus[t.id] && lastStatus[t.id] !== 'done' && t.status === 'done') notifyDone(t);
      lastStatus[t.id] = t.status;
    }
  }

  /* 完成提醒:震动 + 标题闪烁(页面在后台时) */
  let flashTimer = null;
  const baseTitle = document.title;
  function notifyDone(t) {
    if (navigator.vibrate) navigator.vibrate([150, 80, 150]);
    if (!document.hidden || flashTimer) return;
    let on = false, n = 0;
    flashTimer = setInterval(() => {
      document.title = (on = !on) ? '生成完成' : baseTitle;
      if (++n >= 6) { clearInterval(flashTimer); flashTimer = null; document.title = baseTitle; }
    }, 900);
  }

  function startPoll() {
    if (pollTimer) return;
    pollTimer = setInterval(async () => {
      if (!pollIds.size) {
        clearInterval(pollTimer);
        pollTimer = null;
        return;
      }
      refreshTasks();
    }, 2000);
  }

  /* 初始化:记住上次模板;支持 /?template=<id>&task=<id> 再次生成入口 */
  const q = new URLSearchParams(location.search);
  const last = localStorage.getItem('comfyweb.lastwf');
  const want = q.get('template') || last;
  if (want && [...wfSelect.options].some(o => o.value === want)) {
    wfSelect.value = want;
    loadTemplate().then(async () => {
      const tid = numId(q.get('task'));
      if (tid) {
        try {
          const t = (await toJson(await fetch('/api/tasks/' + tid))).task;
          if (t.workflow_id === numId(want)) {
            refillTaskId = tid;
            await refillFromTask();
          }
        } catch (e) { /* 忽略 */ }
        history.replaceState(null, '', '/');
      }
    });
  }

  refreshTasks();
  if (pollIds.size) startPoll();
})();
