"""A small Spanish policy corpus, and the questions it answers.

This is the sample ticket 33's evaluation is measured on, and it lives here rather
than inside the script so that the *tests* and the *measurement* use the same
documents: a hit rate reported against a corpus nobody else can read is a number
without a subject.

**Why Spanish, and why these documents.** `docs/DESIGN.md` §10.3 fixes the corpus's
language by fixing the full-text configuration to `spanish`, and §5.2's whole claim is
that pure vector search is insensitive to 制度编号、缩写、专有名词 — policy codes,
acronyms and proper nouns. So the documents are three real-shaped Spanish policies that
carry exactly those things:

* **codes**: `PRL-07`, `GT-2026`, and a pay band `B3`;
* **acronyms**: `PRL`, `IRPF`, `GT`, `RGPD`;
* **figures and units**: `0,26 €/km`, `23 días`, `40 horas`, `30.000 €`.

Half the questions ask by code (`¿Qué dice la PRL-07 sobre las pausas?`), which is the
case a hashed-bag-of-words vector model is expected to miss and the text leg to catch —
and the reason hybrid must not be *worse* than vector-only, which is the ticket's own
acceptance line.

**What these documents are not.** They are not a benchmark. Nine questions over three
documents can distinguish "the pipeline works" from "it does not"; they cannot
establish that RRF beats a weighted sum on a real corpus, and the script's own
docstring says so. A real evaluation needs the organisation's own documents and its own
questions, which is what the JSON-Lines input to `eval_retrieval.py` is for.

Each document is a Markdown body of several sections, deliberately long enough that the
structural split produces several children under more than one parent — the parent/child
pair is what this ticket's fusion quotes, so a one-chunk document would measure nothing
about it.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SampleDocument:
    """One document of the sample: what it is called, and its body."""

    title: str
    filename: str
    body: str


@dataclass(frozen=True, slots=True)
class SampleQuestion:
    """One question and the document that answers it.

    `expects` is a document *title*, matching the JSON-Lines format
    `eval_retrieval.py` reads, so a question is the same value whether it came from
    this module or from a file somebody wrote.
    """

    question: str
    expects: str
    #: What the case is about, for the printed report: `lexical` marks the questions
    #: whose answer contains a code or an acronym the vector half is expected to miss.
    kind: str = "semantic"


#: Repeating a clause to a realistic length. The split targets children of ~400 tokens
#: and parents of ~1500, so a section shorter than a child is one child and nothing
#: above it is testable.
def _section(heading: str, body: str) -> str:
    return f"## {heading}\n\n{body}\n\n"


_TELETRABAJO = _section(
    "1. Ámbito y definición",
    "El teletrabajo es la prestación de servicios con carácter regular desde un lugar "
    "distinto de las instalaciones de la empresa, mediante el uso exclusivo de medios "
    "telemáticos. Se considera teletrabajo regular el que se presta con una frecuencia "
    "mínima de dos días por semana durante un periodo de referencia de tres meses. El "
    "personal con contrato temporal inferior a seis meses no accede al teletrabajo "
    "regular, salvo autorización expresa de la dirección de personas. La política "
    "GT-2026 se aplica a toda la plantilla, incluido el personal en movilidad "
    "geográfica, y no modifica las condiciones del convenio colectivo vigente.",
) + _section(
    "2. Requisitos técnicos del puesto",
    "La persona que teletrabaja debe disponer de una conexión de banda ancha de al "
    "menos 100 megabits por segundo y de un espacio de trabajo que permita la "
    "confidencialidad de la información tratada. La empresa entrega un ordenador "
    "portátil de dotación y, cuando el puesto lo requiere, una pantalla adicional. Si "
    "el equipo de dotación sufre una avería, la persona debe abrir una incidencia en el "
    "servicio de soporte indicando el número de serie, el modelo y una descripción del "
    "problema. Soporte responde en un plazo máximo de veinticuatro horas laborables y, "
    "si la reparación supera los tres días, entrega un equipo de sustitución sin coste "
    "para la persona. El traslado del equipo fuera del domicilio comunicado requiere "
    "autorización previa por escrito.",
) + _section(
    "3. Compensación de gastos",
    "La empresa abona los gastos de conectividad y de material directamente "
    "imputables al teletrabajo, previa presentación del justificante. La compensación "
    "de conexión es de 30 euros mensuales y se abona en la nómina del mes siguiente. "
    "Los desplazamientos puntuales a la oficina desde el domicilio de teletrabajo no "
    "generan derecho a dieta, sin perjuicio de lo previsto en la política de gastos de "
    "desplazamiento GT-2026. La persona debe conservar los justificantes durante cuatro "
    "años, en cumplimiento del Reglamento General de Protección de Datos (RGPD) y de la "
    "normativa fiscal aplicable.",
) + _section(
    "4. Registro horario y desconexión digital",
    "El teletrabajo no altera la jornada ordinaria de 40 horas semanales ni el registro "
    "horario diario. La persona debe fichar el inicio y el fin de la jornada con los "
    "mismos medios que en la oficina. Se reconoce el derecho a la desconexión digital "
    "fuera del horario de trabajo: no se exige respuesta a comunicaciones profesionales "
    "en periodo de descanso, permisos, vacaciones o baja médica. El incumplimiento del "
    "deber de desconexión por parte de responsables jerárquicos se comunica a recursos "
    "humanos, que lo registra. La empresa realiza un seguimiento trimestral de las "
    "horas teletrabajadas para verificar que no se supera la jornada máxima.",
) + _section(
    "5. Derechos y deberes de las partes",
    "El teletrabajo es voluntario y reversible para ambas partes mediante la "
    "modalidad de prestación de servicios, articulada a través del acuerdo escrito de "
    "trabajo a distancia. La persona tiene derecho a la formación necesaria para el "
    "uso de las herramientas telemáticas y a la protección de datos de carácter "
    "personal. La empresa debe entregar por escrito la relación de medios y gastos "
    "asociados. La reversibilidad se ejerce mediante preaviso de treinta días y no "
    "supone modificación del contrato ni reducción de derechos. El centro de trabajo "
    "de referencia a efectos de representación colectiva sigue siendo el domicilio "
    "social de la empresa.",
)


VACACIONES = SampleDocument(
    title="Politica de vacaciones y permisos",
    filename="politica_vacaciones.md",
    body="# Política de vacaciones y permisos\n\n"
    + _section(
        "1. Vacaciones anuales retribuidas",
        "El personal con al menos un año de antigüedad podrá solicitar días de "
        "vacaciones adicionales a los veintitrés días laborables que corresponden por "
        "convenio. Las vacaciones anuales retribuidas no son sustituibles por "
        "compensación económica. La solicitud se presentará por escrito con quince días "
        "de antelación y el responsable responderá en un plazo máximo de cinco días "
        "hábiles. La concesión se comunicará al solicitante y al responsable del "
        "departamento. Los días de vacaciones no disfrutados antes del 31 de diciembre "
        "podrán trasladarse al año siguiente hasta un máximo de cinco días laborables, y "
        "el resto se pierde salvo autorización expresa de recursos humanos. El periodo de "
        "prueba no interrumpe el devengo de vacaciones, aunque el disfrute queda "
        "supeditado al acuerdo entre las partes.",
    )
    + _section(
        "2. Permisos retribuidos y licencias",
        "El permiso por matrimonio es de quince días naturales, computados desde el día "
        "de la celebración. El permiso por fallecimiento de familiar de primer grado es "
        "de tres días laborables, ampliables a cinco si la persona debe desplazarse a "
        "otra provincia. El permiso por traslado de domicilio es de dos días por mudanza, "
        "y el permiso por deber inexcusable de carácter público es por el tiempo "
        "indispensable. Las licencias no retribuidas requieren autorización de recursos "
        "humanos y no pueden superar los tres meses. Los permisos retribuidos se "
        "acreditan con el documento oficial correspondiente y deben solicitarse con la "
        "mayor antelación posible, salvo causas imprevisibles.",
    )
    + _section(
        "3. Situaciones especiales",
        "La suspensión por nacimiento y cuidado de menor tiene una duración de veinte "
        "semanas para ambos progenitores. El permiso por lactancia acumulada en jornadas "
        "completas es de veinte días naturales tras el descanso obligatorio. La "
        "excedencia por cuidado de hijo es de tres años de duración y da lugar a reserva "
        "del puesto de trabajo. La reducción de jornada por guarda legal comprende una "
        "reducción de entre un octavo y la mitad de la jornada diaria, con la "
        "correspondiente reducción proporcional del salario. La persona en situación de "
        "excedencia debe solicitar el reingreso con un mes de antelación al vencimiento "
        "del periodo.",
    ),
)


GASTOS = SampleDocument(
    title="Politica de gastos de desplazamiento",
    filename="politica_gastos_desplazamiento.md",
    body="# Política de gastos de desplazamiento\n\n"
    + _section(
        "1. Vehículo propio y kilometraje",
        "El personal que utilice su vehículo propio por necesidades del servicio percibe "
        "una indemnización de 0,26 euros por kilómetro recorrido. La indemnización por "
        "kilometraje cubre los gastos de carburante, mantenimiento y seguro, y no es "
        "compatible con el abono de carburante por otra vía. El importe se calcula sobre "
        "la distancia más corta entre el centro de trabajo y el destino, salvo que el "
        "responsable autorice un itinerario alternativo. La liquidación se presenta "
        "mensualmente mediante la hoja de gastos con el detalle de fechas, destinos y "
        "kilómetros. No se abona kilometraje por los desplazamientos entre el domicilio "
        "y el centro de trabajo habitual.",
    )
    + _section(
        "2. Alojamiento y manutención",
        "El límite de alojamiento es de 90 euros por noche en territorio nacional y de "
        "150 euros por noche en desplazamiento internacional, siempre con factura a "
        "nombre de la empresa. La manutención se abona mediante dieta: 30 euros por "
        "comida y 45 euros por cena fuera del término municipal, con el límite diario de "
        "120 euros. Los gastos de representación requieren autorización previa de la "
        "dirección y se justifican con la relación de asistentes. Las propinas no son "
        "gasto reembolsable. Los billetes de avión y tren se reservan a través de la "
        "agencia corporativa y se facturan directamente a la empresa.",
    )
    + _section(
        "3. Bandas salariales y reembolso",
        "La banda salarial B3 corresponde a puestos de especialista con una retribución "
        "bruta anual de referencia de 30.000 euros. El reembolso de una hoja de gastos "
        "correctamente presentada se abona en un plazo máximo de treinta días desde su "
        "aprobación. La hoja de gastos debe presentarse antes del día 5 del mes "
        "siguiente; una hoja presentada más tarde se abona en el ciclo siguiente. La "
        "empresa practica la retención del Impuesto sobre la Renta de las Personas "
        "Físicas (IRPF) conforme a la normativa vigente. El reembolso de un gasto "
        "rechazado se comunica por escrito con la motivación, y la persona dispone de "
        "diez días para aportar documentación adicional.",
    ),
)


TELETRABAJO = SampleDocument(
    title="Politica de teletrabajo",
    filename="politica_teletrabajo.md",
    body="# Política de teletrabajo\n\n" + _TELETRABAJO,
)


# --- the three distractors ---------------------------------------------------
#
# **Why the corpus has six documents and not three.** A three-document corpus makes the
# acceptance line vacuous: the vector leg's top five is nearly the whole corpus, so every
# mode scores 1.000 and the comparison says nothing. The three below are what a real
# knowledge base looks like — several policies that talk about *people, days, requests and
# approval* — and they are what pushes an answer down the vector leg's ranking, which is
# the state the fusion exists for. Each one is also a genuine policy, so a question about
# it has a real answer rather than a planted one.

SALUD = SampleDocument(
    title="Politica de salud laboral y PRL",
    filename="politica_salud_laboral.md",
    body="# Política de salud laboral y PRL\n\n"
    + _section(
        "1. Vigilancia de la salud",
        "La empresa ofrece un reconocimiento médico anual voluntario a toda la plantilla, "
        "que se realiza dentro de la jornada de trabajo. El examen incluye una analítica "
        "general, una revisión de la vista y una exploración de la espalda para los puestos "
        "con carga física. Los resultados son confidenciales y se comunican únicamente a la "
        "persona interesada, con la aptitud y las recomendaciones. El servicio de prevención "
        "ajeno conserva los historiales clínicos con la confidencialidad que exige la Ley de "
        "Prevención de Riesgos Laborales. Una persona puede solicitar una revisión "
        "extraordinaria cuando cambia de puesto o tras una ausencia prolongada.",
    )
    + _section(
        "2. Pausas y ergonomía",
        "La política PRL-07 regula las pausas en los puestos con pantalla de visualización "
        "de datos: cada dos horas de trabajo continuado corresponde una pausa de diez "
        "minutos. La silla debe ser regulable en altura y el monitor debe situarse a la "
        "altura de los ojos, a una distancia de entre cincuenta y setenta centímetros. La "
        "empresa facilita un reposapiés y un soporte lumbar a quien lo solicite por "
        "prescripción médica. El incumplimiento de las medidas ergonómicas se comunica al "
        "servicio de prevención, que puede proponer una mejora del puesto.",
    )
    + _section(
        "3. Accidentes y primeros auxilios",
        "Todo accidente con baja debe comunicarse al responsable y a recursos humanos el "
        "mismo día, con el parte de accidente. La empresa tramita la baja ante la mutua "
        "colaboradora en un plazo de veinticuatro horas. Los botiquines se revisan cada "
        "trimestre y los delegados de prevención reciben formación de primeros auxilios "
        "cada dos años. En caso de emergencia, el protocolo de evacuación se activa desde "
        "los puntos de encuentro señalizados en cada planta, y el coordinador de "
        "emergencias confirma la evacuación completa al responsable de prevención.",
    ),
)


SEGURIDAD = SampleDocument(
    title="Politica de seguridad de la informacion",
    filename="politica_seguridad_informacion.md",
    body="# Política de seguridad de la información\n\n"
    + _section(
        "1. Contraseñas y acceso",
        "Las credenciales de acceso son personales e intransferibles. La contraseña debe "
        "tener al menos doce caracteres, combinar mayúsculas, minúsculas, dígitos y "
        "símbolos, y renovarse cada noventa días. El acceso a los sistemas críticos exige "
        "un segundo factor de autenticación. La empresa bloquea la cuenta tras cinco "
        "intentos fallidos y restablece el acceso a través del servicio de soporte, que "
        "verifica la identidad por videollamada. Compartir credenciales con otra persona, "
        "aunque sea para cubrir una ausencia, se considera un incidente de seguridad y se "
        "comunica al responsable de seguridad de la información.",
    )
    + _section(
        "2. Datos personales y RGPD",
        "El tratamiento de datos personales se rige por el Reglamento General de Protección "
        "de Datos (RGPD) y por el registro de actividades de tratamiento de la empresa. "
        "Solo se accede a los datos necesarios para la función desempeñada, y el acceso se "
        "revisa cada semestre. Los datos sensibles se cifran en reposo y en tránsito. Una "
        "brecha de seguridad se notifica a la autoridad de control en un plazo de setenta y "
        "dos horas desde que se conoce, y a las personas afectadas cuando existe un riesgo "
        "alto para sus derechos. El delegado de protección de datos atiende las solicitudes "
        "de acceso, rectificación y supresión.",
    )
    + _section(
        "3. Dispositivos y correo corporativo",
        "Los equipos de la empresa se usan con el software autorizado y con la copia de "
        "seguridad corporativa activada. La instalación de programas no autorizados "
        "requiere una excepción aprobada por el responsable de seguridad. El correo "
        "corporativo no debe usarse para asuntos personales, y un mensaje con enlaces o "
        "adjuntos no solicitados se reenvía al buzón de seguridad sin abrirlo. La pérdida o "
        "el robo de un dispositivo se comunica de inmediato a soporte, que bloquea el "
        "equipo y revoca sus credenciales en la misma jornada.",
    ),
)


FORMACION = SampleDocument(
    title="Politica de formacion y desarrollo",
    filename="politica_formacion.md",
    body="# Política de formación y desarrollo\n\n"
    + _section(
        "1. Plan anual de formación",
        "La empresa destina cada año un presupuesto de formación por persona, que se "
        "planifica en el primer trimestre con el responsable del departamento. El plan "
        "anual recoge las acciones obligatorias por puesto, la formación en prevención de "
        "riesgos y las acciones voluntarias que la persona solicita. La solicitud de una "
        "acción formativa se presenta con quince días de antelación y requiere la "
        "aprobación del responsable, que valora su relación con el puesto y el coste. Las "
        "acciones se imparten preferentemente dentro de la jornada laboral.",
    )
    + _section(
        "2. Permisos individuales de formación",
        "El permiso individual de formación permite ausentarse hasta doscientas horas al "
        "año para cursar una titulación oficial o una certificación profesional. La "
        "solicitud se presenta con un mes de antelación, indicando el centro, el programa "
        "y el calendario. La empresa abona el coste de la matrícula cuando la acción está "
        "relacionada con el puesto, y la persona se compromete a permanecer en la empresa "
        "seis meses después de terminar. El permiso no es acumulable de un año a otro y no "
        "reduce las vacaciones que correspondan.",
    )
    + _section(
        "3. Evaluación y promoción interna",
        "La evaluación del desempeño se realiza cada seis meses con una conversación "
        "estructurada sobre objetivos, competencias y desarrollo. La promoción interna se "
        "publica en el portal del empleado durante diez días antes de abrirse al exterior, "
        "y cualquier persona que cumpla los requisitos puede presentarse. La formación "
        "obligatoria pendiente es un requisito para promocionar, y el plan de desarrollo "
        "individual recoge las acciones acordadas con el responsable para los siguientes "
        "doce meses.",
    ),
)


DOCUMENTS: tuple[SampleDocument, ...] = (
    VACACIONES,
    TELETRABAJO,
    GASTOS,
    SALUD,
    SEGURIDAD,
    FORMACION,
)


#: The questions, and which document answers each. Half are ordinary questions and
#: half name a code or an acronym the vector half is expected to miss; `kind` says
#: which, so the report can be read rather than guessed at.
QUESTIONS: tuple[SampleQuestion, ...] = (
    SampleQuestion("¿Cuántos días de vacaciones anuales retribuidas tengo?", VACACIONES.title),
    SampleQuestion("¿Cuántos días de permiso por matrimonio corresponden?", VACACIONES.title),
    SampleQuestion("¿Puedo trasladar días de vacaciones al año siguiente?", VACACIONES.title),
    SampleQuestion(
        "¿Qué establece la política GT-2026 sobre el registro horario?",
        TELETRABAJO.title,
        kind="lexical",
    ),
    SampleQuestion(
        "¿Qué dice la GT-2026 sobre la desconexión digital?", TELETRABAJO.title,
        kind="lexical",
    ),
    SampleQuestion(
        "¿Qué debo hacer si se avería el portátil de dotación?", TELETRABAJO.title
    ),
    SampleQuestion("¿Cuánto se paga por kilómetro con vehículo propio?", GASTOS.title),
    # The one question whose answer is a bare code and two figures. Its chunks share
    # almost no term with the question except the code itself — which is the case
    # §5.2 describes ("pure vector search is insensitive to policy codes") and the
    # reason `hybrid` must be at least as good as `vector`.
    SampleQuestion(
        "¿Cuál es la banda salarial B3 y su retribución de referencia?", GASTOS.title,
        kind="lexical",
    ),
    SampleQuestion(
        "¿En qué plazo se reembolsa una hoja de gastos aprobada?", GASTOS.title
    ),
    # The three distractors' own questions. Each names a term the *other* documents also
    # use — "formación", "datos", "pausa" — so the vector leg has real competition and the
    # text leg's exact match is worth something.
    SampleQuestion(
        "¿Cuántas horas de permiso individual de formación puedo pedir?", FORMACION.title
    ),
    SampleQuestion(
        "¿Cuánto dura la pausa por pantalla en la política PRL-07?", SALUD.title,
        kind="lexical",
    ),
    SampleQuestion(
        "¿En qué plazo se notifica una brecha de datos al RGPD?", SEGURIDAD.title,
        kind="lexical",
    ),
)


__all__ = [
    "DOCUMENTS",
    "FORMACION",
    "GASTOS",
    "QUESTIONS",
    "SALUD",
    "SEGURIDAD",
    "TELETRABAJO",
    "VACACIONES",
    "SampleDocument",
    "SampleQuestion",
]
