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
        # Corrections (ticket 24). Each says what the person can do next: the punch
        # was recorded wrongly, so the way forward is a correction request — which is
        # also what the two refusals below have in common.
        "errors.attendance_correction_not_found": (
            "No se encontró esa solicitud de corrección de fichaje."
        ),
        "errors.attendance_correction_invalid": (
            "La solicitud de corrección no es válida: indica el día, el fichaje que "
            "corriges, la hora correcta y el motivo. La hora no puede ser futura."
        ),
        "errors.attendance_correction_not_draft": (
            "Esta solicitud ya no es un borrador: no se puede modificar ni presentar. "
            "Si fue rechazada, crea una nueva."
        ),
        "errors.attendance_correction_target_unresolved": (
            "Ese día no tiene un único fichaje de ese tipo: hay varios turnos y la "
            "solicitud no puede decir a cuál se refiere. Revisa el día antes de "
            "corregirlo."
        ),
        "errors.attendance_correction_submission_refused": (
            "No se pudo presentar la solicitud: revisa su estado y vuelve a intentarlo."
        ),
        "errors.attendance_correction_apply_failed": (
            "La corrección está aprobada pero no se pudo aplicar al registro de "
            "jornada. No se modificó ningún fichaje; revisa el día y vuelve a intentarlo."
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
        "errors.project_not_found": "No se encontró el proyecto.",
        "errors.project_code_taken": "Ya existe un proyecto con ese código.",
        "errors.project_dates_invalid":
            "La fecha de fin no puede ser anterior a la de inicio.",
        "errors.project_department_not_found":
            "El departamento indicado no existe o está desactivado.",
        "errors.project_manager_not_found":
            "La persona responsable indicada no existe o no está en activo.",
        "errors.project_archived": (
            "El proyecto está archivado: se conserva para consulta, pero no admite "
            "cambios ni nuevas horas."
        ),
        "errors.project_not_active": (
            "El proyecto todavía no está activo: actívalo antes de añadir tareas o "
            "imputar horas."
        ),
        "errors.project_task_not_found": "No se encontró la tarea.",
        "errors.project_task_code_taken": "Ya existe una tarea con ese código en el proyecto.",
        "errors.project_task_not_recordable": (
            "No se pueden imputar horas a esta tarea: está desactivada, o su proyecto "
            "no está activo o queda fuera de tu ámbito."
        ),
        "errors.project_not_manageable": (
            "Solo la persona responsable del proyecto, Administración o Recursos "
            "Humanos pueden gestionarlo."
        ),
        "errors.project_task_already_inactive": "La tarea ya está desactivada.",
        "errors.schedule_not_found": "No se encontró el horario.",
        "errors.schedule_holiday_not_found": "No se encontró ese festivo.",
        "errors.schedule_override_not_found": "No se encontró esa excepción de horario.",
        "errors.schedule_code_taken": "Ya existe un horario con ese código.",
        "errors.schedule_already_set": (
            "Ese ámbito ya tiene un horario activo: modifica el existente en lugar de "
            "crear un segundo, porque solo uno puede aplicarse."
        ),
        "errors.schedule_override_overlaps": (
            "Esta persona ya tiene una excepción de horario que cubre alguno de esos "
            "días: ajusta las fechas o termina antes la anterior."
        ),
        "errors.schedule_invalid_day": (
            "El horario no es válido: las horas previstas de un día deben coincidir con "
            "su franja horaria menos el descanso, y cada día de la semana puede "
            "aparecer una sola vez."
        ),
        "errors.schedule_inactive": (
            "Ese horario está desactivado: elige uno activo."
        ),
        "errors.schedule_invalid_holiday": (
            "El festivo no es válido: los autonómicos y locales indican su región, los "
            "nacionales no, y el año debe coincidir con la fecha."
        ),
        "errors.schedule_invalid_holiday_file": (
            "El archivo de festivos no se pudo leer: revisa las columnas y las fechas "
            "de las líneas indicadas. No se importó ninguna."
        ),
        "errors.schedule_holiday_exists": (
            "Esa fecha ya está marcada como festivo con ese ámbito y esa región."
        ),
        # Weekly timesheets (ticket 28). Each one says what the person can do next: an
        # employee who is told only that something failed cannot act on it, and every one
        # of these is a state they can reach by typing.
        "errors.timesheet_not_found": "No existe ninguna hoja de horas para esa semana.",
        "errors.timesheet_entry_not_found": "Esa entrada de horas no existe en esta semana.",
        "errors.timesheet_entry_task_mismatch":
            "La tarea no pertenece al proyecto indicado.",
        "errors.timesheet_already_exists":
            "Ya tienes una hoja de horas para esa semana.",
        "errors.timesheet_not_editable": (
            "Esta semana ya está enviada. Solo puedes modificarla si te la devuelven."
        ),
        "errors.timesheet_entry_project_not_recordable": (
            "Ese proyecto no admite horas nuevas: solo los proyectos activos las aceptan."
        ),
        "errors.timesheet_copy_target_not_empty": (
            "Esta semana ya tiene horas. Vacíala antes de copiar la anterior."
        ),
        "errors.timesheet_submission_refused": (
            "No se pudo enviar la semana: revisa el estado de la solicitud y vuelve a intentarlo."
        ),
        "errors.timesheet_not_yours": "Solo puedes rellenar tus propias horas.",
        "errors.timesheet_week_not_monday": (
            "La semana empieza en lunes: indica la fecha del lunes."
        ),
        "errors.timesheet_entry_outside_project_dates": (
            "Esa fecha queda fuera del periodo del proyecto. Elige otro día u otro proyecto."
        ),
        "errors.timesheet_entry_minutes_invalid": (
            "Los minutos deben ser un número entero entre 1 y 1440 (24 h)."
        ),
        "errors.timesheet_copy_source_invalid": (
            "La semana anterior no tiene horas que copiar."
        ),
        # Ticket 29. Three sentences the employee has to be able to act on, so each
        # names the way forward rather than the rule: a locked week is corrected with a
        # supplementary submission, and a week outside the eight-week window cannot be
        # corrected at all — which is what "no quedan semanas" says, and the detail
        # states the count for any client that wants it as a number.
        "errors.timesheet_week_locked": (
            "Esta semana está aprobada y bloqueada. Solo se puede corregir con un envío "
            "complementario."
        ),
        "errors.timesheet_week_closed": (
            "Esta semana queda fuera de las 8 semanas de plazo: no quedan semanas de "
            "corrección y ya no admite ningún cambio."
        ),
        "errors.timesheet_supplement_not_locked": (
            "Esta semana no está bloqueada: todavía puedes editarla directamente."
        ),
        "errors.timesheet_supplement_open": (
            "Ya hay un envío complementario pendiente para esta semana. Espera la "
            "decisión antes de enviar otro."
        ),
        "errors.timesheet_supplement_invalid": (
            "El envío complementario no indica ninguna corrección válida: elige las "
            "entradas de esta semana que quieres corregir."
        ),
        "errors.timesheet_entry_is_reversal": (
            "Las líneas de ajuste de un envío complementario no se pueden editar ni "
            "borrar, y tampoco lo que ya está ajustado."
        ),
        "errors.timesheet_report_range_invalid": (
            "El periodo del informe no es válido: indica una fecha de inicio anterior "
            "a la de fin y como máximo {days} días."
        ),
        "errors.leave_type_not_found": "Ese tipo de permiso no existe.",
        "errors.leave_request_not_found": "No se encontró esa solicitud de permiso.",
        "errors.leave_type_code_taken": "Ya existe un tipo de permiso con ese código.",
        "errors.leave_type_inactive": "Ese tipo de permiso ya no se ofrece. Elige otro.",
        "errors.leave_type_invalid": (
            "El tipo de permiso necesita un código y un nombre en los dos idiomas."
        ),
        "errors.leave_request_invalid": (
            "La solicitud de permiso no es válida: revisa las fechas, el tipo y el "
            "justificante."
        ),
        "errors.leave_request_not_draft": (
            "Esta solicitud ya no se puede modificar en su estado actual."
        ),
        "errors.leave_request_overlaps": (
            "Ya tienes una solicitud de permiso que cubre alguno de esos días."
        ),
        "errors.leave_balance_insufficient": (
            "No tienes saldo suficiente para esos días. Revisa los días disponibles."
        ),
        "errors.leave_already_started": (
            "El permiso ya ha comenzado y no se puede anular. Recursos Humanos puede "
            "corregir el registro mediante el flujo de corrección de fichajes."
        ),
        "errors.leave_not_withdrawable": "Esta solicitud ya no se puede anular.",
        "errors.leave_submission_refused": (
            "No se pudo enviar la solicitud a aprobación. Revisa su estado e inténtalo de nuevo."
        ),
        "errors.leave_attachment_required": (
            "Este tipo de permiso necesita un justificante. Adjunta la referencia del "
            "fichero guardado."
        ),
        "errors.leave_settle_failed": (
            "La solicitud se resolvió y el saldo no se pudo actualizar. Se reintentará."
        ),
        "errors.overtime_request_not_found": "No se encontró la solicitud de horas extra.",
        "errors.overtime_record_not_found": "No se encontró el registro de horas extra.",
        "errors.overtime_request_invalid": (
            "La solicitud de horas extra no es válida: la fecha debe ser hoy o posterior, "
            "la duración debe caber en un día y hay que indicar el motivo."
        ),
        "errors.overtime_request_not_draft": (
            "Esta solicitud ya no es un borrador y no se puede modificar ni enviar."
        ),
        "errors.overtime_request_exists": (
            "Ya existe una solicitud o un registro de horas extra para ese día."
        ),
        "errors.overtime_record_not_settled": (
            "El día de este registro aún no se ha calculado. Las horas reales se comparan "
            "cuando el día ha terminado."
        ),
        "errors.overtime_resolve_failed": (
            "La solicitud se aprobó y el registro no se pudo escribir. Se reintentará."
        ),
        "errors.overtime_submission_refused": (
            "No se pudo enviar la solicitud a aprobación. Revisa su estado e inténtalo de nuevo."
        ),
        "errors.overtime_not_withdrawable": (
            "Esta solicitud ya no se puede anular. Recursos Humanos puede confirmar o "
            "ajustar las horas del registro."
        ),
        "errors.overtime_period_invalid": (
            "El periodo indicado no es válido: usa el formato AAAA-MM."
        ),
        # Documents (ticket 31). Each says what the person can do next: a refused
        # file is chosen again, a duplicate is opened, and a document with nothing
        # extracted is replaced by a text version — which is the ticket's own
        # wording for a scanned file.
        "errors.document_not_found": "No se encontró el documento.",
        "errors.document_upload_type_unsupported": (
            "Ese tipo de archivo no se admite. Sube un PDF, un Word (.docx), un Excel "
            "(.xlsx), un texto (.txt) o un Markdown (.md)."
        ),
        "errors.document_upload_too_large": (
            "El archivo supera el límite de 50 MB por documento. Divídelo o reduce su "
            "tamaño antes de subirlo."
        ),
        "errors.document_upload_empty": (
            "El archivo está vacío o no tiene nombre. Elige un archivo con contenido."
        ),
        "errors.document_not_ready": (
            "Este documento todavía no se ha procesado, o el procesado ha fallado. "
            "Consulta su estado antes de abrirlo."
        ),
        "errors.document_duplicate": (
            "Ya existe un documento con este mismo contenido. Ábrelo en lugar de "
            "subirlo otra vez."
        ),
        "errors.document_reprocess_unsupported": (
            "Este documento no se puede reprocesar en su estado actual."
        ),
        "errors.document_file_missing": (
            "El archivo original de este documento ya no está disponible en el "
            "almacenamiento. Avisa a soporte."
        ),
        "errors.document_embedding_unavailable": (
            "El documento se ha procesado, pero no se han podido generar sus vectores "
            "de búsqueda. La búsqueda semántica no lo encontrará hasta que se vuelva a "
            "procesar; avisa a soporte."
        ),
        "errors.retrieval_query_invalid": (
            "La consulta no es válida: escribe una pregunta y vuelve a intentarlo."
        ),
        # Answers (ticket 34). The copy names *what to do*: the model failure is
        # retryable, and the refusal is not the caller's mistake. This last key has no
        # `ErrorCode` on purpose — D20's refusal is a successful answer, and a client
        # that wants exactly one language renders this instead of the two-language
        # sentence the API stores in the message's own content.
        "errors.knowledge_base_no_basis": (
            "No he encontrado base en la base de conocimiento de la empresa para "
            "responder a esta pregunta. Prueba a reformularla o a aportar el documento "
            "que la contiene."
        ),
        "errors.answer_model_unavailable": (
            "No se ha podido generar la respuesta: el modelo de lenguaje no está "
            "disponible en este momento. Vuelve a intentarlo en unos minutos."
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
        # Corrections (ticket 24). Each says what the person can do next: the punch
        # was recorded wrongly, so the way forward is a correction request — which is
        # also what the two refusals below have in common.
        "errors.attendance_correction_not_found": "That correction request was not found.",
        "errors.attendance_correction_invalid": (
            "That correction request is not valid: name the day, the punch you are "
            "correcting, the right time and the reason. The time cannot be in the future."
        ),
        "errors.attendance_correction_not_draft": (
            "This request is no longer a draft, so it cannot be edited or filed. If it "
            "was rejected, raise a new one."
        ),
        "errors.attendance_correction_target_unresolved": (
            "That day does not have exactly one punch of that kind: there are several "
            "shifts and the request cannot say which one it means. Check the day before "
            "correcting it."
        ),
        "errors.attendance_correction_submission_refused": (
            "The request could not be filed: check its state and try again."
        ),
        "errors.attendance_correction_apply_failed": (
            "The correction is approved but could not be applied to the working-time "
            "record. No punch was changed; check the day and try again."
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
        "errors.project_not_found": "The project was not found.",
        "errors.project_code_taken": "A project with that code already exists.",
        "errors.project_dates_invalid": "The end date cannot precede the start date.",
        "errors.project_department_not_found":
            "The chosen department does not exist or is deactivated.",
        "errors.project_manager_not_found":
            "The chosen project manager does not exist or is no longer employed.",
        "errors.project_archived": (
            "This project is archived: it stays readable, and accepts neither changes "
            "nor new time."
        ),
        "errors.project_not_active": (
            "This project is not active yet: activate it before adding tasks or "
            "recording time."
        ),
        "errors.project_task_not_found": "The task was not found.",
        "errors.project_task_code_taken": "A task with that code already exists in the project.",
        "errors.project_task_not_recordable": (
            "Time cannot be recorded against this task: it is deactivated, or its "
            "project is not active or is outside your scope."
        ),
        "errors.project_not_manageable": (
            "Only the project's own manager, administration or HR can manage it."
        ),
        "errors.project_task_already_inactive": "That task is already switched off.",
        "errors.schedule_not_found": "The work schedule was not found.",
        "errors.schedule_holiday_not_found": "That holiday was not found.",
        "errors.schedule_override_not_found": "That schedule override was not found.",
        "errors.schedule_code_taken": "A schedule with that code already exists.",
        "errors.schedule_already_set": (
            "That scope already has an active schedule: edit it instead of adding a "
            "second one, because only one can apply."
        ),
        "errors.schedule_override_overlaps": (
            "This person already has a schedule override covering some of those days: "
            "adjust the dates, or end the earlier one first."
        ),
        "errors.schedule_invalid_day": (
            "That schedule is not valid: a day's expected minutes must equal its "
            "window less its break, and each weekday may appear once."
        ),
        "errors.schedule_inactive": "That schedule is deactivated: choose an active one.",
        "errors.schedule_invalid_holiday": (
            "That holiday is not valid: regional and local holidays name their region, "
            "national ones do not, and the year has to match the date."
        ),
        "errors.schedule_invalid_holiday_file": (
            "The holiday file could not be read: check the columns and the dates on the "
            "lines named. None of it was imported."
        ),
        "errors.schedule_holiday_exists": (
            "That date is already a holiday with that scope and region."
        ),
        # Weekly timesheets (ticket 28). Each one says what the person can do next: an
        # employee told only that something failed cannot act on it, and every one of
        # these is a state they can reach by typing.
        "errors.timesheet_not_found": "There is no timesheet for that week.",
        "errors.timesheet_entry_not_found": "That entry does not exist in this week.",
        "errors.timesheet_entry_task_mismatch":
            "The task does not belong to the project named.",
        "errors.timesheet_already_exists": "You already have a timesheet for that week.",
        "errors.timesheet_not_editable": (
            "This week has been submitted. You can only change it if it comes back to you."
        ),
        "errors.timesheet_entry_project_not_recordable": (
            "That project does not accept new time: only active projects do."
        ),
        "errors.timesheet_copy_target_not_empty": (
            "This week already has hours. Clear it before copying the previous one."
        ),
        "errors.timesheet_submission_refused": (
            "The week could not be submitted: check the request's state and try again."
        ),
        "errors.timesheet_not_yours": "You can only fill in your own hours.",
        "errors.timesheet_week_not_monday": "A week starts on Monday: give the Monday's date.",
        "errors.timesheet_entry_outside_project_dates": (
            "That date is outside the project's period. Pick another day or another project."
        ),
        "errors.timesheet_entry_minutes_invalid": (
            "Minutes must be a whole number from 1 to 1440 (24 h)."
        ),
        "errors.timesheet_copy_source_invalid": (
            "The previous week has no hours to copy."
        ),
        "errors.timesheet_week_locked": (
            "This week is approved and locked. It can only be corrected with a "
            "supplementary submission."
        ),
        "errors.timesheet_week_closed": (
            "This week is outside the 8-week window: no correction weeks are left and it "
            "accepts no further changes."
        ),
        "errors.timesheet_supplement_not_locked": (
            "This week is not locked: you can still edit it directly."
        ),
        "errors.timesheet_supplement_open": (
            "A supplementary submission for this week is already waiting for a decision. "
            "Wait for it before sending another."
        ),
        "errors.timesheet_supplement_invalid": (
            "The supplementary submission states no valid correction: pick the entries of "
            "this week you want to correct."
        ),
        "errors.timesheet_entry_is_reversal": (
            "The adjustment lines of a supplementary submission cannot be edited or "
            "removed, and neither can what has already been adjusted."
        ),
        "errors.timesheet_report_range_invalid": (
            "The report's period is not valid: give a start date no later than the end "
            "date, and at most {days} days."
        ),
        "errors.leave_type_not_found": "There is no such leave type.",
        "errors.leave_request_not_found": "That leave request does not exist.",
        "errors.leave_type_code_taken": "A leave type with that code already exists.",
        "errors.leave_type_inactive": "That leave type is no longer offered. Choose another.",
        "errors.leave_type_invalid": (
            "A leave type needs a code and a name in both languages."
        ),
        "errors.leave_request_invalid": (
            "That leave request is not valid: check the dates, the type and the attachment."
        ),
        "errors.leave_request_not_draft": (
            "This request cannot be changed in its current state."
        ),
        "errors.leave_request_overlaps": (
            "You already have a leave request covering one of those days."
        ),
        "errors.leave_balance_insufficient": (
            "You do not have enough days left for this request. Check your remaining days."
        ),
        "errors.leave_already_started": (
            "This leave has already started and cannot be withdrawn. HR can correct the "
            "record through the attendance correction flow."
        ),
        "errors.leave_not_withdrawable": "This request can no longer be withdrawn.",
        "errors.leave_submission_refused": (
            "The request could not be filed for approval. Check its state and try again."
        ),
        "errors.leave_attachment_required": (
            "This leave type requires an attachment. Send the reference of the stored file."
        ),
        "errors.leave_settle_failed": (
            "The request was decided and its balance could not be updated. It will be retried."
        ),
        "errors.overtime_request_not_found": "The overtime request was not found.",
        "errors.overtime_record_not_found": "The overtime record was not found.",
        "errors.overtime_request_invalid": (
            "That overtime request is not valid: the date must be today or later, the "
            "minutes must fit in a day, and a reason is required."
        ),
        "errors.overtime_request_not_draft": (
            "This request is no longer a draft, so it cannot be changed or filed."
        ),
        "errors.overtime_request_exists": (
            "There is already an overtime request or record for that day."
        ),
        "errors.overtime_record_not_settled": (
            "This record's day has not been computed yet. The hours actually worked are "
            "compared once the day is over."
        ),
        "errors.overtime_resolve_failed": (
            "The request was approved and its record could not be written. It will be retried."
        ),
        "errors.overtime_submission_refused": (
            "The request could not be filed for approval. Check its state and try again."
        ),
        "errors.overtime_not_withdrawable": (
            "This request can no longer be withdrawn. HR can confirm or adjust the hours "
            "on the record."
        ),
        "errors.overtime_period_invalid": "That is not a period: use YYYY-MM.",
        # Documents (ticket 31). The scanned-file refusal is the ticket's own
        # sentence: no OCR, and a text version is what to upload instead.
        "errors.document_not_found": "The document was not found.",
        "errors.document_upload_type_unsupported": (
            "That file type is not accepted. Upload a PDF, a Word file (.docx), an "
            "Excel file (.xlsx), a plain text file (.txt) or Markdown (.md)."
        ),
        "errors.document_upload_too_large": (
            "The file is over the 50 MB limit for one document. Split it or reduce its "
            "size before uploading."
        ),
        "errors.document_upload_empty": (
            "The file is empty or has no name. Choose a file with content."
        ),
        "errors.document_not_ready": (
            "This document has not been processed yet, or processing failed. Check its "
            "status before opening it."
        ),
        "errors.document_duplicate": (
            "A document with this same content already exists. Open it instead of "
            "uploading it again."
        ),
        "errors.document_reprocess_unsupported": (
            "This document cannot be reprocessed in its current state."
        ),
        "errors.document_file_missing": (
            "This document's original file is no longer in storage. Please contact support."
        ),
        "errors.document_embedding_unavailable": (
            "The document was processed, but its search vectors could not be generated. "
            "Semantic search will not find it until it is reprocessed; please contact "
            "support."
        ),
        "errors.retrieval_query_invalid": (
            "The search query is not valid: write a question and try again."
        ),
        "errors.knowledge_base_no_basis": (
            "I found no basis in the company knowledge base to answer this question. "
            "Try rephrasing it, or upload the document that contains the answer."
        ),
        "errors.answer_model_unavailable": (
            "The answer could not be generated: the language model is unavailable right "
            "now. Please try the same question again in a few minutes."
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
