import os

import httpx
import pytest
import yaml
from test_gemini_adapter import MODEL, PROVIDER, metadata, response, settings, spec

from fair.providers.live import register_live
from fair.security.credentials import CredentialConfigurationError, ProviderCredentials

BINDINGS = {PROVIDER: "GEMINI_API_KEY"}


def test_env_file_scopes_secrets_without_changing_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "ambient-key")
    path = tmp_path / ".env"
    path.write_text(
        "\ufeff# saved locally\nGEMINI_API_KEY = 'file-key' # chosen key\n"
        'GROQ_API_KEY="unrelated-secret"\nFAIR_DEMO_MODE=1\nEMPTY=\n',
        encoding="utf-8",
    )
    before = dict(os.environ)
    resolver = ProviderCredentials.from_env_file(BINDINGS, path)
    assert resolver.for_provider(PROVIDER).get_secret_value() == "file-key"
    assert dict(os.environ) == before
    assert "file-key" not in repr(vars(resolver))
    assert "unrelated-secret" not in repr(vars(resolver))
    assert set(resolver._values) == {"GEMINI_API_KEY"}
    with pytest.raises(CredentialConfigurationError):
        resolver.for_provider("groq")
    path.write_text("GEMINI_API_KEY=rotated-key")
    assert resolver.for_provider(PROVIDER).get_secret_value() == "file-key"
    assert (
        ProviderCredentials.from_env_file(BINDINGS, path).for_provider(PROVIDER).get_secret_value()
        == "rotated-key"
    )


@pytest.mark.parametrize("content", ["", "GEMINI_API_KEY=", "GROQ_API_KEY=other-key"])
def test_explicit_file_does_not_fall_back_to_environment(tmp_path, monkeypatch, content):
    monkeypatch.setenv("GEMINI_API_KEY", "ambient-key")
    path = tmp_path / ".env"
    path.write_text(content)
    resolver = ProviderCredentials.from_env_file(BINDINGS, path)
    with pytest.raises(CredentialConfigurationError, match="Provider credential unavailable"):
        resolver.for_provider(PROVIDER)


@pytest.mark.parametrize(
    "content",
    [
        b"GEMINI_API_KEY=secret\nGEMINI_API_KEY=other",
        b"UNRELATED=a\nUNRELATED=b",
        b"GEMINI_API_KEY='secret",
        b'GEMINI_API_KEY="secret" trailing',
        b"GEMINI_API_KEY=has space",
        b"GEMINI_API_KEY=secret\x00",
        b"GEMINI_API_KEY=\xff",
        b"export GEMINI_API_KEY=secret",
        b"GEMINI_API_KEY",
        b"GEMINI_API_KEY='multi\nline'",
        b"#" * 65537,
        b"GEMINI_API_KEY=" + b"s" * 4097,
    ],
    ids=[
        "duplicate-key",
        "duplicate-unrelated",
        "unclosed-quote",
        "trailing-text",
        "key-whitespace",
        "nul-byte",
        "invalid-utf8",
        "shell-export",
        "missing-equals",
        "multiline",
        "oversized-file",
        "oversized-key",
    ],
)
def test_invalid_env_files_fail_without_echoing_contents(tmp_path, content):
    path = tmp_path / ".env"
    path.write_bytes(content)
    with pytest.raises(CredentialConfigurationError) as error:
        ProviderCredentials.from_env_file(BINDINGS, path)
    assert str(error.value) == "Invalid provider env file"
    assert str(path) not in str(error.value)


def test_missing_file_and_duplicate_bindings_fail(tmp_path):
    with pytest.raises(CredentialConfigurationError, match="Invalid provider env file"):
        ProviderCredentials.from_env_file(BINDINGS, tmp_path / "missing.env")
    with pytest.raises(CredentialConfigurationError, match="Ambiguous provider credential"):
        ProviderCredentials({"a": "SAME_KEY", "b": "SAME_KEY"})


def test_values_are_literal_without_shell_or_variable_expansion(tmp_path):
    path = tmp_path / ".env"
    value = "${OTHER_KEY}$(whoami)`hostname`#literal"
    path.write_text("GEMINI_API_KEY=" + value)
    assert (
        ProviderCredentials.from_env_file(BINDINGS, path).for_provider(PROVIDER).get_secret_value()
        == value
    )


async def test_smoke_uses_selected_file_key_and_retains_admission(tmp_path, monkeypatch):
    import fair.providers.smoke as smoke_module

    path = tmp_path / ".env"
    path.write_text("GEMINI_API_KEY=local-test-key\nGROQ_API_KEY=unrelated-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "ambient-key")
    before = dict(os.environ)
    (tmp_path / "providers.yaml").write_text(
        yaml.safe_dump({"providers": [spec().model_dump(mode="json")]}), encoding="utf-8"
    )
    (tmp_path / "live_adapters.yaml").write_text(
        yaml.safe_dump(settings().model_dump()), encoding="utf-8"
    )
    calls = []

    def handler(req):
        assert req.headers["x-goog-api-key"] == "local-test-key"
        assert b"unrelated-secret" not in req.content
        calls.append(req.method)
        return httpx.Response(200, json=metadata() if req.method == "GET" else response())

    monkeypatch.setattr(
        smoke_module,
        "register_live",
        lambda registry, specs, config, **kwargs: register_live(
            registry, specs, config, httpx.MockTransport(handler), **kwargs
        ),
    )
    result = await smoke_module.smoke(tmp_path, PROVIDER, MODEL, env_file=path)
    assert result["status"] == "ACCEPTED"
    assert calls == ["GET", "POST"] and dict(os.environ) == before
    assert "local-test-key" not in str(result) and "unrelated-secret" not in str(result)

    calls.clear()
    (tmp_path / "live_adapters.yaml").write_text(
        yaml.safe_dump(settings(gemini_free_tier_confirmed=False).model_dump()), encoding="utf-8"
    )
    with pytest.raises(
        CredentialConfigurationError, match="Provider adapter initialization failed"
    ):
        await smoke_module.smoke(tmp_path, PROVIDER, MODEL, env_file=path)
    assert calls == []


def test_cli_env_file_argument_and_redacted_failure(tmp_path, monkeypatch, capsys):
    import fair.providers.smoke as smoke_module

    path = tmp_path / ".env"
    seen = []

    async def fake_smoke(directory, provider_id, model_id, *, env_file):
        seen.append(env_file)
        raise ValueError("private-key-and-path")

    monkeypatch.setattr(smoke_module, "smoke", fake_smoke)
    monkeypatch.setattr(
        "sys.argv",
        [
            "smoke",
            "--config-directory",
            str(tmp_path),
            "--provider",
            PROVIDER,
            "--model",
            MODEL,
            "--env-file",
            str(path),
        ],
    )
    assert smoke_module.main() == 1
    assert seen == [path]
    output = capsys.readouterr().out
    assert "PROVIDER_SMOKE_FAILED" in output and "private-key-and-path" not in output
