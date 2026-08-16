/* ER図キャンバス。ライブラリなしで、テーブル移動・関連のドラッグ作成・
   多重度の変更・削除・元に戻す／やり直す・拡大縮小をまかなう。

   座標系は2つ。
     world  … ノードが持つ論理座標（.meta.yaml の er_layout と同じ）
     screen … 画面のピクセル。world に translate(tx,ty) scale(k) をかけたもの */

const ER = (() => {
    let data = { nodes: [], edges: [], alias: '' };
    let view = { tx: 40, ty: 40, k: 1 };
    let selected = null;          // {type:'edge'|'table', ...}
    // 履歴（元に戻す／やり直す）。ER図上のすべての操作を1手ずつ戻せる。
    // 操作ごとに「戻す手順」「やり直す手順」を関数で積む（コマンド方式）。
    //   移動                       … 画面の中だけ動かす（保存は 💾）
    //   関連の追加・削除・多重度 /
    //   主キー / 他DBテーブルの出し入れ … サーバに保存済みなので、逆の操作をサーバに送って戻す
    let past = [], future = [];
    const HIST_MAX = 50;
    let savedLayout = '';         // 最後に保存（または読み込み）した配置。💾 の活性判定に使う
    let root, viewport, world, svg, panel;
    // 読み取り専用（チャットからの表示）。編集の入口だけを閉じ、
    // 移動・パン・ズーム・全画面はそのまま使えるようにする。
    let ro = false;
    let docWired = false;         // documentへのキーハンドラは1回だけ張る
    // 過去の分析で実際に使われた結合の回数（{ "a.t.c||a.t.c": n }）。
    // 宣言された関連の上に「本当に通っている道」を重ねるためのもの。
    let usage = null;
    let showUsage = true;
    // 他DBから「こちらを参照している」テーブル。マスタ系DBでは数が多くなり、
    // 自分のテーブルが埋もれるので既定では出さない。
    let showIncoming = false;

    const NS = 'http://www.w3.org/2000/svg';
    const CARDS = ['N:1', '1:N', '1:1', 'N:M'];

    /* --- 描画 ---------------------------------------------------------------- */

    function applyView() {
        world.style.transform = `translate(${view.tx}px, ${view.ty}px) scale(${view.k})`;
        svg.style.transform = world.style.transform;
    }

    function tableEl(n) {
        // 他DBから借りたテーブルは薄く・破線で描き、どのDBのものかを見出しに出す。
        // 中身の編集はそのDBのカタログで行う（ここでは線をつなぐためだけに置く）
        const box = el('div', {
            class: 'ertable' + (n.external ? ' ertable--ext' : ''),
            'data-id': n.id, style: `left:${n.x}px; top:${n.y}px`,
        },
            el('div', { class: 'ertable__head' },
                n.external ? el('div', { class: 'ertable__db' }, n.alias) : null,
                el('div', {}, n.table),
                el('div', { class: 'rows' },
                    n.rows === null || n.rows === undefined ? '行数不明' : `${n.rows.toLocaleString()}行`)));
        n.columns.forEach(c => {
            box.append(el('div', {
                class: 'ercol' + (c.pk ? ' pk' : '') + (c.fk ? ' fk' : ''),
                'data-col': c.name,
            },
                // 接続点は左右どちらにも置く。相手が左にあるときは左から引けたほうが自然なため。
                el('div', { class: 'erhandle erhandle--l', 'data-handle': c.name, 'data-side': 'left' }),
                el('span', { class: 'ercol__name' }, c.name),
                el('span', { class: 'ercol__type' }, c.type),
                el('div', { class: 'erhandle erhandle--r', 'data-handle': c.name, 'data-side': 'right' })));
        });
        return box;
    }

    /** 列の接続点（world座標）。DOMの実寸から取るのでフォント差に強い。 */
    function anchor(tableId, colName, side) {
        const box = world.querySelector(`.ertable[data-id="${CSS.escape(tableId)}"]`);
        if (!box) return null;
        const node = data.nodes.find(n => n.id === tableId);
        const col = box.querySelector(`.ercol[data-col="${CSS.escape(colName)}"]`);
        const y = node.y + (col ? col.offsetTop + col.offsetHeight / 2 : 14);
        const x = side === 'left' ? node.x : node.x + box.offsetWidth;
        return { x, y, w: box.offsetWidth };
    }

    function edgePath(e) {
        const [fa, ft, fc] = e.from, [ta, tt, tc] = e.to;
        const fid = `${fa}.${ft}`, tid = `${ta}.${tt}`;
        const fn = data.nodes.find(n => n.id === fid), tn = data.nodes.find(n => n.id === tid);
        if (!fn || !tn) return null;
        const fromRight = fn.x <= tn.x;
        const a = anchor(fid, fc, fromRight ? 'right' : 'left');
        const b = anchor(tid, tc, fromRight ? 'left' : 'right');
        if (!a || !b) return null;
        const dx = Math.max(40, Math.abs(b.x - a.x) * 0.45);
        const c1 = fromRight ? a.x + dx : a.x - dx;
        const c2 = fromRight ? b.x - dx : b.x + dx;
        // 3次ベジェの中点。t=0.5 を代入すると (P0 + 3P1 + 3P2 + P3) / 8 になる
        const mid = { x: (a.x + 3 * c1 + 3 * c2 + b.x) / 8, y: (a.y + 3 * a.y + 3 * b.y + b.y) / 8 };
        return { d: `M ${a.x} ${a.y} C ${c1} ${a.y}, ${c2} ${b.y}, ${b.x} ${b.y}`, a, b, mid };
    }

    /* 利用状況のキー。端点の並び順に依らないよう、文字列順で正規化する */
    function usageKey(e) {
        const a = e.from.join('.'), b = e.to.join('.');
        return a <= b ? `${a}||${b}` : `${b}||${a}`;
    }

    /* --- 利用回数の色（濃さ）------------------------------------------------------
       回数は片寄る（よく使う1本が何十回、残りは0〜数回）ので、対数で段を作る。
       いちばん多い線を濃さ1として、その中での位置で色を決める。 */

    function rgb(varName) {
        const v = getComputedStyle(document.documentElement)
            .getPropertyValue(varName).trim();
        const m = v.match(/^#?([0-9a-f]{6})$/i);
        if (m) {
            const n = parseInt(m[1], 16);
            return [n >> 16 & 255, n >> 8 & 255, n & 255];
        }
        const p = v.match(/\d+/g);
        return p ? p.slice(0, 3).map(Number) : [128, 128, 128];
    }

    /** 未使用（薄い）→ よく使う（濃い）の間を、0〜1の位置で混ぜる。 */
    function ramp(t, hot) {
        const a = rgb('--border-2'), b = rgb(hot);
        const c = a.map((v, i) => Math.round(v + (b[i] - v) * Math.min(1, Math.max(0, t))));
        return `rgb(${c[0]},${c[1]},${c[2]})`;
    }

    /** その図の中で、いちばん多く使われた回数（濃さの基準）。 */
    function usageMax() {
        if (!usage) return 0;
        return data.edges.reduce((m, e) => Math.max(m, usage[usageKey(e)] || 0), 0);
    }

    function drawEdges() {
        svg.replaceChildren();
        const marks = [], counts = [];
        const top = usageMax();
        data.edges.forEach(e => {
            const p = edgePath(e);
            if (!p) return;
            const on = selected?.type === 'edge' && selected.id === e.id;
            // 実際に使われた回数。null は「重ね表示オフ or 未取得」
            const count = (usage && showUsage) ? (usage[usageKey(e)] || 0) : null;

            // 当たり判定用の太い透明な線。見える線は細いので、
            // これが無いと1px幅を狙わされて実質クリックできない。
            const hit = document.createElementNS(NS, 'path');
            hit.setAttribute('d', p.d);
            hit.setAttribute('fill', 'none');
            hit.setAttribute('stroke', 'transparent');
            hit.setAttribute('stroke-width', '16');
            hit.style.cursor = 'pointer';
            if (count !== null) {
                const tip = document.createElementNS(NS, 'title');
                tip.textContent = count
                    ? `過去の分析で ${count} 回使われた結合`
                    : '過去の分析では一度も使われていない結合（検算されていない経路）';
                hit.append(tip);
            }
            // pointerdown を止めるのが肝。止めないとキャンバスのパン処理が走り、
            // その中の再描画でこのパス自体が差し替わって click が発火しなくなる。
            hit.addEventListener('pointerdown', ev => ev.stopPropagation());
            hit.addEventListener('click', ev => { ev.stopPropagation(); if (!ro) selectEdge(e); });
            svg.append(hit);

            const path = document.createElementNS(NS, 'path');
            path.setAttribute('d', p.d);
            path.setAttribute('fill', 'none');
            // 太さは使わず、色の濃さで回数を表す。太さを変えると、
            // 線が重なったときにどれが太いのか分からなくなるため。
            // DBまたぎは線の刻み方（破線の長さ）で見分ける。
            path.setAttribute('stroke-width', on ? 2.6 : 1.6);
            if (on) {
                path.setAttribute('stroke', 'var(--accent)');
            } else if (count === null) {
                path.setAttribute('stroke', e.kind === 'fk' ? 'var(--muted)' : 'var(--text)');
            } else {
                // 対数で位置を出す。1回でもはっきり色が付くよう下駄を履かせる
                const t = top > 0 && count > 0
                    ? 0.25 + 0.75 * (Math.log(1 + count) / Math.log(1 + top))
                    : 0;
                path.setAttribute('stroke', ramp(t, e.cross ? '--cross' : '--accent'));
            }
            if (e.kind === 'fk') path.setAttribute('stroke-dasharray', '5 4');
            else if (e.cross) path.setAttribute('stroke-dasharray', '11 5');
            path.setAttribute('pointer-events', 'none');   // 当たり判定は hit に任せる
            svg.append(path);

            // 多重度は線の両端に置く（IPA表記なので矢印は使わない）
            const parts = String(e.label || '').split(/[-]/).map(s => s.trim()).filter(Boolean);
            const [l, r] = parts.length === 2 ? parts : ['*', '1'];
            marks.push([p.a, l, p.a.x < p.b.x ? 14 : -14, on],
                       [p.b, r, p.b.x > p.a.x ? -14 : 14, on]);

            // 累積の使用回数を線の中ほどに置く。0 は「一度も検算されていない経路」
            if (count !== null) counts.push([p.mid, count]);
        });
        marks.forEach(([pt, text, dx, on]) => {
            if (!text) return;
            const t = document.createElementNS(NS, 'text');
            t.setAttribute('x', pt.x + dx);
            t.setAttribute('y', pt.y + 4);
            t.setAttribute('text-anchor', 'middle');
            t.setAttribute('class', 'er__edgelabel' + (on ? ' is-selected' : ''));
            t.textContent = text;
            svg.append(t);
        });

        // 使用回数は最後にまとめて描く。線の上に重なるので、
        // 白抜きの座布団を敷いてから数字を置く（細い線の上でも読めるように）
        counts.forEach(([pt, n]) => {
            const label = n ? `${n.toLocaleString()}回` : '未使用';
            // 和文と数字で字幅が倍ほど違うので、文字種ごとに足す
            const w = [...label].reduce((s, c) => s + (/[　-鿿]/.test(c) ? 10.5 : 6), 0) + 10;
            const h = 15;
            const box = document.createElementNS(NS, 'rect');
            box.setAttribute('x', pt.x - w / 2); box.setAttribute('y', pt.y - h / 2);
            box.setAttribute('width', w); box.setAttribute('height', h);
            box.setAttribute('rx', 7);
            box.setAttribute('class', 'er__countbg' + (n ? '' : ' is-zero'));
            svg.append(box);

            const t = document.createElementNS(NS, 'text');
            t.setAttribute('x', pt.x);
            t.setAttribute('y', pt.y + 4);
            t.setAttribute('text-anchor', 'middle');
            t.setAttribute('class', 'er__count' + (n ? '' : ' is-zero'));
            t.textContent = label;
            svg.append(t);
        });
    }

    /** 画面に出すノード。隠したノードへ向かう線は anchor() が null を返すので
        自動的に描かれない（edgePath 側で特別扱いしなくてよい）。 */
    function shownNodes() {
        return data.nodes.filter(n => showIncoming || !n.incoming);
    }

    function render() {
        world.replaceChildren(...shownNodes().map(tableEl));
        wireNodes();
        drawEdges();
        applyView();
        syncIncomingUi();
    }

    function syncIncomingUi() {
        const btn = $('#erIncoming');
        if (!btn) return;
        const n = data.nodes.filter(x => x.incoming).length;
        btn.classList.toggle('hidden', !n);
        btn.classList.toggle('btn--primary', showIncoming);
        btn.textContent = showIncoming ? `参照元を隠す（${n}）` : `参照元を表示（${n}）`;
    }

    /* --- 選択パネル ------------------------------------------------------------ */

    function closePanel() {
        selected = null; panel.classList.add('hidden');
        panel.classList.remove('er__panel--wide', 'er__panel--max');
        drawEdges(); syncSelection();
    }

    /* パネルの見出し（タイトル・最大化・閉じる）。3種類のパネルで同じ形にする。 */
    function panelHead(title, extra) {
        const maxBtn = el('button', { class: 'btn btn--sm btn--ghost', title: '最大化' });
        const syncMax = () => {
            const on = panel.classList.contains('er__panel--max');
            maxBtn.replaceChildren(icon(on ? 'minimize' : 'maximize', 'icon--sm'));
            maxBtn.title = on ? '元の大きさに戻す' : '最大化';
        };
        maxBtn.addEventListener('click', () => {
            panel.classList.toggle('er__panel--max');
            syncMax();
        });
        syncMax();
        return el('div', { class: 'row', style: 'align-items:center;flex:0 0 auto' },
            el('b', { class: 'grow' }, title),
            extra || null,
            maxBtn,
            el('button', { class: 'btn btn--sm btn--ghost', title: '閉じる',
                           onclick: closePanel }, icon('x', 'icon--sm')));
    }

    /* パネルを出す。中身は本文コンテナに入れ、境目に取っ手を付ける。
       ドラッグで決めた大きさ（width/height）は次に開くときも保つ。 */
    function showPanel(title, bodyChildren, opts) {
        panel.classList.remove('hidden');
        panel.classList.toggle('er__panel--wide', !!(opts && opts.wide));
        panel.classList.remove('er__panel--max');
        const body = el('div', { class: 'er__panel__body' }, ...(bodyChildren || []));
        panel.replaceChildren(
            el('div', { class: 'er__grip er__grip--l' }),
            el('div', { class: 'er__grip er__grip--t' }),
            el('div', { class: 'er__grip er__grip--tl' }),
            panelHead(title, opts && opts.extra),
            body);
        wirePanelResize();
        return body;
    }

    /* 左辺・上辺・左上の角をつまんで大きさを変える。パネルは右下に付いているので、
       左へ引けば広く、上へ引けば高くなる。 */
    function wirePanelResize() {
        panel.querySelectorAll('.er__grip').forEach(g => {
            g.addEventListener('pointerdown', ev => {
                ev.preventDefault(); ev.stopPropagation();
                const dirL = g.classList.contains('er__grip--l') || g.classList.contains('er__grip--tl');
                const dirT = g.classList.contains('er__grip--t') || g.classList.contains('er__grip--tl');
                const r = panel.getBoundingClientRect();
                const sx = ev.clientX, sy = ev.clientY, w0 = r.width, h0 = r.height;
                const vp = viewport.getBoundingClientRect();
                const move = e2 => {
                    if (dirL) {
                        const w = Math.max(280, Math.min(vp.width - 20, w0 + (sx - e2.clientX)));
                        panel.style.width = `${w}px`;
                    }
                    if (dirT) {
                        const h = Math.max(160, Math.min(vp.height - 20, h0 + (sy - e2.clientY)));
                        panel.style.height = `${h}px`;
                        panel.style.maxHeight = 'none';
                    }
                };
                const up = () => {
                    document.removeEventListener('pointermove', move);
                    document.removeEventListener('pointerup', up);
                };
                document.addEventListener('pointermove', move);
                document.addEventListener('pointerup', up);
            });
        });
    }

    function syncSelection() {
        world.querySelectorAll('.ertable').forEach(b =>
            b.classList.toggle('is-selected', selected?.type === 'table' && selected.id === b.dataset.id));
    }

    function selectEdge(e) {
        selected = { type: 'edge', id: e.id, edge: e };
        drawEdges(); syncSelection();
        showPanel('関連', [
            el('div', { class: 'small mono mb' },
                `${e.from.join('.')}\n${e.to.join('.')}`),
            e.kind === 'fk'
                ? el('div', { class: 'alert alert--info small' },
                    'DBに FOREIGN KEY として宣言された関連です。ここからは変更・削除できません。')
                : !e.editable
                ? el('div', { class: 'alert alert--info small' },
                    `この関連は ${e.owner} 側で管理されています。`
                    + `変更するには、右上のプルダウンで ${e.owner} に切り替えてください。`)
                : el('div', {},
                    el('div', { class: 'small muted mb' }, `多重度（現在 ${e.cardinality}）`),
                    el('div', { class: 'row mb' }, CARDS.map(c =>
                        el('button', {
                            class: 'btn btn--sm' + (c === e.cardinality ? ' btn--primary' : ''),
                            onclick: () => mutate({ action: 'update', index: e.index, cardinality: c }),
                        }, c))),
                    el('button', {
                        class: 'btn btn--sm btn--danger',
                        onclick: () => { if (confirm('この関連を削除しますか？')) mutate({ action: 'delete', index: e.index }); },
                    }, '削除'))]);
    }

    function selectTable(id) {
        const n = data.nodes.find(x => x.id === id);
        if (!n) return;
        selected = { type: 'table', id };
        drawEdges(); syncSelection();
        // 借りたテーブルは中身をいじらせない。主キーや説明は持ち主のDBで直す
        if (n.external) {
            const extBody = el('div', { class: 'small muted mt' }, el('span', { class: 'spinner' }), ' 読み込み中...');
            showPanel(`${n.alias}.${n.table}`, [
                el('div', { class: 'alert alert--info small' },
                    `${n.alias} のテーブルです。線をつなぐために置いています。`
                    + '主キーや説明は、そのDBのカタログで編集してください。'),
                // 関連が指しているから置かれているものは、外しても線の行き先が
                // 無くなるだけなので外させない。手で足したものだけ外せる
                (!ro && n.pinned) ? el('button', {
                    class: 'btn btn--sm', style: 'margin-top:8px',
                    onclick: () => toggleExternal('remove', n.id),
                }, 'キャンバスから外す')
                : el('div', { class: 'small muted', style: 'margin-top:8px' },
                    n.incoming
                        ? `${n.alias} 側の関連がこのDBを指しているので置いています。`
                        : 'このDBの関連が指しているので置いています。関連を消すと消えます。'),
                // 借りたテーブルでも中身は見られる（説明・列・サンプル行）
                extBody,
            ], { wide: true });
            api(`/api/catalog/table-info?db=${encodeURIComponent(n.alias)}&table=${encodeURIComponent(n.table)}`,
                undefined, 'GET').then(info => {
                if (selected?.id !== id) return;
                extBody.replaceChildren(...describeParts(info, n));
            }).catch(e => extBody.replaceChildren(el('div', { class: 'alert alert--err small' }, e.message)));
            return;
        }

        // 概要・列の説明・実値・サンプル行を取りに行く（描画用の図には入れていない）。
        // 主キーの編集はカタログ画面だけ（読み取り専用のチャットでは出さない）。
        const rowsBadge = (n.rows !== null && n.rows !== undefined)
            ? el('span', { class: 'muted small', style: 'margin-right:6px' },
                 `${Number(n.rows).toLocaleString()}行`) : null;
        const body = el('div', { class: 'small muted' }, el('span', { class: 'spinner' }), ' 読み込み中...');
        showPanel(`${n.table}`, [body], { wide: true, extra: rowsBadge });

        api(`/api/catalog/table-info?db=${encodeURIComponent(n.alias)}&table=${encodeURIComponent(n.table)}`,
            undefined, 'GET').then(info => {
            if (selected?.id !== id) return;          // 読んでいる間に別のものを選んだ
            body.replaceChildren(...describeParts(info, n));
        }).catch(e => {
            body.replaceChildren(el('div', { class: 'alert alert--err small' }, e.message));
        });
    }

    /** テーブルの中身（概要・列・サンプル行・主キー編集）。パネルの中身を作る。 */
    function describeParts(info, n) {
        const parts = [];
        parts.push(el('div', { class: 'small', style: 'margin:6px 0 8px' },
            info.description
                ? el('span', {}, info.description,
                    info.ai_draft ? el('span', { class: 'badge badge--accent', style: 'margin-left:6px' }, 'AI下書き') : null)
                : el('span', { class: 'muted' }, '説明はまだ書かれていません。')));

        // 列（型・PK・説明・実際の値）
        const rows = (info.columns || []).map(c => el('tr', {},
            el('td', {}, c.name, c.pk ? el('span', { class: 'badge', style: 'margin-left:4px' }, 'PK') : null),
            el('td', { class: 'muted' }, c.type),
            el('td', {}, c.description || el('span', { class: 'muted' }, '—')),
            el('td', { class: 'muted', title: c.actual }, c.actual || '')));
        parts.push(el('div', { class: 'tablewrap', style: 'max-height:200px' },
            el('table', { class: 'data' },
                el('thead', {}, el('tr', {}, ['列', '型', '説明', '実際の値'].map(h => el('th', {}, h)))),
                el('tbody', {}, rows))));

        // 用語（このテーブル固有）
        const gl = Object.entries(info.glossary || {});
        if (gl.length) {
            parts.push(el('div', { class: 'small', style: 'margin-top:8px' },
                el('b', {}, '業務用語: '),
                gl.map(([t, e]) => `${t}（${e.description || e.sql || ''}）`).join('、')));
        }

        // サンプル行（全体は別タブのビューアで見る）
        if ((info.sample_rows || []).length) {
            parts.push(el('div', { class: 'row', style: 'align-items:center;margin:8px 0 2px' },
                el('span', { class: 'small muted' }, 'サンプル行'),
                el('div', { class: 'spacer' }),
                tableViewLink(info.alias || info.db, info.table)));
            parts.push(el('div', { class: 'tablewrap', style: 'max-height:160px' },
                dataTable(info.sample_columns || [], info.sample_rows || [])));
        }

        // 主キーの編集（カタログ画面だけ。読み取り専用では出さない）
        if (!ro && !n.external) {
            const checks = n.columns.map(c => el('label',
                { style: 'display:flex;gap:6px;align-items:center;font-size:12px' },
                el('input', { type: 'checkbox', 'data-pk': c.name, ...(c.pk ? { checked: 'checked' } : {}) }),
                c.name));
            parts.push(el('details', { class: 'mt' },
                el('summary', { class: 'small muted', style: 'cursor:pointer' }, '主キーを直す（鍵＝実線の下線）'),
                el('div', { style: 'max-height:150px;overflow:auto;margin:6px 0' }, checks),
                el('button', {
                    class: 'btn btn--primary btn--sm',
                    onclick: async () => {
                        const cols = [...panel.querySelectorAll('[data-pk]')]
                            .filter(c => c.checked).map(c => c.dataset.pk);
                        const before = n.columns.filter(c => c.pk).map(c => c.name);
                        try {
                            await pkApi(n.table, cols);
                            closePanel(); toast('主キーを保存しました。');
                            record({ label: `${n.table} の主キーを変更`,
                                     undo: () => pkApi(n.table, before),
                                     redo: () => pkApi(n.table, cols) });
                        } catch (e) { toast(e.message, 'err'); }
                    },
                }, '主キーを保存')));
        }
        return parts;
    }

    /* --- 変更（サーバに保存して描き直す） ---------------------------------------- */

    /* --- 履歴 ---------------------------------------------------------------- */

    function record(entry) {
        past.push(entry);
        if (past.length > HIST_MAX) past.shift();
        future = [];                      // 新しい操作をしたら「やり直す」先は消える
        syncHistoryUi();
    }

    async function undo() {
        const e = past.pop();
        if (!e) return;
        try { await e.undo(); future.push(e); toast(`元に戻しました: ${e.label}`); }
        catch (err) { toast(`元に戻せませんでした: ${err.message}`, 'err'); }
        syncHistoryUi();
    }

    async function redo() {
        const e = future.pop();
        if (!e) return;
        try { await e.redo(); past.push(e); toast(`やり直しました: ${e.label}`); }
        catch (err) { toast(`やり直せませんでした: ${err.message}`, 'err'); }
        syncHistoryUi();
    }

    function layoutSnapshot() {
        return JSON.stringify(data.nodes.map(n => [n.id, Math.round(n.x), Math.round(n.y)]).sort());
    }

    /* ボタンの活性と、次に戻す／やり直す操作名をツールチップに出す */
    function syncHistoryUi() {
        const u = $('#erUndo'), r = $('#erRedo'), sv = $('#erSave');
        if (u) {
            u.disabled = !past.length;
            u.dataset.tip = past.length ? `元に戻す（Ctrl+Z）: ${past[past.length - 1].label}`
                                        : '元に戻す（Ctrl+Z）\nまだ操作していません。';
        }
        if (r) {
            r.disabled = !future.length;
            r.dataset.tip = future.length ? `やり直す（Ctrl+Y）: ${future[future.length - 1].label}`
                                          : 'やり直す（Ctrl+Y）\n戻した操作はありません。';
        }
        if (sv) {
            const dirty = layoutSnapshot() !== savedLayout;
            sv.disabled = !dirty;
            sv.dataset.tip = dirty ? '配置を保存（Ctrl+S）\nテーブルの位置に、保存していない変更があります。'
                                   : '配置を保存（Ctrl+S）\nテーブルの位置は保存済みです。関連・主キーなどは操作した時点で保存されています。';
        }
    }

    function setPos(id, pos) {
        const n = data.nodes.find(x => x.id === id);
        if (n) { n.x = pos.x; n.y = pos.y; }
        render(); syncHistoryUi();
    }

    /** サーバから返ってきた図を反映する。画面上の位置は動かさない
        （まだ保存していない配置を、関連を1本足しただけで捨てないため）。 */
    function applyEr(er) {
        const positions = Object.fromEntries(data.nodes.map(n => [n.id, [n.x, n.y]]));
        data = er;
        data.nodes.forEach(n => { if (positions[n.id]) [n.x, n.y] = positions[n.id]; });
        render(); syncHistoryUi();
    }

    /* 関連API を1回叩いて図を反映する（履歴には積まない。undo/redo からも使う） */
    async function relApi(body) {
        const r = await api('/api/catalog/relationship', { db: CAT.db, ...body });
        if (r.check) return r;                 // 実データ判定で止まった
        applyEr(r.er); closePanel();
        return r;
    }

    /* 人の操作から呼ぶ。サーバに保存したうえで、逆の操作を履歴に積む */
    async function mutate(body) {
        try {
            const r = await relApi(body);
            // 実データを見て「結ぶべきでない／要確認」と判定されたら、理由を出して止める
            if (r.check) { showLinkCheck(r, body); return; }
            if (body.action === 'add' && r.added) {
                const a = r.added;
                record({ label: `関連を追加（${a.from} → ${a.to}）`,
                         undo: () => relApi({ action: 'delete', from: a.from, to: a.to }),
                         // 一度通した線なので、やり直しでは確認（warn）を飛ばす。block は元々通らない
                         redo: () => relApi({ action: 'add', from: a.from, to: a.to,
                                              cardinality: a.cardinality, force: true }) });
            } else if (body.action === 'delete' && r.removed) {
                const a = r.removed;
                record({ label: `関連を削除（${a.from} → ${a.to}）`,
                         undo: () => relApi({ action: 'add', from: a.from, to: a.to,
                                              cardinality: a.cardinality, force: true }),
                         redo: () => relApi({ action: 'delete', from: a.from, to: a.to }) });
            } else if (body.action === 'update' && r.updated) {
                const a = r.updated;
                record({ label: `多重度を ${a.previous} → ${a.cardinality}（${a.from}）`,
                         undo: () => relApi({ action: 'update', from: a.from, to: a.to, cardinality: a.previous }),
                         redo: () => relApi({ action: 'update', from: a.from, to: a.to, cardinality: a.cardinality }) });
            }
        } catch (e) { toast(e.message, 'err'); }
    }

    async function extApi(action, table) {
        const r = await api('/api/catalog/er-external', { db: CAT.db, action, table });
        applyEr(r.er); closePanel();
        return r;
    }

    /* 他DBのテーブルを図に置く／外す（履歴に積む） */
    async function toggleExternal(action, table) {
        try {
            await extApi(action, table);
            toast(action === 'add' ? `${table} を図に置きました。` : `${table} を外しました。`);
            record({ label: action === 'add' ? `${table} を図に置く` : `${table} を図から外す`,
                     undo: () => extApi(action === 'add' ? 'remove' : 'add', table),
                     redo: () => extApi(action, table) });
        } catch (e) { toast(e.message, 'err'); }
    }

    async function pkApi(table, columns) {
        const r = await api('/api/catalog/primary-key', { db: CAT.db, table, columns });
        applyEr(r.er);
        return r;
    }

    /* 線を引いた先が結べない／結ぶべきでないときのパネル。
       なぜだめかを実データの数字つきで並べる。警告どまりなら「それでも登録する」を出す。
       ER図の線は「この列で JOIN してよい」というAIへの指示なので、成立しない線を
       黙って登録させない。 */
    function showLinkCheck(r, body) {
        const check = r.check;
        const blocked = check.level === 'block';
        const LV = { block: ['alert--err', '結べません'],
                     warn:  ['alert--warn', '確認してください'],
                     info:  ['alert--info', '参考'] };
        const items = check.issues.map(i => {
            const [cls, label] = LV[i.level] || LV.info;
            return el('div', { class: `alert ${cls} small`, style: 'margin:6px 0' },
                el('div', {}, el('b', {}, i.title), ' ', el('span', { class: 'muted' }, `（${label}）`)),
                el('div', { style: 'margin-top:3px' }, i.detail));
        });
        const buttons = el('div', { class: 'row mt', style: 'gap:8px;justify-content:flex-end' },
            el('button', { class: 'btn btn--sm', onclick: closePanel }, blocked ? '閉じる' : 'やめる'),
            blocked ? null
                    : el('button', { class: 'btn btn--sm btn--primary',
                                     onclick: () => mutate({ ...body, force: true }) },
                         'それでも登録する'));
        showPanel(blocked ? 'この線は結べません' : 'この線でよいですか？', [
            el('div', { class: 'small mono mb' }, `${r.from} → ${r.to}（${r.cardinality}）`),
            el('div', { class: 'small muted' },
                blocked
                    ? '実データを見ると、この2列で JOIN しても結果が出ません。列の選び間違いです。'
                    : '実データを見ると気になる点があります。意味を確かめてから登録してください。'),
            ...items,
            el('div', { class: 'small muted mt' },
                'ER図の線は「この列で結合してよい」というAIへの指示です。' +
                '成立しない線を引くと、AIが自信を持って間違った結合を書くようになります。'),
            buttons,
        ], { wide: true });
    }

    /* --- 操作 ------------------------------------------------------------------ */

    function wireNodes() {
        world.querySelectorAll('.ertable').forEach(box => {
            const node = data.nodes.find(n => n.id === box.dataset.id);

            $('.ertable__head', box).addEventListener('pointerdown', ev => {
                ev.stopPropagation();
                const sx = ev.clientX, sy = ev.clientY, ox = node.x, oy = node.y;
                let moved = false;
                const move = e2 => {
                    node.x = ox + (e2.clientX - sx) / view.k;
                    node.y = oy + (e2.clientY - sy) / view.k;
                    box.style.left = `${node.x}px`; box.style.top = `${node.y}px`;
                    moved = true; drawEdges();
                };
                const up = () => {
                    document.removeEventListener('pointermove', move);
                    document.removeEventListener('pointerup', up);
                    if (!moved) { selectTable(node.id); return; }   // 読み取り専用でも中身は見られる
                    // 動かし終わった位置を1手として積む（クリックだけなら積まない）
                    const before = { x: ox, y: oy }, after = { x: node.x, y: node.y };
                    record({ label: `${node.table} を移動`,
                             undo: () => setPos(node.id, before),
                             redo: () => setPos(node.id, after) });
                };
                document.addEventListener('pointermove', move);
                document.addEventListener('pointerup', up);
            });

            if (!ro) box.querySelectorAll('.erhandle').forEach(h => {
                h.addEventListener('pointerdown', ev => {
                    ev.stopPropagation(); ev.preventDefault();
                    startLink(node, h.dataset.handle, h.dataset.side, ev);
                });
            });
        });
    }

    function startLink(fromNode, fromCol, side, ev) {
        const ghost = document.createElementNS(NS, 'path');
        ghost.setAttribute('fill', 'none');
        ghost.setAttribute('stroke', 'var(--accent)');
        ghost.setAttribute('stroke-width', '2');
        ghost.setAttribute('stroke-dasharray', '5 4');
        svg.append(ghost);
        const a = anchor(fromNode.id, fromCol, side || 'right');
        const out = (side === 'left') ? -60 : 60;

        const toWorld = e => {
            const r = viewport.getBoundingClientRect();
            return { x: (e.clientX - r.left - view.tx) / view.k, y: (e.clientY - r.top - view.ty) / view.k };
        };
        const move = e2 => {
            const p = toWorld(e2);
            ghost.setAttribute('d',
                `M ${a.x} ${a.y} C ${a.x + out} ${a.y}, ${p.x - out} ${p.y}, ${p.x} ${p.y}`);
        };
        const up = e2 => {
            document.removeEventListener('pointermove', move);
            document.removeEventListener('pointerup', up);
            ghost.remove();
            const target = document.elementFromPoint(e2.clientX, e2.clientY)?.closest('.ercol');
            const box = target?.closest('.ertable');
            if (!target || !box) return;
            const toNode = data.nodes.find(n => n.id === box.dataset.id);
            if (toNode.id === fromNode.id && target.dataset.col === fromCol) return;
            if (fromNode.external && toNode.external) {
                toast('どちらか一方は、いま開いているDBのテーブルにしてください。', 'err');
                return;
            }
            // 他DBのテーブルは 'DB名.テーブル名' で送る（サーバ側がDBまたぎと解する）
            const ref = n => (n.external ? n.id : n.table);
            mutate({
                action: 'add',
                from_table: ref(fromNode), from_column: fromCol,
                to_table: ref(toNode), to_column: target.dataset.col,
                cardinality: guessCardinality(fromNode, fromCol, toNode, target.dataset.col),
            });
        };
        document.addEventListener('pointermove', move);
        document.addEventListener('pointerup', up);
    }

    /** 「その列がそのテーブルの主キー全体なら 1 側」という規則で多重度を推定する。 */
    function guessCardinality(fromNode, fromCol, toNode, toCol) {
        const solePk = (node, col) => {
            const pks = node.columns.filter(c => c.pk).map(c => c.name);
            return pks.length === 1 && pks[0] === col;
        };
        const a = solePk(fromNode, fromCol), b = solePk(toNode, toCol);
        if (a && b) return '1:1';
        if (b) return 'N:1';
        if (a) return '1:N';
        return 'N:M';
    }

    /* --- 他DBのテーブルを借りる ------------------------------------------------- */

    async function openExternalPicker() {
        let list;
        try {
            list = await api(`/api/catalog/er-tables?db=${encodeURIComponent(CAT.db)}`,
                             undefined, 'GET');
        } catch (e) { return toast(e.message, 'err'); }
        if (!list.dbs.length) return toast('他のDBがありません。', 'err');

        // 関連から自動で置かれているものは外せない（外しても線の行き先として戻る）。
        // 手で足したものだけ外せる、という区別を一覧の見た目に出す。
        const placed = new Set(data.nodes.filter(n => n.external).map(n => n.id));
        const pinned = new Set(data.nodes.filter(n => n.pinned).map(n => n.id));
        const back = el('div', { class: 'modal' });
        const close = () => back.remove();
        back.addEventListener('click', ev => { if (ev.target === back) close(); });

        const body = el('div', { class: 'modal__body' });
        const rows = [];
        list.dbs.forEach(d => {
            body.append(el('div', { class: 'small muted', style: 'padding:8px 10px 2px' },
                d.title ? `${d.alias}（${d.title}）` : d.alias));
            d.tables.forEach(t => {
                const on = placed.has(t.id), fixed = on && !pinned.has(t.id);
                const row = el('div', {
                    class: 'fsrow' + (fixed ? ' is-fixed' : ''),
                    'data-key': `${d.alias} ${t.table}`,
                    title: fixed ? '関連が指しているので置かれています（外せません）' : '',
                    onclick: fixed ? null : async () => {
                        close();
                        await toggleExternal(on ? 'remove' : 'add', t.id);
                    },
                },
                    el('span', { class: 'ico' }, icon(on ? 'check' : 'plus', 'icon--sm')),
                    el('span', { class: 'name' }, t.table),
                    fixed ? el('span', { class: 'small muted' }, '関連あり') : null,
                    el('span', { class: 'small muted' },
                        t.rows === null || t.rows === undefined ? '' : `${t.rows.toLocaleString()}行`));
                rows.push(row);
                body.append(row);
            });
        });

        const filter = el('input', {
            type: 'text', style: 'width:100%', placeholder: 'テーブル名で絞り込み',
            oninput: ev => {
                const q = ev.target.value.trim().toLowerCase();
                rows.forEach(r => r.classList.toggle(
                    'hidden', !!q && !r.dataset.key.toLowerCase().includes(q)));
            },
        });

        back.append(el('div', { class: 'modal__box' },
            el('div', { class: 'modal__head' },
                el('b', { class: 'grow' }, '他DBのテーブルを図に置く'),
                el('button', { class: 'btn btn--sm btn--ghost', onclick: close },
                    icon('x', 'icon--sm'))),
            el('div', { style: 'padding:10px 14px 0' }, filter,
                el('div', { class: 'small muted mt' },
                    '置いたテーブルへは、いつもどおり列からドラッグして関連を作れます。'
                    + '関連が指しているテーブルは自動で置かれます。')),
            body));
        document.body.append(back);
        filter.focus();
        document.addEventListener('keydown', function esc(ev) {
            if (ev.key !== 'Escape') return;
            close(); document.removeEventListener('keydown', esc);
        });
    }

    function wireViewport() {
        viewport.addEventListener('pointerdown', ev => {
            if (ev.target.closest('.ertable') || ev.target.closest('.er__panel')) return;
            closePanel();
            const sx = ev.clientX, sy = ev.clientY, ox = view.tx, oy = view.ty;
            viewport.classList.add('is-panning');
            const move = e2 => { view.tx = ox + (e2.clientX - sx); view.ty = oy + (e2.clientY - sy); applyView(); };
            const up = () => {
                viewport.classList.remove('is-panning');
                document.removeEventListener('pointermove', move);
                document.removeEventListener('pointerup', up);
            };
            document.addEventListener('pointermove', move);
            document.addEventListener('pointerup', up);
        });

        viewport.addEventListener('wheel', ev => {
            ev.preventDefault();
            const r = viewport.getBoundingClientRect();
            const mx = ev.clientX - r.left, my = ev.clientY - r.top;
            const k2 = Math.min(2.5, Math.max(0.2, view.k * (ev.deltaY < 0 ? 1.12 : 0.89)));
            view.tx = mx - (mx - view.tx) * (k2 / view.k);
            view.ty = my - (my - view.ty) * (k2 / view.k);
            view.k = k2;
            applyView();
        }, { passive: false });
    }

    function fit() {
        const nodes = shownNodes();
        if (!nodes.length) return;
        const boxes = nodes.map(n => {
            const b = world.querySelector(`.ertable[data-id="${CSS.escape(n.id)}"]`);
            return { x: n.x, y: n.y, w: b?.offsetWidth || 232, h: b?.offsetHeight || 120 };
        });
        const minX = Math.min(...boxes.map(b => b.x)), minY = Math.min(...boxes.map(b => b.y));
        const maxX = Math.max(...boxes.map(b => b.x + b.w)), maxY = Math.max(...boxes.map(b => b.y + b.h));
        const r = viewport.getBoundingClientRect();
        view.k = Math.min(1.2, Math.max(0.2,
            Math.min((r.width - 80) / (maxX - minX), (r.height - 80) / (maxY - minY))));
        view.tx = 40 - minX * view.k;
        view.ty = 40 - minY * view.k;
        applyView();
    }

    async function saveLayout() {
        if (ro) return;
        const snap = layoutSnapshot();
        if (snap === savedLayout) { toast('配置は保存済みです。'); return; }
        const layout = Object.fromEntries(data.nodes.map(n => [n.id, [Math.round(n.x), Math.round(n.y)]]));
        try {
            await api('/api/catalog/layout', { db: CAT.db, layout });
            savedLayout = snap; syncHistoryUi();
            toast('配置を保存しました。');
        } catch (e) { toast(e.message, 'err'); }
    }

    function init(opts) {
        root = $('#erRoot'); viewport = $('#erViewport');
        world = $('#erWorld'); svg = $('#erSvg'); panel = $('#erPanel');
        if (!root) return;
        ro = !!(opts && opts.readonly);
        data = (opts && opts.data) || (typeof CAT !== 'undefined' ? CAT.er : null);
        if (!data) return;
        // チャットでは開くたびに init し直すので、前回の状態を持ち越さない
        view = { tx: 40, ty: 40, k: 1 };
        selected = null; past = []; future = [];
        svg.setAttribute('width', '100%'); svg.setAttribute('height', '100%');
        render();
        savedLayout = layoutSnapshot();      // 読み込んだ配置＝保存済みとみなす
        syncHistoryUi();
        wireViewport();
        setTimeout(fit, 30);

        $('#erSave')?.addEventListener('click', saveLayout);
        $('#erUndo')?.addEventListener('click', undo);
        $('#erRedo')?.addEventListener('click', redo);
        $('#erFull')?.addEventListener('click', () => {
            root.classList.toggle('er--full');
            $('#erFull').textContent = root.classList.contains('er--full') ? '全画面を終了' : '全画面';
            setTimeout(fit, 60);
        });
        $('#erExt')?.addEventListener('click', openExternalPicker);
        $('#erIncoming')?.addEventListener('click', () => {
            showIncoming = !showIncoming;
            render(); setTimeout(fit, 20);
        });
        $('#erUsage')?.addEventListener('click', () => {
            showUsage = !showUsage;
            syncUsageUi();
            drawEdges();
        });
        if (!docWired) {
            docWired = true;
            // root は init のたびに差し替わるので、この1本のハンドラで常に最新を見る
            document.addEventListener('keydown', ev => {
                if (ev.key === 'Escape' && root && root.classList.contains('er--full')) {
                    $('#erFull')?.click();
                    return;
                }
                // Ctrl+Z / Ctrl+Y(Ctrl+Shift+Z) / Ctrl+S は、ER図が見えていて
                // 入力欄にいないときだけ受ける（他のタブやチャットでは横取りしない）
                if (ro || !root || !(ev.ctrlKey || ev.metaKey)) return;
                if (!root.getClientRects().length) return;                    // 別タブで隠れている
                if (/INPUT|TEXTAREA|SELECT/.test(ev.target.tagName) || ev.target.isContentEditable) return;
                const k = ev.key.toLowerCase();
                if (k === 'z' && !ev.shiftKey) { ev.preventDefault(); undo(); }
                else if (k === 'y' || (k === 'z' && ev.shiftKey)) { ev.preventDefault(); redo(); }
                else if (k === 's') { ev.preventDefault(); saveLayout(); }
            });
        }
    }

    function syncUsageUi() {
        const btn = $('#erUsage');
        if (btn) btn.classList.toggle('btn--primary', showUsage && usage !== null);
        $('#erUsageLegend')?.classList.toggle('hidden', !(showUsage && usage !== null));
    }

    /** 利用状況を受け取って重ねる（カタログ画面が読み込み後に呼ぶ）。 */
    function setUsage(map) {
        usage = map || {};
        const btn = $('#erUsage');
        if (btn) btn.disabled = false;
        syncUsageUi();
        drawEdges();
    }

    return { init, refit: fit, mutate, setUsage };
})();
