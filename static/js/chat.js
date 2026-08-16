/* チャット画面。描画アイテム（text/sql/table/chart/file/error）を組み立てて流す。 */

let currentChatId = window.CHAT_INIT.chatId || null;
let busy = false;
// 過去の会話を開き直したときに、作成済みファイルの保存が走らないようにする
let replaying = false;
// 次に送るユーザー発言の番号。巻き戻しでどこまで戻すかの目印になる。
let turnCount = 0;

/* --- モデルの選択と画像 --------------------------------------------------------- */

let modelInfo = { current: '', vision: false, image_max_count: 4, image_max_mb: 8 };
let pendingImages = [];        // 送信待ちの画像（サーバに預けたトークン）
// テンプレートが入れた素の文言。モデルに応じて画像の案内を足すため、先に控えておく。
const basePlaceholder = document.getElementById('input')?.placeholder || '';

function renderModel() {
    const sel = $('#modelPick');
    const list = modelInfo.models || [];
    sel.replaceChildren(...(list.length
        ? list.map(m => el('option', {
            value: m.id, ...(m.id === modelInfo.current ? { selected: 'selected' } : {}),
            // カタログ全体が収まらないモデルは選ぶ前から分かるようにする
            ...(m.catalog_fits === false ? { title: 'カタログ全体は入りません（自動で絞ります）' } : {}),
        }, m.id + (m.vision ? ' ' : '') + (m.catalog_fits === false ? ' ⚠' : '')))
        : [el('option', { value: modelInfo.current }, modelInfo.current || '（未設定）')]));

    // カタログ全体がこのモデルに収まらないときの警告（文面はサーバが状況に合わせて作る）
    const warn = $('#modelWarn');
    const scope = modelInfo.scope || {};
    warn.style.display = scope.note ? '' : 'none';
    warn.textContent = scope.note || '';

    const badge = $('#modelBadge');
    // 画像の添付はドラッグ＆ドロップと貼り付けで行う（ボタンは置かない）
    badge.textContent = modelInfo.vision
        ? `画像OK（最大${modelInfo.image_max_count}枚・${modelInfo.image_max_mb}MB）`
        : '文字のみ';
    badge.className = 'badge' + (modelInfo.vision ? ' badge--ok' : '');

    /* 添付ボタンを置いていないので、画像の入れ方は入力欄の薄字で伝える。
       画像を扱えないモデルのときに書くと嘘になるので、そのときは出さない。 */
    const input = $('#input');
    input.placeholder = modelInfo.vision
        ? `${basePlaceholder}（画像は Ctrl+V で貼り付け、ドラッグ＆ドロップでも添付できます）`
        : basePlaceholder;

    if (!modelInfo.vision && pendingImages.length) {
        pendingImages = [];
        renderAttachments();
    }
}

async function loadModels(refresh) {
    try {
        modelInfo = await api('/api/models' + (refresh ? '?refresh=1' : ''),
                              undefined, 'GET');
        renderModel();
    } catch (e) { /* 未設定でも画面は動かす */ }
}

function renderAttachments() {
    const row = $('#attachRow');
    const box = $('#attachList');
    row.style.display = pendingImages.length ? '' : 'none';
    box.replaceChildren(...pendingImages.map((im, i) =>
        el('div', { class: 'attach' },
            el('img', { src: im.url, alt: im.filename }),
            el('span', { class: 'attach__name', title: im.filename }, im.filename),
            el('button', {
                class: 'attach__x', title: '外す',
                onclick: () => { pendingImages.splice(i, 1); renderAttachments(); },
            }, '×'))));
}

async function attachImages(files) {
    if (!modelInfo.vision) {
        toast('いま選ばれているモデルは画像を扱えません。', 'warn');
        return;
    }
    for (const f of files) {
        if (pendingImages.length >= modelInfo.image_max_count) {
            toast(`画像は一度に${modelInfo.image_max_count}枚までです。`, 'warn');
            break;
        }
        const fd = new FormData();
        fd.append('file', f);
        try {
            const res = await fetch('/api/chat/image', { method: 'POST', body: fd });
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || '添付できませんでした');
            pendingImages.push(data);
            renderAttachments();
        } catch (e) { toast(e.message, 'err', 8000); }
    }
}

/* --- 対象データ（一覧表示のみ） ------------------------------------------------
   選択UIは無い。どのDBを使うかは質問ごとにアプリが自動で決める。
   一覧はクリックで開閉でき、中身（テーブルと説明）を確かめられる。 */

function wireScope() {
    $$('.dbpick').forEach(box => {
        $('.dbpick__head', box).addEventListener('click', () => {
            box.classList.toggle('is-open');
        });
    });
}

/* --- 履歴 ------------------------------------------------------------------- */

