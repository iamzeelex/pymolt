"""
pymolt/codemods/apply.py

Apply codemod patterns (Tier-1) and codemod rules (Tier-2) to local source with
LibCST — format-preserving, so the author's whitespace, comments and layout
survive.

Two transformers cover the common Tier-1 deprecation shapes:
  - rewrite-import : same leaf, module moved → rewrite the `from … import …` line
                     (multi-name imports split onto their own lines).
  - rename-call    : leaf changed → rewrite call sites + the import name.

`apply_to_repo` walks a project's .py files (skipping virtualenvs, caches and
dot-dirs), applies the patterns, and reports what changed. It is dry-run by
default — nothing is written unless `write=True`.

`apply_rules_to_repo` / `preview_rules_repo` are the Tier-2 counterparts: they
walk the same file set and delegate the actual match/rewrite to
`pymolt.codemods.rules.apply_rule_detailed` (the declarative rule engine, which
also delegates LEGACY-kind rules straight back to the two transformers above —
see rules.py's module docstring).
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import libcst as cst
from libcst.metadata import MetadataWrapper, ScopeProvider
from libcst.metadata.scope_provider import ImportAssignment

from pymolt.codemods.models import (
    CodemodPattern,
    CodemodRunResult,
    FileChange,
    FilePreview,
    PreviewProgress,
    ProgressSink,
    recipe_key,
)
from pymolt.codemods.rules import CodemodRule, RuleAdvisory, apply_rule_detailed

log = logging.getLogger(__name__)

# Directories we never rewrite into.
_SKIP_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "env", ".env", "site-packages",
    "node_modules", "build", "dist", ".tox", ".nox", ".mypy_cache",
    ".pytest_cache", ".eggs",
}


def _leaf(qualname: str) -> str:
    return qualname.rsplit(".", 1)[-1]


def _module(qualname: str) -> str:
    return qualname.rsplit(".", 1)[0] if "." in qualname else ""


def _dotted(node: cst.BaseExpression) -> str | None:
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr.value}" if base else node.attr.value
    return None


def _module_expr(dotted: str) -> cst.BaseExpression:
    return cst.parse_expression(dotted)


def _clean_last_comma(alias: cst.ImportAlias) -> cst.ImportAlias:
    if isinstance(alias.comma, cst.Comma):
        return alias.with_changes(comma=cst.MaybeSentinel.DEFAULT)
    return alias


# ─────────────────────────────────────────────────────────────────────────────
# Transformers
# ─────────────────────────────────────────────────────────────────────────────


class RewriteImportCodemod(cst.CSTTransformer):
    """`from OLD_MODULE import LEAF` → `from NEW_MODULE import LEAF`."""

    def __init__(self, old_module: str, new_module: str, leaf: str) -> None:
        self.old_module = old_module
        self.new_module = new_module
        self.leaf = leaf
        self.sites = 0

    def leave_SimpleStatementLine(
        self, original: cst.SimpleStatementLine, updated: cst.SimpleStatementLine
    ) -> cst.SimpleStatementLine | cst.FlattenSentinel:
        if len(updated.body) != 1 or not isinstance(updated.body[0], cst.ImportFrom):
            return updated
        imp = updated.body[0]
        if imp.module is None or isinstance(imp.names, cst.ImportStar):
            return updated
        if _dotted(imp.module) != self.old_module:
            return updated
        if not any(a.name.value == self.leaf for a in imp.names):
            return updated

        self.sites += 1
        others = [a for a in imp.names if a.name.value != self.leaf]
        if not others:
            moved = imp.with_changes(
                module=_module_expr(self.new_module),
                names=[_clean_last_comma(a) for a in imp.names],
            )
            return updated.with_changes(body=[moved])
        kept = updated.with_changes(
            body=[imp.with_changes(names=[_clean_last_comma(a) for a in others])]
        )
        new_line = cst.SimpleStatementLine(
            body=[
                cst.ImportFrom(
                    module=_module_expr(self.new_module),
                    names=[cst.ImportAlias(name=cst.Name(self.leaf))],
                )
            ]
        )
        return cst.FlattenSentinel([kept, new_line])


class RenameCallCodemod(cst.CSTTransformer):
    """
    Rename a symbol where it is BOUND to the imported target — not by bare name.

    This is the graph-aware (not textual) rewrite: it consults LibCST's scope
    graph (ScopeProvider) so an unrelated local `old` or a same-named symbol
    imported from a DIFFERENT module is left untouched. Must run through a
    MetadataWrapper (see apply_pattern).

    Handled forms:
      - `from old_module import old_leaf [as a]` + calls to the bound name
        (calls to the bare name are renamed; aliased imports rewrite only the
        import and keep the alias-name call sites valid).
      - `import old_module [as m]` + `m.old_leaf(...)` attribute calls.
    """

    METADATA_DEPENDENCIES = (ScopeProvider,)

    def __init__(self, old_qualname: str, new_qualname: str) -> None:
        self.old_leaf = _leaf(old_qualname)
        self.new_leaf = _leaf(new_qualname)
        self.old_module = _module(old_qualname)
        self.new_module = _module(new_qualname)
        self.sites = 0

    def _import_binding(self, name_node: cst.Name) -> str | None:
        """'direct' | 'aliased' | None — is `name` bound to OUR imported symbol?"""
        bindings = self._bindings(name_node)
        if bindings is None:
            return None
        matched = None
        has_local = False
        for b in bindings:
            node = getattr(b, "node", None)
            if isinstance(b, ImportAssignment) and isinstance(node, cst.ImportFrom):
                if (
                    node.module is not None
                    and _dotted(node.module) == self.old_module
                    and not isinstance(node.names, cst.ImportStar)
                ):
                    for alias in node.names:
                        bound = alias.evaluated_alias or alias.evaluated_name
                        if alias.evaluated_name == self.old_leaf and bound == name_node.value:
                            matched = alias
            elif not isinstance(b, ImportAssignment):
                has_local = True  # a local def/assignment shadows it → leave alone
        if matched is None or has_local:
            return None
        return "aliased" if matched.asname is not None else "direct"

    def _module_binding(self, name_node: cst.Name) -> bool:
        """True if `name` is bound to `import old_module` (for m.old_leaf())."""
        bindings = self._bindings(name_node)
        if bindings is None:
            return False
        for b in bindings:
            node = getattr(b, "node", None)
            if isinstance(b, ImportAssignment) and isinstance(node, cst.Import):
                for alias in node.names:
                    bound = alias.evaluated_alias or alias.evaluated_name
                    if alias.evaluated_name == self.old_module and bound == name_node.value:
                        return True
        return False

    def _bindings(self, name_node: cst.Name):
        try:
            scope = self.get_metadata(ScopeProvider, name_node)
        except KeyError:
            return None
        if scope is None:
            return None
        try:
            return list(scope[name_node.value])
        except KeyError:
            return None

    def leave_Call(self, original: cst.Call, updated: cst.Call) -> cst.BaseExpression:
        ofunc, ufunc = original.func, updated.func
        if isinstance(ufunc, cst.Name):
            if self._import_binding(ofunc) == "direct":
                self.sites += 1
                return updated.with_changes(func=cst.Name(self.new_leaf))
            return updated
        if isinstance(ufunc, cst.Attribute) and ufunc.attr.value == self.old_leaf:
            base = ofunc.value
            if isinstance(base, cst.Name) and self._module_binding(base):
                self.sites += 1
                return updated.with_changes(
                    func=ufunc.with_changes(attr=cst.Name(self.new_leaf))
                )
        return updated

    def leave_ImportFrom(
        self, original: cst.ImportFrom, updated: cst.ImportFrom
    ) -> cst.ImportFrom:
        if updated.module is None or isinstance(updated.names, cst.ImportStar):
            return updated
        if _dotted(updated.module) != self.old_module:
            return updated
        new_names = []
        changed = False
        for alias in updated.names:
            if alias.name.value == self.old_leaf:
                changed = True
                alias = alias.with_changes(name=cst.Name(self.new_leaf))
            new_names.append(alias)
        if not changed:
            return updated
        self.sites += 1
        module = updated.module
        if self.new_module and self.new_module != self.old_module:
            module = _module_expr(self.new_module)
        return updated.with_changes(names=new_names, module=module)


# ─────────────────────────────────────────────────────────────────────────────
# Application
# ─────────────────────────────────────────────────────────────────────────────


class RewriteModuleCodemod(cst.CSTTransformer):
    """Move a whole module namespace: ``OLD`` (and any ``OLD.sub``) → ``NEW`` (``NEW.sub``),
    regardless of which symbols are imported. Unlike RewriteImportCodemod (a single
    symbol), this is what a framework succession needs — e.g. one rule moves all of
    ``keras`` → ``tensorflow.keras`` (keras.layers, keras.models, keras.backend, …).

      from OLD[.sub] import a, b   →  from NEW[.sub] import a, b
      import OLD[.sub] as x        →  import NEW[.sub] as x
      import OLD                   →  import NEW as OLD   (preserve the bound name)

    A bare dotted ``import OLD.sub`` (no alias) is left untouched: rebinding it safely is
    ambiguous, so it surfaces as an unmigrated site rather than a wrong rewrite.
    """

    def __init__(self, old_module: str, new_module: str) -> None:
        self.old_module = old_module
        self.new_module = new_module
        self.sites = 0

    def _remap(self, dotted: str | None) -> str | None:
        if dotted == self.old_module:
            return self.new_module
        if dotted and dotted.startswith(self.old_module + "."):
            return self.new_module + dotted[len(self.old_module):]
        return None

    def leave_ImportFrom(
        self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom
    ) -> cst.ImportFrom:
        if updated_node.module is None:
            return updated_node
        remapped = self._remap(_dotted(updated_node.module))
        if remapped is None:
            return updated_node
        self.sites += 1
        return updated_node.with_changes(module=_module_expr(remapped))

    def leave_Import(self, original_node: cst.Import, updated_node: cst.Import) -> cst.Import:
        new_names: list[cst.ImportAlias] = []
        changed = False
        for alias in updated_node.names:
            dotted = _dotted(alias.name)
            remapped = self._remap(dotted)
            if remapped is None:
                new_names.append(alias)
                continue
            if alias.asname is not None:
                new_names.append(alias.with_changes(name=_module_expr(remapped)))
                changed = True
                self.sites += 1
            elif dotted == self.old_module and "." not in self.old_module:
                # `import OLD` → `import NEW as OLD`, so `OLD.x` references keep working.
                new_names.append(alias.with_changes(
                    name=_module_expr(remapped),
                    asname=cst.AsName(name=cst.Name(self.old_module)),
                ))
                changed = True
                self.sites += 1
            else:
                new_names.append(alias)  # bare dotted import — ambiguous rebinding, skip
        return updated_node.with_changes(names=new_names) if changed else updated_node


def _common_module(old_qualname: str, new_qualname: str) -> str:
    """Longest shared leading dotted prefix (the imported root), e.g.
    ('tensorflow.Session', 'tensorflow.compat.v1.Session') → 'tensorflow'."""
    common: list[str] = []
    for a, b in zip(old_qualname.split("."), new_qualname.split("."), strict=False):
        if a != b:
            break
        common.append(a)
    return ".".join(common)


def _attr_chain(node: cst.BaseExpression) -> tuple[cst.Name | None, str]:
    """For an attribute expression, return (leftmost Name, dotted tail after it).
    ``tf.train.Optimizer`` → (Name('tf'), 'train.Optimizer'); non-attribute → (None, '')."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, cst.Attribute):
        parts.append(cur.attr.value)
        cur = cur.value
    if not isinstance(cur, cst.Name):
        return None, ""
    parts.reverse()
    return cur, ".".join(parts)


