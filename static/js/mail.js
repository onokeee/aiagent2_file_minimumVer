/* メール設定。送信サーバ・差出人・送ってよい宛先を決める。

   送信は社内リレー宛（暗号化なし・認証なし）を前提にしている。
   外部SMTPを使う環境では env の SMTP_SECURITY / SMTP_USER / SMTP_PASSWORD を使う。 */

let state = {};                 // 画面で編集中の設定
const editable = () => !!window.IS_ADMIN;

function chipList(box, items, onRemove, empty) {
    box.replaceChildren();
    if (!items.length) {
        box.append(el('div', { class: 'small muted' }, empty));
        return;
    }
    box.append(el('div', { class: 'chips' }, items.map((v, i) =>
        el('span', { class: 'chip' }, v,
            editable() ? el('button', {
                class: 'chip__x', title: '削除',
                onclick: () => { onRemove(i); render(); },
            }, '×') : null))));
}

function render() {
    const s = state;

    // 送信サーバ
    $('#host').value = s.host || '';
    $('#port').value = s.port ?? 25;
    $('#timeout').value = s.timeout ?? 20;
    ['#host', '#port', '#timeout'].forEach(x => { $(x).disabled = !editable(); });

    const kv = (k, v) => el('div', { class: 'kvrow' },
        el('span', { class: 'kvrow__k' }, k), el('span', { class: 'mono' }, v));
    $('#serverInfo').replaceChildren(kv('設定ファイル', s.settings_file || ''));

    // 差出人
    const sel = $('#sender');
    const opts = [...new Set([...(s.senders || []), s.sender].filter(Boolean))];
    sel.replaceChildren(...(opts.length
        ? opts.map(v => el('option', { value: v, ...(v === s.sender ? { selected: 'selected' } : {}) }, v))
        : [el('option', { value: '' }, '（候補を追加してください）')]));
    sel.disabled = !editable();
    $('#senderName').value = s.sender_name || '';
    $('#senderName').disabled = !editable();
    chipList($('#senderList'), s.senders || [], i => {
        const removed = s.senders.splice(i, 1)[0];
        if (s.sender === removed) s.sender = s.senders[0] || '';
    }, '候補がありません。下の欄から追加してください。');

    // 宛先
    chipList($('#addrList'), s.allow_addresses || [],
        i => s.allow_addresses.splice(i, 1), '登録なし');
    // 定期取り込みの失敗の通知先（管理者）
    chipList($('#alertList'), s.alert_to || [],
        i => s.alert_to.splice(i, 1), '登録なし（通知しません）');
    const n = (s.allow_addresses || []).length;
    $('#allowState').replaceChildren(n
        ? el('div', { class: 'alert alert--ok' },
            `登録した ${n} 件のアドレスにだけ送信できます。`)
        : el('div', { class: 'alert alert--warn' },
            '宛先が1件も登録されていません。いまの状態ではどこにも送信できません。'));
    $('#domainNote').textContent = (s.allowed_domains || []).length
        ? `登録できるのは ${s.allowed_domains_label} のアドレスだけです（env の SEND_OK_MAIL_DOMAIN）。`
        : 'env の SEND_OK_MAIL_DOMAIN が未設定のため、ドメインの制限はかかっていません。';

    $('#maxRecipients').value = s.max_recipients ?? 20;
    $('#dryRun').checked = !!s.dry_run;
    ['#maxRecipients', '#dryRun'].forEach(x => { $(x).disabled = !editable(); });
    $('#dryNote').replaceChildren(s.dry_run
        ? el('div', { class: 'alert alert--info' },
            'テスト送信モードです。チャットの「送信」を押しても外にはメールが出ず、'
            + '組み立てた内容の確認だけを行います。動作を確かめてから外してください。')
        : el('div', { class: 'alert alert--warn' },
            '本番送信モードです。チャットの「送信」を押すと実際にメールが送られます。'));

    // 上部のまとめ
    const box = $('#banner');
    box.replaceChildren();
    if ((s.problems || []).length) {
        box.append(el('div', { class: 'alert alert--err' },
            el('div', {}, 'このままではメールを送れません:'),
            el('ul', {}, s.problems.map(p => el('li', {}, p)))));
    } else {
        box.append(el('div', { class: 'alert alert--ok' },
            `送信できる状態です（${s.sender_name ? s.sender_name + ' <' + s.sender + '>' : s.sender}`
            + ` ${s.host}:${s.port}）。`));
    }
    if (!editable()) {
        box.append(el('div', { class: 'alert alert--info' },
            '設定の変更は管理者のみです。内容の確認だけできます。'));
    }
    ['#save', '#test', '#addSender', '#addAddr']
        .forEach(x => { const b = $(x); if (b) b.disabled = !editable(); });
}

