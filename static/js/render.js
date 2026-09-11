/* 控件渲染:生成页表单与导入/编辑评审共用。
 * renderControl(param, opts) → DOM 元素,值可通过 control.dataset 恢复/收集。 */

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

function fieldWrap(labelText, controlEl, hint) {
  const f = document.createElement('div');
  f.className = 'field';
  const l = document.createElement('label');
  l.className = 'label';
  l.textContent = labelText;
  f.appendChild(l);
  const c = document.createElement('div');
  c.className = 'control';
  c.appendChild(controlEl);
  f.appendChild(c);
  if (hint) {
    const h = document.createElement('p');
    h.className = 'hint';
    h.textContent = hint;
    f.appendChild(h);
  }
  return f;
}

/* 单个控件(不含 label)。返回元素;值读写约定:
 *   input/select/textarea 直接 .value;toggle 为 .checked */
function widgetControl(p) {
  const w = p.widget;
  if (w === 'textarea') {
    const t = document.createElement('textarea');
    t.className = 'textarea';
    t.rows = p.role === 'negative' ? 2 : 3;
    t.value = p.value == null ? '' : p.value;
    return t;
  }
  if (w === 'select') {
    const d = document.createElement('div');
    d.className = 'select is-fullwidth';
    const s = document.createElement('select');
    if (p.dynamic_empty) {  // ComfyUI 不可达,动态列表拉取失败
      const ph = document.createElement('option');
      ph.disabled = true;
      ph.textContent = 'ComfyUI 未连接,列表不可用';
      s.appendChild(ph);
    }
    const opts = p.options && p.options.length ? p.options : [p.value || ''];
    for (const o of opts) {
      const op = document.createElement('option');
      op.value = o; op.textContent = o;
      if (String(o) === String(p.value)) op.selected = true;
      s.appendChild(op);
    }
    d.appendChild(s);
    d.dataset.select = '1';
    return d;
  }
  if (w === 'number' || w === 'float' || w === 'seed') {
    const i = document.createElement('input');
    i.className = 'input';
    i.type = 'number';
    i.step = w === 'float' ? (p.step || 'any') : '1';
    if (p.min != null) i.min = p.min;
    if (p.max != null) i.max = p.max;
    i.value = p.value == null ? '' : p.value;
    if (w === 'seed') i.inputmode = 'numeric';
    return i;
  }
  if (w === 'toggle') {
    const i = document.createElement('input');
    i.type = 'checkbox';
    i.checked = !!p.value;
    return i;
  }
  const i = document.createElement('input');
  i.className = 'input';
  i.type = 'text';
  i.value = p.value == null ? '' : p.value;
  return i;
}

function controlValue(el) {
  const real = el.dataset && el.dataset.select ? el.querySelector('select') : el;
  if (real.type === 'checkbox') return real.checked;
  return real.value;
}

/* 生成页表单:visible 参数 + 高级折叠区 */
function renderForm(container, tpl) {
  container.innerHTML = '';
  const vis = (tpl.params || []).filter(p => p.visible && !p.advanced);
  const advVisible = (tpl.params || []).filter(p => p.visible && p.advanced);
  const hidden = (tpl.params || []).filter(p => !p.visible);

  for (const p of vis) {
    if (p.widget === 'seed') {
      container.appendChild(seedField(p));
      continue;
    }
    const c = widgetControl(p);
    c.dataset.pname = p.name;
    const f = fieldWrap(p.label, c);
    if (p.widget === 'textarea') enhancePromptField(f, p);
    container.appendChild(f);
  }
  if (advVisible.length) {
    const det = document.createElement('details');
    det.className = 'adv';
    const sum = document.createElement('summary');
    sum.textContent = '高级参数';
    det.appendChild(sum);
    const box = document.createElement('div');
    for (const p of advVisible) {
      const c = widgetControl(p);
      c.dataset.pname = p.name;
      const f = fieldWrap(p.label, c);
      if (p.widget === 'textarea') enhancePromptField(f, p);
      box.appendChild(f);
    }
    det.appendChild(box);
    container.appendChild(det);
  }
  // 隐藏参数的默认值也要随提交发送,避免被模板外的旧值覆盖
  for (const p of hidden) {
    const i = document.createElement('input');
    i.type = 'hidden';
    i.dataset.pname = p.name;
    i.value = p.value == null ? '' : p.value;
    container.appendChild(i);
  }
  return { hidden };
}