class RewriteAttrCodemod(cst.CSTTransformer):
    """Insert/rewrite a module segment inside an attribute chain: ``m.OLD_TAIL`` →
    ``m.NEW_TAIL`` where ``m`` is bound to ``import ROOT [as m]``. This is the form
    a leaf rename / import move can't express — e.g. ``tensorflow.Session`` →
    ``tensorflow.compat.v1.Session`` rewrites ``tf.Session(...)`` to
    ``tf.compat.v1.Session(...)`` (what tf_upgrade_v2 does for TF1→TF2). Binding-aware
    (ScopeProvider): only true references to the imported module are moved.
    """

    METADATA_DEPENDENCIES = (ScopeProvider,)

    def __init__(self, old_qualname: str, new_qualname: str) -> None:
        self.root = _common_module(old_qualname, new_qualname)
        self.old_tail = old_qualname[len(self.root) + 1:] if self.root else ""
        self.new_tail = new_qualname[len(self.root) + 1:] if self.root else ""
        self.sites = 0

    def _bound_to_root(self, name_node: cst.Name) -> bool:
        try:
            scope = self.get_metadata(ScopeProvider, name_node)
        except KeyError:
            return False
        if scope is None:
            return False
        try:
            bindings = list(scope[name_node.value])
        except KeyError:
            return False
        for b in bindings:
            node = getattr(b, "node", None)
            if isinstance(b, ImportAssignment) and isinstance(node, cst.Import):
                for alias in node.names:
                    bound = alias.evaluated_alias or alias.evaluated_name
                    if alias.evaluated_name == self.root and bound == name_node.value:
                        return True
        return False

    def leave_Attribute(
        self, original_node: cst.Attribute, updated_node: cst.Attribute
    ) -> cst.BaseExpression:
        if not self.old_tail:
            return updated_node
        base, tail = _attr_chain(original_node)
        if base is None or tail != self.old_tail or not self._bound_to_root(base):
            return updated_node
        self.sites += 1
        expr: cst.BaseExpression = cst.Name(base.value)
        for seg in self.new_tail.split("."):
            expr = cst.Attribute(value=expr, attr=cst.Name(seg))
        return expr


