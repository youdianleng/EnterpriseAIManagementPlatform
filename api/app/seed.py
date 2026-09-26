"""Demo data in one command.

    python -m app.seed              # load (safe to run again)
    python -m app.seed --verify     # report what is there, change nothing

A hundred employees, not three. Permission filtering, pagination and list pages
have to be exercised at a size where they can actually be wrong, and a dataset
that fits on one screen hides exactly the bugs this system exists to prevent.

**Idempotent by natural key.** Departments are looked up by code, positions by
code, employees by email, accounts by username. Running it twice adds nothing and
changes nothing — which is what makes it safe to run against a database that
already has data in it.

**Only the fields the design allows.** Name, contact and company data. No
identity numbers, no bank details, no health data, no biometrics: `docs/DESIGN.md`
Q9 lists the permitted fields, and a database constraint refuses the rest.

Everything goes through the domain services, so the seed cannot create a state the
product would refuse: the depth limit, the clearance rules, one-account-per-person
and the Argon2id hash all apply here exactly as they do to a request.
"""

import argparse
import asyncio
import random
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cache import RedisSessionRevoker, invalidate_org_tree
from app.config import get_settings
from app.db import build_engine
from app.domain.account.models import UserAccountInput
from app.domain.account.service import AccountService
from app.domain.employee.models import (
    AssignmentInput,
    EmployeeInput,
    EmployeePrivate,
)
from app.domain.employee.service import EmployeeService
from app.domain.org.models import ClearanceLevel, DepartmentInput
from app.domain.org.service import DepartmentService
from app.domain.position.models import PositionInput
from app.domain.position.service import PositionService
from app.repositories.account import PostgresAccountRepository
from app.repositories.employee import PostgresEmployeeRepository
from app.repositories.org import PostgresDepartmentRepository
from app.repositories.position import PostgresPositionRepository

#: Fixed so two machines loading the seed get the same people. A demo that
#: differs per run makes "the list looked different yesterday" unanswerable.
SEED = 20260101

