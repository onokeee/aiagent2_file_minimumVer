"""web.py — Flaskアプリ本体と全画面（元: web/ の11モジュール）。

元は以下のファイルに分かれていた。中身は変えずに1つにまとめている:
  web/filestore.py
  web/helpers.py
  web/auth_bp.py
  web/chat_bp.py
  web/catalog_bp.py
  web/import_bp.py
  web/mail_bp.py
  web/models_bp.py
  web/table_bp.py
  web/api_bp.py
  web/__init__.py

"""
from __future__ import annotations

import sys as _sys

import core  # noqa: F401  （統合前のモジュール名を登録させる）

filestore = _sys.modules[__name__]  # 統合前の書き方をそのまま使えるようにする


# ==========================================================================
# ===== 元 web/filestore.py
# 生成ファイル（Excel/CSV/テキスト）の一時置き場。
#
# ツールが作るのはバイト列なので、ブラウザに渡すには一度サーバ側に置いて
# ダウンロードURLを発行する必要がある。ディスクには書かない（Streamlit版と同じ方針）。
# ==========================================================================
import secrets
import threading
from collections import OrderedDict

_MAX_ITEMS = 200          # 保持する本数。古いものから捨てる
_lock = threading.Lock()
_files: OrderedDict[str, dict] = OrderedDict()


def put(data: bytes, filename: str, mime: str, owner: str) -> str:
    token = secrets.token_urlsafe(16)
    with _lock:
        _files[token] = {"data": data, "filename": filename, "mime": mime, "owner": owner}
        while len(_files) > _MAX_ITEMS:
            _files.popitem(last=False)
    return token


def get(token: str, owner: str) -> dict | None:
    """本人が作ったファイルだけ返す（URLを推測されても他人のものは渡さない）。"""
    with _lock:
        item = _files.get(token)
    if item is None or item["owner"] != owner:
        return None
    return item


# ==========================================================================
# ===== 元 web/helpers.py
# 画面まわりの共通処理: ログイン状態・スコープ・描画用の変換。
# ==========================================================================
import functools
import json
import re
from pathlib import Path

from flask import g, jsonify, redirect, render_template, request, session, url_for

import auth
import catalog
import config
import db

_USER_KEY = "user"


# --- ログイン -----------------------------------------------------------------

def load_user_into_context():
    """毎リクエストの冒頭でログイン中のユーザーを復元する。"""
    data = session.get(_USER_KEY)
    g.user = auth.User(**data) if data else None


def login_user(user: auth.User) -> None:
    session[_USER_KEY] = {"username": user.username, "display_name": user.display_name,
                          "groups": list(user.groups), "is_admin": user.is_admin}
    session.permanent = False


def logout_user() -> None:
    session.clear()


def login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **kw):
        if g.get("user") is None:
            if request.path.startswith("/api/"):
                return jsonify({"error": "ログインしてください。"}), 401
            return redirect(url_for("auth.login", next=request.path))
        return view(*a, **kw)
    return wrapped


def admin_required(view):
    """管理者だけが通れる。ログインしていなければログイン画面へ。

    データカタログ・データ取り込み・メール設定は、間違えると全員に影響が出る
    （AIの回答の土台、DBの中身、送信先）ので、閲覧も含めて管理者に限る。
    画面側でメニューを隠すだけでは、URLを直に叩かれると素通りしてしまう。
    """
    @functools.wraps(view)
    def wrapped(*a, **kw):
        user = g.get("user")
        if user is None:
            if request.path.startswith("/api/"):
                return jsonify({"error": "ログインしてください。"}), 401
            return redirect(url_for("auth.login", next=request.path))
        if not user.is_admin:
            if request.path.startswith("/api/"):
                return jsonify({"error": "この操作は管理者のみです。"}), 403
            return render_template("403.html"), 403
        return view(*a, **kw)
    return wrapped


def inject_globals() -> dict:
    """全テンプレートで使う値。"""
    return {"user": g.get("user"), "app_title": config.APP_TITLE,
            "nav": request.endpoint or ""}


# --- 分析スコープ（どのDBのどのテーブルを見るか） --------------------------------

def db_files() -> list[Path]:
    return db.list_db_files()


def build_scope(selection: dict) -> list[dict]:
    """{DBファイル名: [テーブル名, ...]} から scope を組み立てる。

    scope は tools/llm がそのまま受け取る形式。
    """
    files = {f.name: f for f in db_files()}
    chosen = [files[n] for n in selection if n in files]
    aliases = db.aliases_for(chosen)
    scope = []
    for f, alias in zip(chosen, aliases):
        prof = catalog.profile_db(f)
        available = list(prof["tables"].keys())
        want = [t for t in (selection.get(f.name) or available) if t in available]
        scope.append({"path": str(f), "alias": alias, "name": f.name,
                      "tables": want or available, "meta": catalog.load_meta(f)})
    return scope


def dbs_in_sql(sql: str, scope: list[dict]) -> list[dict]:
    """SQLが名前を挙げているDBを、SQLに出てくる順で返す。

    例文の保存先を決めるのに使う。例文はDBごとのファイルに残すので、
    複数のDBを選んでいても「このSQLはどのDBのものか」を決める必要がある。
    DBをまたぐSQLでは、主となるFROM句のDB（最初に出てくるもの）が先頭に来る。
    """
    def first_hit(pattern: str) -> int | None:
        m = re.search(pattern, sql, re.IGNORECASE)
        return m.start() if m else None

    # まずは「エイリアス.テーブル」の形で探す
    found = []
    for s in scope:
        alias = s.get("alias") or ""
        pos = first_hit(r'(?<![\w."])' + re.escape(alias) + r'\s*\.') if alias else None
        if pos is not None:
            found.append((pos, s))
    if found:
        found.sort(key=lambda t: t[0])
        return [s for _, s in found]

    # 修飾されていないSQL（DBを1つしか選んでいないときにAIが書く形）。
    # テーブル名で当てにいく。列名と紛れることがあるので、あくまで最後の手段。
    hits = []
    for s in scope:
        for t in (s.get("tables") or []):
            pos = first_hit(r'(?<![\w."])' + re.escape(t) + r'(?![\w"])')
            if pos is not None:
                hits.append((pos, s))
                break
    hits.sort(key=lambda t: t[0])
    out = []
    for _, s in hits:
        if s not in out:
            out.append(s)
    return out


def tables_in_sql(sql: str, scope: list[dict], limit: int = 6) -> list[dict]:
    """SQLが触れているテーブルを {db, table} で返す。

    チャットからデータカタログの該当テーブルへ飛ぶリンクを作るのに使う。
    「この列が何なのか分からない」とAIが言ったときに、その場で説明を
    書きに行けるようにするためのもの。
    """
    flat = str(sql or "").replace('"', "")       # "orders" のような引用符を外して見る
    out, seen = [], set()
    for s in scope:
        alias = str(s.get("alias") or "")
        for t in (s.get("tables") or []):
            name = str(t)
            qualified = (alias and re.search(
                r'(?<![\w.])' + re.escape(alias) + r'\s*\.\s*' + re.escape(name) + r'(?![\w])',
                flat, re.IGNORECASE))
            bare = re.search(r'(?<![\w.])' + re.escape(name) + r'(?![\w])',
                             flat, re.IGNORECASE)
            if not (qualified or bare):
                continue
            key = (s.get("name"), name)
            if key not in seen:
                seen.add(key)
                out.append({"db": s.get("name"), "table": name})
    return out[:limit]

#: 最初の画面に出す例文の上限。多すぎると選べない。
_EXAMPLE_LIMIT = 6


def scope_starters(scope: list[dict]) -> dict:
    """まだ何も話していない画面に出す「取っ掛かり」。

    例文はカタログ（各DBの .meta.yaml の examples）から取る。固定の例文を
    持たないのは、DB構成が変われば必ず嘘になるため。チャットの
    「この質問とSQLを例文として保存」で貯まるので、使うほど増えていく。

    未登録のDBでは代わりに選択中のテーブル名を出す。空のままだと
    「何を聞けるのか」の手がかりが無くなるため。
    """
    examples, tables = [], []
    for s in scope:
        for ex in (s.get("meta", {}).get("examples") or []):
            q = str(ex.get("q") or "").strip()
            if q and q not in examples:
                examples.append(q)
        tables.extend(s.get("tables") or [])
    return {"examples": examples[:_EXAMPLE_LIMIT], "tables": tables[:12]}


# --- 描画用アイテムの変換 --------------------------------------------------------

def jsonable(value):
    """JSONに載らない値（bytes / datetime など）を落として文字列にする。"""
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, float):
        # NaN / inf は JSON に無い（Excel の空セルは pandas で NaN になる）。
        # そのまま dumps すると "NaN" という不正なJSONになり、ブラウザ側で読めない。
        return None if (value != value or value in (float("inf"), float("-inf"))) else value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def render_item_for_web(item: dict) -> dict:
    """tools.dispatch が返す描画アイテムを、ブラウザに渡せる形へ。

    - グラフは plotly の JSON にしてクライアントで描く
    - ファイルは中身をサーバ側に預け、ダウンロードURLだけ渡す
    """
    import charts

    kind = item.get("kind")
    out = {k: jsonable(v) for k, v in item.items() if k not in ("data", "sheets")}

    if kind in ("chart", "chart_dual"):
        try:
            fig = (charts.build_dual_figure(item) if kind == "chart_dual"
                   else charts.build_figure(item))
            out["figure"] = json.loads(fig.to_json())
            out["kind"] = "chart"
        except Exception as e:
            out["kind"] = "error"
            out["message"] = f"グラフを描けませんでした: {e}"
    elif kind == "report_doc":
        # 節ごとのグラフはここで figure に変換する（画面はそのまま描くだけ）
        out["sections"] = []
        for s in item.get("sections") or []:
            sec = {k: v for k, v in s.items() if k != "chart"}
            if s.get("chart"):
                try:
                    fig = charts.build_figure(s["chart"])
                    sec["figure"] = json.loads(fig.to_json())
                except Exception as e:
                    sec["chart_error"] = f"グラフを描けませんでした: {e}"
            out["sections"].append(jsonable(sec))
    elif kind == "file":
        sheets = item.get("sheets") or []
        out["sheets"] = [{"name": s.get("name"), "columns": jsonable(s.get("columns")),
                          "rows": jsonable((s.get("rows") or [])[:20]),
                          "total": len(s.get("rows") or [])} for s in sheets]
    return out


# ==========================================================================
# ===== 元 web/auth_bp.py
# ログイン / ログアウト。認証の中身は auth.py のプロバイダに任せる。
# ==========================================================================
from flask import Blueprint, flash, g, redirect, render_template, request, url_for

import auth


bp_auth = Blueprint("auth", __name__)


@bp_auth.route("/login", methods=["GET", "POST"])
def login():
    if g.get("user") is not None:
        return redirect(url_for("chat.index"))

    setup_needed = False
    try:
        provider = auth.get_provider()
        # 常設の管理者で入れるなら、ユーザー未登録でも詰まらない
        if (provider.name == "local" and not auth.admin_enabled()
                and not (auth.load_users_file().get("users") or [])):
            setup_needed = True
    except auth.AuthError as e:
        return render_template("login.html", fatal=str(e))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if not username or not password:
            flash("ユーザー名とパスワードを入力してください。", "warning")
        else:
            try:
                user = auth.authenticate(username, password)
            except auth.AuthError as e:
                flash(f"認証できませんでした: {e}", "error")
            else:
                if user is None:
                    flash("ユーザー名またはパスワードが違います。", "error")
                else:
                    login_user(user)
                    nxt = request.args.get("next") or url_for("chat.index")
                    return redirect(nxt if nxt.startswith("/") else url_for("chat.index"))

    return render_template("login.html", setup_needed=setup_needed)


@bp_auth.post("/logout")
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


# ==========================================================================
# ===== 元 web/chat_bp.py
# チャット画面とエージェントループ。
#
# Streamlit 版との違いは状態の置き場所だけで、流れは同じ。
#   質問 → LLM → tool_calls があれば実行 → 結果を返して再度LLM → 最終回答
#
# 会話の実体は chats.py（ユーザーごとのファイル）に置く。
# 使うモデルの選択は prefs.py に置く（ログアウトしても残す）。
# 対象データはユーザーが選ばず、質問ごとに _auto_scope が決める。
# セッションに持つのは「いまどの会話を開いているか」だけ。
# ==========================================================================
import json
from pathlib import Path

from flask import (Blueprint, Response, g, jsonify, render_template, request,
                   session, stream_with_context)

import catalog
import catalog_history
import chats
import config
import custom_tools
import db
import jobs
import llm
import mailer
import models
import tools
import verify


bp_chat = Blueprint("chat", __name__)

TOOL_LABELS = {
    "run_sql_query": "SQL実行 (SELECT)",
    "plot_chart": "グラフ描画",
    "plot_dual_axis": "2軸グラフ描画 (棒+折れ線)",
    "plot_comparison": "グラフ描画（比較）",
    "plot_trend": "グラフ描画（推移）",
    "plot_composition": "グラフ描画（構成）",
    "plot_distribution": "グラフ描画（分布）",
    "plot_relationship": "グラフ描画（関係）",
    "plot_kpi": "グラフ描画（指標）",
    "pivot_table": "クロス集計",
    "analyze_stats": "統計分析",
    "export_excel": "Excel作成",
    "export_csv": "CSV作成",
    "export_text": "テキスト作成",
    "export_pptx": "PowerPoint作成",
    "describe_table": "テーブル詳細の確認",
    "show_er_diagram": "ER図の表示",
    "open_table": "テーブル全体を開く",
    "hypothesis_test": "仮説検定",
    "regression": "回帰分析",
    "distribution_analysis": "分布の分析",
    "forecast": "予測",
    "timeseries_analysis": "時系列分析",
    "monte_carlo_simulation": "モンテカルロ・シミュレーション",
    "scenario_analysis": "シナリオ分析",
    "bootstrap_estimate": "信頼区間の推定",
    "clustering": "クラスタ分析",
    "abc_analysis": "ABC分析",
    "find_mail_recipients": "宛先の検索",
    "compose_email": "メールの下書き",
    "analyze_usage": "利用状況の分析",
    "propose_glossary_term": "用語登録の提案",
    "propose_example": "例文登録の提案",
}


# =============================================================================
# 画面
# =============================================================================

@bp_chat.get("/", endpoint="index")
@login_required
def chat_index():
    files = []
    for f in db.list_db_files():
        meta = catalog.load_meta(f)
        prof = catalog.profile_db(f)
        tmeta = meta.get("tables") or {}
        # サイドバーで名前にマウスを乗せたときに出す説明。カタログに書いた内容が
        # そのままAIの理解になるので、選ぶ側にも同じ説明が見えている方がよい。
        tables = [{"name": t,
                   "description": (tmeta.get(t) or {}).get("description") or "",
                   "rows": info.get("row_count"),
                   "columns": len(info.get("columns") or [])}
                  for t, info in prof["tables"].items()]
        files.append({"name": f.name, "title": meta.get("title") or "",
                      "description": catalog.db_description(meta),
                      "tables": tables})
    # 設定どおりに更新できていない定期取り込み → サイドバーのDB名・テーブル名に警告マーク
    problems = jobs.problems_by_table()
    for f in files:
        marks = []
        for t in f["tables"]:
            ps = problems.get((f["name"], t["name"]))
            if ps:
                t["problem"] = "／".join(p["message"] for p in ps)
                marks.append(t["name"])
        if marks:
            f["problem"] = f"定期取り込みが設定どおりに動いていません: {'、'.join(marks)}"
    return render_template(
        "chat.html",
        db_files=files,
        chat_id=session.get("chat_id"),
        history=chats.list_chats(g.user),
        starters=scope_starters(build_scope({f.name: [] for f in db.list_db_files()})),
        llm_ready=llm.is_configured(),
        placeholder=config.APP_INPUT_PLACEHOLDER,
        auto_download=config.AUTO_DOWNLOAD,
    )


# =============================================================================
# モデルの選択と画像
# =============================================================================

