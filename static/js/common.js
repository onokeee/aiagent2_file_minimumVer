/* 画面共通の小道具: 通信・トースト・DOM生成 */

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/* 線画アイコン。実体は _icons.html のスプライト（絵文字は使わない）。
   el() に渡せるよう DOM ノードで返す。 */
function icon(name, cls = '') {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', `icon ${cls}`.trim());
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#i-${name}`);
    svg.append(use);
    return svg;
}

function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class') node.className = v;
        else if (k === 'html') node.innerHTML = v;
        else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v);
    }
    for (const c of children.flat()) {
        if (c === null || c === undefined || c === false) continue;
        node.append(c.nodeType ? c : document.createTextNode(String(c)));
    }
    return node;
}

/* 「テーブル全体を閲覧」ボタン。サンプル行を出している所には必ずこれを添える。
   先頭数行だけでは「本当にこのテーブルでよいか」は決められないので、
   別タブ（読み取り専用のビューア）で中身を辿れるようにする。
   db はファイル名（sales.db）でもエイリアス（sales）でもよい。 */
function tableViewLink(dbName, table, cls = 'btn btn--sm btn--ghost') {
    if (!dbName || !table) return null;
    const href = `/table?db=${encodeURIComponent(dbName)}&table=${encodeURIComponent(table)}`;
    return el('a', {
        class: cls, href, target: '_blank', rel: 'noopener',
        title: `${table} の全行を別タブで開きます（読み取り専用）`,
    }, icon('table', 'icon--sm'), 'テーブル全体を閲覧');
}

function toast(message, kind = 'ok', ms = 4200) {
    const box = $('#toasts');
    if (!box) return;
    const node = el('div', { class: `toast toast--${kind}` }, message);
    box.append(node);
    setTimeout(() => { node.style.opacity = '0'; setTimeout(() => node.remove(), 250); }, ms);
}

async function api(url, body, method = 'POST') {
    const opt = { method, headers: { 'Content-Type': 'application/json' } };
    if (body !== undefined) opt.body = JSON.stringify(body);
    if (method === 'GET') { delete opt.body; delete opt.headers; }
    const res = await fetch(url, opt);
    let data = {};
    try { data = await res.json(); } catch (e) { /* 本文なし */ }
    if (!res.ok) throw new Error(data.error || `通信に失敗しました (${res.status})`);
    return data;
}

/* 最低限のMarkdown。AIの回答は見出し・箇条書き・強調・コードくらいしか使わない */
/* Markdownの表（| a | b | の並び）を <table> にする。
   AIは一覧を表で返すことが多く、生の | と --- が並ぶと読めない。
   セル内の <br>（AIが改行の意図で書く）は改行として扱う。行の | が揃っていなくても
   ある分だけ描く（崩れた表を全部捨てるより、読める形で出す方がよい）。 */
function mdTables(html) {
    const lines = html.split('\n');
    const out = [];
    let i = 0;
    const isRow = l => /^\s*\|.*\|\s*$/.test(l);
    const isSep = l => /^\s*\|(\s*:?-{2,}:?\s*\|)+\s*$/.test(l);
    const cells = l => l.trim().replace(/^\||\|$/g, '').split('|')
        .map(c => c.trim().replace(/&lt;br\s*\/?&gt;/gi, '<br>'));
    while (i < lines.length) {
        if (isRow(lines[i]) && i + 1 < lines.length && isSep(lines[i + 1])) {
            const head = cells(lines[i]);
            const body = [];
            i += 2;
            while (i < lines.length && isRow(lines[i]) && !isSep(lines[i])) {
                body.push(cells(lines[i])); i += 1;
            }
            out.push('<div class="tablewrap"><table class="data"><thead><tr>'
                + head.map(h => `<th>${h}</th>`).join('') + '</tr></thead><tbody>'
                + body.map(r => '<tr>' + r.map(c => `<td>${c}</td>`).join('') + '</tr>').join('')
                + '</tbody></table></div>');
            continue;
        }
        out.push(lines[i]); i += 1;
    }
    return out.join('\n');
}