def apply_pattern(source: str, pattern: CodemodPattern) -> tuple[str, int]:
    """Apply one pattern to source. Returns (new_source, sites_changed)."""
    module = cst.parse_module(source)
    if pattern.kind == "rewrite-attr":
        # Attribute-path insertion (m.OLD_TAIL → m.NEW_TAIL); binding-aware.
        transformer = RewriteAttrCodemod(pattern.old_qualname, pattern.new_qualname)
        return MetadataWrapper(module).visit(transformer).code, transformer.sites
    if pattern.kind == "rewrite-module":
        # Module-scoped: old_qualname/new_qualname are module names (not module.leaf).
        transformer = RewriteModuleCodemod(pattern.old_qualname, pattern.new_qualname)
        return module.visit(transformer).code, transformer.sites
    if pattern.kind == "rewrite-import":
        # Module-scoped (matches `from old_module import leaf`) — no over-match,
        # so it needs no scope metadata.
        transformer = RewriteImportCodemod(
            _module(pattern.old_qualname),
            _module(pattern.new_qualname),
            _leaf(pattern.old_qualname),
        )
        return module.visit(transformer).code, transformer.sites

    # rename-call is binding-aware → run through a MetadataWrapper so the
    # ScopeProvider is resolved and only true bindings are rewritten.
    rename = RenameCallCodemod(pattern.old_qualname, pattern.new_qualname)
    new_module = MetadataWrapper(module).visit(rename)
    return new_module.code, rename.sites