function renderHistory(items) {
    const box = $('#historyList');
    box.replaceChildren();
    if (!items.length) {
        box.append(el('div', { class: 'small muted' }, 'まだ履歴はありません。'));
        return;
    }
    items.forEach(c => {
        const stamp = (c.updated_at || '').slice(5, 16).replace('T', ' ');
        box.append(el('div', {
            class: 'histitem' + (c.id === currentChatId ? ' is-active' : ''),
            onclick: ev => { if (!ev.target.closest('.histitem__del')) openChat(c.id); },
        },
            el('div', { class: 'histitem__title', title: `${c.title}（${stamp}）` }, c.title || '（無題）'),
            el('span', { class: 'small muted' }, stamp),
            el('button', {
                class: 'histitem__del', title: '削除',
                onclick: async () => {
                    if (!confirm(`「${c.title}」を削除しますか？`)) return;
                    await api('/api/chat/delete', { id: c.id });
                    if (c.id === currentChatId) { currentChatId = null; clearLog(); }
                    refreshHistory();
                },
            }, icon('trash', 'icon--sm'))));
    });
}

async function refreshHistory() {
    const r = await api('/api/history', undefined, 'GET');
    currentChatId = r.current || currentChatId;
    renderHistory(r.chats);
}

async function openChat(id) {
    const r = await api('/api/chat/open', { id });
    currentChatId = id;
    clearLog();
    replaying = true;
    lastRole = null;
    r.items.forEach(addItem);
    replaying = false;
    refreshHistory();
    scrollDown(true);
}

/* --- 描画 ------------------------------------------------------------------- */

/* まだ何も話していないときの画面。例文は全DBのカタログ（examples）から来る。 */
let starters = { examples: [], tables: [] };

function renderEmpty() {
    const box = el('div', { class: 'empty', id: 'emptyState' },
        el('div', { class: 'empty__icon' }, icon('chat')));

    if (!window.CHAT_INIT.llmReady) {
        box.append(el('div', { class: 'alert alert--warn', style: 'display:inline-block;text-align:left' },
            'LLMが未設定です。env の OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL を設定してください。'));
        return box;
    }

    if (!(starters.tables || []).length) {
        // data/ にDBが1つも無い
        box.append(el('div', {}, '分析できるデータがまだありません。'),
            el('div', { class: 'small muted mt' },
                '管理者は「データ取り込み」からExcel/CSVでDBを作成できます。'));
        return box;
    }

    box.append(el('div', {}, '下の欄から質問してください。'));

    if ((starters.examples || []).length) {
        box.append(el('div', { class: 'small muted mt' }, '質問例:'),
            el('div', { class: 'examples', style: 'justify-content:center;margin-top:10px' },
                starters.examples.map(q =>
                    el('button', { class: 'example', onclick: () => send(q) }, q))));
    } else {
        // 例文が未登録のDB。何について聞けるかだけでも見せる
        box.append(el('div', { class: 'small muted mt' },
            `使えるテーブル: ${starters.tables.join('、')}`));
        if (window.CHAT_INIT.isAdmin) {
            box.append(el('div', { class: 'small muted mt' },
                'データカタログの「質問とSQLの例文」に登録すると、ここに出ます。'
                + '回答が正しかったときに出る「この質問とSQLを例文として保存」からも増やせます。'));
        }
    }
    return box;
}

function showEmpty() {
    if ($('#logInner').querySelector('.msg, .toolblock, .report, .doc')) return;
    $('#emptyState')?.remove();
    $('#logInner').append(renderEmpty());
}

function clearLog() {
    turnCount = 0;
    $('#logInner').replaceChildren(renderEmpty());
}

function bubble(role) {
    const wrap = el('div', { class: `msg msg--${role}` },
        el('div', { class: 'msg__body' }));
    $('#emptyState')?.remove();
    $('#logInner').append(wrap);
    return $('.msg__body', wrap);
}

let lastRole = null;
function slot(role) {
    const last = $('#logInner').lastElementChild;
    if (lastRole === role && last && last.classList.contains('msg')) return $('.msg__body', last);
    lastRole = role;
    return bubble(role);
}

/* このSQLが触れているテーブルを、カタログの該当テーブルへのリンクにする。
   AIが「この列が何か分からない」と言ったその場から、説明を書きに行けるようにする。
   カタログは管理者専用なので、リンクも管理者にだけ出す。 */
function catalogLinks(tables) {
    if (!window.CHAT_INIT.isAdmin || !(tables || []).length) return null;
    return el('div', { class: 'catlinks' },
        el('span', { class: 'muted' }, 'カタログで説明を書く:'),
        ...tables.map(t => el('a', {
            href: `/catalog?db=${encodeURIComponent(t.db)}`
                  + `#tab=tables&table=${encodeURIComponent(t.table)}`,
            target: '_blank', rel: 'noopener',
            title: `${t.db} の ${t.table} を開く（列の説明はここで書けます）`,
        }, t.table)));
}

/* ER図のカード。図はその場に埋めず、開いたときにキャンバスを組み立てる。
   （er.js は一度に1つの図しか持てないので、開いている間だけ実体を作る） */
/* 「テーブルを見せて」で出るカード。全行のビューアへのリンクを出すだけで、
   勝手には開かない。開くかどうかは人が「テーブル全体を開く」を押して決める
   （見たいタイミングは人の側にあり、送るたびにタブが増えるのは邪魔なため）。 */
