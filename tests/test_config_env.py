from alice.config import init_home


def test_old_env_gets_new_provider_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_HOME", str(tmp_path))
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=abc\nGROQ_API_KEY=xyz\n", encoding="utf-8")
    init_home()
    text = env.read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=abc" in text and "GROQ_API_KEY=xyz" in text
    assert "\nNVIDIA_API_KEY=\n" in text and "build.nvidia.com" in text
    assert text.count("GEMINI_API_KEY") == 1
    init_home()
    assert env.read_text(encoding="utf-8") == text
