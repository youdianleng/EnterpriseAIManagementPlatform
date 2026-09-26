"""Department rules.

All structural decisions live here: where a department sits in the tree, what a
move does to its subtree, and what may not be deleted. The repository only
persists; the transport layer only translates.

Every mutating method takes the caller's `invalidate` hook so that "when
structure changes, cached organisation data is stale" is expressed once, at the
point of change, instead of relying on a TTL somewhere else.
"""

from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, record
from app.domain.errors import DomainError
from app.domain.org.errors import OrgErrorCode
from app.domain.org.models import (
    Department,
    DepartmentInput,
    DepartmentNode,
    DepartmentPatch,
    DepartmentTree,
)
from app.domain.org.paths import child_path, depth_of, is_descendant_path
from app.domain.org.repository import DepartmentRepository

# Deepest depth index a department may occupy, with the root at 0.#
# The requirement is four *nested* levels (ticket 22: "部门级配置" over a
# "4 层嵌套" tree), which is depths 0..4: the root plus four levels beneath it.
# Stored and compared as an index because that is what the column holds.
MAX_DEPTH = 4

Invalidate = Callable[[], Awaitable[None]]


def build_tree(departments: list[Department]) -> DepartmentTree:
    """Assemble the tree from a flat list.

    The repository returns rows in path order, but this does not rely on it:
    parents are indexed by id first, so a caller passing any order still gets a
    correct tree.
    """
    nodes: dict[UUID, list[Department]] = {}
    by_id = {department.id: department for department in departments}
    for department in departments:
        nodes.setdefault(department.parent_id, []).append(department)  # type: ignore[index]

    def assemble(parent_id: UUID | None) -> tuple[DepartmentNode, ...]:
        children = sorted(nodes.get(parent_id, []), key=lambda item: item.code)
        return tuple(
            DepartmentNode(
                department=child,
                children=assemble(child.id),
            )
            for child in children
        )

    roots = tuple(
        DepartmentNode(department=root, children=assemble(root.id))
        for root in sorted(nodes.get(None, []), key=lambda item: item.code)
    )
    # A department whose parent is missing from the list would silently vanish
    # from the tree; surfacing it as a root keeps the tree complete.
    listed = {department.id for department in departments}
    orphans = [d for d in departments if d.parent_id is not None and d.parent_id not in listed]
    for orphan in sorted(orphans, key=lambda item: item.path):
        roots = (*roots, DepartmentNode(department=orphan, children=assemble(orphan.id)))

    return DepartmentTree(roots=roots, total=len(by_id))


