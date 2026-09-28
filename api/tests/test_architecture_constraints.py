"""Constraint A of `docs/architecture/codebase-design.md` §6, as an automated test.

        **约束 A：`domain/` 绝不 import `ai/`**

        AI is the capability edge and the business rules are the kernel. The dependency
        direction is one way: `ai → domain`.

§6 says the three constraints 「必须写成自动化测试的」 — must be written as automated tests, and
that violating one is a build failure. This module is constraint A's test, and it arrives
with ticket 38 because ticket 38 is what created `app/ai/`: until there was an `ai` package
to import, "does anything in `domain/` import it" had no answer worth asserting.

**What §8 records about the other two, so that this file is not read as claiming them.**
Constraint B (「AI 模块不持有写仓储」) has three layers and §8 assigns its *structural* layer to
ticket 40, together with the runtime read-only context (41) and the read-only database role.
Constraint C (「查询的权限条件不可绕过」) lands with ticket 35, which built the pre-retrieval
permission pushdown and its tests. Neither is asserted here.

**Why an `ast` walk and not an import of the package.** Importing every domain module to
inspect it would execute them — which is the behaviour under test's own risk, and which
needs a database for some of them. Parsing the source costs nothing, works on a module that
cannot be imported in this process, and answers exactly the question asked: does the text of
`app/domain/**` name `app.ai` anywhere.

**The walker is given a positive control.** A test that only asserts "no file imports ai"
passes when the walker is broken and finds nothing, so `test_the_walker_catches_a_module_
that_imports_ai` feeds the resolver a module that does import it and asserts it is caught —
including through a relative import, which is the form a walker written against
`ast.ImportFrom.module` alone gets wrong. `test_the_walk_is_not_empty` pins the other half:
the walk must find the domain package, and `app.domain.answer.driver` in particular.
"""

import ast
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
DOMAIN_ROOT = API_ROOT / "app" / "domain"

#: The package the constraint forbids `domain/` from reaching.
AI_PACKAGE = "app.ai"

#: The package the constraint protects.
DOMAIN_PACKAGE = "app.domain"

#: How many modules the walk must find at least. Well below the 120-odd that exist, so that
#: ordinary work — a module added, a package split — never trips it, and high enough that a
#: walker which silently found one file fails. See `test_the_walk_is_not_empty`.
MINIMUM_DOMAIN_MODULES = 40

#: Modules that must be in the walk, whatever the count. Named because they are the three
#: the constraint is most likely to be broken through: the answer pipeline (which the AI
#: package calls *into*), the permission filter and the kernel.
ANCHOR_MODULES = (
    "app.domain.answer.driver",
    "app.domain.retrieval.filtering",
    "app.domain.access.kernel",
)


def module_and_package_of(path: Path) -> tuple[str, str]:
    """`…/answer/driver.py` → `("app.domain.answer.driver", "app.domain.answer")`.

    Two names, because a relative import is resolved against the *package* the importing
    module lives in, and that is the file's **directory** — not its own name with the last
    component removed. The two differ exactly where it matters: for
    `app/domain/__init__.py` the module is `app.domain` and its package is also
    `app.domain`, so `from .answer import driver` is `app.domain.answer.driver` rather than
    `app.answer.driver`. A walker that derives the package by stripping the module's last
    component gets every `__init__.py` wrong.
    """
    parts = list(path.relative_to(API_ROOT).with_suffix("").parts)
    package = ".".join(parts[:-1])
    if parts[-1] == "__init__":
        return package, package
    return ".".join(parts), package


def imported_modules(source: str, package: str) -> set[str]:
    """Every dotted module name `source` imports, resolved from `package`'s position.

    `package` is what `__package__` would be for the file the source came from — see
    `module_and_package_of`.

    Three forms, and all three matter:

    * `import app.ai.agents` and `import app.ai.agents as agents` — `ast.Import`;
    * `from app.ai.agents import build_graph` — `ast.ImportFrom`, where the *module* is what
      must be tested but the imported names are candidates too, because `from app import ai`
      names the forbidden package only in its `alias`;
    * `from ..ai.agents import build_graph` — a relative import, resolved against the
      package the importing module lives in. Level 1 is that package itself, level 2 its
      parent, and so on; a walker that ignores `level` sees `ai.agents` and concludes it is
      not `app.ai`, which is exactly how a real violation would slip through.
    """
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolved_base(package, node)
            imported.add(base)
            imported.update(f"{base}.{alias.name}" for alias in node.names)
    return imported


def _resolved_base(package: str, node: ast.ImportFrom) -> str:
    """The module an `ImportFrom` imports from, with a relative import made absolute."""
    if node.level == 0:
        return node.module or ""
    # `package` is the importing file's own package; `level` counts upward from it, so
    # level 1 stays where it is and level 2 goes to the parent.
    parts = package.split(".") if package else []
    keep = len(parts) - (node.level - 1)
    base = ".".join(parts[: max(keep, 0)])
    if not node.module:
        return base
    return f"{base}.{node.module}" if base else node.module


def reaches_ai(name: str) -> bool:
    """Whether a module name is `app.ai` or something inside it."""
    return name == AI_PACKAGE or name.startswith(f"{AI_PACKAGE}.")


def domain_sources() -> dict[str, str]:
    """Every module under `app/domain/`, by dotted name. See the module docstring."""
    return {
        name: path.read_text(encoding="utf-8")
        for name, path in sorted(
            (module_and_package_of(path)[0], path) for path in DOMAIN_ROOT.rglob("*.py")
        )
    }


