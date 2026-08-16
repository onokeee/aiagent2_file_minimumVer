/* DBとテーブルの管理（取り込み画面とカタログ画面で共有）。
   中身の確認・定期取り込みの状態と操作・テーブル/DBの削除。

   もともと取り込み画面の「DBの管理」タブにあったが、「中身を見る場所」
   （カタログ）と「管理する場所」が分かれていると使いにくいので、
   同じ部品を両方から使えるようにここへ切り出した。

   使う側は window.MANAGE = { intervals: [...], refresh: fn } を用意する。
     intervals … 更新の頻度の選択肢（ラベル）
     refresh   … 削除や設定変更のあとに一覧を描き直す関数 */

const MANAGE = window.MANAGE || { intervals: [], refresh: () => location.reload() };
const openTables = new Set();      // 描き直しても開いていたテーブルは開いたまま

/* スケジューラの状態。正常なときは何も出さない（"問題なし"の報告は読む理由がない）。
   止まっているときだけ帯を出す。設定どおりに更新できていない個々のジョブは、
   ⚠マーク・AIの注記・管理者メールで別に届く。 */
function renderSched(s) {
    const box = $('#schedBanner');
    if (!box) return;
    if (!s.enabled) {
        box.replaceChildren(el('div', { class: 'alert alert--warn' },
            '自動実行は停止しています（.env の IMPORT_SCHEDULER=false）。手動更新はできます。'));
    } else if (!s.running) {
        box.replaceChildren(el('div', { class: 'alert alert--err' },
            '自動実行のスレッドが動いていません。アプリを再起動してください。'));
    } else {
        box.replaceChildren();
    }
}

/** 定期取り込みの操作ボタン（頻度の変更・手動実行・停止・削除）。 */

function jobControls(j) {
    return [
        el('select', {
            style: 'width:130px',
            title: '更新の頻度',
            onchange: async ev => {
                await api('/api/jobs/update', { id: j.id, interval: ev.target.value });
                toast(`「${j.name}」を ${ev.target.value} に変更しました。`);
                MANAGE.refresh();
            },
        }, MANAGE.intervals.map(i => el('option',
            { ...(i === j.interval_label ? { selected: 'selected' } : {}) }, i))),
        // 定期実行＋追記は手で走らせると間隔が崩れるので押せなくする
        j.manual_blocked
            ? el('button', { class: 'btn btn--sm', disabled: 'disabled',
                             title: j.manual_blocked }, '今すぐ更新（不可）')
            : el('button', {
                class: 'btn btn--sm',
                onclick: async ev => {
                    ev.target.innerHTML = '<span class="spinner"></span>';
                    try {
                        const r = await api('/api/jobs/run', { id: j.id });
                        r.results.forEach(x =>
                            toast(`${x.name}: ${x.message}`, x.ok ? 'ok' : 'err', 7000));
                    } catch (e) { toast(e.message, 'err', 9000); }
                    MANAGE.refresh();
                },
            }, '今すぐ更新'),
        el('button', {
            class: 'btn btn--sm',
            title: j.enabled === false
                ? '自動更新を再開します（次回予定の時刻から動きます）。'
                : '自動更新を一時的に止めます。設定は残るので、いつでも再開できます。'
                  + '止めている間は「更新できていない」警告も出ません。',
            onclick: async () => {
                await api('/api/jobs/update', { id: j.id, enabled: j.enabled === false });
                toast(j.enabled === false
                    ? `「${j.name}」の自動更新を再開しました。`
                    : `「${j.name}」の自動更新を止めました。「再開」でいつでも戻せます。`);
                MANAGE.refresh();
            },
        }, j.enabled === false ? '再開' : '停止'),
        el('button', {
            class: 'btn btn--sm btn--danger',
            title: '定期取り込みの設定だけを消します（テーブルと中のデータは残ります）。'
                   + 'このテーブルは自動更新されなくなります。',
            onclick: async () => {
                if (!confirm(`定期取り込み「${j.name}」の設定を削除しますか？\n`
                    + '（テーブルと中のデータは残ります）')) return;
                await api('/api/jobs/delete', { id: j.id });
                toast('定期取り込みの設定を削除しました。');
                MANAGE.refresh();
            },
        }, '設定を削除'),
    ];
}

