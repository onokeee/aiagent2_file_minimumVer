"""aiagent の Python モジュールを、挙動を変えずに数ファイルへ統合する。

やっていること:
  1. 同じファイルに入れると衝突する名前を、モジュールごとに付け替える
  2. その名前を外から呼んでいる箇所（mod.name）を追随して書き換える
  3. 統合前のモジュール名でも import できるよう sys.modules に登録する
     （= 残り大多数の呼び出し箇所は一切書き換えない）
  4. 変換のたびにASTを突き合わせ、意図した改名以外が起きていないか確かめる

ファイルの前半が道具（名前の付け替えと検証）、後半が「何をどうまとめるか」の一覧。
"""
import ast
import io
import pathlib
import shutil
import tokenize

def bcol(line: str, col: int) -> int:
    """ASTの桁位置（UTF-8のバイト数）を、Python文字列の文字位置に直す。

    日本語を含む行では両者がずれる。ここを直さないと、コメントや文字列に
    日本語が入っている行で置換位置が右へずれる。
    """
    if col <= 0:
        return 0
    return len(line.encode("utf-8")[:col].decode("utf-8", "ignore"))


def _rename_in_fstring(text: str, mapping: dict) -> str:
    """f文字列の { } の中（＝式の部分）だけ名前を付け替える。

    Python 3.9 の tokenize は f文字列を丸ごと1つの文字列として扱うので、
    中の式は NAME トークンとして出てこない。ここで自前で見る。
    """
    out, i, depth, n = [], 0, 0, len(text)
    while i < n:
        c = text[i]
        if depth == 0:
            if c in "{}" and i + 1 < n and text[i + 1] == c:   # {{ }} は文字そのもの
                out.append(c * 2)
                i += 2
                continue
            if c == "{":
                depth = 1
            out.append(c)
            i += 1
            continue
        # ここから式の中
        if c in "\"'":                      # 式の中の文字列リテラルは触らない
            q = c
            j = i + 1
            while j < n and text[j] != q:
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            prev = text[i - 1] if i else ""
            out.append(mapping[word] if word in mapping and prev != "." else word)
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def rename_names(src: str, mapping: dict) -> str:
    """モジュール直下の名前とその参照だけを付け替える。

    ASTで位置を取るので、属性名（obj.name）・キーワード引数名（f(name=...)）・
    import の別名・文字列の中身には手を出さない。そこを間違えると、
    テンプレートに渡す変数名やAPIの引数名が静かに変わってしまう。
    f文字列の中だけはASTの位置が当てにならないので、字句側で見る。
    """
    if not mapping:
        return src
    import re as _re
    lines = src.splitlines(keepends=True)
    edits = []

    def add(row, c0, c1, new):
        edits.append((row, c0, c1, new))

    def walk(node, in_fstring):
        if isinstance(node, ast.JoinedStr):
            in_fstring = True          # 中身は字句側で処理する
        if not in_fstring:
            if isinstance(node, ast.Name) and node.id in mapping:
                ln = lines[node.lineno - 1]
                add(node.lineno, bcol(ln, node.col_offset),
                    bcol(ln, node.end_col_offset), mapping[node.id])
            elif (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                    ast.ClassDef)) and node.name in mapping):
                pat = _re.compile(r"\b(?:class|def)\s+(" +
                                  _re.escape(node.name) + r")\b")
                m = pat.search(lines[node.lineno - 1])
                if m:
                    add(node.lineno, m.start(1), m.end(1), mapping[node.name])
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                for nm in node.names:
                    if nm not in mapping:
                        continue
                    pat = _re.compile(r"\b" + _re.escape(nm) + r"\b")
                    for row in range(node.lineno,
                                     (node.end_lineno or node.lineno) + 1):
                        for m in pat.finditer(lines[row - 1]):
                            add(row, m.start(), m.end(), mapping[nm])
        for child in ast.iter_child_nodes(node):
            walk(child, in_fstring)

    walk(ast.parse(src), False)

    # f文字列の中（ASTの位置が信用できないので字句側で見る）
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.STRING and tok.start[0] == tok.end[0]:
            body = tok.string.lstrip("rRbBuUfF")
            prefix = tok.string[:len(tok.string) - len(body)]
            if "f" in prefix.lower():
                new = _rename_in_fstring(tok.string, mapping)
                if new != tok.string:
                    add(tok.start[0], tok.start[1], tok.end[1], new)

    for row, c0, c1, new in sorted(set(edits), reverse=True):
        ln = lines[row - 1]
        lines[row - 1] = ln[:c0] + new + ln[c1:]
    return "".join(lines)