function mdToHtml(src) {
    const esc = s => s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    const blocks = esc(src || '').split(/```/);
    return blocks.map((chunk, i) => {
        if (i % 2 === 1) return `<pre class="mono" style="background:var(--surface-2);padding:10px;border-radius:6px;overflow:auto">${chunk.replace(/^\w*\n/, '')}</pre>`;
        // 表を先に確定してから行単位の置換にかける（表の中の * や - を箇条書きと誤認しないため）
        return mdTables(chunk)
            .replace(/^### (.*)$/gm, '<h4>$1</h4>')
            .replace(/^## (.*)$/gm, '<h3>$1</h3>')
            .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
            .replace(/`([^`]+)`/g, '<code>$1</code>')
            // データの信頼性にかかわる注意書き（※ ⚠ で始まる行）は赤で出す。
            // 「更新できていない」「値の型が違う」は数字の読み方を左右するので、
            // 本文と同じ色で流すと読み飛ばされる。箇条書きの中の ※ も拾う。
            .replace(/^(\s*(?:[-*]\s*)?)([※⚠].*)$/gm, '$1<span class="caveat">$2</span>')
            .replace(/^\s*[-*] (.*)$/gm, '<li>$1</li>')
            .replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, '<ul>$1</ul>')
            // 表の直前後の改行は <br> にしない（表の周りに余白が二重に入る）
            .replace(/\n*(<div class="tablewrap">)/g, '$1')
            .replace(/(<\/div>)\n*/g, '$1')
            .replace(/\n{2,}/g, '</p><p>')
            .replace(/\n/g, '<br>');
    }).join('');
}

/* 値の見た目。数値は右寄せにしたいので型も返す */
function cellInfo(v) {
    if (v === null || v === undefined) return { text: '', num: false };
    if (typeof v === 'number') return { text: v.toLocaleString(undefined, { maximumFractionDigits: 6 }), num: true };
    return { text: String(v), num: false };
}

function dataTable(columns, rows, opts = {}) {
    const thead = el('thead', {}, el('tr', {}, columns.map(c => el('th', {}, c))));
    const tbody = el('tbody', {}, rows.map(r => el('tr', {},
        r.map(v => { const i = cellInfo(v); return el('td', { class: i.num ? 'num' : null, title: i.text }, i.text); })
    )));
    const wrap = el('div', { class: 'tablewrap' }, el('table', { class: 'data' }, thead, tbody));
    if (opts.caption) {
        return el('div', {}, wrap, el('div', { class: 'small muted', style: 'margin-top:4px' }, opts.caption));
    }
    return wrap;
}

/* --- 説明の吹き出し（はみ出さない版） --------------------------------------------
   CSSだけで作る .hastip は、要素の中に ::after を置くので、横を切り落とす親
   （サイドバーは overflow-x: hidden）の中では見えなくなる。こちらは body の直下に
   1つだけ作って画面座標で置くので、どこに置いた要素でも切れずに出せる。

   使い方: data-desc-title（見出し）/ data-desc（説明）/ data-desc-meta（右下の補足）
   説明が空でも、未登録であること自体を伝えたいので出す。 */

(function describeTip() {
    const DELAY = 260;                 // 通りすがりでは出さない
    let box = null, timer = null, current = null;

    function ensure() {
        if (box) return box;
        box = el('div', { class: 'desctip' });
        document.body.append(box);
        return box;
    }

    function place(target) {
        const b = ensure();
        const r = target.getBoundingClientRect();
        b.style.visibility = 'hidden';
        b.style.display = 'block';
        const w = b.offsetWidth, h = b.offsetHeight;
        // 基本は右側。入らなければ左に回す
        let x = r.right + 10;
        if (x + w > window.innerWidth - 8) x = Math.max(8, r.left - w - 10);
        let y = r.top + r.height / 2 - h / 2;
        y = Math.min(Math.max(8, y), window.innerHeight - h - 8);
        b.style.left = `${Math.round(x)}px`;
        b.style.top = `${Math.round(y)}px`;
        b.style.visibility = 'visible';
    }

    function show(target) {
        const title = target.dataset.descTitle || '';
        const desc = (target.dataset.desc || '').trim();
        const meta = target.dataset.descMeta || '';
        const b = ensure();
        // null は落としてから渡す。replaceChildren はNode以外を文字列にするので、
        // 見出しや補足が無いときに "null" という文字がそのまま吹き出しに出る。
        b.replaceChildren(...[
            title ? el('div', { class: 'desctip__title' }, title) : null,
            el('div', { class: desc ? 'desctip__body' : 'desctip__body desctip__body--none' },
                desc || '説明が未登録です。データカタログで書くと、AIの理解もここの表示も良くなります。'),
            meta ? el('div', { class: 'desctip__meta' }, meta) : null,
        ].filter(Boolean));
        place(target);
        current = target;
    }

    function hide() {
        clearTimeout(timer);
        current = null;
        if (box) box.style.display = 'none';
    }

    document.addEventListener('mouseover', ev => {
        const t = ev.target.closest?.('[data-desc]');
        if (!t || t === current) return;
        clearTimeout(timer);
        timer = setTimeout(() => show(t), DELAY);
    });
    document.addEventListener('mouseout', ev => {
        const t = ev.target.closest?.('[data-desc]');
        if (t && t === current && !t.contains(ev.relatedTarget)) hide();
        else if (t) clearTimeout(timer);
    });
    // キーボードで辿る人にも出す。スクロールしたら位置がずれるので消す
    document.addEventListener('focusin', ev => {
        const t = ev.target.closest?.('[data-desc]') || ev.target.querySelector?.('[data-desc]');
        if (t) show(t);
    });
    document.addEventListener('focusout', hide);
    window.addEventListener('scroll', hide, true);
    window.addEventListener('resize', hide);
})();

