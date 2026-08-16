"""Named classify backends and key masking."""
from observer import classify, llm


def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "_DIR", str(tmp_path))
    llm.reset()
    classify.reset()


def test_seeds_lmstudio_from_config(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    cur = llm.active()
    assert cur["id"] == "lmstudio"
    assert "1234" in cur["base_url"]
    pub = llm.public()
    ids = [p["id"] for p in pub["providers"]]
    assert ids[:3] == ["lmstudio", "academiccloud", "groq"]
    assert all("api_key" not in p for p in pub["providers"])
    groq = next(p for p in pub["providers"] if p["id"] == "groq")
    assert groq["default_model"] == "openai/gpt-oss-20b"
    ac = next(p for p in pub["providers"] if p["id"] == "academiccloud")
    assert ac["default_model"] == "qwen3.6-35b-a3b"
    assert ac["default_base_url"] == "https://chat-ai.academiccloud.de/v1"


def test_switch_and_save_masks_key(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    llm.set_active("academiccloud")
    assert llm.active()["id"] == "academiccloud"
    assert llm.active()["model"] == "qwen3.6-35b-a3b"
    llm.save_provider(
        "academiccloud",
        base_url="https://chat-ai.academiccloud.de/v1",
        model="qwen3.6-35b-a3b",
        api_key="secret-token",
    )
    raw = (tmp_path / "llm.json").read_text()
    assert "secret-token" in raw
    pub = llm.public()
    assert pub["active"] == "academiccloud"
    ac = next(p for p in pub["providers"] if p["id"] == "academiccloud")
    assert ac["has_key"] is True
    assert "secret-token" not in str(pub)
    assert classify._headers() == {"Authorization": "Bearer secret-token"}


def test_empty_key_keeps_existing(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    llm.save_provider("groq", api_key="abc")
    llm.save_provider("groq", model="openai/gpt-oss-20b", api_key=None)
    groq = next(p for p in llm.enabled() if p["id"] == "groq")
    assert groq["api_key"] == "abc"


def test_stop_clears_active(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    llm.set_active("groq")
    assert llm.active()["id"] == "groq"
    llm.set_active("")
    assert llm.active()["id"] == ""
    assert llm.public()["enabled"] is False
    assert classify.available() is False


def test_two_providers_stay_enabled(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    llm.set_active("groq")
    llm.save_provider("academiccloud", api_key="token")
    assert llm.public()["active_ids"] == ["groq", "academiccloud"]
    llm.disable("groq")
    assert llm.public()["active_ids"] == ["academiccloud"]
    ac = next(p for p in llm.public()["providers"] if p["id"] == "academiccloud")
    assert ac["in_use"] is True
    groq = next(p for p in llm.public()["providers"] if p["id"] == "groq")
    assert groq["in_use"] is False


def test_rejects_bad_url(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    try:
        llm.save_provider("groq", base_url="not-a-url")
    except ValueError:
        return
    raise AssertionError("expected ValueError")
