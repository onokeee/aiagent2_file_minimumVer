"""定期取り込みをコマンドから実行する（cron / タスクスケジューラ用）。

  python refresh.py              期限が来たジョブだけ実行
  python refresh.py --all        期限に関係なく全部実行（手動のみのジョブも含む）
  python refresh.py --job <ID>   1本だけ実行
  python refresh.py --list       ジョブの一覧を表示するだけ

Linuxサーバでの設定例（15分おきに期限チェック）:
  */15 * * * * cd /opt/aiagent && .venv/bin/python refresh.py >> /var/log/aiagent-refresh.log 2>&1

ジョブごとの間隔は画面（🗄 データ取り込み → 🔁 定期取り込み）で設定する。
cron は「期限が来ているか」を見に行くだけなので、cron 側は細かく回しておけばよい。

終了コード: 0=全部成功（または対象なし） / 1=1本でも失敗
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

# 統合ファイルを先に読む。これで統合前と同じ `import jobs` などが通る。
import core  # noqa: F401

import jobs


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _show(job: dict) -> str:
    nxt = jobs.next_run_at(job)
    return (f"{job.get('name') or job['id']}"
            f" [{job.get('db_file')} / {job.get('table')}"
            f" / {jobs.MODES.get(job.get('mode'), job.get('mode'))}"
            f" / {jobs.interval_label(job.get('interval_minutes', 0))}]"
            + ("" if job.get("enabled", True) else " (停止中)")
            + (f" 次回 {nxt:%Y-%m-%d %H:%M}" if nxt else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description="定期取り込みの実行")
    ap.add_argument("--all", action="store_true", help="期限に関係なく全ジョブを実行")
    ap.add_argument("--job", help="指定したIDのジョブだけ実行")
    ap.add_argument("--list", action="store_true", help="一覧表示のみ")
    args = ap.parse_args()

    all_jobs = jobs.list_jobs()
    if args.list:
        if not all_jobs:
            print("ジョブは登録されていません。")
        for j in all_jobs:
            print(" ", j["id"], _show(j))
        return 0

    if args.job:
        target = jobs.get_job(args.job)
        if target is None:
            print(f"ジョブが見つかりません: {args.job}", file=sys.stderr)
            return 1
        targets = [target]
    elif args.all:
        targets = [j for j in all_jobs if j.get("enabled", True)]
    else:
        targets = jobs.due_jobs()

    if not targets:
        print(f"[{_stamp()}] 実行対象はありません。")
        return 0

    failed = 0
    for j in targets:
        res = jobs.run_job(j)
        mark = "OK " if res["ok"] else "NG "
        print(f"[{_stamp()}] {mark}{_show(j)} -> {res['message']}")
        if res["degraded"]:
            print(f"        ※ TEXTに落とした列: {', '.join(res['degraded'])}")
        if not res["ok"]:
            failed += 1
    print(f"[{_stamp()}] {len(targets)}件中 {len(targets) - failed}件成功 / {failed}件失敗")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
