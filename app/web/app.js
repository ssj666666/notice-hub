/* 通知中枢 · 看板前端（无构建步骤，原生 JS） */
'use strict';

const $ = (sel) => document.querySelector(sel);

const state = {
  view: 'all',
  q: '',
  campus: '',
  category: '',
  sort: 'importance',
  sortManual: false,
  includeStale: false,
  items: [],
  categories: [],
  campuses: [],
  sources: [],
  current: null,
  stats: null,
};

const VIEWS = {
  key:     { min_importance: 4, unread_only: false, starred_only: false },
  unread:  { min_importance: 1, unread_only: true,  starred_only: false },
  starred: { min_importance: 1, unread_only: false, starred_only: true  },
  all:     { min_importance: 1, unread_only: false, starred_only: false },
};

const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

function toast(msg, ms = 2600) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove('show'), ms);
}

async function api(path, options) {
  const resp = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, options));
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).error || detail; } catch (e) { /* ignore */ }
    throw new Error(detail);
  }
  return resp.json();
}

/* ---------------- 时间显示 ---------------- */
function relTime(iso) {
  if (!iso) return '';
  const t = new Date(iso);
  if (isNaN(t)) return '';
  const diff = (Date.now() - t.getTime()) / 1000;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
  if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
  if (diff < 86400 * 7) return Math.floor(diff / 86400) + ' 天前';
  return `${t.getMonth() + 1}月${t.getDate()}日`;
}

function daysUntil(dateStr) {
  if (!dateStr) return null;
  const d = new Date(dateStr + 'T23:59:59');
  if (isNaN(d)) return null;
  return Math.ceil((d.getTime() - Date.now()) / 86400000);
}

function daysAgo(dateStr) {
  if (!dateStr) return null;
  const d = new Date(dateStr + 'T00:00:00');
  if (isNaN(d)) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((today.getTime() - d.getTime()) / 86400000);
}

/* ---------------- 加载 ---------------- */
async function loadStats() {
  try {
    state.stats = await api('/api/stats');
    renderStats();
    renderToolbar();
  } catch (e) {
    $('#subtitle').textContent = '无法连接后端';
  }
}

function renderStats() {
  const s = state.stats;
  if (!s) return;
  const srcCount = (s.sources || []).length;
  const last = s.last_run;
  const lastText = last && last.finished_at
    ? `最近抓取 ${relTime(last.finished_at)}`
    : '尚未抓取过';
  $('#subtitle').textContent =
    `${srcCount} 个源 · ${lastText}` + (s.llm_enabled ? '' : ' · LLM 未启用');

  $('#stats').innerHTML = `
    <div class="stat ${s.high_unread > 0 ? 'hot' : ''}">
      <b>${s.high_unread}</b><span>未读重点</span>
    </div>
    <div class="stat"><b>${s.unread}</b><span>未读总数</span></div>
    <div class="stat"><b>${s.today}</b><span>今日新增</span></div>`;
}

async function loadItems() {
  const v = VIEWS[state.view] || VIEWS.key;
  const params = new URLSearchParams({
    limit: '200',
    min_importance: String(v.min_importance),
    unread_only: String(v.unread_only),
    starred_only: String(v.starred_only),
    include_stale: String(state.includeStale),
    sort: state.sort,
  });
  if (state.q) params.set('q', state.q);
  if (state.campus) params.set('campus', state.campus);
  if (state.category) params.set('category', state.category);
  try {
    const data = await api('/api/items?' + params.toString());
    state.items = data.items || [];
    renderList();
  } catch (e) {
    $('#list').innerHTML = `<div class="empty"><h3>读取失败</h3><p>${esc(e.message)}</p></div>`;
  }
  renderToolbar();
}

/* ---------------- 排序规则 ----------------
   全部通知 → 按重要程度；选进具体类目 → 按时间。
   用户手动切过之后就不再自动改。 */
function applyAutoSort() {
  if (state.sortManual) return;
  state.sort = state.category ? 'time' : 'importance';
}

