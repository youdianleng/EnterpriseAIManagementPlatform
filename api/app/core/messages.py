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
    "errors.org_manager_not_in_department": (
        "Solo puede ser responsable alguien con un puesto activo en ese departamento."
    ),
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
        "errors.approval_not_found": "No se encontró la solicitud de aprobación.",
        "errors.approval_already_open":
            "Ya hay una solicitud de aprobación abierta para este documento.",
        "errors.approval_approver_unresolved":
            "No se puede determinar quién debe aprobar: ni el puesto principal ni su "
            "departamento tienen una persona responsable configurada.",
        "errors.approval_hr_unavailable":
            "No hay ninguna persona de recursos humanos disponible para la segunda aprobación.",
        "errors.approval_not_approver": "No te corresponde decidir esta solicitud.",
        "errors.approval_not_requester":
            "Solo quien presentó la solicitud puede retirarla.",
        "errors.approval_not_withdrawable": "La solicitud ya no se puede retirar.",
        "errors.approval_not_pending": "La solicitud ya no está pendiente de decisión.",
        "errors.approval_previously_rejected":
            "La solicitud fue rechazada y no se puede volver a presentar.",
        "errors.notification_not_yours": (
            "Esa notificación no está dirigida a ti."
        ),
        "errors.attendance_already_clocked_in": (
            "Ya tienes una jornada abierta. Ficha la salida antes de volver a fichar "
            "la entrada."
        ),
        "errors.attendance_no_open_shift": (
            "No hay ninguna jornada abierta que esta salida pueda cerrar: ficha la "
            "entrada primero."
        ),
        "errors.attendance_event_in_future": (
            "No se puede registrar un fichaje con una hora futura."
        ),
        "errors.attendance_employee_terminated": (
            "El registro de jornada de esta persona está cerrado: un fichaje que falta "
            "se corrige con una solicitud de corrección."
        ),
        "errors.attendance_correction_not_a_punch": (
            "Una corrección no es un fichaje: indica el fichaje que corrige y el motivo, "
            "y se registra mediante el flujo de corrección."
        ),
        "errors.attendance_range_invalid": (
            "El rango de fechas no es válido: la fecha inicial no puede ser posterior a "
            "la final, ni el rango superar los cuatro años."
        ),
        "errors.personnel_change_not_found": "No se encontró la solicitud de cambio.",
        "errors.personnel_change_invalid_payload": (
            "El detalle del cambio no es válido: indica los campos y sus valores "
            "anteriores y nuevos, sin texto libre."
        ),
        "errors.personnel_change_employee_required": (
            "Indica la persona a la que se refiere el cambio; solo el alta crea una "
            "persona nueva."
        ),
        "errors.personnel_change_not_draft": (
            "Este cambio ya no es un borrador: no se puede modificar ni presentar."
        ),
        "errors.personnel_change_already_applied": (
            "El cambio ya está aplicado y no se puede anular. Crea un cambio inverso."
        ),
        "errors.personnel_change_not_cancellable": (
            "El cambio ya está cerrado y no se puede anular."
        ),
        "errors.personnel_change_apply_failed": (
            "El cambio no se pudo aplicar: el registro al que se refiere ya no existe "
            "o ya no admite este cambio. No se aplicó ninguna de sus partes."
        ),
        "errors.personnel_approver_terminated": (
            "No se puede presentar: la persona que debe aprobarlo ya no está en la "
            "empresa. Recursos Humanos debe asignar antes una persona responsable a "
            "quien dependa de ella."
        ),
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
    "errors.org_manager_not_in_department": (
        "Only somebody with an active position in that department can be its manager."
    ),
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
        "errors.approval_not_found": "The approval request was not found.",
        "errors.approval_already_open":
            "This document already has an open approval request.",
        "errors.approval_approver_unresolved":
            "Cannot tell who should approve this: neither the primary position nor its "
            "department has an approver configured.",
        "errors.approval_hr_unavailable":
            "No HR approver other than the requester is available for the second approval.",
        "errors.approval_not_approver": "This request is not yours to decide.",
        "errors.approval_not_requester":
            "Only the person who submitted the request can withdraw it.",
        "errors.approval_not_withdrawable": "The request can no longer be withdrawn.",
        "errors.approval_not_pending": "The request is no longer awaiting a decision.",
        "errors.approval_previously_rejected":
            "The request was rejected and cannot be submitted again.",
        "errors.notification_not_yours": "That notification is not addressed to you.",
        "errors.attendance_already_clocked_in": (
            "You already have a shift open. Clock out before clocking in again."
        ),
        "errors.attendance_no_open_shift": (
            "There is no open shift for this clock-out to close: clock in first."
        ),
        "errors.attendance_event_in_future": "A punch cannot be recorded for a future time.",
        "errors.attendance_employee_terminated": (
            "This person's working-time record is closed: a missing punch is fixed with "
            "a correction request."
        ),
        "errors.attendance_correction_not_a_punch": (
            "A correction is not a punch: name the event it corrects and the reason, and "
            "it is recorded through the correction flow."
        ),
        "errors.attendance_range_invalid": (
            "That date range is not valid: the start cannot be after the end, and a "
            "single range cannot exceed four years."
        ),
        "errors.personnel_change_not_found": "The personnel change was not found.",
        "errors.personnel_change_invalid_payload": (
            "The change detail is not valid: name the fields with their previous and "
            "new values, not free text."
        ),
        "errors.personnel_change_employee_required": (
            "Name the person this change is about; only a join creates a new person."
        ),
        "errors.personnel_change_not_draft": (
            "This change is no longer a draft, so it cannot be edited or filed."
        ),
        "errors.personnel_change_already_applied": (
            "This change has already taken effect and cannot be cancelled. "
            "Raise a counter-change instead."
        ),
        "errors.personnel_change_not_cancellable": (
            "This change is already closed and cannot be cancelled."
        ),
        "errors.personnel_change_apply_failed": (
            "The change could not be applied: the record it refers to no longer exists "
            "or no longer accepts it. None of its parts were applied."
        ),
        "errors.personnel_approver_terminated": (
            "This cannot be filed: the person who has to approve it has left the "
            "company. HR has to assign an approver to their reports first."
        ),
        "errors.internal_error": "An internal error occurred. Please try again later.",
        "errors.service_unavailable": "The service is unavailable right now.",
    },
}

SUPPORTED_LOCALES: Final[tuple[str, ...]] = tuple(MESSAGES)


def message_for(key: str, locale: str) -> str | None:
    """Look up one key, falling back to Spanish and then to nothing."""
    catalogue = MESSAGES.get(locale) or MESSAGES["es"]
    return catalogue.get(key)
