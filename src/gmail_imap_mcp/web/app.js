/* Gmail 本地客户端 —— 原生 JS，无框架无外链 */
(() => {
  'use strict';

  const TOKEN = document.querySelector('meta[name="app-token"]').content;

  const $ = (id) => document.getElementById(id);
  const el = {
    setup: $('setup'), setupAddress: $('setupAddress'), setupPassword: $('setupPassword'),
    setupSave: $('setupSave'), setupMsg: $('setupMsg'),
    accountLabel: $('accountLabel'), composeBtn: $('composeBtn'), folders: $('folders'),
    reconnectBtn: $('reconnectBtn'), settingsBtn: $('settingsBtn'),
    searchInput: $('searchInput'), searchBtn: $('searchBtn'), searchClear: $('searchClear'),
    chips: $('chips'), listTitle: $('listTitle'), listCount: $('listCount'),
    messageList: $('messageList'), listEmpty: $('listEmpty'), moreBtn: $('moreBtn'),
    reader: $('reader'), readerEmpty: $('readerEmpty'), readerBody: $('readerBody'),
    statusDot: $('statusDot'), statusText: $('statusText'), statusExtra: $('statusExtra'),
    composePanel: $('composePanel'), composeTitle: $('composeTitle'),
    composeClose: $('composeClose'), composeMin: $('composeMin'),
    cTo: $('cTo'), cCc: $('cCc'), cBcc: $('cBcc'), cSubject: $('cSubject'), cBody: $('cBody'),
    cHtml: $('cHtml'), attachBtn: $('attachBtn'), attachInput: $('attachInput'),
    cAttachList: $('cAttachList'), sendBtn: $('sendBtn'), toast: $('toast'),
    fabCompose: $('fabCompose'),
  };

  const state = {
    status: {}, labels: [], messages: [], selected: null,
    query: 'in:inbox', limit: 30, activeFolder: 'in:inbox',
    attachments: [], replyToUid: null, replyToMailbox: null,
    mailboxRaw: null, busy: false,
  };

  /* ------------------------------------------------------------ 基础 */

  async function api(path, { method = 'GET', body = null } = {}) {
    const res = await fetch(path, {
      method,
      headers: Object.assign(
        { 'X-App-Token': TOKEN },
        body ? { 'Content-Type': 'application/json' } : {}
      ),
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (_) { /* 非 JSON */ }
    if (!res.ok || (data && data.ok === false)) {
      throw new Error((data && data.error) || `HTTP ${res.status}`);
    }
    return data || {};
  }

  let toastTimer = null;
  function toast(msg, kind = '') {
    el.toast.textContent = msg;
    el.toast.className = 'toast show ' + kind;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.toast.className = 'toast ' + kind; }, 3200);
  }

  function setStatus(kind, text, extra = '') {
    el.statusDot.className = 'dot ' + kind;
    el.statusText.textContent = text;
    el.statusExtra.textContent = extra;
  }

  function setBusy(on) {
    state.busy = on;
    el.searchBtn.disabled = on;
    if (on) setStatus('busy', '读取中…');
  }

  function esc(s) { return String(s == null ? '' : s); }

  function splitAddrs(text) {
    return String(text || '').split(/[,;\n]/).map(s => s.trim()).filter(Boolean);
  }

  function senderName(from) {
    if (!from) return '(未知发件人)';
    const m = from.match(/^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$/);
    if (m) return (m[1] || m[2]).trim() || m[2];
    return from.trim();
  }

  function senderAddr(from) {
    if (!from) return '';
    const m = from.match(/<([^>]+)>/);
    return (m ? m[1] : from).trim();
  }

  function fmtDate(raw) {
    if (!raw) return '';
    const d = new Date(raw);
    if (isNaN(d.getTime())) return raw.slice(0, 24);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    if (sameDay) return d.toTimeString().slice(0, 5);
    if (d.getFullYear() === now.getFullYear()) {
      return `${d.getMonth() + 1}月${d.getDate()}日`;
    }
    return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`;
  }

  function fmtSize(n) {
    if (!n && n !== 0) return '';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
    return `${(n / 1048576).toFixed(1)} MB`;
  }

  const SYSTEM_LABELS = new Set([
    '\\Inbox', '\\Sent', '\\Draft', '\\Drafts', '\\Spam', '\\Trash',
    '\\Starred', '\\Important', '\\All', '\\Flagged', '\\Junk',
  ]);

  /* ------------------------------------------------------------ 初始化 */

  async function init() {
    try {
      const st = await api('/api/status');
      state.status = st;
      const credPath = $('credPath');
      if (credPath && st.config_path) credPath.textContent = st.config_path;
      if (st.read_only) {
        el.sendBtn.disabled = true;
        el.sendBtn.title = '只读模式（GMAIL_MCP_READ_ONLY=1）';
      }
      if (!st.configured) {
        showSetup();
        setStatus('err', '未配置账号');
        return;
      }
      hideSetup();
      el.accountLabel.textContent = st.address || '';
      setStatus(st.connected ? 'ok' : 'err',
        st.connected ? '已连接' : '连不上',
        st.address || '');
      await Promise.all([loadFolders(), loadMessages()]);
    } catch (err) {
      setStatus('err', '初始化失败：' + err.message);
      toast(err.message, 'err');
    }
  }

  function showSetup() {
    el.setup.classList.remove('hidden');
    el.setupAddress.value = state.status.address || '';
    setTimeout(() => el.setupAddress.focus(), 60);
  }
  function hideSetup() { el.setup.classList.add('hidden'); }

  el.setupSave.addEventListener('click', async () => {
    const address = el.setupAddress.value.trim();
    const app_password = el.setupPassword.value.trim();
    if (!address || !app_password) {
      el.setupMsg.className = 'setup-msg err';
      el.setupMsg.textContent = '两个都得填';
      return;
    }
    el.setupSave.disabled = true;
    el.setupSave.textContent = '验证中…';
    el.setupMsg.className = 'setup-msg';
    el.setupMsg.textContent = '正在连 Google，可能要几秒…';
    try {
      const st = await api('/api/config', { method: 'POST', body: { address, app_password } });
      state.status = st;
      el.setupMsg.className = 'setup-msg ok';
      el.setupMsg.textContent = '连上了，正在载入…';
      setTimeout(async () => {
        hideSetup();
        el.accountLabel.textContent = st.address || address;
        setStatus('ok', '已连接', st.address || address);
        await Promise.all([loadFolders(), loadMessages()]);
      }, 500);
    } catch (err) {
      el.setupMsg.className = 'setup-msg err';
      el.setupMsg.textContent = err.message;
    } finally {
      el.setupSave.disabled = false;
      el.setupSave.textContent = '保存并测试';
    }
  });

  el.settingsBtn.addEventListener('click', showSetup);

  el.reconnectBtn.addEventListener('click', async () => {
    setStatus('busy', '重连中…');
    try {
      const st = await api('/api/reconnect', { method: 'POST', body: {} });
      state.status = st;
      setStatus(st.connected ? 'ok' : 'err', st.connected ? '已连接' : '连不上', st.address || '');
      if (st.connected) await loadMessages();
    } catch (err) {
      setStatus('err', '重连失败');
      toast(err.message, 'err');
    }
  });

  /* ------------------------------------------------------------ 文件夹 */

  // Gmail 的 LIST 返回 \Flagged，X-GM-LABELS 返回 \Starred，两个都得认
  function isStarFolder(flags) {
    return flags.includes('\\Starred') || flags.includes('\\Flagged');
  }

  function folderIcon(name, flags) {
    if (flags.includes('\\All')) return '🗂';
    if (isStarFolder(flags)) return '★';
    if (flags.includes('\\Sent')) return '↗';
    if (flags.includes('\\Drafts') || flags.includes('\\Draft')) return '✎';
    if (flags.includes('\\Spam') || flags.includes('\\Junk')) return '⚠';
    if (flags.includes('\\Trash')) return '🗑';
    if (name === 'INBOX') return '📥';
    return '🏷';
  }

  function folderQuery(name, flags) {
    if (flags.includes('\\All')) return 'in:anywhere';
    if (isStarFolder(flags)) return 'is:starred';
    if (flags.includes('\\Sent')) return 'in:sent';
    if (flags.includes('\\Drafts') || flags.includes('\\Draft')) return 'in:drafts';
    if (flags.includes('\\Spam') || flags.includes('\\Junk')) return 'in:spam';
    if (flags.includes('\\Trash')) return 'in:trash';
    if (flags.includes('\\Important')) return 'is:important';
    if (name === 'INBOX') return 'in:inbox';
    return `label:"${name.replace(/"/g, '\\"')}"`;
  }

  function folderLabel(name, flags) {
    if (flags.includes('\\All')) return '所有邮件';
    if (isStarFolder(flags)) return '已加星标';
    if (flags.includes('\\Sent')) return '已发送';
    if (flags.includes('\\Drafts') || flags.includes('\\Draft')) return '草稿';
    if (flags.includes('\\Spam') || flags.includes('\\Junk')) return '垃圾邮件';
    if (flags.includes('\\Trash')) return '回收站';
    if (flags.includes('\\Important')) return '重要';
    if (name === 'INBOX') return '收件箱';
    return name;
  }

  async function loadFolders() {
    try {
      const data = await api('/api/labels');
      state.labels = data.labels || [];
    } catch (err) {
      toast('标签加载失败：' + err.message, 'err');
      return;
    }
    el.folders.textContent = '';
    for (const item of state.labels) {
      const btn = document.createElement('button');
      btn.className = 'folder' + (folderQuery(item.name, item.flags) === state.activeFolder ? ' active' : '');
      const ico = document.createElement('span');
      ico.className = 'ico';
      ico.textContent = folderIcon(item.name, item.flags);
      const txt = document.createElement('span');
      txt.textContent = folderLabel(item.name, item.flags);
      btn.append(ico, txt);
      btn.addEventListener('click', () => {
        state.activeFolder = folderQuery(item.name, item.flags);
        state.query = state.activeFolder;
        state.limit = 30;
        el.searchInput.value = '';
        el.searchClear.classList.add('hidden');
        setActiveChip(null);
        document.querySelectorAll('.folder').forEach(n => n.classList.remove('active'));
        btn.classList.add('active');
        el.listTitle.textContent = folderLabel(item.name, item.flags);
        loadMessages();
      });
      el.folders.appendChild(btn);
    }
  }

  /* ------------------------------------------------------------ 搜索 */

  function setActiveChip(chip) {
    el.chips.querySelectorAll('.chip').forEach(c => c.classList.toggle('active', c === chip));
  }

  el.chips.addEventListener('click', (ev) => {
    const chip = ev.target.closest('.chip');
    if (!chip) return;
    setActiveChip(chip);
    state.query = chip.dataset.q;
    state.activeFolder = chip.dataset.q;
    state.limit = 30;
    el.searchInput.value = '';
    el.searchClear.classList.add('hidden');
    el.listTitle.textContent = chip.textContent;
    loadMessages();
  });

  function doSearch() {
    const q = el.searchInput.value.trim();
    state.query = q || 'in:inbox';
    state.limit = 30;
    setActiveChip(null);
    el.listTitle.textContent = q ? `搜索：${q}` : '收件箱';
    el.searchClear.classList.toggle('hidden', !q);
    loadMessages();
  }

  el.searchBtn.addEventListener('click', doSearch);
  el.searchInput.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); doSearch(); }
  });
  el.searchClear.addEventListener('click', () => {
    el.searchInput.value = '';
    el.searchClear.classList.add('hidden');
    doSearch();
    el.searchInput.focus();
  });

  /* ------------------------------------------------------------ 列表 */

  function skeleton() {
    el.messageList.textContent = '';
    for (let i = 0; i < 6; i++) {
      const li = document.createElement('li');
      li.className = 'skeleton';
      el.messageList.appendChild(li);
    }
  }

  async function loadMessages({ append = false } = {}) {
    setBusy(true);
    if (!append) skeleton();
    const limit = append ? state.limit + 30 : state.limit;
    try {
      const data = await api(
        `/api/messages?q=${encodeURIComponent(state.query)}&limit=${limit}`
      );
      state.messages = data.messages || [];
      state.mailboxRaw = data.mailbox_raw || null;
      state.limit = limit;
      renderMessages();
      const st = state.status;
      setStatus(st.connected ? 'ok' : 'err',
        `${state.messages.length} 封`,
        st.address || '');
      el.statusExtra.textContent = `「${state.query}」 · ${state.messages.length} 封`;
    } catch (err) {
      el.messageList.textContent = '';
      el.listEmpty.classList.remove('hidden');
      el.listEmpty.textContent = err.message;
      setStatus('err', '读取失败');
      toast(err.message, 'err');
    } finally {
      setBusy(false);
    }
  }

  function renderMessages() {
    el.messageList.textContent = '';
    el.listEmpty.classList.toggle('hidden', state.messages.length > 0);
    el.listCount.textContent = `${state.messages.length} 封`;

    for (const m of state.messages) {
      const li = document.createElement('li');
      li.className = 'msg' + (m.unread ? ' unread' : '') + (m.uid === state.selected ? ' active' : '');
      li.dataset.uid = m.uid;

      const dot = document.createElement('span'); dot.className = 'dotmark';
      const who = document.createElement('span'); who.className = 'who';
      who.textContent = senderName(m.from);
      who.title = m.from || '';
      const when = document.createElement('span'); when.className = 'when';
      when.textContent = fmtDate(m.date);
      const subj = document.createElement('span'); subj.className = 'subj';
      subj.textContent = m.subject || '(无主题)';

      const meta = document.createElement('span'); meta.className = 'meta';
      const labels = (m.labels || []).filter(l => !SYSTEM_LABELS.has(l));
      if ((m.labels || []).includes('\\Starred')) {
        const t = document.createElement('span'); t.className = 'tag star'; t.textContent = '★';
        meta.appendChild(t);
      }
      for (const l of labels.slice(0, 3)) {
        const t = document.createElement('span'); t.className = 'tag'; t.textContent = l;
        meta.appendChild(t);
      }
      if (m.size && m.size > 1024 * 1024) {
        const t = document.createElement('span'); t.className = 'tag att'; t.textContent = fmtSize(m.size);
        meta.appendChild(t);
      }

      li.append(dot, who, when, subj, meta);
      li.addEventListener('click', () => openMessage(m.uid, li));
      el.messageList.appendChild(li);
    }

    el.moreBtn.classList.toggle('hidden', state.messages.length < state.limit || state.limit >= 150);
  }

  el.moreBtn.addEventListener('click', () => loadMessages({ append: true }));

  /* ------------------------------------------------------------ 阅读 */

  function renderReader(m) {
    el.readerEmpty.classList.add('hidden');
    el.readerBody.classList.remove('hidden');
    el.readerBody.textContent = '';

    const head = document.createElement('div');
    head.className = 'rd-head';

    const back = document.createElement('button');
    back.className = 'ghost rd-back';
    back.textContent = '← 返回列表';
    back.addEventListener('click', () => document.body.classList.remove('reading'));
    back.style.display = 'none';
    head.appendChild(back);

    const h = document.createElement('h1');
    h.className = 'rd-subject';
    h.textContent = m.subject || '(无主题)';
    head.appendChild(h);

    const dl = document.createElement('dl');
    dl.className = 'rd-meta';
    const rows = [
      ['发件人', m.from], ['收件人', m.to],
      ['抄送', m.cc], ['时间', m.date],
    ];
    for (const [k, v] of rows) {
      if (!v) continue;
      const dt = document.createElement('dt'); dt.textContent = k;
      const dd = document.createElement('dd'); dd.textContent = v;
      dl.append(dt, dd);
    }
    head.appendChild(dl);

    const actions = document.createElement('div');
    actions.className = 'rd-actions';
    const mk = (label, fn) => {
      const b = document.createElement('button');
      b.textContent = label;
      b.addEventListener('click', fn);
      actions.appendChild(b);
    };
    mk('↩ 回复', () => reply(m, false));
    mk('↩ 回复全部', () => reply(m, true));
    head.appendChild(actions);

    el.readerBody.appendChild(head);

    if (m.html) {
      const frame = document.createElement('iframe');
      frame.className = 'body-frame';
      frame.setAttribute('sandbox', '');           // 禁脚本
      frame.setAttribute('referrerpolicy', 'no-referrer');
      const csp = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:";
      frame.srcdoc = `<!doctype html><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<style>html,body{margin:0;padding:0}body{padding:16px;font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:#1a1a1a;background:#fff;word-break:break-word}img{max-width:100%;height:auto}a{color:#1a73e8}blockquote{margin:0 0 0 8px;padding-left:12px;border-left:2px solid #dadce0;color:#5f6368}table{max-width:100%}</style>
${m.html}`;
      frame.style.height = 'min(62vh, 720px)';
      frame.style.minHeight = '320px';
      el.readerBody.appendChild(frame);
    } else {
      const pre = document.createElement('div');
      pre.className = 'body-text';
      pre.textContent = m.text || '(没有可读正文)';
      el.readerBody.appendChild(pre);
    }

    if (m.attachments && m.attachments.length) {
      const wrap = document.createElement('div');
      wrap.className = 'attachments';
      for (const a of m.attachments) {
        const link = document.createElement('a');
        link.className = 'att-chip';
        const box = m.mailbox_raw ? `&mailbox=${encodeURIComponent(m.mailbox_raw)}` : '';
        link.href = `/api/attachment?uid=${encodeURIComponent(m.uid)}&idx=${a.idx}${box}&t=${encodeURIComponent(TOKEN)}`;
        link.setAttribute('download', a.name);
        const name = document.createElement('span');
        name.textContent = '📎 ' + a.name;
        const size = document.createElement('span');
        size.className = 'size';
        size.textContent = fmtSize(a.size);
        link.append(name, size);
        wrap.appendChild(link);
      }
      const title = document.createElement('div');
      title.style.cssText = 'margin-top:18px;color:var(--muted);font-size:12px';
      title.textContent = `附件（${m.attachments.length}）`;
      el.readerBody.appendChild(title);
      el.readerBody.appendChild(wrap);
    }

    el.reader.scrollTop = 0;
  }

  async function openMessage(uid, li) {
    state.selected = uid;
    document.querySelectorAll('.msg').forEach(n => n.classList.toggle('active', n === li));
    document.body.classList.add('reading');
    el.readerEmpty.classList.remove('hidden');
    el.readerEmpty.querySelector('p').textContent = '读取中…';
    el.readerBody.classList.add('hidden');
    try {
      const boxArg = state.mailboxRaw
        ? `&mailbox=${encodeURIComponent(state.mailboxRaw)}` : '';
      const m = await api(`/api/message?uid=${encodeURIComponent(uid)}${boxArg}`);
      renderReader(m);
      const backBtn = el.readerBody.querySelector('.rd-back');
      if (backBtn) backBtn.style.display = window.innerWidth <= 860 ? 'inline-block' : 'none';
    } catch (err) {
      el.readerEmpty.querySelector('p').textContent = err.message;
      toast(err.message, 'err');
    }
  }

  /* ------------------------------------------------------------ 写邮件 */

  function openCompose(prefill = {}) {
    state.replyToUid = prefill.replyToUid || null;
    state.replyToMailbox = prefill.replyToMailbox || null;
    state.attachments = [];
    renderComposeAttachments();
    el.cTo.value = prefill.to || '';
    el.cCc.value = prefill.cc || '';
    el.cBcc.value = prefill.bcc || '';
    el.cSubject.value = prefill.subject || '';
    el.cBody.value = prefill.body || '';
    el.cHtml.checked = false;
    el.composeTitle.textContent = prefill.replyToUid ? '回复' : '新邮件';
    el.composePanel.classList.remove('hidden');
    el.composePanel.style.display = '';
    setTimeout(() => (el.cTo.value ? el.cBody : el.cTo).focus(), 60);
  }

  function closeCompose() {
    el.composePanel.classList.add('hidden');
    state.attachments = [];
    state.replyToUid = null;
    state.replyToMailbox = null;
  }

  function reply(m, all) {
    const to = senderAddr(m.from);
    let cc = '';
    if (all) {
      const extra = String(m.to || '').split(',').map(s => senderAddr(s))
        .filter(a => a && a !== to && a !== state.status.address);
      cc = extra.join(', ');
    }
    const subject = /^re:/i.test(m.subject || '') ? m.subject : `Re: ${m.subject || ''}`;
    const quoted = [
      '', '',
      '-------- 原始邮件 --------',
      `发件人: ${m.from || ''}`,
      `时间: ${m.date || ''}`,
      `主题: ${m.subject || ''}`,
      '',
      (m.text || '').split('\n').map(line => '> ' + line).join('\n'),
    ].join('\n');
    openCompose({
      to, cc, subject, body: quoted, replyToUid: m.uid,
      replyToMailbox: m.mailbox_raw || state.mailboxRaw,
    });
  }

  el.composeBtn.addEventListener('click', () => openCompose());
  el.fabCompose.addEventListener('click', () => openCompose());
  el.composeClose.addEventListener('click', closeCompose);
  el.composeMin.addEventListener('click', () => {
    el.composePanel.classList.add('hidden');
  });

  el.attachBtn.addEventListener('click', () => el.attachInput.click());

  function readAsBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const s = String(reader.result || '');
        resolve(s.slice(s.indexOf(',') + 1));
      };
      reader.onerror = () => reject(new Error('读文件失败'));
      reader.readAsDataURL(file);
    });
  }

  el.attachInput.addEventListener('change', async () => {
    const files = Array.from(el.attachInput.files || []);
    el.attachInput.value = '';
    for (const f of files) {
      if (f.size > 15 * 1024 * 1024) {
        toast(`${f.name} 超过 15MB，Gmail 不收`, 'err');
        continue;
      }
      const total = state.attachments.reduce((s, a) => s + (a.size || 0), 0);
      if (total + f.size > 22 * 1024 * 1024) {
        toast('附件总量超过 Gmail 的 25MB 上限', 'err');
        break;
      }
      try {
        const data = await readAsBase64(f);
        state.attachments.push({ name: f.name, size: f.size, data });
      } catch (err) {
        toast(err.message, 'err');
      }
    }
    renderComposeAttachments();
  });

  function renderComposeAttachments() {
    el.cAttachList.textContent = '';
    state.attachments.forEach((a, i) => {
      const chip = document.createElement('span');
      chip.className = 'att-chip';
      const name = document.createElement('span');
      name.textContent = '📎 ' + a.name;
      const size = document.createElement('span');
      size.className = 'size';
      size.textContent = fmtSize(a.size);
      const rm = document.createElement('button');
      rm.className = 'rm icon-btn';
      rm.textContent = '×';
      rm.title = '移除';
      rm.addEventListener('click', () => {
        state.attachments.splice(i, 1);
        renderComposeAttachments();
      });
      chip.append(name, size, rm);
      el.cAttachList.appendChild(chip);
    });
  }

  el.sendBtn.addEventListener('click', async () => {
    if (state.status.read_only) {
      toast('只读模式：服务器设置了 GMAIL_MCP_READ_ONLY，不能发信', 'err');
      return;
    }
    const to = splitAddrs(el.cTo.value);
    if (!to.length) {
      toast('收件人还没填', 'err');
      el.cTo.focus();
      return;
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(to[0])) {
      toast('收件人地址格式看着不对', 'err');
      el.cTo.focus();
      return;
    }
    el.sendBtn.disabled = true;
    el.sendBtn.textContent = '发送中…';
    try {
      const res = await api('/api/send', {
        method: 'POST',
        body: {
          to,
          cc: splitAddrs(el.cCc.value),
          bcc: splitAddrs(el.cBcc.value),
          subject: el.cSubject.value,
          body: el.cBody.value,
          html: el.cHtml.checked,
          attachments: state.attachments.map(a => ({ name: a.name, data: a.data })),
          reply_to_uid: state.replyToUid,
          reply_to_mailbox: state.replyToMailbox,
        },
      });
      closeCompose();
      toast(`发出去了 → ${to.join(', ')}`, 'ok');
      if (res.message_id) el.statusExtra.textContent = `Message-ID ${res.message_id.slice(0, 24)}…`;
      setTimeout(() => loadMessages(), 900);
    } catch (err) {
      toast('发送失败：' + err.message, 'err');
    } finally {
      el.sendBtn.disabled = !!state.status.read_only;
      el.sendBtn.textContent = '发送';
    }
  });

  /* ------------------------------------------------------------ 快捷键 */

  document.addEventListener('keydown', (ev) => {
    const typing = /^(INPUT|TEXTAREA)$/.test(ev.target.tagName) || ev.target.isContentEditable;
    if (ev.key === 'Escape') {
      if (!el.composePanel.classList.contains('hidden')) closeCompose();
      else document.body.classList.remove('reading');
      return;
    }
    if (!typing && (ev.key === 'c' || ev.key === 'C') && !ev.ctrlKey && !ev.metaKey) {
      ev.preventDefault();
      openCompose();
    }
  });

  /* ------------------------------------------------------------ 启动 */

  init();
})();