def apply_patterns(
    source: str, patterns: list[CodemodPattern]
) -> tuple[str, list[tuple[CodemodPattern, int]]]:
    """Apply a sequence of patterns. Returns (new_source, [(pattern, sites)…])."""
    hits: list[tuple[CodemodPattern, int]] = []
    for pattern in patterns:
        source, sites = apply_pattern(source, pattern)
        if sites:
            hits.append((pattern, sites))
    return source, hits


def _emit(sink: ProgressSink, path: Path, hits: list[tuple] | None = None) -> None:
    """Report one scanned file to a progress sink, if there is one.

    A misbehaving observer must not take the walk down with it — a progress
    view is never worth failing a preview over.
    """
    if sink is None:
        return
    hits = hits or []
    try:
        sink(
            PreviewProgress(
                path=str(path),
                sites=sum(n for _, n in hits),
                recipes=tuple(recipe_key(item) for item, _ in hits),
            )
        )
    except Exception:  # noqa: BLE001 — an observer cannot break the walk
        log.warning("progress sink raised on %s", path, exc_info=True)


def _iter_py_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        try:
            rel = path.relative_to(root)
            if any(
                part in _SKIP_DIRS or (part.startswith(".") and part != ".")
                for part in rel.parts
            ):
                continue
        except Exception:
            if any(
                part in _SKIP_DIRS or (part.startswith(".") and part != ".")
                for part in path.parts
            ):
                continue
        yield path