/* --- サイドバーの幅と開閉 ------------------------------------------------------
   境目をドラッグすると幅が変わり、クリックすると折りたたむ。
   「動かさずに離した」ときだけクリック扱いにしたいので、移動量で見分ける。 */

(function sidebarHandle() {
    const MIN = 180, MAX = 460, DEFAULT = 244;
    const root = document.documentElement;
    const store = {
        get width() { return parseInt(localStorage.getItem('sidebarWidth') || '', 10) || DEFAULT; },
        set width(v) { localStorage.setItem('sidebarWidth', String(v)); },
        get collapsed() { return localStorage.getItem('sidebarCollapsed') === '1'; },
        set collapsed(v) { localStorage.setItem('sidebarCollapsed', v ? '1' : '0'); },
    };
    const clamp = (v) => Math.min(MAX, Math.max(MIN, Math.round(v)));

    function apply(width, collapsed) {
        root.style.setProperty('--sidebar', (collapsed ? 0 : width) + 'px');
        document.body.classList.toggle('is-collapsed', collapsed);
    }

    // 読み込み直後に反映（前回の状態を覚えている）
    let width = clamp(store.width), collapsed = store.collapsed;
    apply(width, collapsed);

    document.addEventListener('DOMContentLoaded', () => {
        const handle = $('#sidebarResizer');
        if (!handle) return;

        let startX = 0, startW = width, moved = false, dragging = false;

        const onMove = (ev) => {
            if (!dragging) return;
            const dx = ev.clientX - startX;
            if (Math.abs(dx) > 3) moved = true;
            if (!moved) return;
            // 畳んだ状態から右へ引いたら、その場で開く
            if (collapsed && dx > 0) { collapsed = false; store.collapsed = false; }
            width = clamp((collapsed ? 0 : startW) + dx);
            apply(width, false);
        };

        const onUp = () => {
            if (!dragging) return;
            dragging = false;
            handle.classList.remove('is-dragging');
            document.body.classList.remove('is-resizing');
            document.removeEventListener('pointermove', onMove);
            document.removeEventListener('pointerup', onUp);
            if (moved) {
                store.width = width;
            } else {                      // 動かさなかった = クリック
                collapsed = !collapsed;
                store.collapsed = collapsed;
                apply(width, collapsed);
            }
        };

        handle.addEventListener('pointerdown', ev => {
            if (ev.button !== 0) return;
            ev.preventDefault();
            dragging = true; moved = false;
            startX = ev.clientX;
            startW = collapsed ? 0 : width;
            handle.classList.add('is-dragging');
            document.body.classList.add('is-resizing');
            document.addEventListener('pointermove', onMove);
            document.addEventListener('pointerup', onUp);
        });

        // キーボードでも操作できるようにする
        handle.addEventListener('keydown', ev => {
            const step = ev.shiftKey ? 40 : 16;
            if (ev.key === 'ArrowLeft' || ev.key === 'ArrowRight') {
                ev.preventDefault();
                collapsed = false; store.collapsed = false;
                width = clamp(width + (ev.key === 'ArrowRight' ? step : -step));
                store.width = width;
                apply(width, false);
            } else if (ev.key === 'Enter' || ev.key === ' ') {
                ev.preventDefault();
                collapsed = !collapsed; store.collapsed = collapsed;
                apply(width, collapsed);
            }
        });

        handle.addEventListener('dblclick', () => {   // 既定の幅に戻す
            width = DEFAULT; collapsed = false;
            store.width = width; store.collapsed = false;
            apply(width, false);
        });
    });
})();