/** 1件ぶんの定期取り込みの中身（取り込み元と更新のしかた）。 */

function jobDetail(j, withName) {
    const box = el('div', { style: 'margin-top:6px' });
    if (withName) {
        box.append(el('div', { style: 'font-weight:600;font-size:12.5px;margin-bottom:2px' },
            `${j.name}`,
            j.enabled === false ? el('span', { class: 'badge badge--warn' }, '停止中') : null));
    }
    box.append(
        kv('ファイル名', j.source_label ? j.source_label.split(/[\\/]/).pop() : '―', true),
        kv('フルパス', j.source),
        kv('シート', j.sheet || '（Excel以外）'),
        kv('区切り文字', j.delimiter === null || j.delimiter === undefined
            ? '自動判定' : JSON.stringify(j.delimiter)),
        kv('見出しの行', (Number(j.header_row || 0) + 1) + ' 行目'),
        kv('更新の方法', j.mode_label, true),
        kv('更新の頻度', j.interval_label, true),
        kv('開始日時', (j.start_at || '').replace('T', ' ') || '（すぐ対象）'),
        kv('次回予定', j.next_label, true),
        kv('前回実行', (j.last_run || '').replace('T', ' ')),
        kv('状態', j.enabled === false ? '停止中' : '有効'));
    // 前回の結果。失敗（赤）と要確認（黄＝数値列が文字に落ちた）は、文章を読まなくても
    // 分かるように色を付ける。成功はそのまま小さく出す。
    if (j.last_status === 'error') {
        box.append(el('div', { class: 'alert alert--err small', style: 'margin-top:6px' },
            el('b', {}, '前回の更新に失敗しています'),
            el('div', { style: 'margin-top:2px' }, j.last_message || '')));
    } else if ((j.last_degraded || []).length) {
        box.append(el('div', { class: 'alert alert--warn small', style: 'margin-top:6px' },
            el('b', {}, `数値にできない値がありました（${j.last_degraded.join('、')}）`),
            el('div', { style: 'margin-top:2px' },
                '文字として保存したので、合計や平均がずれる可能性があります。元ファイルの値を確認してください。')));
    } else {
        box.append(kv('前回結果', j.last_message || '―'));
    }
    if (j.mode === 'append') box.append(kv('保存回数', `${j.keep_runs} 回まで`, true));
    if (j.manual_blocked) {
        box.append(el('div', { class: 'small muted mt' }, ''+ j.manual_blocked));
    }
    box.append(el('div', { class: 'row mt', style: 'gap:6px' }, ...jobControls(j)));
    return box;
}

function kv(label, value, strong) {
    return el('div', { style: 'display:flex;gap:8px;font-size:12.5px;padding:1px 0' },
        el('span', { class: 'muted', style: 'width:120px;flex:0 0 120px' }, label),
        el('span', { class: strong ? '': 'mono', style: strong ? 'font-weight:600': '' },
            value === null || value === undefined || value === '' ? '―' : String(value)));
}

/* --- 削除（テーブル / DB） --------------------------------------------------------
   消す前に「何が巻き添えになるか」を必ず見せる。カタログの説明・関連・例文・
   検算ルールはあちこちのDBに散っていて、画面を見ているだけでは分からないため。 */

function impactList(groups) {
    if (!groups.length) {
        return el('div', { class: 'small muted' }, '巻き添えになるものはありません。');
    }
    return el('div', {}, groups.map(g => el('details', { class: 'acc' },
        el('summary', {},
            el('strong', {}, g.label),
            el('span', { class: 'muted small' }, `${g.items.length}件`)),
        el('div', { class: 'acc__body' },
            g.items.map(it => el('div', { class: 'small', style: 'padding:1px 0' },
                el('span', { class: 'muted mono', style: 'margin-right:6px' }, it.db),
                it.text))))));
}

/** 削除の確認ダイアログ。opts で文言と実行内容を差し替える。 */