function seedField(p) {
  const f = document.createElement('div');
  f.className = 'field';
  const l = document.createElement('label');
  l.className = 'label';
  l.textContent = p.label || '随机种子';
  f.appendChild(l);
  const row = document.createElement('div');
  row.className = 'seed-row';
  const c = widgetControl(p);
  c.dataset.pname = p.name;
  row.appendChild(c);
  const lab = document.createElement('label');
  lab.className = 'checkbox';
  lab.style.fontSize = '.82rem';
  lab.style.whiteSpace = 'nowrap';
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = true;
  cb.id = 'seedRandom';
  lab.appendChild(cb);
  lab.appendChild(document.createTextNode(' 每次随机'));
  row.appendChild(lab);
  f.appendChild(row);
  return f;
}

/* 导入/编辑评审表格 */
function renderParamEditor(tbody, params) {
  tbody.innerHTML = '';
  for (const p of params) {
    const tr = document.createElement('tr');
    tr.dataset.pname = p.name;

    const tdVis = document.createElement('td');
    tdVis.className = 'w-check';
    const vis = document.createElement('input');
    vis.type = 'checkbox';
    vis.checked = !!p.visible;
    vis.title = '在生成页显示';
    tdVis.appendChild(vis);

    const tdLabel = document.createElement('td');
    tdLabel.className = 'w-label';
    const li = document.createElement('input');
    li.className = 'input is-small';
    li.type = 'text';
    li.value = p.label || '';
    tdLabel.appendChild(li);

    const tdWidget = document.createElement('td');
    tdWidget.className = 'w-widget';
    const tag = document.createElement('span');
    tag.className = 'wf-widget-tag';
    tag.textContent = widgetName(p.widget);
    tdWidget.appendChild(tag);

    const tdVal = document.createElement('td');
    const c = widgetControl(p);
    c.classList && c.classList.add('is-small');
    tdVal.appendChild(c);

    const tdAdv = document.createElement('td');
    tdAdv.className = 'w-check';
    const adv = document.createElement('input');
    adv.type = 'checkbox';
    adv.checked = !!p.advanced;
    adv.title = '归入高级折叠区';
    tdAdv.appendChild(adv);

    tr.append(tdVis, tdLabel, tdWidget, tdVal, tdAdv);
    tr._param = p;
    tbody.appendChild(tr);
  }
}

function collectParams(tbody) {
  const out = [];
  for (const tr of tbody.querySelectorAll('tr')) {
    const p = Object.assign({}, tr._param);
    const cells = tr.children;
    p.visible = cells[0].querySelector('input').checked;
    p.label = cells[1].querySelector('input').value.trim() || p.name;
    const valEl = cells[3].firstChild;
    p.value = controlValue(valEl);
    p.advanced = cells[4].querySelector('input').checked;
    out.push(p);
  }
  return out;
}

const WIDGET_NAMES = { textarea: '文本域', text: '文本', number: '整数', float: '小数',
  select: '下拉', toggle: '开关', seed: '种子' };
function widgetName(w) { return WIDGET_NAMES[w] || w || '文本'; }

const STATUS_TEXT = { queued: '排队中', running: '生成中', done: '完成',
  error: '失败', canceled: '已取消' };
function statusDot(status) {
  const s = document.createElement('span');
  s.className = 'status dot-' + status;
  s.textContent = STATUS_TEXT[status] || status;
  return s;
}

/* ===== 提示词框增强(生成页):Danbooru tag 联想 + AI 润色/翻译 ===== */

const AI_STYLES = [['enhance', '通用增强'], ['detail', '细节丰富'], ['anime', '动漫风'],
  ['photo', '写实摄影'], ['concise', '精简']];

/* 模型列表每页只拉一次;失败不缓存,下次再试 */
let _aiModelsCache = null;
function _aiModels() {
  if (!_aiModelsCache) {
    _aiModelsCache = fetch('/api/ai/models').then(r => r.json()).catch(() => {
      _aiModelsCache = null;
      return {};
    });
  }
  return _aiModelsCache;
}

/* 光标所在的当前 tag(逗号/换行分隔的一段) */
function _currentToken(t) {
  const pos = t.selectionStart == null ? t.value.length : t.selectionStart;
  const before = t.value.slice(0, pos);
  const m = before.match(/[^,\n]*$/);
  return { start: pos - m[0].length, end: pos, text: m[0] };
}

