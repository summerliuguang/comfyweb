/* 生成页:加载模板表单、提交、轮询任务状态 */
(function () {
  const $ = id => document.getElementById(id);
  const wfSelect = $('wfSelect'), formArea = $('formArea'), submitRow = $('submitRow');
  const taskList = $('taskList'), tasksPanel = $('tasksPanel');
  let tpl = null;
  let pollIds = new Set(window.ACTIVE_IDS || []);
  let pollTimer = null;

  async function api(url, opts) {
    const r = await fetch(url, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.status);
    return data;
  }

  wfSelect.addEventListener('change', async () => {
    formArea.innerHTML = '';
    submitRow.hidden = true;
    if (!wfSelect.value) return;
    try {
      tpl = await api('/api/workflows/' + wfSelect.value);
      renderForm(formArea, tpl);
      submitRow.hidden = false;
    } catch (e) {
      formArea.innerHTML = `<p class="task-err">模板加载失败: ${esc(e.message)}</p>`;
    }
  });

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
      const res = await api('/api/generate', {
        method: 'POST',
        body: JSON.stringify({
          workflow_id: parseInt(wfSelect.value, 10),
          values,
          count: Math.max(1, parseInt($('countInput').value, 10) || 1),
          random_seed: $('seedRandom') ? $('seedRandom').checked : true,
        }),
      });
      for (const id of res.task_ids) pollIds.add(id);
      if (res.error) { errBox.textContent = res.error; errBox.hidden = false; }
      startPoll();
      refreshTasks();
    } catch (e) {
      errBox.textContent = e.message;
      errBox.hidden = false;
    } finally {
      btn.classList.remove('is-loading');
    }
  });

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
      for (const im of t.images.slice(0, 8)) {
        const a = document.createElement('a');
        a.href = '/gallery/image/' + im.id;
        const img = document.createElement('img');
        img.src = im.thumb;
        img.loading = 'lazy';
        a.appendChild(img);
        g.appendChild(a);
      }
      d.appendChild(g);
    }
    if (t.status === 'queued' || t.status === 'running') {
      const row = document.createElement('div');
      row.style.marginTop = '.5rem';
      const b = document.createElement('button');
      b.className = 'button is-small';
      b.textContent = '取消';
      b.addEventListener('click', async () => {
        b.classList.add('is-loading');
        try { await api(`/api/tasks/${t.id}/cancel`, { method: 'POST' }); refreshTasks(); }
        catch (e) { b.classList.remove('is-loading'); alert(e.message); }
      });
      row.appendChild(b);
      d.appendChild(row);
    }
    return d;
  }

  async function refreshTasks() {
    let ids = [...pollIds];
    if (!ids.length) {
      const recent = await api('/api/tasks/recent?limit=8').catch(() => null);
      if (!recent) return;
      renderCards(recent.tasks);
      pollIds = new Set(recent.tasks.filter(t => t.status === 'queued' || t.status === 'running').map(t => t.id));
      return;
    }
    const res = await api('/api/tasks?ids=' + ids.join(',')).catch(() => null);
    if (!res) return;
    renderCards(res.tasks);
    pollIds = new Set(res.tasks.filter(t => t.status === 'queued' || t.status === 'running').map(t => t.id));
  }

  function renderCards(tasks) {
    tasksPanel.hidden = false;
    if (!tasks.length) { taskList.innerHTML = '<p class="empty">暂无任务</p>'; return; }
    taskList.innerHTML = '';
    for (const t of tasks) taskList.appendChild(taskCard(t));
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

  refreshTasks();
  if (pollIds.size) startPoll();
})();