function tableCardLink(item) {
    const href = `/table?db=${encodeURIComponent(item.db)}&table=${encodeURIComponent(item.table)}`;
    const meta = [
        item.rows === null || item.rows === undefined ? null : `${Number(item.rows).toLocaleString()}行`,
        (item.columns || []).length ? `${item.columns.length}列` : null,
        item.description || null,
    ].filter(Boolean).join(' ・ ');
    return el('div', { class: 'filecard' },
        icon('table', 'icon--lg'),
        el('div', { class: 'grow' },
            el('div', { class: 'name' }, item.title || `${item.table}（${item.db}）`),
            el('div', { class: 'small muted' }, meta || 'テーブルの中身を別タブで開きます')),
        el('a', { class: 'btn btn--primary btn--sm', href, target: '_blank', rel: 'noopener',
                  title: `${item.table} の全行を別タブで開きます（読み取り専用）` },
           'テーブル全体を開く'));
}

function erCard(item) {
    return el('div', { class: 'filecard' },
        icon('table', 'icon--lg'),
        el('div', { class: 'grow' },
            el('div', { class: 'name' }, item.title || (item.db + ' のER図')),
            el('div', { class: 'small muted' },
                'テーブルの関係図（読み取り専用・拡大縮小と全画面ができます）')),
        el('button', { class: 'btn btn--primary btn--sm',
                       onclick: () => openErModal(item) }, '表示'));
}

function openErModal(item) {
    const back = el('div', { class: 'modal' });
    const close = () => { back.remove(); };
    back.addEventListener('click', ev => { if (ev.target === back) close(); });

    const svgEl = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svgEl.setAttribute('class', 'er__svg');
    svgEl.id = 'erSvg';

    // er.js が期待するIDでキャンバスの骨組みを作る（カタログ画面と同じ構造）
    const box = el('div', { class: 'modal__box', style: 'max-width:96vw;width:1200px' },
        el('div', { class: 'modal__head' },
            el('b', { class: 'grow' }, item.title || (item.db + ' のER図')),
            el('button', { class: 'btn btn--sm btn--ghost', onclick: close }, icon('x', 'icon--sm'))),
        el('div', { class: 'modal__body', style: 'padding:10px' },
            el('div', { class: 'er er--chat', id: 'erRoot' },
                el('div', { class: 'er__toolbar' },
                    el('button', { class: 'btn btn--sm', id: 'erFull' }, '全画面')),
                el('div', { class: 'er__viewport', id: 'erViewport' },
                    svgEl,
                    el('div', { class: 'er__world', id: 'erWorld' })),
                el('div', { class: 'er__legend' },
                    el('b', {}, 'IPA表記'), '　下線＝主キー　線の両端の 1・*＝多重度　',
                    '実線＝登録済み／短い破線＝FOREIGN KEY　長い破線＝DBをまたぐ関連'),
                el('div', { class: 'er__panel hidden', id: 'erPanel' }))));
    back.append(box);
    document.body.append(back);
    ER.init({ data: item.er, readonly: true });
    // Escは段階的に効かせる: 全画面中なら解除だけ(er.jsに任せる)、通常表示なら閉じる。
    // capture=true で er.js より先に状態を見る（同じイベントで両方起きるのを防ぐ）
    document.addEventListener('keydown', function esc(ev) {
        if (ev.key !== 'Escape') return;
        const root = document.getElementById('erRoot');
        if (root && root.classList.contains('er--full')) return;   // 解除はer.js側
        document.removeEventListener('keydown', esc, true);
        close();
    }, true);
}

/* 用語の登録カード。AIは提案まで。書き込みはボタンを押したときだけ。
   SQLや置き場所は出さない。代わりに「どう数えるか」の日本語と実データの件数で、
   SQLを読めない人でも正しさを判断できるようにする。 */
function glossaryCard(item) {
    const card = el('div', { class: 'mailcard' });
    const row = (label, value) => value ? el('div', { class: 'mailcard__row' },
        el('span', { class: 'mailcard__label' }, label),
        el('span', { class: 'grow' }, value)) : null;

    card.append(el('div', { class: 'mailcard__head' },
        icon('catalog', 'icon--sm'), el('b', {}, '用語集への登録の提案'),
        el('div', { class: 'spacer' }),
        el('span', { class: 'badge' + (item.exists ? ' badge--warn' : ' badge--ok') },
            item.exists ? '既存の定義を変更' : '新規登録')));
    [row('用語', item.term),
     row('意味', item.description),
     row('どう数えるか', item.how),
     item.detail ? row('実データで確認', item.detail) : null,
     item.exists && item.old ? el('div', { class: 'mailcard__row' },
         el('span', { class: 'mailcard__label' }, '変更前'),
         el('span', { class: 'grow small muted' },
             item.old.description || '', item.old.sql ? '（式あり）' : '')) : null,
    ].filter(Boolean).forEach(x => card.append(x));

    const btn = el('button', { class: 'btn btn--primary btn--sm', onclick: async () => {
        btn.disabled = true;
        try {
            const r = await api('/api/chat/glossary-save', {
                db: item.db, table: item.table, term: item.term,
                description: item.description, sql: item.sql });
            toast(r.message, 'ok', 8000);
            btn.textContent = '登録済み';
        } catch (e) { toast(e.message, 'err', 8000); btn.disabled = false; }
    } }, '用語集に登録');
    card.append(el('div', { class: 'mailcard__row', style: 'justify-content:flex-end' },
        el('span', { class: 'small muted grow' },
            '登録すると全員のAIがこの定義に従います。登録した人と変更の記録は残ります。'),
        btn));
    return card;
}

