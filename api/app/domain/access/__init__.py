"""The authorization kernel: one place that decides what a principal may do."""

from app.domain.access.kernel import (
    CLEARANCE_RANK,
    PROJECT_ACTIVE_STATUS,
    Decision,
    FilterSpec,
    Reason,
    Resource,
    ResourceKind,
    apply_rls_context,
    can,
    department_scope_ids,
    filter_for,
)
from app.domain.access.permissions import (
    PROJECT_ADMIN_ROLES,
    RULES,
    Action,
    ActionRule,
    roles_may,
    rule_for,
)
from app.domain.access.principal import PRIVILEGED_ROLES, SYSTEM_ROLES, Principal
from app.domain.access.snapshot import (
    SNAPSHOT_TTL_SECONDS,
    AccountFacts,
    PrincipalBuilder,
    invalidate_user,
    resolve_principal,
    to_viewer_context,
)

__all__ = [
    "CLEARANCE_RANK",
    "PRIVILEGED_ROLES",
    "PROJECT_ACTIVE_STATUS",
    "PROJECT_ADMIN_ROLES",
    "RULES",
    "SNAPSHOT_TTL_SECONDS",
    "SYSTEM_ROLES",
    "AccountFacts",
    "Action",
    "ActionRule",
    "Decision",
    "FilterSpec",
    "Principal",
    "PrincipalBuilder",
    "Reason",
    "Resource",
    "ResourceKind",
    "apply_rls_context",
    "can",
    "department_scope_ids",
    "filter_for",
    "invalidate_user",
    "resolve_principal",
    "roles_may",
    "rule_for",
    "to_viewer_context",
]
