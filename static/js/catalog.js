/* データカタログ画面。タブ・テーブル編集・用語集・例文・ツール。 */

/* --- タブ ------------------------------------------------------------------- */

/* いま見ているタブはURLのハッシュに残す。ツール保存などでページを
   読み直しても、同じタブに戻ってこられる。 */
function activateTab(pane) {
    // 「DB情報」タブは「DB・テーブル」に統合した。古いブックマークや、
    // DBを切り替えたときの持ち越しで来ても迷子にせず、その場所を開いて見せる。
    if (pane === 'info') {
        pane = 'tables';
        $('#dbInfo')?.setAttribute('open', 'open');
    }
    const tab = $(`.tab[data-pane="${pane}"]`);
    if (!tab) return;
    $$('.tab[data-pane]').forEach(t => t.classList.toggle('is-active', t === tab));
    $$('.tabpane').forEach(p => p.classList.toggle('is-active', p.id === `pane-${pane}`));
    // ツールはDBに属さない（全DB共通）。DBの切替と充実度メーターを見せたままだと
    // 「選択中のDBのツール」と読めてしまうので、このタブでは隠す。
    const end = $('.tabs__end');
    if (end) end.style.display = (pane === 'tools') ? 'none' : '';
    if (pane === 'er') ER.refit();
    // 隠れているタブでは高さを測れないので、表示された瞬間に伸びる欄を測り直す
    if (pane === 'glossary') $$('#pane-glossary textarea').forEach(autoGrow);
    history.replaceState(null, '',
        pane === 'glossary' ? `#tab=glossary&sec=${glSec}` : `#tab=${pane}`);
}

/* チャットから「カタログで説明を書く」で来たとき、そのテーブルを開いて光らせる。
   一覧の途中にあると、開いても自分で探すことになるので、位置まで運ぶ。 */
function revealTable(name) {
    const acc = $(`#pane-tables details.acc[data-table="${CSS.escape(name)}"]`);
    if (!acc) {
        toast(`テーブル「${name}」はこのDBにありません。`, 'warn');
        return;
    }
    acc.classList.remove('hidden');
    acc.open = true;
    acc.classList.add('is-target');
    acc.scrollIntoView({ block: 'center', behavior: 'smooth' });
    // 光らせるのは合図なので、見つけてもらえたら消す
    setTimeout(() => acc.classList.remove('is-target'), 2600);
}

/* DBを切り替えるときに持ち越すハッシュ。
   見ていたタブ（と用語集の中の切り替え）はそのままにする。
   テーブルの指定だけは落とす。DBが変われば同じ名前のテーブルは無いのが普通で、
   持ち越すと「そのテーブルはありません」と言われるだけになるため。 */