#: The tree: code, Spanish and English names, parent, clearance, and the
#: positions that belong to it. Four levels — root, area, team, and one team
#: split again — because the depth rules and the subtree queries only see a
#: difference at the fourth level.
DEPARTMENTS: tuple[dict, ...] = (
    {
        "code": "direccion",
        "name_es": "Dirección General",
        "name_en": "Executive Office",
        "parent": None,
        "clearance": ClearanceLevel.HIGH,
        "cost_center": "CC-100",
        "positions": (
            ("dir-general", "Director General", "General Manager", True),
            ("dir-asistente", "Asistente de Dirección", "Executive Assistant", False),
        ),
    },
    {
        "code": "rrhh",
        "name_es": "Recursos Humanos",
        "name_en": "Human Resources",
        "parent": "direccion",
        "clearance": ClearanceLevel.HIGH,
        "cost_center": "CC-200",
        "positions": (
            ("rrhh-responsable", "Responsable de Recursos Humanos", "HR Manager", True),
            ("rrhh-tecnico", "Técnico de Recursos Humanos", "HR Specialist", False),
            ("rrhh-seleccion", "Técnico de Selección", "Recruiter", False),
        ),
    },
    {
        "code": "tecnologia",
        "name_es": "Tecnología",
        "name_en": "Technology",
        "parent": "direccion",
        "clearance": ClearanceLevel.MEDIUM,
        "cost_center": "CC-300",
        "positions": (
            ("tec-director", "Director de Tecnología", "Technology Director", True),
            ("tec-arquitecto", "Arquitecto de Software", "Software Architect", False),
        ),
    },
    {
        "code": "desarrollo",
        "name_es": "Desarrollo de Software",
        "name_en": "Software Development",
        "parent": "tecnologia",
        "clearance": ClearanceLevel.MEDIUM,
        "cost_center": "CC-310",
        "positions": (
            ("dev-lead", "Líder Técnico", "Technical Lead", True),
            ("dev-senior", "Desarrollador Senior", "Senior Developer", False),
            ("dev-junior", "Desarrollador Junior", "Junior Developer", False),
            ("dev-qa", "Ingeniero de Calidad", "QA Engineer", False),
        ),
    },
    {
        "code": "movil",
        "name_es": "Aplicaciones Móviles",
        "name_en": "Mobile Applications",
        "parent": "desarrollo",
        "clearance": ClearanceLevel.MEDIUM,
        "cost_center": "CC-311",
        "positions": (
            ("mov-responsable", "Responsable de Móvil", "Mobile Lead", True),
            ("mov-ingeniero", "Ingeniero de Aplicaciones Móviles", "Mobile Engineer", False),
        ),
    },
    {
        "code": "datos",
        "name_es": "Datos y Analítica",
        "name_en": "Data and Analytics",
        "parent": "desarrollo",
        "clearance": ClearanceLevel.HIGH,
        "cost_center": "CC-312",
        "positions": (
            ("dat-responsable", "Responsable de Datos", "Head of Data", True),
            ("dat-cientifico", "Científico de Datos", "Data Scientist", False),
            ("dat-analista", "Analista de Datos", "Data Analyst", False),
        ),
    },
    {
        "code": "infraestructura",
        "name_es": "Infraestructura",
        "name_en": "Infrastructure",
        "parent": "tecnologia",
        "clearance": ClearanceLevel.MEDIUM,
        "cost_center": "CC-320",
        "positions": (
            ("infra-responsable", "Responsable de Infraestructura", "Head of Infrastructure", True),
            ("infra-sysadmin", "Administrador de Sistemas", "Systems Administrator", False),
            ("infra-sre", "Ingeniero de Plataforma", "Platform Engineer", False),
        ),
    },
    {
        "code": "finanzas",
        "name_es": "Finanzas",
        "name_en": "Finance",
        "parent": "direccion",
        "clearance": ClearanceLevel.HIGH,
        "cost_center": "CC-400",
        "positions": (
            ("fin-responsable", "Responsable Financiero", "Finance Manager", True),
            ("fin-contable", "Contable", "Accountant", False),
        ),
    },
    {
        "code": "contabilidad",
        "name_es": "Contabilidad",
        "name_en": "Accounting",
        "parent": "finanzas",
        "clearance": ClearanceLevel.HIGH,
        "cost_center": "CC-410",
        "positions": (
            ("con-responsable", "Responsable de Contabilidad", "Head of Accounting", True),
            ("con-auxiliar", "Auxiliar Contable", "Accounting Assistant", False),
            ("con-facturacion", "Técnico de Facturación", "Billing Specialist", False),
        ),
    },
    {
        "code": "comercial",
        "name_es": "Comercial",
        "name_en": "Sales",
        "parent": "direccion",
        "clearance": ClearanceLevel.LOW,
        "cost_center": "CC-500",
        "positions": (
            ("com-director", "Director Comercial", "Sales Director", True),
            ("com-ejecutivo", "Ejecutivo de Cuentas", "Account Executive", False),
        ),
    },
    {
        "code": "marketing",
        "name_es": "Marketing",
        "name_en": "Marketing",
        "parent": "comercial",
        "clearance": ClearanceLevel.LOW,
        "cost_center": "CC-510",
        "positions": (
            ("mkt-responsable", "Responsable de Marketing", "Marketing Manager", True),
            ("mkt-especialista", "Especialista en Marketing", "Marketing Specialist", False),
            ("mkt-contenido", "Redactor de Contenidos", "Content Writer", False),
        ),
    },
)

#: How the hundred are spread. Areas with more people get more rows, so a
#: department list is not uniform — a page that looks right on three identical
#: departments can still be wrong on unequal ones.
HEADCOUNT: dict[str, int] = {
    "direccion": 4,
    "rrhh": 8,
    "tecnologia": 6,
    "desarrollo": 22,
    "movil": 9,
    "datos": 8,
    "infraestructura": 10,
    "finanzas": 5,
    "contabilidad": 7,
    "comercial": 12,
    "marketing": 9,
}

