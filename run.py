"""アプリの起動口。

  開発:  python run.py
  本番:  waitress-serve --host=0.0.0.0 --port=8000 "web:create_app()"
         gunicorn -w 1 -b 0.0.0.0:8000 "web:create_app()"

本番でワーカーを増やす場合は必ず 1 にすること。
定期取り込みのスレッド（scheduler.py）がワーカーの数だけ立ち、
同じジョブを多重に実行してしまうため。同時アクセス数を稼ぎたいときは
スレッド数（waitress の --threads）で増やす。

回答の逐次表示（Server-Sent Events）を使うので、前段に nginx などを置く場合は
そのパスだけバッファリングを切ること（proxy_buffering off;）。
切らないと、回答がまとめて届いて逐次表示にならない。
"""
from __future__ import annotations

import os

from web import create_app

app = create_app()

if __name__ == "__main__":
    # reloader を切っているのは、二重起動でスケジューラのスレッドが増えるのを避けるため
    app.run(host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "8000")),
            debug=os.getenv("FLASK_DEBUG", "").lower() in ("1", "true"),
            use_reloader=False, threaded=True)