class DepartmentService:
    def __init__(
        self,
        repository: DepartmentRepository,
        invalidate: Invalidate,
        session: AsyncSession | None = None,
    ) -> None:
        self._repository = repository
        self._invalidate = invalidate
        # Optional so a service can be built without a session in a unit test that
        # only exercises the tree rules. Every write path in the application
        # passes one, because every write is audited.
        self._session = session

    # --- reads -------------------------------------------------------------

    async def list_tree(self, *, include_inactive: bool = True) -> DepartmentTree:
        departments = await self._repository.list_all(include_inactive=include_inactive)
        return build_tree(departments)

    async def get(self, department_id: UUID) -> Department:
        department = await self._repository.get(department_id)
        if department is None:
            raise DomainError(
                OrgErrorCode.ORG_DEPARTMENT_NOT_FOUND, detail=f"unknown department {department_id}"
            )
        return department

    async def list_subtree(self, path: str, *, include_self: bool = True) -> list[Department]:
        return await self._repository.list_subtree(path, include_self=include_self)

    # --- writes ------------------------------------------------------------

    async def _audit(
        self,
        action: AuditAction,
        department: Department,
        *,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
    ) -> None:
        """Record one structural change, before the caller commits.

        Clearing somebody's department is how their documents become reachable to
        somebody else, so this is not bookkeeping: it is the record that answers
        "who could see this, and since when".
        """
        if self._session is None:
            return
        await record(
            self._session,
            action=action,
            entity_type="department",
            entity_id=department.id,
            before=before,
            after=after,
        )

    async def create(self, data: DepartmentInput) -> Department:
        existing = await self._repository.get_by_code(data.code)
        if existing is not None:
            raise DomainError(
                OrgErrorCode.ORG_DEPARTMENT_CODE_TAKEN, detail=f"code {data.code} already exists"
            )

        parent = await self._require_parent(data.parent_id)
        parent_path = parent.path if parent else None
        depth = depth_of(child_path(parent_path, data.code))
        if depth > MAX_DEPTH:
            raise DomainError(
                OrgErrorCode.ORG_DEPARTMENT_DEPTH_EXCEEDED,
                detail=f"depth {depth} would exceed the limit of {MAX_DEPTH}",
            )

        department = await self._repository.save(
            data, path=child_path(parent_path, data.code), depth=depth
        )
        await self._audit(
            AuditAction.DEPARTMENT_CREATED,
            department,
            after={
                "code": department.code,
                "path": department.path,
                "clearance_level": str(department.clearance_level),
            },
        )
        await self._repository.commit()
        await self._invalidate()
        return department

    async def update(self, department_id: UUID, patch: DepartmentPatch) -> Department:
        before = await self.get(department_id)
        department = await self._repository.update(department_id, patch)
        changes = patch.changes()
        await self._audit(
            AuditAction.DEPARTMENT_UPDATED,
            department,
            before={name: getattr(before, name) for name in changes},
            after={name: getattr(department, name) for name in changes},
        )
        if "clearance_level" in changes and (
            str(before.clearance_level) != str(department.clearance_level)
        ):
            # Its own record on purpose. A clearance edit changes what everybody in
            # the department can reach, and "who could see this, and since when" is
            # a question that must not require reading a diff of a rename.
            await self._audit(
                AuditAction.CLEARANCE_CHANGED,
                department,
                before={"clearance_level": str(before.clearance_level)},
                after={"clearance_level": str(department.clearance_level)},
            )
        await self._repository.commit()
        await self._invalidate()
        return department

    async def move(self, department_id: UUID, new_parent_id: UUID | None) -> Department:
        department = await self.get(department_id)

        if new_parent_id == department.id:
            raise DomainError(
                OrgErrorCode.ORG_DEPARTMENT_MOVE_INTO_DESCENDANT,
                detail="a department cannot be its own parent",
            )

        new_parent = await self._require_parent(new_parent_id)

        if new_parent is not None:
            # The whole subtree moves, so the new parent must not be inside it.
            # The guard mirrors the SQL `<@` operator used by the update below.
            if is_descendant_path(new_parent.path, department.path):
                raise DomainError(
                    OrgErrorCode.ORG_DEPARTMENT_MOVE_INTO_DESCENDANT,
                    detail=f"{new_parent.path} is inside {department.path}",
                )
            # Moving the subtree shifts everything beneath it by the same amount,
            # so the deepest existing descendant decides whether it still fits.
            subtree_height = await self._repository.subtree_height(department.path)
            resulting_depth = depth_of(f"{new_parent.path}.{department.code}") + subtree_height
            if resulting_depth > MAX_DEPTH:
                raise DomainError(
                    OrgErrorCode.ORG_DEPARTMENT_DEPTH_EXCEEDED,
                    detail=f"move would reach depth {resulting_depth}, limit is {MAX_DEPTH}",
                )

        new_path = child_path(new_parent.path if new_parent else None, department.code)

        await self._repository.update_code_and_path(
            department.id,
            code=department.code,
            path=new_path,
            depth=depth_of(new_path),
        )
        await self._repository.move_subtree(
            old_path=department.path,
            old_code=department.code,
            new_path=new_path,
            new_code=department.code,
        )
        moved = await self.get(department_id)
        await self._audit(
            AuditAction.DEPARTMENT_MOVED,
            moved,
            before={"path": department.path, "parent_id": str(department.parent_id)},
            after={"path": moved.path, "parent_id": str(moved.parent_id)},
        )
        await self._repository.commit()
        await self._invalidate()
        return moved

    async def delete(self, department_id: UUID) -> None:
        department = await self.get(department_id)

        # Employees first: "this department is in use" is the more actionable of
        # the two messages, and it is the one a human can act on.
        employees = await self._repository.count_employees(department_id)
        if employees:
            raise DomainError(
                OrgErrorCode.ORG_DEPARTMENT_NOT_EMPTY,
                detail=f"{employees} active employees are assigned to {department.code}",
            )

        children = await self._repository.count_children(department_id)
        if children:
            raise DomainError(
                OrgErrorCode.ORG_DEPARTMENT_HAS_CHILDREN,
                detail=f"{children} sub-departments hang off {department.code}",
            )

        await self._repository.delete(department_id)
        await self._audit(
            AuditAction.DEPARTMENT_DELETED,
            department,
            before={"code": department.code, "path": department.path},
        )
        await self._repository.commit()
        await self._invalidate()

    # --- internals ---------------------------------------------------------

    async def _require_parent(self, parent_id: UUID | None) -> Department | None:
        if parent_id is None:
            return None
        parent = await self._repository.get(parent_id)
        if parent is None:
            raise DomainError(
                OrgErrorCode.ORG_DEPARTMENT_PARENT_INVALID,
                detail=f"parent {parent_id} does not exist",
            )
        if not parent.is_active:
            raise DomainError(
                OrgErrorCode.ORG_DEPARTMENT_PARENT_INVALID,
                detail=f"parent {parent.code} is inactive",
            )
        return parent