function renderToolbar() {
  const sortBtn = $('#btn-sort');
  sortBtn.textContent = state.sort === 'time' ? '⇅ 按时间' : '⇅ 按重要度';
  sortBtn.classList.toggle('on', state.sortManual);
  sortBtn.title = state.sortManual
    ? '已手动指定排序，点一下恢复自动（全部按重要度 / 类目按时间）'
    : '当前自动：全部通知按重要度，具体类目按时间';

  const staleBtn = $('#btn-stale');
  staleBtn.textContent = state.includeStale ? '过期：显示' : '过期：隐藏';
  staleBtn.classList.toggle('on', state.includeStale);

  const s = state.stats || {};
  const bits = [];
  if (s.published_today) bits.push(`今日发布 ${s.published_today} 条`);
  if (s.stale) bits.push(`已隐藏过期 ${s.stale} 条`);
  $('#toolbar-hint').textContent = bits.join(' · ');
}

/* ---------------- 一级分类：校区 ---------------- */
async function loadCampuses() {
  const v = VIEWS[state.view] || VIEWS.key;
  const params = new URLSearchParams({
    min_importance: String(v.min_importance),
    unread_only: String(v.unread_only),
    include_stale: String(state.includeStale),
  });
  try {
    const data = await api('/api/campuses?' + params.toString());
    state.campuses = (data.campuses || []).filter((c) => c.count > 0);
  } catch (e) {
    state.campuses = [];
  }
  renderCampuses();
}

function renderCampuses() {
  const el = $('#campuses');
  if (!state.campuses.length) {
    el.innerHTML = '';
    return;
  }
  const total = state.campuses.reduce((sum, c) => sum + c.count, 0);
  const all = `<button class="campus ${state.campus === '' ? 'active' : ''}" data-campus="">
      <span class="ci">📋</span>
      <span class="cn">全部</span>
      <span class="cc">${total} 条</span>
    </button>`;
  const rest = state.campuses.map((c) => `
    <button class="campus ${state.campus === c.key ? 'active' : ''}"
            data-campus="${esc(c.key)}" title="${esc(c.desc)}">
      <span class="ci">${c.icon}</span>
      <span class="cn">${esc(c.key)}</span>
      <span class="cc">${c.count} 条${c.unread ? ` · 未读 ${c.unread}` : ''}</span>
    </button>`).join('');
  el.innerHTML = all + rest;
}

/* ---------------- 分类 ---------------- */
async function loadCategories() {
  const v = VIEWS[state.view] || VIEWS.key;
  const params = new URLSearchParams({
    min_importance: String(v.min_importance),
    unread_only: String(v.unread_only),
    include_stale: String(state.includeStale),
  });
  if (state.campus) params.set('campus', state.campus);
  try {
    const data = await api('/api/categories?' + params.toString());
    state.categories = (data.categories || []).filter((c) => c.count > 0);
    renderCats();
  } catch (e) {
    state.categories = [];
    renderCats();
  }
}

function renderCats() {
  const el = $('#cats');
  if (!state.categories.length) {
    el.innerHTML = '';
    return;
  }
  const total = state.categories.reduce((sum, c) => sum + c.count, 0);
  const all = `<button class="cat ${state.category === '' ? 'active' : ''}" data-cat="">
      <span>全部分类</span><span class="n">${total}</span></button>`;
  const rest = state.categories.map((c) => `
    <button class="cat ${state.category === c.key ? 'active' : ''}" data-cat="${esc(c.key)}"
            title="${esc(c.desc)}">
      <span>${c.icon} ${esc(c.key)}</span>
      <span class="n ${c.unread > 0 ? 'hot' : ''}">${c.unread > 0 ? c.unread : c.count}</span>
    </button>`).join('');
  el.innerHTML = all + rest;
}

