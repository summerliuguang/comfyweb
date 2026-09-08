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
    container.appendChild(fieldWrap(p.label, c));
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
      box.appendChild(fieldWrap(p.label, c));
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
