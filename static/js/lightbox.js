/* 通用灯箱:浏览模式(上下滑动/滚轮/方向键切换图片) + 聚焦模式(捏合/滚轮缩放、拖动平移)。
 * 用法:Lightbox.open([{url, thumb, caption?}, ...], startIndex, {caption?})
 */
(function () {
  const SWIPE_PX = 48;

  let root = null, imgEl = null, cntEl = null, capEl = null, hintEl = null;
  let imgs = [], idx = 0, mode = 'browse', onIdxChange = null;
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
      '<span class="lb-count"></span>' +
      '<div class="lb-stage"><img class="lb-img" alt=""></div>' +
      '<div class="lb-cap"></div>' +
      '<div class="lb-hint">上下滑动切换 · 点击图片放大 · ×关闭</div>';
    document.body.appendChild(root);
    imgEl = root.querySelector('.lb-img');
    cntEl = root.querySelector('.lb-count');
    capEl = root.querySelector('.lb-cap');
    hintEl = root.querySelector('.lb-hint');
    bind();
  }

  function apply() {
    imgEl.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
  }

  function setMode(m) {
    mode = m;
    root.classList.toggle('focus', m === 'focus');
    scale = 1; tx = 0; ty = 0; apply();
    hintEl.textContent = m === 'focus'
      ? '捏合/滚轮缩放 · 拖动平移 · 双击复位 · 点击图片返回'
      : '上下滑动切换 · 点击图片放大 · ×关闭';
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
    ensure();
    show(Math.max(0, Math.min(start || 0, list.length - 1)));
    root.hidden = false;
    document.body.style.overflow = 'hidden';
  }

  function close() {
    if (!root || root.hidden) return;
    root.hidden = true;
    document.body.style.overflow = '';
    imgEl.src = '';
  }

  function dist2(a, b) {
    return Math.hypot(a.x - b.x, a.y - b.y);
  }

  function bind() {
    root.querySelector('.lb-close').addEventListener('click', close);

    root.addEventListener('wheel', (e) => {
      e.preventDefault();
      if (mode === 'browse') {
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
      if (mode === 'browse' && !dragged) { e.stopPropagation(); setMode('focus'); }
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

  window.Lightbox = { open, close };
})();