/* ---------------- 渲染 ---------------- */
function renderList() {
  const list = $('#list');
  if (!state.items.length) {
    if (state.stats && (state.stats.sources || []).length === 0) {
      list.innerHTML = `<div class="empty">
        <h3>还没有配置信息源</h3>
        <p>打开项目目录里的 <code>config.yaml</code>，把源填进去：</p>
        <ol>
          <li>把 <code>sources.imap</code> 填上校内邮箱和授权码（最可靠）</li>
          <li>把 <code>sources.rss</code> 填上公众号 RSS 地址</li>
          <li><code>sources.web</code> 已经预置了教务部通知页</li>
          <li>保存后回到这里点右上角 ⟳ 重新抓取</li>
        </ol>
      </div>`;
    } else {
      list.innerHTML = `<div class="empty"><h3>没有符合条件的通知</h3>
        <p>点右上角 ⟳ 抓取一次，或切换上面的筛选。</p></div>`;
    }
    return;
  }

  // 未选中具体分类时，按分类分组展示
  const order = state.categories.map((c) => c.key);
  const buckets = new Map();
  for (const it of state.items) {
    const key = it.category || '其他';
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(it);
  }
  const keys = [...buckets.keys()].sort((a, b) => {
    const ia = order.indexOf(a);
    const ib = order.indexOf(b);
    return (ia < 0 ? 999 : ia) - (ib < 0 ? 999 : ib);
  });

  let html = '';
  for (const key of keys) {
    const meta = state.categories.find((c) => c.key === key) || {};
    const group = buckets.get(key);
    html += `<div class="cat-group">
        <span>${meta.icon || '📌'} ${esc(key)}</span>
        <span class="line"></span>
        <span class="cnt">${group.length} 条</span>
      </div>`;
    html += group.map((it) => cardHtml(it, false)).join('');
  }
  list.innerHTML = html;
}

function cardHtml(it, showCat) {
  const d = daysUntil(it.deadline);
  const age = daysAgo(it.published_at);
  const deadlineTag = it.deadline
    ? `<span class="tag deadline">截止 ${esc(it.deadline.slice(5))}${d !== null && d >= 0 ? ` · 剩${d}天` : ''}</span>`
    : '';
  const impTag = it.importance >= 5
    ? '<span class="tag imp5">致命</span>'
    : it.importance >= 4 ? '<span class="tag imp4">重要</span>' : '';
  // 时效标签：一眼看出是不是新的
  // 注意 age 可能是负数 —— 讲座类源给的日期是「活动时间」，可能在未来
  let freshTag = '';
  if (age !== null && it.published_at) {
    if (age === 0) {
      freshTag = '<span class="tag today">今日</span>';
    } else if (age === 1) {
      freshTag = '<span class="tag today">昨天</span>';
    } else if (age > 1 && age <= 7) {
      freshTag = `<span class="tag">${age} 天前</span>`;
    } else if (age < 0) {
      freshTag = `<span class="tag cat-tag">${esc(it.published_at.slice(5))} 开讲</span>`;
    }
  }
  const starTag = it.starred ? '<span class="tag star">★</span>' : '';
  const catTag = showCat && it.category
    ? `<span class="tag cat-tag">${esc(it.category)}</span>` : '';
  // 在「全部」视图下额外标出校区，避免两个校区的内容混在一起分不清
  const campTag = state.campus === '' && it.campus
    ? `<span class="tag">${esc(it.campus)}</span>` : '';
  const staleTag = it.is_stale
    ? '<span class="tag">已过期</span>'
    : (it.is_baseline ? '<span class="tag">首次接入</span>' : '');
  const summary = it.summary || (it.content || '').slice(0, 90);

  return `<article class="card ${it.is_read ? 'read' : ''} ${it.is_stale ? 'stale' : ''}"
                   data-imp="${it.importance}" data-id="${it.id}">
      <div class="bar"></div>
      <div class="card-body">
        <h3 class="card-title">${esc(it.title)}</h3>
        ${summary ? `<p class="card-sum">${esc(summary)}</p>` : ''}
        <div class="card-meta">
          ${impTag}${freshTag}${deadlineTag}${catTag}${campTag}${starTag}${staleTag}
          <span>${esc(it.source)}</span>
          <span>·</span>
          <span>${relTime(it.published_at || it.fetched_at)}</span>
        </div>
      </div>
      ${it.is_read ? '' : '<div class="dot-unread"></div>'}
    </article>`;
}