@dataclass(frozen=True, slots=True)
class _PendingRewrite:
    """One fully computed source rewrite, before any project file is replaced."""

    path: Path
    original: bytes
    replacement: bytes


@dataclass(frozen=True, slots=True)
class _PreparedRewrite:
    """Same-directory files that make one rewrite atomic and reversible."""

    rewrite: _PendingRewrite
    staged: Path
    rollback: Path


def _read_source(path: Path) -> tuple[bytes, str]:
    """Read once so the rollback copy is byte-identical to the original file."""
    original = path.read_bytes()
    return original, original.decode("utf-8")


def _write_transaction_file(path: Path, payload: bytes, *, kind: str, mode: int) -> Path:
    """Create and fsync a hidden same-directory transaction file."""
    fd, raw_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.pymolt-{kind}-",
        suffix=".tmp",
    )
    temp_path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise
    return temp_path


def _cleanup_transaction_files(prepared: list[_PreparedRewrite]) -> None:
    for item in prepared:
        for path in (item.staged, item.rollback):
            try:
                path.unlink()
            except OSError:
                pass


def _commit_rewrites(rewrites: list[_PendingRewrite]) -> None:
    """Atomically apply a set of rewrites, rolling the whole set back on failure.

    Every replacement and rollback file is validated and materialized beside its
    destination before the first source file is touched.  ``os.replace`` then
    makes each individual transition atomic.  A later failure restores every
    destination already replaced in this transaction.
    """
    if not rewrites:
        return

    prepared: list[_PreparedRewrite] = []
    try:
        # Prepare the complete transaction before replacing any project source.
        for rewrite in rewrites:
            if rewrite.path.is_symlink():
                raise OSError(f"refusing to replace symlink: {rewrite.path}")
            if rewrite.path.read_bytes() != rewrite.original:
                raise RuntimeError(
                    f"file changed while codemods were being computed: {rewrite.path}"
                )

            # A generated module must parse before it is allowed into the write set.
            cst.parse_module(rewrite.replacement.decode("utf-8"))
            mode = stat.S_IMODE(rewrite.path.stat().st_mode)
            rollback = _write_transaction_file(
                rewrite.path, rewrite.original, kind="rollback", mode=mode
            )
            try:
                staged = _write_transaction_file(
                    rewrite.path, rewrite.replacement, kind="new", mode=mode
                )
            except BaseException:
                try:
                    rollback.unlink()
                except OSError:
                    pass
                raise
            prepared.append(
                _PreparedRewrite(rewrite=rewrite, staged=staged, rollback=rollback)
            )

        replaced: list[_PreparedRewrite] = []
        try:
            for item in prepared:
                # Catch edits made after preparation but before this destination's commit.
                if item.rewrite.path.read_bytes() != item.rewrite.original:
                    raise RuntimeError(
                        f"file changed while codemods were being committed: {item.rewrite.path}"
                    )
                os.replace(item.staged, item.rewrite.path)
                replaced.append(item)
        except BaseException as exc:
            rollback_errors: list[str] = []
            for item in reversed(replaced):
                try:
                    os.replace(item.rollback, item.rewrite.path)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{item.rewrite.path}: {rollback_exc}")
            if rollback_errors:
                raise RuntimeError(
                    "codemod transaction failed and rollback was incomplete: "
                    + "; ".join(rollback_errors)
                ) from exc
            raise
    finally:
        _cleanup_transaction_files(prepared)


