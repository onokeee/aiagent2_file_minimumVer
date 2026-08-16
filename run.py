"""アプリの起動口。

  python run.py     ← 通常はこれで起動する

起動の設定はこのファイルの中だけで完結している（下の HOST / PORT / DEBUG）。
env や環境変数は見ない。「run.py に書いた値がそのまま使われる」ようにするため。
特に PORT は他のソフトが環境変数として設定していることがあり、それを読むと
ここに 8000 と書いてあるのに違うポートで起動する、という事故が起きる。

より頑丈なサーバで動かしたくなったときは、下記も使える（任意）。
その場合 HOST / PORT はコマンド側で指定するので、ここの値は使われない。

  waitress-serve --host=0.0.0.0 --port=8000 --threads=8 "web:create_app()"
  gunicorn -w 1 -b 0.0.0.0:8000 "web:create_app()"

gunicorn でワーカーを増やす場合は必ず 1 にすること。定期取り込みのスレッド
（scheduler）がワーカーの数だけ立ち、同じジョブを多重に実行してしまうため。
同時アクセス数を稼ぎたいときはスレッド数（waitress の --threads）で増やす。
waitress は元から1プロセスなので、この問題は起きない。

回答の逐次表示（Server-Sent Events）を使うので、前段に nginx などを置く場合は
そのパスだけバッファリングを切ること（proxy_buffering off;）。
切らないと、回答がまとめて届いて逐次表示にならない。
"""
from __future__ import annotations

from web import create_app

# --- 待ち受け先 -------------------------------------------------------------
# ここを書き換えれば起動先が変わる。
#
#   HOST = "0.0.0.0"    社内の他のPCからも開ける（本番はこちら）
#                       起動後は「サーバのIP:PORT」でアクセスする
#   HOST = "127.0.0.1"  起動したPCからだけ開ける（手元で試すとき）
#
# ポートが他のアプリと重なると起動に失敗する。その場合は PORT を変える。
HOST = "0.0.0.0"
PORT = 8000

# エラー画面に詳細を出すか。本番では必ず False のままにすること。
# True にすると、ブラウザからサーバ上で任意のコードを実行できてしまう。
DEBUG = False

app = create_app()

if __name__ == "__main__":
    # reloader を切っているのは、二重起動でスケジューラのスレッドが増えるのを避けるため
    app.run(host=HOST, port=PORT, debug=DEBUG,
            use_reloader=False, threaded=True)