@bp_chat.get("/api/models")
@login_required
def list_models():
    return jsonify(models.status(g.user,
                                 refresh=request.args.get("refresh") == "1"))


@bp_chat.post("/api/models")
@login_required
def choose_model():
    try:
        models.choose(g.user, (request.json or {}).get("model", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, **models.status(g.user)})


@bp_chat.post("/api/chat/image")
@login_required
def upload_image():
    """画像を1枚受け取り、送信待ちとして預かる。

    ここではLLMに送らない。実際に送るのは、その画像を付けて質問したとき。
    """
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "画像が選ばれていません。"}), 400
    if not models.is_vision(models.current(g.user)):
        return jsonify({"error": "いま選ばれているモデルは画像を扱えません。"
                                 "画像に対応したモデルに切り替えてください。"}), 400
    mime = (f.mimetype or "").lower()
    if mime not in llm.IMAGE_MIMES:
        return jsonify({"error": f"この形式は送れません（{mime or '不明'}）。"
                                 "PNG / JPEG / GIF / WebP を使ってください。"}), 400
    data = f.read()
    limit = int(config.IMAGE_MAX_MB * 1024 * 1024)
    if len(data) > limit:
        return jsonify({"error": f"画像が大きすぎます（{len(data) / 1024 / 1024:.1f}MB）。"
                                 f"{config.IMAGE_MAX_MB:.0f}MB以下にしてください。"}), 400
    if not data:
        return jsonify({"error": "中身が空の画像です。"}), 400

    token = filestore.put(data, f.filename or "image.png", mime, g.user.username)
    return jsonify({"ok": True, "token": token, "filename": f.filename or "image.png",
                    "mime": mime, "size": len(data),
                    "url": f"/api/file/{token}"})


def _images_from(tokens: list) -> tuple[list, list]:
    """預かった画像を、LLMに渡せる形（base64）にする。

    戻り値は (LLM用, 画面表示用)。
    """
    import base64
    send, show = [], []
    for t in (tokens or [])[: config.IMAGE_MAX_COUNT]:
        item = filestore.get(str(t), g.user.username)
        if item is None:
            continue
        send.append({"mime": item["mime"],
                     "b64": base64.b64encode(item["data"]).decode("ascii")})
        show.append({"filename": item["filename"], "mime": item["mime"],
                     "size": len(item["data"]), "url": f"/api/file/{t}"})
    return send, show


# =============================================================================
# 会話の読み書き
# =============================================================================

def _load_current() -> dict:
    """いま開いている会話。無ければ新規の空会話。"""
    cid = session.get("chat_id")
    if cid:
        chat = chats.load_chat(g.user, cid)
        if chat:
            return chat
    return {"id": None, "title": "", "created_at": "", "messages": [], "render_log": []}


def _persist(chat: dict) -> dict:
    # 新しい会話は、何か話すまでファイルを作らない。
    # 既にある会話は空になっても保存する（巻き戻しで全部消したときに、
    # 保存済みの古いやり取りが復活してしまうため）。
    if not chat["render_log"] and not chat.get("id"):
        return chat
    if not chat.get("id"):
        chat["id"] = chats.new_id()
    # db_names は「この会話で実際にSQLが触ったDB」。開いて続きを聞いたときに
    # 同じDBをスコープへ残すために使う（_auto_scope 参照）。
    used = set(chat.get("db_names") or [])
    for i in chat["render_log"]:
        if i.get("kind") == "sql" and i.get("sql"):
            used |= set(db.dbs_named_in(str(i["sql"])))
    chat["db_names"] = sorted(used)
    saved = chats.save_chat(
        g.user, chat["id"], chat["messages"], chat["render_log"],
        db_names=chat["db_names"], tables={},
        title=chat.get("title") or "", created_at=chat.get("created_at") or "")
    session["chat_id"] = chat["id"]
    chat["title"], chat["created_at"] = saved["title"], saved["created_at"]
    return chat


def _count_turns(render_log: list[dict]) -> int:
    """ユーザーの発言が何回あったか。"""
    return sum(1 for i in render_log
               if i.get("role") == "user" and i.get("kind") == "text")


def _split_at_turn(chat: dict, turn: int) -> tuple[list, list, str]:
    """指定の発言の直前までを切り出す。

    画面(render_log)とLLMの会話(messages)は別物なので、
    「何回目のユーザー発言か」を共通の目盛りにして両方を同じ位置で切る。
    戻り値は (切り詰めた messages, 切り詰めた render_log, もとの発言内容)。
    """
    seen, cut_log, original = 0, None, ""
    for i, item in enumerate(chat.get("render_log") or []):
        if item.get("role") == "user" and item.get("kind") == "text":
            if seen == turn:
                cut_log, original = i, item.get("content", "")
                break
            seen += 1
    if cut_log is None:
        raise ValueError(f"{turn + 1}番目の発言が見つかりません。")

    seen, cut_msg = 0, None
    for i, m in enumerate(chat.get("messages") or []):
        if m.get("role") == "user":
            if seen == turn:
                cut_msg = i
                break
            seen += 1
    if cut_msg is None:
        raise ValueError(f"{turn + 1}番目の発言が会話履歴にありません。")
    return chat["messages"][:cut_msg], chat["render_log"][:cut_log], original


def _web_log(render_log: list[dict], start: int = 0) -> list[dict]:
    """保存形式 → ブラウザ表示用。ファイルはダウンロードURLに差し替える。

    ユーザーの発言には通し番号(turn)を振る。巻き戻しのとき、
    画面のどの吹き出しが messages の何番目に当たるかを、これで対応付ける。
    """
    out = []
    turn = _count_turns(render_log[:start])
    for item in render_log[start:]:
        w = render_item_for_web(item)
        if item.get("role") == "user" and item.get("kind") == "text":
            w["turn"] = turn
            turn += 1
        # 中身(bytes)を持つアイテムは、種類を問わずダウンロードURLに置き換える
        if item.get("data"):
            token = filestore.put(item["data"], item.get("filename", "download"),
                                  item.get("mime", "application/octet-stream"),
                                  g.user.username)
            w["url"] = f"/api/file/{token}"
        if item.get("kind") == "sql":
            w["label"] = TOOL_LABELS.get(item.get("tool"), item.get("tool"))
        out.append(w)
    return out


@bp_chat.get("/api/history", endpoint="history")
@login_required
def chat_history():
    return jsonify({"chats": [{**c, "label": chats.label(c)} for c in chats.list_chats(g.user)],
                    "current": session.get("chat_id")})


@bp_chat.post("/api/chat/open")
@login_required
def open_chat():
    cid = request.json.get("id")
    if not cid:
        session.pop("chat_id", None)
        return jsonify({"ok": True, "items": []})
    chat = chats.load_chat(g.user, cid)
    if chat is None:
        return jsonify({"error": "この会話は見つかりませんでした。"}), 404
    session["chat_id"] = cid
    return jsonify({"ok": True, "items": _web_log(chat.get("render_log") or []),
                    "title": chat.get("title", "")})


@bp_chat.post("/api/chat/delete")
@login_required
def delete_chat():
    cid = request.json.get("id")
    chats.delete_chat(g.user, cid)
    if session.get("chat_id") == cid:
        session.pop("chat_id", None)
    return jsonify({"ok": True})


@bp_chat.post("/api/chat/rename")
@login_required
def rename_chat():
    if not chats.rename_chat(g.user, request.json.get("id"), request.json.get("title") or ""):
        return jsonify({"error": "この会話は見つかりませんでした。"}), 404
    return jsonify({"ok": True})


# =============================================================================
# エージェントループ
# =============================================================================

def _msg_to_dict(m) -> dict:
    d = {"role": "assistant", "content": m.content}
    if m.tool_calls:
        d["tool_calls"] = [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name,
                                         "arguments": tc.function.arguments}}
                           for tc in m.tool_calls]
    return d


def _extract_calls(m) -> list[dict]:
    if not m.tool_calls:
        return []
    return [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
            for tc in m.tool_calls]


def _call_previews(calls: list[dict], scope: list[dict], question: str) -> list[dict]:
    """実行『前』に見せる内容（生成SQLなど）。"""
    out = []
    for c in calls:
        try:
            args = json.loads(c["arguments"]) if c["arguments"] else {}
        except json.JSONDecodeError:
            args = {}
        custom = next((t for t in custom_tools.collect_everywhere(scope)
                       if t.get("name") == c["name"]), None)
        # 触れているテーブルを添える。画面ではカタログの該当テーブルへのリンクになり、
        # 「列の意味が分からない」と言われた場所から、そのまま説明を書きに行ける。
        if c["name"] in tools.SQL_TOOLS and "sql" in args:
            out.append({"role": "assistant", "kind": "sql", "tool": c["name"],
                        "sql": args["sql"], "purpose": args.get("purpose", ""),
                        "question": question,
                        "tables": tables_in_sql(args["sql"], scope)})
        elif custom is not None:
            binds = ", ".join(f"{k}={v!r}" for k, v in args.items()) or "（引数なし）"
            sql = tools.render_sql(custom)
            out.append({"role": "assistant", "kind": "sql", "tool": c["name"],
                        "sql": sql,
                        "purpose": f"{custom.get('description', '')[:60]} / 引数: {binds}",
                        "question": question,
                        "tables": tables_in_sql(sql, scope)})
        elif c["name"] == "describe_table":
            alias, table = args.get("db"), args.get("table")
            owner = next((s for s in scope if s.get("alias") == alias), None)
            out.append({"role": "assistant", "kind": "text",
                        "content": f"🛠 テーブル詳細を確認: `{alias}.{table}`",
                        "tables": ([{"db": owner["name"], "table": table}]
                                   if owner and table else [])})
    return out


class _Guard:
    """同じ失敗を繰り返させないための見張り。

    LLMは、直せない指摘を受けると同じ引数のまま呼び直すことがある。
    そのまま通すと上限まで同じエラーが並び、ユーザーには何も残らない。
    2回目以降は実行せずに「同じ呼び出しです」と返し、
    それでも繰り返すならその質問を打ち切る。
    """

    LIMIT = 2                     # 同じ呼び出しが何回来たら打ち切るか

    def __init__(self):
        self.failed: dict[tuple, str] = {}    # 失敗した呼び出し -> 理由
        self.repeats = 0

    @staticmethod
    def key(call: dict) -> tuple:
        return (call["name"], (call.get("arguments") or "").strip())

    def known_failure(self, call: dict) -> str | None:
        return self.failed.get(self.key(call))

    def note(self, call: dict, res: dict) -> None:
        if not res.get("ok"):
            try:
                why = json.loads(res["llm_content"]).get("error", "")
            except (ValueError, TypeError):
                why = ""
            self.failed[self.key(call)] = why or "同じ内容で失敗しました。"

    def repeated(self, call: dict) -> str:
        """2回目以降の同じ呼び出しに返す、LLM向けの差し戻し文。"""
        self.repeats += 1
        why = self.failed.get(self.key(call), "")
        return json.dumps({
            "error": "同じツールを同じ引数で呼び直しています。実行しませんでした。",
            "previous_error": why,
            "hint": "引数を直してから呼ぶこと。直せないなら、そのツールは諦めて"
                    "別の方法（表だけで示す・SQLを見直す・ユーザーに確認する）に切り替える。"
                    "同じ呼び出しをもう一度行ってはいけない。",
        }, ensure_ascii=False)

    @property
    def stuck(self) -> bool:
        return self.repeats >= self.LIMIT


def _stop_note(reason: str) -> dict:
    return {"role": "assistant", "kind": "text", "content": reason}


def _is_admin() -> bool:
    """管理者専用ツールを渡してよい相手か。

    「データ取り込み」画面が管理者専用なので、AI経由でも同じ線を引く。
    そうしないと、画面では見られない中身がチャットからは見える、という
    抜け道ができる。
    """
    return bool(getattr(g.get("user"), "is_admin", False))


def _advance(chat: dict, scope: list[dict], question: str) -> None:
    """最終回答が出るまで回す。

    実行するSQLは _call_previews で毎回チャットに出るので、
    何が走ったかは後からでも追える。
    """
    guard = _Guard()
    for _ in range(config.MAX_AGENT_STEPS):
        try:
            msg = llm.chat(chat["messages"], tools.build_tools(scope, admin=_is_admin()),
                           model=models.current(g.user))
        except Exception as e:
            chat["render_log"].append({"role": "assistant", "kind": "error",
                                       "message": f"LLM呼び出しに失敗しました: {e}"})
            return

        chat["messages"].append(_msg_to_dict(msg))
        if msg.content:
            chat["render_log"].append({"role": "assistant", "kind": "text",
                                       "content": msg.content})

        calls = _extract_calls(msg)
        if not calls:
            return                             # 最終回答

        fresh = [c for c in calls if guard.known_failure(c) is None]
        chat["render_log"].extend(_call_previews(fresh, scope, question))
        _execute(chat, calls, scope, guard)
        if guard.stuck:
            chat["render_log"].append(_stop_note(_STUCK_MESSAGE))
            return

    chat["render_log"].append(_stop_note(
        f"（ツールの呼び出しが{config.MAX_AGENT_STEPS}回に達したので、"
        "ここで一区切りにしました。続きが必要なら、"
        "「続けて」と送るか、質問を分けてください。）"))


_STUCK_MESSAGE = ("（同じ操作の失敗が続いたため、ここで止めました。"
                  "上のエラーに出ている列名や条件を指定し直すか、"
                  "質問を「まず集計だけ」「次にグラフ」のように分けて試してください。）")


def _merge_alerts(content: str, alerts: list[dict]) -> str:
    """検算の不一致をツール結果に混ぜて、LLMに気づかせる。"""
    notes = [verify.llm_note(a) for a in alerts]
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            data["verification_warnings"] = notes
            return json.dumps(data, ensure_ascii=False, default=str)
    except (ValueError, TypeError):
        pass
    return content + "\n\n【検算の不一致】" + json.dumps(notes, ensure_ascii=False, default=str)


def _fresh_alerts(chat: dict, alerts: list[dict]) -> list[dict]:
    """この会話でまだ見せていない検算だけを残す。

    同じデータ・同じルールの警告を質問のたびに繰り返すと、読まれなくなる。
    データが変わる（=キーの版が変わる）と、また1回だけ出る。
    """
    seen = {i.get("verify_key") for i in chat["render_log"] if i.get("verify_key")}
    return [a for a in alerts if a["key"] not in seen]


def _execute(chat: dict, calls: list[dict], scope: list[dict],
             guard: "_Guard | None" = None) -> None:
    for c in calls:
        if guard is not None and guard.known_failure(c) is not None:
            # 同じ失敗の繰り返し。実行せずに差し戻す（時間もお金も使わない）
            chat["messages"].append({"role": "tool", "tool_call_id": c["id"],
                                     "content": guard.repeated(c)})
            continue
        res = tools.dispatch(c["name"], c["arguments"], scope, scope, admin=_is_admin())
        if guard is not None:
            guard.note(c, res)

        # 相互検証。数字が食い違っていたら、回答の前に画面とLLMの両方へ
        content = res["llm_content"]
        alerts = _fresh_alerts(chat, res.get("verify_alerts") or [])
        if alerts:
            content = _merge_alerts(content, alerts)
        chat["messages"].append({"role": "tool", "tool_call_id": c["id"],
                                 "content": content})
        if res.get("render"):
            chat["render_log"].append(dict(res["render"]))
        for a in alerts:
            chat["render_log"].append(verify.render_item(a))


def _reply(chat: dict, before: int, replace: bool = False):
    _persist(chat)
    return jsonify({
        "ok": True,
        # replace=True のときは画面をいったん空にして全部描き直してもらう
        "items": _web_log(chat["render_log"], 0 if replace else before),
        "replace": replace,
        "chat_id": chat.get("id"),
        "title": chat.get("title", ""),
    })


class _TurnError(Exception):
    """送信を始められないときの理由。画面にそのまま出せる文言を持つ。"""

    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.payload = {"error": message, **extra}
        self.status = status