def apply_to_repo(
    root: str | Path,
    patterns: list[CodemodPattern],
    *,
    write: bool = False,
) -> CodemodRunResult:
    """
    Apply patterns across every .py file under `root`.

    Dry-run by default (nothing written); pass write=True to persist. Files where
    no pattern matches are left untouched. Parse errors on a file are skipped
    (logged), never abort the run.
    """
    root = Path(root)
    result = CodemodRunResult(root=str(root), dry_run=not write)

    rewrites: list[_PendingRewrite] = []
    for path in _iter_py_files(root):
        result.files_scanned += 1
        try:
            original_bytes, original = _read_source(path)
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("skip %s: %s", path, exc)
            continue
        try:
            new_source, hits = apply_patterns(original, patterns)
        except Exception as exc:  # libcst parse error → skip the file
            log.warning("skip %s (parse error): %s", path, exc)
            continue
        if not hits or new_source == original:
            continue

        total_sites = sum(n for _, n in hits)
        result.changes.append(
            FileChange(
                path=str(path),
                sites=total_sites,
                patterns=[p.summary() for p, _ in hits],
            )
        )
        result.patterns_applied += total_sites
        rewrites.append(
            _PendingRewrite(
                path=path,
                original=original_bytes,
                replacement=new_source.encode("utf-8"),
            )
        )

    if write:
        _commit_rewrites(rewrites)

    return result


def preview_repo(
    root: str | Path,
    patterns: list[CodemodPattern],
    *,
    on_file: ProgressSink = None,
) -> list[FilePreview]:
    """
    Compute per-file before/after for every file the patterns would change —
    without writing anything. This allows caller interfaces to render diff cards so the
    engineer can accept/reject each application individually.

    `on_file`, if given, is called once per file *scanned* — including files
    that matched nothing or could not be parsed — so a live view can show the
    real denominator instead of only the handful of hits. It is a pure
    observer: it can never change what the walk returns.
    """
    root = Path(root)
    previews: list[FilePreview] = []
    for path in _iter_py_files(root):
        try:
            _original_bytes, original = _read_source(path)
        except (OSError, UnicodeDecodeError):
            _emit(on_file, path)
            continue
        try:
            new_source, hits = apply_patterns(original, patterns)
        except Exception as exc:  # libcst parse error → skip
            log.warning("skip %s (parse error): %s", path, exc)
            _emit(on_file, path)
            continue
        if not hits or new_source == original:
            _emit(on_file, path)
            continue
        previews.append(
            FilePreview(
                path=str(path),
                old_source=original,
                new_source=new_source,
                sites=sum(n for _, n in hits),
                patterns=[p for p, _ in hits],
            )
        )
        _emit(on_file, path, hits)
    return previews


def write_preview(preview: FilePreview) -> None:
    """Persist a single reviewed preview's new source to disk (on Accept)."""
    write_previews([preview])


def write_previews(previews: list[FilePreview]) -> None:
    """Persist an exact reviewed preview batch in one rollback boundary.

    Unlike re-running recipes, this writes precisely the source the engineer
    reviewed. Every file must still match ``old_source``; otherwise the entire
    batch is rejected before any destination is replaced.
    """
    rewrites: list[_PendingRewrite] = []
    for preview in previews:
        if preview.old_source == preview.new_source:
            continue
        path = Path(preview.path)
        original, source = _read_source(path)
        if source != preview.old_source:
            raise RuntimeError(
                f"file changed since the codemod preview was computed: {path}"
            )
        rewrites.append(
            _PendingRewrite(
                path=path,
                original=original,
                replacement=preview.new_source.encode("utf-8"),
            )
        )
    _commit_rewrites(rewrites)
# ─────────────────────────────────────────────────────────────────────────────
# Tier-2: rules (declarative match/rewrite + advisories)
# ─────────────────────────────────────────────────────────────────────────────


def _rule_summary(rule: CodemodRule) -> str:
    """A one-line summary of a rule for FileChange.patterns (mirrors
    CodemodPattern.summary())."""
    if rule.old_qualname and rule.new_qualname:
        target = f"{rule.old_qualname} → {rule.new_qualname}"
    elif rule.rewrite:
        target = f"{rule.match} → {rule.rewrite[0]}"
    else:
        target = rule.match
    return f"[{rule.kind or 'template'}] {target}  ({rule.confidence})"


