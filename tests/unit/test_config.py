"""Unit tests for config and registry."""

from pathlib import Path

from breadforge.config import Config, Registry, RepoEntry


class TestConfig:
    def test_defaults(self) -> None:
        config = Config(repo="owner/repo")
        assert config.concurrency == 3
        assert config.max_retries == 3
        assert config.agent_timeout_minutes == 60

    def test_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("BREADFORGE_CONCURRENCY", "5")
        monkeypatch.setenv("BREADFORGE_MODEL", "claude-opus-4-6")
        monkeypatch.setenv("BREADFORGE_MAX_RETRIES", "2")
        config = Config.from_env("owner/repo")
        assert config.concurrency == 5
        assert config.model == "claude-opus-4-6"
        assert config.max_retries == 2

    def test_beads_dir_default(self) -> None:
        config = Config(repo="owner/repo")
        assert ".breadforge" in str(config.beads_dir)
        assert "beads" in str(config.beads_dir)


class TestRegistry:
    def test_add_and_get(self, tmp_path: Path) -> None:
        registry = Registry(path=tmp_path / "breadforge.toml")
        entry = RepoEntry(
            repo="owner/myrepo",
            local_path=tmp_path / "myrepo",
            spec_dir=tmp_path / "myrepo" / "specs",
        )
        registry.add(entry)
        result = registry.get("owner/myrepo")
        assert result is not None
        assert result.repo == "owner/myrepo"

    def test_remove(self, tmp_path: Path) -> None:
        registry = Registry(path=tmp_path / "breadforge.toml")
        entry = RepoEntry(
            repo="owner/myrepo",
            local_path=tmp_path / "myrepo",
            spec_dir=tmp_path / "myrepo" / "specs",
        )
        registry.add(entry)
        assert registry.remove("owner/myrepo") is True
        assert registry.get("owner/myrepo") is None

    def test_remove_nonexistent(self, tmp_path: Path) -> None:
        registry = Registry(path=tmp_path / "breadforge.toml")
        assert registry.remove("nobody/nothing") is False

    def test_list(self, tmp_path: Path) -> None:
        registry = Registry(path=tmp_path / "breadforge.toml")
        for i in range(3):
            registry.add(
                RepoEntry(
                    repo=f"owner/repo{i}",
                    local_path=tmp_path / f"repo{i}",
                    spec_dir=tmp_path / f"repo{i}" / "specs",
                )
            )
        assert len(registry.list()) == 3

    def test_persistence(self, tmp_path: Path) -> None:
        path = tmp_path / "breadforge.toml"
        registry = Registry(path=path)
        registry.add(
            RepoEntry(
                repo="owner/persisted",
                local_path=tmp_path / "persisted",
                spec_dir=tmp_path / "persisted" / "specs",
            )
        )
        # Re-load
        registry2 = Registry(path=path)
        assert registry2.get("owner/persisted") is not None

    def test_empty_registry(self, tmp_path: Path) -> None:
        registry = Registry(path=tmp_path / "breadforge.toml")
        assert registry.list() == []
        assert registry.get("anyone/anything") is None
