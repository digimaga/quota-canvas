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
def _no_real_balance_credentials(monkeypatch):
    """The suite must never read or use real credentials for new API providers."""
    from quotacanvas import keychain
    original = keychain.read
    def read(provider):
        if provider in {"openrouter", "deepseek", "kimi"}:
            return None
        return original(provider)
    monkeypatch.setattr(keychain, "read", read)
    for variable in ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY"):
        monkeypatch.delenv(variable, raising=False)