function carriedHash() {
    const parts = location.hash.replace(/^#/, '').split('&')
        .filter(p => p && !p.startsWith('table='));
    return parts.length ? '#' + parts.join('&') : '';
}

function wireTabs() {
    $$('.tab[data-pane]').forEach(tab => tab.addEventListener('click', () => activateTab(tab.dataset.pane)));
    // activateTab はハッシュを書き換えるので、必要な値は先に全部読んでおく
    const hash = location.hash;
    const sec = hash.match(/sec=(\w+)/);
    const tab = hash.match(/tab=(\w+)/);
    const table = hash.match(/table=([^&]+)/);

    if (sec && ['gl', 'ex', 'ck'].includes(sec[1])) switchSec(sec[1]);
    if (table) {
        activateTab('tables');
        // 描画が終わってから運ぶ（開いた直後は高さが確定していない）
        requestAnimationFrame(() => revealTable(decodeURIComponent(table[1])));
    } else if (tab) {
        activateTab(tab[1]);
    }
}

/* --- 未保存の変更 --------------------------------------------------------------
   書きかけの内容を黙って失わせない。編集した場所に「未保存」の印を付け、
   画面右下のバーからまとめて保存できるようにする。ページを離れるときは警告。 */

const dirty = { tables: new Set(), glossary: false, examples: false, checks: false,
                info: false };

function dirtyLabel() {
    const parts = [];
    if (dirty.tables.size) parts.push(`テーブル${dirty.tables.size}件`);
    if (dirty.glossary) parts.push('用語集');
    if (dirty.examples) parts.push('例文');
    if (dirty.checks) parts.push('検算');
    if (dirty.info) parts.push('DB情報');
    return parts.join('・');
}

function updateSavebar() {
    const label = dirtyLabel();
    $('#savebar')?.classList.toggle('hidden', !label);
    if (label) $('#savebarText').textContent = `未保存: ${label}`;
}

function markTableDirty(acc) {
    dirty.tables.add(acc.dataset.table);
    const summary = $('summary', acc);
    if (!$('.js-dirty', summary)) {
        summary.append(el('span', { class: 'badge badge--accent js-dirty' }, '未保存'));
    }
    updateSavebar();
}

function clearTableDirty(acc) {
    dirty.tables.delete(acc.dataset.table);
    $('.js-dirty', acc)?.remove();
    updateSavebar();
}

function setDirty(key, on = true) {
    dirty[key] = on;
    updateSavebar();
}

// アプリ都合の遷移（DB切替・再プロファイル・保存後の再読込）では警告を出さない
let leavingOnPurpose = false;
window.addEventListener('beforeunload', ev => {
    if (dirtyLabel() && !leavingOnPurpose) { ev.preventDefault(); ev.returnValue = ''; }
});
function reloadClean() { leavingOnPurpose = true; window.location.reload(); }

/* --- 充実度（上部の数字） -------------------------------------------------------
   保存のたびに数え直す。ページを読み直さなくても数字が現実に追いつくように。 */

function recomputeMetrics() {
    const tabs = CAT.tables;
    const td = tabs.filter(t => t.description).length;
    const cd = tabs.reduce((n, t) => n + t.columns.filter(c => c.description).length, 0);
    const ct = tabs.reduce((n, t) => n + t.columns.length, 0);
    $('#mTables').textContent = `${td}/${tabs.length}`;
    $('#mCols').textContent = `${cd}/${ct}`;
    $('#mGloss').textContent = Object.keys(CAT.dbGlossary || {}).length
        + tabs.reduce((n, t) => n + Object.keys(t.glossary || {}).length, 0);
    $('#mEx').textContent = (CAT.examples || []).length;
}

/* --- テーブル説明 ------------------------------------------------------------ */

function parseValues(text) {
    const out = {};
    String(text || '').split(/[;\n]/).forEach(part => {
        const i = part.indexOf('=');
        if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
    });
    return out;
}

/** 1テーブル分を保存する。成功したら画面内の控え（CAT.tables）とバッジも合わせる。 */
async function saveTable(acc) {
    const table = acc.dataset.table;
    const desc = $('.t-desc', acc).value;
    const columns = {};
    $$('tr[data-col]', acc).forEach(tr => {
        columns[tr.dataset.col] = {
            description: $('.c-desc', tr).value,
            values: parseValues($('.c-vals', tr).value),
        };
    });
    await api('/api/catalog/table', { db: CAT.db, table, description: desc, columns });

    // 絞り込みと充実度は CAT.tables を見るので、保存内容をそちらへも反映する
    const t = CAT.tables.find(x => x.name === table);
    if (t) {
        t.description = desc.trim();
        t.ai_draft = false;
        t.columns.forEach(c => {
            const tr = acc.querySelector(`tr[data-col="${CSS.escape(c.name)}"]`);
            if (tr) c.description = $('.c-desc', tr).value.trim();
        });
    }
    const summary = $('summary', acc);
    $$('.badge', summary).forEach(b => b.remove());
    summary.append(el('span', { class: `badge ${desc.trim() ? 'badge--ok' : 'badge--warn'}` },
        desc.trim() ? '説明あり' : '説明なし'));
    clearTableDirty(acc);
    recomputeMetrics();
}

/** 未保存のものを全部保存する（右下のバーと Ctrl+S から）。 */
async function saveAllDirty() {
    for (const acc of $$('#pane-tables details.acc[data-table]')) {
        if (!dirty.tables.has(acc.dataset.table)) continue;
        try { await saveTable(acc); }
        catch (e) { toast(`${acc.dataset.table}: ${e.message}`, 'err', 8000); }
    }
    if (dirty.glossary) await saveGlossary();
    if (dirty.examples) await saveExamples();
    if (dirty.checks) await saveChecks();
    if (dirty.info) await saveInfo();
    if (!dirtyLabel()) toast('すべて保存しました。');
}

/* --- 管理（取り込み元・定期取り込み・更新履歴・削除） -----------------------------
   取り込み画面の「DBの管理」タブにあったものを、DBを見ているこの画面に集約した。
   描画は manage.js（両画面で共有）。ここでは /api/import/manage から
   このDBの分だけを取り出して、各テーブルの「管理」とDB情報の行に流し込む。 */
let manageData = null;

async function loadManage(force) {
    if (manageData && !force) return manageData;
    try {
        const m = await api('/api/import/manage', undefined, 'GET');
        manageData = m;
    } catch (e) {
        $('#dbManageInfo') && ($('#dbManageInfo').textContent = e.message);
        return null;
    }
    // 定期取り込みの全体状態（スケジューラ・宙に浮いた設定）
    renderSched(manageData.sched);
    renderOrphans(manageData.orphans || []);

    const d = (manageData.dbs || []).find(x => x.name === CAT.db);
    // DB情報の行: サイズ・更新日・削除ボタン
    const info = $('#dbManageInfo'), slot = $('#dbDeleteSlot');
    if (info && d) {
        info.textContent = `${d.tables.length}テーブル ・ `
            + `${((d.size || 0) / 1024).toLocaleString(undefined, { maximumFractionDigits: 0 })} KB`
            + ` ・ 更新 ${d.mtime || '―'}`;
        slot.replaceChildren(dbDeleteButton(d));
    }
    // 各テーブルの「管理」: 開いているものだけ描く（未開封は開いたときに描く）
    $$('.t-manage').forEach(acc => {
        if (acc.open) renderTableManage(acc);
    });
    return manageData;
}

/** テーブル1つぶんの管理欄を描く（manage.js の tableCard の中身を流用）。 */
function renderTableManage(acc) {
    const body = $('.t-manage__body', acc);
    const d = (manageData?.dbs || []).find(x => x.name === CAT.db);
    const t = d?.tables.find(x => x.name === acc.dataset.table);
    if (!t) {
        body.replaceChildren(el('div', { class: 'small muted' }, '管理情報を取得できませんでした。'));
        return;
    }
    // tableCard は <details> を返す。中身の acc__body だけをここに載せる
    const card = tableCard(CAT.db, t);
    const inner = card.querySelector('.acc__body');
    body.replaceChildren(...(inner ? [...inner.childNodes] : []));
    // 中身のサンプル行と更新履歴は、tableCard 側が toggle 時に読む設計なので手で呼ぶ
    const sampleBox = inner?.__sampleBox, histBox = inner?.__histBox;
    if (sampleBox && histBox) loadTableDetail(CAT.db, t.name, sampleBox, histBox);
}

function wireManage() {
    $$('.t-manage').forEach(acc => acc.addEventListener('toggle', async () => {
        if (!acc.open) return;
        await loadManage();
        renderTableManage(acc);
    }));
    // DB情報を開いたときにサイズ・削除ボタンを出す
    $('#dbInfo')?.addEventListener('toggle', ev => { if (ev.target.open) loadManage(); });
    // 定期取り込みの全体状態は開いてすぐ見えるようにする
    loadManage();
}

function wireTables() {
    // どの入力欄をいじっても、そのテーブルに「未保存」の印を付ける
    $('#pane-tables').addEventListener('input', ev => {
        const acc = ev.target.closest('details.acc[data-table]');
        if (acc && ev.target.matches('.t-desc, .c-desc, .c-vals')) markTableDirty(acc);
    });

    $$('details.acc[data-table]').forEach(acc => {
        const table = acc.dataset.table;

        $('.t-save', acc)?.addEventListener('click', async ev => {
            ev.target.disabled = true;
            try {
                await saveTable(acc);
                toast(`${table} を保存しました。`);
            } catch (e) { toast(e.message, 'err'); }
            ev.target.disabled = false;
        });

        $('.t-draft', acc)?.addEventListener('click', async ev => {
            ev.target.disabled = true;
            ev.target.innerHTML = '<span class="spinner"></span> 生成中';
            try {
                const r = await api('/api/catalog/draft-table', { db: CAT.db, table });
                const d = r.draft || {};
                let filled = false;
                if (d.description && !$('.t-desc', acc).value.trim()) {
                    $('.t-desc', acc).value = d.description; filled = true;
                }
                Object.entries(d.columns || {}).forEach(([name, cd]) => {
                    const tr = acc.querySelector(`tr[data-col="${CSS.escape(name)}"]`);
                    if (!tr) return;
                    if (cd.description && !$('.c-desc', tr).value.trim()) {
                        $('.c-desc', tr).value = cd.description; filled = true;
                    }
                    if (cd.values && !$('.c-vals', tr).value.trim()) {
                        $('.c-vals', tr).value =
                            Object.entries(cd.values).map(([k, v]) => `${k}=${v}`).join('; ');
                        filled = true;
                    }
                });
                // スクリプトからの書き込みは input が飛ばないので、印は自分で付ける
                if (filled) markTableDirty(acc);
                toast('AIの下書きを入れました。内容を確認して保存してください。');
            } catch (e) { toast(e.message, 'err'); }
            ev.target.disabled = false;
            ev.target.textContent = 'AIに下書きさせる';
        });
    });
}

/* --- テーブルの絞り込み ---------------------------------------------------------
   検索対象は名前だけでなく、説明・コード値・実際の値も含める。
   「単価はどのテーブル？」「'出荷済' はどこに入っている？」に答えるため。 */

let missingOnly = false;

function applyTableFilter() {
    const q = $('#tblFilter').value.trim().toLowerCase();
    const accs = $$('#pane-tables details.acc[data-table]');
    let shown = 0;
    const matched = [];
    accs.forEach(acc => {
        const descNow = $('.t-desc', acc).value.trim();
        let hay = `${acc.dataset.table} ${descNow}`.toLowerCase();
        $$('tr[data-col]', acc).forEach(tr => {
            const rowText = [tr.dataset.col, $('.c-desc', tr).value, $('.c-vals', tr).value,
                             tr.cells[4]?.getAttribute('title') || ''].join(' ').toLowerCase();
            const hit = !!q && rowText.includes(q);
            tr.classList.toggle('is-hit', hit);     // 当たった列は行ごと着色
            hay += ' ' + rowText;
        });
        const show = (!q || hay.includes(q)) && (!missingOnly || !descNow);
        acc.classList.toggle('hidden', !show);
        if (show) { shown++; matched.push(acc); }
    });
    // 数件まで絞れたら開いて見せる（開いて回る手間を省く）
    if (q && shown && shown <= 4) matched.forEach(a => { a.open = true; });
    const info = $('#tblFilterInfo');
    if (q || missingOnly) {
        info.classList.remove('hidden');
        info.textContent = `${accs.length}テーブル中 ${shown}件を表示`;
    } else {
        info.classList.add('hidden');
    }
}

function wireTableFilter() {
    $('#tblFilter').addEventListener('input', applyTableFilter);
    $('#tblMissing').addEventListener('click', ev => {
        missingOnly = !missingOnly;
        ev.target.classList.toggle('btn--primary', missingOnly);
        applyTableFilter();
    });
    $('#tblOpenAll').addEventListener('click', () =>
        $$('#pane-tables details.acc[data-table]:not(.hidden)').forEach(a => { a.open = true; }));
    $('#tblCloseAll').addEventListener('click', () =>
        $$('#pane-tables details.acc[data-table]').forEach(a => { a.open = false; }));
}

/* --- 用語集・例文（一覧＋エディタの2ペイン） ------------------------------------
   以前は全行が常に編集フォームで並び、件数が増えると走査も編集もつらかった。
   左に見渡すための一覧、右にいま選んだ1件だけのエディタ、という構成に分け、
   SQLを書く手が止まらないよう、列名を1クリックで式に挿せる参照を
   SQL欄のすぐ下に置く。行の実体は glItems / exItems（JSの配列）に持ち、
   画面はそこから描き直す。 */

const SCOPE_DB = '';                 // 「DB全体」を表す値
let glInitialScopes = new Set();     // 保存時に「空になった場所」も書き戻して消すため
let glItems = [], glSelId = null;    // 用語: {id, term, scope, description, sql, verdict, detail, dirty}
let exItems = [], exSelId = null;    // 例文: {id, q, description, sql, verdict, detail, dirty}
let idSeq = 0;
let glSec = 'gl';                    // いま開いている側（gl=用語集 / ex=例文）

/* SQLは1行に収まらないことが多いので、書いたぶんだけ伸びる欄にする */
function autoGrow(ta) {
    ta.style.height = 'auto';
    ta.style.height = Math.min(220, Math.max(34, ta.scrollHeight)) + 'px';
}

function growingSql(cls, value, placeholder) {
    const ta = el('textarea', { class: `${cls} mono`, rows: '1', placeholder }, value || '');
    ta.addEventListener('input', () => autoGrow(ta));
    // 一度でも触ったかどうか。参照からの挿入位置の判断に使う
    ta.addEventListener('focus', () => { ta.dataset.touched = '1'; });
    requestAnimationFrame(() => autoGrow(ta));
    return ta;
}

/* 検証結果の色。OK系は緑、0行は注意、エラーは赤 */
function verdictClass(verdict) {
    if (['エラー', '不一致'].includes(verdict)) return 'err';
    if (verdict === '0行') return 'warn';
    if (['条件式', '計算式', 'OK', '一致'].includes(verdict)) return 'ok';
    return '';
}

/* エディタ内の検証結果（バッジ＋説明文） */
function setStatus(box, it) {
    if (!it.verdict) { box.replaceChildren(); return; }
    const cls = verdictClass(it.verdict);
    box.replaceChildren(
        el('span', { class: `badge${cls ? ' badge--' + cls : ''}` }, it.verdict),
        el('span', { class: 'small muted' }, it.detail || ''));
}

/* 一覧の行頭の点。検証結果がひと目で分かるように */
function statusDot(it) {
    const cls = verdictClass(it.verdict);
    return el('span', { class: `dot${cls ? ' dot--' + cls : ''}`,
                        title: it.verdict ? `検証: ${it.verdict}${it.detail ? ' — ' + it.detail : ''}` : '未検証' });
}

const dirtyDot = it => it.dirty
    ? el('span', { class: 'dot dot--dirty', title: '未保存の変更' }) : null;

/* カーソル位置に文字列を挿し込む。参照の列名クリックから使う */
function insertIntoTa(ta, text) {
    let s = ta.selectionStart ?? ta.value.length;
    let e = ta.selectionEnd ?? s;
    // まだ一度も触っていない欄はカーソルが先頭にあるだけなので、末尾に足す
    if (!ta.dataset.touched) {
        s = e = ta.value.length;
        if (s && !/[\s(.,]$/.test(ta.value)) text = ' ' + text;
    }
    ta.value = ta.value.slice(0, s) + text + ta.value.slice(e);
    ta.selectionStart = ta.selectionEnd = s + text.length;
    ta.focus();
    // input を流して、未保存の印と欄の高さを通常の入力と同じ経路で更新する
    ta.dispatchEvent(new Event('input', { bubbles: true }));
}

/* --- 参照（エディタ内に出す、テーブルの中身） ------------------------------------ */

function fmtCodes(c) {
    return Object.entries(c.codes || {}).map(([k, v]) => `${k}=${v}`).join('; ');
}

function refTable(t, qualify, ta) {
    const head = el('thead', {}, el('tr', {},
        ['列', '型', '説明・コード値', '実際の値'].map(c => el('th', {}, c))));
    const body = el('tbody', {}, t.columns.map(c => {
        const ins = qualify ? `${t.name}.${c.name}` : c.name;
        const desc = [c.description, fmtCodes(c)].filter(Boolean).join(' ／ ');
        return el('tr', {},
            el('td', {}, el('button', { class: 'reflink', type: 'button',
                title: `クリックで「${ins}」をSQLに挿入`,
                onclick: () => insertIntoTa(ta, ins) }, c.name + (c.pk ? ' (PK)' : ''))),
            el('td', { class: 'muted' }, c.type),
            el('td', { class: 'muted', title: desc }, desc),
            el('td', { class: 'muted', title: c.actual }, c.actual));
    }));
    return el('div', { class: 'tablewrap' }, el('table', { class: 'data' }, head, body));
}

function sampleAcc(t) {
    if (!t.sample_rows?.length) return null;
    return el('details', { class: 'acc mt' },
        el('summary', {}, 'サンプル行を見る'),
        el('div', { class: 'acc__body' },
            el('div', { class: 'row mb', style: 'align-items:center' },
                el('span', { class: 'small muted' },
                    `先頭 ${t.sample_rows.length} 行のみ`),
                el('div', { class: 'spacer' }),
                tableViewLink(CAT.db, t.name)),
            dataTable(t.sample_columns, t.sample_rows)));
}

function rowsLabel(t) {
    return t.rows === null || t.rows === undefined ? '行数不明'
        : t.rows.toLocaleString() + '行';
}

/** テーブルを1つ選んでいればそれを開いて、DB全体なら全テーブルを畳んで見せる。 */
function refPanel(scope, ta) {
    const single = scope ? CAT.tables.find(x => x.name === scope) : null;
    const box = el('div', { class: 'refbox' });
    if (single) {
        box.append(
            el('div', { class: 'small muted mb' },
                `参照: ${single.name}（${rowsLabel(single)}）。列名をクリックするとSQLに入ります。`),
            refTable(single, false, ta), sampleAcc(single));
    } else if (CAT.tables.length) {
        box.append(el('div', { class: 'small muted mb' },
            '参照: テーブルを開いて列名をクリックすると「テーブル名.列名」の形でSQLに入ります。'));
        CAT.tables.forEach(t => box.append(el('details', { class: 'acc' },
            el('summary', {}, el('strong', {}, t.name),
                el('span', { class: 'muted small' }, `${rowsLabel(t)} / ${t.columns.length}列`)),
            el('div', { class: 'acc__body' }, refTable(t, true, ta), sampleAcc(t)))));
    }
    return box;
}

/* --- 用語集⇄例文の切り替え ----------------------------------------------------- */

function switchSec(sec) {
    glSec = sec;
    $('#sec-gl').classList.toggle('hidden', sec !== 'gl');
    $('#sec-ex').classList.toggle('hidden', sec !== 'ex');
    $('#sec-ck').classList.toggle('hidden', sec !== 'ck');
    $$('.seg__btn').forEach(b => b.classList.toggle('is-active', b.dataset.sec === sec));
    // 隠れている間は高さを測れないので、表示された側の伸びる欄を測り直す
    $$(`#sec-${sec} textarea`).forEach(autoGrow);
    if ($('#pane-glossary')?.classList.contains('is-active')) {
        history.replaceState(null, '', `#tab=glossary&sec=${sec}`);
    }
}

/* ↑↓キーで一覧を移動できるようにする（一覧にフォーカスがあるとき） */
function listArrowNav(ev, listSel, pick) {
    if (ev.key !== 'ArrowDown' && ev.key !== 'ArrowUp') return;
    ev.preventDefault();
    const btns = $$(`${listSel} .mlist__item`);
    if (!btns.length) return;
    const i = btns.findIndex(b => b.classList.contains('is-active'));
    const next = i === -1 ? btns[0] : btns[i + (ev.key === 'ArrowDown' ? 1 : -1)];
    if (next) pick(Number(next.dataset.id), true);
}

/* --- 用語集 ------------------------------------------------------------------- */

const glById = id => glItems.find(x => x.id === id);
const glScopeLabel = scope => scope || 'DB全体（複数テーブル）';

function markGlDirty(it) { it.dirty = true; setDirty('glossary'); }

/** 同じ置き場所に同じ用語が他にもあるか。保存すると片方しか残らないので警告する。 */
function glDupOf(it) {
    return glItems.some(x => x !== it && x.scope === it.scope
        && x.term.trim() && x.term.trim() === it.term.trim());
}

function loadGlossaryAll() {
    glItems = [];
    Object.entries(CAT.dbGlossary || {}).forEach(([term, v]) => glItems.push({
        id: ++idSeq, term, scope: SCOPE_DB,
        description: v.description || '', sql: v.sql || '',
        verdict: null, detail: '', dirty: false }));
    CAT.tables.forEach(t => Object.entries(t.glossary || {}).forEach(([term, v]) => glItems.push({
        id: ++idSeq, term, scope: t.name,
        description: v.description || '', sql: v.sql || '',
        verdict: null, detail: '', dirty: false })));
    glInitialScopes = new Set(glItems.map(r => r.scope));
    glSelId = glItems[0]?.id ?? null;
    glRenderList();
    glRenderEditor();
}

function glRenderList() {
    const q = ($('#glFilter')?.value || '').trim().toLowerCase();
    const groups = [[SCOPE_DB, glScopeLabel(SCOPE_DB)],
                    ...CAT.tables.map(t => [t.name, t.name])];
    const nodes = [];
    let shown = 0;
    for (const [scope, label] of groups) {
        const items = glItems.filter(it => it.scope === scope && (!q ||
            `${it.term} ${it.description} ${it.sql} ${label}`.toLowerCase().includes(q)));
        if (!items.length) continue;
        nodes.push(el('div', { class: 'mlist__group' }, `${label}（${items.length}）`));
        items.forEach(it => {
            shown++;
            nodes.push(el('button', {
                class: `mlist__item${it.id === glSelId ? ' is-active' : ''}`,
                type: 'button', 'data-id': it.id,
                onclick: () => glSelect(it.id),
            },
                statusDot(it),
                el('span', { class: 'mlist__term' }, it.term || '（無題）'),
                el('span', { class: 'mlist__desc' }, it.description || it.sql || ''),
                dirtyDot(it)));
        });
    }
    if (!nodes.length) {
        nodes.push(el('div', { class: 'mlist__empty' },
            glItems.length ? '絞り込みに当たる用語がありません。'
                           : 'まだ用語がありません。「＋ 用語を追加」から登録してください。'));
    }
    $('#glList').replaceChildren(...nodes);
    $('#glCount').textContent = q ? `${glItems.length}件中 ${shown}件` : `${glItems.length}件`;
    $('#segGlN').textContent = glItems.length;
}

function glSelect(id, focusList = false) {
    glSelId = id;
    glRenderList();
    glRenderEditor();
    const btn = $(`#glList .mlist__item[data-id="${id}"]`);
    btn?.scrollIntoView({ block: 'nearest' });
    if (focusList) btn?.focus();
}

function glAdd() {
    // 置き場所は、いま見ている用語と同じ所を最初の候補にする
    const cur = glById(glSelId);
    const it = { id: ++idSeq, term: '',
                 scope: cur ? cur.scope : (CAT.tables[0]?.name ?? SCOPE_DB),
                 description: '', sql: '', verdict: null, detail: '', dirty: true };
    glItems.push(it);
    glSelect(it.id);
    $('#glEditor .ed-term')?.focus();
}

function glRenderEditor() {
    const box = $('#glEditor');
    const it = glById(glSelId);
    if (!it) {
        box.replaceChildren(el('div', { class: 'editcard editcard--empty' },
            el('div', {},
                el('div', { class: 'muted mb' }, glItems.length
                    ? '左の一覧から用語を選んでください。'
                    : 'まだ用語がありません。最初の用語を登録しましょう。'),
                el('button', { class: 'btn btn--sm', type: 'button', onclick: glAdd },
                    '＋ 用語を追加'))));
        return;
    }

    const term = el('input', { type: 'text', class: 'ed-term', value: it.term,
        placeholder: '用語（例: 有効な受注）' });
    const scopeSel = el('select', { title: 'この用語の置き場所' },
        ...CAT.tables.map(t => el('option',
            { value: t.name, ...(it.scope === t.name ? { selected: 'selected' } : {}) }, t.name)),
        el('option', { value: SCOPE_DB, ...(it.scope === SCOPE_DB ? { selected: 'selected' } : {}) },
            'DB全体（複数テーブル）'));
    const desc = el('input', { type: 'text', value: it.description,
        placeholder: '説明（自然言語でOK。例: キャンセル以外の、実際に売上になる受注）' });
    const sql = growingSql('ed-sql', it.sql, "SQL条件・計算式（任意。例: status != '9'）");
    const dup = el('div', { class: 'small ed-dup hidden' });
    const status = el('div', { class: 'vstatus' });
    setStatus(status, it);

    const showDup = () => {
        const bad = !!it.term.trim() && glDupOf(it);
        dup.classList.toggle('hidden', !bad);
        if (bad) dup.textContent = `「${it.term.trim()}」は「${glScopeLabel(it.scope)}」に`
            + '既にあります。保存する前に1つにまとめてください。';
    };
    showDup();

    term.addEventListener('input', () => {
        it.term = term.value; markGlDirty(it); glRenderList(); showDup();
    });
    desc.addEventListener('input', () => {
        it.description = desc.value; markGlDirty(it); glRenderList();
    });
    sql.addEventListener('input', () => {
        if (it.sql === sql.value) return;
        // 式が変わったら前の検証結果はあてにならないので消す
        it.sql = sql.value; it.verdict = null; it.detail = '';
        setStatus(status, it); markGlDirty(it); glRenderList();
    });
    scopeSel.addEventListener('change', () => {
        it.scope = scopeSel.value;
        it.verdict = null; it.detail = '';
        markGlDirty(it);
        glRenderList();
        glRenderEditor();          // 参照パネルを新しい置き場所に合わせて作り直す
    });

    const draftBtn = el('button', {
        class: 'btn btn--sm', type: 'button',
        ...(CAT.llmReady ? {} : { disabled: 'disabled' }),
        title: '説明をもとに、AIがこの用語のSQL式を下書きします',
        onclick: async () => {
            if (!it.term.trim() || !it.description.trim()) {
                toast('先に用語と説明を書いてください。', 'warn'); return;
            }
            draftBtn.disabled = true;
            draftBtn.innerHTML = '<span class="spinner"></span> 生成中';
            try {
                const r = await api('/api/catalog/glossary/draft',
                    { db: CAT.db, table: it.scope || null,
                      terms: [{ term: it.term.trim(), description: it.description.trim() }] });
                const drafted = (r.drafted || {})[it.term.trim()];
                if (drafted) {
                    it.sql = drafted; it.verdict = null; it.detail = '';
                    markGlDirty(it);
                    glRenderList(); glRenderEditor();
                    toast('SQL式を下書きしました。「検証」で確かめてから保存してください。');
                    return;        // エディタは作り直したので、このボタンはもう無い
                }
                toast('AIが判断できませんでした。説明をもう少し具体的に書いてみてください。', 'warn');
            } catch (e) { toast(e.message, 'err'); }
            draftBtn.disabled = !CAT.llmReady;
            draftBtn.textContent = 'AIで下書き';
        },
    }, 'AIで下書き');

    const verifyBtn = el('button', { class: 'btn btn--sm', type: 'button',
        title: 'SQL式を実データに当てて確かめる',
        onclick: () => glVerify([it]) }, '検証');

    const delBtn = el('button', { class: 'btn btn--sm btn--danger', type: 'button',
        onclick: () => {
            if (!confirm(`用語「${it.term || '（無題）'}」を削除しますか？（「保存」までは確定しません）`)) return;
            const i = glItems.indexOf(it);
            glItems.splice(i, 1);
            glSelId = (glItems[i] || glItems[i - 1])?.id ?? null;
            setDirty('glossary');
            glRenderList(); glRenderEditor();
        } }, '削除');

    box.replaceChildren(el('div', { class: 'editcard' },
        el('div', { class: 'row' },
            el('div', { class: 'grow' }, el('label', { class: 'field' }, '用語'), term),
            el('div', { style: 'width:230px' },
                el('label', { class: 'field' }, '置き場所'), scopeSel),
            delBtn),
        dup,
        el('div', { class: 'mt' },
            el('label', { class: 'field' }, '説明（自然言語で構いません）'), desc),
        el('div', { class: 'mt' },
            el('div', { class: 'row', style: 'align-items:center;margin-bottom:6px' },
                el('label', { class: 'field', style: 'margin:0' },
                    'SQL条件・計算式（任意。空欄なら説明からAIが組み立てます）'),
                el('div', { class: 'spacer' }), draftBtn, verifyBtn),
            sql),
        status,
        refPanel(it.scope, sql)));
}

async function glVerify(items, btn) {
    const rows = items.filter(r => r.term.trim());
    if (!rows.length) { toast('検証する用語がありません。', 'warn'); return; }
    let orig;
    if (btn) {
        orig = btn.textContent;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> 検証中';
    }
    // 検証APIは置き場所ごとなので、まとめて呼んで結果を各行へ戻す
    const byScope = new Map();
    rows.forEach(r => {
        if (!byScope.has(r.scope)) byScope.set(r.scope, []);
        byScope.get(r.scope).push(r);
    });
    for (const [scope, list] of byScope) {
        try {
            const res = await api('/api/catalog/glossary/verify',
                { db: CAT.db, table: scope || null,
                  terms: list.map(r => ({ term: r.term, sql: r.sql })) });
            res.results.forEach((x, i) => { list[i].verdict = x.verdict; list[i].detail = x.detail; });
        } catch (e) { toast(e.message, 'err'); }
    }
    if (btn) { btn.disabled = false; btn.textContent = orig; }
    glRenderList();
    glRenderEditor();
}

/** 用語集を保存する。行を場所ごとにまとめ、空になった場所も書き戻して消す。 */
async function saveGlossary() {
    const rows = glItems.filter(r => r.term.trim());

    // 同じ場所に同じ用語が2つあると後の1つしか残らないので、先に止める
    for (const r of rows) {
        if (glDupOf(r)) {
            toast(`「${r.term.trim()}」が同じ場所（${glScopeLabel(r.scope)}）に2回あります。`
                  + '1つにまとめてください。', 'err', 8000);
            glSelect(r.id);
            return;
        }
    }

    const byScope = new Map();
    rows.forEach(r => {
        if (!byScope.has(r.scope)) byScope.set(r.scope, []);
        byScope.get(r.scope).push(r);
    });
    try {
        for (const scope of new Set([...glInitialScopes, ...byScope.keys()])) {
            const terms = (byScope.get(scope) || []).map(r =>
                ({ term: r.term.trim(), description: r.description.trim(), sql: r.sql.trim() }));
            await api('/api/catalog/glossary', { db: CAT.db, table: scope || null, terms });
            const obj = {};
            terms.forEach(t => { obj[t.term] = { description: t.description, sql: t.sql }; });
            if (scope) {
                const t = CAT.tables.find(x => x.name === scope);
                if (t) t.glossary = obj;
            } else {
                CAT.dbGlossary = obj;
            }
        }
    } catch (e) { toast(e.message, 'err'); return; }
    glInitialScopes = new Set(byScope.keys());

    // まっさらな行は片付ける。用語名が無くて保存されなかった行は消さずに知らせる
    glItems = glItems.filter(r => r.term.trim() || r.description.trim() || r.sql.trim());
    const nameless = glItems.filter(r => !r.term.trim());
    glItems.forEach(r => { if (r.term.trim()) r.dirty = false; });
    if (!glById(glSelId)) glSelId = glItems[0]?.id ?? null;
    setDirty('glossary', !!nameless.length);
    recomputeMetrics();
    glRenderList();
    glRenderEditor();
    toast(nameless.length
        ? '用語集を保存しました（用語名が空の行は保存されていません）。'
        : '用語集を保存しました。');
}

/** 説明だけ書かれた用語すべてに、AIでSQL式を一括下書きする。 */
async function draftGlossary(btn) {
    const targets = glItems.filter(r => r.term.trim() && r.description.trim() && !r.sql.trim());
    if (!targets.length) {
        toast('SQL式が空で、説明が書かれている用語がありません。', 'warn');
        return;
    }
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> 変換中';
    const byScope = new Map();
    targets.forEach(r => {
        if (!byScope.has(r.scope)) byScope.set(r.scope, []);
        byScope.get(r.scope).push(r);
    });
    let n = 0;
    for (const [scope, list] of byScope) {
        try {
            const r = await api('/api/catalog/glossary/draft',
                { db: CAT.db, table: scope || null,
                  terms: list.map(x => ({ term: x.term.trim(), description: x.description.trim() })) });
            list.forEach(x => {
                const sql = (r.drafted || {})[x.term.trim()];
                if (sql) { x.sql = sql; x.verdict = null; x.detail = ''; x.dirty = true; n++; }
            });
        } catch (e) { toast(e.message, 'err'); }
    }
    if (n) setDirty('glossary');
    glRenderList();
    glRenderEditor();
    toast(n ? `${n}件のSQL式を下書きしました。「すべて検証」で確かめてから保存してください。`
            : 'AIが判断できませんでした。説明をもう少し具体的に書いてみてください。',
          n ? 'ok' : 'warn');
    btn.disabled = false;
    btn.textContent = '説明からSQL式を下書き';
}

function wireGlossary() {
    $('#glFilter').addEventListener('input', glRenderList);
    $('#glAdd').addEventListener('click', glAdd);
    $('#glDraft').addEventListener('click', ev => draftGlossary(ev.currentTarget));
    $('#glVerifyAll').addEventListener('click', ev => glVerify(glItems, ev.currentTarget));
    $('#glSave').addEventListener('click', saveGlossary);
    $('#glList').addEventListener('keydown', ev => listArrowNav(ev, '#glList', glSelect));
    $$('.seg__btn').forEach(b => b.addEventListener('click', () => switchSec(b.dataset.sec)));
}

/* --- 例文 --------------------------------------------------------------------- */

const exById = id => exItems.find(x => x.id === id);

function markExDirty(it) { it.dirty = true; setDirty('examples'); }

function loadExamples() {
    exItems = (CAT.examples || []).map(e => ({ id: ++idSeq, q: e.q || '',
        description: e.description || '', sql: e.sql || '',
        verdict: null, detail: '', dirty: false }));
    exSelId = exItems[0]?.id ?? null;
    exRenderList();
    exRenderEditor();
}

function exRenderList() {
    const q = ($('#exFilter')?.value || '').trim().toLowerCase();
    const nodes = [];
    let shown = 0;
    exItems.forEach(it => {
        if (q && !`${it.q} ${it.description} ${it.sql}`.toLowerCase().includes(q)) return;
        shown++;
        nodes.push(el('button', {
            class: `mlist__item${it.id === exSelId ? ' is-active' : ''}`,
            type: 'button', 'data-id': it.id,
            onclick: () => exSelect(it.id),
        },
            statusDot(it),
            el('span', { class: 'mlist__q' }, it.q || '（質問未入力）'),
            it.description ? el('span', { class: 'mlist__desc' }, it.description) : null,
            dirtyDot(it)));
    });
    if (!nodes.length) {
        nodes.push(el('div', { class: 'mlist__empty' },
            exItems.length ? '絞り込みに当たる例文がありません。'
                           : 'まだ例文がありません。「＋ 例文を追加」から登録してください。'));
    }
    $('#exList').replaceChildren(...nodes);
    $('#exCount').textContent = q ? `${exItems.length}件中 ${shown}件`
                                  : `${exItems.length}件 / 上限20件`;
    $('#segExN').textContent = exItems.length;
    const add = $('#exAdd');
    add.disabled = exItems.length >= 20;
    add.title = add.disabled ? '例文は20件までです。使わないものを削除してください。' : '';
}

function exSelect(id, focusList = false) {
    exSelId = id;
    exRenderList();
    exRenderEditor();
    const btn = $(`#exList .mlist__item[data-id="${id}"]`);
    btn?.scrollIntoView({ block: 'nearest' });
    if (focusList) btn?.focus();
}

function exAdd() {
    if (exItems.length >= 20) { toast('例文は20件までです。', 'warn'); return; }
    const it = { id: ++idSeq, q: '', description: '', sql: '',
                 verdict: null, detail: '', dirty: true };
    exItems.push(it);
    exSelect(it.id);
    $('#exEditor .ed-q')?.focus();
}

function exRenderEditor() {
    const box = $('#exEditor');
    const it = exById(exSelId);
    if (!it) {
        box.replaceChildren(el('div', { class: 'editcard editcard--empty' },
            el('div', {},
                el('div', { class: 'muted mb' }, exItems.length
                    ? '左の一覧から例文を選んでください。'
                    : 'まだ例文がありません。最初の例文を登録しましょう。'),
                el('button', { class: 'btn btn--sm', type: 'button', onclick: exAdd },
                    '＋ 例文を追加'))));
        return;
    }

    const q = el('input', { type: 'text', class: 'ed-q', value: it.q,
        placeholder: '質問（例: 部門別の平均残業時間を教えて）' });
    const desc = el('input', { type: 'text', value: it.description,
        placeholder: '説明（任意。例: 残業は分で入っているので60で割る。休職者は含めない）' });
    const sql = growingSql('ed-sql', it.sql, 'SELECT ...');
    const status = el('div', { class: 'vstatus' });
    setStatus(status, it);

    q.addEventListener('input', () => {
        it.q = q.value; markExDirty(it); exRenderList();
    });
    desc.addEventListener('input', () => {
        it.description = desc.value; markExDirty(it); exRenderList();
    });
    sql.addEventListener('input', () => {
        if (it.sql === sql.value) return;
        it.sql = sql.value; it.verdict = null; it.detail = '';
        setStatus(status, it); markExDirty(it); exRenderList();
    });

    const verifyBtn = el('button', { class: 'btn btn--sm', type: 'button',
        title: 'このSQLが実際に通るか確かめる',
        onclick: () => exVerify([it]) }, '検証');

    // 例文は「日本語の質問＋確認済みのSQL」なので、そのままツールの中身になる。
    // よく聞かれる質問はツールにしておくと、AIが毎回書き起こさずに済む。
    const toolBtn = el('button', { class: 'btn btn--sm', type: 'button',
        title: 'この例文をもとに、AIが呼び出せるツールを作る',
        onclick: () => {
            if (!it.q.trim() || !it.sql.trim()) {
                toast('質問とSQLの両方が要ります。', 'warn');
                return;
            }
            openToolWizard({ purpose: it.description
                ? `${it.q}（${it.description}）` : it.q, sql: it.sql });
        } }, 'ツールにする');

    const delBtn = el('button', { class: 'btn btn--sm btn--danger', type: 'button',
        onclick: () => {
            if (!confirm('この例文を削除しますか？（「保存」までは確定しません）')) return;
            const i = exItems.indexOf(it);
            exItems.splice(i, 1);
            exSelId = (exItems[i] || exItems[i - 1])?.id ?? null;
            setDirty('examples');
            exRenderList(); exRenderEditor();
        } }, '削除');

    box.replaceChildren(el('div', { class: 'editcard' },
        el('div', { class: 'row' },
            el('div', { class: 'grow' }, el('label', { class: 'field' }, '質問'), q),
            delBtn),
        el('div', { class: 'mt' },
            el('label', { class: 'field' },
                '説明（任意。この例をどう読めばよいかをAIに伝える）'), desc),
        el('div', { class: 'mt' },
            el('div', { class: 'row', style: 'align-items:center;margin-bottom:6px' },
                el('label', { class: 'field', style: 'margin:0' },
                    'SQL（この質問への正しい答えを返すSELECT）'),
                el('div', { class: 'spacer' }), toolBtn, verifyBtn),
            sql),
        status,
        refPanel(SCOPE_DB, sql)));
}

// 例文はAIに「正しい例」として渡すので、通らないSQLが混ざると害になる
async function exVerify(items, btn) {
    const rows = items.filter(r => r.q.trim() || r.sql.trim());
    if (!rows.length) { toast('検証する例文がありません。', 'warn'); return; }
    let orig;
    if (btn) {
        orig = btn.textContent;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> 検証中';
    }
    try {
        const r = await api('/api/catalog/examples/verify',
            { db: CAT.db, examples: rows.map(x => ({ q: x.q, sql: x.sql })) });
        r.results.forEach((x, i) => { rows[i].verdict = x.verdict; rows[i].detail = x.detail; });
    } catch (e) { toast(e.message, 'err'); }
    if (btn) { btn.disabled = false; btn.textContent = orig; }
    exRenderList();
    exRenderEditor();
}

/** 例文を保存する。サーバ側で重複がまとめられたら、その結果に画面も揃える。 */
async function saveExamples() {
    let r;
    try {
        r = await api('/api/catalog/examples',
            { db: CAT.db, examples: exItems.map(x => ({ q: x.q.trim(),
                description: x.description.trim(), sql: x.sql.trim() })) });
    } catch (e) { toast(e.message, 'err'); return; }
    CAT.examples = r.examples || [];

    // サーバの確定結果で作り直す。検証結果は内容が同じ行へ引き継ぐ
    const key = x => `${x.q.trim()}\u0000${x.sql.trim()}`;
    const old = new Map(exItems.map(x => [key(x), x]));
    const selKey = exById(exSelId) ? key(exById(exSelId)) : null;
    const kept = CAT.examples.map(e => {
        const o = old.get(`${e.q}\u0000${e.sql}`);
        return { id: ++idSeq, q: e.q, description: e.description || '', sql: e.sql,
                 verdict: o?.verdict ?? null, detail: o?.detail ?? '', dirty: false };
    });
    // 質問とSQLが揃っていない行は保存されないので、書きかけを消さずに残す
    const partial = exItems.filter(x =>
        (x.q.trim() || x.sql.trim() || x.description.trim()) && !(x.q.trim() && x.sql.trim()));
    exItems = [...kept, ...partial];
    exSelId = (exItems.find(x => key(x) === selKey) || exItems[0])?.id ?? null;
    setDirty('examples', !!partial.length);
    recomputeMetrics();
    exRenderList();
    exRenderEditor();
    if (r.dropped) {
        toast(`例文を保存しました。同じSQLの重複 ${r.dropped} 件をまとめました。`, 'ok', 7000);
    } else {
        toast(partial.length
            ? '例文を保存しました（質問とSQLが揃っていない行は保存されていません）。'
            : '例文を保存しました。');
    }
}

function wireExamples() {
    loadExamples();
    $('#exFilter').addEventListener('input', exRenderList);
    $('#exAdd').addEventListener('click', exAdd);
    $('#exVerifyAll').addEventListener('click', ev => exVerify(exItems, ev.currentTarget));
    $('#exSave').addEventListener('click', saveExamples);
    $('#exList').addEventListener('keydown', ev => listArrowNav(ev, '#exList', exSelect));
}

/* --- 検算（一致するはずの2つの数字） --------------------------------------------
   登録しておくと、AIが関係するテーブルに触れるたびに自動で突き合わせ、
   食い違っていればチャットに警告が出る（verify.py）。ここはその管理画面。 */

let ckItems = [], ckSelId = null;

const ckById = id => ckItems.find(x => x.id === id);

function markCkDirty(it) { it.dirty = true; setDirty('checks'); }

function loadChecks() {
    ckItems = (CAT.checks || []).map(c => ({
        id: ++idSeq, name: c.name || '',
        left_label: (c.left || {}).label || '', left_sql: (c.left || {}).sql || '',
        right_label: (c.right || {}).label || '', right_sql: (c.right || {}).sql || '',
        tolerance_pct: c.tolerance_pct ?? 0.5,
        drilldown: c.drilldown || '', enabled: c.enabled !== false,
        verdict: null, detail: '', dirty: false }));
    ckSelId = ckItems[0]?.id ?? null;
    ckRenderList();
    ckRenderEditor();
}

function ckPayload(it) {
    return { name: it.name.trim(),
             left: { label: it.left_label.trim(), sql: it.left_sql.trim() },
             right: { label: it.right_label.trim(), sql: it.right_sql.trim() },
             tolerance_pct: Number(it.tolerance_pct) || 0,
             drilldown: it.drilldown.trim(), enabled: !!it.enabled };
}

function ckRenderList() {
    const nodes = [];
    ckItems.forEach(it => {
        nodes.push(el('button', {
            class: `mlist__item${it.id === ckSelId ? ' is-active' : ''}`,
            type: 'button', 'data-id': it.id,
            onclick: () => ckSelect(it.id),
        },
            statusDot(it),
            el('span', { class: 'mlist__term' }, it.name || '（無題）'),
            el('span', { class: 'mlist__desc' },
                it.enabled ? `${it.left_label || '左'} = ${it.right_label || '右'}` : '無効'),
            dirtyDot(it)));
    });
    if (!nodes.length) {
        nodes.push(el('div', { class: 'mlist__empty' },
            'まだ検算ルールがありません。「＋ ルールを追加」から登録してください。'));
    }
    $('#ckList').replaceChildren(...nodes);
    $('#ckCount').textContent = `${ckItems.length}件`;
    $('#segCkN').textContent = ckItems.length;
}

function ckSelect(id, focusList = false) {
    ckSelId = id;
    ckRenderList();
    ckRenderEditor();
    const btn = $(`#ckList .mlist__item[data-id="${id}"]`);
    btn?.scrollIntoView({ block: 'nearest' });
    if (focusList) btn?.focus();
}

function ckAdd() {
    const it = { id: ++idSeq, name: '', left_label: '', left_sql: '',
                 right_label: '', right_sql: '', tolerance_pct: 0.5,
                 drilldown: '', enabled: true,
                 verdict: null, detail: '', dirty: true };
    ckItems.push(it);
    ckSelect(it.id);
    $('#ckEditor .ed-name')?.focus();
}

function ckSetStatus(box, it) {
    if (!it.verdict) { box.replaceChildren(); return; }
    const cls = verdictClass(it.verdict);
    box.replaceChildren(
        el('span', { class: `badge${cls ? ' badge--' + cls : ''}` }, it.verdict),
        el('span', { class: 'small muted' }, it.detail || ''));
}

function ckRenderEditor() {
    const box = $('#ckEditor');
    const it = ckById(ckSelId);
    if (!it) {
        box.replaceChildren(el('div', { class: 'editcard editcard--empty' },
            el('div', {},
                el('div', { class: 'muted mb' },
                    '一致するはずの2つの数字を、左右のSQLで登録します。'),
                el('button', { class: 'btn btn--sm', type: 'button', onclick: ckAdd },
                    '＋ ルールを追加'))));
        return;
    }

    const bind = (input, key, opts = {}) => {
        input.addEventListener('input', () => {
            it[key] = opts.number ? input.value : input.value;
            if (opts.resetVerdict !== false) { it.verdict = null; it.detail = ''; }
            markCkDirty(it); ckRenderList();
        });
        return input;
    };

    const name = bind(el('input', { type: 'text', class: 'ed-name', value: it.name,
        placeholder: '名前（例: 入金と請求の一致）' }), 'name');
    const enabled = el('input', { type: 'checkbox',
        ...(it.enabled ? { checked: 'checked' } : {}) });
    enabled.addEventListener('change', () => {
        it.enabled = enabled.checked; markCkDirty(it); ckRenderList();
    });
    const llabel = bind(el('input', { type: 'text', value: it.left_label,
        placeholder: '左の名前（例: 入金の合計）' }), 'left_label');
    const lsql = bind(growingSql('ed-lsql', it.left_sql,
        'SELECT SUM(...) FROM ...（1行1列を返すこと）'), 'left_sql');
    const rlabel = bind(el('input', { type: 'text', value: it.right_label,
        placeholder: '右の名前（例: 請求のうち入金済み）' }), 'right_label');
    const rsql = bind(growingSql('ed-rsql', it.right_sql,
        'SELECT SUM(...) FROM ...（1行1列を返すこと）'), 'right_sql');
    const tol = bind(el('input', { type: 'number', value: it.tolerance_pct,
        min: '0', step: '0.1', style: 'width:110px' }), 'tolerance_pct',
        { number: true });
    const drill = bind(growingSql('ed-drill', it.drilldown,
        '差の内訳を出すSELECT（任意。不一致のとき警告と一緒に表示されます）'),
        'drilldown');
    const status = el('div', { class: 'vstatus' });
    ckSetStatus(status, it);

    const verifyBtn = el('button', { class: 'btn btn--sm', type: 'button',
        title: '左右のSQLをいま実行して、値と差を確かめる',
        onclick: () => ckVerify([it]) }, '検算');

    const delBtn = el('button', { class: 'btn btn--sm btn--danger', type: 'button',
        onclick: () => {
            if (!confirm(`検算ルール「${it.name || '（無題）'}」を削除しますか？（「保存」までは確定しません）`)) return;
            const i = ckItems.indexOf(it);
            ckItems.splice(i, 1);
            ckSelId = (ckItems[i] || ckItems[i - 1])?.id ?? null;
            setDirty('checks');
            ckRenderList(); ckRenderEditor();
        } }, '削除');

    box.replaceChildren(el('div', { class: 'editcard' },
        el('div', { class: 'row' },
            el('div', { class: 'grow' }, el('label', { class: 'field' }, '名前'), name),
            el('label', { class: 'check', style: 'align-self:flex-end;padding-bottom:8px' },
                enabled, el('span', {}, '有効')),
            delBtn),
        el('div', { class: 'mt' },
            el('label', { class: 'field' }, '左の数字（名前とSQL。1行1列のSELECT）'),
            llabel, el('div', { style: 'height:6px' }), lsql),
        el('div', { class: 'mt' },
            el('label', { class: 'field' }, '右の数字（左と一致するはずのもの）'),
            rlabel, el('div', { style: 'height:6px' }), rsql),
        el('div', { class: 'row mt', style: 'align-items:center' },
            el('label', { class: 'field', style: 'margin:0' }, '許容差(%)'), tol,
            el('div', { class: 'spacer' }), verifyBtn),
        el('div', { class: 'mt' },
            el('label', { class: 'field' }, '差の内訳SQL（任意）'), drill),
        status));
}

async function ckVerify(items, btn) {
    const targets = items.filter(it => it.left_sql.trim() && it.right_sql.trim());
    if (!targets.length) { toast('左右のSQLが入っているルールがありません。', 'warn'); return; }
    let orig;
    if (btn) {
        orig = btn.textContent;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> 検算中';
    }
    try {
        const r = await api('/api/catalog/checks/verify',
            { db: CAT.db, checks: targets.map(ckPayload) });
        r.results.forEach((x, i) => {
            const it = targets[i];
            if (!x.ok_run) {
                it.verdict = 'エラー';
                it.detail = x.error || '実行できませんでした。';
            } else {
                it.verdict = x.match ? '一致' : '不一致';
                const fmt = v => Number(v).toLocaleString(undefined,
                    { maximumFractionDigits: 2 });
                it.detail = `左 ${fmt(x.left)} / 右 ${fmt(x.right)}`
                    + `（差 ${fmt(x.diff)}・${x.pct}%）`;
                if (!x.match && (x.drill || {}).rows) {
                    it.detail += ` 内訳 ${x.drill.rows.length}行${x.drill.truncated ? '以上' : ''}`;
                }
            }
        });
    } catch (e) { toast(e.message, 'err'); }
    if (btn) { btn.disabled = false; btn.textContent = orig; }
    ckRenderList();
    ckRenderEditor();
}

/** 検算ルールを保存する。 */
async function saveChecks() {
    const bad = ckItems.find(it =>
        (it.name.trim() || it.left_sql.trim() || it.right_sql.trim())
        && !(it.left_sql.trim() && it.right_sql.trim()));
    if (bad) {
        toast(`「${bad.name || '（無題）'}」は左右の両方にSQLが必要です。`, 'err', 7000);
        ckSelect(bad.id);
        return;
    }
    let r;
    try {
        r = await api('/api/catalog/checks',
            { db: CAT.db, checks: ckItems.map(ckPayload) });
    } catch (e) { toast(e.message, 'err', 8000); return; }
    CAT.checks = r.checks || [];
    // サーバの確定結果で作り直す（空のルールはここで消える）
    const results = new Map(ckItems.map(it => [it.name.trim(), it]));
    loadChecks();
    ckItems.forEach(it => {
        const o = results.get(it.name);
        if (o) { it.verdict = o.verdict; it.detail = o.detail; }
    });
    setDirty('checks', false);
    ckRenderList(); ckRenderEditor();
    toast('検算ルールを保存しました。次の質問から自動で突き合わせます。');
}

function wireChecks() {
    loadChecks();
    $('#ckAdd').addEventListener('click', ckAdd);
    $('#ckVerifyAll').addEventListener('click', ev => ckVerify(ckItems, ev.currentTarget));
    $('#ckSave').addEventListener('click', saveChecks);
    $('#ckList').addEventListener('keydown', ev => listArrowNav(ev, '#ckList', ckSelect));
}

/* --- ツール ------------------------------------------------------------------ */

/* 結果の見せ方。値はサーバの render と同じ。表示だけ日本語にする。 */
const RENDER_KINDS = [
    ['table', '表'], ['chart', 'グラフ'], ['chart_dual', '2軸グラフ（棒＋折れ線）'],
    ['excel', 'Excelファイル'], ['csv', 'CSVファイル'], ['none', '出さない（AIにだけ渡す）'],
];
const PARAM_TYPES = [['string', '文字'], ['integer', '整数'],
                     ['number', '小数'], ['boolean', 'はい/いいえ']];

/* 日本語の説明から、英数字のツール名を作る。
   AIのfunction名は英数字しか使えないが、それを人に考えさせない。 */
function toolNameFrom(desc, taken) {
    const ascii = String(desc || '').toLowerCase()
        .replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
    let base = /^[a-z]/.test(ascii) ? ascii.slice(0, 40) : '';
    if (!base) base = 'tool';                 // 日本語だけの説明はここに来る
    let name = base, n = 2;
    while ((taken || []).includes(name)) name = `${base}_${n++}`;
    return name;
}

/* パラメータ1行ぶんの入力欄。 */
function paramRow(p) {
    const v = p || { name: '', type: 'string', description: '', required: true };
    const row = el('div', { class: 'prow' },
        el('input', { type: 'text', class: 'p-name', placeholder: '名前（英数字）', value: v.name }),
        el('select', { class: 'p-type' }, PARAM_TYPES.map(([k, label]) =>
            el('option', { value: k, ...(k === (v.type || 'string') ? { selected: 'selected' } : {}) }, label))),
        el('input', { type: 'text', class: 'p-desc grow',
                      placeholder: '説明（AIがここを読んで値を決めます）', value: v.description || '' }),
        el('label', { class: 'small', style: 'display:flex;gap:4px;align-items:center' },
            el('input', { type: 'checkbox', class: 'p-req',
                          ...(v.required !== false ? { checked: 'checked' } : {}) }), '必須'),
        el('button', { class: 'btn btn--sm btn--ghost', title: 'この行を消す',
                       onclick: () => row.remove() }, icon('x', 'icon--sm')));
    return row;
}

function readParams(card) {
    return $$('.prow', card).map(r => ({
        name: $('.p-name', r).value.trim(),
        type: $('.p-type', r).value,
        description: $('.p-desc', r).value.trim(),
        required: $('.p-req', r).checked,
    })).filter(p => p.name);
}

/* グラフの設定欄。種別ごとに要る項目が違うので、選ばれた種別に合わせて出し直す。
   ここが無かったせいで、見せ方に「グラフ」を選ぶと必ず保存に失敗していた。 */
function chartFields(box, kind, chart) {
    const c = chart || {};
    box.replaceChildren();
    if (kind === 'chart_dual') {
        box.append(
            el('div', { class: 'row mb' },
                el('div', { class: 'grow' },
                    el('label', { class: 'field' }, '横軸にする列'),
                    el('input', { type: 'text', class: 'ch-x', value: c.x || '' })),
                el('div', { class: 'grow' },
                    el('label', { class: 'field' }, '棒にする列（カンマ区切り）'),
                    el('input', { type: 'text', class: 'ch-bar', value: (c.bar_y || []).join(', ') })),
                el('div', { class: 'grow' },
                    el('label', { class: 'field' }, '折れ線にする列（カンマ区切り）'),
                    el('input', { type: 'text', class: 'ch-line', value: (c.line_y || []).join(', ') }))));
        return;
    }
    if (kind !== 'chart') return;
    const type = c.chart_type || 'bar';
    const sel = el('select', { class: 'ch-type' },
        Object.keys(CAT.chartFields || {}).map(k =>
            el('option', { value: k, ...(k === type ? { selected: 'selected' } : {}) }, k)));
    sel.addEventListener('change', () => chartFields(box, kind, { ...readChart(box, kind), chart_type: sel.value }));
    const need = (CAT.chartFields || {})[type] || ['x', 'y'];
    box.append(
        el('div', { class: 'row mb', style: 'align-items:flex-end' },
            el('div', { style: 'width:180px' }, el('label', { class: 'field' }, 'グラフの種類'), sel),
            ...need.map(k => el('div', { class: 'grow' },
                el('label', { class: 'field' }, `${k} にする列`),
                el('input', { type: 'text', class: `ch-f ch-${k}`, 'data-key': k,
                              value: Array.isArray(c[k]) ? c[k].join(', ') : (c[k] || '') }))),
            el('div', { class: 'grow' },
                el('label', { class: 'field' }, 'グラフの表題（任意）'),
                el('input', { type: 'text', class: 'ch-title', value: c.title || '' }))));
}

function readChart(box, kind) {
    if (kind === 'chart_dual') {
        const list = s => (s || '').split(',').map(x => x.trim()).filter(Boolean);
        return { x: $('.ch-x', box)?.value.trim() || '',
                 bar_y: list($('.ch-bar', box)?.value),
                 line_y: list($('.ch-line', box)?.value) };
    }
    if (kind !== 'chart') return {};
    const out = { chart_type: $('.ch-type', box)?.value || 'bar',
                  title: $('.ch-title', box)?.value.trim() || '' };
    $$('.ch-f', box).forEach(inp => {
        const k = inp.dataset.key, v = inp.value.trim();
        // path / dimensions は列を並べて渡す種別（treemap や散布図行列）
        out[k] = (k === 'path' || k === 'dimensions')
            ? v.split(',').map(x => x.trim()).filter(Boolean) : v;
    });
    return out;
}

function toolCard(tool) {
    const t = tool || { name: '', description: '', sql: '', parameters: [],
                        render: 'table', chart: {}, enabled: true };
    const original = t.name;
    // 定義が置かれているDB。一覧は全DBぶん出すので、開いているDBとは限らない
    const ownerFile = t.owner_file || CAT.db;

    const params = el('div', {}, (t.parameters || []).map(paramRow));
    const chartBox = el('div', { class: 'mt' });
    const renderSel = el('select', { class: 'tl-render' }, RENDER_KINDS.map(([k, label]) =>
        el('option', { value: k, ...(k === t.render ? { selected: 'selected' } : {}) }, label)));
    renderSel.addEventListener('change', () => chartFields(chartBox, renderSel.value, t.chart));

    const card = el('details', { class: 'acc', ...(tool ? {} : { open: 'open' }) },
        el('summary', {},
            el('strong', {}, t.name || '（新しいツール）'),
            t.owner_file ? el('span', { class: 'small muted', style: 'margin-left:8px' },
                              '保存先: ' + t.owner_file) : null,
            t.enabled === false ? el('span', { class: 'badge badge--warn' }, '無効') : null),
        el('div', { class: 'acc__body' },
            el('div', { class: 'row mb' },
                el('div', { class: 'grow' },
                    el('label', { class: 'field' }, '説明（AIが使うかどうかの判断材料）'),
                    el('input', { type: 'text', class: 'tl-desc', value: t.description })),
                el('div', { style: 'width:200px' },
                    el('label', { class: 'field' }, '結果の見せ方'), renderSel)),
            chartBox,
            el('div', { class: 'row mt', style: 'align-items:center' },
                el('label', { class: 'field', style: 'margin:0' }, 'パラメータ（毎回変えられる値）'),
                el('div', { class: 'spacer' }),
                el('button', { class: 'btn btn--sm',
                               onclick: () => params.append(paramRow(null)) }, '＋ 追加')),
            params,
            el('details', { class: 'mt' },
                el('summary', { class: 'small muted', style: 'cursor:pointer' }, '詳しい設定（SQL・ツール名）'),
                el('div', { class: 'mt' },
                    el('label', { class: 'field' }, 'SQL（パラメータは :名前 で書く）'),
                    el('textarea', { class: 'tl-sql mono', rows: '5' }, t.sql || ''),
                    el('label', { class: 'field mt' }, 'ツール名（英数字と_。空なら自動で付けます）'),
                    el('input', { type: 'text', class: 'tl-name', value: t.name }))),
            el('div', { class: 'row mt' },
                el('label', { style: 'display:flex;gap:6px;align-items:center;font-size:12.5px' },
                    el('input', { type: 'checkbox', class: 'tl-enabled',
                        ...(t.enabled !== false ? { checked: 'checked' } : {}) }), '有効'),
                el('div', { class: 'spacer' }),
                el('button', {
                    class: 'btn btn--sm',
                    title: '実際のデータで動かして、結果を確かめます',
                    onclick: ev => tryTool(readTool(card, original), ev.target, null, ownerFile),
                }, '試す'),
                tool ? el('button', {
                    class: 'btn btn--sm btn--danger',
                    onclick: async () => {
                        if (!confirm(`${t.name} を削除しますか？`)) return;
                        await api('/api/catalog/tool', { db: ownerFile, action: 'delete', name: t.name });
                        toast('削除しました。'); reloadClean();
                    },
                }, '削除') : null,
                el('button', {
                    class: 'btn btn--primary btn--sm',
                    onclick: async () => {
                        const payload = readTool(card, original);
                        try {
                            await api('/api/catalog/tool',
                                { db: ownerFile, tool: payload, name: payload.name, original });
                            toast('保存しました。'); reloadClean();
                        } catch (e) { toast(e.message, 'err'); }
                    },
                }, '保存'))));

    chartFields(chartBox, t.render, t.chart);
    return card;
}

/** 編集欄の中身を、保存できる形にまとめる。 */
function readTool(card, original) {
    const render = $('.tl-render', card).value;
    const desc = $('.tl-desc', card).value.trim();
    const typed = $('.tl-name', card).value.trim();
    const taken = (CAT.custom || []).map(x => x.name).filter(n => n !== original);
    const out = {
        name: typed || toolNameFrom(desc, taken),
        description: desc,
        sql: $('.tl-sql', card).value,
        parameters: readParams(card),
        render,
        enabled: $('.tl-enabled', card).checked,
    };
    if (render === 'chart' || render === 'chart_dual') out.chart = readChart(card, render);
    return out;
}

/* --- 試し実行 ------------------------------------------------------------------
   SQLを読めない人に「合っているか」を判断してもらうには、実際に出てくる表を
   見せるのがいちばん早い。結果はその場に出す。 */

function resultTable(res) {
    if (!res.ok) {
        return el('div', { class: 'alert alert--err small' },
            'うまく動きませんでした: ' + (res.error || '原因不明'));
    }
    const box = el('div', {});
    if (res.note) box.append(el('div', { class: 'alert alert--warn small' }, res.note));
    if ((res.problems || []).length) {
        box.append(el('div', { class: 'alert alert--warn small' },
            '保存の前に直すところ: ' + res.problems.join(' / ')));
    }
    if ((res.cross || []).length) {
        box.append(el('div', { class: 'small muted mb' },
            `${res.cross.join('、')} のデータも使っています。`
            + 'チャットではこれらのDBも一緒に選ぶ必要があります。'));
    }
    if (res.columns?.length) {
        box.append(dataTable(res.columns, res.rows || []));
        box.append(el('div', { class: 'small muted mt' },
            res.rows?.length ? `先頭 ${res.rows.length} 行です。` : '行はありませんでした。'));
    }
    return box;
}

async function tryTool(payload, btn, into, dbFile) {
    const target = into || (() => {
        // 押したボタンの近くに結果を出す。無ければ作る
        const card = btn.closest('.acc__body') || btn.parentElement;
        let box = $('.tl-result', card);
        if (!box) { box = el('div', { class: 'tl-result mt' }); card.append(box); }
        return box;
    })();
    target.replaceChildren(el('div', { class: 'small muted' }, '試しています…'));
    if (btn) btn.disabled = true;
    try {
        const res = await api('/api/catalog/tool/try', { db: dbFile || CAT.db, tool: payload });
        target.replaceChildren(resultTable(res));
        return res;
    } catch (e) {
        target.replaceChildren(el('div', { class: 'alert alert--err small' }, e.message));
        return { ok: false, error: e.message };
    } finally {
        if (btn) btn.disabled = false;
    }
}

/* --- 日本語だけで作る ------------------------------------------------------------
   SQLを書かずにツールを作るための入口。日本語で目的を書いてもらい、
   AIにSQLを起こさせ、その場で実データに当てて結果を見せてから保存する。 */

function openToolWizard(seed) {
    const back = el('div', { class: 'modal', id: 'toolWiz' });
    const close = () => back.remove();
    back.addEventListener('click', ev => { if (ev.target === back) close(); });

    const purpose = el('textarea', { rows: '3', style: 'width:100%',
        placeholder: '例: 指定した年の月別売上を、部署ごとに出す' }, seed?.purpose || '');

    const out = el('div', { class: 'mt' });
    let drafted = null;
    // 保存先の .meta.yaml。SQLが主に見ているDBをサーバが決める（作る人は選ばない）
    let homeDb = CAT.db;

    const saveBtn = el('button', { class: 'btn btn--primary btn--sm', disabled: 'disabled',
        onclick: async () => {
            try {
                await api('/api/catalog/tool',
                    { db: homeDb, tool: drafted, name: drafted.name, original: '' });
                close(); toast('ツールを作りました。'); reloadClean();
            } catch (e) { toast(e.message, 'err', 9000); }
        } }, 'この内容で作る');

    const makeBtn = el('button', { class: 'btn btn--primary btn--sm', onclick: async () => {
        const text = purpose.value.trim();
        if (!text) return toast('何をするツールかを書いてください。', 'warn');
        makeBtn.disabled = true; saveBtn.disabled = true;
        out.replaceChildren(el('div', { class: 'small muted' },
            'AIがSQLを起こして、実際のデータで確かめています…'));
        try {
            // db は送らない。全DBのカタログを見て、AIがどのDBを使うか決める
            const res = await api('/api/catalog/tool/draft', {
                purpose: text, render: 'table' });
            drafted = res.tool;
            if (res.home_db) homeDb = res.home_db;
            out.replaceChildren();
            if (!res.ok) {
                out.append(el('div', { class: 'alert alert--err small' },
                    'うまく作れませんでした: ' + (res.error || '原因不明')
                    + '　やりたいことをもう少し具体的に書き直して、もう一度お試しください。'));
            } else {
                out.append(el('div', { class: 'alert alert--ok small' },
                    'できました。下の内容で作ります。'));
                // 何ができたかを先に見せる。SQLを読めなくても、決まった内容と
                // 実際に出た行を見れば「これでいい」と判断できる。
                out.append(draftSummary(drafted));
                out.append(el('div', { class: 'small muted mt' },
                    '実際のデータで動かした結果（先頭のみ）:'));
                out.append(resultTable({ ok: true, columns: res.columns, rows: res.rows }));
                if (!(res.rows || []).length) {
                    out.append(el('div', { class: 'alert alert--warn small mt' },
                        '動きましたが0行でした。条件が厳しいだけかもしれません。'
                        + '中身を確かめてから保存してください。'));
                }
                saveBtn.disabled = false;
            }
            // 起こした中身は必ず見せる。保存前に人が直せるようにする
            if (drafted) out.append(draftDetail(drafted, res.ok ? null : out));
        } catch (e) {
            out.replaceChildren(el('div', { class: 'alert alert--err small' }, e.message));
        }
        makeBtn.disabled = false;
    } }, 'AIに作ってもらう');

    /* AIが決めたことを、SQLを読まなくても確かめられる形で見せる。 */
    function draftSummary(t) {
        const ps = t.parameters || [];
        return el('div', { class: 'card mt', style: 'padding:10px 12px' },
            el('div', { class: 'small' },
                el('b', {}, 'このツールがすること: '), t.description || ''),
            ps.length
                ? el('div', { class: 'small mt' },
                    el('b', {}, '毎回変えられる値: '),
                    ps.map(p => `${p.description || p.name}`).join('、'),
                    el('div', { class: 'small muted', style: 'margin-top:2px' },
                        '下の結果は ' + ps.map(p =>
                            `${p.description || p.name}=「${p.example ?? ''}」`).join('、')
                        + ' で試した結果です。AIが呼ぶときは質問に合わせて値を入れます。'))
                : el('div', { class: 'small muted mt' },
                    '毎回変える値はありません（いつも同じ条件で返します）。'),
            el('div', { class: 'small muted mt' },
                `定義の保存先: ${homeDb}（SQLが主に見ているDB。使うときに意識する必要はありません）`));
    }

    /* 起こした中身をその場で直せるようにする。ふつうは開かなくてよい。 */
    function draftDetail(t, retryInto) {
        const sql = el('textarea', { class: 'mono', rows: '6', style: 'width:100%' }, t.sql || '');
        const desc = el('input', { type: 'text', style: 'width:100%', value: t.description || '' });
        sql.addEventListener('input', () => { drafted.sql = sql.value; });
        desc.addEventListener('input', () => { drafted.description = desc.value; });
        return el('details', { class: 'mt', ...(retryInto ? { open: 'open' } : {}) },
            el('summary', { class: 'small muted', style: 'cursor:pointer' },
                '中身を見る・直す（ふつうは不要）'),
            el('div', { class: 'mt' },
                el('label', { class: 'field' }, 'AIに渡す説明'), desc,
                el('label', { class: 'field mt' }, 'SQL'), sql,
                el('div', { class: 'row mt' },
                    el('button', { class: 'btn btn--sm', onclick: async ev => {
                        const res = await tryTool(drafted, ev.target, retryBox, homeDb);
                        saveBtn.disabled = !res.ok;
                    } }, 'この内容で試す')),
                retryBox));
    }
    const retryBox = el('div', { class: 'mt' });

    back.append(el('div', { class: 'modal__box' },
        el('div', { class: 'modal__head' },
            el('b', { class: 'grow' }, '日本語でツールを作る'),
            el('button', { class: 'btn btn--sm btn--ghost', onclick: close }, icon('x', 'icon--sm'))),
        el('div', { class: 'modal__body', style: 'padding:12px 14px' },
            el('div', { class: 'small muted mb' },
                'やりたいことを日本語で書くだけです。SQLも設定も要りません。'
                + 'AIがSQLを組み立て、毎回変える値があればそれも自分で見つけ、'
                + '実際のデータで動くところまで確かめてから作ります。'),
            el('label', { class: 'field' }, 'このツールは何をする？'),
            purpose,
            el('div', { class: 'small muted', style: 'margin-top:4px' },
                '例:「指定した年の月別売上を出す」「ある部署の残業時間の多い順に社員を並べる」。'
                + '「指定した」「ある〇〇の」と書けば、そこが毎回変えられる値になります。'),
            el('div', { class: 'row mt' }, el('div', { class: 'spacer' }), makeBtn),
            out),
        el('div', { class: 'modal__foot row', style: 'align-items:center' },
            el('div', { class: 'spacer' }),
            el('button', { class: 'btn btn--sm', onclick: close }, 'やめる'),
            saveBtn)));
    document.body.append(back);
    purpose.focus();
    // 例文から来たときは、検証済みのSQLをそのまま使う。AIに書き直させると
    // 通っていたSQLが別物になりかねないし、LLMの呼び出しも無駄になる。
    // いまのデータで通らなくなっていたときだけ、「AIに作ってもらう」に切り替えてもらう。
    if (seed?.sql) {
        drafted = {
            name: toolNameFrom(seed.purpose,
                (CAT.custom || []).map(x => x.name)
                    .concat((CAT.builtin || []).map(b => b.name))),
            description: seed.purpose, sql: seed.sql,
            parameters: [], render: 'table', enabled: true,
        };
        out.replaceChildren(el('div', { class: 'small muted' },
            '例文のSQLを実際のデータで確かめています…'));
        (async () => {
            try {
                const res = await api('/api/catalog/tool/try', { db: CAT.db, tool: drafted });
                out.replaceChildren();
                if (res.ok) {
                    out.append(el('div', { class: 'alert alert--ok small' },
                        '例文の検証済みSQLをそのまま使います。実際のデータで動かした結果です。'));
                    out.append(resultTable(res));
                    saveBtn.disabled = false;
                } else {
                    out.append(el('div', { class: 'alert alert--warn small' },
                        '例文のSQLが、いまのデータでは通りませんでした: '
                        + (res.error || '') + '　「AIに作ってもらう」で作り直せます。'));
                }
                out.append(draftDetail(drafted, res.ok ? null : out));
            } catch (e) {
                out.replaceChildren(el('div', { class: 'alert alert--err small' }, e.message));
            }
        })();
    } else if (seed?.purpose) {
        makeBtn.click();
    }
}

function wireTools() {
    const list = $('#toolList');
    list.replaceChildren(...CAT.custom.map(toolCard));
    if (!CAT.custom.length) list.append(el('div', { class: 'small muted' }, 'まだありません。'));
    $('#toolWizard')?.addEventListener('click', () => openToolWizard(null));

    renderBuiltins();
    $('#btFilter').addEventListener('input', renderBuiltins);
}

/* --- 組み込みツール（中身を見る） -------------------------------------------------
   AIに渡している JSON Schema をそのまま読める形にして出す。
   説明が長いものが多く、パラメータは今まで一切見えていなかった。 */

function builtinCard(b) {
    const ov = CAT.builtinOverrides[b.name] || {};
    const off = ov.enabled === false;

    const head = el('summary', {},
        el('code', {}, b.name),
        off ? el('span', { class: 'badge badge--warn' }, '無効') : null,
        b.is_sql ? el('span', { class: 'badge' }, 'SQL') : null,
        ov.description ? el('span', { class: 'badge badge--accent' }, '説明を上書き中') : null);

    // AIが読んでいる説明。上書きがあればそちらが実際に使われる
    const body = el('div', { class: 'acc__body' },
        el('div', { class: 'small muted' }, 'AIに渡している説明'),
        el('div', { class: 'toolblock mt' },
            el('pre', { class: 'mono', style: 'white-space:pre-wrap' },
                ov.description || b.description)),
        ov.description
            ? el('div', { class: 'small muted mt' },
                `元の説明: ${b.description}`)
            : null);

    if (b.params.length) {
        body.append(el('div', { class: 'small muted mt' }, 'パラメータ'),
            dataTable(['名前', '型', '必須', '説明'],
                b.params.map(p => [
                    p.name,
                    p.type + (p.enum.length ? `（${p.enum.join(' / ')}）` : ''),
                    p.required ? '必須' : '',
                    p.description,
                ])));
    } else {
        body.append(el('div', { class: 'small muted mt' }, 'パラメータはありません。'));
    }

    // 実装コード。重いので開いたときに初めて取りに行く
    const srcAcc = el('details', { class: 'acc mt' },
        el('summary', {}, '実装コードを見る（このツールが実際に何をするか）'),
        el('div', { class: 'acc__body', 'data-src-body': '1' },
            el('div', { class: 'small muted' },
                el('span', { class: 'spinner' }), ' 読み込み中...')));
    srcAcc.addEventListener('toggle', async () => {
        if (!srcAcc.open || srcAcc.dataset.loaded) return;
        srcAcc.dataset.loaded = '1';
        const box = $('[data-src-body]', srcAcc);
        try {
            const r = await api(`/api/catalog/builtin/source?name=${encodeURIComponent(b.name)}`,
                                undefined, 'GET');
            box.replaceChildren(...r.parts.map(p =>
                el('div', { class: 'toolblock mt' },
                    el('div', { class: 'toolblock__head' },
                        el('span', {}, p.label),
                        el('span', { class: 'muted small' }, `— ${p.where}`)),
                    el('pre', { class: 'mono' }, p.code))));
            if (!r.parts.length) {
                box.replaceChildren(el('div', { class: 'small muted' }, 'コードを取得できませんでした。'));
            }
        } catch (e) {
            srcAcc.dataset.loaded = '';
            box.replaceChildren(el('div', { class: 'alert alert--err' }, e.message));
        }
    });
    body.append(srcAcc);

    // 編集は補助。まず中身が読めることを優先し、操作は下にまとめる
    body.append(el('div', { class: 'row mt', style: 'align-items:center' },
        el('label', { class: 'check' },
            el('input', { type: 'checkbox', class: 'bt-en', ...(off ? {} : { checked: 'checked' }) }),
            el('span', {}, 'このツールをAIに渡す')),
        el('div', { class: 'grow' },
            el('input', { type: 'text', class: 'bt-desc',
                value: ov.description || '',
                placeholder: '説明を上書きする（空欄なら上の元の説明を使う）' })),
        el('button', {
            class: 'btn btn--sm',
            onclick: async ev => {
                const card = ev.target.closest('details');
                try {
                    await api('/api/catalog/builtin', {
                        db: CAT.db, name: b.name,
                        enabled: $('.bt-en', card).checked,
                        description: $('.bt-desc', card).value,
                    });
                    // 画面内の控えも合わせる（開き直さずにバッジを正しくする）
                    CAT.builtinOverrides[b.name] = {
                        enabled: $('.bt-en', card).checked,
                        description: $('.bt-desc', card).value.trim(),
                    };
                    const open = card.open;
                    card.replaceWith(builtinCard(b));
                    $(`#builtinList details[data-tool="${b.name}"]`).open = open;
                    toast(`${b.name} を保存しました。`);
                } catch (e) { toast(e.message, 'err'); }
            },
        }, '保存')));

    return el('details', { class: 'acc', 'data-tool': b.name }, head, body);
}

function renderBuiltins() {
    const q = ($('#btFilter')?.value || '').trim().toLowerCase();
    const hit = CAT.builtin.filter(b =>
        !q || `${b.name} ${b.description}`.toLowerCase().includes(q));
    $('#builtinList').replaceChildren(...hit.map(builtinCard));
    $('#btCount').textContent = q
        ? `${CAT.builtin.length}件中 ${hit.length}件`
        : `${CAT.builtin.length}件`;
}

/* --- DB情報・その他 ---------------------------------------------------------- */

async function saveInfo() {
    try {
        await api('/api/catalog/overview', {
            db: CAT.db, title: $('#dbTitle').value, description: $('#dbDesc').value,
        });
    } catch (e) { toast(e.message, 'err'); return; }
    setDirty('info', false);
    toast('保存しました。');
}

function wireMisc() {
    $('#infoSave')?.addEventListener('click', saveInfo);
    $('#dbInfo')?.addEventListener('input', () => setDirty('info'));

    // DBの切り替え。未保存があるときだけ確認してから移動する
    $('#dbSel')?.addEventListener('change', ev => {
        if (dirtyLabel() && !confirm(`未保存の変更（${dirtyLabel()}）があります。破棄してDBを切り替えますか？`)) {
            ev.target.value = CAT.db;
            return;
        }
        leavingOnPurpose = true;
        location.href = `?db=${encodeURIComponent(ev.target.value)}${carriedHash()}`;
    });
    $$('.sug-add').forEach(b => b.addEventListener('click', async () => {
        const [ft, fc] = b.dataset.from.split('.');
        const parts = b.dataset.to.split('.');
        // 別DBへの関連は「DB名.テーブル.列」の3要素。DB名を落とすと
        // 同名の自DBテーブルを指してしまうので、テーブル名に含めたまま渡す
        const [tt, tc] = parts.length === 3
            ? [`${parts[0]}.${parts[1]}`, parts[2]] : parts;
        await ER.mutate({ action: 'add', from_table: ft, from_column: fc,
                          to_table: tt, to_column: tc, cardinality: b.dataset.card });
        toast('関連を登録しました。');
        b.closest('.row').remove();
    }));

    // 充実度の数字から該当タブへ飛ぶ。「テーブル説明」は未記入だけに絞って開く
    $$('.metric[data-jump]').forEach(m => m.addEventListener('click', () => {
        activateTab(m.dataset.jump);
        if (m.dataset.sec) switchSec(m.dataset.sec);
        if (m.dataset.filter === 'missing' && !missingOnly) {
            missingOnly = true;
            $('#tblMissing').classList.add('btn--primary');
            applyTableFilter();
        }
    }));

    $('#saveAll')?.addEventListener('click', saveAllDirty);

    // Ctrl+S = いま見ているタブの内容を保存（ブラウザの保存ダイアログは出さない）
    document.addEventListener('keydown', ev => {
        if (!(ev.ctrlKey || ev.metaKey) || ev.key.toLowerCase() !== 's') return;
        ev.preventDefault();
        if (dirtyLabel()) saveAllDirty();
        else toast('未保存の変更はありません。');
    });
}

document.addEventListener('DOMContentLoaded', () => {
    wireTables();
    wireManage();
    wireTableFilter();
    loadGlossaryAll();
    wireGlossary();
    wireExamples();
    wireChecks();
    ER.init();
    wireTabs();          // ハッシュのタブ復元は ER.init の後（er タブ復元時に refit するため）
    wireTools();
    wireMisc();

    // 過去の分析で実際に使われた結合を、ER図に重ねる（読み込みはページと非同期）
    api(`/api/catalog/usage?db=${encodeURIComponent(CAT.db)}`, undefined, 'GET')
        .then(r => ER.setUsage(r.edges || {}))
        .catch(() => {});          // 取れなくてもER図自体は使える
});
