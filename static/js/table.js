/* テーブル全体を見る画面。

   サンプル行の「テーブル全体を閲覧」から別タブで開く読み取り専用のビューア。
   行はサーバ側で1ページずつ切って返るので、何百万行あっても画面は重くならない。
   絞り込み・並べ替えもサーバ側（表示中のページだけを並べても意味がないため）。 */

const T = window.TABLE_INIT || {};
let state = {
    offset: 0, limit: 100, q: '', sort: '', dir: 'asc',
    total: 0, matched: 0,
    filters: {},          // { 列名: {values:[...]} | {op:'>=', value:'100'} }
};
let timer = null;

function filterParam() {
    return Object.keys(state.filters).length ? JSON.stringify(state.filters) : '';
}

async function load() {
    const box = $('#tableBox');
    box.replaceChildren(el('div', { class: 'small muted', style: 'padding:10px' },
        el('span', { class: 'spinner' }), ' 読み込み中…'));
    const p = new URLSearchParams({
        db: T.db, table: T.table, offset: state.offset, limit: state.limit,
        q: state.q, sort: state.sort, dir: state.dir, filters: filterParam(),
    });
    let r;
    try {
        r = await api('/api/table/rows?' + p.toString(), undefined, 'GET');
    } catch (e) {
        box.replaceChildren(el('div', { class: 'alert alert--err' }, e.message));
        return;
    }
    Object.assign(state, {
        total: r.total, matched: r.matched, offset: r.offset,
        limit: r.limit, sort: r.sort, dir: r.dir,
    });
    render(r);
}

/* 列の見出し。名前を押すと並べ替え、漏斗を押すとフィルター（Excelと同じ感覚）。 */
function headCell(col) {
    const on = !!state.filters[col];
    return el('th', {},
        el('div', { class: 'th__inner' },
            el('span', {
                class: 'th__name', title: `${col} で並べ替え`,
                onclick: () => {
                    if (state.sort === col) state.dir = state.dir === 'asc' ? 'desc' : 'asc';
                    else { state.sort = col; state.dir = 'asc'; }
                    state.offset = 0; load();
                },
            }, col, state.sort === col ? (state.dir === 'asc' ? ' ↑' : ' ↓') : ''),
            el('button', {
                class: 'th__filter' + (on ? ' is-on' : ''),
                title: on ? `${col} で絞り込み中（クリックで変更）` : `${col} で絞り込む`,
                onclick: ev => { ev.stopPropagation(); openFilter(col, ev.currentTarget); },
            }, icon('filter', 'icon--sm'))));
}

function render(r) {
    const head = el('thead', {}, el('tr', {},
        el('th', { style: 'text-align:right;width:1%' }, '#'),
        r.columns.map(headCell)));

    const body = el('tbody', {}, r.rows.map((row, i) => el('tr', {},
        el('td', { class: 'num muted' }, (r.offset + i + 1).toLocaleString()),
        row.map(v => {
            const info = cellInfo(v);
            const empty = v === null || v === undefined;
            // 値が NULL なのか空文字なのかは、集計の食い違いの原因になるので区別して出す
            return el('td', { class: empty ? 'muted' : (info.num ? 'num' : null), title: info.text },
                empty ? 'NULL' : info.text);
        }))));

    $('#tableBox').replaceChildren(el('table', { class: 'data' }, head, body));
    renderChips();

    const shown = r.rows.length;
    const range = shown
        ? `${(r.offset + 1).toLocaleString()}〜${(r.offset + shown).toLocaleString()}行目を表示`
        : '該当なし';
    const filtered = state.q || Object.keys(state.filters).length;
    $('#countLabel').textContent = filtered
        ? `全 ${state.total.toLocaleString()}行 中 ${state.matched.toLocaleString()}行が一致（${range}）`
        : `全 ${state.total.toLocaleString()}行 ・ ${r.columns.length}列（${range}）`;

    const end = Math.max(1, Math.ceil((state.matched || 0) / state.limit));
    const page = Math.floor(state.offset / state.limit) + 1;
    $('#pageLabel').textContent = `${page} / ${end} ページ`;
    $('#prev').disabled = $('#first').disabled = state.offset <= 0;
    $('#next').disabled = $('#last').disabled = state.offset + state.limit >= state.matched;
}

