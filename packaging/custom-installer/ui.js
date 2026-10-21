(() => {
  'use strict';
  const $ = s => document.querySelector(s);
  const screens = [...document.querySelectorAll('.screen')];
  let current = 'welcome', busy = false, closing = false, swap = 0;
  const send = (cmd, extra = {}) => window.ipc.postMessage(JSON.stringify({ cmd, ...extra }));
  function show(name) {
    const before = $('#s-' + current), after = $('#s-' + name), token = ++swap;
    current = name;
    screens.forEach(s => { if (s !== before) { s.hidden = true; s.classList.remove('enter', 'leave'); } });
    if (before === after) return;
    before.classList.add('leave');
    setTimeout(() => {
      if (token !== swap) return;
      before.hidden = true; before.classList.remove('leave');
      after.hidden = false; after.classList.remove('enter');
      void after.offsetWidth; after.classList.add('enter');
    }, 150);
  }
  function close(launch) {
    if (busy || closing) return;
    closing = true;
    $('#win').classList.add('closing');
    const duration = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--m-close'));
    setTimeout(() => send(launch ? 'launch' : 'close'), duration || 200);
  }
  window.host = msg => {
    if (msg.kind === 'init') {
      $('#folder').value = msg.folder;
      if (msg.installed) {
        $('[data-name]').hidden = true;
        $('#s-welcome .ver').hidden = true;
        $('#s-welcome .logo').classList.add('mid');
        ['#updateTitle', '#updateChip', '#updateLead'].forEach(s => { $(s).hidden = false; });
        $('#installedVersion').textContent = msg.installedVersion || 'installed';
        $('#updateLead').textContent = APP.updateText;
        $('[data-install]').textContent = 'Update';
      }
      $('#licenceText').textContent = APP.licence;
      $('#win').classList.add('opening');
      const logo = $('#s-welcome [data-logo]');
      logo.classList.add('shine');
    } else if (msg.kind === 'folder') {
      $('#folder').value = msg.folder;
    } else if (msg.kind === 'progress') {
      const pct = Math.min(100, Math.max(0, msg.pct));
      $('#fill').style.width = pct + '%';
      $('#fill').classList.toggle('lit', 320 * pct / 100 >= 60);
      $('#pct').textContent = Math.floor(pct) + '%';
      $('#track').setAttribute('aria-valuenow', Math.floor(pct));
      $('#step').textContent = msg.step;
      $('#detail').textContent = msg.detail || '';
    } else if (msg.kind === 'done') {
      busy = false;
      $('#doneTitle').textContent = APP.name + ' is ready';
      show('done');
    } else if (msg.kind === 'error') {
      busy = false; closing = false;
      $('#win').classList.remove('closing');
      $('#errorText').textContent = msg.message;
      show('error');
    } else if (msg.kind === 'launchError') {
      closing = false;
      $('#win').classList.remove('closing');
      $('#doneLead').textContent = msg.message;
    } else if (msg.kind === 'close') close(false);
  };
  document.addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.hasAttribute('data-install') && !busy) {
      busy = true;
      $('#fill').style.width = '0%'; $('#fill').classList.remove('lit');
      $('#pct').textContent = '0%'; $('#step').textContent = 'Preparing'; $('#detail').textContent = '';
      show('install');
      send('install', { folder: $('#folder').value,
        desktop: $('[data-opt="Desktop"]').classList.contains('active'),
        startMenu: $('[data-opt="Start menu"]').classList.contains('active') });
    } else if (b.hasAttribute('data-opts')) {
      const open = $('#s-welcome').classList.toggle('open');
      $('#s-welcome .opts').inert = !open;
      b.setAttribute('aria-expanded', String(open));
    } else if (b.hasAttribute('data-opt')) {
      b.setAttribute('aria-checked', String(b.classList.toggle('active')));
    } else if (b.hasAttribute('data-close')) close(b.dataset.close === 'launch');
    else if (b.id === 'browse') send('browse');
    else if (b.id === 'minimise') send('minimise');
    else if (b.id === 'retry' || b.id === 'licenceBack') show('welcome');
    else if (b.id === 'licenceButton') show('licence');
  });
  $('.tb-drag').addEventListener('mousedown', e => { if (e.button === 0) send('drag'); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(false); });
  document.addEventListener('contextmenu', e => e.preventDefault());
  send('ready');
})();