# --------------------------------------------------------------------------
# 2) 呼び出し側の書き換え  mod.old -> mod.new （属性アクセスのみ・AST位置で特定）
# --------------------------------------------------------------------------
def rewrite_attrs(src: str, rules: dict) -> tuple:
    """rules: {(module_name, old_attr): new_attr}"""
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    edits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
            key = (node.value.id, node.attr)
            if key in rules:
                # 属性名は value の終わり以降にある。行内で該当名を探す。
                row = node.value.end_lineno
                ln = lines[row - 1]
                start = ln.find(node.attr, bcol(ln, node.value.end_col_offset))
                if start >= 0:
                    edits.append((row, start, start + len(node.attr), rules[key]))
    for row, c0, c1, new in sorted(set(edits), reverse=True):
        ln = lines[row - 1]
        lines[row - 1] = ln[:c0] + new + ln[c1:]
    return "".join(lines), len(edits)


# --------------------------------------------------------------------------
# 3) 検証: 元ASTと変換後ASTを並べて歩き、変わってよい場所だけが変わったか見る
# --------------------------------------------------------------------------
class RenameMismatch(Exception):
    pass


def assert_only_renamed(old_src: str, new_src: str, mapping: dict, where: str):
    """mapping に沿った Name / def / class / global の改名だけが起きたか確認する。

    属性名・キーワード引数名・文字列リテラルが変わっていたら、そこで落とす
    （= 意図しない書き換えを取りこぼさない）。
    """
    a, b = ast.parse(old_src), ast.parse(new_src)

    def conv(name):
        return mapping.get(name, name)

    def walk(x, y, path):
        if type(x) is not type(y):
            raise RenameMismatch(f"{where}: 構造が違う {path}: "
                                 f"{type(x).__name__} vs {type(y).__name__}")
        if isinstance(x, ast.AST):
            for f in x._fields:
                vx, vy = getattr(x, f, None), getattr(y, f, None)
                p = f"{path}.{type(x).__name__}.{f}"
                if isinstance(x, ast.Name) and f == "id":
                    if conv(vx) != vy:
                        raise RenameMismatch(f"{where}: Name {vx!r}->{vy!r} at {p}")
                elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef,
                                    ast.ClassDef)) and f == "name":
                    if conv(vx) != vy:
                        raise RenameMismatch(f"{where}: def {vx!r}->{vy!r} at {p}")
                elif isinstance(x, (ast.Global, ast.Nonlocal)) and f == "names":
                    if [conv(n) for n in vx] != list(vy):
                        raise RenameMismatch(f"{where}: global {vx}->{vy} at {p}")
                elif isinstance(x, ast.arg) and f == "arg":
                    # 引数名は変えない（呼ぶ側が name=... で渡しているかもしれない）
                    if vx != vy:
                        raise RenameMismatch(f"{where}: arg {vx!r}->{vy!r} at {p}")
                elif isinstance(x, ast.alias) and f in ("name", "asname"):
                    # import で持ち込む名前も変えない（元のモジュールに無い名前になる）
                    if vx != vy:
                        raise RenameMismatch(f"{where}: import {vx!r}->{vy!r} at {p}")
                else:
                    walk(vx, vy, p)
        elif isinstance(x, list):
            if len(x) != len(y):
                raise RenameMismatch(f"{where}: 要素数が違う {path}: "
                                     f"{len(x)} vs {len(y)}")
            for i, (ex, ey) in enumerate(zip(x, y)):
                walk(ex, ey, f"{path}[{i}]")
        else:
            # 属性名(str)・キーワード引数名・文字列/数値リテラルはここに来る。
            # 一切変わっていないことを求める。
            if x != y:
                raise RenameMismatch(f"{where}: 変えてはいけない値が変わった "
                                     f"{path}: {x!r} -> {y!r}")

    walk(a, b, "")


