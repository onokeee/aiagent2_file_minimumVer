"""認証。社内LDAP認証APIへ差し替えることを前提にした作り。

差し替えるときに触るのは **このファイルの Provider 1クラスと env だけ** で、
画面やカタログ側のコードは変更不要。

  env の AUTH_PROVIDER で切り替える
    local … auth_users.yaml のユーザーで認証（既定。LDAP導入前の暫定）
    http  … 社内の認証APIにHTTPで問い合わせる（LDAP API用）

アプリ側が認証結果に求めるのは User だけ:
    username     … カタログとチャット履歴の保存先フォルダ名に使う識別子
    display_name … 画面表示名
    groups       … 所属グループ（LDAP側の情報をそのまま持つ）
    is_admin     … AUTH_ADMIN_GROUP に属しているか（今は表示のみ）

※ このログインは「ユーザーごとにカタログとチャット履歴を分ける」ための仕組みであって、
   OSレベルのアクセス制御ではない。data/ のファイルを直接読める人には効かない。
   詳しくは README の「ログインについての注意」を参照。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import yaml

import config

# --- ユーザー ------------------------------------------------------------------

@dataclass
class User:
    username: str
    display_name: str = ""
    groups: list = field(default_factory=list)
    is_admin: bool = False

    def __post_init__(self):
        self.display_name = self.display_name or self.username

    @property
    def safe_key(self) -> str:
        """フォルダ名に使える識別子（個人カタログの保存先）。"""
        return "".join(c if (c.isalnum() or c in "-_.@") else "_" for c in self.username)[:64]


# --- パスワードのハッシュ（localプロバイダ用） --------------------------------------

_ITER = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITER)
    return f"pbkdf2_sha256${_ITER}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = str(stored).split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# --- プロバイダ ----------------------------------------------------------------

class AuthProvider(ABC):
    """認証方式の共通インタフェース。差し替えるときはこれを実装する。"""

    name = "base"
    #: 画面のログインフォームに出す補足
    hint = ""

    @abstractmethod
    def authenticate(self, username: str, password: str) -> User | None:
        """成功なら User、失敗なら None を返す。
        通信エラーなど「認証以前の失敗」は AuthError を送出する。"""


class AuthError(RuntimeError):
    """認証処理そのものが行えなかった（設定不備・通信不可など）。"""


class LocalAuthProvider(AuthProvider):
    """auth_users.yaml のユーザーで認証する（LDAP導入までの暫定）。

    ユーザーの追加は manage_users.py で行う（パスワードはハッシュ化して保存）。
    """

    name = "local"
    hint = "社内LDAP導入までの暫定アカウントです。"

    def __init__(self, path: Path | None = None):
        self.path = Path(path or config.AUTH_USERS_FILE)

    def _load(self) -> list:
        if not self.path.exists():
            return []
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            raise AuthError(f"ユーザー定義を読めませんでした: {self.path} ({e})")
        return data.get("users") or []

    def authenticate(self, username: str, password: str) -> User | None:
        users = self._load()
        if not users:
            raise AuthError(
                "ユーザーが1人も登録されていません。"
                "`python manage_users.py add <ユーザー名>` で追加してください。")
        for u in users:
            if str(u.get("username", "")).lower() != str(username).lower():
                continue
            if not verify_password(password, u.get("password_hash", "")):
                return None
            groups = list(u.get("groups") or [])
            return User(username=str(u["username"]), display_name=str(u.get("display_name") or ""),
                        groups=groups, is_admin=config.AUTH_ADMIN_GROUP in groups)
        return None


class HttpApiAuthProvider(AuthProvider):
    """社内の認証API（LDAP連携API）にHTTPで問い合わせる。

    エンドポイントの仕様に合わせて env の AUTH_API_* を設定するだけで動く想定。
    レスポンスのJSONからどのキーを読むかも env で指定できる。

      AUTH_API_URL           = https://example.co.jp/api/auth
      AUTH_API_USER_FIELD    = username     # 送信するJSONのキー
      AUTH_API_PASS_FIELD    = password
      AUTH_API_SUCCESS_FIELD = authenticated  # 真偽値が入るキー（省略時はHTTP200で成功）
      AUTH_API_DISPLAY_FIELD = displayName
      AUTH_API_GROUPS_FIELD  = memberOf
      AUTH_API_TIMEOUT       = 10

    仕様が上記で表現できない場合は、この authenticate() だけ書き換えればよい。
    """

    name = "http"
    hint = "社内アカウントでログインしてください。"

    def authenticate(self, username: str, password: str) -> User | None:
        url = config.AUTH_API_URL
        if not url:
            raise AuthError("AUTH_API_URL が設定されていません（env を確認してください）。")

        payload = json.dumps({
            config.AUTH_API_USER_FIELD: username,
            config.AUTH_API_PASS_FIELD: password,
        }).encode()
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=config.AUTH_API_TIMEOUT) as res:
                body = res.read().decode("utf-8", "replace")
                status = res.status
        except urllib.error.HTTPError as e:
            # 401/403 は「認証失敗」、それ以外は異常として扱う
            if e.code in (400, 401, 403):
                return None
            raise AuthError(f"認証APIがエラーを返しました: HTTP {e.code}")
        except Exception as e:
            raise AuthError(f"認証APIに接続できません: {e}")

        try:
            data = json.loads(body) if body.strip() else {}
        except json.JSONDecodeError:
            raise AuthError("認証APIの応答がJSONではありません。")

        ok_field = config.AUTH_API_SUCCESS_FIELD
        if ok_field:
            if not bool(_dig(data, ok_field)):
                return None
        elif status != 200:
            return None

        # グループを返さない認証APIは珍しくない。その場合は全員を一般ユーザーとして扱う
        # （管理者は env の ADMIN_PASS で入る admin だけになる）。
        # 応答に無いものを推測して管理者にするのは危険なので、迷ったら一般にする。
        groups = []
        if config.AUTH_API_GROUPS_FIELD:
            raw = _dig(data, config.AUTH_API_GROUPS_FIELD) or []
            groups = [str(g) for g in ([raw] if isinstance(raw, str) else raw)]
        return User(
            username=str(_dig(data, config.AUTH_API_USER_FIELD) or username),
            display_name=str(_dig(data, config.AUTH_API_DISPLAY_FIELD) or ""),
            groups=groups,
            is_admin=bool(config.AUTH_ADMIN_GROUP) and config.AUTH_ADMIN_GROUP in groups,
        )


def _dig(data: dict, path: str):
    """'user.displayName' のようなドット区切りでネストしたJSONから値を取る。"""
    if not path:
        return None
    cur = data
    for part in str(path).split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


_PROVIDERS = {"local": LocalAuthProvider, "http": HttpApiAuthProvider}


def get_provider(name: str | None = None) -> AuthProvider:
    """設定に応じた認証プロバイダを返す。"""
    key = (name or config.AUTH_PROVIDER or "local").strip().lower()
    cls = _PROVIDERS.get(key)
    if cls is None:
        raise AuthError(f"未知の AUTH_PROVIDER です: {key} / 使えるのは {', '.join(_PROVIDERS)}")
    return cls()


# --- 常設の管理者（env の ADMIN_PASS） -------------------------------------------
#
# LDAPにも auth_users.yaml にも依存しない固定アカウント。
# LDAPが落ちている・まだ繋いでいない状況でも設定画面に入れるようにするための口。
#
# 安全のための決めごと:
#   1. ADMIN_PASS が空なら、このアカウントは存在しない扱い（空パスワードで入れない）
#   2. 比較は hmac.compare_digest（入力の長さや内容で処理時間が変わらないように）
#   3. 通常のプロバイダより先に判定する（LDAP側に同名ユーザーがいても取り違えない）

def admin_enabled() -> bool:
    return bool(config.ADMIN_USER and config.ADMIN_PASS)


def _try_builtin_admin(username: str, password: str) -> User | None:
    if not admin_enabled():
        return None
    if str(username).strip().lower() != config.ADMIN_USER.lower():
        return None
    if not hmac.compare_digest(str(password), config.ADMIN_PASS):
        return None
    return User(username=config.ADMIN_USER, display_name="管理者",
                groups=[config.AUTH_ADMIN_GROUP], is_admin=True)


def authenticate(username: str, password: str) -> User | None:
    """ログインの入口。常設の管理者を先に見て、その後プロバイダに渡す。

    画面からはこの関数だけを呼ぶ。プロバイダを差し替えても、管理者の非常口は
    そのまま残る。
    """
    admin = _try_builtin_admin(username, password)
    if admin is not None:
        return admin
    # 常設管理者と同じIDなら、パスワード違いとして扱いプロバイダには渡さない
    if admin_enabled() and str(username).strip().lower() == config.ADMIN_USER.lower():
        return None
    return get_provider().authenticate(username, password)


# --- ユーザー定義ファイルの操作（manage_users.py から使う） --------------------------

def load_users_file(path: Path | None = None) -> dict:
    p = Path(path or config.AUTH_USERS_FILE)
    if not p.exists():
        return {"users": []}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {"users": []}


def save_users_file(data: dict, path: Path | None = None) -> None:
    p = Path(path or config.AUTH_USERS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
    try:                       # 他ユーザーから読めないようにする（Windowsでは無視される）
        os.chmod(p, 0o600)
    except OSError:
        pass
