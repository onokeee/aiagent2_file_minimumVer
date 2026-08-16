/* データ取り込み画面。プレビュー 列の設定 取り込み先 実行 / 定期登録。 */

let plan = [];          // 列の設定
let previewInfo = null;
// いま選ばれている取り込み元。サーバのファイル（path）か、アップロード（upload）のどちらか。
let source = null;      // {kind:'server'|'upload', path?, upload?, name}

function readOptions() {
    return {
        path: source?.kind === 'server' ? source.path : '',
        upload: source?.kind === 'upload' ? source.upload : null,
        sheet: $('#sheetWrap').classList.contains('hidden') ? null : $('#sheet').value,
        header_row: Math.max(0, parseInt($('#headerRow').value || '1', 10) - 1),
        delimiter: $('#delimiter').value,
    };
}

/* --- 取り込み元フォルダの管理 --------------------------------------------------- */

async function loadDirs() {
    const r = await api('/api/import/dirs', undefined, 'GET');
    const box = $('#dirList');
    box.replaceChildren(...r.dirs.map(d => el('div', {
        class: 'row', style: 'align-items:center;gap:8px;padding:5px 0;'
            + 'border-bottom:1px solid var(--border)',
    },
        el('span', { class: d.ok ? 'badge badge--ok': 'badge badge--err' }, d['状態']),
        el('code', { class: 'grow', title: d['実際のパス'] }, d['設定値']),
        el('span', { class: 'badge' }, d.source === 'env'? '.env': '画面から追加'),
        (r.editable && d.removable) ? el('button', {
            class: 'btn btn--sm btn--danger',
            onclick: async () => {
                if (!confirm(`${d['設定値']} を取り込み元から外しますか？\n（フォルダ自体は削除されません）`)) return;
                await api('/api/import/dirs', { action: 'remove', path: d['設定値'] });
                toast('取り込み元から外しました。');
                loadDirs();
            },
        }, '外す') : null)));
    if (!r.dirs.length) box.append(el('div', { class: 'small muted' }, '登録がありません。'));
}

function wireDirs() {
    const add = $('#addDir');
    if (!add) return;
    const submit = async () => {
        const v = $('#newDir').value.trim();
        if (!v) return;
        add.disabled = true;
        try {
            await api('/api/import/dirs', { action: 'add', path: v });
            $('#newDir').value = '';
            toast('取り込み元フォルダを追加しました。');
            loadDirs();
        } catch (e) { toast(e.message, 'err', 8000); }
        add.disabled = false;
    };
    add.addEventListener('click', submit);
    $('#newDir').addEventListener('keydown', ev => { if (ev.key === 'Enter') submit(); });
}

/* --- サーバのフォルダを辿るダイアログ -------------------------------------------- */

async function openBrowser(path) {
    $('#browser').classList.remove('hidden');
    const list = $('#browserList');
    list.replaceChildren(el('div', { class: 'fsrow' }, el('span', { class: 'spinner' }), '読み込み中...'));
    let r;
    try {
        r = await api('/api/import/browse', { path: path || null });
    } catch (e) {
        list.replaceChildren(el('div', { class: 'alert alert--err' }, e.message));
        return;
    }

    $('#crumbs').replaceChildren(...r.crumbs.flatMap((c, i) => [
        i ? el('span', { class: 'muted' }, '/') : null,
        el('button', { onclick: () => openBrowser(c.path) }, c.name),
    ]).filter(Boolean));
    if (!r.crumbs.length) $('#crumbs').replaceChildren(el('span', { class: 'muted' }, '取り込み元フォルダ'));

    const rows = [];
    if (r.parent) {
        rows.push(el('div', { class: 'fsrow', onclick: () => openBrowser(r.parent) },
            icon('back', 'icon--sm'), el('span', { class: 'name' }, '上のフォルダへ')));
    }
    r.dirs.forEach(d => rows.push(el('div', { class: 'fsrow', onclick: () => openBrowser(d.path) },
        icon('folder', 'icon--sm'), el('span', { class: 'name' }, d.name))));
    r.files.forEach(f => rows.push(el('div', {
        class: 'fsrow',
        onclick: () => { chooseServerFile(f.path, f.name); closeBrowser(); },
    },
        icon('file', 'icon--sm'), el('span', { class: 'name' }, f.name),
        el('span', { class: 'meta' }, `${(f.size / 1024).toFixed(0)} KB ・ ${f.mtime}`))));
    if (!rows.length) rows.push(el('div', { class: 'small muted', style: 'padding:12px' },
        'このフォルダには取り込めるファイルがありません。'));
    list.replaceChildren(...rows);
}