@bp_chat.errorhandler(_TurnError)
def _turn_error(e: _TurnError):
    return jsonify(e.payload), e.status


def _auto_scope(question: str, chat: dict) -> list[dict]:
    """質問に合わせて対象DBを決める。利用者はDBを選ばない。

    決め方は config.SCOPE_MODE:
      auto   … カタログ全体が「選択中モデルの読める量」に収まるなら全DB直載せ
               （ルーター省略＝選び漏れゼロ・プロンプトが毎回同一でキャッシュ最大）。
               収まらないときだけルーターで絞り、詳細を保つ（既定）。
      router … 常にルーターで絞る（無関係なDBを本番プロンプトに入れない）。
      all    … 常に全DB（収まらなければ要約モードに落ちる。旧来の挙動）。

    ルーターで絞るときは、さらに2つを合わせる:
      この会話で使ったDB … 「それをグラフに」のような続きの質問はルーターに手がかりが
                           無いので、実際にSQLが触ったDBは残し続ける。
      判定できないとき   … 全DB。ルーターの不調で答えられなくなるのがいちばん悪い。
    """
    all_names = [f.name for f in db.list_db_files()]
    full = build_scope({n: [] for n in all_names})
    if config.SCOPE_MODE == "all":
        return full
    if config.SCOPE_MODE == "auto":
        limit = models.inline_limit_for(models.current(g.user))
        if catalog.inline_length(full) <= limit:
            return full                    # 全部入りで選び漏れゼロ
    chat_history = [i.get("content") or "" for i in (chat.get("render_log") or [])
               if i.get("role") == "user" and i.get("kind") == "text"]
    routed = llm.route_dbs(question, chat_history)
    names = set(routed if routed else all_names)
    names |= set(chat.get("db_names") or [])          # この会話で実際に使ったDB
    return build_scope({n: [] for n in all_names if n in names})


def _begin_turn():
    """/send と /stream に共通する前処理。

    質問を検証し、スコープを確定し、会話にユーザーの発言を積むところまで。
    始められないときは _TurnError を投げる（呼び出し側で分岐を書かずに済む）。
    """
    text = (request.json.get("text") or "").strip()
    if not text:
        raise _TurnError("質問を入力してください。")
    if not llm.is_configured():
        raise _TurnError("LLMが未設定です。env の OPENAI_* を設定してください。")

    chat = _load_current()
    scope = _auto_scope(text, chat)
    if not scope:
        raise _TurnError("data/ に分析できるDBがありません。"
                         "「データ取り込み」からDBを作成してください。")

    images, show = _images_from((request.json or {}).get("images"))
    if images and not models.is_vision(models.current(g.user)):
        raise _TurnError("いま選ばれているモデルは画像を扱えません。")
    if not chat["messages"] or chat["messages"][0].get("role") != "system":
        chat["messages"].insert(0, {"role": "system", "content": ""})
    chat["messages"][0] = {"role": "system",
                           "content": llm.build_system_prompt(
                               scope, admin=_is_admin(),
                               model=models.current(g.user))}
    chat["messages"].append(llm.user_message(text, images))
    # 質問の時刻はここで入れる。保存は応答が終わってからなので、
    # 保存時に付けると「聞いた時刻」ではなく「答え終わった時刻」になってしまう。
    chat["render_log"].append({"role": "user", "kind": "text", "content": text,
                               "at": chats.now(),
                               **({"images": show} if show else {})})
    return chat, scope, text


@bp_chat.post("/api/chat/send")
@login_required
def send():
    chat, scope, text = _begin_turn()
    before = len(chat["render_log"]) - 1
    _advance(chat, scope, text)
    return _reply(chat, before)


# =============================================================================
# ストリーミング送信
#
# 通常の /api/chat/send は、ツールを何回か呼んで最終回答が出るまで待ってから
# まとめて返す。待ち時間が長く、届いた瞬間に画面がいちばん下へ飛ぶ。
# こちらは、起きたことをその都度 Server-Sent Events で流す。
# =============================================================================

def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _stream_advance(chat: dict, scope: list[dict], question: str):
    """_advance のストリーミング版。起きたことを逐次 yield する。"""
    guard = _Guard()
    for _ in range(config.MAX_AGENT_STEPS):
        msg = None
        try:
            for kind, payload in llm.chat_stream(
                    chat["messages"], tools.build_tools(scope, admin=_is_admin()),
                    model=models.current(g.user)):
                if kind == "text":
                    yield _sse("delta", {"text": payload})
                else:
                    msg = payload
        except Exception as e:
            item = {"role": "assistant", "kind": "error",
                    "message": f"LLM呼び出しに失敗しました: {e}"}
            chat["render_log"].append(item)
            yield _sse("item", _web_log([item])[0])
            return
        if msg is None:
            return

        chat["messages"].append(_msg_to_dict(msg))
        if msg.content:
            chat["render_log"].append({"role": "assistant", "kind": "text",
                                       "content": msg.content})
        calls = _extract_calls(msg)
        if not calls:
            yield _sse("text_end", {})
            return                                  # 最終回答

        yield _sse("text_end", {})
        fresh = [c for c in calls if guard.known_failure(c) is None]
        previews = _call_previews(fresh, scope, question)
        chat["render_log"].extend(previews)
        for p in _web_log(previews):
            yield _sse("item", p)

        for c in calls:
            if guard.known_failure(c) is not None:
                _execute(chat, [c], scope, guard)      # 実行せず差し戻すだけ
                continue
            yield _sse("running", {"name": c["name"],
                                   "label": TOOL_LABELS.get(c["name"], c["name"])})
            before = len(chat["render_log"])
            _execute(chat, [c], scope, guard)
            for item in _web_log(chat["render_log"], before):
                yield _sse("item", item)

        if guard.stuck:
            item = _stop_note(_STUCK_MESSAGE)
            chat["render_log"].append(item)
            yield _sse("item", _web_log([item])[0])
            return

    item = {"role": "assistant", "kind": "text",
            "content": f"（ツールの呼び出しが{config.MAX_AGENT_STEPS}回に達したので、"
                       "ここで一区切りにしました。続きが必要なら、"
                       "「続けて」と送るか、質問を分けてください。）"}
    chat["render_log"].append(item)
    yield _sse("item", _web_log([item])[0])


@bp_chat.post("/api/chat/stream")
@login_required
def stream():
    """1問1答をSSEで流す。イベントの種類:

        delta     … 回答の文字（少しずつ）
        text_end  … ひとまとまりの回答が終わった
        item      … 表・グラフ・ファイルなどの描画アイテム
        running   … ツールを実行し始めた
        end       … 終わり（保存後の会話ID・タイトルを載せる）
    """
    chat, scope, text = _begin_turn()
    # 会話IDはここで確定させてセッションに入れる。
    # 応答を流し始めるとセッションに書けなくなるので、あとから入れても消える
    # （次の質問が別の会話として始まってしまう）。
    if not chat.get("id"):
        chat["id"] = chats.new_id()
    session["chat_id"] = chat["id"]

    def generate():
        try:
            yield from _stream_advance(chat, scope, text)
        except Exception as e:                       # 途中で落ちても接続は閉じる
            yield _sse("item", {"role": "assistant", "kind": "error",
                                "message": f"処理中にエラーが発生しました: {e}"})
        finally:
            _persist(chat)
            yield _sse("end", {"chat_id": chat.get("id"), "title": chat.get("title", "")})

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})   # nginx等でのバッファ抑止


@bp_chat.post("/api/chat/rewind")
@login_required
def rewind():
    """指定の発言まで巻き戻して、そこからやり直す。

    text を送ると、その発言を書き換えたうえで会話を続ける。
    text が空なら巻き戻すだけ（それ以降を消して、入力欄に戻す）。
    どちらも、その発言より後のやり取りは消える。
    """
    body = request.json or {}
    try:
        turn = int(body.get("turn"))
    except (TypeError, ValueError):
        return jsonify({"error": "巻き戻す位置が指定されていません。"}), 400
    text = (body.get("text") or "").strip()

    chat = _load_current()
    try:
        messages, render_log, original = _split_at_turn(chat, turn)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    dropped = len(chat["render_log"]) - len(render_log)
    chat["messages"], chat["render_log"] = messages, render_log

    if not text:
        _persist(chat)
        return jsonify({"ok": True, "replace": True,
                        "items": _web_log(chat["render_log"]),
                        "restored": original, "dropped": dropped,
                        "chat_id": chat.get("id"), "title": chat.get("title", "")})

    if not llm.is_configured():
        return jsonify({"error": "LLMが未設定です。env の OPENAI_* を設定してください。"}), 400
    scope = _auto_scope(text, chat)
    if not scope:
        return jsonify({"error": "data/ に分析できるDBがありません。"}), 400

    # やり直しなので、カタログの現状に合わせてシステムプロンプトも入れ直す
    if not chat["messages"] or chat["messages"][0].get("role") != "system":
        chat["messages"].insert(0, {"role": "system", "content": ""})
    chat["messages"][0] = {"role": "system",
                           "content": llm.build_system_prompt(
                               scope, admin=_is_admin(),
                               model=models.current(g.user))}
    chat["messages"].append({"role": "user", "content": text})
    chat["render_log"].append({"role": "user", "kind": "text", "content": text,
                               "at": chats.now()})

    _advance(chat, scope, text)
    return _reply(chat, 0, replace=True)


# =============================================================================
# メール送信
#
# 送信はここだけ。LLMは compose_email で下書きを作るところまでしかできず、
# 実際に外へ出るのはユーザーが画面の「送信」を押したときだけにしてある。
# 宛先の間違いは取り消せないため、AIの判断だけで外部に何かを出さない。
# =============================================================================

def _attachments_for(chat: dict, names: list) -> tuple[list, list]:
    """この会話で作ったファイルから、名前が一致する添付を集める。

    'all' が指定されたら直近に作ったものを全部付ける。
    戻り値は (添付, 見つからなかった名前)。
    """
    made = [i for i in (chat.get("render_log") or [])
            if i.get("kind") == "file" and i.get("data")]
    wanted = [str(n) for n in (names or [])]
    if not wanted:
        return [], []
    if any(w.lower() == "all" for w in wanted):
        picked = made[-5:]
        return [{"filename": i.get("filename"), "mime": i.get("mime"),
                 "data": i["data"]} for i in picked], []

    out, missing = [], []
    for w in wanted:
        hit = next((i for i in reversed(made)
                    if (i.get("filename") or "").lower() == w.lower()), None)
        if hit is None:                      # 部分一致でも拾う（拡張子の付け忘れなど）
            hit = next((i for i in reversed(made)
                        if w.lower() in (i.get("filename") or "").lower()), None)
        if hit is None:
            missing.append(w)
        else:
            out.append({"filename": hit.get("filename"), "mime": hit.get("mime"),
                        "data": hit["data"]})
    return out, missing


@bp_chat.post("/api/mail/send")
@login_required
def mail_send():
    """実際に送る。押したのがユーザー本人であることが唯一の前提。"""
    body = request.json or {}
    draft = body.get("draft") or {}
    if not body.get("confirm"):
        return jsonify({"error": "確認されていません。"}), 400
    chat = _load_current()
    files, missing = _attachments_for(chat, draft.get("attach_filenames"))
    if missing:
        return jsonify({"error": f"添付ファイルが見つかりません: {', '.join(missing)}"}), 400
    try:
        record = mailer.send(draft, files, user=g.user.username)
    except mailer.MailError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"送信に失敗しました: {e}"}), 500

    chat["render_log"].append({
        "role": "assistant", "kind": "text",
        "content": ("📤 " + record["message"]
                    + f"（件名: {record['subject']} / 宛先: {', '.join(record['to'])}"
                    + (f" / 添付: {', '.join(record['attachments'])}"
                       if record["attachments"] else "") + "）")})
    _persist(chat)
    return jsonify({"ok": True, "record": record})


@bp_chat.post("/api/mail/test")
@admin_required
def mail_test():
    """SMTPの疎通確認だけ（メールは送らない）。"""
    return jsonify(mailer.test_connection())


@bp_chat.post("/api/chat/glossary-save")
@login_required
def glossary_save():
    """チャットの登録カードから、用語をカタログの用語集へ保存する。

    AIは propose_glossary_term でカードを出すところまで。書き込みはこの
    エンドポイントだけで、カードのボタンを押したときに起こる。
    一般ユーザーも登録できる（カタログを皆で育てる）。その代わり、
    誰がいつ何を変えたかを catalog_history に必ず残す。
    """
    body = request.json or {}
    term = (body.get("term") or "").strip()
    desc = (body.get("description") or "").strip()
    sql = (body.get("sql") or "").strip()
    table = (body.get("table") or "").strip()
    if not term or not desc:
        return jsonify({"error": "用語と説明が必要です。"}), 400
    try:
        path = db.path_for(body.get("db") or "")
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 400

    meta = catalog.load_meta(path)
    entry = {"description": desc, "sql": sql}
    if table:
        gl = catalog.table_glossary(meta, table)      # 既存の用語を消さずに足す
        old = gl.get(term)
        gl[term] = entry
        catalog.set_table_glossary(meta, table, gl)
    else:
        gl = catalog.db_glossary(meta)
        old = gl.get(term)
        gl[term] = entry
        meta["glossary"] = gl
    catalog.save_meta(path, meta)
    catalog_history.add_catalog_change("glossary", "update" if old else "add", path.name, term,
                        user=g.user.username, table=table or None,
                        before=old, after=entry, source="chat")
    where = f"{path.name} の {table}" if table else f"{path.name}（DB全体）"
    return jsonify({"ok": True,
                    "message": f"「{term}」を {where} の用語集に"
                               f"{'上書き登録' if old else '登録'}しました。"
                               "次の質問からAIがこの定義に従います。"})


@bp_chat.post("/api/chat/save-example")
@login_required
def save_example():
    """チャットの登録カードから、例文をカタログへ保存する。

    一般ユーザーも登録できる（カタログを皆で育てる）。誰がいつ何を変えたかは
    catalog_history に必ず残す。同じSQLの例文が既にあれば、質問文と説明を更新する。
    """
    scope = build_scope({f.name: [] for f in db.list_db_files()})
    q = (request.json.get("question") or "").strip()
    sql = (request.json.get("sql") or "").strip()
    desc = (request.json.get("description") or "").strip()
    if not q or not sql:
        return jsonify({"error": "質問とSQLの両方が必要です。"}), 400

    # 例文はDBごとのファイルに残すので、保存先を1つに決める必要がある。
    # 複数のDBを選んでいても、SQLがどのDBを見ているかで決められる。
    # DBをまたぐ例文（人事の勤怠 × マスタの社員、など）は珍しくないため、
    # 「1つだけ選んでいるとき」に限ると保存できる場面が狭くなりすぎる。
    hits = dbs_in_sql(sql, scope)
    # 登録カード（propose_example）は置き場のDBを持っている。あればそれを使う。
    # SQLに DB名 が無い（単一DBの略記など）ときも、カードの db で決められる。
    asked = (request.json.get("db") or "").strip()
    target = next((s for s in scope if s["name"] == asked or s.get("alias") == asked), None) if asked else None
    if target is None:
        target = hits[0] if hits else (scope[0] if len(scope) == 1 else None)
    if target is None:
        return jsonify({"error": "このSQLがどのDBのものか判断できませんでした。"
                                 "テーブル名を『DB名.テーブル名』の形で書いたSQLにするか、"
                                 "db を指定してください。"}), 400

    p = Path(target["path"])
    meta = catalog.load_meta(p)
    examples = meta.get("examples") or []

    # 同じSQLが既にあれば、増やさずにその1件の質問文・説明を更新する。
    # 例文は毎回プロンプトに載るので、言い回し違いで同じSQLが並ぶと
    # トークンを食うだけで精度は上がらない。
    same = catalog.find_example(examples, sql)
    if same is not None:
        before = dict(same)
        same["q"] = q
        if desc:
            same["description"] = desc
        meta["examples"] = catalog.dedupe_examples(examples)
        catalog.save_meta(p, meta)
        catalog_history.add_catalog_change("example", "update", p.name, q,
                            user=g.user.username, before=before,
                            after={k: same.get(k) for k in ("q", "description", "sql")},
                            source="chat")
        return jsonify({"ok": True, "added": False, "updated": True,
                        "message": f"{p.name} に同じSQLの例文があったため、"
                                   f"質問文を「{q}」に更新しました。"})
    if len(examples) >= catalog.EXAMPLES_MAX:
        return jsonify({"error": f"{p.name} の例文は{catalog.EXAMPLES_MAX}件までです。"
                                 "データカタログの「質問とSQLの例文」で古いものを"
                                 "整理してください。"}), 400

    new_entry = {"q": q, "sql": sql}
    if desc:
        new_entry["description"] = desc
    meta["examples"] = catalog.dedupe_examples([*examples, new_entry])
    catalog.save_meta(p, meta)
    catalog_history.add_catalog_change("example", "add", p.name, q, user=g.user.username,
                        after=new_entry, source="chat")
    others = [s["name"] for s in hits[1:]]
    message = f"{p.name} の例文に追加しました。"
    if others:
        # DBをまたぐ例文は、質問時に相手DBも対象になったときだけ効く（自動判定）
        message += f"（{'、'.join(others)} も参照する例文です）"
    return jsonify({"ok": True, "added": True, "message": message})