def assert_attr_rewrite(old_src: str, new_src: str, rules: dict, where: str):
    """mod.old -> mod.new の書き換えだけが起きたか確認する。"""
    a, b = ast.parse(old_src), ast.parse(new_src)

    def walk(x, y, path):
        if type(x) is not type(y):
            raise RenameMismatch(f"{where}: 構造が違う {path}")
        if isinstance(x, ast.AST):
            for f in x._fields:
                vx, vy = getattr(x, f, None), getattr(y, f, None)
                p = f"{path}.{type(x).__name__}.{f}"
                if isinstance(x, ast.Attribute) and f == "attr":
                    mod = x.value.id if isinstance(x.value, ast.Name) else None
                    want = rules.get((mod, vx), vx)
                    if want != vy:
                        raise RenameMismatch(f"{where}: attr {mod}.{vx}->{vy} at {p}")
                else:
                    walk(vx, vy, p)
        elif isinstance(x, list):
            if len(x) != len(y):
                raise RenameMismatch(f"{where}: 要素数が違う {path}")
            for i, (ex, ey) in enumerate(zip(x, y)):
                walk(ex, ey, f"{path}[{i}]")
        else:
            if x != y:
                raise RenameMismatch(f"{where}: 値が変わった {path}: {x!r} -> {y!r}")

    walk(a, b, "")


# --------------------------------------------------------------------------
# 4) 行の削除（相対import・future import など、統合で不要になるもの）
# --------------------------------------------------------------------------
def drop_statements(src: str, predicate) -> str:
    """トップレベル文のうち predicate(node) が真のものを、行ごと消す。"""
    tree = ast.parse(src)
    kill = set()
    for node in tree.body:
        if predicate(node):
            for ln in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                kill.add(ln)
    return "".join(ln for i, ln in enumerate(src.splitlines(keepends=True), 1)
                   if i not in kill)


def strip_module_docstring(src: str) -> tuple:
    """先頭のモジュールdocstringを外し、(本体, docstring本文) を返す。"""
    tree = ast.parse(src)
    if (tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)):
        n = tree.body[0]
        doc = n.value.value
        lines = src.splitlines(keepends=True)
        rest = "".join(lines[(n.end_lineno or n.lineno):])
        return rest, doc
    return src, ""


SRC = pathlib.Path(r"C:\Users\onoke\Desktop\aiagent")
DST = pathlib.Path(r"C:\Users\onoke\Desktop\aiagent_file_minimum")