/* ---------------- 详情 ---------------- */
function openSheet(item) {
  state.current = item;
  const d = daysUntil(item.deadline);
  const age = daysAgo(item.published_at);
  const rows = [
    ['分类', item.category],
    ['来源', item.source],
    ['事项', item.topic],
    ['摘要', item.summary],
    ['截止', item.deadline ? `${item.deadline}${d !== null && d >= 0 ? ` （剩 ${d} 天）` : ''}` : ''],
    ['面向', item.audience],
    ['要做', item.action],
    ['日期', item.published_at ? `${item.published_at}${age !== null && age < 0 ? '（未来活动）' : ''}` : ''],
    ['判定', item.analysis],
  ].filter(([, v]) => v);

  $('#sheet-body').innerHTML = `
    <h2>${esc(item.title)}</h2>
    ${rows.map(([k, v]) => `<div class="kv"><b>${k}</b><span>${esc(v)}</span></div>`).join('')}
    ${item.content ? `<div class="body-text">${esc(item.content.slice(0, 4000))}</div>` : ''}`;

  $('#sheet-open').disabled = !item.url;
  $('#sheet-star').textContent = item.starred ? '取消星标' : '星标';
  $('#sheet-read').textContent = item.is_read ? '标记未读' : '标记已读';
  $('#sheet').classList.remove('hidden');
}

function closeSheet() {
  $('#sheet').classList.add('hidden');
  state.current = null;
}

/* ---------------- 操作 ---------------- */
async function setFlag(id, flag, value) {
  await api(`/api/items/${id}/flag`, {
    method: 'POST',
    body: JSON.stringify({ flag, value }),
  });
}

async function refreshAll() {
  const btn = $('#btn-refresh');
  btn.classList.add('spin');
  btn.disabled = true;
  try {
    await loadStats();
    await loadCampuses();
    await loadCategories();
    await loadItems();
  } finally {
    btn.classList.remove('spin');
    btn.disabled = false;
  }
}

async function runNow() {
  const btn = $('#btn-refresh');
  btn.classList.add('spin');
  toast('正在抓取，请稍候…', 8000);
  try {
    const data = await api('/api/run', { method: 'POST' });
    const r = data.result || {};
    if (r.error) toast('抓取出错：' + r.error, 5000);
    else if (r.skipped) toast('上一轮还在进行中');
    else toast(`采集 ${r.collected} 条 / 新增 ${r.new} 条 / 推送 ${r.pushed} 条`, 4000);
    await refreshAll();
  } catch (e) {
    toast('失败：' + e.message, 5000);
  } finally {
    btn.classList.remove('spin');
  }
}

async function openSourcesPanel() {
  const body = $('#sources-body');
  body.innerHTML = '<h3>正在读取…</h3>';
  $('#sources-panel').classList.remove('hidden');
  let data;
  try {
    data = await api('/api/sources');
  } catch (e) {
    body.innerHTML = `<h3>读取失败</h3><p>${esc(e.message)}</p>`;
    return;
  }
  const badge = (s) => {
    const color = s === '活跃' ? '#16a34a'
      : (s === '停更' ? '#a16207' : (s === '无数据' ? '#e5484d' : '#6b7280'));
    return `<span style="color:${color};font-weight:650">${esc(s)}</span>`;
  };
  const rows = (data.sources || []).map((s) => `
    <div class="kv" style="align-items:flex-start">
      <b style="width:auto;min-width:96px">${badge(s.status)}</b>
      <span>
        <b style="color:var(--text)">${esc(s.name)}</b>
        <span class="tag" style="margin-left:6px">${esc(s.type)}</span><br>
        <span style="color:var(--muted)">
          抓到 ${s.total} 条 · 可见 ${s.visible} 条 · 丢弃 ${s.dropped} 条<br>
          最新一条：${s.newest ? esc(s.newest) : '（无日期）'}
          ${s.newest_age_days !== null && s.newest_age_days !== undefined
            ? `（${s.newest_age_days} 天前）` : ''}<br>
          ${s.note ? `⚠️ ${esc(s.note)}<br>` : ''}
        </span>
      </span>
    </div>`).join('');

  const disabled = (data.disabled || []).map((s) => `
    <div class="kv"><b style="width:auto;min-width:96px">已关闭</b>
      <span>${esc(s.name)}<br><span style="color:var(--muted)">${esc(s.url)}</span></span>
    </div>`).join('');

  body.innerHTML = `
    <h2>各源抓取状态</h2>
    <p style="color:var(--muted);font-size:13px;margin-top:0">
      共 ${(data.sources || []).length} 个启用中的源。这里能看到每个站的真实情况——
      如果某个站没内容，先看这里是不是它自己停更了。
    </p>
    ${rows || '<p>没有启用的源</p>'}
    ${disabled ? `<h3 style="margin-top:18px">已关闭的源</h3>${disabled}` : ''}`;
}
/* ---------------- 添加网站 ---------------- */
let lastProbe = null;