function renderLog(list) {
    $('#logCount').textContent = list.length;
    const box = $('#logList');
    if (!list.length) {
        box.replaceChildren(el('div', { class: 'small muted' }, 'まだ送信していません。'));
        return;
    }
    box.replaceChildren(dataTable(
        ['日時', '結果', '宛先', '件名', '添付', 'モード', '実行者'],
        list.map(r => [
            (r.at || '').replace('T', ' '),
            r.ok ? '成功' : '失敗',
            (r.to || []).join(', ') + (r.bcc_count ? `（+Bcc ${r.bcc_count}）` : ''),
            r.subject || '',
            (r.attachments || []).join(', '),
            r.dry_run ? 'テスト' : '本番',
            r.user || '',
        ])));
}

function addTo(key, input, normalize) {
    const v = (input.value || '').trim();
    if (!v) return;
    const value = normalize ? normalize(v) : v;
    state[key] = state[key] || [];
    if (state[key].some(x => x.toLowerCase() === value.toLowerCase())) {
        toast('すでに登録されています。', 'warn');
        return;
    }
    state[key].push(value);
    input.value = '';
    if (key === 'senders' && !state.sender) state.sender = value;
    render();
}

async function load() {
    const r = await api('/api/mail/settings', undefined, 'GET');
    state = r;
    render();
    renderLog(r.log || []);
}

document.addEventListener('DOMContentLoaded', () => {
    state = window.MAIL || {};
    render();
    renderLog(window.MAIL_LOG || []);

    // 送信サーバ
    $('#host').addEventListener('input', ev => { state.host = ev.target.value.trim(); });
    $('#timeout').addEventListener('input',
        ev => { state.timeout = parseInt(ev.target.value || '20', 10); });
    $('#port').addEventListener('input',
        ev => { state.port = parseInt(ev.target.value || '25', 10); });

    $('#sender').addEventListener('change', ev => { state.sender = ev.target.value; });
    $('#senderName').addEventListener('input', ev => { state.sender_name = ev.target.value; });
    $('#maxRecipients').addEventListener('input',
        ev => { state.max_recipients = parseInt(ev.target.value || '20', 10); });
    $('#dryRun').addEventListener('change', ev => {
        state.dry_run = ev.target.checked;
        render();
    });

    $('#addSender').addEventListener('click', () => addTo('senders', $('#newSender')));
    $('#addAddr').addEventListener('click', () => {
        // 保存時にも弾かれるが、その場で言った方が直しやすい
        const v = ($('#newAddr').value || '').trim();
        const doms = state.allowed_domains || [];
        const dom = v.toLowerCase().split('@').pop();
        if (v && doms.length && !doms.some(d => dom === d || dom.endsWith('.' + d))) {
            toast(`${state.allowed_domains_label} のアドレスだけ登録できます。`, 'warn', 7000);
            return;
        }
        addTo('allow_addresses', $('#newAddr'));
    });
    $('#addAlert').addEventListener('click', () => {
        const v = ($('#newAlert').value || '').trim();
        const doms = state.allowed_domains || [];
        const dom = v.toLowerCase().split('@').pop();
        if (v && doms.length && !doms.some(d => dom === d || dom.endsWith('.' + d))) {
            toast(`${state.allowed_domains_label} のアドレスだけ登録できます。`, 'warn', 7000);
            return;
        }
        addTo('alert_to', $('#newAlert'));
    });
    [['#newSender', '#addSender'], ['#newAddr', '#addAddr'], ['#newAlert', '#addAlert']].forEach(([i, b]) =>
        $(i).addEventListener('keydown', ev => { if (ev.key === 'Enter') $(b).click(); }));

    $('#save').addEventListener('click', async ev => {
        ev.target.disabled = true;
        try {
            const r = await api('/api/mail/settings', {
                host: state.host, port: state.port, timeout: state.timeout,
                sender: state.sender, sender_name: state.sender_name,
                senders: state.senders || [],
                allow_addresses: state.allow_addresses || [],
                alert_to: state.alert_to || [],
                max_recipients: state.max_recipients,
                dry_run: state.dry_run,
            });
            state = { ...state, ...r };
            toast('保存しました。', 'ok');
            render();
        } catch (e) { toast(e.message, 'err', 9000); }
        ev.target.disabled = false;
    });

    $('#test').addEventListener('click', async ev => {
        ev.target.disabled = true;
        ev.target.innerHTML = '<span class="spinner"></span> 確認中';
        try {
            const r = await api('/api/mail/test', {});
            toast(r.message, r.ok ? 'ok' : 'err', 9000);
        } catch (e) { toast(e.message, 'err', 9000); }
        ev.target.disabled = false;
        ev.target.textContent = '送信サーバへの接続を確認';
    });

    $('#reload').addEventListener('click', load);
});
