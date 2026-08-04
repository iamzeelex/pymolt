from pathlib import Path

from pymolt.core.enums import Mode, Provenance
from pymolt.core.graph import DependencyGraph, NameMapping
from pymolt.core.layers import IngestionReport
from pymolt.ingestion import conda_ingest, resolve_cache, uv_runner
from pymolt.ingestion.detect import DiscoveredSource, detect_project_python_version, detect_sources
from pymolt.ingestion.name_mapping import NameMapper


def orchestrate_ingestion(
    project_dir: str | Path,
    chosen_source: DiscoveredSource | None = None,
    target_python: str | None = None,
    container_id: str | None = None,
    base_python: str | None = None,
    constraint_file: Path | None = None,
    force_recompile: bool = False,
    use_cache: bool = True,
    tool: str | None = None
) -> tuple[DependencyGraph, IngestionReport]:
    """Orchestrate the ingestion workflow:
    1. Detect sources.
    2. Check for ambiguity/need for disambiguation.
    3. Dispatch to uv_runner or conda_ingest.
    4. Enrich nodes with provenance and name mappings.
    5. Construct the manual zone and output report.

    When ``force_recompile`` is set, an authoritative lock file is *not* read
    back verbatim; the manifest is re-resolved instead. This is what the audit
    uses for the prospective target-Python graph, where reusing the baseline
    lock would make every package look unchanged.
    """
    project_path = Path(project_dir)
    sources = detect_sources(project_path)
    
    warnings = []
    
    # Detect project's Python version
    detected_python = base_python or detect_project_python_version(project_path)
    if detected_python:
        try:
            import re
            match = re.match(r"^(\d+)\.(\d+)", detected_python)
            if match:
                major = int(match.group(1))
                minor = int(match.group(2))
                if major < 3 or (major == 3 and minor < 7):
                    warnings.append(
                        f"Project specifies legacy Python {detected_python} (< 3.7). "
                        "Using pip-compile fallback workflow with local python interpreter or Docker container for dependency resolution."
                    )
        except Exception:
            pass
            
    if not sources:
        raise ValueError(f"No python dependency sources detected in directory {project_dir}")
        
    # Disambiguation / selection
    selected_source = chosen_source
    if not selected_source:
        if len(sources) > 1:
            # Auto-select the first lock file, or pyproject.toml, or requirements.txt
            # Add warnings about competing/complementary sources
            lock_sources = [s for s in sources if s.is_lock]
            if lock_sources:
                selected_source = lock_sources[0]
            else:
                selected_source = sources[0]
                
            warnings.append(
                f"Multiple dependency sources detected: {[s.path.name for s in sources]}. "
                f"Auto-selected {selected_source.path.name}. Consider explicit selection to resolve ambiguity."
            )
        else:
            selected_source = sources[0]

    # conda-lock.yml alongside an environment.yml (folds into both the resolution
    # and the cache key).
    conda_lock_path = None
    if selected_source.mode == Mode.CONDA:
        for s in sources:
            if s.mode == Mode.CONDA and s.is_lock:
                conda_lock_path = s.path
                break

    # Resolution is a pure function of these inputs, so consult the content cache
    # before running the (slow) solve/compile.
    cache_key = resolve_cache.compute_key(
        source_path=selected_source.path,
        base_python=detected_python,
        container_id=container_id,
        force_recompile=force_recompile,
        constraint_file=constraint_file,
        extra_path=conda_lock_path,
        tool=tool,
    )
    graph = resolve_cache.load(project_path, cache_key) if use_cache else None

    if graph is None:
        if selected_source.mode == Mode.CONDA:
            graph = conda_ingest.ingest(env_file=selected_source.path, lock_file=conda_lock_path, container_id=container_id, force_recompile=force_recompile, base_python=detected_python)
        else:
            # PyPI mode
            graph = uv_runner.ingest(selected_source, current_python=detected_python, container_id=container_id, constraint_file=constraint_file, force_recompile=force_recompile, tool=tool)
        if use_cache:
            resolve_cache.store(project_path, cache_key, graph)

    # Warn if the source is not pinned
    if selected_source.fixation != "pinned":
        warnings.append(
            f"Source manifest {selected_source.path.name} is not fully pinned. "
            "The generated resolution map represents a reconstruction, not an exact snapshot of the production state."
        )

    # Name mapping enrichment
    mapper = NameMapper(project_dir=project_path)
    manual_zone = []

    for name, node in graph.nodes.items():
        if node.mode == Mode.CONDA:
            mapping = mapper.conda_to_pypi(node.name)
            node.mapping = mapping
            if node.provenance != Provenance.PIP_IN_CONDA:
                node.provenance = Provenance.CONDA_FORGE
            # Heuristic (self-name) guesses carry confidence="heuristic" on the
            # mapping so the renderer can hint "verify". We deliberately do NOT
            # route every heuristic into the manual zone: most self-names are
            # correct, and flooding it would bury the real no-twin cases. Precise
            # no-twin detection (system libs, mkl, *-ng, ...) needs a PyPI
            # existence check via the pypi_metadata adapter — tracked separately.
            if mapping is None:
                node.manual_bridge = True
                node.provenance = Provenance.CONDA_ONLY
                manual_zone.append(name)
        else:
            # PyPI nodes: default mapping is self-referential
            node.mapping = NameMapping(
                pypi_name=node.name,
                conda_name=node.name,
                import_name=node.name.replace("-", "_"),
                confidence="authoritative",
                source="pypi"
            )

    report = IngestionReport(
        resolution_quality=graph.resolution_quality.value,
        source_fixation=graph.source_fixation.value,
        manual_zone=manual_zone,
        warnings=warnings,
        detected_python=detected_python
    )
    
    return graph, report