function closeBrowser() { $('#browser').classList.add('hidden'); }

/* --- 選択の確定 ----------------------------------------------------------------- */

function showChosen(icon, label, note) {
    $('#chosen').replaceChildren(el('div', { class: 'chosenfile' },
        el('span', {}, icon),
        el('div', { class: 'grow' },
            el('div', { style: 'font-weight:700' }, label),
            note ? el('div', { class: 'small muted' }, note) : null)));
    $('#readOpts').classList.remove('hidden');
}

function chooseServerFile(path, name) {
    source = { kind: 'server', path, name };
    showChosen('', name, path);
    loadPreview();
}

async function chooseLocalFile(file) {
    const fd = new FormData();
    fd.append('file', file);
    showChosen('', file.name, 'アップロード中...');
    try {
        const res = await fetch('/api/import/upload', { method: 'POST', body: fd });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'アップロードに失敗しました');
        source = { kind: 'upload', upload: data.upload, name: data.name };
        showChosen('', data.name,
            `自分のPCから（${(data.size / 1024).toFixed(0)} KB）・定期取り込みには登録できません`);
        loadPreview();
    } catch (e) {
        toast(e.message, 'err', 8000);
        $('#chosen').replaceChildren(el('div', { class: 'alert alert--err' }, e.message));
    }
}

/* --- プレビュー --------------------------------------------------------------- */

async function loadPreview() {
    if (!source) return;
    const area = $('#previewArea');
    area.replaceChildren(el('div', { class: 'card' },
        el('span', { class: 'spinner' }), '読み込み中...'));
    try {
        const r = await api('/api/import/preview', readOptions());
        previewInfo = r;
        plan = r.plan.map(p => ({
            source: p['元の列名'], name: p['列名'], type: p['型'], include: true,
        }));
        // Excel ならシート欄を出す
        const has = (r.sheets || []).length > 0;
        $('#sheetWrap').classList.toggle('hidden', !has);
        $('#sepWrap').classList.toggle('hidden', has);
        if (has && $('#sheet').options.length !== r.sheets.length) {
            $('#sheet').replaceChildren(...r.sheets.map(s => el('option', {}, s)));
        }
        renderPreview(r);
    } catch (e) {
        area.replaceChildren(el('div', { class: 'alert alert--err' }, e.message));
    }
}

