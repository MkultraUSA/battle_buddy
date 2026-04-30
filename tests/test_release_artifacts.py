from pathlib import Path


def test_required_portfolio_docs_exist():
    required = [
        "README.md",
        "INSTALL.md",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "config.env.example",
        "LICENSE",
        "docs/architecture.md",
        "docs/release-checklist.md",
    ]

    for filename in required:
        assert Path(filename).exists(), f"Missing required portfolio artifact: {filename}"


def test_example_config_contains_placeholders_not_empty_file():
    content = Path("config.env.example").read_text(encoding="utf-8")

    assert "Never commit config.env" in content
    assert "your_openrouter_key_here" in content
    assert "your_nextcloud_app_password_here" in content
    assert "replace_with_random" in content