# --------------------------------------------------------------------------
# 名前の付け替え表  (元モジュール, 元の名前) -> 新しい名前
# --------------------------------------------------------------------------
RENAMES = {
    # ---- core_data ----
    ("catalog_history", "KINDS"): "CATALOG_CHANGE_KINDS",
    ("history", "KINDS"): "IMPORT_RECORD_KINDS",
    ("catalog_history", "add"): "add_catalog_change",
    ("history", "add"): "add_import_record",
    ("catalog_history", "recent"): "recent_catalog_changes",
    ("history", "recent"): "recent_import_records",
    ("catalog_history", "_lock"): "_catalog_history_lock",
    ("history", "_lock"): "_history_lock",
    ("models", "_lock"): "_models_lock",
    ("prefs", "_lock"): "_prefs_lock",
    ("catalog_history", "_path"): "_catalog_history_path",
    ("history", "_path"): "_history_path",
    ("prefs", "_path"): "_prefs_path",
    ("models", "_cache"): "_models_cache",
    ("verify", "_cache"): "_verify_cache",
    # models の catalog() は、同じファイルに入る `import catalog`（カタログ本体）と
    # 名前がぶつかる。後から来る import に潰されるので別名にする。
    ("models", "catalog"): "model_catalog",

    # ---- core_report ----
    ("analysis", "_clean"): "_clean",              # 据え置き（pptx側を改名）
    ("pptx_report", "_clean"): "_pptx_clean",
    ("business", "_numeric"): "_business_numeric",  # charts 側を据え置き
    # business が advanced から持ち込む _table は docx_report._table と別物なので分ける
    ("business", "_table"): "_advanced_table",
    ("excel", "CHART_TYPES"): "EXCEL_CHART_TYPES",
    ("pptx_report", "CHART_TYPES"): "PPTX_CHART_TYPES",
    ("excel", "build"): "build_excel",
    ("docx_report", "build"): "build_docx",
    ("pptx_report", "build"): "build_pptx",
    ("excel", "safe_filename"): "excel_safe_filename",
    ("docx_report", "safe_filename"): "docx_safe_filename",
    ("pptx_report", "safe_filename"): "pptx_safe_filename",
    ("docx_report", "outline"): "outline_docx",
    ("pptx_report", "outline"): "outline_pptx",
    ("docx_report", "ReportError"): "DocxReportError",
    ("pptx_report", "ReportError"): "PptxReportError",
    ("docx_report", "MAX_TABLE_ROWS"): "DOCX_MAX_TABLE_ROWS",
    ("pptx_report", "MAX_TABLE_ROWS"): "PPTX_MAX_TABLE_ROWS",
    ("docx_report", "ACCENT"): "DOCX_ACCENT",
    ("pptx_report", "ACCENT"): "PPTX_ACCENT",
    ("docx_report", "BAND"): "DOCX_BAND",
    ("pptx_report", "BAND"): "PPTX_BAND",
    ("docx_report", "HILITE"): "DOCX_HILITE",
    ("pptx_report", "HILITE"): "PPTX_HILITE",
    ("docx_report", "INK"): "DOCX_INK",
    ("pptx_report", "INK"): "PPTX_INK",
    ("docx_report", "MUTED"): "DOCX_MUTED",
    ("pptx_report", "MUTED"): "PPTX_MUTED",
    ("docx_report", "NAVY"): "DOCX_NAVY",
    ("pptx_report", "NAVY"): "PPTX_NAVY",
    ("docx_report", "_callout"): "_docx_callout",
    ("pptx_report", "_callout"): "_pptx_callout",
    ("docx_report", "_fmt"): "_docx_fmt",
    ("pptx_report", "_fmt"): "_pptx_fmt",
    ("pptx_report", "_BUILDERS"): "_PPTX_BUILDERS",
    ("pptx_report", "_box"): "_pptx_box",
    # qn / Pt / RGBColor は docx と pptx で「同じ名前・別のクラス」。
    # 1ファイルに入れると後から読んだ pptx 側が docx 側を上書きしてしまい、
    # Word 文書の作成が PowerPoint 用のクラスで動いてしまう。pptx 側を分ける。
    ("pptx_report", "qn"): "_pptx_qn",
    ("pptx_report", "Pt"): "_pptx_Pt",
    ("pptx_report", "RGBColor"): "_pptx_RGBColor",

    # ---- core_ingest ----
    ("jobs", "_lock"): "_jobs_lock",
    ("mailer", "_lock"): "_mailer_lock",
    ("mailer", "_log"): "_mailer_log",
    ("scheduler", "_log"): "_scheduler_log",
    ("cleanup", "_walk"): "_cleanup_walk",
    ("importer", "_walk"): "_importer_walk",
    ("custom_tools", "safe_name"): "custom_tool_safe_name",
    ("custom_tools", "validate"): "validate_custom_tool",
    ("jobs", "validate"): "validate_job",
    ("mailer", "status"): "mail_status",
    ("scheduler", "status"): "scheduler_status",

    # ---- 業務ロジックを core.py 1本にしたときに新たにぶつかるもの ----
    ("exports", "DELIMITERS"): "EXPORT_DELIMITERS",   # importer.DELIMITERS を据え置き
    ("catalog_history", "summarize"): "summarize_catalog_changes",  # cleanup 側据え置き
    ("sqlusage", "_RESERVED"): "_sqlusage_RESERVED",
    ("pptx_report", "_blank"): "_pptx_blank",
    ("importer", "_qi"): "_importer_qi",
    ("figures", "available"): "_figures_available",   # models.available を据え置き
    # usage の3つは、他所から入ってくる同名のものに潰される側:
    #   _dt  … excel/exports の `import datetime as _dt`
    #   _out … business が advanced から持ち込む _out
    #   _table … docx_report._table / tools/files._table
    ("usage", "_dt"): "_usage_dt",
    ("usage", "_out"): "_usage_out",
    ("usage", "_table"): "_usage_table",
    ("tools/files", "_table"): "_files_table",
}
for _m in ("business", "files", "mail", "query", "reports", "stats", "usage"):
    RENAMES[(f"tools/{_m}", "HANDLERS")] = f"HANDLERS_{_m}"
    RENAMES[(f"tools/{_m}", "SQL_TOOLS")] = f"SQL_TOOLS_{_m}"
    RENAMES[(f"tools/{_m}", "ADMIN_TOOLS")] = f"ADMIN_TOOLS_{_m}"