# ==========================================================================
# ===== 元 web/catalog_bp.py
# データカタログ画面。テーブル/列の説明・用語集・結合(ER)・ツールを編集する。
# ==========================================================================
import inspect
import re
from pathlib import Path

from flask import Blueprint, g, jsonify, render_template, request

import advanced
import catalog
import catalog_history
import charts
import custom_tools
import db
import jobs
import llm
import sqlusage
import tools
import verify


bp_catalog = Blueprint("catalog", __name__)


@bp_catalog.errorhandler(FileNotFoundError)
def _db_missing(e: FileNotFoundError):
    """db.path_for が見つけられなかったとき。存在しないDB名を投げられても500にしない。"""
    return jsonify({"error": str(e)}), 400


def _pick(name: str | None) -> Path | None:
    files = db.list_db_files()
    if not files:
        return None
    return next((f for f in files if f.name == name), files[0])


def _builtin_view(tool: dict) -> dict:
    """組み込みツールを画面で見られる形にする。

    AIに渡しているのは JSON Schema そのものなので、説明もパラメータも
    切らずに全部見せる。「AIがこのツールをどう理解しているか」が
    そのまま分かるようにするのが目的（説明の上書きを決める材料になる）。
    """
    fn = tool["function"]
    params = fn.get("parameters") or {}
    required = set(params.get("required") or [])
    return {
        "name": fn["name"],
        "description": fn.get("description") or "",
        # SQLを受け取るツールか（実行したSQLがチャットに表示される対象）
        "is_sql": fn["name"] in tools.SQL_TOOLS,
        "params": [{"name": k,
                    "type": (v or {}).get("type") or "",
                    "required": k in required,
                    "description": (v or {}).get("description") or "",
                    # 選択肢が決まっている引数は、そのまま候補を見せる
                    "enum": (v or {}).get("enum") or []}
                   for k, v in (params.get("properties") or {}).items()],
    }


def _tool_source(name: str) -> list[dict]:
    """組み込みツールが実際に何をしているかを、コードそのもので見せる。

    チャットで生成SQLを見せているのと同じ考え方で、「AIがこのツールを呼ぶと
    データに何が起きるか」を確かめられるようにする。
    統計系のツールは共通の入れ物でくるまれているので、中の呼び出しと
    その先の実装（advanced.py）まで辿って出す。
    """
    fn = tools._HANDLERS.get(name)
    if fn is None:
        return []

    out: list[dict] = []

    def add(target, label):
        try:
            src = inspect.getsource(target)
            where = f"{Path(inspect.getsourcefile(target)).name}:{inspect.getsourcelines(target)[1]}"
        except (OSError, TypeError):
            return
        out.append({"label": label, "where": where, "code": src})

    # _analysis_tool でくるまれたものは、中の呼び出し（どの分析を呼ぶか）を出す
    inner = None
    if fn.__name__ == "run" and fn.__closure__:
        inner = fn.__closure__[0].cell_contents
    add(inner or fn, "ツールの処理")

    # advanced.py に委譲しているなら、その本体も見せる（実際の計算はここ）
    if out:
        m = re.search(r"\badvanced\.(\w+)", out[0]["code"])
        if m:
            target = getattr(advanced, m.group(1), None)
            if callable(target):
                add(target, f"実際の計算 advanced.{m.group(1)}()")
    return out


@bp_catalog.get("/api/catalog/builtin/source")
@admin_required
def builtin_source():
    """組み込みツールのコード。開いたときだけ取りに行く（全部で65KBあるため）。"""
    name = request.args.get("name") or ""
    if name not in tools._HANDLERS:
        return jsonify({"error": f"未知のツールです: {name}"}), 404
    return jsonify({"name": name, "parts": _tool_source(name)})


def _overview(path: Path) -> dict:
    profile = catalog.profile_db(path)
    meta = catalog.load_meta(path)
    cov = catalog.coverage(profile, meta)
    return {"profile": profile, "meta": meta, "coverage": cov,
            "drift": catalog.drift_warnings(profile, meta)}


@bp_catalog.get("/catalog", endpoint="index")
@admin_required
def catalog_index():
    target = _pick(request.args.get("db"))
    if target is None:
        return render_template("catalog.html", db_files=[], target=None)
    ov = _overview(target)
    profile, meta = ov["profile"], ov["meta"]

    tables = []
    for tname, t in profile["tables"].items():
        tmeta = (meta.get("tables") or {}).get(tname) or {}
        mcols = tmeta.get("columns") or {}
        pk_cols, pk_src = catalog.effective_pk(profile, meta, tname)
        cols = []
        for c in t["columns"]:
            cm = mcols.get(c["name"]) or {}
            stat = (t.get("col_stats") or {}).get(c["name"]) or {}
            if "values" in stat:
                actual = ", ".join(str(v) for v in stat["values"][:12])
            elif "min" in stat:
                actual = f"{stat['min']} 〜 {stat['max']}"
            else:
                actual = ""
            cols.append({"name": c["name"], "type": c["type"], "pk": c["name"] in set(pk_cols),
                         "description": cm.get("description", ""),
                         "codes": cm.get("values") or {}, "actual": actual})
        tables.append({
            "name": tname, "rows": t.get("row_count"),
            "description": tmeta.get("description", ""),
            "ai_draft": bool(tmeta.get("ai_draft")),
            "pk": pk_cols, "pk_src": pk_src,
            "columns": cols,
            "glossary": catalog.table_glossary(meta, tname),
            "sample_columns": t.get("sample_columns") or [],
            "sample_rows": (t.get("sample_rows") or [])[:5],
        })

    return render_template(
        "catalog.html",
        db_files=[f.name for f in db.list_db_files()],
        target=target.name, title=meta.get("title", ""),
        description=catalog.db_description(meta),
        coverage=ov["coverage"], drift=ov["drift"], tables=tables,
        db_glossary=catalog.db_glossary(meta),
        relationships=meta.get("relationships") or [],
        examples=meta.get("examples") or [],
        checks=verify.normalize(meta.get("checks")),
        suggestions=(catalog.join_suggestions(profile, meta)
                     + sqlusage.suggestions_for(db.alias_for(target), profile, meta)),
        er=_er_payload(target, profile, meta),
        # ツールはDBに紐づけずに作るので、一覧も全DB分を出す（組み込みと同じ扱い）
        custom=custom_tools.collect_everywhere(),
        builtin=[_builtin_view(t) for t in tools.BUILTIN_TOOLS],
        chart_fields={t: list(charts.required_fields(t)) for t in charts.CHART_TYPES},
        builtin_overrides=meta.get("builtin_tools") or {},
        cat_history=[{**r, "summary": catalog_history.summarize_catalog_changes(r)}
                     for r in catalog_history.recent_catalog_changes(50)],
        intervals=list(jobs.INTERVALS.keys()),
        llm_ready=llm.is_configured(),
    )


# =============================================================================
# ER図（キャンバス用のデータ）
# =============================================================================

def _er_payload(path: Path, profile: dict, meta: dict) -> dict:
    """ERキャンバス用ペイロード。実体は catalog.er_payload（チャットのツールと共用）。"""
    return catalog.er_payload(path, profile, meta)


def _alias_lookup(own_alias: str, own_profile: dict, own_meta: dict):
    """エイリアス → (profile, meta)。DBまたぎの関連を扱うのに要る。"""
    cache = {own_alias: (own_profile, own_meta)}

    def get(alias: str):
        if alias not in cache:
            p = next((f for f in db.list_db_files() if db.alias_for(f) == alias), None)
            cache[alias] = ((catalog.profile_db(p), catalog.load_meta(p)) if p
                            else (None, None))
        return cache[alias]
    return get


def _endpoint_error(ep: tuple, lookup) -> str | None:
    """関連の端点が実在するかを確かめる。DB名を含む3要素も受ける。"""
    alias, table, column = ep
    profile, _ = lookup(alias)
    if profile is None:
        return f"DB '{alias}' が見つかりません。"
    t = (profile.get("tables") or {}).get(table)
    if t is None:
        return f"{alias} にテーブル '{table}' がありません。"
    if column not in {c["name"] for c in t.get("columns", [])}:
        return f"{alias}.{table} に列 '{column}' がありません。"
    return None


def _ref(ep: tuple, own_alias: str) -> str:
    """保存する文字列。自DBなら 'table.col'、他DBなら 'alias.table.col'。"""
    return f"{ep[1]}.{ep[2]}" if ep[0] == own_alias else f"{ep[0]}.{ep[1]}.{ep[2]}"


@bp_catalog.post("/api/catalog/relationship")
@admin_required
def relationship():
    """関連の追加・多重度変更・削除。ER図キャンバスから呼ばれる。"""
    body = request.json or {}
    path = db.path_for(body["db"])
    meta = catalog.load_meta(path)
    rels = meta.setdefault("relationships", [])
    action = body.get("action")
    alias = db.alias_for(path)

    # 関連の指定は2通り。ドラッグ直後は from_table/from_column、
    # 「元に戻す／やり直す」は保存済みの文字列 from/to（'table.col' や 'db.table.col'）で来る
    def _ep(side):
        if body.get(side):
            return catalog.parse_endpoint(str(body[side]), alias)
        return catalog.parse_endpoint(f"{body.get(side + '_table')}.{body.get(side + '_column')}", alias)

    if action == "add":
        lookup = _alias_lookup(alias, catalog.profile_db(path), meta)
        # テーブル名は 'table' でも 'otherdb.table' でもよい（DBまたぎ）
        a, b = _ep("from"), _ep("to")
        if not a or not b:
            return jsonify({"error": "関連の指定が正しくありません。"}), 400
        for ep in (a, b):
            err = _endpoint_error(ep, lookup)
            if err:
                return jsonify({"error": err}), 400
        if a == b:
            return jsonify({"error": "同じ列同士は関連にできません。"}), 400
        if a[0] != alias and b[0] != alias:
            return jsonify({"error": "どちらか一方は、いま開いているDBのテーブルにしてください。"}), 400

        # 向きを「子（外部キー側）→ 親（主キー側）」に揃えてから保存する。
        # ER図は矢印を描かないので、人はどちら向きにもドラッグする。
        # from/to は描画順ではなく参照の向きで、整合性チェックがこれに依存する。
        a, b, card = catalog.normalize_direction(a, b, body.get("cardinality"), lookup)
        if a[0] != alias:
            # 入れ替えた結果、子が他DBになった。その関連は相手のDBが持つべき
            return jsonify({"error":
                            f"この向きの関連は {a[0]} 側で登録してください"
                            f"（外部キーを持つのは {a[0]}.{a[1]} です）。"
                            "DBを切り替えてから、同じようにつないでください。"}), 400

        new = {"from": _ref(a, alias), "to": _ref(b, alias), "cardinality": card}
        if any(r.get("from") == new["from"] and r.get("to") == new["to"] for r in rels):
            return jsonify({"error": "この関連はすでに登録されています。"}), 400

        # 結んでよい列か、実データを見て確かめる。
        #   block … 保存しない（値が全く重ならない等。JOINが成立しない線をAIに教えない）
        #   warn  … 理由を返して止める。人が確認して force=true で送り直せば保存する
        def _path_of(al):
            return next((f for f in db.list_db_files() if db.alias_for(f) == al), path)
        check = catalog.link_check(a, b, lookup, _path_of)
        if check["level"] == "block" or (check["level"] == "warn" and not body.get("force")):
            # 200 で返す: 画面の api() は非2xxだと本文を捨てて例外にするため
            return jsonify({"ok": False, "check": check,
                            "from": new["from"], "to": new["to"], "cardinality": card})
        rels.append(new)
        extra = {"added": new}
    elif action in ("update", "delete"):
        # 位置（index）でも、保存済みの from/to 文字列でも指せる。
        # 「元に戻す」は index がずれるので from/to で来る
        if body.get("from") and body.get("to"):
            i = next((k for k, r in enumerate(rels)
                      if r.get("from") == body["from"] and r.get("to") == body["to"]), -1)
        else:
            i = int(body.get("index", -1))
        if not (0 <= i < len(rels)):
            return jsonify({"error": "この関連は既に削除されています。"}), 400
        if action == "delete":
            extra = {"removed": rels.pop(i)}
        else:
            prev = rels[i].get("cardinality")
            rels[i]["cardinality"] = body.get("cardinality") or prev
            extra = {"updated": {**rels[i], "previous": prev}}
    else:
        return jsonify({"error": "不正な操作です。"}), 400

    catalog.save_meta(path, meta)
    profile = catalog.profile_db(path)
    return jsonify({"ok": True, "er": _er_payload(path, profile, catalog.load_meta(path)), **extra})


@bp_catalog.get("/api/catalog/table-info")
@login_required
def table_info():
    """ER図でテーブルをクリックしたときの中身（概要・列・実値・サンプル行）。

    描画用のペイロードには入れていない（全テーブル分を持つと重い）ので、
    開いたときに取りに来る。チャットの読み取り専用ER図からも使うので、
    管理者に限らずログイン済みなら見られる（describe_table でAIに渡している
    情報と同じ範囲）。
    """
    alias = request.args.get("db") or ""
    tname = request.args.get("table") or ""
    path = next((f for f in db.list_db_files() if db.alias_for(f) == alias), None)
    if path is None:
        return jsonify({"error": f"DB '{alias}' が見つかりません。"}), 404
    profile, meta = catalog.profile_db(path), catalog.load_meta(path)
    t = (profile.get("tables") or {}).get(tname)
    if t is None:
        return jsonify({"error": f"テーブル '{tname}' が見つかりません。"}), 404
    tmeta = (meta.get("tables") or {}).get(tname) or {}
    mcols = tmeta.get("columns") or {}
    pk = set(catalog.effective_pk(profile, meta, tname)[0])
    cols = []
    for c in t.get("columns") or []:
        cm = mcols.get(c["name"]) or {}
        stat = (t.get("col_stats") or {}).get(c["name"]) or {}
        if "values" in stat:
            actual = ", ".join(str(v[0]) if isinstance(v, (list, tuple)) else str(v)
                               for v in stat["values"][:8])
        elif "min" in stat:
            actual = f"{stat['min']} 〜 {stat['max']}"
        else:
            actual = ""
        cols.append({"name": c["name"], "type": c.get("type") or "",
                     "pk": c["name"] in pk,
                     "description": cm.get("description") or "",
                     "codes": cm.get("values") or {}, "actual": actual})
    return jsonify({
        "db": path.name, "alias": alias, "table": tname,
        "rows": t.get("row_count"),
        "description": tmeta.get("description") or "",
        "ai_draft": bool(tmeta.get("ai_draft")),
        "columns": cols,
        "glossary": catalog.table_glossary(meta, tname),
        "sample_columns": t.get("sample_columns") or [],
        "sample_rows": jsonable((t.get("sample_rows") or [])[:5]),
    })