def apply_rules(
    source: str,
    rules: list[CodemodRule],
    *,
    allow: Callable[[CodemodRule], bool] | None = None,
) -> tuple[str, list[tuple[CodemodRule, int]], list[CodemodRule], list[RuleAdvisory]]:
    """
    Apply `rules` to `source` sequentially — each rule is fed the PREVIOUS
    rule's output source (see `rules.apply_rule_detailed`).

    Returns (new_source, hits, touched, advisories):
      - hits: (rule, sites) for every rule whose rewrite was ADOPTED into the
        chained source
      - touched: every rule that either rewrote or raised an advisory, in rule
        order (for a FilePreview's info header — includes advisory-only rules)
      - advisories: every RuleAdvisory raised, regardless of `allow`

    `allow`, if given, gates whether a rule's rewrite is ADOPTED into the
    chained source — its advisories are still collected either way, and a
    blocked rewrite is simply discarded (the chain continues from the
    unmodified source). Used by `apply_rules_to_repo` to keep unverified rules
    detect-only when writing to disk. `allow=None` (the default; always used by
    `preview_rules_repo`) adopts every rewrite unconditionally.
    """
    hits: list[tuple[CodemodRule, int]] = []
    touched: list[CodemodRule] = []
    advisories: list[RuleAdvisory] = []
    for rule in rules:
        app = apply_rule_detailed(source, rule)
        if app.sites or app.advisories:
            touched.append(rule)
        advisories.extend(app.advisories)
        if app.sites and (allow is None or allow(rule)):
            hits.append((rule, app.sites))
            source = app.new_source
    return source, hits, touched, advisories


def apply_rules_to_repo(
    root: str | Path,
    rules: list[CodemodRule],
    *,
    write: bool = False,
) -> CodemodRunResult:
    """
    Apply rules across every .py file under `root`, sequentially chaining each
    rule's output source into the next.

    Dry-run by default (nothing written); pass write=True to persist. Auto-
    apply policy: when write=True, only confidence=="verified" rules may
    modify a file — heuristic (unverified) rules still run so their advisories
    surface, but their rewrite is never adopted/written. In dry-run every
    rule's rewrite is previewed regardless of confidence. `advisories_by_file`
    is populated for every file that raised >=1 advisory, even one with zero
    rewrites. Parse errors (or a malformed rule) on a file are skipped
    (logged), never abort the run.
    """
    root = Path(root)
    result = CodemodRunResult(root=str(root), dry_run=not write)
    allow = (lambda rule: rule.confidence == "verified") if write else None

    rewrites: list[_PendingRewrite] = []
    for path in _iter_py_files(root):
        result.files_scanned += 1
        try:
            original_bytes, original = _read_source(path)
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("skip %s: %s", path, exc)
            continue
        try:
            new_source, hits, _touched, advisories = apply_rules(original, rules, allow=allow)
        except Exception as exc:  # malformed rule / libcst parse error → skip
            log.warning("skip %s (rule error): %s", path, exc)
            continue

        if advisories:
            result.advisories_by_file[str(path)] = advisories
        if not hits or new_source == original:
            continue

        total_sites = sum(n for _, n in hits)
        result.changes.append(
            FileChange(
                path=str(path),
                sites=total_sites,
                patterns=[_rule_summary(r) for r, _ in hits],
                advisories=advisories,
            )
        )
        result.patterns_applied += total_sites
        rewrites.append(
            _PendingRewrite(
                path=path,
                original=original_bytes,
                replacement=new_source.encode("utf-8"),
            )
        )

    if write:
        _commit_rewrites(rewrites)

    return result