for _b in ("api", "auth", "catalog", "chat", "import", "mail", "models", "table"):
    RENAMES[(f"web/{_b}_bp", "bp")] = f"bp_{_b}"
# 画面の関数名を変えるところ。url_for の名前（エンドポイント）は元のまま固定する。
WEB_VIEW_RENAMES = {(f"web/{_b}_bp", "index"): f"{_b}_index"
                    for _b in ("catalog", "chat", "import", "mail", "models",
                               "table")}
# chat の history() は、同じファイルに入る `import history`（更新履歴）と
# 名前がぶつかる。ルートは登録済みなので実害は出ないが、紛らわしいので分ける。
WEB_VIEW_RENAMES[("web/chat_bp", "history")] = "chat_history"
RENAMES.update(WEB_VIEW_RENAMES)

# 外から mod.name で呼んでいる箇所の書き換え
ATTR_REWRITES = {}
for (mod, old), new in RENAMES.items():
    if mod.startswith(("tools/", "web/")):
        continue
    ATTR_REWRITES[(mod, old)] = new

# --------------------------------------------------------------------------
# 個別の手当て（改名だけでは済まないところ）。改名を当てた後のテキストに対して行う。
# --------------------------------------------------------------------------
PATCHES = {
    # docx 側と名前がぶつかる3つを、取り込むときに別名にする
    "pptx_report": [
        ("from pptx.dml.color import RGBColor",
         "from pptx.dml.color import RGBColor as _pptx_RGBColor"),
        ("from pptx.oxml.ns import qn",
         "from pptx.oxml.ns import qn as _pptx_qn"),
        ("from pptx.util import Emu, Inches, Pt",
         "from pptx.util import Emu, Inches, Pt as _pptx_Pt"),
    ],
    # advanced の _table は docx_report の _table と名前が重なるので別名で取り込む
    "business": [(
        "from advanced import AnalysisError, _clean, _df, _out, _table",
        "from advanced import AnalysisError, _clean, _df, _out, "
        "_table as _advanced_table",
    )],
    # 統合前は各サブモジュールが自分の HANDLERS を持っていた。1ファイルになったので
    # モジュールごとに名前を分け、同じ並び順で集める（後勝ちの順序も元のまま）。
    "tools/__init__": [(
        """_MODULES = (query, stats, reports, mail, business, files, usage)

# ツール名 -> 実処理。各モジュールが自分のぶんを申告する。
_HANDLERS = {name: fn for m in _MODULES for name, fn in m.HANDLERS.items()}

# SQLを受け取る組み込みツール（実行前プレビュー表示の対象）
SQL_TOOLS = {name for m in _MODULES for name in m.SQL_TOOLS}

# 管理者にだけ渡すツール。画面側で管理者専用になっているものは、
# AI経由でも同じ制限にしないと抜け道になる（取り込み元フォルダの中身など）。
ADMIN_TOOLS = {name for m in _MODULES for name in getattr(m, "ADMIN_TOOLS", ())}""",
        """# (実処理, SQLを受け取るもの, 管理者専用) をモジュールごとに並べる。
# 並び順は統合前の (query, stats, reports, mail, business, files, usage) のまま。
# 名前が重なったときにどちらが残るかを変えないため、順序は動かさないこと。
_MODULES = (
    (HANDLERS_query, SQL_TOOLS_query, ()),
    (HANDLERS_stats, SQL_TOOLS_stats, ()),
    (HANDLERS_reports, SQL_TOOLS_reports, ()),
    (HANDLERS_mail, SQL_TOOLS_mail, ()),
    (HANDLERS_business, SQL_TOOLS_business, ()),
    (HANDLERS_files, SQL_TOOLS_files, ADMIN_TOOLS_files),
    (HANDLERS_usage, SQL_TOOLS_usage, ADMIN_TOOLS_usage),
)

# ツール名 -> 実処理。各モジュールが自分のぶんを申告する。
_HANDLERS = {name: fn for m in _MODULES for name, fn in m[0].items()}

# SQLを受け取る組み込みツール（実行前プレビュー表示の対象）
SQL_TOOLS = {name for m in _MODULES for name in m[1]}

# 管理者にだけ渡すツール。画面側で管理者専用になっているものは、
# AI経由でも同じ制限にしないと抜け道になる（取り込み元フォルダの中身など）。
ADMIN_TOOLS = {name for m in _MODULES for name in m[2]}""",
    )],
    # 画面は同じファイルの中にあるので、取り込み直さずそのまま登録する。
    # 登録の順番は統合前と同じ（ルートの解決順が変わらないように）。
    "web/__init__": [(
        """    from . import (api_bp, auth_bp, catalog_bp, chat_bp, import_bp, mail_bp,
                   models_bp, table_bp)
    app.register_blueprint(auth_bp.bp)
    app.register_blueprint(chat_bp.bp)
    app.register_blueprint(catalog_bp.bp)
    app.register_blueprint(import_bp.bp)
    app.register_blueprint(mail_bp.bp)
    app.register_blueprint(models_bp.bp)
    app.register_blueprint(table_bp.bp)
    app.register_blueprint(api_bp.bp)

    from .helpers import inject_globals, load_user_into_context
    app.before_request(load_user_into_context)""",
        """    app.register_blueprint(bp_auth)
    app.register_blueprint(bp_chat)
    app.register_blueprint(bp_catalog)
    app.register_blueprint(bp_import)
    app.register_blueprint(bp_mail)
    app.register_blueprint(bp_models)
    app.register_blueprint(bp_table)
    app.register_blueprint(bp_api)

    app.before_request(load_user_into_context)""",
    )],
    # tools はパッケージから1ファイルになったので、取り出し口も1つになる
    "custom_tools": [(
        "    from tools.schemas import BUILTIN_TOOLS",
        "    from tools import BUILTIN_TOOLS",
    )],
}


