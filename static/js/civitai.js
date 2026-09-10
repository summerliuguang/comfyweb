/* Civitai 模型/LoRA 页:本地已装列表、搜索、卡片网格、详情弹层 */
(function () {
  const $ = id => document.getElementById(id);
  const TYPE = window.CIVI_TYPE;
  let pageNo = 1, lastQ = '', lastSort = 'Most Downloaded', lastBase = '';
  let cursorFor = { 1: null };  // 每页的请求游标;cursorFor[N+1] 在第 N 页返回后可知
  let hasNext = false;

  async function api(path) {
    const r = await fetch(path);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || r.status);
    return d;
  }

  /* ---------- 本地已安装 ---------- */
  let autoIdentifyDone = false, identifyPoll = null;

  async function apiPost(path, body) {
    const r = await fetch(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || r.status);
    return d;
  }

  async function loadLocal() {
    const box = $('localBox');
    try {
      const d = await api('/api/local/models?type=' + window.LOCAL_DIR + '&meta=1');
      box.innerHTML = '';
      const un = d.files.filter(f => !f.identified).length;
      const head = document.createElement('div');
      head.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:.5rem';
      const txt = document.createElement('span');
      txt.style.cssText = 'font-size:.85rem;font-weight:600';
      txt.textContent = `${d.files.length} 个文件` + (un ? `,未识别 ${un}` : ',已全部识别');
      head.appendChild(txt);
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'button is-small';
      btn.textContent = un ? `识别(${un})` : '重新识别';
      btn.addEventListener('click', async () => {
        btn.classList.add('is-loading');
        try {
          await apiPost('/api/local/identify', { folder: window.LOCAL_DIR });
          startIdentifyPoll();
        } catch (e) { alert(e.message); }
        btn.classList.remove('is-loading');
      });
      head.appendChild(btn);
      box.appendChild(head);

      const wrap = document.createElement('details');
      const sum = document.createElement('summary');
      sum.textContent = '点开查看';
      sum.style.cssText = 'cursor:pointer;font-size:.85rem;font-weight:600;margin:.4rem 0';
      wrap.appendChild(sum);
      const list = document.createElement('div');
      for (const f of d.files) {
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:.6rem;padding:.3rem 0;border-bottom:1px solid var(--line);flex-wrap:wrap';
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'button is-small';
        b.textContent = f.filename.replace(/\.(safetensors|ckpt|pt|sft)$/i, '');
        b.title = '在 Civitai 搜索: ' + f.filename;
        b.style.maxWidth = '100%';
        b.addEventListener('click', () => {
          $('q').value = f.filename.replace(/\.(safetensors|ckpt|pt)$/i, '').replace(/[_-]+/g, ' ').trim();
          doSearch(true);
          $('grid').scrollIntoView({ behavior: 'smooth' });
        });
        row.appendChild(b);
        if (f.civ_name) {
          const info = document.createElement('span');
          info.className = 'task-meta';
          info.textContent = `${f.civ_name}${f.base_model ? ' · ' + f.base_model : ''}`;
          info.title = info.textContent;
          row.appendChild(info);
        } else if (!f.identified) {
          const s = document.createElement('span');
          s.className = 'task-meta';
          s.textContent = '未识别';
          row.appendChild(s);
        }
        list.appendChild(row);
      }
      wrap.appendChild(list);
      box.appendChild(wrap);

      // 有未识别条目时自动开始一次识别,并轮询进度
      if (un && !autoIdentifyDone) {
        autoIdentifyDone = true;
        apiPost('/api/local/identify', { folder: window.LOCAL_DIR })
          .then(() => startIdentifyPoll()).catch(() => {});
      }
    } catch (e) {
      box.innerHTML = `<p class="task-err">${e.message}</p>`;
    }
  }

  function startIdentifyPoll() {
    if (identifyPoll) return;
    let ticks = 0;
    identifyPoll = setInterval(async () => {
      ticks++;
      await loadLocal();
      const txt = $('localBox').textContent || '';
      const un = Number((txt.match(/未识别 (\d+)/) || [])[1] || 0);
      if (!un || ticks > 60) { clearInterval(identifyPoll); identifyPoll = null; }
    }, 5000);
  }

  /* ---------- 搜索(全部走 cursor 分页;本地库优先,refresh 强制在线更新) ---------- */
  async function doSearch(reset, refresh) {
    const errBox = $('listError');
    errBox.hidden = true;
    $('grid').innerHTML = '<p class="empty">加载中…</p>';
    $('emptyHint').hidden = true;
    $('pager').hidden = true;
    $('srcHint').hidden = true;
    if (reset) {
      lastQ = $('q').value.trim();
      lastSort = $('sort').value;
      lastBase = $('base').value;
      pageNo = 1;
      cursorFor = { 1: null };
    }
    const qs = new URLSearchParams({ type: TYPE, sort: lastSort });
    if (lastQ) qs.set('q', lastQ);
    if (lastBase) qs.set('base', lastBase);
    if (cursorFor[pageNo]) qs.set('cursor', cursorFor[pageNo]);
    if (refresh) qs.set('refresh', '1');
    try {
      const d = await api('/api/civitai/search?' + qs);
      hasNext = !!d.nextCursor;
      if (d.nextCursor) cursorFor[pageNo + 1] = d.nextCursor;
      renderGrid(d);
      const hint = $('srcHint');
      hint.textContent = d.cached
        ? `已从本地库加载(${d.items.length} 项),点「刷新」从 Civitai 更新`
        : '已从 Civitai 拉取并入库,下次秒开';
      hint.hidden = false;
    } catch (e) {
      $('grid').innerHTML = '';
      errBox.textContent = e.message;
      errBox.hidden = false;
    }
  }

  function renderGrid(d) {
    const grid = $('grid');
    grid.innerHTML = '';
    if (!d.items.length) { $('emptyHint').hidden = false; return; }
    for (const it of d.items) grid.appendChild(card(it));
    $('pageInfo').textContent = `第 ${pageNo} 页`;
    $('pager').hidden = false;
    $('prevPage').disabled = pageNo <= 1;
    $('nextPage').disabled = !hasNext;
  }

  function card(it) {
    const a = document.createElement('a');
    a.className = 'gitem';
    a.href = 'javascript:void(0)';
    const img = document.createElement('img');
    img.loading = 'lazy';
    img.alt = it.name;
    img.src = it.cover ? '/civimg?u=' + encodeURIComponent(it.cover) : '';
    a.appendChild(img);
    const meta = document.createElement('div');
    meta.className = 'gmeta';
    meta.innerHTML = '';
    const nm = document.createElement('div');
    nm.textContent = it.name;
    nm.style.cssText = 'font-weight:600;color:var(--text);white-space:normal;margin-bottom:.15rem';
    meta.appendChild(nm);
    const info = document.createElement('div');
    info.textContent = `${it.base || '?'} · ${fmtNum(it.downloads)} 下载`;
    meta.appendChild(info);
    a.appendChild(meta);
    a.addEventListener('click', () => showDetail(it.id, it.cover));
    return a;
  }

  function fmtNum(n) {
    n = n || 0;
    return n >= 10000 ? (n / 10000).toFixed(1) + 'w' : n >= 1000 ? (n / 1000).toFixed(1) + 'k' : n;
  }

  /* ---------- 详情弹层 ---------- */
  async function showDetail(id, cover, refresh) {
    const box = $('detailBox');
    if (!refresh) {  // 刷新失败时保留已打开的详情,只在首次打开时才整页替换
      box.innerHTML = '<p class="empty">加载中…</p>';
      $('detailOverlay').hidden = false;
      $('detailOverlay').scrollTop = 0;
    }
    try {
      const m = await api('/api/civitai/model/' + id + (refresh ? '?refresh=1' : ''));
      renderDetail(m, cover);
    } catch (e) {
      if (refresh) alert('从 Civitai 更新失败: ' + e.message);
      else box.innerHTML = `<p class="task-err">${e.message}</p>`;
    }
  }

  function renderDetail(m, cover) {
    const box = $('detailBox');
    box.innerHTML = '';
    const head = document.createElement('div');
    head.style.cssText = 'display:flex;justify-content:space-between;gap:.6rem;align-items:flex-start';
    const h = document.createElement('h2');
    h.className = 'title is-5';
    h.style.margin = '0';
    h.textContent = m.name;
    head.appendChild(h);
    const btns = document.createElement('div');
    btns.style.cssText = 'display:flex;gap:.4rem;flex:none';
    const upd = document.createElement('button');
    upd.className = 'button is-small is-light';
    upd.textContent = '从 Civitai 更新';
    upd.addEventListener('click', async () => {
      upd.disabled = true;
      upd.classList.add('is-loading');
      try { await showDetail(m.id, cover, true); } finally { upd.disabled = false; }
    });
    btns.appendChild(upd);
    const close = document.createElement('button');
    close.className = 'button is-small';
    close.textContent = '关闭';
    close.addEventListener('click', hideDetail);
    btns.appendChild(close);
    head.appendChild(btns);
    box.appendChild(head);

    const sub = document.createElement('p');
    sub.className = 'task-meta';
    sub.textContent = `${m.type} · ${m.creator || ''} · ${fmtNum(m.downloads)} 下载 · ${fmtNum(m.likes)} 赞`;
    sub.style.margin = '.4rem 0 .6rem';
    box.appendChild(sub);

    const img = document.createElement('img');
    img.src = cover ? '/civimg?u=' + encodeURIComponent(cover) : '';
    img.style.cssText = 'width:100%;border-radius:var(--radius-s);border:1px solid var(--line);display:block';
    img.loading = 'lazy';
    box.appendChild(img);

    if (m.tags && m.tags.length) {
      const tags = document.createElement('div');
      tags.style.cssText = 'display:flex;flex-wrap:wrap;gap:.35rem;margin-top:.6rem';
      for (const t of m.tags.slice(0, 10)) {
        const s = document.createElement('span');
        s.className = 'tag is-light';
        s.textContent = t;
        tags.appendChild(s);
      }
      box.appendChild(tags);
    }

    const v = m.versions && m.versions[0];
    if (v) {
      box.appendChild(usageBlock(m, v));
    }

    const descHead = document.createElement('h3');
    descHead.className = 'title is-6';
    descHead.style.margin = '1rem 0 .4rem';
    descHead.textContent = '说明 / 使用方法(来自 Civitai)';
    box.appendChild(descHead);
    const desc = document.createElement('div');
    desc.className = 'civ-desc';
    desc.style.cssText = 'font-size:.86rem;line-height:1.7;word-break:break-word';
    desc.innerHTML = m.description || '<p class="empty">无说明</p>';
    box.appendChild(desc);
  }

  function usageBlock(m, v) {
    const isLora = m.type === 'LORA';
    const box = document.createElement('div');
    box.className = 'panel';
    box.style.cssText = 'margin-top:.8rem;background:var(--primary-weak);border-color:#d5e0fc';

    const t = document.createElement('h3');
    t.className = 'title is-6';
    t.textContent = '在 ComfyUI 里怎么用';
    box.appendChild(t);

    const ul = document.createElement('ul');
    ul.style.cssText = 'font-size:.85rem;line-height:1.8;padding-left:1.1rem';
    const dir = isLora ? 'models/loras' : 'models/checkpoints(或 diffusion_models)';
    const li1 = document.createElement('li');
    li1.textContent = `下载文件放到 ComfyUI 的 ${dir} 目录`;
    ul.appendChild(li1);
    if (isLora) {
      const li2 = document.createElement('li');
      li2.textContent = '生成页工作流模板需含 LoRA 节点,在 LoRA 下拉中选择它;没有就先编辑模板勾选显示';
      ul.appendChild(li2);
      const li3 = document.createElement('li');
      li3.textContent = '强度建议 0.6~1.0(过低没效果,过高容易崩图)';
      ul.appendChild(li3);
    } else {
      const li2 = document.createElement('li');
      li2.textContent = '「设置」页点「刷新模型列表」,生成页模板的模型下拉里就能选到';
      ul.appendChild(li2);
      const li3 = document.createElement('li');
      li3.textContent = '注意底模匹配:此模型基于 ' + (v.baseModel || '未知') + ',提示词风格按对应底模写';
      ul.appendChild(li3);
    }
    box.appendChild(ul);

    if (v.trainedWords && v.trainedWords.length) {
      const tw = document.createElement('div');
      tw.style.marginTop = '.5rem';
      const lab = document.createElement('p');
      lab.style.cssText = 'font-size:.78rem;font-weight:600;margin-bottom:.3rem';
      lab.textContent = '触发词(点击复制):';
      tw.appendChild(lab);
      const chips = document.createElement('div');
      chips.style.cssText = 'display:flex;flex-wrap:wrap;gap:.35rem';
      for (const w of v.trainedWords) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'button is-small';
        b.textContent = w;
        b.title = '点击复制';
        b.addEventListener('click', () => {
          navigator.clipboard.writeText(w).then(() => { b.textContent = '已复制'; setTimeout(() => (b.textContent = w), 1500); });
        });
        chips.appendChild(b);
      }
      tw.appendChild(chips);
      box.appendChild(tw);
    }

    const fileLine = document.createElement('p');
    fileLine.className = 'task-meta';
    fileLine.style.marginTop = '.6rem';
    fileLine.textContent = `版本 ${v.name || ''} · ${v.file || '未知文件名'} · ${v.sizeKB ? Math.round(v.sizeKB / 1024) + ' MB' : ''} · 底模 ${v.baseModel || '?'}`;
    box.appendChild(fileLine);
    if (v.downloadUrl) {
      const dl = document.createElement('a');
      dl.className = 'button is-small is-link is-light';
      dl.href = v.downloadUrl;
      dl.target = '_blank';
      dl.rel = 'noopener';
      dl.textContent = '打开下载页(需能访问 Civitai)';
      box.appendChild(dl);
    }
    return box;
  }

  function hideDetail() { $('detailOverlay').hidden = true; }
  $('detailOverlay').addEventListener('click', e => { if (e.target === $('detailOverlay')) hideDetail(); });

  /* ---------- 事件 ---------- */
  $('searchForm').addEventListener('submit', e => { e.preventDefault(); doSearch(true); });
  $('btnRef').addEventListener('click', async () => {
    const b = $('btnRef');
    b.classList.add('is-loading');
    b.disabled = true;
    try { await doSearch(false, true); } finally {
      b.classList.remove('is-loading');
      b.disabled = false;
    }
  });
  $('sort').addEventListener('change', () => doSearch(true));
  $('base').addEventListener('change', () => doSearch(true));
  $('prevPage').addEventListener('click', () => { if (pageNo > 1) { pageNo--; doSearch(false); } });
  $('nextPage').addEventListener('click', () => { if (hasNext) { pageNo++; doSearch(false); } });

  loadLocal();
  doSearch(true);
})();