async function openAddPanel() {
  $('#add-result').innerHTML = '';
  $('#add-url').value = '';
  lastProbe = null;
  $('#add-panel').classList.remove('hidden');
  await loadCustomList();
}

async function loadCustomList() {
  const box = $('#add-custom-list');
  try {
    const { sources } = await api('/api/custom-sources');
    if (!sources || !sources.length) {
      box.innerHTML = '<p class="hint" style="margin-top:16px">还没有通过界面添加过网站。</p>';
      return;
    }
    box.innerHTML = '<h4 style="font-size:13.5px;margin:16px 0 0">已添加的网站</h4>' +
      sources.map((s) => `<div class="custom-item">
          <span title="${esc(s.url)}">${esc(s.name)}</span>
          <button data-del="${esc(s.name)}">删除</button>
        </div>`).join('');
  } catch (e) {
    box.innerHTML = '';
  }
}

async function probeUrl(url) {
  const box = $('#add-result');
  box.innerHTML = '<div class="probe-box"><p>正在探测，请稍候…</p></div>';
  let d;
  try {
    d = await api('/api/discover', { method: 'POST', body: JSON.stringify({ url }) });
  } catch (e) {
    box.innerHTML = `<div class="probe-box warn"><h4>探测失败</h4><p>${esc(e.message)}</p></div>`;
    return;
  }
  lastProbe = d;

  if (!d.ok) {
    box.innerHTML = `<div class="probe-box warn"><h4>做不到</h4><p>${esc(d.message)}</p></div>`;
    return;
  }

  const kindLabel = {
    rss: 'RSS 订阅源', list: '静态列表页', js: 'JS 动态加载页', unknown: '结构未识别',
  }[d.kind] || d.kind;
  const cls = (d.kind === 'list' || d.kind === 'rss') ? 'ok' : 'warn';

  let html = `<div class="probe-box ${cls}">
    <h4>判断结果：${esc(kindLabel)}</h4>
    <p>${esc(d.message)}</p>`;

  if (d.stats && d.stats.items !== undefined) {
    html += `<p>识别到 ${d.stats.items} 项`
      + (d.stats.dated ? `，其中约 ${d.stats.dated} 项带日期` : '') + `。</p>`;
  }
  if (d.suggested) {
    const s = d.suggested;
    html += `<p>准备这样接入：<br><code>name: ${esc(s.name)}</code>`
      + `<br><code>type: ${esc(s.type)}</code>`
      + (s.item_selector ? `<br><code>item_selector: ${esc(s.item_selector)}</code>` : '')
      + `</p>`;
  }
  if (d.suggested_note) html += `<p>${esc(d.suggested_note)}</p>`;
  if (d.api_candidates && d.api_candidates.length) {
    html += '<p>疑似接口（可用 sources.api 手工配置）：</p>'
      + d.api_candidates.slice(0, 5).map((u) => `<div class="sample"><code>${esc(u)}</code></div>`).join('');
  }
  if (d.samples && d.samples.length) {
    html += '<p>抓到的样例标题：</p>'
      + d.samples.slice(0, 4).map((t) => `<div class="sample">· ${esc(t)}</div>`).join('');
  }
  if (d.suggested) {
    html += '<button class="btn primary" id="add-confirm" style="margin-top:10px">➕ 加入采集</button>';
  }
  html += '</div>';

  if (d.candidates && d.candidates.length) {
    html += '<div class="probe-box"><h4>这个页面上的其他栏目</h4>'
      + '<p>如果结果不理想，点一个更像「通知列表」的页面重新探测：</p>'
      + d.candidates.map((c) => `<button class="cand" data-cand="${esc(c.url)}">`
          + `${esc(c.label)}<small>${esc(c.url)}</small></button>`).join('') + '</div>';
  }

  box.innerHTML = html;
  const confirmBtn = $('#add-confirm');
  if (confirmBtn) confirmBtn.addEventListener('click', addProbedSource);
}