# --------------------------------------------------------------------------
# 統合ファイルの構成
# --------------------------------------------------------------------------
#: core.py に入れる順番。依存の順に並べる（先に読まれた側が後から使われる）。
CORE_MEMBERS = (
    # データ層: DB・カタログ・利用者ごとの状態
    ["db", "filecheck", "chats", "history", "catalog_history",
     "prefs", "models", "catalog", "verify", "sqlusage", "usage"]
    # 出力層: グラフ・Excel・CSV・PowerPoint・Word・業務分析
    + ["exports", "excel", "charts", "figures",
       "docx_report", "pptx_report", "business"]
    # 取り込み・定期実行・メール・ユーザー定義ツール
    + ["importer", "jobs", "scheduler", "cleanup", "mailer", "custom_tools"]
    # LLMに渡すツール（宣言と実処理）
    + ["tools/results", "tools/common", "tools/schemas", "tools/business",
       "tools/files", "tools/mail", "tools/query", "tools/reports",
       "tools/stats", "tools/usage", "tools/__init__"]
    # LLMクライアント
    + ["llm"]
)

GROUPS = [
    dict(
        out="core.py",
        why="業務ロジック一式。画面(web.py)と設定(config.py)と統計の計算(advanced.py)"
            "以外は全部ここ（元: 35モジュール）",
        members=CORE_MEMBERS,
        head_imports=["advanced"],
        self_alias=["results", "_results"],
    ),
    dict(
        out="web.py",
        why="Flaskアプリ本体と全画面（元: web/ の11モジュール）",
        members=["web/filestore", "web/helpers", "web/auth_bp", "web/chat_bp",
                 "web/catalog_bp", "web/import_bp", "web/mail_bp",
                 "web/models_bp", "web/table_bp", "web/api_bp", "web/__init__"],
        head_imports=["core"],
        self_alias=["filestore"],
    ),
]
# 統合先ファイル -> 元モジュール名（sys.modules に登録する別名）。
# これがあるので、呼び出し側は統合前のまま `import db` / `db.run_select(...)` と書ける。
ALIASES = {
    "core.py": [m for m in CORE_MEMBERS if not m.startswith("tools/")]
               + ["tools"],
}
# analysis.py は advanced.py の末尾へ入れる（build_advanced 参照）
MERGED_AWAY = {m for g in GROUPS for m in g["members"]} | {"analysis"}


# --------------------------------------------------------------------------
def read(mod):
    return (SRC / f"{mod}.py").read_text(encoding="utf-8")


