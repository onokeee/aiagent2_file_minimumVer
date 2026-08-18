"""このフォルダのファイルが、GitHub にあるものと同じか照合する。

GitHub の画面からコピーして持ってきたときに、
「途中で切れた」「行番号が混ざった」「文字コードが違う」「名前を間違えた」
といった取りこぼしが起きていないかを、まとめて確かめるための道具です。

使い方（run.py と同じ場所に置いて実行）:

    python check_files.py

ファイルを読むだけで、何も書き換えません。
改行コード（CRLF / LF）の違いは無視して、中身だけを見ます。

Linux では大文字・小文字が区別されるため、er.js を ER.js のような名前で
保存していると「ファイルが無い」と表示されます。
"""
import hashlib
import pathlib
import sys

# (ファイル名, 正しいバイト数, 中身のSHA-256 先頭16桁)
EXPECT = [
    ('.build/README.md', 2092, '5e39dcae4a81154e'),
    ('.build/build.py', 35991, '58891b1cfee1446f'),
    ('.claude/launch.json', 198, '12d5c2d023b29ed3'),
    ('.gitattributes', 77, '18939d565fd0629b'),
    ('.gitignore', 1223, '5671f4476af4b0aa'),
    ('advanced.py', 99389, '5d1ecf0d3e7d8cf8'),
    ('auth.py', 11789, 'a5740a8804115e89'),
    ('config.py', 22033, '8a2e8144f4d71bf2'),
    ('core.py', 694837, '3532dd31a231cf37'),
    ('data/.gitkeep', 0, 'e3b0c44298fc1c14'),
    ('env.example', 11292, '9db024083d3086ef'),
    ('import/.gitkeep', 0, 'e3b0c44298fc1c14'),
    ('manage_users.py', 4408, '20b4208818a458e4'),
    ('refresh.py', 3221, '015934f268286f0a'),
    ('requirements.txt', 990, 'db60ed39313d78e5'),
    ('run.py', 2584, '388fd1d790f938ba'),
    ('static/css/app.css', 52430, '714845000ed44da2'),
    ('static/js/catalog.js', 90959, 'fdf7019a79c8d73c'),
    ('static/js/chat.js', 44914, '040ecc6667c996ea'),
    ('static/js/common.js', 14733, 'a353f8d61cc80efa'),
    ('static/js/er.js', 49133, '8426669e76304e7b'),
    ('static/js/import.js', 22341, 'c8b1905774ac3bac'),
    ('static/js/mail.js', 9637, 'db2d0ed8d6fdf6d4'),
    ('static/js/manage.js', 22215, '5b8edb927a6ad2e4'),
    ('static/js/models.js', 13957, 'f245f3976d47e1ad'),
    ('static/js/table.js', 11916, '13318d72467ee6d0'),
    ('templates/403.html', 909, '432cfd81607e347d'),
    ('templates/_icons.html', 5497, '5789659ac9e687df'),
    ('templates/base.html', 5046, 'b8741883744afbd2'),
    ('templates/catalog.html', 24121, '01c051b4e3aa7393'),
    ('templates/chat.html', 5302, '569a09db39558c47'),
    ('templates/import.html', 5112, '0766680f2161a17b'),
    ('templates/login.html', 1739, '65e61274a2e9979f'),
    ('templates/mail.html', 5520, 'd9dd82fc99a6428e'),
    ('templates/models.html', 5692, 'a24b5c3097202b6c'),
    ('templates/table.html', 2798, 'f6a4730681f96cba'),
    ('web.py', 146307, '442200a675198942'),
]


def judge(path: pathlib.Path, size: int, want: str):
    """1ファイルを見て、問題があれば理由を返す。無ければ None。"""
    if not path.exists():
        return "ファイルが無い（置き忘れ、または名前の綴り違い）"
    body = path.read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(body).hexdigest()[:16] == want:
        return None
    got = len(body)
    if body[:3] == b"\xef\xbb\xbf":
        return "先頭にBOMが付いている（UTF-8（BOMなし）で保存し直す）"
    if got < size * 0.98:
        return f"途中で切れている（{got:,} / 正しくは {size:,} バイト）"
    if got > size * 1.02:
        return f"余分なものが入っている（{got:,} / 正しくは {size:,} バイト・行番号の混入など）"
    return f"中身が違う（{got:,} / 正しくは {size:,} バイト）"


def main() -> int:
    if not pathlib.Path("run.py").exists():
        print("run.py が見つかりません。アプリのフォルダで実行してください。")
        return 2

    ng = [(name, why) for name, size, want in EXPECT
          if (why := judge(pathlib.Path(name), size, want))]

    print(f"照合 {len(EXPECT)} ファイル")
    if not ng:
        print("  すべて一致しました。ファイルのコピーは正しくできています。")
        print("  それでも画面が動かない場合は、ブラウザのキャッシュを消して")
        print("  再読み込みしてください（Ctrl+F5）。")
        return 0

    print(f"  問題のあるファイル: {len(ng)} 件")
    for name, why in ng:
        print(f"    x {name:34s} {why}")
    print()
    print("  直し方:")
    print("    GitHub でそのファイルを開き、右上の Raw ボタンを押してから保存する")
    print("    （またはダウンロードのアイコン）。画面のコードを選択してコピーすると、")
    print("    行番号が混ざったり、長いファイルが途中で切れたりします。")
    print("    文字コードは UTF-8（BOMなし）、ファイル名は綴りをそのままにしてください。")
    print()
    print("    リポジトリのトップで Code → Download ZIP を使えば、")
    print("    全ファイルを一度に正しく取得できます。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
