from pymolt.ingestion import resolve_cache


def test_compute_key_is_stable_and_input_sensitive(tmp_path):
    src = tmp_path / "requirements.txt"
    src.write_text("anyio==4.3.0\n", encoding="utf-8")

    k1 = resolve_cache.compute_key(src, "3.11", None, False)
    k2 = resolve_cache.compute_key(src, "3.11", None, False)
    assert k1 == k2  # deterministic

    # Any input change moves the key.
    assert resolve_cache.compute_key(src, "3.12", None, False) != k1
    assert resolve_cache.compute_key(src, "3.11", "abc", False) != k1
    assert resolve_cache.compute_key(src, "3.11", None, True) != k1

    src.write_text("anyio==4.4.0\n", encoding="utf-8")
    assert resolve_cache.compute_key(src, "3.11", None, False) != k1  # content changed


def test_orchestrator_reuses_cached_resolution(tmp_path, monkeypatch):
    """A second identical orchestration hits the cache and skips re-resolving."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'proj'\ndependencies = ['anyio']", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        """
version = 1

[[package]]
name = "proj"
version = "0.1.0"
source = { editable = "." }
dependencies = [{ name = "anyio" }]

[[package]]
name = "anyio"
version = "4.3.0"
source = { registry = "https://pypi.org/simple" }
""",
        encoding="utf-8",
    )

    import pymolt.ingestion.uv_runner as uv_runner
    from pymolt.ingestion.orchestrator import orchestrate_ingestion

    real_ingest = uv_runner.ingest
    calls = {"n": 0}

    def counting_ingest(*args, **kwargs):
        calls["n"] += 1
        return real_ingest(*args, **kwargs)

    monkeypatch.setattr(uv_runner, "ingest", counting_ingest)

    g1, _ = orchestrate_ingestion(tmp_path, base_python="3.12")
    g2, _ = orchestrate_ingestion(tmp_path, base_python="3.12")

    assert calls["n"] == 1  # second run served from cache
    assert g1.nodes["anyio"].version == g2.nodes["anyio"].version == "4.3.0"


def test_no_cache_forces_reresolution(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'proj'\ndependencies = ['anyio']", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        """
version = 1

[[package]]
name = "proj"
version = "0.1.0"
source = { editable = "." }
dependencies = [{ name = "anyio" }]

[[package]]
name = "anyio"
version = "4.3.0"
source = { registry = "https://pypi.org/simple" }
""",
        encoding="utf-8",
    )

    import pymolt.ingestion.uv_runner as uv_runner
    from pymolt.ingestion.orchestrator import orchestrate_ingestion

    real_ingest = uv_runner.ingest
    calls = {"n": 0}

    def counting_ingest(*args, **kwargs):
        calls["n"] += 1
        return real_ingest(*args, **kwargs)

    monkeypatch.setattr(uv_runner, "ingest", counting_ingest)

    orchestrate_ingestion(tmp_path, base_python="3.12", use_cache=False)
    orchestrate_ingestion(tmp_path, base_python="3.12", use_cache=False)
    assert calls["n"] == 2  # cache bypassed both times