def pin_endpoint(src, funcname, endpoint):
    """ルートのエンドポイント名を明示する（関数名を変えても url_for を保つ）。"""
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == funcname:
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr in ("route", "get", "post", "put", "delete")):
                    row = dec.end_lineno
                    ln = lines[row - 1]
                    col = bcol(ln, dec.end_col_offset)
                    assert ln[col - 1] == ")", f"{funcname}: 想定外のデコレータ形"
                    lines[row - 1] = (ln[:col - 1] + f', endpoint="{endpoint}"'
                                      + ln[col - 1:])
                    return "".join(lines)
    raise SystemExit(f"pin_endpoint: {funcname} が見つからない")


def prepare(mod, group):
    """1モジュール分のソースを、統合先に入れられる形にする。"""
    src = read(mod)
    orig = src

    # 1) 名前の付け替え
    mapping = {old: new for (m, old), new in RENAMES.items() if m == mod}
    if mapping:
        src = rename_names(src, mapping)
        assert_only_renamed(orig, src, mapping, mod)

    # 2) 個別の手当て
    for old, new in PATCHES.get(mod, []):
        if old not in src:
            raise SystemExit(f"{mod}: 当てるべき箇所が見つからない:\n{old[:120]}")
        src = src.replace(old, new, 1)

    # 3) 画面の関数名を変えたところは、url_for の名前を元のまま固定する
    for (m, old), new in WEB_VIEW_RENAMES.items():
        if m == mod:
            src = pin_endpoint(src, new, old)

    # 3) 統合で不要になる文を消す
    #    - from __future__ import annotations（統合先の先頭にまとめる）
    #    - 相対import（同じファイルの中に入るため）
    def kill(node):
        if isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                return True
            if (node.level or 0) > 0:
                return True
        return False

    src = drop_statements(src, kill)

    # 4) モジュールdocstringは見出しコメントへ回す
    body, doc = strip_module_docstring(src)
    return body, doc


def build_group(group):
    out_name = group["out"]
    aliases = ALIASES.get(out_name, [])
    chunks = []
    for mod in group["members"]:
        body, doc = prepare(mod, group)
        title = f"{mod}.py"
        head = [f"# {'=' * 74}",
                f"# ===== 元 {title}",
                ]
        for line in (doc or "").strip().splitlines():
            head.append(f"# {line}".rstrip())
        head.append(f"# {'=' * 74}")
        chunks.append("\n".join(head) + "\n" + body.lstrip("\n"))

    header = [f'"""{out_name} — {group["why"]}。', ""]
    header.append("元は以下のファイルに分かれていた。中身は変えずに1つにまとめている:")
    for mod in group["members"]:
        header.append(f"  {mod}.py")
    header.append("")
    if aliases:
        header.append("統合前と同じく `import db` / `db.run_select(...)` と書けるよう、")
        header.append("このファイルを元のモジュール名でも参照できるよう登録している。")
        header.append("そのため呼び出し側のコードは統合前のまま動く。")
    header.append('"""')
    header.append("from __future__ import annotations")
    header.append("")
    if aliases or group.get("self_alias"):
        header.append("import sys as _sys")
        header.append("")
    if aliases:
        header.append("# 元のモジュール名でも import できるようにする（呼び出し側を変えないため）")
        header.append("for _alias in (" + ", ".join(f'"{a}"' for a in aliases) + "):")
        header.append("    _sys.modules[_alias] = _sys.modules[__name__]")
        header.append("del _alias")
        header.append("")
    for imp in group.get("head_imports", []):
        header.append(f"import {imp}  # noqa: F401  （統合前のモジュール名を登録させる）")
    if group.get("head_imports"):
        header.append("")
    for a in group.get("self_alias", []):
        header.append(f"{a} = _sys.modules[__name__]"
                      "  # 統合前の書き方をそのまま使えるようにする")
    if group.get("self_alias"):
        header.append("")

    text = "\n".join(header) + "\n\n" + "\n\n".join(chunks)
    return text