/* いま効いている絞り込みを見出しの下に並べる。1つずつ外せる。 */
function renderChips() {
    const box = $('#chips');
    const names = Object.keys(state.filters);
    box.replaceChildren();
    if (!names.length) { box.classList.add('hidden'); return; }
    box.classList.remove('hidden');
    box.append(...names.map(col => el('span', { class: 'chip' },
        el('b', {}, col), '：', describeFilter(state.filters[col]),
        el('button', {
            class: 'chip__x', title: 'この絞り込みを外す',
            onclick: () => { delete state.filters[col]; state.offset = 0; load(); },
        }, '×'))),
        el('button', {
            class: 'btn btn--sm btn--ghost',
            onclick: () => { state.filters = {}; state.offset = 0; load(); },
        }, 'すべて解除'));
}

function describeFilter(f) {
    if (f.values) {
        const v = f.values.map(x => (x === null ? 'NULL' : x));
        return v.length <= 3 ? v.join('、') : `${v.slice(0, 3).join('、')} ほか${v.length - 3}件`;
    }
    const label = { contains: 'を含む', not_contains: 'を含まない',
                    empty: '空(NULL)', not_empty: '空でない' }[f.op];
    if (f.op === 'empty' || f.op === 'not_empty') return label;
    if (label) return `「${f.value}」${label}`;
    return `${f.op} ${f.value}`;
}

/* --- 列ごとの絞り込み（Excelのフィルター） --------------------------------------
   値の一覧はサーバから取る（テーブル全体を見た一覧。表示中のページではない）。
   種類が多い列は上から300件までなので、探す欄で絞ってから選ぶ。 */