/* 例文の登録カード。SQLは出さず、「何をどう集計したか」と実データの先頭数行を見せる。 */
function exampleCard(item) {
    const card = el('div', { class: 'mailcard' });
    const row = (label, value) => value ? el('div', { class: 'mailcard__row' },
        el('span', { class: 'mailcard__label' }, label),
        el('span', { class: 'grow' }, value)) : null;

    card.append(el('div', { class: 'mailcard__head' },
        icon('catalog', 'icon--sm'), el('b', {}, '例文への登録の提案'),
        el('div', { class: 'spacer' }),
        el('span', { class: 'badge' + (item.exists ? ' badge--warn' : ' badge--ok') },
            item.exists ? '既存の例文を更新' : '新規登録')));
    [row('質問', item.question),
     row('何をしたか', item.summary),
     item.exists && item.old_q && item.old_q !== item.question
         ? row('変更前の質問', item.old_q) : null,
    ].filter(Boolean).forEach(x => card.append(x));

    if ((item.rows || []).length) {
        card.append(el('div', { class: 'mailcard__row' },
            el('span', { class: 'mailcard__label' }, '実データ'),
            el('div', { class: 'grow' },
                dataTable(item.columns || [], item.rows || [],
                    { caption: `全 ${Number(item.total || 0).toLocaleString()} 件中 先頭 ${item.rows.length} 行` }))));
    }

    const btn = el('button', { class: 'btn btn--primary btn--sm', onclick: async () => {
        btn.disabled = true;
        try {
            const r = await api('/api/chat/save-example', {
                db: item.db, question: item.question, sql: item.sql, description: item.summary });
            toast(r.message, 'ok', 8000);
            btn.textContent = '登録済み';
        } catch (e) { toast(e.message, 'err', 8000); btn.disabled = false; }
    } }, '例文として登録');
    card.append(el('div', { class: 'mailcard__row', style: 'justify-content:flex-end' },
        el('span', { class: 'small muted grow' },
            '登録すると似た質問へのAIのお手本になります。登録した人と変更の記録は残ります。'),
        btn));
    return card;
}

function addItem(item) {
    const body = slot(item.role === 'user' ? 'user' : 'assistant');
    if (item.kind === 'text' && item.role === 'user' && item.turn !== undefined) {
        turnCount = item.turn + 1;           // 次に送る発言の番号
        body.append(userTurn(item));
    } else if (item.kind === 'text') {
        body.append(el('div', { html: `<p>${mdToHtml(item.content)}</p>` },
                      catalogLinks(item.tables)));
    } else if (item.kind === 'sql') {
        const block = el('div', { class: 'toolblock' },
            el('div', { class: 'toolblock__head' },
                icon('table', 'icon--sm'), el('span', {}, item.label || item.tool),
                item.purpose ? el('span', { class: 'muted small' }, `— ${item.purpose}`) : null),
            el('pre', { class: 'mono' }, item.sql));
        const links = catalogLinks(item.tables);
        if (item.question || links) {
            const foot = el('div', { class: 'toolblock__foot' });
            if (item.question) {
                // 直接保存ではなくAIに頼む。AIが内容の日本語説明と実データ付きの
                // 登録カードを出し、そこで確定する（何が登録されるか見えるように）
                foot.append(el('button', {
                    class: 'btn btn--sm',
                    onclick: ev => {
                        ev.target.disabled = true;
                        send(`「${item.question}」の回答に使ったSQLを、そのまま例文として登録してください。`);
                    },
                }, 'この質問と答え方を例文にする'));
            }
            if (links) foot.append(links);
            block.append(foot);
        }
        body.append(block);
    } else if (item.kind === 'table') {
        const cap = `${(item.rows || []).length} 行`
            + (item.truncated ? '（上限で切り詰め）' : '');
        body.append(dataTable(item.columns || [], item.rows || [], { caption: cap }));
    } else if (item.kind === 'chart') {
        const div = el('div', { class: 'plot' });
        body.append(div);
        if (item.figure) {
            Plotly.newPlot(div, item.figure.data, {
                ...item.figure.layout, autosize: true,
                margin: { l: 55, r: 20, t: 40, b: 50 },
                paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
            }, { responsive: true, displaylogo: false });
        }
    } else if (item.kind === 'file') {
        const card = el('div', { class: 'filecard' },
            icon('file', 'icon--lg'),
            el('div', { class: 'grow' },
                el('div', { class: 'name' }, item.filename),
                el('div', { class: 'small muted' },
                    item.note || (item.sheets || []).map(s => `${s.name}: ${s.total}行`).join('/ '))),
            item.url ? el('a', { class: 'btn btn--primary btn--sm', href: item.url }, 'ダウンロード') : null);
        body.append(card);
        // 作られた直後だけ自動で保存を始める（履歴を開き直したときは出さない）
        if (item.url && !replaying && window.CHAT_INIT.autoDownload) window.location.href = item.url;
        (item.sheets || []).forEach(s => {
            if (!s.rows || !s.rows.length) return;
            body.append(el('details', { class: 'acc' },
                el('summary', {}, `「${s.name}」の内容を確認（先頭${s.rows.length}行）`),
                el('div', { class: 'acc__body' }, dataTable(s.columns, s.rows))));
        });
    } else if (item.kind === 'glossary_term') {
        body.append(glossaryCard(item));
    } else if (item.kind === 'example_proposal') {
        body.append(exampleCard(item));
    } else if (item.kind === 'table_link') {
        body.append(tableCardLink(item));
    } else if (item.kind === 'er') {
        body.append(erCard(item));
    } else if (item.kind === 'report') {
        body.append(reportBlock(item));
    } else if (item.kind === 'report_doc') {
        body.append(reportDoc(item));
    } else if (item.kind === 'mail_draft') {
        body.append(mailCard(item));
    } else if (item.kind === 'error') {
        body.append(el('div', { class: 'alert alert--err' }, item.message));
    }
}

