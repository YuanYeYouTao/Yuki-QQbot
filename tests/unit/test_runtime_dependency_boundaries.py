"""AST-level dependency boundary enforcement for the 3.6.0 runtime (R1 §7).

Scanned guarantees:

- ``qq_ai_bot.runtime`` never imports ``qq_ai_bot.planner`` — not even under
  ``TYPE_CHECKING`` — and never runtime-imports conversation / capabilities /
  memory / plugin_host / application / services (annotation-only
  ``TYPE_CHECKING`` references to those value types are the one allowed
  exception).
- ``qq_ai_bot.conversation`` and ``qq_ai_bot.memory.runtime`` never import
  ``qq_ai_bot.planner``.
- ``qq_ai_bot.memory.runtime`` never imports ``qq_ai_bot.capabilities`` and
  ``qq_ai_bot.capabilities`` never imports memory implementation: both sides
  interact only through the pure data contracts in ``qq_ai_bot.runtime``.
- ``qq_ai_bot.capabilities`` files added by R1 never import
  ``qq_ai_bot.planner``; pre-existing legacy files live on the explicit
  allowlist below.

The scan covers plain imports, relative imports, ``TYPE_CHECKING`` blocks and
dynamic ``importlib.import_module(...)`` / ``__import__(...)`` string
constants.  ``ALLOWED_LEGACY_IMPORTS`` may only shrink between rounds (R2-R5)
and must be empty by R5; a stale entry fails the suite so shrinkage is forced,
not aspirational.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

SRC_ROOT = Path(__file__).parents[2] / "src" / "qq_ai_bot"

# file (posix, relative to src/qq_ai_bot) -> forbidden prefixes it may still
# use.  Only remove entries; never add one without a plan-document deviation.
ALLOWED_LEGACY_IMPORTS: dict[str, frozenset[str]] = {}


@dataclass(frozen=True)
class BoundaryRule:
    """One scanned scope with its forbidden import prefixes."""

    scope: str
    forbidden: tuple[str, ...]
    # True: only runtime imports are forbidden (TYPE_CHECKING is allowed).
    runtime_only: bool
    reason: str


RULES: tuple[BoundaryRule, ...] = (
    BoundaryRule(
        scope="runtime",
        forbidden=("qq_ai_bot.planner",),
        runtime_only=False,
        reason="runtime owns the planner-free vocabulary (R1 §7)",
    ),
    BoundaryRule(
        scope="runtime",
        forbidden=(
            "qq_ai_bot.conversation",
            "qq_ai_bot.capabilities",
            "qq_ai_bot.memory",
            "qq_ai_bot.plugin_host",
            "qq_ai_bot.application",
            "qq_ai_bot.services",
        ),
        runtime_only=True,
        reason="runtime may reference upper layers only as TYPE_CHECKING annotations",
    ),
    BoundaryRule(
        scope="conversation",
        forbidden=("qq_ai_bot.planner",),
        runtime_only=False,
        reason="conversation runtime files must be planner-free (R1 §7)",
    ),
    BoundaryRule(
        scope="memory/runtime",
        forbidden=("qq_ai_bot.planner", "qq_ai_bot.capabilities"),
        runtime_only=False,
        reason="memory runtime talks to capabilities only via runtime.contracts",
    ),
    BoundaryRule(
        scope="capabilities",
        forbidden=("qq_ai_bot.planner", "qq_ai_bot.memory"),
        runtime_only=False,
        reason="capability runtime is planner-free and never touches memory implementation",
    ),
)


@dataclass(frozen=True)
class ImportRecord:
    module: str
    lineno: int
    type_checking: bool


@dataclass(frozen=True)
class Violation:
    file: str
    module: str
    lineno: int
    type_checking: bool
    reason: str


def _module_package(path: Path) -> str:
    """Return the package that relative imports in ``path`` resolve against."""

    relative = path.relative_to(SRC_ROOT.parent)
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    else:
        parts = parts[:-1]
    return ".".join(parts)


class _ImportCollector(ast.NodeVisitor):
    def __init__(self, module_package: str) -> None:
        self.records: list[ImportRecord] = []
        self._package = module_package
        self._type_checking_depth = 0

    @staticmethod
    def _is_type_checking_test(test: ast.expr) -> bool:
        if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
            return True
        return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"

    def _record(self, module: str, lineno: int) -> None:
        if module:
            self.records.append(
                ImportRecord(
                    module=module,
                    lineno=lineno,
                    type_checking=self._type_checking_depth > 0,
                )
            )

    def visit_If(self, node: ast.If) -> None:
        if self._is_type_checking_test(node.test):
            self._type_checking_depth += 1
            for child in node.body:
                self.visit(child)
            self._type_checking_depth -= 1
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record(alias.name, node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0:
            self._record(node.module or "", node.lineno)
            return
        base_parts = self._package.split(".")
        anchor = base_parts[: len(base_parts) - (node.level - 1)]
        if node.module:
            anchor = [*anchor, *node.module.split(".")]
        self._record(".".join(anchor), node.lineno)

    def visit_Call(self, node: ast.Call) -> None:
        target: str | None = None
        if isinstance(node.func, ast.Attribute) and node.func.attr == "import_module":
            target = _first_string_argument(node)
        elif isinstance(node.func, ast.Name) and node.func.id == "__import__":
            target = _first_string_argument(node)
        if target is not None:
            self._record(target, node.lineno)
        self.generic_visit(node)


def _first_string_argument(node: ast.Call) -> str | None:
    if node.args and isinstance(node.args[0], ast.Constant):
        value = node.args[0].value
        if isinstance(value, str):
            return value
    return None


def _scan_file(path: Path) -> tuple[ImportRecord, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    collector = _ImportCollector(_module_package(path))
    collector.visit(tree)
    return tuple(collector.records)


def _scope_files(scope: str) -> tuple[Path, ...]:
    root = SRC_ROOT / scope
    assert root.is_dir(), f"scanned package moved or disappeared: {scope}"
    files = tuple(sorted(root.rglob("*.py")))
    assert files, f"scanned package is empty: {scope}"
    return files


def _matches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _collect_violations() -> list[Violation]:
    violations: list[Violation] = []
    for rule in RULES:
        for path in _scope_files(rule.scope):
            file_key = path.relative_to(SRC_ROOT).as_posix()
            for record in _scan_file(path):
                if rule.runtime_only and record.type_checking:
                    continue
                if not any(_matches(record.module, prefix) for prefix in rule.forbidden):
                    continue
                violations.append(
                    Violation(
                        file=file_key,
                        module=record.module,
                        lineno=record.lineno,
                        type_checking=record.type_checking,
                        reason=rule.reason,
                    )
                )
    return violations


def _is_allowlisted(violation: Violation) -> bool:
    allowed = ALLOWED_LEGACY_IMPORTS.get(violation.file, frozenset())
    return any(_matches(violation.module, prefix) for prefix in allowed)


def test_runtime_boundaries_hold_outside_the_legacy_allowlist() -> None:
    unexpected = [
        violation for violation in _collect_violations() if not _is_allowlisted(violation)
    ]
    details = "\n".join(
        f"  {violation.file}:{violation.lineno} imports {violation.module}"
        f" ({'TYPE_CHECKING' if violation.type_checking else 'runtime'}) — {violation.reason}"
        for violation in unexpected
    )
    assert not unexpected, f"forbidden imports crossed a runtime boundary:\n{details}"


def test_legacy_allowlist_only_shrinks() -> None:
    """Every allowlist entry must still be required; delete stale ones."""

    violations = _collect_violations()
    for file_key, prefixes in ALLOWED_LEGACY_IMPORTS.items():
        for prefix in sorted(prefixes):
            still_used = any(
                violation.file == file_key and _matches(violation.module, prefix)
                for violation in violations
            )
            assert still_used, (
                f"stale allowlist entry: {file_key} no longer imports {prefix};"
                " remove it so the allowlist keeps shrinking"
            )


def test_runtime_package_init_exports_nothing() -> None:
    """The runtime __init__ intentionally re-exports nothing (R1 commit 1)."""

    tree = ast.parse((SRC_ROOT / "runtime" / "__init__.py").read_text(encoding="utf-8"))
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert imports == [], "runtime/__init__.py must stay import-free to prevent cycles"


def test_scan_actually_sees_the_new_runtime_modules() -> None:
    """Guard the scanner itself: an empty scan must fail loudly."""

    runtime_files = {path.name for path in _scope_files("runtime")}
    assert {
        "origin.py",
        "trigger.py",
        "keys.py",
        "authority.py",
        "turn.py",
        "invariants.py",
        "observability.py",
        "contracts.py",
    } <= runtime_files
    memory_runtime_files = {path.name for path in _scope_files("memory/runtime")}
    assert {"contract.py", "session.py", "resolver.py", "capability_view.py"} <= (
        memory_runtime_files
    )