@bp_catalog.get("/api/catalog/er-tables")
@admin_required
def er_tables():
    """「他DBのテーブルを追加」の一覧。いま開いているDB以外のテーブル。"""
    alias = db.alias_for(db.path_for(request.args.get("db") or ""))
    out = []
    for f in db.list_db_files():
        a = db.alias_for(f)
        if a == alias:
            continue
        prof = catalog.profile_db(f)
        meta = catalog.load_meta(f)
        out.append({
            "alias": a, "name": f.name, "title": meta.get("title") or "",
            "tables": [{"id": f"{a}.{t}", "table": t,
                        "rows": info.get("row_count")}
                       for t, info in (prof.get("tables") or {}).items()],
        })
    return jsonify({"dbs": out})


@bp_catalog.post("/api/catalog/er-external")
@admin_required
def er_external():
    """キャンバスに引き込む他DBのテーブルを足す・外す。

    関連が1本も無いDBともつなげるようにするための入口。
    関連から自動で引き込まれているものは、ここから外しても残る
    （線の行き先が消えてしまうため）。
    """
    body = request.json or {}
    path = db.path_for(body["db"])
    meta = catalog.load_meta(path)
    alias = db.alias_for(path)
    target = str(body.get("table") or "").strip()
    parts = target.split(".")
    if len(parts) != 2 or parts[0] == alias:
        return jsonify({"error": "他のDBのテーブルを「DB名.テーブル名」で指定してください。"}), 400

    current = [str(x) for x in (meta.get("er_external") or [])]
    if body.get("action") == "remove":
        current = [x for x in current if x != target]
    else:
        p = next((f for f in db.list_db_files() if db.alias_for(f) == parts[0]), None)
        if p is None or parts[1] not in (catalog.profile_db(p).get("tables") or {}):
            return jsonify({"error": f"{target} が見つかりません。"}), 400
        if target not in current:
            current.append(target)
    if current:
        meta["er_external"] = current
    else:
        meta.pop("er_external", None)
    catalog.save_meta(path, meta)
    profile = catalog.profile_db(path)
    return jsonify({"ok": True, "er": _er_payload(path, profile, catalog.load_meta(path))})


@bp_catalog.post("/api/catalog/layout")
@admin_required
def save_layout():
    body = request.json or {}
    path = db.path_for(body["db"])
    meta = catalog.load_meta(path)
    incoming = body.get("layout") or {}
    if not isinstance(incoming, dict):
        return jsonify({"error": "配置の形式が正しくありません。"}), 400
    clean = {}
    for k, v in incoming.items():
        # 1ノード = [x, y] の数値2つ。それ以外は受け付けない（保存すると以後ER図が読めなくなる）
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            return jsonify({"error": f"配置の形式が正しくありません（{k}）。"}), 400
        try:
            clean[str(k)] = [int(round(float(v[0]))), int(round(float(v[1])))]
        except (TypeError, ValueError):
            return jsonify({"error": f"配置の座標が数値ではありません（{k}）。"}), 400
    meta["er_layout"] = {**(meta.get("er_layout") or {}), **clean}
    catalog.save_meta(path, meta)
    return jsonify({"ok": True})


@bp_catalog.post("/api/catalog/primary-key")
@admin_required
def primary_key():
    body = request.json or {}
    path = db.path_for(body["db"])
    profile, meta = catalog.profile_db(path), catalog.load_meta(path)
    tm = meta.setdefault("tables", {}).setdefault(body["table"], {})
    declared = catalog.declared_pk(profile, body["table"])
    cols = body.get("columns") or []
    if cols and cols != declared:
        tm["primary_key"] = cols
    else:
        tm.pop("primary_key", None)
    catalog.save_meta(path, meta)
    return jsonify({"ok": True, "er": _er_payload(path, profile, catalog.load_meta(path))})


# =============================================================================
# 保存系
# =============================================================================

@bp_catalog.post("/api/catalog/table")
@admin_required
def save_table():
    body = request.json or {}
    path = db.path_for(body["db"])
    meta = catalog.load_meta(path)
    tables = meta.setdefault("tables", {})
    tm = tables.setdefault(body["table"], {})
    tm["description"] = (body.get("description") or "").strip()
    cols = {}
    for name, c in (body.get("columns") or {}).items():
        entry = {}
        if (c.get("description") or "").strip():
            entry["description"] = c["description"].strip()
        if c.get("values"):
            entry["values"] = c["values"]
        if entry:
            cols[name] = entry
    if cols:
        tm["columns"] = cols
    else:
        tm.pop("columns", None)
    tm.pop("ai_draft", None)
    if not any(tm.get(k) for k in ("description", "columns", "primary_key", "glossary")):
        tables.pop(body["table"], None)
    catalog.save_meta(path, meta)
    return jsonify({"ok": True})


@bp_catalog.post("/api/catalog/glossary")
@admin_required
def save_glossary():
    body = request.json or {}
    path = db.path_for(body["db"])
    meta = catalog.load_meta(path)
    gl = {}
    for row in body.get("terms") or []:
        term = (row.get("term") or "").strip()
        desc = (row.get("description") or "").strip()
        sql = (row.get("sql") or "").strip()
        if term and (desc or sql):
            gl[term] = {"description": desc, "sql": sql}
    # 誰が何を変えたかを残す（チャットからの登録と同じ記録に揃える）
    before_gl = (catalog.table_glossary(meta, body["table"]) if body.get("table")
                 else catalog.db_glossary(meta))
    _log_glossary_diff(path.name, body.get("table") or None, before_gl, gl)
    if body.get("table"):
        catalog.set_table_glossary(meta, body["table"], gl)
    elif gl:
        meta["glossary"] = gl
    else:
        meta.pop("glossary", None)
    catalog.save_meta(path, meta)
    return jsonify({"ok": True})


def _sql_scope(sql: str, path: Path) -> list[dict]:
    """このSQLを実行するのに繋ぐべきDBを決める。

    例文も用語のSQL式も、チャットと同じように別DBのテーブルへ
    「demo_master.employees」の形で入ることがある（人事DBに社員の氏名は無く、
    マスタDB側にある、など）。編集中のDBだけを繋いで検証すると、
    実際には通るSQLが "no such table" で落ちてしまうので、
    式が名前を挙げているDBは一緒に繋ぐ。
    """
    alias = db.alias_for(path)
    scope = [{"path": str(path), "alias": alias}]
    for p in db.list_db_files():
        if p == path or len(scope) >= db.MAX_ATTACHED:
            continue
        a = db.alias_for(p)
        if a.lower() == alias.lower():
            continue
        if re.search(r'(?<![\w."])' + re.escape(a) + r'\s*\.', sql, re.IGNORECASE):
            scope.append({"path": str(p), "alias": a})
    return scope


def _entries_for(scope: list[dict], cache: dict) -> list[dict]:
    """結合定義を引くための材料（各DBのプロファイルとメタ）を揃える。

    「すべて検証」では同じDBを何度も見るので、1リクエストの間だけ控えておく。
    """
    entries = []
    for s in scope:
        if s["alias"] not in cache:
            p = Path(s["path"])
            cache[s["alias"]] = {"alias": s["alias"], "profile": catalog.profile_db(p),
                                 "meta": catalog.load_meta(p)}
        entries.append(cache[s["alias"]])
    return entries


def _referenced_tables(sql: str, entries: list[dict], own_alias: str) -> list[tuple]:
    """SQL式の中に出てくるテーブルを (エイリアス, テーブル名) で拾う。

    自DBの "attendances.overtime_min" という書き方と、DBをまたぐ
    "demo_master.employees.employee_id" という書き方の両方を見つける。
    長い名前から先に照合する（"emp" が "employees" に化けるのを防ぐ）。
    """
    found = []
    for e in entries:
        a = e["alias"]
        for t in sorted(e["profile"].get("tables") or {}, key=len, reverse=True):
            qualified = (r'(?<![\w."])' + re.escape(a) + r'\s*\.\s*'
                         + re.escape(t) + r'\s*\.')
            if re.search(qualified, sql, re.IGNORECASE):
                found.append((a, t))
            elif a == own_alias and re.search(
                    r'(?<![\w."])' + re.escape(t) + r'\s*\.', sql, re.IGNORECASE):
                found.append((a, t))
    return found


def _table_label(at: tuple, own_alias: str) -> str:
    """画面に出すテーブル名。別DBのものはどのDBか分かるようにする。"""
    return at[1] if at[0] == own_alias else f"{at[0]}.{at[1]}"


def _from_clause(tables: list[tuple], entries: list[dict]) -> tuple[str, bool]:
    """複数テーブルをつなぐ FROM句を組み立てる。

    tables: [(エイリアス, テーブル名), ...]
    カタログに結合定義があればそれで JOIN する。無ければ素直に並べる
    （直積になるが、SELECT専用・タイムアウトつきなので暴走はしない）。
    戻り値の2つ目は「全部つなげたか」。直積のときは件数の割合に意味が無いので、
    呼び出し側でその旨を添える。
    """
    def q(at):
        return f'{at[0]}."{at[1]}"'

    if len(tables) <= 1:
        return q(tables[0]), True

    edges = catalog.collect_edges(entries)
    joined, sql, all_linked = [tables[0]], q(tables[0]), True
    for t in tables[1:]:
        cond = None
        for e in edges:
            (fa, ft, fc), (ta, tt, tc) = e["from"], e["to"]
            pair = {(fa, ft), (ta, tt)}
            if t in pair and pair & set(joined) and (fa, ft) != (ta, tt):
                cond = f'{fa}."{ft}"."{fc}" = {ta}."{tt}"."{tc}"'
                break
        if cond:
            sql += f" JOIN {q(t)} ON {cond}"
        else:
            sql += f", {q(t)}"
            all_linked = False
        joined.append(t)
    return sql, all_linked


@bp_catalog.post("/api/catalog/glossary/verify")
@admin_required
def verify_glossary():
    """用語のSQL式を実データに当てて確かめる。

    テーブルを1つ選んでいればそのテーブルで、DB全体の用語なら式が触れている
    テーブルを式から読み取って組み立てる。結合定義があればJOINでつなぐ。
    """
    body = request.json or {}
    path = db.path_for(body["db"])
    alias = db.alias_for(path)
    picked = body.get("table")
    cache: dict = {}

    out = []
    for row in body.get("terms") or []:
        sql = (row.get("sql") or "").strip()
        term = row.get("term") or ""
        if not sql:
            out.append({"term": term, "verdict": "－", "detail": "SQL式が未入力"})
            continue

        # 式が別DBの名前を出していれば、そのDBも繋いだ上で確かめる
        scope = _sql_scope(sql, path)
        entries = _entries_for(scope, cache)
        # 置き場所のテーブルを土台にしつつ、式が名前を挙げているテーブルも足す。
        # 「MTBF = 稼働時間 ÷ アラーム件数」のように、1つの用語が
        # 隣のテーブルを見に行くことがあるため（置き場所だけでは列が足りない）。
        used = _referenced_tables(sql, entries, alias)
        if picked and (alias, picked) not in used:
            used.insert(0, (alias, picked))
        if not used:
            # どのテーブルにも触れていない式。定数などはそのまま評価できる
            try:
                _, rows, _ = db.run_select(f"SELECT {sql} AS v", scope, max_rows=1)
                out.append({"term": term, "verdict": "計算式",
                            "detail": f"計算結果: {rows[0][0]}"})
            except Exception as e:
                out.append({"term": term, "verdict": "エラー",
                            "detail": "テーブル名が見つかりません。"
                                      "「売上.金額」のようにテーブル名から書いてください。"
                                      f"（{str(e).splitlines()[0][:70]}）"})
            continue

        src, linked = _from_clause(used, entries)
        labels = [_table_label(t, alias) for t in used]
        note = f"／ 対象: {'、'.join(labels)}" if len(used) > 1 or not picked else ""
        if not linked:
            note += "（結合定義が無いため総当たりで数えています。"
            note += "「結合・ER図」で関連を登録すると正確になります）"
        try:
            _, rows, _ = db.run_select(
                f"SELECT COUNT(*) AS n, (SELECT COUNT(*) FROM {src}) AS total "
                f"FROM {src} WHERE {sql}", scope, max_rows=1)
            n, total = rows[0]
            pct = f"（{n / total * 100:.1f}%）" if total else ""
            out.append({"term": term, "verdict": "条件式",
                        "detail": f"該当 {n:,} 行 / 全 {total:,} 行{pct}{note}"})
            continue
        except Exception as first:
            err = str(first).splitlines()[0][:120]
        try:
            _, rows, _ = db.run_select(f"SELECT {sql} AS v FROM {src}", scope, max_rows=1)
            out.append({"term": term, "verdict": "計算式",
                        "detail": f"計算結果の例: {rows[0][0]}{note}"})
        except Exception:
            out.append({"term": term, "verdict": "エラー", "detail": err})
    return jsonify({"results": out})


@bp_catalog.post("/api/catalog/examples/verify")
@admin_required
def verify_examples():
    """例文のSQLが実際に通るか確かめる。

    例文は「正しいと確認済みの例」としてAIに渡すので、通らないSQLが混ざると
    そのまま間違いを教えることになる。保存前にここで気づけるようにする。
    """
    body = request.json or {}
    path = db.path_for(body["db"])
    out = []
    for row in body.get("examples") or []:
        sql = (row.get("sql") or "").strip()
        q = (row.get("q") or "").strip()
        if not sql:
            out.append({"q": q, "verdict": "－", "detail": "SQLが未入力"})
            continue
        # 例文はDBをまたぐことがある（人事の勤怠 × マスタの社員、など）。
        # チャットと同じように、式が名前を挙げているDBを全部繋いで確かめる。
        scope = _sql_scope(sql, path)
        others = [s["alias"] for s in scope[1:]]
        cross = (f"／ {'、'.join(others)} も参照しています"
                 "（チャットではこれらのDBも一緒に選ぶ必要があります）") if others else ""
        try:
            columns, rows, truncated = db.run_select(sql, scope, max_rows=5)
        except Exception as e:
            out.append({"q": q, "verdict": "エラー",
                        "detail": str(e).splitlines()[0][:160]})
            continue
        if not rows:
            out.append({"q": q, "verdict": "0行",
                        "detail": f"実行できましたが0行でした（列: {'、'.join(columns)}）。"
                                  f"抽出条件が厳しすぎないか確認してください。{cross}"})
        else:
            more = "以上" if truncated else ""
            out.append({"q": q, "verdict": "OK",
                        "detail": f"{len(rows)}{more}行 取得（列: {'、'.join(columns)}）{cross}"})
    return jsonify({"results": out})


def _home_db(sql: str, preferred: Path | None = None) -> str:
    """このツール定義を置くDBファイルを決める。

    ツールはDBを選ばずに作るが、定義の置き場（どの .meta.yaml か）は
    1つに決めないといけない。SQLが最初に名指ししているDB＝主に見ているDBに置く。
    そのDBを消せばツールも一緒に片づく（cleanup.py の巻き添え掃除に乗る）。
    """
    if preferred is not None:
        return Path(preferred).name
    allscope = [{"path": str(p), "alias": db.alias_for(p), "name": p.name,
                 "tables": list((catalog.profile_db(p).get("tables") or {}).keys())}
                for p in db.list_db_files()]
    hits = dbs_in_sql(sql, allscope)
    if hits:
        return hits[0]["name"]
    files = db.list_db_files()
    return files[0].name if files else ""


def _sample_params(tool: dict, given: dict | None = None) -> dict:
    """試し実行に使う値。画面で入れた値 → AIが添えた例 → 型ごとの既定値、の順に採る。

    例を使うのは、日本語だけで作ったツールを人が確かめられるようにするため。
    空の値で流すと 0行 になり、「SQLが通った」ことしか分からない。実在する値を
    入れて実際の行を見せれば、SQLを読まなくても正しさを判断できる。

    それでも0行になることはある（条件が厳しいだけかもしれない）ので、
    0行は失敗にせず、そのことを画面に出す。
    """
    out = {}
    for p in (tool.get("parameters") or []):
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        for v in ((given or {}).get(name), p.get("example")):
            if v not in (None, ""):
                out[name] = v
                break
        else:
            t = p.get("type") or "string"
            out[name] = 0 if t in ("integer", "number", "boolean") else ""
    return out