/* --- 発言の巻き戻し・編集 --------------------------------------------------------- */

/** ユーザーの発言。マウスを乗せると編集アイコンが出る。 */
function userTurn(item) {
    const wrap = el('div', { class: 'turn' });
    const text = el('div', { class: 'turn__text' });
    if ((item.images || []).length) {
        text.append(el('div', { class: 'sentimgs' },
            item.images.map(im => el('a', { href: im.url, target: '_blank',
                                            title: im.filename },
                el('img', { src: im.url, alt: im.filename })))));
    }
    text.append(el('div', { html: `<p>${mdToHtml(item.content)}</p>` }));
    const tools = el('div', { class: 'turn__tools' },
        el('button', {
            class: 'turn__btn', title: 'この発言を修正してやり直す',
            onclick: () => editTurn(wrap, item),
        }, icon('tool', 'icon--sm')),
        el('button', {
            class: 'turn__btn', title: 'ここまで巻き戻す（この発言以降を消す）',
            onclick: () => rewindTo(item, ''),
        }, icon('back', 'icon--sm')));
    wrap.append(text, tools);
    return wrap;
}

/** 発言を編集する形に差し替える。 */
function editTurn(wrap, item) {
    if (busy) return;
    const area = el('textarea', { class: 'turn__edit', rows: '3' });
    area.value = item.content;
    const cancel = () => {
        const fresh = userTurn(item);
        wrap.replaceWith(fresh);
    };
    const box = el('div', {},
        area,
        el('div', { class: 'row mt' },
            el('button', { class: 'btn btn--primary btn--sm',
                           onclick: () => rewindTo(item, area.value) }, 'ここからやり直す'),
            el('button', { class: 'btn btn--sm', onclick: cancel }, 'キャンセル'),
            el('div', { class: 'spacer' }),
            el('span', { class: 'small muted' }, 'これ以降のやり取りは消えます')));
    wrap.replaceChildren(box);
    area.focus();
    area.setSelectionRange(area.value.length, area.value.length);
    area.style.height = 'auto';
    area.style.height = Math.min(240, area.scrollHeight) + 'px';
    area.addEventListener('input', () => {
        area.style.height = 'auto';
        area.style.height = Math.min(240, area.scrollHeight) + 'px';
    });
    area.addEventListener('keydown', ev => {
        if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); rewindTo(item, area.value); }
        if (ev.key === 'Escape') cancel();
    });
    scrollDown();
}

/** 巻き戻して、必要ならその内容で会話をやり直す。 */
async function rewindTo(item, text) {
    if (busy) return;
    const send = (text || '').trim();
    if (!send && !confirm('この発言と、それ以降のやり取りを消して巻き戻します。よろしいですか？')) {
        return;
    }
    setBusy(true, send ? 'やり直し中' : '巻き戻し中');
    try {
        const r = await api('/api/chat/rewind', { turn: item.turn, text: send });
        clearLog();
        lastRole = null;
        replaying = true;                    // 再描画なのでファイルの自動保存は走らせない
        (r.items || []).forEach(addItem);
        replaying = false;
        if (!send) {
            // 巻き戻しだけのときは、消した発言を入力欄に戻す
            $('#input').value = r.restored || '';
            $('#input').style.height = 'auto';
            $('#input').style.height = Math.min(200, $('#input').scrollHeight) + 'px';
            $('#input').focus();
            toast(`${r.dropped}件のやり取りを取り消しました。入力欄から続けられます。`, 'ok');
        }
        currentChatId = r.chat_id || currentChatId;
        refreshHistory();
    } catch (e) {
        toast(e.message, 'err', 8000);
    }
    setBusy(false);
    scrollDown(true);
}

/* --- 分析結果（表＋所見） ------------------------------------------------------- */

function reportBlock(item) {
    const box = el('div', { class: 'report' });
    if (item.title) box.append(el('div', { class: 'report__title' }, item.title));
    (item.tables || []).forEach((t, i) => {
        const cap = `${(t.rows || []).length} 行`;
        const table = dataTable(t.columns || [], t.rows || [], { caption: cap });
        // 表が多いときは1つ目だけ開いておく（結論はたいてい先頭にある）
        if ((item.tables || []).length > 1) {
            box.append(el('details', { class: 'acc', ...(i === 0 ? { open: 'open' } : {}) },
                el('summary', {}, t.name || `表${i + 1}`),
                el('div', { class: 'acc__body' }, table)));
        } else {
            if (t.name) box.append(el('div', { class: 'small muted mt' }, t.name));
            box.append(table);
        }
    });
    if ((item.notes || []).length) {
        box.append(el('ul', { class: 'report__notes' },
            item.notes.map(n => el('li', {}, n))));
    }
    return box;
}