function openFilter(col, anchor) {
    $('.colfilter')?.remove();
    const cur = state.filters[col] || {};
    const box = el('div', { class: 'colfilter' });
    box.addEventListener('click', ev => ev.stopPropagation());

    // ① 条件（含む・比較など）
    const op = el('select', {},
        ['（選んだ値だけ）', '含む', '含まない', '=', '!=', '>', '>=', '<', '<=', '空(NULL)', '空でない']
            .map(t => el('option', {}, t)));
    const OPS = { '含む': 'contains', '含まない': 'not_contains', '=': '=', '!=': '!=',
                  '>': '>', '>=': '>=', '<': '<', '<=': '<=',
                  '空(NULL)': 'empty', '空でない': 'not_empty' };
    const NAMES = Object.fromEntries(Object.entries(OPS).map(([k, v]) => [v, k]));
    if (cur.op) op.value = NAMES[cur.op] || '（選んだ値だけ）';
    const opValue = el('input', { type: 'text', placeholder: '値', value: cur.value ?? '' });

    // ② 値の一覧（チェックボックス）
    const search = el('input', { type: 'text', placeholder: '値を探す' });
    const list = el('div', { class: 'colfilter__list' },
        el('div', { class: 'small muted' }, el('span', { class: 'spinner' }), ' 読み込み中…'));
    const picked = new Set((cur.values || []).map(v => JSON.stringify(v)));

    const syncMode = () => {
        const byValues = op.value === '（選んだ値だけ）';
        opValue.style.display = byValues || ['空(NULL)', '空でない'].includes(op.value) ? 'none' : '';
        search.style.display = list.style.display = byValues ? '' : 'none';
    };
    op.addEventListener('change', syncMode);
    syncMode();

    let vtimer = null;
    async function loadValues() {
        const p = new URLSearchParams({ db: T.db, table: T.table, column: col, q: search.value.trim() });
        let r;
        try { r = await api('/api/table/values?' + p.toString(), undefined, 'GET'); }
        catch (e) { list.replaceChildren(el('div', { class: 'alert alert--err small' }, e.message)); return; }
        const rows = r.values.map(v => {
            const key = JSON.stringify(v.value);
            const cb = el('input', { type: 'checkbox', ...(picked.has(key) ? { checked: 'checked' } : {}) });
            cb.addEventListener('change', () => (cb.checked ? picked.add(key) : picked.delete(key)));
            return el('label', { class: 'colfilter__row' }, cb,
                el('span', { class: 'grow' }, v.value === null || v.value === undefined ? 'NULL'
                    : (String(v.value) === '' ? '（空文字）' : String(v.value))),
                el('span', { class: 'muted small' }, v.count.toLocaleString()));
        });
        list.replaceChildren(
            el('div', { class: 'small muted mb' },
                `${r.kinds.toLocaleString()}種類` + (r.truncated ? '（多い順に300件まで表示。探す欄で絞れます）' : '')),
            ...(rows.length ? rows : [el('div', { class: 'small muted' }, '該当する値がありません。')]));
    }
    search.addEventListener('input', () => { clearTimeout(vtimer); vtimer = setTimeout(loadValues, 250); });

    const apply = () => {
        if (op.value === '（選んだ値だけ）') {
            const vals = [...picked].map(k => JSON.parse(k));
            if (vals.length) state.filters[col] = { values: vals };
            else delete state.filters[col];
        } else if (['空(NULL)', '空でない'].includes(op.value)) {
            state.filters[col] = { op: OPS[op.value] };
        } else if (opValue.value.trim() !== '') {
            state.filters[col] = { op: OPS[op.value], value: opValue.value.trim() };
        } else {
            delete state.filters[col];
        }
        box.remove(); state.offset = 0; load();
    };

    box.append(
        el('div', { class: 'colfilter__head' }, el('b', { class: 'grow' }, col),
            el('button', { class: 'btn btn--sm btn--ghost', onclick: () => box.remove() }, '×')),
        el('div', { class: 'row', style: 'gap:6px' }, op, opValue),
        search, list,
        el('div', { class: 'row mt', style: 'gap:6px' },
            el('button', {
                class: 'btn btn--sm', onclick: () => {
                    delete state.filters[col]; box.remove(); state.offset = 0; load();
                },
            }, '解除'),
            el('div', { class: 'spacer' }),
            el('button', { class: 'btn btn--sm btn--primary', onclick: apply }, '適用')));

    document.body.append(box);
    // 見出しの真下に出す。画面の右端からはみ出すときは左へずらす
    const r = anchor.getBoundingClientRect();
    box.style.top = `${Math.round(r.bottom + 4)}px`;
    box.style.left = `${Math.round(Math.min(r.left, window.innerWidth - box.offsetWidth - 12))}px`;
    loadValues();
    setTimeout(() => document.addEventListener('click', function once() {
        box.remove(); document.removeEventListener('click', once);
    }), 0);
}

function move(delta) {
    state.offset = Math.max(0, state.offset + delta * state.limit);
    load();
}

document.addEventListener('DOMContentLoaded', () => {
    if (T.error) return;                       // テーブルが無いときは表示だけ
    $('#q').addEventListener('input', ev => {
        // 1文字ごとに投げない（打ち終わりを待つ）
        clearTimeout(timer);
        timer = setTimeout(() => { state.q = ev.target.value.trim(); state.offset = 0; load(); }, 300);
    });
    $('#size').addEventListener('change', ev => {
        state.limit = Number(ev.target.value); state.offset = 0; load();
    });
    $('#first').addEventListener('click', () => { state.offset = 0; load(); });
    $('#prev').addEventListener('click', () => move(-1));
    $('#next').addEventListener('click', () => move(1));
    $('#last').addEventListener('click', () => {
        state.offset = Math.max(0, (Math.ceil(state.matched / state.limit) - 1) * state.limit);
        load();
    });
    load();
});
