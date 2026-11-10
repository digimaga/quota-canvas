import pytest


@pytest.fixture(autouse=True)
def _no_real_grok_login(tmp_path, monkeypatch):
    """Point the Grok provider at an empty home so tests never use the real login."""
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok-home"))


@pytest.fixture(autouse=True)
def _no_real_gemini_login(monkeypatch):
    """キーチェーン gemini/antigravity (Antigravity CLI の実ログイン) を読ませない。
    使いたいテストは自分でもう一度 monkeypatch すればよい (test_gemini.py)。"""
    from quotacanvas import providers

    def no_entry(*args, **kwargs):
        raise providers.CredentialError(providers.t("providers.no_credentials"))

    monkeypatch.setattr(providers.gemini, "_read_entry", no_entry)


@pytest.fixture(autouse=True)
def _no_real_agy(monkeypatch):
    """実機の agy (Antigravity CLI) を実行させない: agy は常に見つからないことに
    する (detect は未検出、fetch は agy 見つからないのエラーになる)。agy を
    使うテストは自分でもう一度 monkeypatch すればよい (test_gemini.py)。"""
    from quotacanvas import providers

    monkeypatch.setattr(providers.gemini, "_find_agy", lambda path=None: None)


@pytest.fixture(autouse=True)
def _no_mac_power(monkeypatch):
    """実機のディスプレイ状態・IOKit に触れない: Mac スリープ連携は既定で無効。
    スリープ連携を試すテストは、この後に自分でもう一度 monkeypatch すればよい。"""
    from quotacanvas import macpower

    monkeypatch.setattr(macpower, "display_asleep", lambda: False)
    monkeypatch.setattr(macpower, "start_watch", lambda on_sleep, on_wake: False)


@pytest.fixture(autouse=True)
def _japanese_ui():
    """既存テストは日本語の文言を前提にしている: 既定では表示言語を ja に固定する。"""
    from quotacanvas import i18n

    i18n.set_language("ja")
    yield
    i18n.set_language("ja")


@pytest.fixture(autouse=True)
def _no_real_routing_environment(monkeypatch):
    """routing の status パスや強制フラグを実行環境から引き継がない (実 HOME の
    status.json を予備として読んだり、判定を強制したりしないため)。必要なテストは
    自分で設定する。"""
    for variable in ("QUOTA_STATUS_PATH", "QUOTA_STATUS_FALLBACK", "QUOTA_GATE_FORCE",
                     "QUOTA_CANVAS_EXAMPLE_CONFIG"):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture(autouse=True)
def _no_real_balance_credentials(monkeypatch):
    """The suite must never read or use real credentials for new API providers."""
    from quotacanvas import keychain
    original = keychain.read
    def read(provider):
        if provider in {"openrouter", "deepseek", "kimi", "opencode", "devin",
                        "muse"}:
            return None
        return original(provider)
    monkeypatch.setattr(keychain, "read", read)
    for variable in ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY",
                     "OPENCODE_API_KEY", "MUSE_COOKIE"):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture(autouse=True)
def _no_real_opencode_login(tmp_path, monkeypatch):
    """Point the OpenCode provider at a nonexistent auth.json so tests never
    read the real opencode CLI login."""
    from quotacanvas import providers

    monkeypatch.setattr(providers.opencode, "_auth_file",
                        lambda: tmp_path / "opencode-auth.json")


@pytest.fixture(autouse=True)
def _no_real_devin_login(tmp_path, monkeypatch):
    """Point the Devin provider at nonexistent credential files so tests never
    read the real Devin CLI login or desktop app state database."""
    from quotacanvas import providers

    monkeypatch.setattr(providers.devin, "_credentials_file",
                        lambda: tmp_path / "devin-credentials.toml")
    monkeypatch.setattr(providers.devin, "_desktop_state_db",
                        lambda: tmp_path / "devin-state.vscdb")


@pytest.fixture(autouse=True)
def _no_real_detect_cache(tmp_path, monkeypatch):
    """detect-cache.json を実データディレクトリではなく tmp_path に向ける。
    detect コマンドや常駐側の更新を試すテストが実キャッシュを上書きしない。"""
    from quotacanvas import detect_cache

    monkeypatch.setattr(detect_cache, "DETECT_CACHE_PATH",
                        tmp_path / "detect-cache.json")


@pytest.fixture(autouse=True)
def _no_real_muse_cache(tmp_path, monkeypatch):
    """Point the Muse provider at a nonexistent action-id cache so tests never
    read or write the real cache file."""
    from quotacanvas import providers

    monkeypatch.setattr(providers.muse, "_action_cache_file",
                        lambda: tmp_path / "muse-action.json")