function renderPreview(r) {
    const dbSelect = el('select', { id: 'dbTarget' },
        el('option', { value: '' }, '＋ 新しいDBを作る'),
        IMP.dbFiles.map(f => el('option', { value: f }, f)));

    const area = $('#previewArea');
    area.replaceChildren(
        el('div', { class: 'card' },
            el('div', { class: 'card__title' }, 'プレビュー'),
            el('div', { class: 'card__desc' },
                `先頭 ${Math.min(30, r.rows.length)} 行 / 読み込んだ ${r.scanned.toLocaleString()} 行から型を推定しています。`
                + '　いちばん右の取得日時は取り込み時に自動で追加される列です。'),
            el('div', { id: 'previewTable' })),

        el('div', { class: 'card' },
            el('div', { class: 'card__title' }, '列の設定'),
            el('div', { id: 'planTable' })),

        el('div', { class: 'card' },
            el('div', { class: 'card__title' }, '取り込み先'),
            el('div', { class: 'row mb' },
                el('div', { style: 'width:240px' }, el('label', { class: 'field' }, '取り込み先のDB'), dbSelect),
                el('div', { id: 'newDbWrap', style: 'width:240px' },
                    el('label', { class: 'field' }, '新しいDBの名前'),
                    el('input', { type: 'text', id: 'dbName', value: r.suggest_db })),
                el('div', { style: 'width:240px' },
                    el('label', { class: 'field' }, 'テーブル名'),
                    el('input', { type: 'text', id: 'tableName', value: r.suggest_table }))),
            el('div', { class: 'row mb' },
                el('div', { style: 'width:320px' },
                    el('label', { class: 'field' }, '更新のしかた'),
                    el('select', { id: 'mode', onchange: syncMode },
                        Object.entries(IMP.modes).map(([k, v]) =>
                            el('option', { value: k }, v)))),
                el('div', { style: 'width:240px' },
                    el('label', { class: 'field' },
                        '取得日時の列名 ', el('span', { class: 'badge badge--err' }, '必須')),
                    el('input', { type: 'text', id: 'tsCol', value: IMP.defaultTs,
                        oninput: renderColumnPlan })),
                el('div', { id: 'keepWrap', class: 'hidden', style: 'width:220px' },
                    el('label', { class: 'field' },
                        '保存回数 ', el('span', { class: 'badge badge--err' }, '必須')),
                    el('input', { type: 'number', id: 'keepRuns', value: '',
                        placeholder: `1〜${IMP.maxKeep}`,
                        min: '1', max: String(IMP.maxKeep) }))),
            el('div', { class: 'small muted mb' },
                '取得日時の列は更新の仕方によらず必ず追加され、取り込んだ日時が入ります。'),
            el('div', { id: 'appendNote', class: 'hidden small muted mb' },
                `追記では、取り込み1回ぶんを「1回」と数え、新しい方から最大 ${IMP.maxKeep} 回分まで保持できます。`
                + '上限を超えた古い回は取り込みのたびに自動で削除されます'
                + '（取得日時が入っていない既存の行は消しません）。'),
            el('div', { id: 'destNote' }),
            el('div', { class: 'row mt' },
                el('button', { class: 'btn btn--primary', id: 'runImport' }, 'いま取り込む'),
                el('div', { class: 'spacer' }))),

        el('details', { class: 'acc' },
            el('summary', {}, 'この設定を定期取り込みに登録する'),
            el('div', { class: 'acc__body' },
                source?.kind === 'upload'
                    ? el('div', { class: 'alert alert--warn' },
                        'アップロードしたファイルは定期取り込みに登録できません。'
                        + 'サーバ上に残らないため、次回以降読み直せないからです。'
                        + '繰り返し取り込むなら、取り込み元フォルダに置いてから選び直してください。')
                    : null,
                el('div', { class: 'row' },
                    el('div', { class: 'grow' },
                        el('label', { class: 'field' }, '設定の名前'),
                        el('input', { type: 'text', id: 'jobName',
                            value: `${r.suggest_db} ${r.suggest_table}` })),
                    el('div', { style: 'width:210px' },
                        el('label', { class: 'field' }, '開始日時'),
                        el('input', { type: 'datetime-local', id: 'jobStart' })),
                    el('div', { style: 'width:170px' },
                        el('label', { class: 'field' }, '更新間隔'),
                        el('select', { id: 'jobInterval' },
                            IMP.intervals.map(i => el('option',
                                { ...(i === '1日ごと'? { selected: 'selected' } : {}) }, i)))),
                    el('button', { class: 'btn btn--sm', id: 'saveJob' }, '登録する')),
                el('div', { class: 'small muted mt' },
                    '開始日時を入れると、その時刻を過ぎるまで自動実行されません（空なら登録後すぐ対象）。'
                    + '「更新」での手動実行は開始日時に関係なくいつでもできます。'))));

    $('#dbTarget').addEventListener('change', syncDest);
    $('#tableName').addEventListener('input', syncDest);
    $('#runImport').addEventListener('click', runImport);
    $('#saveJob').addEventListener('click', saveJob);
    // 開始日時は過去を選べないようにする（分単位で今から）
    const jobStart = $('#jobStart');
    if (jobStart) jobStart.min = localNow();
    renderColumnPlan();
    syncMode(); syncDest();
}