/* --- まとまったレポート --------------------------------------------------------- */

function reportDoc(item) {
    const doc = el('div', { class: 'doc' });
    doc.append(el('div', { class: 'doc__head' },
        el('div', { class: 'grow' },
            el('div', { class: 'doc__title' }, item.title),
            item.subtitle ? el('div', { class: 'small muted' }, item.subtitle) : null),
        item.url ? el('a', { class: 'btn btn--sm', href: item.url },
                      `${item.filename}`) : null));

    if ((item.summary || []).length) {
        doc.append(el('div', { class: 'doc__summary' },
            el('div', { class: 'doc__label' }, '要点'),
            el('ul', {}, item.summary.map(s => el('li', {}, s)))));
    }

    (item.sections || []).forEach((s, i) => {
        const sec = el('div', { class: 'doc__section' },
            el('h4', { class: 'doc__h' }, `${i + 1}. ${s.heading}`));
        if (s.body) sec.append(el('div', { html: mdToHtml(s.body) }));
        if (s.figure) {
            const div = el('div', { class: 'plot' });
            sec.append(div);
            Plotly.newPlot(div, s.figure.data, {
                ...s.figure.layout, autosize: true,
                margin: { l: 55, r: 20, t: 40, b: 50 },
                paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
            }, { responsive: true, displaylogo: false });
        }
        if (s.chart_error) sec.append(el('div', { class: 'alert alert--warn' }, s.chart_error));
        if (s.table) {
            const cap = s.table.truncated
                ? `全 ${(s.table.total || 0).toLocaleString()} 行のうち上位 ${s.table.rows.length} 行`
                : `${s.table.rows.length} 行`;
            sec.append(dataTable(s.table.columns, s.table.rows, { caption: cap }));
        }
        if (s.note) sec.append(el('div', { class: 'doc__note' }, s.note));
        doc.append(sec);
    });

    if (item.conclusion) {
        doc.append(el('div', { class: 'doc__section' },
            el('h4', { class: 'doc__h' }, '結論'),
            el('div', { html: mdToHtml(item.conclusion) })));
    }
    if ((item.recommendations || []).length) {
        doc.append(el('div', { class: 'doc__section' },
            el('h4', { class: 'doc__h' }, '推奨する打ち手'),
            el('ol', { class: 'doc__actions' },
                item.recommendations.map(r => el('li', {}, r)))));
    }
    if ((item.caveats || []).length) {
        doc.append(el('details', { class: 'acc' },
            el('summary', {}, '前提・注意'),
            el('div', { class: 'acc__body' },
                el('ul', {}, item.caveats.map(c => el('li', {}, c))))));
    }
    return doc;
}

/* --- メールの下書き ------------------------------------------------------------- */

function mailCard(item) {
    const p = item.preview || {};
    const draft = item.draft || {};
    const card = el('div', { class: 'mailcard' });
    const row = (label, value) => el('div', { class: 'mailcard__row' },
        el('span', { class: 'mailcard__label' }, label),
        el('span', { class: 'grow' }, value));

    card.append(el('div', { class: 'mailcard__head' },
        icon('mail', 'icon--sm'), el('b', {}, 'メールの下書き'),
        el('div', { class: 'spacer' }),
        el('span', { class: 'badge' }, p.dry_run ? 'テスト送信モード': '本番送信')));
    card.append(
        row('From', p.from || '（未設定）'),
        row('To', (p.to || []).join(', ') || '（なし）'));
    if ((p.cc || []).length) card.append(row('Cc', p.cc.join(', ')));
    if ((p.bcc || []).length) card.append(row('Bcc', `${p.bcc.length}件（非表示）`));
    card.append(row('件名', p.subject || '（なし）'));
    if ((draft.attach_filenames || []).length) {
        card.append(row('添付', draft.attach_filenames.join(', ')));
    }
    card.append(el('pre', { class: 'mailcard__body' }, p.body || ''));

    const foot = el('div', { class: 'mailcard__foot' });
    if ((p.errors || []).length) {
        card.append(el('div', { class: 'alert alert--err' },
            el('div', {}, 'このままでは送信できません:'),
            el('ul', {}, p.errors.map(e => el('li', {}, e)))));
    } else {
        if (p.dry_run) {
            card.append(el('div', { class: 'alert alert--warn' },
                'いまはテスト送信モードです（.env の SMTP_DRY_RUN=true）。'
                + '「送信」を押しても実際には送られず、内容の確認だけ行います。'));
        }
        const send = el('button', { class: 'btn btn--primary btn--sm' },
            p.dry_run ? '送信（テスト）' : 'このまま送信する');
        send.addEventListener('click', async () => {
            const to = (p.to || []).join(', ');
            if (!confirm(`次の宛先に送信します。よろしいですか？\n\n`
                + `宛先: ${to}\n件名: ${p.subject}`
                + ((draft.attach_filenames || []).length
                    ? `\n添付: ${draft.attach_filenames.join(', ')}` : ''))) return;
            send.disabled = true;
            send.innerHTML = '<span class="spinner"></span> 送信中';
            try {
                const r = await api('/api/mail/send', { draft, confirm: true });
                toast(r.record.message, 'ok', 8000);
                send.textContent = '送信済み';
                foot.append(el('span', { class: 'small muted' },
                    `${r.record.at.replace('T', ' ')} に送信`));
            } catch (e) {
                toast(e.message, 'err', 9000);
                send.disabled = false;
                send.textContent = 'このまま送信する';
            }
        });
        foot.append(send);
    }
    foot.append(el('div', { class: 'spacer' }),
        el('span', { class: 'small muted' }, `送信サーバ: ${p.smtp || '未設定' }`));
    card.append(foot);
    return card;
}

