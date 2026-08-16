# このフォルダについて

`aiagent/` から この統合版を作り直すためのスクリプトです。
**アプリの動作には一切関わりません。要らなければフォルダごと消して構いません。**

元の `aiagent/` を直したあと、こちらへ反映したいときに使います。

## 手順

1. `aiagent/` の中身をこのフォルダへ上書きコピー
   （`.venv` `.git` `.ruff_cache` `__pycache__` は除く）
2. `web/templates` → `templates`、`web/static` → `static` へ移動
3. `python .build/build.py` を実行
4. `.build/docs/README.md` を上の階層へ戻す
   （手順1で元のものに戻ってしまうため。ファイル構成の説明だけが違う）
5. 実環境に不要なものを消す（手順1で戻ってくるため）:
   - `sample_db.py` `fab_db.py`（デモDB生成）
   - `TESTING.md` `TEST_RESULTS.md`
   - `data/` の中身（DB・メタ情報・履歴・`users/`・`.profile_cache/`・
     `import_jobs.yaml`・`mail_settings.yaml`・`model_settings.yaml` など。
     `README.txt` だけ残す）
   - `import/` の中身（`README.md` だけ残す）
   - `auth_users.yaml` `.flask_secret`（環境ごとに作り直す）

## 中身

- `build.py` … これ1本。前半が道具（名前の付け替えと、変換が正しいかのAST突き合わせ）、
  後半が「どのモジュールをどのファイルにまとめるか、ぶつかる名前をどう付け替えるか」の一覧
- `docs/`    … 統合版むけに書き直した README

## 変換で気をつけていること

- **`advanced.py` は行番号を動かさない**。カタログ画面の「ツール」タブが
  `inspect` でこのファイルの行番号を読み、`advanced.py:NNN` と画面に出しているため
- 名前がぶつかるものだけ付け替え、その呼び出し元を追随させる
- 付け替えのたびにASTを突き合わせ、属性名・キーワード引数名・文字列が
  変わっていないことを確認する（変わっていたらその場で止まる）