@bp_catalog.post("/api/catalog/tool/try")
@admin_required
def try_tool():
    """ツールのSQLを実データで動かして、出てくる列と先頭の行を返す。

    SQLを読めない人にも「何が出るか」で正しさを判断してもらうための口。
    実行は run_select を通すので SELECT 以外は動かない。
    """
    body = request.json or {}
    path = db.path_for(body["db"])
    tool = body.get("tool") or {}
    errs = [e for e in custom_tools.validate_custom_tool(tool) if not e.startswith("'")]
    sql = str(tool.get("sql") or "").strip()
    if not sql:
        return jsonify({"ok": False, "error": "SQLがありません。"})
    try:
        params = custom_tools.coerce_params(tool, _sample_params(tool, body.get("values")))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)})

    scope = _sql_scope(sql, path)
    try:
        columns, rows, truncated = db.run_select(sql, scope, max_rows=8, params=params)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e).splitlines()[0][:200]})
    others = [s["alias"] for s in scope[1:]]
    return jsonify({
        "ok": True, "columns": columns,
        "rows": [[jsonable(v) for v in r] for r in rows],
        "truncated": truncated, "problems": errs,
        "cross": others,
        "note": ("実行できましたが0行でした。抽出条件やパラメータの値を見直してください。"
                 if not rows else ""),
    })


@bp_catalog.post("/api/catalog/tool/draft")
@admin_required
def draft_tool():
    """日本語の「やりたいこと」から、ツールの下書きをAIに起こさせる。

    起こしたらその場で実データに当てて確かめ、失敗したらエラーを添えて
    もう一度だけ書き直させる。通らないSQLをそのまま画面に出さないため。
    """
    body = request.json or {}
    # DBは指定させない。どのDBを使うかは、やりたいことを読んだAIが決める。
    # 特定のDBに限りたいときだけ db を渡せる。
    path = db.path_for(body["db"]) if body.get("db") else None
    purpose = str(body.get("purpose") or "").strip()
    if not purpose:
        return jsonify({"error": "何をするツールかを書いてください。"}), 400
    if not llm.is_configured():
        return jsonify({"error": "LLMが未設定です。env の OPENAI_* を設定してください。"}), 400

    wanted = [str(x).strip() for x in (body.get("params") or []) if str(x).strip()]
    render = body.get("render") or "table"
    # AIが付けた名前が不正・重複でも、保存で突き返されるのはユーザーには
    # 意味不明（名前を入力していないので）。ここで必ず有効な名前に直す。
    taken = [t.get("name") for t in custom_tools.collect_everywhere()]
    tried = []
    draft, last_err = None, None
    for attempt in range(2):          # 1回目でだめならエラーを見せて書き直させる
        try:
            draft = llm.draft_tool(path, purpose, wanted, render,
                                   previous=draft, error=last_err)
        except Exception as e:
            return jsonify({"error": f"下書きに失敗しました: {e}"}), 500
        draft["name"] = custom_tools.custom_tool_safe_name(draft.get("name") or purpose, taken)
        sql = draft.get("sql") or ""
        if not sql:
            last_err = "SQLが空でした。"
            tried.append(last_err)
            continue
        try:
            params = custom_tools.coerce_params(draft, _sample_params(draft))
            scope = (_sql_scope(sql, path) if path
                     else db.widen_scope(sql, []))
            columns, rows, _ = db.run_select(sql, scope, max_rows=8, params=params)
        except Exception as e:
            last_err = str(e).splitlines()[0][:200]
            tried.append(last_err)
            continue
        return jsonify({"ok": True, "tool": draft, "columns": columns,
                        "rows": [[jsonable(v) for v in r] for r in rows],
                        "home_db": _home_db(sql, path),
                        "attempts": attempt + 1, "tried": tried})

    # 2回とも通らなかった。下書きは返す（人が直せるように）
    return jsonify({"ok": False, "tool": draft, "error": last_err, "tried": tried})


@bp_catalog.post("/api/catalog/glossary/draft")
@admin_required
def draft_glossary():
    body = request.json or {}
    path = db.path_for(body["db"])
    terms = [{"term": r["term"], "description": r.get("description", "")}
             for r in (body.get("terms") or []) if r.get("term") and r.get("description")]
    try:
        drafted = llm.draft_glossary_sql(path, body.get("table"), terms)
    except Exception as e:
        return jsonify({"error": f"下書きに失敗しました: {e}"}), 500
    return jsonify({"ok": True, "drafted": drafted})


@bp_catalog.post("/api/catalog/draft-table")
@admin_required
def draft_table():
    body = request.json or {}
    path = db.path_for(body["db"])
    try:
        draft = llm.draft_table_meta(path, body["table"])
    except Exception as e:
        return jsonify({"error": f"AI下書きに失敗しました: {e}"}), 500
    return jsonify({"ok": True, "draft": draft})


@bp_catalog.post("/api/catalog/checks")
@admin_required
def save_checks():
    """検算ルールの保存。空になったらキーごと消す。"""
    body = request.json or {}
    path = db.path_for(body["db"])
    checks = verify.normalize(body.get("checks"))
    names = [c["name"] for c in checks]
    if len(names) != len(set(names)):
        dup = next(n for n in names if names.count(n) > 1)
        return jsonify({"error": f"「{dup}」という名前の検算ルールが複数あります。"
                                 "名前を変えて区別してください。"}), 400
    meta = catalog.load_meta(path)
    if checks:
        meta["checks"] = checks
    else:
        meta.pop("checks", None)
    catalog.save_meta(path, meta)
    verify.clear_cache()          # ルールが変わったので、古い検算結果は捨てる
    return jsonify({"ok": True, "checks": checks})


@bp_catalog.post("/api/catalog/checks/verify")
@admin_required
def verify_checks():
    """検算ルールをその場で実行して、左右の値と差を返す（保存前の内容でよい）。"""
    body = request.json or {}
    path = db.path_for(body["db"])
    out = []
    for raw in body.get("checks") or []:
        raw = raw or {}
        lsql = str((raw.get("left") or {}).get("sql") or "").strip()
        rsql = str((raw.get("right") or {}).get("sql") or "").strip()
        if not lsql or not rsql:
            out.append({"ok_run": False, "error": "左右の両方にSQLが必要です。"})
            continue
        check = verify.normalize([raw])
        if not check:
            out.append({"ok_run": False, "error": "ルールの形が正しくありません。"})
            continue
        # 検算のSQLは別DBを参照できる。名前を挙げているDBも繋いで実行する
        combined = " ".join([lsql, rsql, str(raw.get("drilldown") or "")])
        scope = _sql_scope(combined, path)
        res = verify.run_check(check[0], scope, use_cache=False)
        out.append({k: res[k] for k in
                    ("ok_run", "match", "left", "right", "diff", "pct", "error", "drill")})
    return jsonify({"results": out})


@bp_catalog.get("/api/catalog/usage")
@admin_required
def er_usage():
    """過去の分析で実際に使われた結合の回数（ER図に重ねる）。"""
    path = db.path_for(request.args.get("db") or "")
    return jsonify(sqlusage.usage_for(db.alias_for(path)))


def _log_glossary_diff(db_file: str, table, before: dict, after: dict) -> None:
    """用語集の一括保存を、用語ごとの差分にして履歴へ。"""
    user = getattr(g.user, "username", None)
    for term in after:
        if term not in before:
            catalog_history.add_catalog_change("glossary", "add", db_file, term, user=user,
                                table=table, after=after[term], source="catalog")
        elif before[term] != after[term]:
            catalog_history.add_catalog_change("glossary", "update", db_file, term, user=user,
                                table=table, before=before[term],
                                after=after[term], source="catalog")
    for term in before:
        if term not in after:
            catalog_history.add_catalog_change("glossary", "remove", db_file, term, user=user,
                                table=table, before=before[term], source="catalog")


@bp_catalog.post("/api/catalog/examples")
@admin_required
def save_examples():
    body = request.json or {}
    path = db.path_for(body["db"])
    meta = catalog.load_meta(path)
    incoming = [e for e in (body.get("examples") or [])
                if str(e.get("q", "")).strip() and str(e.get("sql", "")).strip()]
    new = catalog.dedupe_examples(incoming)

    # 差分をSQLをキーに取り、誰が何を変えたかを残す
    user = getattr(g.user, "username", None)
    old_by_sql = {e.get("sql"): e for e in (meta.get("examples") or [])}
    new_by_sql = {e.get("sql"): e for e in new}
    for s_, e_ in new_by_sql.items():
        if s_ not in old_by_sql:
            catalog_history.add_catalog_change("example", "add", path.name, e_.get("q", ""),
                                user=user, after=e_, source="catalog")
        elif old_by_sql[s_] != e_:
            catalog_history.add_catalog_change("example", "update", path.name, e_.get("q", ""),
                                user=user, before=old_by_sql[s_], after=e_,
                                source="catalog")
    for s_, e_ in old_by_sql.items():
        if s_ not in new_by_sql:
            catalog_history.add_catalog_change("example", "remove", path.name, e_.get("q", ""),
                                user=user, before=e_, source="catalog")

    meta["examples"] = new
    catalog.save_meta(path, meta)
    dropped = len(incoming) - len(meta["examples"])
    return jsonify({"ok": True, "dropped": dropped,
                    "examples": meta["examples"]})


@bp_catalog.post("/api/catalog/overview")
@admin_required
def save_overview():
    body = request.json or {}
    path = db.path_for(body["db"])
    meta = catalog.load_meta(path)
    meta["title"] = (body.get("title") or "").strip()
    # 説明は1欄（注意したい事実は ※ で始める行として同じ欄に書く）。旧 caveats は畳む
    meta["description"] = "\n".join(
        l.rstrip() for l in (body.get("description") or "").splitlines()).strip()
    meta.pop("caveats", None)
    catalog.save_meta(path, meta)
    return jsonify({"ok": True})


@bp_catalog.post("/api/catalog/tool")
@admin_required
def save_tool():
    """ユーザー定義ツールの追加・更新・削除。"""
    body = request.json or {}
    path = db.path_for(body["db"])
    meta = catalog.load_meta(path)
    items = list(meta.get("tools") or [])
    name = body.get("name")

    if body.get("action") == "delete":
        items = [t for t in items if t.get("name") != name]
    else:
        tool = body.get("tool") or {}
        # 既存の名前も見て検証する。見ていないと、新規作成で同名を付けたとき
        # 既存のツールを黙って上書きしてしまう（更新は original の名前だけ除く）。
        original = body.get("original") or ""
        others = {t.get("name") for t in items if t.get("name") != original}
        errors = custom_tools.validate_custom_tool(tool, others)
        if errors:
            return jsonify({"error": " / ".join(errors)}), 400
        items = [t for t in items if t.get("name") != (original or name)]
        items.append(tool)
    if items:
        meta["tools"] = items
    else:
        meta.pop("tools", None)
    catalog.save_meta(path, meta)
    return jsonify({"ok": True})


@bp_catalog.post("/api/catalog/builtin")
@admin_required
def save_builtin():
    body = request.json or {}
    path = db.path_for(body["db"])
    meta = catalog.load_meta(path)
    over = dict(meta.get("builtin_tools") or {})
    over[body["name"]] = {"enabled": bool(body.get("enabled", True)),
                          "description": (body.get("description") or "").strip()}
    if not over[body["name"]]["description"] and over[body["name"]]["enabled"]:
        over.pop(body["name"])
    meta["builtin_tools"] = over
    catalog.save_meta(path, meta)
    return jsonify({"ok": True})


# ==========================================================================
# ===== 元 web/import_bp.py
# データ取り込み画面。Excel / CSV / TXT から DB・テーブルを作り、定期更新も設定する。
# ==========================================================================
from datetime import datetime
from pathlib import Path

from flask import Blueprint, g, jsonify, render_template, request

import catalog
import cleanup
import config
import db
import history
import importer
import jobs
import scheduler


bp_import = Blueprint("imp", __name__)


@bp_import.get("/import", endpoint="index")
@admin_required
def import_index():
    return render_template(
        "import.html",
        dirs=importer.dir_status(),
        files=[{"path": str(p), "label": importer.display_name(p)}
               for p in importer.list_source_files()],
        max_files=config.IMPORT_MAX_FILES,
        extensions=", ".join(config.IMPORT_EXTENSIONS),
        delimiters=list(importer.DELIMITERS),
        db_files=[f.name for f in db.list_db_files()],
        existing={f.name: importer.existing_tables(f) for f in db.list_db_files()},
        manage=_manage_view(),
        intervals=list(jobs.INTERVALS),
        modes=jobs.MODES,
        default_ts=config.IMPORT_TIMESTAMP_COLUMN,
        max_keep=jobs.MAX_KEEP_RUNS,
        default_keep=jobs.DEFAULT_KEEP_RUNS,
        dirs_editable=config.IMPORT_DIRS_EDITABLE,
        allow_upload=config.IMPORT_ALLOW_UPLOAD,
    )


def _manage_view() -> dict:
    """「DBの管理」タブが必要とするもの一式。

    定期取り込みは対象テーブルに紐づけて見せるので、DB→テーブル→そのテーブルを
    更新するジョブ、という並びにまとめる。テーブルが消えた・まだ作られていない
    ジョブは宙に浮くので orphans に分けて、画面から必ず触れるようにする。
    """
    by_target: dict[tuple, list[dict]] = {}
    for j in jobs.list_jobs():
        by_target.setdefault((j.get("db_file"), j.get("table")), []).append(j)

    used: set[tuple] = set()
    dbs = []
    for f in db.list_db_files():
        try:
            names = importer.existing_tables(f)
        except Exception as e:
            dbs.append({"name": f.name, "error": str(e), "tables": []})
            continue
        tables = []
        for t in names:
            js = by_target.get((f.name, t), [])
            if js:
                used.add((f.name, t))
            info = importer.table_info(f, t, js[0].get("timestamp_column") if js else None)
            tables.append({**info, "jobs": [_job_row(j) for j in js]})
        st = f.stat()
        dbs.append({"name": f.name, "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "tables": tables})

    orphans = [_job_row(j) for key, js in by_target.items() if key not in used for j in js]
    return {"dbs": dbs, "orphans": orphans, "locked": _locked_tables(),
            "sched": scheduler.scheduler_status()}


def _locked_tables() -> dict:
    """手で更新してはいけないテーブル。{DBファイル: {テーブル: 理由}}

    定期実行＋追記のテーブルは、画面からの1回きりの取り込みでも
    余計な取得日時が1回ぶん増えて更新間隔が崩れるので、そちらも止める。
    """
    out: dict[str, dict] = {}
    for j in jobs.list_jobs():
        why = jobs.manual_run_blocked(j)
        if why:
            out.setdefault(j.get("db_file", ""), {})[j.get("table", "")] = why
    return out


def _job_row(j: dict) -> dict:
    nxt = jobs.next_run_at(j)
    kept = None
    if j.get("timestamp_column"):
        try:
            kept = importer.run_count(config.DATA_DIR / j["db_file"], j["table"],
                                      j["timestamp_column"])
        except Exception:
            kept = None
    # 画面はこれらのキーを必ず読むので、古い定義や手書きのジョブでも欠けないよう埋める
    defaults = {"sheet": None, "delimiter": None, "header_row": 0, "start_at": "",
                "timestamp_column": None, "keep_runs": None, "enabled": True,
                "last_run": "", "last_status": "", "last_message": "", "columns": [],
                "last_degraded": []}
    return {**defaults, **j,
            "interval_label": jobs.interval_label(j.get("interval_minutes", 0)),
            "mode_label": "追記" if j.get("mode") == "append" else "全件入れ替え",
            "source_label": importer.display_name(Path(j.get("source", ""))),
            "kept": kept,
            "manual_blocked": jobs.manual_run_blocked(j),
            "next_label": nxt.strftime("%m-%d %H:%M") if nxt else "手動のみ"}


# =============================================================================
# 取り込み元フォルダの管理とファイル選択
# =============================================================================

@bp_import.get("/api/import/dirs")
@admin_required
def dirs_list():
    return jsonify({"dirs": [{k: (str(v) if k == "path" else v) for k, v in d.items()}
                             for d in importer.dir_status()],
                    "editable": config.IMPORT_DIRS_EDITABLE and bool(g.user.is_admin)})


@bp_import.post("/api/import/dirs")
@admin_required
def dirs_edit():
    """取り込み元フォルダの追加・削除。読める範囲が広がる操作なので管理者だけ。"""
    if not g.user.is_admin:
        return jsonify({"error": "取り込み元フォルダの変更は管理者のみです。"}), 403
    body = request.json or {}
    try:
        if body.get("action") == "remove":
            importer.remove_dir(body.get("path", ""))
        else:
            importer.add_dir(body.get("path", ""))
    except importer.ImportError_ as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "dirs": importer.dir_status()})