/** datetime-local に入れる「今」。ローカル時刻の YYYY-MM-DDTHH:MM。 */
function localNow(offsetMinutes = 0) {
    const d = new Date(Date.now() + offsetMinutes * 60000);
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
         + `T${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 取得日時の列名。空なら既定値。 */
function tsName() {
    return ($('#tsCol')?.value || '').trim();
}

/** プレビューと「列の設定」を描き直す。取得日時の列も混ぜて見せる。 */
function renderColumnPlan() {
    const ts = tsName();
    const stamp = localNow().replace('T', ' ') + ':00';

    const pv = $('#previewTable');
    if (pv && previewInfo) {
        const cols = [...previewInfo.columns, ts ? `${ts}（自動追加）` : '（取得日時：列名が未入力）'];
        const rows = previewInfo.rows.slice(0, 30).map(r => [...r, ts ? stamp : '—']);
        pv.replaceChildren(dataTable(cols, rows));
    }

    const box = $('#planTable');
    if (!box) return;
    const rows = plan.map((c, i) => el('tr', {},
        el('td', {}, el('input', {
            type: 'checkbox', ...(c.include ? { checked: 'checked' } : {}),
            onchange: ev => { plan[i].include = ev.target.checked; },
        })),
        el('td', { class: 'muted' }, c.source),
        el('td', {}, el('input', {
            type: 'text', value: c.name,
            onchange: ev => { plan[i].name = ev.target.value; },
        })),
        el('td', {}, el('select', {
            onchange: ev => { plan[i].type = ev.target.value; },
        }, ['TEXT', 'INTEGER', 'REAL'].map(t =>
            el('option', { value: t, ...(t === c.type ? { selected: 'selected' } : {}) }, t))))));

    // 自動で足される取得日時の行。外せないので操作欄は出さない。
    rows.push(el('tr', { style: 'background:var(--accent-weak)' },
        el('td', {}, '自動'),
        el('td', { class: 'muted' }, '（取り込み日時）'),
        el('td', {}, ts
            ? el('b', {}, ts)
            : el('span', { style: 'color:var(--err)' }, '列名を入力してください')),
        el('td', {}, 'TEXT')));

    box.replaceChildren(el('div', { class: 'tablewrap', style: 'max-height:340px' },
        el('table', { class: 'data' },
            el('thead', {}, el('tr', {},
                el('th', { style: 'width:52px' }, '取込'), el('th', {}, '元の列名'),
                el('th', {}, '列名（DB側）'), el('th', { style: 'width:120px' }, '型'))),
            el('tbody', {}, rows))));
}

function syncMode() {
    const append = $('#mode').value === 'append';
    ['#keepWrap', '#appendNote'].forEach(s => $(s)?.classList.toggle('hidden', !append));
    syncDest();
}

/** 保存前の必須チェック。足りなければ理由を配列で返す。 */
function formProblems(forJob = false) {
    const out = [];
    if (!tsName()) out.push('取得日時の列名を入力してください（必須）。');
    if ($('#mode').value === 'append') {
        const raw = ($('#keepRuns')?.value || '').trim();
        const keep = parseInt(raw, 10);
        if (!raw) out.push('保存回数を入力してください（必須）。');
        else if (!Number.isInteger(keep) || keep < 1 || keep > IMP.maxKeep) {
            out.push(`保存回数は 1〜${IMP.maxKeep} で指定してください。`);
        }
    }
    if (forJob) {
        const raw = ($('#jobStart')?.value || '').trim();
        // input の min だけでは手入力を防げないので、送る前にもう一度見る
        if (raw && raw < localNow(-2)) {
            out.push(`開始日時に過去の時刻は指定できません（指定: ${raw.replace('T', ' ')}）。`);
        }
    }
    return out;
}

function syncDest() {
    const dbFile = $('#dbTarget').value;
    $('#newDbWrap').classList.toggle('hidden', !!dbFile);
    const note = $('#destNote');
    note.replaceChildren();
    const run = $('#runImport');
    if (run) { run.disabled = false; run.title = ''; }
    if (!dbFile) return;
    const tables = IMP.existing[dbFile] || [];
    const t = $('#tableName').value.trim();
    note.append(el('div', { class: 'small muted' },
        `このDBにあるテーブル: ${tables.length ? tables.join(', ') : '（なし）' }`));

    // 定期実行＋追記のテーブルは、ここから手で足しても間隔が崩れるので入れさせない
    const locked = (lockedTables[dbFile] || {})[t];
    if (locked) {
        note.append(el('div', { class: 'alert alert--warn mt' }, ''+ locked));
        if (run) { run.disabled = true; run.title = locked; }
        return;
    }
    if (tables.includes(t) && $('#mode').value === 'replace') {
        note.append(el('div', { class: 'alert alert--warn mt' },
            `${t} は既にあります。全件入れ替えなので、いま入っている行はすべて削除されて入れ直されます。`));
    }
}

function importPayload() {
    const dbFile = $('#dbTarget').value;
    return {
        ...readOptions(),
        new_db: !dbFile, db_file: dbFile, db_name: $('#dbName')?.value,
        table: $('#tableName').value, mode: $('#mode').value,
        timestamp_column: $('#tsCol')?.value || null,
        keep_runs: $('#mode').value === 'append' ? $('#keepRuns')?.value : null,
        columns: plan,
    };
}

async function runImport(ev) {
    const bad = formProblems();
    if (bad.length) { bad.forEach(m => toast(m, 'warn')); return; }
    ev.target.disabled = true;
    ev.target.innerHTML = '<span class="spinner"></span> 取り込み中';
    try {
        const r = await api('/api/import/run', importPayload());
        let msg = `${r.db} の ${r.table} に ${r.rows.toLocaleString()}行を取り込みました。`;
        if (r.timestamp_column) msg += ` 取得日時列「${r.timestamp_column}」つき。`;
        if (r.keep) msg += ` 保持 ${r.kept}/${r.keep}回`;
        if (r.removed) msg += `（古い ${r.removed.toLocaleString()}行を削除）`;
        toast(msg, 'ok', 8000);
        if (r.degraded?.length) {
            toast(`数値にできない値があったため TEXT で取り込んだ列: ${r.degraded.join(', ')}`, 'warn', 9000);
        }
        setTimeout(() => window.location.reload(), 1500);
    } catch (e) { toast(e.message, 'err', 9000); }
    ev.target.disabled = false;
    ev.target.textContent = 'いま取り込む';
}

async function saveJob() {
    const bad = formProblems(true);
    if (bad.length) { bad.forEach(m => toast(m, 'warn')); return; }
    // 送信中はボタンを止める。二重クリックで同じ設定が2件できるのを防ぐ
    // （サーバ側でも同じ取り込み元→同じテーブルは弾く）
    const btn = $('#saveJob');
    if (btn) { btn.disabled = true; btn.textContent = '登録中…'; }
    try {
        await api('/api/jobs/save', {
            ...importPayload(), name: $('#jobName').value, interval: $('#jobInterval').value,
            start_at: $('#jobStart').value,
        });
        toast('定期取り込みに登録しました。データカタログの各テーブルの「管理」で確認できます。');
        refreshLocked();
    } catch (e) { toast(e.message, 'err', 9000); }
    if (btn) { btn.disabled = false; btn.textContent = '登録する'; }
}

/* --- 手で更新してはいけないテーブル ---------------------------------------------
   定期実行＋追記のテーブルは、手で足すと取得日時が1回ぶん余計に増えて間隔が崩れる。
   取り込み先を選ぶ欄でそのテーブルに鍵をかけるため、状態だけ取っておく。
   定期取り込みの一覧・操作・スケジューラの状態は「データカタログ > DB・テーブル」にある。 */
let lockedTables = {};

async function refreshLocked() {
    try {
        const m = await api('/api/import/manage', undefined, 'GET');
        lockedTables = m.locked || {};
        if ($('#dbTarget')) syncDest();
    } catch (e) { /* 鍵が取れなくても取り込みはできる */ }
}

/* --- 起動 ------------------------------------------------------------------- */

document.addEventListener('DOMContentLoaded', () => {
    loadDirs();
    wireDirs();
    $('#pickServer')?.addEventListener('click', () => openBrowser(null));
    $('#browserClose')?.addEventListener('click', closeBrowser);
    $('#browser')?.addEventListener('click', ev => {
        if (ev.target.id === 'browser') closeBrowser();   // 背景をクリックで閉じる
    });
    document.addEventListener('keydown', ev => {
        if (ev.key === 'Escape') closeBrowser();
    });
    $('#pickLocal')?.addEventListener('click', () => $('#localFile')?.click());
    $('#localFile')?.addEventListener('change', ev => {
        if (ev.target.files?.[0]) chooseLocalFile(ev.target.files[0]);
        ev.target.value = '';        // 同じファイルを選び直せるように
    });
    ['#sheet', '#delimiter', '#headerRow'].forEach(sel =>
        $(sel)?.addEventListener('change', loadPreview));
    $('#reload')?.addEventListener('click', loadPreview);

    lockedTables = (IMP.manage || {}).locked || {};
    if ($('#dbTarget')) syncDest();
});
