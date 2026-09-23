/* 通用灯箱:浏览模式(上下滑动/滚轮/方向键切换图片) + 聚焦模式(捏合/滚轮缩放、拖动平移)。
 * 用法:Lightbox.open([{url, thumb, caption?}, ...], startIndex,
 *                     {onIdxChange?, onShow?, onAction?, onClose?, onImgTap?})
 * onShow(i)/onAction(i):可选底部操作按钮——传入 onAction 即显示,show/点击时回调。
 * onImgTap(i):传入后点击图片=触发它(勾选场景),不再进入聚焦缩放;onClose():关闭时回调。
 */
(function () {
  const SWIPE_PX = 48;

  let root = null, imgEl = null, cntEl = null, capEl = null, hintEl = null, actEl = null;
  let imgs = [], idx = 0, mode = 'browse';
  let onIdxChange = null, onShow = null, onAction = null, onClose = null, onImgTap = null;
  let scale = 1, tx = 0, ty = 0;
  let pointers = new Map();   // pointerId -> {x,y}
  let startDist = 0, startScale = 1;
  let startY = 0, startX = 0, dragged = false;

  function ensure() {
    if (root) return;
    root = document.createElement('div');
    root.className = 'lightbox';
    root.hidden = true;
    root.innerHTML =
      '<button class="lb-close" type="button" aria-label="关闭">×</button>' +
      '<button class="lb-bg" type="button" aria-label="切换背景色">◐</button>' +
      '<span class="lb-count"></span>' +
      '<div class="lb-stage"><img class="lb-img" alt=""></div>' +
      '<div class="lb-cap"></div>' +
      '<button class="lb-act" type="button" hidden></button>' +
      '<div class="lb-hint"></div>';
    document.body.appendChild(root);
    imgEl = root.querySelector('.lb-img');
    cntEl = root.querySelector('.lb-count');
    capEl = root.querySelector('.lb-cap');
    hintEl = root.querySelector('.lb-hint');
    actEl = root.querySelector('.lb-act');
    if (localStorage.getItem('comfyweb.viewerbg') === 'light') {
      root.classList.add('lb-light');
    }
    root.querySelector('.lb-bg').addEventListener('click', () => {
      const light = root.classList.toggle('lb-light');
      localStorage.setItem('comfyweb.viewerbg', light ? 'light' : 'dark');
    });
    bind();
  }

  function apply() {
    imgEl.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
  }

  function hintText() {
    if (mode === 'focus') {
      return '捏合/滚轮缩放 · 拖动平移 · 双击复位 · 点击图片返回';
    }
    const sw = imgs.length > 1 ? '上下滑动切换 · ' : '';
    if (onImgTap) return `${sw}点击图片勾选/取消 · ◐切换背景`;
    return `${sw}点击图片放大 · ◐切换背景`;
  }

  function setMode(m) {
    mode = m;
    root.classList.toggle('focus', m === 'focus');
    scale = 1; tx = 0; ty = 0; apply();
    hintEl.textContent = hintText();
  }

  function show(i) {
    if (!imgs.length) return;
    idx = (i + imgs.length) % imgs.length;
    const it = imgs[idx];
    imgEl.src = it.url || it.thumb;
    cntEl.textContent = imgs.length > 1 ? `${idx + 1} / ${imgs.length}` : '';
    const cap = it.caption || '';
    capEl.textContent = cap.length > 80 ? cap.slice(0, 80) + '…' : cap;
    capEl.hidden = !cap;
    setMode('browse');
    if (onShow) onShow(idx);
  }

  function step(d) {
    if (imgs.length < 2) return;
    show(idx + d);
    if (onIdxChange) onIdxChange(idx);
  }

  function open(list, start, opts) {
    if (!list || !list.length) return;
    imgs = list;
    onIdxChange = (opts && opts.onIdxChange) || null;
    onShow = (opts && opts.onShow) || null;
    onAction = (opts && opts.onAction) || null;
    onClose = (opts && opts.onClose) || null;
    onImgTap = (opts && opts.onImgTap) || null;
    ensure();
    root.classList.remove('closing');   // 快速关开后清掉淡出态(forwards 会保持透明)
    if (actEl) actEl.hidden = !onAction;
    show(Math.max(0, Math.min(start || 0, list.length - 1)));
    root.hidden = false;
    document.body.style.overflow = 'hidden';
  }

  function close() {
    if (!root || root.hidden) return;
    root.classList.add('closing');            // 先播淡出,再卸载(hidden 立即消失太生硬)
    setTimeout(() => {
      root.classList.remove('closing');
      root.hidden = true;
      document.body.style.overflow = '';
      imgEl.src = '';
      if (onClose) { const cb = onClose; onClose = null; cb(); }
    }, 150);
  }

  function dist2(a, b) {
    return Math.hypot(a.x - b.x, a.y - b.y);
  }

  function bind() {
    root.querySelector('.lb-close').addEventListener('click', close);
    actEl.addEventListener('click', () => { if (onAction) onAction(idx); });

    let wheelLock = 0;   // 触控板一格滚动会连发多个 wheel,不加锁一次跳好几张
    root.addEventListener('wheel', (e) => {
      e.preventDefault();
      if (mode === 'browse') {
        const now = Date.now();
        if (now - wheelLock < 350) return;
        wheelLock = now;
        if (e.deltaY > 0) step(1); else if (e.deltaY < 0) step(-1);
      } else {
        scale = Math.min(6, Math.max(1, scale * (e.deltaY < 0 ? 1.15 : 0.87)));
        if (scale === 1) { tx = 0; ty = 0; }
        apply();
      }
    }, { passive: false });

    root.addEventListener('pointerdown', (e) => {
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      dragged = false;
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        startDist = dist2(a, b); startScale = scale;
      }
      startX = e.clientX; startY = e.clientY;
      if (mode === 'focus') imgEl.setPointerCapture(e.pointerId);
    });

    root.addEventListener('pointermove', (e) => {
      if (!pointers.has(e.pointerId)) return;
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pointers.size === 2 && mode === 'focus') {
        const [a, b] = [...pointers.values()];
        scale = Math.min(6, Math.max(1, startScale * dist2(a, b) / (startDist || 1)));
        apply();
        return;
      }
      if (mode === 'focus') {
        const dx = e.clientX - startX, dy = e.clientY - startY;
        if (Math.abs(dx) + Math.abs(dy) > 4) dragged = true;
        tx += dx; ty += dy;
        startX = e.clientX; startY = e.clientY;
        apply();
      } else { // 浏览模式:跟随拖动,松手判定
        const dy = e.clientY - startY;
        if (Math.abs(dy) > 8) dragged = true;
        imgEl.style.transform = `translateY(${dy * 0.4}px)`;
      }
    });

    function endPointer(e) {
      const wasTwo = pointers.size === 2;
      pointers.delete(e.pointerId);
      if (mode === 'browse') {
        const dy = e.clientY - startY;
        imgEl.style.transform = '';
        if (dragged && Math.abs(dy) >= SWIPE_PX) step(dy < 0 ? 1 : -1);
      } else if (!wasTwo && !dragged && e.target === imgEl) {
        setMode('browse');  // 聚焦模式点一下图片返回浏览
      }
    }
    root.addEventListener('pointerup', endPointer);
    root.addEventListener('pointercancel', endPointer);

    imgEl.addEventListener('click', (e) => {
      if (mode === 'browse' && !dragged) {
        e.stopPropagation();
        if (onImgTap) { onImgTap(idx); return; }
        setMode('focus');
      }
    });
    imgEl.addEventListener('dblclick', (e) => {
      e.preventDefault();
      if (mode === 'focus') {
        scale = scale > 1 ? 1 : 2.5; tx = 0; ty = 0; apply();
      }
    });

    document.addEventListener('keydown', (e) => {
      if (root.hidden) return;
      if (e.key === 'Escape') close();
      else if (mode === 'browse' && e.key === 'ArrowDown') step(1);
      else if (mode === 'browse' && e.key === 'ArrowUp') step(-1);
    });
  }

  /* 运行中更新底部说明文字(如异步拉取的图片详情);空串隐藏 */
  function setCaption(t) {
    if (!capEl) return;
    capEl.textContent = t || '';
    capEl.hidden = !t;
  }

  window.Lightbox = { open, close, setCaption };
})();
