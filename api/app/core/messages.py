"""Bilingual message catalogue.

The API never returns a sentence, only a key plus an optional detail. Each
client renders the key in the user's language, which keeps one source of truth
for wording and lets a new language ship without touching the backend.

`tests/test_errors.py` asserts both catalogues cover exactly the same keys.
"""

from typing import Final

MESSAGES: Final[dict[str, dict[str, str]]] = {
    "es": {
        "errors.validation_failed": "Los datos enviados no son válidos.",
        "errors.invalid_request": "La solicitud no se pudo procesar.",
    "errors.method_not_allowed": "Este recurso no admite ese método.",
        "errors.unauthenticated": "Necesitas iniciar sesión.",
        "errors.forbidden": "No tienes permiso para esta acción.",
        "errors.account_locked": "La cuenta está bloqueada temporalmente.",
        "errors.password_change_required": "Debes cambiar la contraseña antes de continuar.",
        "errors.not_found": "No se encontró el recurso solicitado.",
        "errors.conflict": "La operación entra en conflicto con el estado actual.",
        "errors.resource_gone": "El recurso ya no está disponible.",
        "errors.department_not_found": "No se encontró el departamento.",
        "errors.department_code_taken": "Ya existe un departamento con ese código.",
        "errors.department_not_empty": "El departamento todavía tiene personal asignado.",
        "errors.department_has_children": "El departamento todavía tiene subdepartamentos.",
        "errors.department_parent_invalid":
            "El departamento superior indicado no existe o está desactivado.",
        "errors.department_move_into_descendant":
            "No se puede mover un departamento dentro de sí mismo.",
        "errors.department_depth_exceeded": "Se superaría el número máximo de niveles.",
        "errors.employee_not_found": "No se encontró la persona.",
        "errors.employee_email_taken": "Ya existe una persona con ese correo.",
        "errors.employee_number_taken": "Ya existe una persona con ese número de empleado.",
        "errors.employee_dates_invalid":
            "La fecha de baja no puede ser anterior a la de alta.",
        "errors.employee_position_not_found": "El puesto indicado no existe.",
        "errors.employee_position_inactive": "El puesto indicado está desactivado.",
        "errors.employee_manager_not_found":
            "La persona responsable indicada no existe o no es válida.",
        "errors.employee_assignment_not_found": "No se encontró esa asignación de puesto.",
        "errors.employee_assignment_ended": "Esa asignación de puesto ya ha finalizado.",
        "errors.employee_last_assignment":
            "La persona debe conservar al menos un puesto activo.",
        "errors.position_not_found": "No se encontró el puesto.",
        "errors.position_code_taken": "Ya existe un puesto con ese código en el departamento.",
        "errors.position_department_invalid":
            "El departamento indicado no existe o está desactivado.",
        "errors.position_in_use":
            "El puesto tiene personal asignado; desactívalo en lugar de eliminarlo.",
        "errors.account_not_found": "No se encontró la cuenta.",
        "errors.account_username_taken": "Ese nombre de usuario ya está en uso.",
        "errors.account_employee_has_account": "Esa persona ya tiene una cuenta.",
        "errors.account_employee_not_active":
            "No se puede crear una cuenta para una persona que ya no está en activo.",
        "errors.account_already_in_state": "La cuenta ya está en ese estado.",
        "errors.account_password_policy": "La contraseña no cumple la política de seguridad.",
        "errors.account_invalid_credentials": "Usuario o contraseña incorrectos.",
        "errors.account_disabled": "Esta cuenta está desactivada. Contacta con soporte.",
        "errors.account_password_reused": "La nueva contraseña debe ser distinta de la actual.",
        "errors.account_role_unknown": "El rol indicado no existe en el sistema.",
        "errors.account_last_administrator": (
            "No se puede retirar el rol al último administrador activo."
        ),
        "errors.session_invalid": "Tu sesión ha caducado. Vuelve a iniciar sesión.",
        "errors.internal_error": "Se produjo un error interno. Inténtalo de nuevo más tarde.",
        "errors.service_unavailable": "El servicio no está disponible en este momento.",
    },
    "en": {
        "errors.validation_failed": "The submitted data is not valid.",
        "errors.invalid_request": "The request could not be processed.",
        "errors.method_not_allowed": "This resource does not accept that method.",
        "errors.unauthenticated": "You need to sign in.",
        "errors.forbidden": "You do not have permission for this action.",
        "errors.account_locked": "The account is temporarily locked.",
        "errors.password_change_required": "You must change your password before continuing.",
        "errors.not_found": "The requested resource was not found.",
        "errors.conflict": "The operation conflicts with the current state.",
        "errors.resource_gone": "The resource is no longer available.",
        "errors.department_not_found": "The department was not found.",
        "errors.department_code_taken": "A department with that code already exists.",
        "errors.department_not_empty": "The department still has staff assigned to it.",
        "errors.department_has_children": "The department still has sub-departments.",
        "errors.department_parent_invalid":
            "The chosen parent department does not exist or is deactivated.",
        "errors.department_move_into_descendant":
            "A department cannot be moved inside itself.",
        "errors.department_depth_exceeded": "The maximum number of levels would be exceeded.",
        "errors.employee_not_found": "The person was not found.",
        "errors.employee_email_taken": "A person with that email already exists.",
        "errors.employee_number_taken": "A person with that staff number already exists.",
        "errors.employee_dates_invalid": "The end date cannot precede the start date.",
        "errors.employee_position_not_found": "The chosen position does not exist.",
        "errors.employee_position_inactive": "The chosen position is deactivated.",
        "errors.employee_manager_not_found":
            "The chosen approver does not exist or is not valid.",
        "errors.employee_assignment_not_found": "That position assignment was not found.",
        "errors.employee_assignment_ended": "That position assignment has already ended.",
        "errors.employee_last_assignment":
            "A person must keep at least one active position.",
        "errors.position_not_found": "The position was not found.",
        "errors.position_code_taken": "A position with that code already exists in the department.",
        "errors.position_department_invalid":
            "The chosen department does not exist or is deactivated.",
        "errors.position_in_use":
            "The position has staff assigned; deactivate it instead of deleting it.",
        "errors.account_not_found": "The account was not found.",
        "errors.account_username_taken": "That username is already taken.",
        "errors.account_employee_has_account": "That person already has an account.",
        "errors.account_employee_not_active":
            "An account cannot be created for someone who is no longer employed.",
        "errors.account_already_in_state": "The account is already in that state.",
        "errors.account_password_policy": "The password does not meet the security policy.",
        "errors.account_invalid_credentials": "Incorrect username or password.",
        "errors.account_disabled": "This account is disabled. Please contact support.",
        "errors.account_password_reused": "The new password must differ from the current one.",
        "errors.account_role_unknown": "That role does not exist in this system.",
        "errors.account_last_administrator": (
            "The last active administrator cannot lose the role."
        ),
        "errors.session_invalid": "Your session has expired. Please sign in again.",
        "errors.internal_error": "An internal error occurred. Please try again later.",
        "errors.service_unavailable": "The service is unavailable right now.",
    },
}

SUPPORTED_LOCALES: Final[tuple[str, ...]] = tuple(MESSAGES)


def message_for(key: str, locale: str) -> str | None:
    """Look up one key, falling back to Spanish and then to nothing."""
    catalogue = MESSAGES.get(locale) or MESSAGES["es"]
    return catalogue.get(key)