async function addProbedSource() {
  if (!lastProbe || !lastProbe.suggested) return;
  const btn = $('#add-confirm');
  if (btn) { btn.disabled = true; btn.textContent = '正在加入并抓取…'; }
  try {
    const r = await api('/api/custom-sources', {
      method: 'POST',
      body: JSON.stringify(lastProbe.suggested),
    });
    const res = r.result || {};
    if (res.error) toast('加入失败：' + res.error, 5000);
    else toast(`已加入「${r.added.name}」，抓到 ${res.collected} 条（新增 ${res.new} 条）`, 5000);
    lastProbe = null;
    $('#add-url').value = '';
    $('#add-result').innerHTML = '';
    await loadCustomList();
    await refreshAll();
  } catch (e) {
    toast('失败：' + e.message, 6000);
    if (btn) { btn.disabled = false; btn.textContent = '➕ 加入采集'; }
  }
}

function bind() {
  $('#filters').addEventListener('click', (ev) => {
    const chip = ev.target.closest('.chip[data-view]');
    if (!chip) return;
    document.querySelectorAll('.chip[data-view]').forEach((c) => c.classList.remove('active'));
    chip.classList.add('active');
    state.view = chip.dataset.view;
    state.category = '';
    state.sortManual = false;
    applyAutoSort();
    loadCategories();
    loadItems();
  });

  $('#campuses').addEventListener('click', (ev) => {
    const tab = ev.target.closest('.campus[data-campus]');
    if (!tab) return;
    state.campus = tab.dataset.campus;
    state.category = '';      // 切校区时清掉二级筛选
    state.sortManual = false;
    applyAutoSort();
    renderCampuses();
    loadCategories();
    loadItems();
  });

  $('#cats').addEventListener('click', (ev) => {
    const chip = ev.target.closest('.cat[data-cat]');
    if (!chip) return;
    state.category = chip.dataset.cat;
    state.sortManual = false;
    applyAutoSort();
    renderCats();
    loadItems();
  });

  $('#btn-sort').addEventListener('click', () => {
    if (!state.sortManual) {
      // 第一次点：在当前基础上切到另一种，并锁定
      state.sort = state.sort === 'time' ? 'importance' : 'time';
      state.sortManual = true;
    } else {
      // 再点：恢复自动规则
      state.sortManual = false;
      applyAutoSort();
    }
    loadItems();
  });

  $('#btn-stale').addEventListener('click', () => {
    state.includeStale = !state.includeStale;
    loadCategories();
    loadItems();
  });

  let searchTimer;
  $('#search').addEventListener('input', (ev) => {
    clearTimeout(searchTimer);
    const value = ev.target.value.trim();
    searchTimer = setTimeout(() => { state.q = value; loadItems(); }, 280);
  });

  $('#list').addEventListener('click', async (ev) => {
    const card = ev.target.closest('.card');
    if (!card) return;
    const item = state.items.find((i) => String(i.id) === card.dataset.id);
    if (!item) return;
    openSheet(item);
    if (!item.is_read) {
      item.is_read = 1;
      card.classList.add('read');
      const dot = card.querySelector('.dot-unread');
      if (dot) dot.remove();
      try { await setFlag(item.id, 'is_read', true); loadStats(); } catch (e) { /* 静默 */ }
    }
  });

  $('#btn-refresh').addEventListener('click', runNow);
  $('#btn-readall').addEventListener('click', async () => {
    await api('/api/items/read-all', { method: 'POST' });
    toast('已全部标记为已读');
    refreshAll();
  });

  $('#sheet').addEventListener('click', (ev) => { if (ev.target.id === 'sheet') closeSheet(); });
  $('#sheet-open').addEventListener('click', () => {
    if (state.current && state.current.url) window.open(state.current.url, '_blank', 'noopener');
  });
  $('#sheet-star').addEventListener('click', async () => {
    const it = state.current;
    if (!it) return;
    const next = !it.starred;
    await setFlag(it.id, 'starred', next);
    toast(next ? '已加星标' : '已取消星标');
    closeSheet();
    refreshAll();
  });
  $('#sheet-read').addEventListener('click', async () => {
    const it = state.current;
    if (!it) return;
    const next = !it.is_read;
    await setFlag(it.id, 'is_read', next);
    closeSheet();
    refreshAll();
  });

  $('#btn-menu').addEventListener('click', () => $('#menu').classList.remove('hidden'));
  $('#menu').addEventListener('click', (ev) => { if (ev.target.id === 'menu') $('#menu').classList.add('hidden'); });
  $('#menu-close').addEventListener('click', () => $('#menu').classList.add('hidden'));
  $('#menu-run').addEventListener('click', async () => {
    $('#menu').classList.add('hidden');
    await runNow();
  });
  $('#menu-testpush').addEventListener('click', async () => {
    $('#menu').classList.add('hidden');
    toast('正在发送测试消息…', 6000);
    try {
      const r = await api('/api/test-push', { method: 'POST' });
      toast(r.ok ? '已发送，去微信看看' : '发送失败', 4000);
    } catch (e) {
      toast('失败：' + e.message, 6000);
    }
  });
  $('#menu-add').addEventListener('click', async () => {
    $('#menu').classList.add('hidden');
    await openAddPanel();
  });
  $('#add-close').addEventListener('click', () => $('#add-panel').classList.add('hidden'));
  $('#add-panel').addEventListener('click', (ev) => {
    if (ev.target.id === 'add-panel') $('#add-panel').classList.add('hidden');
  });
  $('#add-probe').addEventListener('click', () => {
    const url = $('#add-url').value.trim();
    if (!url) { toast('先粘一个网址进来'); return; }
    probeUrl(url);
  });
  $('#add-url').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') $('#add-probe').click();
  });
  $('#add-result').addEventListener('click', (ev) => {
    const cand = ev.target.closest('.cand[data-cand]');
    if (cand) {
      $('#add-url').value = cand.dataset.cand;
      probeUrl(cand.dataset.cand);
    }
  });
  $('#add-custom-list').addEventListener('click', async (ev) => {
    const btn = ev.target.closest('button[data-del]');
    if (!btn) return;
    const name = btn.dataset.del;
    if (!confirm(`确定删除源「${name}」？已抓到的条目会保留。`)) return;
    try {
      await api('/api/custom-sources?name=' + encodeURIComponent(name), { method: 'DELETE' });
      toast('已删除 ' + name);
      await loadCustomList();
      await refreshAll();
    } catch (e) {
      toast('失败：' + e.message, 5000);
    }
  });
  $('#menu-sources').addEventListener('click', async () => {
    $('#menu').classList.add('hidden');
    await openSourcesPanel();
  });
  $('#sources-close').addEventListener('click', () => $('#sources-panel').classList.add('hidden'));
  $('#sources-panel').addEventListener('click', (ev) => {
    if (ev.target.id === 'sources-panel') $('#sources-panel').classList.add('hidden');
  });
  $('#menu-runs').addEventListener('click', async () => {
    $('#menu').classList.add('hidden');
    try {
      const { runs } = await api('/api/runs');
      const text = (runs || []).map((r) =>
        `${r.started_at}  采集${r.collected} 新增${r.new_items} 推送${r.pushed} LLM${r.llm_calls}${r.notes ? ' ' + r.notes : ''}`
      ).join('\n') || '还没有运行记录';
      alert(text);
    } catch (e) {
      toast('失败：' + e.message);
    }
  });

  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') {
      closeSheet();
      $('#menu').classList.add('hidden');
      $('#sources-panel').classList.add('hidden');
      $('#add-panel').classList.add('hidden');
    }
  });
}

/* ---------------- 启动 ---------------- */
bind();
refreshAll();
setInterval(() => { loadStats(); }, 30000);

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('sw.js').catch(() => { /* http 下会失败，正常 */ });
  });
}