function _attachTagComplete(control, t) {
  const dd = document.createElement('div');
  dd.className = 'tag-dd';
  dd.hidden = true;
  control.appendChild(dd);
  let items = [], active = -1, tmr = 0, fetchSeq = 0;
  const close = () => { dd.hidden = true; items = []; active = -1; };
  const render = () => {
    dd.innerHTML = '';
    items.forEach((it, i) => {
      const d = document.createElement('div');
      d.className = 'tag-dd-item' + (i === active ? ' on' : '');
      const nm = document.createElement('span');
      nm.textContent = it.name;
      const meta = document.createElement('span');
      meta.className = 'meta';
      meta.textContent = (it.cat ? it.cat + ' · ' : '') +
        (it.count >= 10000 ? Math.round(it.count / 10000) + '万' : it.count);
      d.append(nm, meta);
      d.addEventListener('pointerdown', (e) => { e.preventDefault(); accept(i); });
      dd.appendChild(d);
    });
    dd.hidden = !items.length;
  };
  const accept = (i) => {
    const it = items[i];
    if (!it) return;
    const tok = _currentToken(t);
    const after = t.value.slice(tok.end);
    const afterTrim = after.trim();
    // 结尾补逗号;后面已有内容且没逗号隔开时也补一个
    const insert = it.name + (afterTrim && !afterTrim.startsWith(',') ? ',' :
                              afterTrim ? '' : ', ');
    t.value = t.value.slice(0, tok.start) + insert + after;
    t.selectionStart = t.selectionEnd = tok.start + insert.length;  // 落在补的逗号后,继续输入即新 tag
    close();
    t.dispatchEvent(new Event('input', { bubbles: true }));
    t.focus();
  };
  t.addEventListener('input', () => {
    clearTimeout(tmr);
    const q = _currentToken(t).text.trim().toLowerCase();
    if (!q) { close(); return; }
    const seq = ++fetchSeq;                 // 快速输入时丢弃过期响应,防止旧结果闪回
    tmr = setTimeout(async () => {
      try {
        const r = await fetch('/api/tags/search?q=' + encodeURIComponent(q));
        const tags = r.ok ? ((await r.json()).tags || []) : [];
        if (seq !== fetchSeq) return;
        items = tags;
      } catch (e) {
        if (seq !== fetchSeq) return;
        items = [];
      }
      active = items.length ? 0 : -1;
      render();
    }, 180);
  });
  t.addEventListener('keydown', (e) => {
    if (dd.hidden) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (items.length) {
        active = (active + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length;
        render();
      }
    } else if ((e.key === 'Enter' || e.key === 'Tab') && active >= 0) {
      e.preventDefault();
      accept(active);
    } else if (e.key === 'Escape') {
      close();
    }
  });
  t.addEventListener('blur', () => setTimeout(close, 150));
}

function _attachAiRow(control, t) {
  const row = document.createElement('div');
  row.className = 'ai-row';
  const wrap = document.createElement('div');
  wrap.className = 'select is-small ai-style';
  const sel = document.createElement('select');
  for (const [v, label] of AI_STYLES) {
    const op = document.createElement('option');
    op.value = v;
    op.textContent = label;
    sel.appendChild(op);
  }
  wrap.appendChild(sel);
  row.appendChild(wrap);
  // 模型下拉:列表来自 LiteGate 网关,选择记忆在 localStorage
  const mwrap = document.createElement('div');
  mwrap.className = 'select is-small ai-model';
  mwrap.hidden = true;
  const selM = document.createElement('select');
  mwrap.appendChild(selM);
  row.appendChild(mwrap);
  _aiModels().then(d => {
    const models = d.models || [];
    if (!models.length) return;
    const saved = localStorage.getItem('comfyweb.aimodel');
    const want = models.includes(saved) ? saved : d.default;   // 已下线的模型回退默认
    for (const m of models) {
      const op = document.createElement('option');
      op.value = m;
      op.textContent = m;
      if (m === want) op.selected = true;
      selM.appendChild(op);
    }
    mwrap.hidden = false;
  }).catch(() => {});
  selM.addEventListener('change', () =>
    localStorage.setItem('comfyweb.aimodel', selM.value));
  const run = async (kind, btn) => {
    const text = t.value.trim();
    if (!text) return;
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = kind === 'polish' ? '润色中…' : '翻译中…';
    try {
      const r = await fetch('/api/ai/' + kind, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, style: sel.value, model: selM.value || undefined }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || r.status);
      t.value = d.text;
      t.dispatchEvent(new Event('input', { bubbles: true }));
      t.focus();
    } catch (e) {
      alert('AI ' + (kind === 'polish' ? '润色' : '翻译') + '失败: ' + e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  };
  for (const [kind, label] of [['polish', 'AI 润色'], ['translate', 'AI 翻译']]) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'button is-small is-light';
    b.textContent = label;
    b.addEventListener('click', () => run(kind, b));
    row.appendChild(b);
  }
  control.appendChild(row);
}

/* textarea 参数字段:tag 联想下拉;正面提示词加 AI 润色/翻译行 */
function enhancePromptField(field, p) {
  const t = field.querySelector('textarea');
  if (!t) return;
  const control = field.querySelector('.control');
  control.classList.add('prompt-control');
  _attachTagComplete(control, t);
  if (p.role === 'positive') _attachAiRow(control, t);
}