@bp_import.post("/api/import/browse")
@admin_required
def browse():
    """フォルダを1階層ぶん開く（エクスプローラ風の選択画面用）。"""
    try:
        return jsonify(importer.browse((request.json or {}).get("path") or None))
    except importer.ImportError_ as e:
        return jsonify({"error": str(e)}), 400


# =============================================================================
# プレビューと取り込み
# =============================================================================

def _read_source(body: dict, nrows=None):
    """サーバのフォルダ / アップロード のどちらからでも DataFrame を返す。"""
    delim = importer.DELIMITERS.get(body.get("delimiter") or "自動判定")
    header = int(body.get("header_row") or 0)
    sheet = body.get("sheet") or None
    token = body.get("upload")
    if token:
        item = filestore.get(token, g.user.username)
        if item is None:
            raise importer.ImportError_(
                "アップロードしたファイルが見つかりません。もう一度選び直してください。")
        return importer.read_upload(item["data"], item["filename"], sheet=sheet,
                                    header_row=header, delimiter=delim, nrows=nrows)
    return importer.read_table(Path(body.get("path", "")), sheet=sheet, header_row=header,
                               delimiter=delim, nrows=nrows)


@bp_import.post("/api/import/upload")
@admin_required
def upload():
    """手元のPCから選んだファイルを受け取る。ディスクには書かず、メモリに預かる。"""
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "ファイルが選ばれていません。"}), 400
    data = f.read()
    try:
        importer.check_upload(data, f.filename or "")
    except importer.ImportError_ as e:
        return jsonify({"error": str(e)}), 400
    token = filestore.put(data, f.filename or "upload", "application/octet-stream",
                          g.user.username)
    try:
        sheets = importer.upload_sheet_names(data, f.filename or "")
    except importer.ImportError_ as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "upload": token, "name": f.filename,
                    "size": len(data), "sheets": sheets})


@bp_import.post("/api/import/preview")
@admin_required
def preview():
    body = request.json or {}
    token = body.get("upload")
    try:
        if token:
            item = filestore.get(token, g.user.username)
            if item is None:
                raise importer.ImportError_(
                    "アップロードしたファイルが見つかりません。もう一度選び直してください。")
            stem = Path(item["filename"]).stem
            sheets = importer.upload_sheet_names(item["data"], item["filename"])
        else:
            path = Path(body.get("path", ""))
            stem = path.stem
            sheets = importer.sheet_names(path)
        df = _read_source(body, nrows=2000)
    except importer.ImportError_ as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"読み込みに失敗しました: {e}"}), 400

    plan = importer.plan_columns(df)
    head = df.head(config.IMPORT_PREVIEW_ROWS)
    return jsonify({
        "ok": True, "sheets": sheets,
        "columns": [str(c) for c in df.columns],
        "rows": jsonable(head.values.tolist()),
        "scanned": len(df),
        "plan": [{**p, "include": True} for p in plan],
        "suggest_table": importer.safe_name(stem),
        "suggest_db": stem,
    })


def _log_manual(db_path, body: dict, mode: str, ok: bool, message: str,
                started, **kw) -> None:
    """画面からの1回きりの取り込みを履歴に残す（成功も失敗も）。"""
    upload = body.get("upload")
    source = "（自分のPCからアップロード）" if upload else body.get("path", "")
    history.add_import_record(db_path.name if db_path else (body.get("db_file") or ""),
                importer.safe_name(body.get("table", "")), ok, message,
                kind="manual", mode=mode, source=source,
                sheet=body.get("sheet") or None,
                user=getattr(g.user, "username", None), started=started, **kw)


@bp_import.post("/api/import/run")
@admin_required
def run():
    body = request.json or {}
    cols = [{"元の列名": c["source"], "列名": importer.safe_name(c["name"], c["source"]),
             "型": c["type"]} for c in (body.get("columns") or []) if c.get("include")]
    if not cols:
        return jsonify({"error": "取り込む列が選ばれていません。"}), 400

    mode = body.get("mode") or "replace"
    ts_col = (body.get("timestamp_column") or "").strip() or None
    keep = body.get("keep_runs")
    # 1回きりの取り込みでも、定期取り込みと同じ条件を課す。
    # 後から定期化したときに「取得日時が無い古い行」が残らないようにするため。
    errors = jobs.validate_job({"db_file": "x", "table": "x", "source": "x", "mode": mode,
                            "timestamp_column": ts_col, "keep_runs": keep})
    if errors:
        return jsonify({"error": " / ".join(errors)}), 400
    if mode == "append":
        keep = int(keep)

    # 定期実行＋追記のテーブルは、手で足すと取得日時が1回ぶん余計に増えて
    # 更新間隔が崩れる。定期取り込みの「▶ 今すぐ更新」と同じ理由で止める。
    target_db = (body.get("db_file") or "") if not body.get("new_db") else ""
    locked = _locked_tables().get(target_db, {}).get(importer.safe_name(body.get("table", "")))
    if locked:
        return jsonify({"error": locked}), 400

    started = datetime.now()
    db_path = None
    try:
        db_path = (importer.db_path_for(body["db_name"]) if body.get("new_db")
                   else config.DATA_DIR / body["db_file"])
        full = _read_source(body)
        n, degraded = importer.import_dataframe(
            db_path, body["table"], full, cols, mode=mode, timestamp_col=ts_col)
        removed = (importer.prune_runs(db_path, body["table"], ts_col, keep)
                   if mode == "append" else 0)
        kept = importer.run_count(db_path, body["table"], ts_col)
    except importer.ImportError_ as e:
        _log_manual(db_path, body, mode, False, str(e), started, keep=keep)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        _log_manual(db_path, body, mode, False, f"取り込みに失敗しました: {e}",
                    started, keep=keep)
        return jsonify({"error": f"取り込みに失敗しました: {e}"}), 500

    message = f"{n:,}行を{'追記' if mode == 'append' else '全件入れ替え'}しました。"
    if mode == "append":
        message += f" 保持 {kept}/{keep}回"
        if removed:
            message += f"（古い {removed:,}行を削除）"
    _log_manual(db_path, body, mode, True, message, started,
                rows=n, removed=removed, kept=kept, keep=keep)

    catalog.profile_db(db_path, force=True)
    return jsonify({"ok": True, "rows": n, "degraded": degraded, "removed": removed,
                    "kept": kept, "keep": keep if mode == "append" else None,
                    "timestamp_column": importer.safe_name(ts_col, "取得日時"),
                    "db": db_path.name, "table": importer.safe_name(body["table"])})


@bp_import.get("/api/import/manage")
@admin_required
def manage_view():
    return jsonify(_manage_view())


@bp_import.get("/api/import/table")
@admin_required
def table_detail():
    """テーブルを開いたときに読む中身（サンプル行と更新履歴）。

    一覧を出すたびに全テーブルを走査すると重いので、開いたものだけ取りに来る。
    """
    db_file = request.args.get("db", "")
    table = request.args.get("table", "")
    path = config.DATA_DIR / db_file
    if path.parent.resolve() != config.DATA_DIR.resolve() or not path.exists():
        return jsonify({"error": "DBが見つかりません。"}), 404
    ts = next((j.get("timestamp_column") for j in jobs.list_jobs()
               if j.get("db_file") == db_file and j.get("table") == table), None)
    return jsonify({
        "sample": jsonable(importer.sample_rows(path, table, timestamp_col=ts)),
        "history": history.for_table(db_file, table, limit=50),
        "kinds": history.IMPORT_RECORD_KINDS,
    })


@bp_import.get("/api/import/impact")
@admin_required
def impact():
    """消す前の下見。何が巻き添えになるかを返す（何も書き換えない）。

    table を付ければテーブル1つ、無ければDB丸ごとの分。
    """
    try:
        path = db.path_for(request.args.get("db") or "")
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    table = request.args.get("table") or ""
    found = (cleanup.table_impact(path, table) if table
             else cleanup.db_impact(path))
    return jsonify({"db": path.name, "table": table,
                    "groups": cleanup.summarize(found)})


@bp_import.post("/api/import/drop-table")
@admin_required
def drop_table():
    """テーブルを消して、カタログに残る参照も一緒に片づける。

    掃除をしないと、存在しないテーブルの説明がAIに渡り続け、
    例文の検証は no such table で落ちる。
    """
    body = request.json or {}
    try:
        path = db.path_for(body.get("db") or "")
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    table = str(body.get("table") or "").strip()
    if not table:
        return jsonify({"error": "テーブル名がありません。"}), 400
    importer.drop_table(path, table)
    done = cleanup.clean_table(path, table,
                              drop_jobs=body.get("drop_jobs", True) is not False)
    print(f"[import] {path.name} の {table} を削除しました（{g.user.username}）")
    return jsonify({"ok": True, "groups": cleanup.summarize(done)})


@bp_import.post("/api/import/delete-db")
@admin_required
def delete_db():
    """DBをファイルごと消す。元には戻せないので、
    ファイル名をそのまま入力してもらったときだけ実行する。
    """
    body = request.json or {}
    try:
        path = db.path_for(body.get("db") or "")
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    if str(body.get("confirm") or "").strip() != path.name:
        return jsonify({"error": f"確認のため、DBのファイル名「{path.name}」を"
                                 "そのまま入力してください。"}), 400
    try:
        done = cleanup.delete_db(path, drop_jobs=body.get("drop_jobs", True) is not False)
    except (ValueError, OSError) as e:
        return jsonify({"error": str(e)}), 400
    print(f"[import] {path.name} を削除しました（{g.user.username}）")
    return jsonify({"ok": True, "groups": cleanup.summarize(done)})


# =============================================================================
# 定期取り込み
# =============================================================================

@bp_import.post("/api/jobs/save")
@admin_required
def job_save():
    body = request.json or {}
    if body.get("upload") or not body.get("path"):
        return jsonify({"error": "アップロードしたファイルは定期取り込みに登録できません"
                                 "（サーバ上に置かれていないため、次回以降読み直せません）。"
                                 "取り込み元フォルダに置いたファイルを選んでください。"}), 400
    cols = [{"元の列名": c["source"], "列名": importer.safe_name(c["name"], c["source"]),
             "型": c["type"]} for c in (body.get("columns") or []) if c.get("include")]
    # 「＋ 新しいDBを作る」のままでも登録できるようにする。
    # ファイルは最初の実行時に作られるので、ここでは名前だけ決めておけばよい。
    db_file = body.get("db_file")
    if not db_file and body.get("db_name"):
        try:
            db_file = importer.db_path_for(body["db_name"]).name
        except importer.ImportError_ as e:
            return jsonify({"error": str(e)}), 400
    draft = {
        "id": body.get("id"),
        "name": (body.get("name") or "").strip() or Path(body.get("path", "")).stem,
        "source": body.get("path"), "sheet": body.get("sheet") or None,
        "header_row": int(body.get("header_row") or 0),
        "delimiter": importer.DELIMITERS.get(body.get("delimiter") or "自動判定"),
        "db_file": db_file, "table": importer.safe_name(body.get("table", "")),
        "mode": body.get("mode") or "replace",
        "timestamp_column": (body.get("timestamp_column") or "").strip() or None,
        # 取得日時は全件入れ替えでも付ける（いつ時点のデータかを残すため）
        "keep_runs": body.get("keep_runs"),
        "start_at": (body.get("start_at") or "").strip(),
        "columns": cols,
        "interval_minutes": jobs.INTERVALS.get(body.get("interval") or "手動のみ", 0),
        "enabled": True,
    }
    errors = jobs.validate_job(draft)
    if errors:
        return jsonify({"error": " / ".join(errors)}), 400
    # 同じ取り込み元→同じテーブルは1つだけ。2つあると同時刻に2回追記されて全行が二重になる
    dup = jobs.find_duplicate(draft)
    if dup:
        return jsonify({"error":
                        f"この取り込み元と保存先の定期取り込み「{dup.get('name')}」はすでに登録されています"
                        f"（{jobs.interval_label(dup.get('interval_minutes') or 0)}）。"
                        "頻度や停止はデータカタログの各テーブルの「管理」で変更できます。"}), 400
    if draft["mode"] == "append":
        draft["keep_runs"] = int(draft["keep_runs"])
    return jsonify({"ok": True, "job": _job_row(jobs.save_job(draft))})


@bp_import.post("/api/jobs/run")
@admin_required
def job_run():
    body = request.json or {}
    # 画面から押した実行は、裏のスケジューラと区別できるように印を付けて履歴に残す
    who = getattr(g.user, "username", None)
    job = jobs.get_job(body.get("id", ""))
    if job is None:
        return jsonify({"error": "ジョブが見つかりません。"}), 404
    blocked = jobs.manual_run_blocked(job)
    if blocked:
        return jsonify({"error": blocked}), 400
    results = [(job, jobs.run_job(job, kind="job", user=who))]
    for j, r in results:
        if r["ok"]:
            catalog.profile_db(config.DATA_DIR / j["db_file"], force=True)
    return jsonify({"ok": True,
                    "results": [{"name": j.get("name"), **r} for j, r in results],
                    "jobs": [_job_row(x) for x in jobs.list_jobs()]})


@bp_import.post("/api/jobs/update")
@admin_required
def job_update():
    body = request.json or {}
    job = jobs.get_job(body.get("id", ""))
    if job is None:
        return jsonify({"error": "ジョブが見つかりません。"}), 404
    if "enabled" in body:
        job["enabled"] = bool(body["enabled"])
    if body.get("interval"):
        job["interval_minutes"] = jobs.INTERVALS.get(body["interval"], 0)
    # 開始日時は触らないので過去チェックはしない（登録時に済んでいる）
    errors = jobs.validate_job(job, check_start=False)
    if errors:
        return jsonify({"error": " / ".join(errors)}), 400
    jobs.save_job(job)
    return jsonify({"ok": True, "jobs": [_job_row(x) for x in jobs.list_jobs()]})


@bp_import.post("/api/jobs/delete")
@admin_required
def job_delete():
    jobs.delete_job((request.json or {}).get("id", ""))
    return jsonify({"ok": True, "jobs": [_job_row(x) for x in jobs.list_jobs()]})




# ==========================================================================
# ===== 元 web/mail_bp.py
# メール設定の画面。
#
# 送信サーバ（ホスト・ポート・タイムアウト）と、誰から誰に送ってよいかを決める。
# env の値は初期値として使い、画面から保存したものが優先される。
#
# 暗号化と認証は社内リレー前提（なし）なので画面には出さない。必要な環境では
# env の SMTP_SECURITY / SMTP_USER / SMTP_PASSWORD で指定する。
# 閲覧・変更ともに管理者のみ。
# ==========================================================================
from flask import Blueprint, g, jsonify, render_template, request

import mailer


bp_mail = Blueprint("mail", __name__)


@bp_mail.get("/mail", endpoint="index")
@admin_required
def mail_index():
    return render_template("mail.html", status=mailer.mail_status(),
                           log=mailer.sent_log(20))