def domain_modules_reaching_ai() -> dict[str, set[str]]:
    """The offending modules, mapped to the names that reached `app.ai`. Empty is a pass."""
    offenders: dict[str, set[str]] = {}
    for path in sorted(DOMAIN_ROOT.rglob("*.py")):
        name, package = module_and_package_of(path)
        source = path.read_text(encoding="utf-8")
        reached = {found for found in imported_modules(source, package) if reaches_ai(found)}
        if reached:
            offenders[name] = reached
    return offenders


def test_the_walk_is_not_empty() -> None:
    """A walker that finds nothing must fail rather than pass. See the module docstring."""
    found = domain_sources()
    assert len(found) >= MINIMUM_DOMAIN_MODULES, (
        f"the walk found {len(found)} modules under {DOMAIN_ROOT}, which is fewer than the "
        f"{MINIMUM_DOMAIN_MODULES} it must find: the walker is broken, and every assertion "
        "below it would pass vacuously"
    )
    missing = [name for name in ANCHOR_MODULES if name not in found]
    assert not missing, f"the walk did not reach {missing}, so it is not walking the package"


def test_no_domain_module_imports_the_ai_package() -> None:
    """Constraint A. See the module docstring for what it buys and what it does not."""
    offenders = domain_modules_reaching_ai()
    assert not offenders, (
        "constraint A is violated: `domain/` must never import `ai/` — the dependency is "
        f"one way (ai → domain). Offenders: {offenders}"
    )


def test_the_walker_catches_a_module_that_imports_ai() -> None:
    """The positive control: the same resolver, on source that *is* a violation.

    Four forms, because they are four different branches of `imported_modules`: an absolute
    `from` import, a bare `import`, `from app import ai` (where the forbidden package is an
    alias rather than the module), and a relative one. The last is the one a naive walker
    misses, and it is a form a developer inside `app/domain/answer/` would naturally write.
    """
    cases = {
        "absolute from": ("from app.ai.agents import build_graph", "app.domain.answer"),
        "absolute import": ("import app.ai.tools", "app.domain"),
        "from app import ai": ("from app import ai", "app.domain"),
        # `app/domain/probe.py`: its package is `app.domain`, so `..` is `app`.
        "relative": ("from ..ai.agents import build_graph", "app.domain"),
        # `app/domain/answer/probe.py`: `...` climbs past `app.domain.answer` to `app`.
        "relative, deep": ("from ...ai import agents", "app.domain.answer"),
    }
    for label, (source, package) in cases.items():
        reached = {name for name in imported_modules(source, package) if reaches_ai(name)}
        assert reached, f"the walker missed a violation written as a {label}: {source!r}"


def test_the_walker_does_not_flag_an_ordinary_domain_import() -> None:
    """The negative control for the resolver: a domain import is not a violation.

    Without this, a resolver that returned every dotted name prefixed with `app.ai` — or
    that ignored the level of a relative import and resolved `from .errors import X` to
    something outside the package — would pass the control above and fail the suite with a
    false positive, and the natural fix for a false positive is to weaken the test.
    """
    source = (
        "from app.domain.errors import Something\n"
        "from app.domain.answer import driver\n"
        "from .errors import Other\n"
        "import app.domain.retrieval.service\n"
    )
    reached = {
        name for name in imported_modules(source, "app.domain.answer") if reaches_ai(name)
    }
    assert not reached, f"an ordinary domain import was flagged as reaching ai: {reached}"


def test_the_resolver_resolves_relative_imports_to_the_right_package() -> None:
    """`from .x import y` inside `app/domain/answer/` is `app.domain.answer.x`.

    Asserted directly rather than only through the two controls, because the level
    arithmetic is the part of this file most likely to be wrong in a way that still passes a
    coarse "did anything reach app.ai" check.
    """
    resolved = imported_modules("from .sibling import thing", "app.domain.answer")
    assert "app.domain.answer.sibling" in resolved, resolved
    resolved_up = imported_modules("from ..retrieval import filtering", "app.domain.answer")
    assert "app.domain.retrieval.filtering" in resolved_up, resolved_up
    # The `__init__.py` case: the module *is* the package, so level 1 does not climb.
    module, package = module_and_package_of(DOMAIN_ROOT / "answer" / "__init__.py")
    assert (module, package) == ("app.domain.answer", "app.domain.answer"), (module, package)


def test_the_ai_package_exists_and_domain_is_the_only_protected_side() -> None:
    """The constraint is about a real package, and the direction is the one §6 states.

    `app/ai` must exist for the walk above to mean anything (a walk that finds no `ai`
    package would be asserting about a name nothing uses), and `app/ai/**` importing
    `app/domain/**` is the *permitted* direction — asserted here so that a future reader who
    moves a domain module into `app/ai` sees which way round the rule is.
    """
    ai_root = API_ROOT / "app" / "ai"
    assert ai_root.is_dir(), "app/ai does not exist, so constraint A has nothing to protect"
    nodes = (ai_root / "agents" / "nodes.py").read_text(encoding="utf-8")
    assert f"from {DOMAIN_PACKAGE}." in nodes, (
        "the AI package is expected to import the domain package (ai → domain is the "
        "permitted direction); if it no longer does, this test's premise has changed"
    )