/** いちばん下の近くを見ているか（読んでいる途中で飛ばさないための判定）。 */
function atBottom(slack = 120) {
    const log = $('#log');
    return log.scrollHeight - log.scrollTop - log.clientHeight < slack;
}

/**
 * 下までスクロールする。
 * force を付けない限り、ユーザーが上の方を読んでいるときは動かさない。
 * 回答が届くたびに勝手に飛ばされると、途中の表やグラフを読めないため。
 */
function scrollDown(force = false) {
    if (!force && !atBottom()) {
        showJump(true);
        return;
    }
    const log = $('#log');
    log.scrollTop = log.scrollHeight;
    showJump(false);
}

/** 「最新へ」ボタンの出し入れ。上を読んでいるあいだだけ出す。 */
function showJump(on) {
    $('#jumpDown')?.classList.toggle('is-on', !!on);
}

/** スクロール位置を見てボタンを出し入れする。 */
function watchScroll() {
    const log = $('#log');
    if (!log) return;
    // 位置を読んでクラスを付け替えるだけなので、間引かずにそのまま呼ぶ
    const update = () => showJump(!atBottom());
    log.addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', update);
    update();
}

/* --- 送信 ------------------------------------------------------------------- */

/* 待ち時間の表示。
   何をしているか言えないときは経過秒だけを出す。時間が見えていれば
   「止まっているのか動いているのか」が分かる。
   ツール実行中はその名前も添える。計測は送信から通しで、途中で戻さない。 */
let thinkTimer = null;

const elapsedText = (sec) =>
    sec < 60 ? `${sec}s` : `${Math.floor(sec / 60)}m ${String(sec % 60).padStart(2, '0')}s`;

function setBusy(on, label = '') {
    busy = on;
    $('#send').disabled = on;
    $('#send').innerHTML = on ? `<span class="spinner"></span>` : '送信';

    if (!on) {
        clearInterval(thinkTimer);
        thinkTimer = null;
        $('#thinking')?.remove();
        return;
    }

    let ind = $('#thinking');
    if (!ind) {
        const started = Date.now();
        ind = el('div', { class: 'msg msg--assistant', id: 'thinking' },
            el('div', { class: 'msg__body thinking' },
                el('span', { class: 'spinner' }),
                el('span', { class: 'thinking__label' }),
                el('span', { class: 'thinking__time' })));
        $('#logInner').append(ind);
        const tick = () => {
            const sec = Math.floor((Date.now() - started) / 1000);
            $('.thinking__time', ind).textContent = elapsedText(sec);
        };
        tick();
        thinkTimer = setInterval(tick, 1000);
        scrollDown(true);
    }
    $('.thinking__label', ind).textContent = label;
}

async function send(text) {
    if (busy) return;
    text = (text || $('#input').value).trim();
    if (!text) return;
    $('#input').value = '';
    $('#input').style.height = 'auto';
    lastRole = null;
    // 添付は送信の時点で確定させる（送信中に足しても混ざらないように）
    const images = pendingImages.slice();
    pendingImages = [];
    renderAttachments();
    addItem({ role: 'user', kind: 'text', content: text, turn: turnCount,
              images: images.length ? images : undefined });
    scrollDown(true);                       // 自分の発言のときは必ず下へ
    setBusy(true);
    const tokens = images.map(i => i.token);
    const ok = await sendStreaming(text, tokens);
    if (!ok) await sendAtOnce(text, tokens);   // 逐次表示が使えない環境では従来方式へ
    setBusy(false);
}

/** 従来方式。最後にまとめて受け取る。 */
async function sendAtOnce(text, imageTokens) {
    try {
        const r = await api('/api/chat/send', { text, images: imageTokens });
        lastRole = null;
        r.items.slice(1).forEach(addItem);      // 先頭は今出したユーザー発言
        currentChatId = r.chat_id || currentChatId;
        refreshHistory();
        scrollDown();
    } catch (e) {
        addItem({ role: 'assistant', kind: 'error', message: e.message });
        scrollDown();
    }
    return true;
}

/**
 * 逐次表示。届いた文字からすぐ出す。
 * 戻り値 false は「この方式が使えなかった」の意味で、呼び出し側が従来方式に切り替える。
 */