@bp_mail.get("/api/mail/settings")
@admin_required
def get_settings():
    return jsonify({**mailer.mail_status(), "editable": True,
                    "log": mailer.sent_log(20)})


@bp_mail.post("/api/mail/settings")
@admin_required
def post_settings():
    """送信サーバ・差出人・宛先の許可リストを保存する。"""
    try:
        mailer.save_settings(request.json or {}, user=g.user.username)
    except mailer.MailError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, **mailer.mail_status()})


# ==========================================================================
# ===== 元 web/models_bp.py
# モデル設定の画面（管理者のみ）。
#
# チャット画面のプルダウンに出す候補・既定のモデル・画像を扱えるモデルの
# 判定キーワードを決める。ここで候補から外したモデルは、既にそれを選んで
# いた利用者も使えなくなり、既定のモデルに戻る。
# ==========================================================================
from flask import Blueprint, g, jsonify, render_template, request

import db
import models


bp_models = Blueprint("models", __name__)


def _scope():
    """文脈の使用量を測るための基準。

    上限は全員に効くので、見せる数字は「いちばん重いとき」＝全DBを選んだ場合に
    そろえる。管理者本人の選択で測ると、人によって見える数字が変わってしまう。
    """
    return build_scope({f.name: [] for f in db.list_db_files()})


@bp_models.get("/models", endpoint="index")
@admin_required
def models_index():
    return render_template("models.html", status=models.admin_status(scope=_scope()))


@bp_models.get("/api/models/admin")
@admin_required
def get_admin():
    return jsonify(models.admin_status(refresh=request.args.get("refresh") == "1",
                                       scope=_scope()))


@bp_models.post("/api/models/admin")
@admin_required
def post_admin():
    try:
        models.save_admin(request.json or {}, user=g.user.username)
        return jsonify({"ok": True, **models.admin_status(scope=_scope())})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


# ==========================================================================
# ===== 元 web/table_bp.py
# テーブル全体を見る画面。
#
# サンプル行（先頭数行）だけでは「本当にこのテーブルでよいか」が分からないので、
# 中身を1ページずつ辿れる読み取り専用のビューアを別タブで開けるようにする。
#
# ・読むのは db.connect_ro（読み取り専用接続）だけ。書き込みの経路は持たない。
# ・行数が多いテーブルでも落ちないよう、常にサーバ側で LIMIT/OFFSET を付けて返す。
# ・絞り込みは全列を文字として LIKE する素朴なもの。値はプレースホルダで渡す
#   （列名は実在する列名と照合してからでないと SQL に入れない）。
# ==========================================================================
import json

from flask import Blueprint, jsonify, render_template, request

import catalog
import db


bp_table = Blueprint("tableview", __name__)

PAGE_SIZES = (50, 100, 200, 500)
MAX_LIMIT = 500


def _qi(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _resolve(db_name: str, table: str):
    """DBファイルとテーブル名を確かめる。

    db は 'sales.db'（ファイル名）でも 'sales'（エイリアス）でも受ける。
    ER図やチャットからはエイリアスで来るため。
    """
    files = db.list_db_files()
    path = next((f for f in files if f.name == db_name), None)
    if path is None:
        path = next((f for f in files if db.alias_for(f) == db_name), None)
    if path is None:
        return None, None, f"DB '{db_name}' が見つかりません。"
    names = list((catalog.profile_db(path).get("tables") or {}).keys())
    if table not in names:
        return path, None, f"テーブル '{table}' が {path.name} にありません。"
    return path, table, None


@bp_table.get("/table", endpoint="index")
@login_required
def table_index():
    """別タブで開くビューア本体。中身は table.js が API から取ってくる。"""
    db_name = request.args.get("db") or ""
    table = request.args.get("table") or ""
    path, tname, err = _resolve(db_name, table)
    meta = catalog.load_meta(path) if path else {}
    tmeta = ((meta.get("tables") or {}).get(tname) or {}) if tname else {}
    return render_template(
        "table.html",
        nav="tableview",
        db_file=path.name if path else db_name,
        alias=db.alias_for(path) if path else db_name,
        db_title=meta.get("title") or "",
        table=tname or table,
        description=tmeta.get("description") or "",
        error=err or "",
        page_sizes=list(PAGE_SIZES),
    )


def _like(text: str) -> str:
    """LIKE のワイルドカードを打ち消して、入力された文字そのものを探す。"""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _filters_sql(raw: str, cols: list[str]) -> tuple:
    """列ごとの絞り込み（Excelのフィルターに相当）を WHERE 句にする。

    raw は画面から来るJSON:
      {"店舗コード": {"values": ["S01", null]},        … 選んだ値だけ（null は NULL 行）
       "売上金額":   {"op": ">=", "value": "100000"},  … 数の比較
       "顧客名":     {"op": "contains", "value": "商事"}}
    列名は実在するものだけを通し、値は必ずプレースホルダで渡す。
    """
    try:
        spec = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return "", [], {}
    if not isinstance(spec, dict):
        return "", [], {}

    OPS = {"=": "=", "!=": "<>", ">": ">", ">=": ">=", "<": "<", "<=": "<="}
    clauses, params, used = [], [], {}
    for col, f in spec.items():
        if col not in cols or not isinstance(f, dict):
            continue
        q = _qi(col)
        vals = f.get("values")
        if isinstance(vals, list) and vals:
            # 値の選択。NULL は IN で拾えないので別に足す
            plain = [v for v in vals if v is not None]
            parts = []
            if plain:
                parts.append(f"CAST({q} AS TEXT) IN ({', '.join('?' for _ in plain)})")
                params.extend(str(v) for v in plain)
            if any(v is None for v in vals):
                parts.append(f"{q} IS NULL")
            if parts:
                clauses.append("(" + " OR ".join(parts) + ")")
                used[col] = f
            continue
        op, value = str(f.get("op") or ""), f.get("value")
        if op in ("contains", "not_contains") and str(value or "") != "":
            neg = "NOT " if op == "not_contains" else ""
            clauses.append(f"CAST({q} AS TEXT) {neg}LIKE ? ESCAPE '\\'")
            params.append(_like(str(value)))
            used[col] = f
        elif op in OPS and str(value or "") != "":
            # 数として比較できるなら数で、無理なら文字で比べる
            try:
                num = float(value)
                clauses.append(f"CAST({q} AS REAL) {OPS[op]} ?")
                params.append(num)
            except (TypeError, ValueError):
                clauses.append(f"CAST({q} AS TEXT) {OPS[op]} ?")
                params.append(str(value))
            used[col] = f
        elif op == "empty":
            clauses.append(f"({q} IS NULL OR CAST({q} AS TEXT) = '')")
            used[col] = f
        elif op == "not_empty":
            clauses.append(f"({q} IS NOT NULL AND CAST({q} AS TEXT) <> '')")
            used[col] = f
    return (" AND ".join(clauses), params, used)


@bp_table.get("/api/table/rows")
@login_required
def rows():
    """1ページぶんの行。offset/limit・絞り込み・並べ替えはすべてサーバ側で行う。"""
    path, table, err = _resolve(request.args.get("db") or "", request.args.get("table") or "")
    if err:
        return jsonify({"error": err}), 404

    try:
        offset = max(0, int(request.args.get("offset") or 0))
        limit = int(request.args.get("limit") or 100)
    except ValueError:
        return jsonify({"error": "表示位置の指定が正しくありません。"}), 400
    limit = max(1, min(MAX_LIMIT, limit))
    q = (request.args.get("q") or "").strip()
    sort = request.args.get("sort") or ""
    desc = (request.args.get("dir") or "asc").lower() == "desc"

    conn = db.connect_ro(path)
    try:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({_qi(table)})")]
        if sort and sort not in cols:        # 実在しない列名は SQL に入れない
            sort = ""
        conds, params = [], []
        if q:
            # 全列を文字として見て部分一致。数値列も CAST して同じ扱いにする
            conds.append("(" + " OR ".join(
                f"CAST({_qi(c)} AS TEXT) LIKE ? ESCAPE '\\'" for c in cols) + ")")
            params.extend([_like(q)] * len(cols))
        fsql, fparams, used = _filters_sql(request.args.get("filters") or "", cols)
        if fsql:
            conds.append(fsql)
            params.extend(fparams)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""

        total = conn.execute(f"SELECT COUNT(*) FROM {_qi(table)}").fetchone()[0]
        matched = (conn.execute(f"SELECT COUNT(*) FROM {_qi(table)}{where}", params).fetchone()[0]
                   if where else total)
        order = f" ORDER BY {_qi(sort)} {'DESC' if desc else 'ASC'}" if sort else ""
        cur = conn.execute(
            f"SELECT * FROM {_qi(table)}{where}{order} LIMIT ? OFFSET ?", [*params, limit, offset])
        data = [list(r) for r in cur.fetchall()]
    except Exception as e:                   # 壊れたDB・読めないテーブルでも画面は保つ
        return jsonify({"error": f"読み取りに失敗しました: {e}"}), 400
    finally:
        conn.close()

    return jsonify({"ok": True, "columns": cols, "rows": jsonable(data),
                    "total": total, "matched": matched,
                    "offset": offset, "limit": limit,
                    "sort": sort, "dir": "desc" if desc else "asc",
                    "filters": used})


@bp_table.get("/api/table/values")
@login_required
def values():
    """1列の値の一覧（Excelのフィルターで出る候補）。多い順に返す。

    種類が多すぎる列（IDなど）は全部返しても選べないので、上限で切って
    「絞り込んで探す」に誘導する（truncated で画面に伝える）。
    """
    path, table, err = _resolve(request.args.get("db") or "", request.args.get("table") or "")
    if err:
        return jsonify({"error": err}), 404
    column = request.args.get("column") or ""
    q = (request.args.get("q") or "").strip()
    limit = 300

    conn = db.connect_ro(path)
    try:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({_qi(table)})")]
        if column not in cols:
            return jsonify({"error": f"列 '{column}' がありません。"}), 404
        c = _qi(column)
        where, params = "", []
        if q:
            where = f" WHERE CAST({c} AS TEXT) LIKE ? ESCAPE '\\'"
            params = [_like(q)]
        kinds = conn.execute(f"SELECT COUNT(DISTINCT {c}) FROM {_qi(table)}").fetchone()[0]
        cur = conn.execute(
            f"SELECT {c} AS v, COUNT(*) AS n FROM {_qi(table)}{where} "
            f"GROUP BY v ORDER BY n DESC, v LIMIT ?", [*params, limit + 1])
        rows_ = cur.fetchall()
    except Exception as e:
        return jsonify({"error": f"値を読めませんでした: {e}"}), 400
    finally:
        conn.close()

    truncated = len(rows_) > limit
    return jsonify({"ok": True, "column": column, "kinds": kinds, "truncated": truncated,
                    "values": [{"value": jsonable(v), "count": n} for v, n in rows_[:limit]]})


# ==========================================================================
# ===== 元 web/api_bp.py
# 細々したAPI: 生成ファイルのダウンロードと plotly.js の配信。
# ==========================================================================
from pathlib import Path

from flask import Blueprint, Response, abort, g, send_file


bp_api = Blueprint("api", __name__)


@bp_api.get("/api/file/<token>")
@login_required
def download(token: str):
    item = filestore.get(token, g.user.username)
    if item is None:
        abort(404)
    from io import BytesIO
    return send_file(BytesIO(item["data"]), mimetype=item["mime"],
                     as_attachment=True, download_name=item["filename"])


@bp_api.get("/vendor/plotly.min.js")
def plotly_js():
    """plotly パッケージ同梱のJSをそのまま配る（CDNに出ない・オフラインで動く）。"""
    import plotly
    p = Path(plotly.__file__).parent / "package_data" / "plotly.min.js"
    if not p.exists():
        abort(404)
    return Response(p.read_bytes(), mimetype="application/javascript",
                    headers={"Cache-Control": "public, max-age=604800"})


# ==========================================================================
# ===== 元 web/__init__.py
# Flask アプリ本体（アプリファクトリ）。
#
# 画面まわりだけをここに置き、業務ロジックは既存モジュールをそのまま使う。
#   db / catalog / llm / tools / custom_tools / charts / exports / excel / analysis
#   auth / chats / importer / jobs / scheduler
#
# Streamlit 版との違いは「状態の持ち方」だけ。
#   Streamlit: st.session_state（プロセス内）
#   Flask    : サーバ側セッション + 会話の実体は chats.py（ファイル）
# ==========================================================================
import os
import secrets

from flask import Flask

import auth
import config
import scheduler

_SECRET_FILE = config.BASE_DIR / ".flask_secret"


def _secret_key() -> str:
    """セッション署名鍵。再起動でログアウトさせないためファイルに保持する。"""
    env = os.getenv("FLASK_SECRET_KEY", "").strip()
    if env:
        return env
    if _SECRET_FILE.exists():
        return _SECRET_FILE.read_text(encoding="utf-8").strip()
    key = secrets.token_urlsafe(48)
    _SECRET_FILE.write_text(key, encoding="utf-8")
    try:
        os.chmod(_SECRET_FILE, 0o600)
    except OSError:
        pass
    return key


def _warn_if_no_admin() -> None:
    """管理者が1人も居ない設定なら、起動時に知らせる。

    認証APIがグループを返さない構成では、管理者になれるのは env の ADMIN_PASS で
    入る admin だけになる。これを設定し忘れると、カタログ・取り込み・モデル・メールの
    画面に誰も入れないまま動き続ける。気づけるのは「設定を直したいとき」なので、
    起動時に言う。
    """
    if auth.admin_enabled():
        return
    try:
        provider = auth.get_provider()
    except auth.AuthError:
        return
    if provider.name == "local":
        has_admin = any(config.AUTH_ADMIN_GROUP in (u.get("groups") or [])
                        for u in (auth.load_users_file().get("users") or []))
        if has_admin:
            return
        how = "manage_users.py add <ユーザー名> --admin で管理者を作るか、"
    else:
        # 認証APIがグループを返さないなら、ここに来た時点で管理者は現れない
        how = f"認証APIが '{config.AUTH_ADMIN_GROUP}' グループを返すようにするか、"
    print(f"[auth] 警告: 管理者が1人も居ません。{how}"
          "env の ADMIN_PASS を設定してください。"
          "このままではデータカタログ・データ取り込み・モデル設定・メール設定を"
          "誰も開けません（チャットは使えます）。")


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.update(
        SECRET_KEY=_secret_key(),
        MAX_CONTENT_LENGTH=64 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        JSON_AS_ASCII=False,
        # テンプレートだけ自動リロードするとPython側と食い違うので、
        # 開発時も両方まとめて再起動する運用にする（既定はOFF）
        TEMPLATES_AUTO_RELOAD=bool(os.getenv("FLASK_DEBUG")),
    )

    app.register_blueprint(bp_auth)
    app.register_blueprint(bp_chat)
    app.register_blueprint(bp_catalog)
    app.register_blueprint(bp_import)
    app.register_blueprint(bp_mail)
    app.register_blueprint(bp_models)
    app.register_blueprint(bp_table)
    app.register_blueprint(bp_api)

    app.before_request(load_user_into_context)
    app.context_processor(inject_globals)

    _warn_if_no_admin()

    @app.after_request
    def _no_html_cache(res):
        """画面のHTMLはキャッシュさせない。

        中身はログイン中の人・選択中のDB・カタログの現状で毎回変わるので、
        取っておいても正しくない。既定ではキャッシュ指定が付かず、ブラウザが
        独自の判断で古い画面を出すため、直したはずの表示が変わらないことがある。
        静的ファイル（CSS/JS）は ETag で更新を見ているのでそのまま。
        """
        if res.mimetype == "text/html":
            res.headers["Cache-Control"] = "no-store"
        return res

    # 定期取り込みの裏スレッド。Streamlit版と同じく cron 不要。
    # Flask では起動時に1回通るので、誰かがページを開くのを待たずに動き出す。
    scheduler.start()
    return app
