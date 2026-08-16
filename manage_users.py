"""ローカル認証のユーザー管理（LDAP導入までの暫定アカウント用）。

  python manage_users.py list
  python manage_users.py add onoke --admin
  python manage_users.py passwd onoke
  python manage_users.py remove onoke

パスワードはプロンプトで入力する（画面に表示されない）。
保存されるのは PBKDF2-SHA256 のハッシュで、平文は保存しない。

社内LDAP認証APIに切り替えた後は、このファイルと auth_users.yaml は不要になる。
"""
from __future__ import annotations

import argparse
import getpass
import sys

import auth
import config


def _find(users: list, username: str):
    for i, u in enumerate(users):
        if str(u.get("username", "")).lower() == username.lower():
            return i
    return -1


def _ask_password(password: str | None) -> str:
    if password:
        print("警告: コマンドラインで渡したパスワードは履歴に残ります。", file=sys.stderr)
        return password
    p1 = getpass.getpass("パスワード: ")
    if not p1:
        sys.exit("パスワードが空です。")
    if p1 != getpass.getpass("パスワード（確認）: "):
        sys.exit("パスワードが一致しません。")
    return p1


def cmd_list(_args):
    users = auth.load_users_file().get("users") or []
    if not users:
        print("ユーザーは登録されていません。")
        return
    print(f"{'ユーザー名':<20} {'表示名':<20} グループ")
    for u in users:
        print(f"{u.get('username',''):<20} {u.get('display_name',''):<20} "
              f"{', '.join(u.get('groups') or []) or '-'}")


def cmd_add(args):
    data = auth.load_users_file()
    users = data.setdefault("users", [])
    if _find(users, args.username) >= 0:
        sys.exit(f"'{args.username}' は既に存在します。パスワード変更は passwd を使ってください。")
    groups = [g.strip() for g in (args.groups or "").split(",") if g.strip()]
    if args.admin and config.AUTH_ADMIN_GROUP not in groups:
        groups.append(config.AUTH_ADMIN_GROUP)
    users.append({
        "username": args.username,
        "display_name": args.display_name or args.username,
        "password_hash": auth.hash_password(_ask_password(args.password)),
        "groups": groups,
    })
    auth.save_users_file(data)
    print(f"追加しました: {args.username}"
          + (f"（{', '.join(groups)}）" if groups else ""))
    print(f"保存先: {config.AUTH_USERS_FILE}")


def cmd_passwd(args):
    data = auth.load_users_file()
    users = data.get("users") or []
    i = _find(users, args.username)
    if i < 0:
        sys.exit(f"'{args.username}' は存在しません。")
    users[i]["password_hash"] = auth.hash_password(_ask_password(args.password))
    auth.save_users_file(data)
    print(f"パスワードを変更しました: {args.username}")


def cmd_remove(args):
    data = auth.load_users_file()
    users = data.get("users") or []
    i = _find(users, args.username)
    if i < 0:
        sys.exit(f"'{args.username}' は存在しません。")
    users.pop(i)
    auth.save_users_file(data)
    print(f"削除しました: {args.username}")
    print("※ 個人カタログ（data/users/配下）は残ります。不要なら手動で削除してください。")


def main():
    ap = argparse.ArgumentParser(description="ローカル認証のユーザー管理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="一覧").set_defaults(func=cmd_list)

    a = sub.add_parser("add", help="追加")
    a.add_argument("username")
    a.add_argument("--display-name", default="")
    a.add_argument("--groups", default="", help="カンマ区切り")
    a.add_argument("--admin", action="store_true",
                   help=f"管理者グループ({config.AUTH_ADMIN_GROUP})に入れる")
    a.add_argument("--password", help="非対話で渡す（履歴に残るので非推奨）")
    a.set_defaults(func=cmd_add)

    p = sub.add_parser("passwd", help="パスワード変更")
    p.add_argument("username")
    p.add_argument("--password")
    p.set_defaults(func=cmd_passwd)

    r = sub.add_parser("remove", help="削除")
    r.add_argument("username")
    r.set_defaults(func=cmd_remove)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