async function confirmDelete(opts) {
    let groups;
    try {
        groups = (await api(opts.impactUrl, undefined, 'GET')).groups;
    } catch (e) { return toast(e.message, 'err'); }

    // この画面には「ファイルを選ぶ」の .modal が最初から置いてある。
    // 取り違えないよう、こちらには id を付けておく
    const back = el('div', { class: 'modal', id: 'delModal' });
    const close = () => back.remove();
    back.addEventListener('click', ev => { if (ev.target === back) close(); });

    const dropJobs = el('input', { type: 'checkbox', checked: 'checked' });
    const jobCount = (groups.find(g => g.key === 'jobs')?.items || []).length;
    // 合言葉。DB削除のときだけ、ファイル名をそのまま打ってもらう
    const phrase = opts.phrase
        ? el('input', { type: 'text', style: 'width:100%',
                        placeholder: opts.phrase, autocomplete: 'off' })
        : null;

    const go = el('button', {
        class: 'btn btn--sm btn--danger',
        ...(phrase ? { disabled: 'disabled' } : {}),
        onclick: async () => {
            go.disabled = true;
            try {
                const r = await api(opts.url, {
                    ...opts.body,
                    ...(phrase ? { confirm: phrase.value.trim() } : {}),
                    drop_jobs: dropJobs.checked,
                });
                close();
                toast(opts.done(r));
                MANAGE.refresh();
            } catch (e) { toast(e.message, 'err', 9000); go.disabled = false; }
        },
    }, opts.action);
    phrase?.addEventListener('input',
        () => { go.disabled = phrase.value.trim() !== opts.phrase; });

    back.append(el('div', { class: 'modal__box' },
        el('div', { class: 'modal__head' },
            el('b', { class: 'grow' }, opts.title),
            el('button', { class: 'btn btn--sm btn--ghost', onclick: close },
                icon('x', 'icon--sm'))),
        el('div', { class: 'modal__body', style: 'padding:12px 14px' },
            el('div', { class: 'alert alert--err' }, opts.warning),
            el('div', { class: 'small muted', style: 'margin:10px 0 4px' },
                '一緒に片づけるもの'),
            impactList(groups),
            jobCount
                ? el('label', { class: 'row mt', style: 'align-items:center;gap:6px' },
                    dropJobs,
                    el('span', { class: 'small' },
                        `定期取り込みの設定 ${jobCount} 件も削除する`
                        + '（外すと、次の実行でまた取り込まれます）'))
                : null,
            phrase
                ? el('div', { class: 'mt' },
                    el('div', { class: 'small', style: 'margin-bottom:4px' },
                        `確認のため、`, el('b', { class: 'mono' }, opts.phrase),
                        ` をそのまま入力してください。`),
                    phrase)
                : null),
        el('div', { class: 'modal__foot row', style: 'align-items:center' },
            el('div', { class: 'spacer' }),
            el('button', { class: 'btn btn--sm', onclick: close }, 'やめる'),
            go)));
    document.body.append(back);
    (phrase || go).focus();
}

/** 「DBを削除」ボタン。両画面から同じ文言・同じ確認で。 */
function dbDeleteButton(d) {
    return el('button', {
        class: 'btn btn--sm btn--ghost hastip',
        'data-tip': 'このDBをファイルごと削除します。&#10;元には戻せません。',
        onclick: () => confirmDelete({
            title: `DBを削除: ${d.name}`,
            warning: `${d.name} をファイルごと削除します。`
                     + `テーブル ${d.tables.length} 件と、このDBのカタログ`
                     + '（説明・関連・用語・例文・検算ルール）がまとめて消えます。'
                     + 'この操作は元に戻せません。',
            impactUrl: `/api/import/impact?db=${encodeURIComponent(d.name)}`,
            url: '/api/import/delete-db',
            body: { db: d.name },
            phrase: d.name,
            action: 'このDBを削除する',
            done: () => `${d.name} を削除しました。`,
        }),
    }, 'DBを削除');
}