FIRST_NAMES = (
    "Ana", "Carlos", "María", "Javier", "Lucía", "Miguel", "Elena", "Pablo",
    "Carmen", "Sergio", "Laura", "Alberto", "Marta", "Diego", "Nuria", "Raúl",
    "Isabel", "Andrés", "Paula", "Iván", "Rocío", "Óscar", "Silvia", "Jorge",
    "Beatriz", "Adrián", "Cristina", "Rubén", "Natalia", "Víctor", "Alicia",
    "Fernando", "Sara", "Gonzalo", "Irene", "Hugo", "Claudia", "Álvaro",
)
LAST_NAMES = (
    "García", "Martínez", "López", "Sánchez", "Pérez", "Gómez", "Martín",
    "Jiménez", "Ruiz", "Hernández", "Díaz", "Moreno", "Muñoz", "Álvarez",
    "Romero", "Alonso", "Gutiérrez", "Navarro", "Torres", "Domínguez",
    "Vázquez", "Ramos", "Gil", "Ramírez", "Serrano", "Blanco", "Molina",
    "Morales", "Suárez", "Ortega", "Delgado", "Castro", "Ortiz", "Rubio",
)
CITIES = (
    ("Madrid", "28001"),
    ("Barcelona", "08001"),
    ("Valencia", "46001"),
    ("Sevilla", "41001"),
    ("Bilbao", "48001"),
    ("Málaga", "29001"),
)

#: The logins the demo needs to be usable: administration, HR, one manager and one
#: ordinary employee. Everyone else exists as a record; creating accounts for all
#: hundred would cost a hundred Argon2id hashes for no extra coverage.
DEMO_LOGINS = (
    ("admin", "direccion", "dir-general", ("admin",)),
    ("rrhh", "rrhh", "rrhh-responsable", ("hr",)),
    ("devlead", "desarrollo", "dev-lead", ("employee",)),
    ("empleado", "marketing", "mkt-especialista", ("employee",)),
)


@dataclass(slots=True)
class Seeded:
    """What one run created, as opposed to found already there."""

    departments: int = 0
    positions: int = 0
    employees: int = 0
    assignments: int = 0
    logins: int = 0

    def summary(self) -> str:
        return (
            f"{self.departments} departments, {self.positions} positions, "
            f"{self.employees} employees, {self.assignments} assignments, "
            f"{self.logins} new logins"
        )