async function sendStreaming(text, imageTokens) {
    let res;
    try {
        res = await fetch('/api/chat/stream', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text, images: imageTokens }),
        });
    } catch (e) {
        return false;
    }
    if (!res.ok || !res.body) return false;

    let node = null;          // いま書き込んでいる回答の入れ物
    let buf = '';             // 表示中の本文
    const openText = () => {
        if (node) return;
        $('#thinking')?.remove();
        lastRole = null;
        node = el('div', { class: 'streaming' });
        slot('assistant').append(node);
    };
    const closeText = () => {
        if (node && !buf.trim()) node.remove();   // 中身が無ければ跡を残さない
        else if (node) node.classList.add('is-done');   // 点滅カーソルを消す
        node = null; buf = '';
    };

    const handle = (event, data) => {
        if (event === 'delta') {
            openText();
            buf += data.text;
            node.innerHTML = mdToHtml(buf);
            scrollDown();
        } else if (event === 'text_end') {
            closeText();
        } else if (event === 'running') {
            setBusy(true, `${data.label}`);
        } else if (event === 'item') {
            closeText();
            lastRole = null;
            addItem(data);
            setBusy(true);
            scrollDown();
        } else if (event === 'end') {
            currentChatId = data.chat_id || currentChatId;
            refreshHistory();
        }
    };

    // SSE を1行ずつ組み立てる（EventSource は POST を使えないので自前で読む）
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let rest = '';
    try {
        for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            rest += dec.decode(value, { stream: true });
            const blocks = rest.split('\n\n');
            rest = blocks.pop();
            for (const block of blocks) {
                let ev = 'message', payload = '';
                for (const line of block.split('\n')) {
                    if (line.startsWith('event: ')) ev = line.slice(7).trim();
                    else if (line.startsWith('data: ')) payload += line.slice(6);
                }
                if (!payload) continue;
                try { handle(ev, JSON.parse(payload)); } catch (e) { /* 壊れた行は捨てる */ }
            }
        }
    } catch (e) {
        addItem({ role: 'assistant', kind: 'error', message: `通信が途切れました: ${e.message}` });
    }
    closeText();
    return true;
}

/* --- 起動 ------------------------------------------------------------------- */

document.addEventListener('DOMContentLoaded', () => {
    starters = window.CHAT_INIT.starters || { examples: [], tables: [] };
    showEmpty();
    wireScope();
    renderHistory(window.CHAT_INIT.history || []);
    if (currentChatId) openChat(currentChatId);

    watchScroll();
    $('#jumpDown').addEventListener('click', () => scrollDown(true));

    $('#send').addEventListener('click', () => send());
    $('#input').addEventListener('keydown', ev => {
        if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); send(); }
    });
    $('#input').addEventListener('input', ev => {
        ev.target.style.height = 'auto';
        ev.target.style.height = Math.min(200, ev.target.scrollHeight) + 'px';
    });
    $('#newChat').addEventListener('click', async () => {
        await api('/api/chat/open', { id: null });
        currentChatId = null; lastRole = null;
        clearLog(); refreshHistory();
    });
    // --- モデルの選択 ---
    loadModels();
    $('#modelPick').addEventListener('change', async ev => {
        const chosen = ev.target.value;
        try {
            modelInfo = await api('/api/models', { model: chosen });
            renderModel();
            toast(`モデルを ${modelInfo.current} にしました。`
                  + (modelInfo.vision ? '（画像も送れます）' : ''), 'ok');
        } catch (e) {
            toast(e.message, 'err');
            renderModel();               // 選択を元に戻す
        }
    });

    // --- 画像の添付（貼り付け・ドラッグ＆ドロップ）---
    $('#input').addEventListener('paste', ev => {
        const files = [...(ev.clipboardData?.files || [])]
            .filter(f => f.type.startsWith('image/'));
        if (files.length) { ev.preventDefault(); attachImages(files); }
    });

    /* ドラッグ中の判定。子要素をまたぐたびに dragleave が飛ぶので、
       enter と leave の数を数えて「本当に外へ出た」ときだけ消す。 */
    const zone = $('#dropzone');
    const hasFiles = (ev) => [...(ev.dataTransfer?.types || [])].includes('Files');
    let depth = 0;

    const show = () => {
        $('#dropzoneText').textContent = modelInfo.vision
            ? '画像をドロップして添付'
            : 'いま選ばれているモデルは画像を扱えません';
        zone.classList.toggle('is-warn', !modelInfo.vision);
        zone.classList.add('is-on');
    };
    const hide = () => { depth = 0; zone.classList.remove('is-on'); };

    document.addEventListener('dragenter', ev => {
        if (!hasFiles(ev)) return;
        depth++; show();
    });
    document.addEventListener('dragover', ev => {
        if (hasFiles(ev)) ev.preventDefault();      // これが無いと drop が飛ばない
    });
    document.addEventListener('dragleave', ev => {
        if (!hasFiles(ev)) return;
        if (--depth <= 0) hide();
    });
    document.addEventListener('drop', ev => {
        if (!hasFiles(ev)) return;
        ev.preventDefault();
        hide();
        attachImages([...(ev.dataTransfer?.files || [])]
            .filter(f => f.type.startsWith('image/')));
    });
});