function tableCard(dbName, t) {
    const js = t.jobs || [];
    const j = js[0];
    const head = el('summary', {},
        el('strong', {}, t.name),
        el('span', { class: 'muted small' },
            `${(t.rows || 0).toLocaleString()}行 / ${t.column_count}列`),
        js.length
            ? el('span', { class: j.enabled === false ? 'badge badge--warn': 'badge badge--ok' },
                js.length > 1 ? `定期取り込み ${js.length}件`
                    : (j.enabled === false ? '定期取り込み（停止中）' : `定期取り込み ${j.interval_label}`))
            : el('span', { class: 'badge' }, '定期取り込みなし'),
        js.some(x => x.last_status === 'error')
            ? el('span', { class: 'badge badge--err' }, '前回失敗') : null);

    const body = el('div', { class: 'acc__body' });

    // 定期取り込み（取り込み元と更新のしかた）
    body.append(el('div', { style: 'font-weight:700;margin-bottom:4px' }, '定期取り込み'));
    if (js.length) {
        js.forEach(x => body.append(jobDetail(x, js.length >1)));
    } else {
        body.append(el('div', { class: 'small muted' },
            '設定されていません。取り込み元も分かりません'
            + '（手動で取り込んだか、外部で作られたテーブルです）。'
            + '「ファイルから取り込む」で取り込むときに登録できます。'));
    }

    // いま入っているデータ
    body.append(el('div', { style: 'font-weight:700;margin:10px 0 4px' }, '中身'),
        kv('行数', (t.rows || 0).toLocaleString(), true),
        kv('列数', t.column_count),
        kv('取得日時列', t.timestamp_column || '（なし）'),
        kv('保持している回数', t.runs === null ? '―' : `${t.runs} 回分`, true),
        kv('最新の取り込み', (t.latest || '').replace('T', ' ')),
        kv('最古の取り込み', (t.oldest || '').replace('T', ' ')),
        kv('列', t.columns.join(', ')));

    // サンプル行と更新履歴は開いたときに取りに行く（全テーブル分を先読みすると重い）
    const sampleBox = el('div', { class: 'mt' }, el('div', { class: 'small muted' }, '—'));
    const histBox = el('div', { class: 'mt' }, el('div', { class: 'small muted' }, '—'));
    body.append(
        el('div', { class: 'row mt', style: 'align-items:center;gap:6px' },
            el('div', { style: 'font-weight:700' }, 'サンプル行'),
            el('div', { class: 'spacer' }),
            tableViewLink(dbName, t.name),
            el('button', {
                class: 'btn btn--sm btn--ghost',
                onclick: () => loadTableDetail(dbName, t.name, sampleBox, histBox, true),
            }, '読み直す')),
        sampleBox,
        el('div', { style: 'font-weight:700;margin-top:10px' }, '更新履歴'),
        histBox);

    body.append(el('div', { class: 'row mt' },
        el('div', { class: 'spacer' }),
        el('button', {
            class: 'btn btn--sm btn--danger',
            onclick: () => confirmDelete({
                title: `テーブルを削除: ${dbName} の ${t.name}`,
                warning: `${t.name} と、その中の ${(t.rows || 0).toLocaleString()}行 を削除します。`
                         + 'この操作は元に戻せません。',
                impactUrl: `/api/import/impact?db=${encodeURIComponent(dbName)}`
                           + `&table=${encodeURIComponent(t.name)}`,
                url: '/api/import/drop-table',
                body: { db: dbName, table: t.name },
                action: 'テーブルを削除する',
                done: () => `${t.name} を削除し、カタログの記述も片づけました。`,
            }),
        }, 'テーブルを削除')));

    // 「今すぐ更新」を押すと一覧を描き直すので、開いていた表は開いたままにする
    const key = `${dbName}/${t.name}`;
    const acc = el('details', {
        class: 'acc',
        ...(openTables.has(key) ? { open: 'open' } : {}),
        ontoggle: ev => {
            if (!ev.target.open) { openTables.delete(key); return; }
            openTables.add(key);
            loadTableDetail(dbName, t.name, sampleBox, histBox);
        },
    }, head, body);
    // 最初から開いている場合は toggle が飛ばないので、こちらから読みに行く
    if (openTables.has(key)) loadTableDetail(dbName, t.name, sampleBox, histBox);
    // 中身だけを別の入れ物に載せ替える使い方（カタログ画面）のために、
    // サンプル行と更新履歴の置き場を外から辿れるようにしておく
    body.__sampleBox = sampleBox;
    body.__histBox = histBox;
    return acc;
}

/** テーブルのサンプル行と更新履歴を取ってきて流し込む。 */