def build_advanced():
    """advanced.py = 元のまま + 末尾に analysis.py を足したもの。

    23行目の `from analysis import ...` を「同じ1行の」コメントに置き換えるので、
    24行目以降の行番号は1つも動かない。これは web.py の _tool_source() が
    inspect でこのファイルの行番号を読み、管理画面に advanced.py:NNN と
    表示しているため（統合前と同じ表示を保つ）。
    analysis の中身は関数の外からは使われていないので、末尾に置いても動く。
    """
    src = read("advanced")
    lines = src.splitlines(keepends=True)
    assert lines[22].startswith("from analysis import"), \
        f"advanced.py の23行目が想定と違う: {lines[22]!r}"
    lines[22] = ("# _clean / _df / _out / _to_numeric / numeric_columns は"
                 "このファイルの末尾（元 analysis.py）にある\n")

    body, doc = prepare("analysis", None)
    head = ["", "", f"# {'=' * 74}", "# ===== 元 analysis.py"]
    for line in (doc or "").strip().splitlines():
        head.append(f"# {line}".rstrip())
    head.append(f"# {'=' * 74}")
    tail = ("\n".join(head) + "\n" + body.lstrip("\n")
            + "\n\n# 統合前と同じく `import analysis` と書けるようにする。\n"
              "# （ここより上の行番号を動かさないよう、末尾に置いている）\n"
              "import sys as _sys\n"
              '_sys.modules["analysis"] = _sys.modules[__name__]\n')
    text = "".join(lines) + tail
    ast.parse(text)
    (DST / "advanced.py").write_text(text, encoding="utf-8")
    kept = len(src.splitlines())
    print(f"  advanced.py      {len(text.splitlines()):6d} 行  "
          f"<- 元 {kept} 行 + analysis.py（行番号は据え置き）")


def bootstrap_standalone():
    """単体で起動するファイル（CLI など）に、統合ファイルを先に読む1行を足す。

    `import jobs` のような統合前の書き方は、統合ファイルが一度読まれて
    sys.modules に名前が入ってから使える。web 経由なら先に読まれるが、
    `python refresh.py` のように直接叩く口では自分で読ませる必要がある。
    """
    owner = {n: out[:-3] for out, names in ALIASES.items() for n in names}
    outs = {g["out"] for g in GROUPS}
    for p in sorted(DST.glob("*.py")):
        if p.name in outs or p.name == "advanced.py":
            continue                     # advanced.py は1バイトも変えない
        src = p.read_text(encoding="utf-8")
        needed, first = [], None
        for node in ast.parse(src).body:
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif (isinstance(node, ast.ImportFrom) and not (node.level or 0)
                  and node.module):
                mods = [node.module.split(".")[0]]
            for m in mods:
                if m in owner:
                    if owner[m] not in needed:
                        needed.append(owner[m])
                    if first is None:
                        first = node.lineno
        if not needed:
            continue
        lines = src.splitlines(keepends=True)
        lines[first - 1:first - 1] = (
            ["# 統合ファイルを先に読む。これで統合前と同じ `import jobs` などが通る。\n"]
            + [f"import {m}  # noqa: F401\n" for m in needed] + ["\n"])
        p.write_text("".join(lines), encoding="utf-8")
        print(f"  起動口の手当て {p.name}: {', '.join(needed)} を先に読ませる")


def main():
    # --- 統合ファイルを書く ---
    for g in GROUPS:
        text = build_group(g)
        ast.parse(text)                       # 構文が壊れていないこと
        (DST / g["out"]).write_text(text, encoding="utf-8")
        print(f"  {g['out']:16s} {len(text.splitlines()):6d} 行  "
              f"<- {len(g['members'])} モジュール")
    build_advanced()

    # --- 呼び出し側（mod.name）の追随書き換え ---
    targets = [p for p in DST.rglob("*.py")
               if not any(x in p.parts for x in (".venv", "__pycache__"))]
    total = 0
    for p in targets:
        s = p.read_text(encoding="utf-8")
        try:
            new, n = rewrite_attrs(s, ATTR_REWRITES)
        except SyntaxError as e:
            raise SystemExit(f"{p}: {e}")
        if n:
            assert_attr_rewrite(s, new, ATTR_REWRITES, str(p))
            p.write_text(new, encoding="utf-8")
            total += n
            print(f"  書き換え {p.relative_to(DST)}: {n} 箇所")
    print(f"  呼び出し側の書き換え合計: {total} 箇所")

    # --- 統合済みの元ファイルを消す ---
    for mod in sorted(MERGED_AWAY):
        f = DST / f"{mod}.py"
        if f.exists():
            f.unlink()
    for d in ("tools", "web"):
        if (DST / d).exists():
            shutil.rmtree(DST / d)
    print(f"  削除: {len(MERGED_AWAY)} ファイル + tools/ web/ ディレクトリ")

    bootstrap_standalone()


if __name__ == "__main__":
    main()