def apply_mixed_to_repo(
    root: str | Path,
    rules: list[CodemodRule],
    patterns: list[CodemodPattern],
    *,
    write: bool = False,
) -> CodemodRunResult:
    """Apply Tier-2 rules then legacy patterns in one repository transaction.

    The two recipe sets belong to disjoint packages, but may still touch the
    same source file. Computing both passes in memory before one commit keeps a
    mixed server response just as atomic as a rules-only or patterns-only run.
    """
    root = Path(root)
    result = CodemodRunResult(root=str(root), dry_run=not write)
    allow = (lambda rule: rule.confidence == "verified") if write else None
    rewrites: list[_PendingRewrite] = []

    for path in _iter_py_files(root):
        result.files_scanned += 1
        try:
            original_bytes, original = _read_source(path)
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("skip %s: %s", path, exc)
            continue

        current = original
        rule_hits: list[tuple[CodemodRule, int]] = []
        advisories: list[RuleAdvisory] = []
        try:
            current, rule_hits, _touched, advisories = apply_rules(
                current, rules, allow=allow
            )
        except Exception as exc:
            log.warning("skip rules for %s (rule error): %s", path, exc)

        pattern_hits: list[tuple[CodemodPattern, int]] = []
        try:
            current, pattern_hits = apply_patterns(current, patterns)
        except Exception as exc:
            log.warning("skip patterns for %s (parse error): %s", path, exc)

        if advisories:
            result.advisories_by_file[str(path)] = advisories
        if current == original or not (rule_hits or pattern_hits):
            continue

        sites = sum(count for _, count in rule_hits) + sum(
            count for _, count in pattern_hits
        )
        summaries = [_rule_summary(rule) for rule, _ in rule_hits]
        summaries.extend(pattern.summary() for pattern, _ in pattern_hits)
        result.changes.append(
            FileChange(
                path=str(path), sites=sites, patterns=summaries,
                advisories=advisories,
            )
        )
        result.patterns_applied += sites
        rewrites.append(
            _PendingRewrite(
                path=path,
                original=original_bytes,
                replacement=current.encode("utf-8"),
            )
        )

    if write:
        _commit_rewrites(rewrites)
    return result


def preview_mixed_repo(
    root: str | Path,
    rules: list[CodemodRule],
    patterns: list[CodemodPattern],
    *,
    on_file: ProgressSink = None,
    verified_only: bool = False,
) -> list[FilePreview]:
    """Preview the exact chained output that :func:`apply_mixed_to_repo` writes."""
    root = Path(root)
    previews: list[FilePreview] = []
    allow = (lambda rule: rule.confidence == "verified") if verified_only else None
    for path in _iter_py_files(root):
        try:
            _original_bytes, original = _read_source(path)
        except (OSError, UnicodeDecodeError):
            _emit(on_file, path)
            continue
        try:
            after_rules, rule_hits, touched, advisories = apply_rules(
                original, rules, allow=allow
            )
            new_source, pattern_hits = apply_patterns(after_rules, patterns)
        except Exception as exc:
            log.warning("skip %s (mixed preview error): %s", path, exc)
            _emit(on_file, path)
            continue
        if not (touched or pattern_hits):
            _emit(on_file, path)
            continue
        previews.append(
            FilePreview(
                path=str(path),
                old_source=original,
                new_source=new_source,
                sites=sum(count for _, count in rule_hits)
                + sum(count for _, count in pattern_hits),
                patterns=[pattern for pattern, _ in pattern_hits],
                rules=touched,
                advisories=advisories,
            )
        )
        _emit(on_file, path, [*rule_hits, *pattern_hits])
    return previews


def preview_rules_repo(
    root: str | Path,
    rules: list[CodemodRule],
    *,
    on_file: ProgressSink = None,
    verified_only: bool = False,
) -> list[FilePreview]:
    """
    Compute per-file before/after for every file the rules would change OR
    advise on — without writing anything (confidence never gates a preview).

    A file with only advisories and no text change still yields a preview
    (old_source == new_source, sites=0, advisories set) so caller interfaces can render
    the manual-review card instead of silently dropping it.
    """
    root = Path(root)
    previews: list[FilePreview] = []
    allow = (lambda rule: rule.confidence == "verified") if verified_only else None
    for path in _iter_py_files(root):
        try:
            _original_bytes, original = _read_source(path)
        except (OSError, UnicodeDecodeError):
            _emit(on_file, path)
            continue
        try:
            new_source, hits, touched, advisories = apply_rules(
                original, rules, allow=allow
            )
        except Exception as exc:  # malformed rule / libcst parse error → skip
            log.warning("skip %s (rule error): %s", path, exc)
            _emit(on_file, path)
            continue
        if not touched:
            _emit(on_file, path)
            continue
        previews.append(
            FilePreview(
                path=str(path),
                old_source=original,
                new_source=new_source,
                sites=sum(n for _, n in hits),
                rules=touched,
                advisories=advisories,
            )
        )
    return previews