async function loadTableDetail(dbName, table, sampleBox, histBox, force) {
    if (sampleBox.dataset.loaded && !force) return;
    sampleBox.dataset.loaded = '1';
    const wait = () => el('div', { class: 'small muted' },
        el('span', { class: 'spinner' }), '読み込み中...');
    sampleBox.replaceChildren(wait());
    histBox.replaceChildren(wait());
    let r;
    try {
        r = await api(`/api/import/table?db=${encodeURIComponent(dbName)}`
            + `&table=${encodeURIComponent(table)}`, undefined, 'GET');
    } catch (e) {
        sampleBox.dataset.loaded = '';
        sampleBox.replaceChildren(el('div', { class: 'alert alert--err' }, e.message));
        histBox.replaceChildren();
        return;
    }
    renderSample(sampleBox, r.sample);
    renderHistory(histBox, r.history, r.kinds);
}

function renderSample(box, s) {
    if (s.error) {
        box.replaceChildren(el('div', { class: 'alert alert--warn' }, s.error));
        return;
    }
    if (!s.rows.length) {
        box.replaceChildren(el('div', { class: 'small muted' }, 'まだ1行も入っていません。'));
        return;
    }
    box.replaceChildren(
        el('div', { class: 'small muted', style: 'margin-bottom:4px' },
            s.order_by
                ? `最大 ${s.limit} 行 ・ 「${s.order_by}」の新しい順（直近の取り込み分が上）`
                : `先頭 ${s.limit} 行 ・ 取得日時の列がないので入っている順`),
        dataTable(s.columns, s.rows));
}

function renderHistory(box, list, kinds) {
    if (!list || !list.length) {
        box.replaceChildren(el('div', { class: 'small muted' },
            'まだありません。この画面から取り込むと、ここに1回ぶんずつ残ります。'));
        return;
    }
    const ok = list.filter(h => h.ok).length;
    const rows = list.map(h => el('tr', {},
        el('td', { class: 'mono' }, (h.at || '').replace('T', ' ')),
        el('td', {}, h.ok
            ? el('span', { style: 'color:var(--ok)' }, '成功')
            : el('span', { style: 'color:var(--err)' }, '失敗')),
        el('td', {}, (kinds || {})[h.kind] || h.kind),
        el('td', {}, h.mode === 'append' ? '追記' : '全件入れ替え'),
        el('td', { class: 'num' }, h.ok ? (h.rows || 0).toLocaleString() : '―'),
        el('td', { class: 'num' }, h.removed ? `-${h.removed.toLocaleString()}` : ''),
        el('td', {}, h.kept === null || h.kept === undefined ? ''
            : `${h.kept}${h.keep ? '/'+ h.keep : '' }`),
        el('td', {}, h.seconds === null || h.seconds === undefined ? '' : `${h.seconds}秒`),
        el('td', {}, h.user || (h.kind === 'auto' ? '（自動）' : '')),
        el('td', { title: h.message }, h.message || '')));

    box.replaceChildren(
        el('div', { class: 'small muted', style: 'margin-bottom:4px' },
            `直近 ${list.length} 件（成功 ${ok} / 失敗 ${list.length - ok}）`),
        el('div', { class: 'tablewrap', style: 'max-height:320px' },
            el('table', { class: 'data' },
                el('thead', {}, el('tr', {},
                    ['日時', '結果', 'きっかけ', '方法', '行数', '削除', '保持', '所要', '実行者', 'メッセージ']
                        .map(h => el('th', {}, h)))),
                el('tbody', {}, rows))));
}

/** 対象のテーブルがまだ無い（または消された）定期取り込み。 */

function renderOrphans(list) {
    const box = $('#orphanJobs');
    box.replaceChildren();
    if (!list.length) return;
    const card = el('div', { class: 'card' },
        el('div', { class: 'card__title' }, '対象のテーブルがない定期取り込み'),
        el('div', { class: 'card__desc' },
            'まだ一度も実行されていないか、テーブルが削除された設定です。'
            + '実行すればテーブルが作られます。'));
    list.forEach(j => card.append(el('div', { class: 'acc__body' },
        el('div', { class: 'small mono muted' }, `${j.db_file} / ${j.table}`),
        jobDetail(j, true))));
    box.append(card);
}