class Seeder:
    """Loads the demo dataset through the same services the API uses."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._departments = PostgresDepartmentRepository(session)
        self._positions = PostgresPositionRepository(session)
        self._employees = PostgresEmployeeRepository(session)
        self._accounts = PostgresAccountRepository(session)

        self.department_service = DepartmentService(
            repository=self._departments, invalidate=invalidate_org_tree
        )
        self.position_service = PositionService(
            repository=self._positions, departments=self._departments
        )
        self.employee_service = EmployeeService(
            repository=self._employees, departments=self._departments
        )
        self.account_service = AccountService(
            repository=self._accounts,
            session=session,
            revoker=RedisSessionRevoker(),
        )
        self.rng = random.Random(SEED)
        self.created = Seeded()
        #: code -> (department id, parent code), filled as the tree is walked.
        self.department_ids: dict[str, UUID] = {}
        #: position code -> position id
        self.position_ids: dict[str, UUID] = {}

    # --- the tree ----------------------------------------------------------

    async def load_departments(self) -> None:
        # Parents come before children in DEPARTMENTS, which is what lets the
        # parent id be looked up rather than searched for.
        for spec in DEPARTMENTS:
            existing = await self._departments.get_by_code(spec["code"])
            if existing is not None:
                self.department_ids[spec["code"]] = existing.id
                continue

            parent_id = (
                self.department_ids[spec["parent"]] if spec["parent"] else None
            )
            created = await self.department_service.create(
                DepartmentInput(
                    code=spec["code"],
                    name_es=spec["name_es"],
                    name_en=spec["name_en"],
                    parent_id=parent_id,
                    clearance_level=spec["clearance"],
                    cost_center=spec["cost_center"],
                    description_es=f"{spec['name_es']} de la empresa.",
                    description_en=f"Company {spec['name_en']}.",
                )
            )
            self.department_ids[spec["code"]] = created.id
            self.created.departments += 1

        for spec in DEPARTMENTS:
            for code, title_es, title_en, managerial in spec["positions"]:
                if code in self.position_ids:
                    continue
                created = await self.position_service.create(
                    PositionInput(
                        code=code,
                        title_es=title_es,
                        title_en=title_en,
                        department_id=self.department_ids[spec["code"]],
                        is_managerial=managerial,
                    )
                )
                self.position_ids[code] = created.id
                self.created.positions += 1

    async def index_positions(self) -> None:
        """Remember what is already in the catalogue, so a second run adds nothing."""
        for position in await self.position_service.list_positions():
            self.position_ids.setdefault(position.code, position.id)

    # --- people ------------------------------------------------------------

    async def load_employees(self) -> None:
        serial = 0
        for spec in DEPARTMENTS:
            code = spec["code"]
            for index in range(HEADCOUNT[code]):
                serial += 1
                # A generator per person, seeded from the serial number alone. A
                # single shared generator would drift: skipping somebody who is
                # already loaded would consume a different number of draws than
                # creating them, and the next person would get a different email
                # — which is how an "idempotent" seed starts inserting duplicates
                # on its second run.
                rng = random.Random(f"{SEED}:{serial}")
                first = rng.choice(FIRST_NAMES)
                last = f"{rng.choice(LAST_NAMES)} {rng.choice(LAST_NAMES)}"
                email = f"{_slug(first)}.{_slug(last.split()[0])}{serial}@empresa.es"
                # Drawn before the existence check so that both paths consume the
                # same values: the second run must derive the same person, not a
                # different one that happens to look similar.
                city, postal = rng.choice(CITIES)
                start_date = date(2024, 1, 15) + timedelta(days=rng.randint(0, 700))

                existing = await self._employees.get_by_email(email)
                if existing is not None:
                    employee_id = existing.id
                else:
                    record = await self.employee_service.create(
                        EmployeeInput(
                            first_name=first,
                            last_name=last,
                            email=email,
                            hire_date=start_date,
                            city=city,
                            country="España",
                            private=EmployeePrivate(
                                address_line=(
                                    f"Calle {rng.choice(LAST_NAMES)}, {rng.randint(1, 120)}"
                                ),
                                postal_code=postal,
                                employee_no=f"E-{serial:04d}",
                                emergency_contact={
                                    "name": f"{rng.choice(FIRST_NAMES)} {last}",
                                    "phone": f"+34 6{rng.randint(10_000_000, 99_999_999)}",
                                },
                            ),
                        )
                    )
                    self.created.employees += 1
                    employee_id = record.employee.id

                # Positions are decided by what the person already holds, not by
                # whether this run created them. A seed that only finishes jobs
                # it started itself leaves a half-loaded database half-loaded
                # forever; this way a second run heals it.
                active = await self._active_assignment_count(employee_id)

                if active == 0:
                    # The first position a person holds becomes primary, and is
                    # part-time for roughly one in fifteen — a real case the
                    # attendance and timesheet rules have to handle.
                    position_code = self._position_for(code, index)
                    await self.employee_service.assign_position(
                        employee_id,
                        AssignmentInput(
                            department_id=self.department_ids[code],
                            job_position_id=self.position_ids[position_code],
                            start_date=start_date,
                            is_part_time=(serial % 15 == 0),
                        ),
                    )
                    self.created.assignments += 1
                    active = 1

                # Roughly one in twelve also works in another department: the
                # multi-position case D12 is about, and the one that makes a
                # department union rather than a single value.
                if active == 1 and serial % 12 == 0 and code != "direccion":
                    second = self._other_department(code, rng)
                    await self.employee_service.assign_position(
                        employee_id,
                        AssignmentInput(
                            department_id=self.department_ids[second],
                            job_position_id=self.position_ids[
                                self._position_for(second, index)
                            ],
                            start_date=start_date + timedelta(days=180),
                            is_part_time=True,
                        ),
                    )
                    self.created.assignments += 1

    async def _active_assignment_count(self, employee_id: UUID) -> int:
        count = await self._session.scalar(
            text(
                "SELECT count(*) FROM employee_assignments "
                "WHERE employee_id = :id AND end_date IS NULL"
            ),
            {"id": employee_id},
        )
        return int(count or 0)

    def _position_for(self, department_code: str, index: int) -> str:
        """The positions of a department, cycling so every one is held."""
        spec = next(item for item in DEPARTMENTS if item["code"] == department_code)
        codes = [entry[0] for entry in spec["positions"]]
        # Start past the managerial position for most people: a department whose
        # everyone is the manager has no one to approve.
        non_managerial = [
            entry[0] for entry in spec["positions"] if not entry[3]
        ] or codes
        return non_managerial[index % len(non_managerial)]

    def _other_department(self, code: str, rng: random.Random) -> str:
        candidates = [item["code"] for item in DEPARTMENTS if item["code"] != code]
        return candidates[rng.randrange(len(candidates))]

    async def load_managers(self) -> None:
        """One manager per department, in a managerial position, pointing at it.

        Every level, so an approval route exists at every level and the
        department's own manager field is not decorative.
        """
        for spec in DEPARTMENTS:
            code = spec["code"]
            department_id = self.department_ids[code]
            current = await self._departments.get(department_id)
            if current is not None and current.manager_employee_id is not None:
                continue

            managerial = next(
                (entry[0] for entry in spec["positions"] if entry[3]), None
            )
            if managerial is None:
                continue

            manager = await self._find_employee_in(code)
            if manager is None:
                continue

            await self.employee_service.assign_position(
                manager,
                AssignmentInput(
                    department_id=department_id,
                    job_position_id=self.position_ids[managerial],
                    start_date=date(2024, 1, 15),
                ),
            )
            self.created.assignments += 1
            # Written directly because no service sets the department's manager
            # yet: it is the fallback approver for assignments that name none, and
            # the person is checked to be in the department by `_find_employee_in`.
            await self._session.execute(
                text("UPDATE departments SET manager_employee_id = :manager WHERE id = :id"),
                {"manager": manager, "id": department_id},
            )
            await self._session.commit()
            await invalidate_org_tree()

    async def _find_employee_in(self, department_code: str) -> UUID | None:
        """Somebody already assigned to this department, its manager first.

        The manager is preferred so the demo logins land on the people whose
        names the org chart shows, rather than on whoever was loaded first.
        """
        rows = await self._session.execute(
            text(
                """
                SELECT a.employee_id
                FROM employee_assignments a
                JOIN departments d ON d.id = a.department_id
                WHERE d.code = :code AND a.end_date IS NULL
                ORDER BY (a.employee_id = d.manager_employee_id) DESC NULLS LAST,
                         a.start_date, a.id
                """
            ),
            {"code": department_code},
        )
        return rows.scalars().first()

    # --- logins ------------------------------------------------------------

    async def load_logins(self) -> list[tuple[str, str]]:
        """A few usable accounts. Returns (username, one-time password) pairs.

        The password is printed by the caller and never stored in the clear, same
        as the API: it is a real account, not a backdoor.
        """
        issued: list[tuple[str, str]] = []
        for username, department_code, _position_code, roles in DEMO_LOGINS:
            if await self._accounts.get_by_username(username) is not None:
                continue

            employee_id = await self._find_employee_in(department_code)
            if employee_id is None:
                continue

            result = await self.account_service.create(
                UserAccountInput(employee_id=employee_id, username=username),
                actor_user_id=None,
                actor_roles=frozenset({"system"}),
            )
            # Roles are written here rather than through an endpoint because the
            # endpoint that grants them is ticket 08b. The database validates the
            # values, so this cannot invent a role.
            await self._session.execute(
                text("UPDATE users SET roles = CAST(:roles AS jsonb) WHERE id = :id"),
                {
                    "roles": _json_array(roles),
                    "id": result.account.id,
                },
            )
            await self._session.commit()
            issued.append((username, result.temporary_password))
            self.created.logins += 1
        return issued


def _slug(value: str) -> str:
    """Lowercase ASCII, so an email address is one that could exist."""
    table = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")
    return "".join(ch for ch in value.translate(table).lower() if ch.isalnum())


def _json_array(values: tuple[str, ...]) -> str:
    import json

    return json.dumps(sorted(set(values) | {"employee"}))


# --- the commands ----------------------------------------------------------


async def load() -> None:
    engine = build_engine(get_settings())
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with factory() as session:
            seeder = Seeder(session)
            await seeder.index_positions()
            await seeder.load_departments()
            await seeder.load_employees()
            await seeder.load_managers()
            logins = await seeder.load_logins()
    finally:
        await engine.dispose()

    print(f"Seeded: {seeder.created.summary()}")
    if logins:
        print()
        print("Demo logins — these are one-time passwords, shown once:")
        for username, password in logins:
            print(f"  {username:<10} {password}")
        print()
        print("Each one must change its password at first sign-in.")
    else:
        print("Demo logins already existed; nothing reissued.")


async def verify() -> int:
    """Print the shape of the data, for a human to look at.

    Exits non-zero when something the seed promises is missing, so this is usable
    as a check rather than only as a report.
    """
    engine = build_engine(get_settings())
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    problems: list[str] = []
    try:
        async with factory() as session:
            headcount = (
                await session.execute(
                    text(
                        """
                        SELECT d.code, d.name_es, d.depth,
                               count(a.id) FILTER (WHERE a.end_date IS NULL) AS people,
                               d.manager_employee_id IS NOT NULL AS has_manager
                        FROM departments d
                        LEFT JOIN employee_assignments a ON a.department_id = d.id
                        GROUP BY d.code, d.name_es, d.depth, d.manager_employee_id,
                                 d.path
                        ORDER BY d.path::text
                        """
                    )
                )
            ).all()
            titles = (
                await session.execute(
                    text(
                        """
                        SELECT p.code, p.title_es, p.is_managerial, count(a.id) AS people
                        FROM job_positions p
                        LEFT JOIN employee_assignments a
                               ON a.job_position_id = p.id AND a.end_date IS NULL
                        GROUP BY p.code, p.title_es, p.is_managerial
                        ORDER BY p.code
                        """
                    )
                )
            ).all()
            totals = (
                await session.execute(
                    text(
                        """
                        SELECT
                          (SELECT count(*) FROM employees) AS employees,
                          (SELECT count(*) FROM employees e
                            WHERE NOT EXISTS (SELECT 1 FROM employee_assignments a
                                               WHERE a.employee_id = e.id AND a.end_date IS NULL))
                            AS unassigned,
                          (SELECT count(*) FROM employee_assignments
                            WHERE is_part_time) AS part_time,
                          (SELECT count(*) FROM employees e
                            WHERE (SELECT count(*) FROM employee_assignments a
                                    WHERE a.employee_id = e.id AND a.end_date IS NULL) > 1)
                            AS multi_position,
                          (SELECT count(*) FROM users) AS logins,
                          (SELECT count(*) FROM departments) AS departments,
                          (SELECT max(depth) FROM departments) AS depth
                        """
                    )
                )
            ).one()

        print("Employees per department")
        for code, name, depth, people, has_manager in headcount:
            flag = "" if has_manager else "   <- no manager"
            print(f"  {'  ' * depth}{code:<16} {name:<28} {people:>4}{flag}")
            if not has_manager:
                problems.append(f"{code} has no manager")

        print()
        print("Positions")
        for code, title, managerial, people in titles:
            mark = " (managerial)" if managerial else ""
            print(f"  {code:<18} {title}{mark}: {people}")

        print()
        print(
            f"Totals: {totals.employees} employees, {totals.unassigned} without a position, "
            f"{totals.multi_position} holding more than one, {totals.part_time} part-time, "
            f"{totals.departments} departments to depth {totals.depth}, {totals.logins} logins"
        )

        if totals.employees < 100:
            problems.append(f"only {totals.employees} employees")
        if totals.depth < 3:
            problems.append(f"the tree is only {totals.depth} levels deep")
        if totals.multi_position == 0:
            problems.append("nobody holds two positions")
        if totals.part_time == 0:
            problems.append("nobody is part-time")
        if totals.logins == 0:
            problems.append("no login exists")
    finally:
        await engine.dispose()

    print()
    if problems:
        print("PROBLEMS: " + "; ".join(problems))
        return 1
    print("OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.seed", description=__doc__)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="report the current data instead of loading more",
    )
    args = parser.parse_args(argv)

    if args.verify:
        return asyncio.run(verify())
    asyncio.run(load())
    return 0


if __name__ == "__main__":
    sys.exit(main())
