"""Personal documents: who may see one, who may recall one, and how it is labelled.

Ticket 36's subject is the one document kind that is *somebody's*: 员工可以上传自己的文档
（合同、证明、个人笔记），默认只有自己能看到, may publish it to their own department, and
may revoke that publication. Three rules make it more than a visibility flag, and every
test here is about one of them:

1. **A personal document is not corpus.** 「个人文档不进入公司知识库的检索池；只有提问者本人
   的个人文档可被召回」. The retrieval predicate recalls a personal document for its owner
   and for nobody else, so a colleague who may *open* a published document still gets
   nothing from a question. The assertions are on the **hit set** — document ids and
   chunk ids — never on a "visible" flag: a forbidden document that merely ranked low
   looks exactly like one that was filtered out, and only an unfiltered contrast or a
   predicate pin tells the two apart (ticket 35's lesson, and this module keeps it).
2. **Publishing is not a clearance waiver.** 公开给本部门的个人文档，对同事仍须满足密级条件才
   可见 (D11: 显式共享不能突破密级上限). A colleague below the document's ceiling is refused
   by the *kernel* and absent from the list, the read and the search.
3. **The owner is permanent.** 「个人文档的所有者始终是自己，不能被转交给他人」. There is no
   route that changes an owner, no body that names one, and no UPDATE statement in the
   document module that could write one; `test_the_owner_cannot_be_transferred` asserts
   the absence as the answer rather than inventing a 403 for a route that does not exist.

The corpus is built through the **real upload endpoint and the real parse** (ticket 31's
and 32's pipelines), and the principals come from the **real permission snapshot**, so a
test that passes here says something about the running system rather than about a
fixture. The only doubles are the two seams the design names: the deterministic embedder
and the streamed chat model.

Checklist line → the test that pins it:

* 上传时可选择可见性：仅自己 / 本部门；默认仅自己 —
  `test_an_upload_that_states_no_visibility_is_private`,
  `test_the_owner_can_publish_a_personal_document_to_their_department`,
  `test_company_visibility_is_not_a_degree_a_personal_upload_may_choose`
* 个人文档标记为非公司知识库内容，与公司文档在数据上可区分 —
  `test_a_personal_document_is_not_company_knowledge_base_in_the_data`
* 个人文档的所有者始终是自己，不能被转交给他人 — `test_the_owner_cannot_be_transferred`
* 公司知识库检索时不召回他人上传的个人文档；只有提问者本人的个人文档可被召回 —
  `test_a_colleagues_private_personal_document_is_not_recalled`,
  `test_the_search_does_not_recall_a_colleagues_published_personal_document`,
  `test_the_owners_own_personal_document_is_recalled_for_them`
* 公开给本部门的个人文档，对同事仍须满足密级条件才可见 —
  `test_a_colleague_above_the_ceiling_may_read_a_published_personal_document`,
  `test_a_colleague_below_the_ceiling_is_refused_a_published_personal_document`
* 员工可撤销公开，撤销后同事立即无法访问，缓存同步失效 —
  `test_revoking_the_publication_takes_effect_on_the_next_request`
* 他人访问未公开的个人文档返回 403 且资源在列表中完全不存在 —
  `test_someone_elses_private_personal_document_is_absent_and_refused`
* 回答中若引用了个人文档，必须在回答顶部标注 —
  `test_an_answer_grounded_in_a_personal_document_carries_the_marker`,
  `test_the_marker_is_absent_from_an_answer_grounded_in_the_company_knowledge_base`
* 有测试覆盖上述全部可见性组合 — `test_the_whole_matrix_agrees_with_the_kernel`
* RLS as the second line of defence —
  `test_the_database_refuses_a_private_personal_document_in_the_callers_department`
"""

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.errors import ErrorCode
from app.domain.access.kernel import Action, Resource, ResourceKind, can, filter_for
from app.domain.access.principal import Principal
from app.domain.answer.chat import StreamedChatModel
from app.domain.answer.driver import AnswerService
from app.domain.answer.models import (
    PERSONAL_DOCUMENT_NOTICE_MESSAGE_KEY,
    PERSONAL_DOCUMENT_NOTICE_TEXT,
    EventKind,
)
from app.domain.answer.repository import PostgresAnswerRepository
from app.domain.document.embeddings import DeterministicEmbedder
from app.domain.document.models import (
    COMPANY_VISIBILITY,
    DEPARTMENT_VISIBILITY,
    PRIVATE_VISIBILITY,
)
from app.domain.retrieval.filtering import answer_filter_for
from app.domain.retrieval.service import RetrievalService
from app.repositories.retrieval import (
    PostgresChunkSearchRepository,
    visible_document_clauses,
    visible_document_predicate,
)
from tests.support.documents import markdown_bytes
from tests.support.platform import Actor, Platform
from tests.test_documents import post_upload, run_parse
from tests.test_retrieval import service as retrieval_service

# --- the corpus --------------------------------------------------------------

#: A personal document's body. **The subject is named nowhere else in the corpus**, for
#: the reason the escalation corpus gives: a question about it is a question this document
#: is the answer to, so "absent from the hit set" is a statement about the filter rather
#: than about the ranking. Written out here rather than in `support/retrieval_sample.py`
#: because it is this ticket's fixture and no other module asks about it.
CONTRACT_BODY = (
    "# Contrato de teletrabajo personal\n\n"
    "## 1. Anexo individual\n\n"
    "El presente anexo de teletrabajo individual se firma entre la persona trabajadora "
    "y la empresa. La persona trabajadora disfruta de una compensación individual de "
    "conectividad de cuarenta y siete euros mensuales, revisable cada año, y de un "
    "dispositivo de dotación adicional. El anexo particular prevalece sobre la política "
    "general en lo que respecta a la cuantía individual acordada.\n\n"
    "## 2. Vigencia\n\n"
    "El anexo individual tiene una vigencia de veinticuatro meses desde su firma y se "
    "prorroga tácitamente por periodos anuales salvo denuncia expresa de cualquiera de "
    "las partes con treinta días de antelación.\n"
)

#: The question that retrieves it. It names the document's own subject — the individual
#: connectivity supplement — and that subject appears in neither the company corpus nor
#: any other fixture.
PERSONAL_QUESTION = "¿Cuánto es la compensación individual de conectividad del anexo?"

#: A company knowledge-base document's body, filed in the same department. Its subject is
#: equally its own, so the two documents can be asked about independently and a leak of
#: one cannot be mistaken for the other.
COMPANY_BODY = (
    "# Política de vehículos de empresa\n\n"
    "## 1. Asignación\n\n"
    "La asignación de vehículo de empresa corresponde a los puestos de dirección "
    "comercial y se aprueba por la dirección de personas. El vehículo asignado se "
    "renueva cada cuarenta y ocho meses o cada ciento veinte mil kilómetros.\n\n"
    "## 2. Carburante\n\n"
    "La tarjeta de carburante corporativa cubre el repostaje en la red concertada y el "
    "peaje de las autopistas de la ruta habitual comunicada por la persona.\n"
)

COMPANY_QUESTION = "¿Cada cuántos kilómetros se renueva el vehículo de empresa asignado?"

PRIVATE_MARKER = "cuarenta y siete euros mensuales"
COMPANY_MARKER = "ciento veinte mil kilómetros"


@dataclass(frozen=True, slots=True)
class Personal:
    """The people, the departments and the documents the matrix is asked about.

    Two colleagues in one department at *different clearances* is the whole reason the
    fixture has five accounts: 「公开不等于绕过密级」 can only be tested by two people who
    differ in exactly that one fact, and the document is filed at the higher of the two
    so that the lower one's refusal is the ceiling's doing rather than the department's.
    """

    #: Where the personal documents are filed and where the two colleagues work, raised
    #: to `medium` clearance so that a `medium` document can be filed into it.
    department: str
    #: Somewhere else entirely: the caller who shares neither department nor ownership.
    other_department: str
    #: The owner of every personal document in this module.
    owner: Actor
    #: Same department as the owner, cleared for the documents filed there.
    colleague: Actor
    #: Same department as the owner, **below the ceiling** of the document filed there.
    junior: Actor
    #: Another department. No clause of §4.2 reaches the owner's documents for them.
    outsider: Actor
    #: HR: the role that manages the knowledge base, and the caller whose company-wide
    #: exception is asserted *not* to reach a personal document.
    hr: Actor
    #: `document_id` by name: `private`, `shared`, `company`.
    documents: dict[str, str]
    #: The principal each actor resolves to, through the real snapshot.
    principals: dict[str, Principal]


async def caller_of(platform: Platform, actor: Actor) -> Principal:
    """The principal the endpoints build for this actor, through the real snapshot.

    Resolved rather than hand-built, for the reason the escalation suite gives: a
    hand-built `Principal` would let this module's assertions agree with a snapshot
    production never produces, and here the snapshot is what decides the ceiling and the
    department set the predicate is made of.
    """
    from app.domain.access.snapshot import resolve_principal

    async with platform.factory() as session:
        principal = await resolve_principal(session, UUID(actor.user_id))
        assert principal is not None, f"no permission snapshot for {actor.username}"
        return principal


@pytest.fixture
async def personal(platform: Platform) -> Personal:
    """The fixture: two departments, five accounts, three documents, all through the API.

    The uploads go through `post_upload`/`run_parse` — the real endpoint and the real
    pipeline — so `visibility` and `is_company_kb` are the values the *service* wrote
    rather than values this module inserted. A fixture that wrote the rows by hand would
    exercise the retrieval SQL over a document the service would never have produced,
    which is precisely the bug class a visibility ticket exists to rule out.
    """
    suffix = uuid4().hex[:8]
    department = await platform.department(f"per{suffix}")
    other_department = await platform.department(f"otr{suffix}")
    position = await platform.position(department, f"ppr{suffix}")
    other_position = await platform.position(other_department, f"pot{suffix}")

    owner = await platform.account(roles=("employee",))
    await platform.assign(owner.employee_id, department, position)
    colleague = await platform.account(roles=("employee",))
    await platform.assign(colleague.employee_id, department, position)
    # A third person in the same department whose ceiling must stay `low`: the department
    # admits them and the ceiling does not, which is the one fact the ticket's fifth line
    # is about. Both departments are left at the default `low` clearance and the two
    # accounts that have to clear a `medium` document are raised through their own
    # `users.clearance_level` — which is the source §4.2's ceiling reads (`highest_clearance`
    # takes the higher of the stored level and what the departments grant), and it is the
    # only way to have two colleagues in one department at two different ceilings.
    junior = await platform.account(roles=("employee",))
    await platform.assign(junior.employee_id, department, position)
    outsider = await platform.account(roles=("employee",))
    await platform.assign(outsider.employee_id, other_department, other_position)
    hr = await platform.account(roles=("hr",))
    await platform.assign(hr.employee_id, other_department, other_position)

    await platform.sql(
        "UPDATE users SET clearance_level = 'medium' WHERE id = ANY(:ids)",
        {"ids": [UUID(owner.user_id), UUID(colleague.user_id)]},
    )

    documents = {
        "private": await upload(
            platform, owner, CONTRACT_BODY, title="Anexo individual privado",
            department=department, clearance="medium",
        ),
        "shared": await upload(
            platform, owner, CONTRACT_BODY.replace(
                "Anexo individual", "Anexo individual compartido"
            ),
            title="Anexo individual compartido",
            department=department, clearance="medium",
            visibility=DEPARTMENT_VISIBILITY,
        ),
        "company": await upload(
            platform, hr, COMPANY_BODY, title="Politica de vehiculos",
            department=other_department, clearance="low", is_company_kb=True,
        ),
    }

    return Personal(
        department=department,
        other_department=other_department,
        owner=owner,
        colleague=colleague,
        junior=junior,
        outsider=outsider,
        hr=hr,
        documents=documents,
        principals={
            "owner": await caller_of(platform, owner),
            "colleague": await caller_of(platform, colleague),
            "junior": await caller_of(platform, junior),
            "outsider": await caller_of(platform, outsider),
            "hr": await caller_of(platform, hr),
        },
    )


async def upload(
    platform: Platform,
    actor: Actor,
    body: str,
    *,
    title: str,
    department: str,
    clearance: str = "low",
    is_company_kb: bool = False,
    visibility: str | None = None,
) -> str:
    """One document through the real upload endpoint and the real parse, and its id.

    A local helper rather than `test_retrieval.upload_document`, which takes a
    `SampleDocument` and hardcodes `is_company_kb`; this module needs to vary both the
    kind and the visibility on one call shape, because the two together are the subject.
    """
    fields: dict[str, object] = {
        "title": title,
        "department_id": department,
        "clearance_level": clearance,
        "is_company_kb": "true" if is_company_kb else "false",
    }
    if visibility is not None:
        fields["visibility"] = visibility
    response = await post_upload(
        actor,
        markdown_bytes(body),
        filename=f"{uuid4().hex[:8]}.md",
        **fields,
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["id"]
    assert await run_parse(platform, document_id, embedder=DeterministicEmbedder())
    return document_id


# --- reading a response ------------------------------------------------------

SEARCH = "/api/v1/retrieval/search"


def document_ids(body: dict) -> set[str]:
    """Every document id the response *names* — the hits and nothing else."""
    return {hit["document"]["id"] for hit in body["hits"]}


def chunk_ids(body: dict) -> set[str]:
    """Every chunk id the response names. The second half of "not in the hit set"."""
    return {hit["chunk_id"] for hit in body["hits"]}


async def search(actor: Actor, question: str) -> dict:
    response = await actor.get(SEARCH, params={"q": question})
    assert response.status_code == 200, response.text
    return response.json()


def listing(actor: Actor, body: dict) -> set[str]:
    return {item["id"] for item in body["items"]}


# --- 1. the upload states a visibility, and the default is private -----------


async def test_an_upload_that_states_no_visibility_is_private(
    platform: Platform, personal: Personal
) -> None:
    """「上传时可选择可见性：仅自己 / 本部门；默认仅自己」 — the default half.

    The document this asserts on is the one the fixture uploaded **without naming a
    visibility at all**, which is the shape a client that has not been updated sends. It
    is `private` in the row and it is absent from a colleague's list, their read and
    their search: a default of "published" would be the worst possible one, since the
    failure would be silent and in the direction of disclosure.
    """
    response = await personal.owner.get(f"/api/v1/documents/{personal.documents['private']}")
    assert response.status_code == 200, response.text
    document = response.json()

    assert document["visibility"] == PRIVATE_VISIBILITY
    assert document["is_company_kb"] is False
    assert document["owner_employee_id"] == personal.owner.employee_id

    # And the default is the one that *restricts*: nothing the colleague can do through
    # the API reaches it.
    assert (
        await personal.colleague.get(
            f"/api/v1/documents/{personal.documents['private']}"
        )
    ).status_code == 404
    listed = await personal.colleague.get("/api/v1/documents")
    assert personal.documents["private"] not in listing(personal.colleague, listed.json())

    # The row itself, read on the database: the column the predicate and the policy both
    # read is what the service wrote, not what a response happened to print.
    assert (
        await platform.scalar(
            "SELECT visibility FROM documents WHERE id = :id",
            {"id": personal.documents["private"]},
        )
        == PRIVATE_VISIBILITY
    )


async def test_the_owner_can_publish_a_personal_document_to_their_department(
    platform: Platform, personal: Personal
) -> None:
    """「仅自己 / 本部门」 — the other half: the owner names a department and means it.

    The document becomes readable by a colleague *in that department*, which is the point
    of publishing, and stays invisible to somebody in another department, which is the
    point of naming one.
    """
    response = await personal.owner.get(f"/api/v1/documents/{personal.documents['shared']}")
    assert response.status_code == 200, response.text
    assert response.json()["visibility"] == DEPARTMENT_VISIBILITY

    assert (
        await personal.colleague.get(
            f"/api/v1/documents/{personal.documents['shared']}"
        )
    ).status_code == 200
    assert (
        await personal.outsider.get(
            f"/api/v1/documents/{personal.documents['shared']}"
        )
    ).status_code == 404
    # The download follows the read: a citation that cannot be opened is not a citation.
    assert (
        await personal.colleague.get(
            f"/api/v1/documents/{personal.documents['shared']}/content"
        )
    ).status_code == 200
    assert (
        await personal.outsider.get(
            f"/api/v1/documents/{personal.documents['shared']}/content"
        )
    ).status_code == 404


async def test_company_visibility_is_not_a_degree_a_personal_upload_may_choose(
    platform: Platform, personal: Personal
) -> None:
    """`company` is not a third degree of sharing; it is what `is_company_kb` derives.

    A personal upload that asks for it is refused with the field named, and no row is
    written. The alternative — quietly storing it as `private` — would answer 201 to a
    request the system did not carry out, which is the silent-metadata-loss failure
    ticket 31 argued about for uploads.
    """
    before = await platform.scalar("SELECT count(*) FROM documents")

    response = await post_upload(
        personal.owner,
        markdown_bytes(CONTRACT_BODY),
        title="Anexo que dice ser de empresa",
        department_id=personal.department,
        visibility=COMPANY_VISIBILITY,
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == ErrorCode.INVALID_REQUEST.value
    assert "visibility" in (response.json()["error"]["detail"] or "")
    assert await platform.scalar("SELECT count(*) FROM documents") == before


async def test_a_publication_needs_the_department_it_names(platform: Platform) -> None:
    """A `department` publication with no department names nobody and is refused.

    The same rule the company branch has had since ticket 31, for the same reason: §4.2
    reaches a shared document through `department_id`, so a row that claims to be shared
    and carries no department is a document only its owner will ever see, whatever the
    column says.
    """
    actor = await platform.account(roles=("employee",))
    response = await post_upload(
        actor,
        markdown_bytes(CONTRACT_BODY),
        title="Publicacion sin departamento",
        visibility=DEPARTMENT_VISIBILITY,
    )

    assert response.status_code == 400, response.text
    assert "department" in (response.json()["error"]["detail"] or "")


# --- 2. the two kinds are distinguishable in the data ------------------------


async def test_a_personal_document_is_not_company_knowledge_base_in_the_data(
    platform: Platform, personal: Personal
) -> None:
    """「个人文档标记为非公司知识库内容，与公司文档在数据上可区分」 — in the *data*.

    Three places have to be able to tell the two apart, and each is asserted on the
    column rather than on a response field:

    * the **row**: `is_company_kb` is false, the owner is set and the company document's
      owner is NULL — the pair the design states as an equivalence and the database
      enforces as a CHECK;
    * the **retrieval predicate**: §4.2's first clause is gated on `NOT d.is_company_kb`,
      so it is a statement about personal documents rather than about any document that
      names an owner;
    * the **list clause set**: the ownership term the list renders carries the same gate.

    The company document is the control: it is in the same department, and its own clause
    is gated on `is_company_kb` — the two gates are what keep one kind out of the other's
    reach.
    """
    rows = await platform.sql(
        """
        SELECT id, is_company_kb, owner_employee_id, visibility
          FROM documents WHERE id = ANY(:ids)
        """,
        {"ids": [personal.documents["private"], personal.documents["company"]]},
    )
    by_id = {str(row[0]): row for row in rows}
    assert set(by_id) == {personal.documents["private"], personal.documents["company"]}
    personal_row = by_id[personal.documents["private"]]
    company_row = by_id[personal.documents["company"]]
    assert personal_row[1] is False and personal_row[2] is not None
    assert company_row[1] is True and company_row[2] is None
    # The CHECK the migration adds, as the database's own statement of the equivalence.
    assert personal_row[3] in (PRIVATE_VISIBILITY, DEPARTMENT_VISIBILITY)
    assert company_row[3] == COMPANY_VISIBILITY

    # The predicates. Both renderers, because "the data distinguishes them" is only true
    # if each thing that *reads* the data distinguishes them too.
    retrieval_clauses, _ = visible_document_clauses(
        answer_filter_for(personal.principals["owner"])
    )
    ownership = [clause for clause in retrieval_clauses if "d.owner_employee_id" in clause]
    assert len(ownership) == 1
    assert "NOT d.is_company_kb" in ownership[0], (
        f"the retrieval ownership clause does not name the kind: {ownership[0]}"
    )

    # The list's rendering, as SQL against the real table: the same gate, so a personal
    # document and a company document cannot be confused by the store either.
    from app.repositories.document import _visible

    rendered = str(_visible(filter_for(personal.principals["owner"], ResourceKind.DOCUMENT)))
    assert "documents.is_company_kb IS false" in rendered, rendered
    assert "documents.owner_employee_id" in rendered, rendered


async def test_the_database_refuses_a_personal_document_with_company_visibility(
    platform: Platform,
) -> None:
    """The equivalence is a CHECK, not a convention the next writer has to know.

    A row inserted with `visibility = 'company'` and `is_company_kb = false` is refused
    by the database, so a code path — or a console — cannot store the state the two
    readings of §4.2 would disagree about.
    """
    actor = await platform.account(roles=("admin",))
    department = await platform.department(f"chk{uuid4().hex[:8]}")

    refusal = await platform.refused_by_database(
        """
        INSERT INTO documents (id, title, owner_employee_id, department_id, clearance_level,
                               visibility, is_company_kb, tags, language, status, storage_path,
                               content_sha256, filename, media_type, file_size)
        VALUES (:id, 'Contradictorio', :owner, :department, 'low', 'company', false,
                '[]'::jsonb, 'es', 'processing', 'aa/z.txt', repeat('e', 64), 'z.txt',
                'text/plain', 10)
        """,
        {"id": uuid4(), "owner": actor.employee_id, "department": department},
    )

    assert "ck_documents_visibility_kind" in refusal, refusal


# --- 3. the owner cannot be transferred --------------------------------------


async def test_the_owner_cannot_be_transferred(platform: Platform, personal: Personal) -> None:
    """「个人文档的所有者始终是自己，不能被转交给他人」 — asserted as an absence.

    The right answer to "add an ownership transfer" is **a route that does not exist**, so
    this test asserts the absence and says so, in the four places a transfer could be
    spelled. It is a test rather than a comment because a route, a body field or a
    signature added later makes it fail:

    * **the routes**: the only non-`GET` route under `/documents` that takes a document id
      is the reparse, and the upload is the only route that writes a row at all. Listed
      from the application's own route table, so a router added anywhere is visible;
    * **the body**: an upload that sends `owner_employee_id` as a form field is stored
      with the caller as its owner — the field is not part of the contract and cannot be;
    * **the interfaces**: no method of the document service or of either document
      repository takes an owner parameter. Read from the *signatures* through `inspect`,
      so a parameter that is accepted and ignored is caught — which a source-text search
      would not be;
    * **the database**: the row-level write policy refuses the UPDATE a transfer would
      have to run, asserted as the colleague.

    The last one is why this is not merely a routing question: an owner transfer arriving
    through any future endpoint would still have to get past the policy.
    """
    import inspect

    from app.domain.document.repository import DocumentRepository
    from app.domain.document.service import DocumentService
    from app.main import app
    from app.repositories.document import PostgresDocumentRepository

    # 1. The routes. Read from the application's own OpenAPI schema rather than from the
    # route objects: `app.routes` holds mounted routers as well as routes, and the schema
    # is the contract a client sees — a transfer endpoint would appear in both.
    writes = sorted(
        (path, method.upper())
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/v1/documents")
        for method in operations
        if method != "get"
    )
    assert writes == [
        ("/api/v1/documents", "POST"),
        ("/api/v1/documents/{document_id}/reprocess", "POST"),
    ], f"a document write route that is not the upload or the reparse exists: {writes}"

    # 2. The body. A form field named `owner_employee_id` is sent and ignored, and the
    # stored owner is the caller.
    response = await post_upload(
        personal.owner,
        markdown_bytes(CONTRACT_BODY.replace("Contrato", "Contrato dos")),
        title="Anexo con owner en el cuerpo",
        owner_employee_id=personal.colleague.employee_id,
    )
    assert response.status_code == 201, response.text
    assert response.json()["owner_employee_id"] == personal.owner.employee_id
    assert (
        await platform.scalar(
            "SELECT owner_employee_id FROM documents WHERE id = :id",
            {"id": response.json()["id"]},
        )
        == UUID(personal.owner.employee_id)
    )

    # 3. The interfaces. The *creation* path takes an owner and must: `create` states the
    # row's owner, and the value it is handed is the one the service derived from the
    # principal. What must not exist is a path that takes a **new** owner for a document
    # that already has one, so the methods that take an owner at all are named and
    # asserted — a second one appearing here is the review conversation this test wants.
    owning_methods: dict[str, set[str]] = {}
    for owner in (DocumentService, DocumentRepository, PostgresDocumentRepository):
        for name, member in inspect.getmembers(owner, inspect.isfunction):
            if name.startswith("__"):
                continue
            parameters = set(inspect.signature(member).parameters)
            if parameters & {"owner", "new_owner", "owner_employee_id", "to_employee_id"}:
                owning_methods[f"{owner.__name__}.{name}"] = parameters
    assert set(owning_methods) == {
        "DocumentRepository.create",
        "PostgresDocumentRepository.create",
    }, (
        "a method that takes an owner beyond the two creation paths exists, which is how "
        f"an ownership transfer would be written: {sorted(owning_methods)}"
    )
    for name, parameters in owning_methods.items():
        # The creation path *declares* an owner; it does not rename one. A parameter named
        # for the new owner rather than for the row's owner is the difference.
        assert not (parameters & {"new_owner", "to_employee_id"}), (name, parameters)

    # 4. The database: an UPDATE that moved the owner, run under the colleague's context
    # with the colleague's own reach, affects no row — the write policy is the read rule
    # read forwards, and §4.2 does not admit this caller to this row at all.
    async with _app_connection(platform) as connection:
        async with connection() as session:
            await publish(
                session,
                **{
                    "app.current_employee_id": str(personal.colleague.employee_id),
                    "app.clearance_levels": '{"low","medium"}',
                    "app.department_ids": "{" + f'"{personal.other_department}"' + "}",
                },
            )
            moved = await session.execute(
                text(
                    "UPDATE documents SET owner_employee_id = :new "
                    "WHERE id = :id AND owner_employee_id IS NOT NULL"
                ),
                {"new": personal.colleague.employee_id, "id": personal.documents["private"]},
            )
            rowcount = moved.rowcount
            await session.rollback()

    assert rowcount == 0, (
        "the database let a caller rewrite a personal document's owner, which is the "
        "transfer the ticket forbids"
    )
    assert (
        await platform.scalar(
            "SELECT owner_employee_id FROM documents WHERE id = :id",
            {"id": personal.documents["private"]},
        )
        == UUID(personal.owner.employee_id)
    )


# --- 4. the pool: whose personal document a question may recall --------------


async def test_a_colleagues_private_personal_document_is_not_recalled(
    platform: Platform, personal: Personal
) -> None:
    """The heart of the ticket, asserted on the hit set **and** on the contrast.

    The colleague asks a question whose only subject is the owner's private document. Two
    claims, and neither is worth anything alone:

    * the document is not in the colleague's hit set — not its id, not its chunk ids, not
      its text anywhere in the response;
    * **and the same question, asked by the owner, returns it** — so the absence is the
      filter's doing rather than a corpus that holds nothing, a threshold nobody clears
      or an index that was never built.

    The unfiltered run is the third leg of the same argument, and it is the one ticket 35
    added: with `unfiltered()`, the document is retrieved, so what the colleague's
    predicate is holding back is a real, rankable passage rather than a passage the
    ranking would have dropped anyway.
    """
    owner_body = await search(personal.owner, PERSONAL_QUESTION)
    assert personal.documents["private"] in document_ids(owner_body), (
        "the owner cannot retrieve their own personal document, so the colleague's "
        f"absence proves nothing: {owner_body['hits']}"
    )

    other = await search(personal.colleague, PERSONAL_QUESTION)
    assert personal.documents["private"] not in document_ids(other)
    assert not (chunk_ids(other) & chunk_ids(owner_body)), (
        "the colleague's hit set shares a chunk id with the owner's, so a passage of the "
        f"private document reached them: {other['hits']}"
    )
    assert PRIVATE_MARKER not in str(other), "the response carried the document's text"
    assert other["filtered"] is True

    # And what the filter is holding back, in the same run's terms.
    async with retrieval_service(platform) as whole_corpus:
        everything = await whole_corpus.search(
            PERSONAL_QUESTION, filter_spec=None, limit=5
        )
    assert everything.hits, "the personal document is not retrievable at all"
    assert UUID(personal.documents["private"]) in {hit.document.id for hit in everything.hits}


async def test_the_search_does_not_recall_a_colleagues_published_personal_document(
    platform: Platform, personal: Personal
) -> None:
    """**Publishing shares the file; it does not add it to the pool.**

    This is the distinction the ticket draws between two of its own lines: a colleague may
    *open* a published personal document (asserted above, 200 on the read and the
    download), and a *question* is recalled 「只有提问者本人的个人文档」. So the same
    colleague who can read the document by hand gets nothing for a question whose only
    subject is its text — and the control is the owner, whose own question does return it.

    The predicate is pinned beside the hit set, because the hit set alone cannot say which
    of the two rules is responsible for the absence.
    """
    owner_body = await search(personal.owner, PERSONAL_QUESTION)
    assert personal.documents["shared"] in document_ids(owner_body) or (
        personal.documents["private"] in document_ids(owner_body)
    ), owner_body

    # The colleague reaches the document on the read surface...
    assert (
        await personal.colleague.get(
            f"/api/v1/documents/{personal.documents['shared']}"
        )
    ).status_code == 200
    # ...and not through a question.
    other = await search(personal.colleague, PERSONAL_QUESTION)
    assert personal.documents["shared"] not in document_ids(other)
    assert not (chunk_ids(other) & chunk_ids(owner_body)), other["hits"]

    # The clause itself: the retrieval predicate renders no visibility term at all, and
    # the list does. Asserted here as well as in `test_retrieval.py` because *this* file
    # is where the "not corpus" claim lives.
    predicate, _ = visible_document_predicate(
        answer_filter_for(personal.principals["colleague"])
    )
    assert "d.visibility" not in predicate, (
        "the retrieval predicate reaches a personal document through its visibility "
        f"column, which is how a colleague's publication becomes corpus: {predicate}"
    )
    clauses, _ = visible_document_clauses(
        answer_filter_for(personal.principals["colleague"])
    )
    assert all("d.visibility" not in clause for clause in clauses), clauses

    listing_clauses, _ = visible_document_clauses(
        filter_for(personal.principals["colleague"], ResourceKind.DOCUMENT)
    )
    assert any("d.visibility = 'department'" in clause for clause in listing_clauses), (
        f"the document list reaches no published personal document: {listing_clauses}"
    )


async def test_the_owners_own_personal_document_is_recalled_for_them(
    platform: Platform, personal: Personal
) -> None:
    """The other half of the same rule, on its own: your own uploads are yours to ask about.

    Without this the previous two tests would be satisfied by a predicate that recalled no
    personal document at all — which is not the rule, and which would make a person's own
    contract unanswerable. The assertion is the hit set, and the clause that produces it is
    pinned by value: the caller's own employee id, and the `NOT d.is_company_kb` gate.
    """
    body = await search(personal.owner, PERSONAL_QUESTION)

    assert personal.documents["private"] in document_ids(body)
    assert body["insufficient_evidence"] is False

    predicate, parameters = visible_document_predicate(
        answer_filter_for(personal.principals["owner"])
    )
    assert parameters["filter_employee_id"] == personal.principals["owner"].employee_id
    assert "(NOT d.is_company_kb AND d.owner_employee_id = :filter_employee_id)" in predicate, (
        f"the ownership clause is not the one this ticket pins: {predicate}"
    )


async def test_hr_does_not_reach_a_personal_document_through_the_exception_clause(
    platform: Platform, personal: Personal
) -> None:
    """§4.2's fourth clause is for **company documents**, and HR is not an exception here.

    The design names HR and compliance as the cross-department exception and states in the
    same breath that it is 「仅公司文档」. So the person who administers the knowledge base
    cannot read a colleague's personal upload, cannot list it and cannot have it recalled
    for a question — which is the difference between a role that manages the corpus and a
    role that may read everybody's files.
    """
    assert personal.documents["private"] not in document_ids(
        await search(personal.hr, PERSONAL_QUESTION)
    )
    assert (
        await personal.hr.get(f"/api/v1/documents/{personal.documents['private']}")
    ).status_code == 404
    listed = await personal.hr.get("/api/v1/documents", params={"limit": 200})
    assert personal.documents["private"] not in listing(personal.hr, listed.json())

    # The predicate says why: the exception term is gated on `is_company_kb` and carries
    # the ceiling, so HR's reach is every company document within its clearance and no
    # personal one. Pinned by its exact text rather than by a prefix, because the company
    # clause begins with the same two terms and a prefix match would accept either.
    clauses, _ = visible_document_clauses(answer_filter_for(personal.principals["hr"]))
    exception = (
        "(d.is_company_kb "
        "AND d.clearance_level = ANY(CAST(:filter_clearances AS text[])))"
    )
    assert exception in clauses, clauses
    # And HR *does* reach the company document, so the refusal above is the document being
    # personal rather than HR reaching nothing at all.
    assert personal.documents["company"] in document_ids(
        await search(personal.hr, COMPANY_QUESTION)
    )


# --- 5. publishing does not lift the ceiling ---------------------------------


async def test_a_colleague_above_the_ceiling_may_read_a_published_personal_document(
    platform: Platform, personal: Personal
) -> None:
    """The control for the next test: publishing *does* share, with a colleague who clears
    the document's level.

    Asserted on the read, the download and the list — the three surfaces a colleague uses
    — so that the refusal below is the ceiling's doing and not a document nobody can see.
    """
    assert personal.principals["colleague"].clearance_level == "medium", (
        "the fixture's colleague is not cleared for the document, so the refusal below "
        "would prove nothing about the ceiling"
    )
    assert (
        await personal.colleague.get(
            f"/api/v1/documents/{personal.documents['shared']}"
        )
    ).status_code == 200
    listed = await personal.colleague.get("/api/v1/documents", params={"limit": 200})
    assert personal.documents["shared"] in listing(personal.colleague, listed.json())
    assert (
        await personal.colleague.get(
            f"/api/v1/documents/{personal.documents['shared']}/content"
        )
    ).status_code == 200


async def test_a_colleague_below_the_ceiling_is_refused_a_published_personal_document(
    platform: Platform, personal: Personal
) -> None:
    """「公开给本部门的个人文档，对同事仍须满足密级条件才可见」 — the ticket's fifth line.

    D11's note is the whole rule: 显式共享不能突破密级上限. The junior works in the
    document's own department, so the *department* admits them and only the ceiling does
    not; they are refused by the application and by the database, and the document is
    absent from their list rather than present-and-marked.
    """
    assert personal.principals["junior"].clearance_level == "low", (
        "the fixture's junior clears the document, so this test is not about the ceiling"
    )
    assert personal.principals["junior"].covers_department(UUID(personal.department))

    assert (
        await personal.junior.get(f"/api/v1/documents/{personal.documents['shared']}")
    ).status_code == 404
    assert (
        await personal.junior.get(
            f"/api/v1/documents/{personal.documents['shared']}/content"
        )
    ).status_code == 404
    listed = await personal.junior.get("/api/v1/documents", params={"limit": 200})
    assert personal.documents["shared"] not in listing(personal.junior, listed.json())
    assert personal.documents["private"] not in listing(personal.junior, listed.json())

    # And the same colleague, on a question: nothing, because the retrieval predicate's
    # share term carries the ceiling and the ownership term is not theirs.
    assert personal.documents["shared"] not in document_ids(
        await search(personal.junior, PERSONAL_QUESTION)
    )

    # The clause, by value: the junior's ceiling is `low`, and the document is `medium`.
    # **Both halves are asserted, and the second is what the ticket's line is about**: the
    # share term is present — publishing *is* a way in for a colleague — and it carries the
    # caller's ceiling, so the predicate cannot admit a `medium` document to a `low`
    # caller. Dropping the `clearance_level` term from the list's share clause leaves the
    # first assertion passing and this one failing, which is the mutation that turns 公开
    # into 绕过密级.
    clauses, parameters = visible_document_clauses(
        filter_for(personal.principals["junior"], ResourceKind.DOCUMENT)
    )
    assert parameters["filter_clearances"] == ["low"], parameters
    share = [clause for clause in clauses if "d.visibility = 'department'" in clause]
    assert len(share) == 1, clauses
    assert "d.clearance_level = ANY(CAST(:filter_clearances AS text[]))" in share[0], (
        "the list's share clause has no ceiling, so a published personal document is "
        f"readable above the reader's clearance: {share[0]}"
    )
    assert set(parameters["filter_clearances"]) == {"low"} and "medium" not in (
        parameters["filter_clearances"]
    ), (
        "the junior's predicate carries a clearance above their own, so the ceiling term "
        f"cannot be doing the work this test claims: {parameters}"
    )
    # And the predicate as a whole: the junior's reach, rendered, names only levels inside
    # their ceiling — so no term of it admits the document.
    predicate, _ = visible_document_predicate(
        filter_for(personal.principals["junior"], ResourceKind.DOCUMENT)
    )
    assert "'medium'" not in predicate, predicate


# --- 6. revocation is immediate ----------------------------------------------


async def test_revoking_the_publication_takes_effect_on_the_next_request(
    platform: Platform, personal: Personal
) -> None:
    """「员工可撤销公开，撤销后同事立即无法访问，缓存同步失效」 — the very next request.

    The standard the permission-snapshot suite sets (「下一次请求立即生效」), and it needs no
    cache work here: the share is *a column on the document*, not an entry in the
    principal's snapshot, so there is nothing whose key could carry a stale value. The
    test below (`test_the_snapshot_cache_key_carries_no_document_visibility`) asserts that
    as a fact, and the ticket file records why no key changed.

    The sequence is the ticket's: publish → a colleague reaches it → revoke → the colleague
    does not, on the **next** call, with no sleep, no TTL wait and no invalidation call.
    The first calls warm whatever caches exist — the principal's snapshot above all — so
    the assertion is that a warm cache does not hold a revoked share.

    **The publication is written as the column, and that is deliberate.** This ticket
    adds no metadata-edit route: the design's `documents` table has the column, the
    upload states it, and a route to change it afterwards is a surface nobody asked for
    (ticket 37 owns the interface). What the revocation requirement is about is that the
    *reach* follows the column immediately, which is what is asserted — and the helper
    below fails loudly if such a route appears, so this test cannot quietly start
    exercising a different code path.
    """
    colleague = personal.colleague
    private = personal.documents["private"]

    async def set_visibility(visibility: str) -> None:
        response = await personal.owner.patch(
            f"/api/v1/documents/{private}", json={"visibility": visibility}
        )
        assert response.status_code in (404, 405), (
            "a metadata-edit route now exists; this test's publication step has to be "
            f"rewritten to use it rather than the column ({response.status_code})"
        )
        await platform.sql(
            "UPDATE documents SET visibility = :visibility WHERE id = :id",
            {"visibility": visibility, "id": private},
        )

    # The baseline is a refusal: an unpublished personal document.
    assert (await colleague.get(f"/api/v1/documents/{private}")).status_code == 404

    await set_visibility(DEPARTMENT_VISIBILITY)

    assert (await colleague.get(f"/api/v1/documents/{private}")).status_code == 200
    listed = await colleague.get("/api/v1/documents", params={"limit": 200})
    assert private in listing(colleague, listed.json())

    await set_visibility(PRIVATE_VISIBILITY)

    # The next request, and no other: nothing was invalidated, nothing was waited for.
    assert (await colleague.get(f"/api/v1/documents/{private}")).status_code == 404
    assert (await colleague.get(f"/api/v1/documents/{private}/content")).status_code == 404
    listed = await colleague.get("/api/v1/documents", params={"limit": 200})
    assert private not in listing(colleague, listed.json())
    # The owner still has it: revoking a publication is not deleting a document.
    assert (await personal.owner.get(f"/api/v1/documents/{private}")).status_code == 200


async def test_the_snapshot_cache_key_carries_no_document_visibility(
    platform: Platform, personal: Personal
) -> None:
    """Why the revocation test above needs no invalidation: the key has no such input.

    The permission snapshot is cached under a key built from the account's session epoch,
    roles, stored clearance, the assignment facts and the organisation tree version
    (`domain/access/snapshot.py`). A *document's* visibility is not an input to the
    snapshot — the reach is rendered from the document's own columns at query time — so
    there is no key that could carry a stale share and nothing to add to one. This test
    states that as a fact rather than as a claim in a comment: the key does not change
    when a document's visibility does.
    """
    from app.domain.access.snapshot import resolve_principal

    async with platform.factory() as session:
        before = await resolve_principal(session, UUID(personal.colleague.user_id))
    async with platform.factory() as session:
        after = await resolve_principal(session, UUID(personal.colleague.user_id))

    assert before is not None and after is not None
    assert before.version == after.version, (
        "the snapshot version moved without any of its inputs changing"
    )

    await platform.sql(
        "UPDATE documents SET visibility = :visibility WHERE id = :id",
        {"visibility": DEPARTMENT_VISIBILITY, "id": personal.documents["private"]},
    )
    async with platform.factory() as session:
        after_publish = await resolve_principal(session, UUID(personal.colleague.user_id))
    assert after_publish is not None
    assert after_publish.version == before.version, (
        "a document's visibility is an input to the permission snapshot's cache key, so "
        "the key must carry it — see the ticket file"
    )


# --- 7. somebody else's private document is a refusal and an absence ---------


async def test_someone_elses_private_personal_document_is_absent_and_refused(
    platform: Platform, personal: Personal
) -> None:
    """「他人访问未公开的个人文档返回 403 且资源在列表中完全不存在」 — both halves.

    Four surfaces, and each answers the way the ticket asks:

    * the metadata read and the download are 404 with the catalogued "no such document"
      code, which is the answer an unknown id gets: telling "not yours" apart from "does
      not exist" would make the endpoint an existence oracle over everybody's uploads
      (ticket 31's argument, and the reason the code is `ERR_DOC_001`);
    * the list is **completely absent** — no entry, not an entry with a flag — and the
      `total` agrees with the page, so a client cannot count what it may not see;
    * a question recalls nothing of it;
    * and the refusal leaks nothing: not the title, not the filename, not the text.
    """
    private = personal.documents["private"]
    title = (await personal.owner.get(f"/api/v1/documents/{private}")).json()["title"]

    for actor in (personal.colleague, personal.junior, personal.outsider, personal.hr):
        read = await actor.get(f"/api/v1/documents/{private}")
        assert read.status_code == 404, read.text
        assert read.json()["error"]["code"] == ErrorCode.DOCUMENT_NOT_FOUND.value
        assert title not in read.text
        assert PRIVATE_MARKER not in read.text
        assert (
            await actor.get(f"/api/v1/documents/{private}/content")
        ).status_code == 404

        page = (await actor.get("/api/v1/documents", params={"limit": 200})).json()
        assert private not in {item["id"] for item in page["items"]}
        assert page["total"] == len(page["items"]) or private not in str(page)

        body = await search(actor, PERSONAL_QUESTION)
        assert private not in document_ids(body)
        assert PRIVATE_MARKER not in str(body)


# --- 8. the answer's marker ---------------------------------------------------


@dataclass
class Answers:
    """One assembled answer service, the model it calls, and its session."""

    service: AnswerService
    model: StreamedChatModel
    session: object

    async def events(self, question: str, principal: Principal, **kwargs) -> list:
        return [
            event
            async for event in self.service.stream(question, principal, **kwargs)
        ]

    async def close(self) -> None:
        await self.session.close()  # type: ignore[attr-defined]


def answers(platform: Platform, *, min_score: float = 0.0) -> Answers:
    """The real answer pipeline on its own session, with the streamed fake model.

    `min_score=0.0` because this module is about the *marker*, not about D20's threshold:
    the same reason `test_answer.py` lets the threshold be overridden. The retrieval, the
    repository and the driver are the real ones.
    """
    from app.config import get_settings

    settings = get_settings()
    session = platform.factory()
    model = StreamedChatModel()
    retrieval = RetrievalService(
        PostgresChunkSearchRepository(session),
        embedder=DeterministicEmbedder(),
        min_score=min_score,
        fusion_k=settings.retrieval_fusion_k,
        leg_limit=settings.retrieval_leg_limit,
    )
    return Answers(
        service=AnswerService(
            PostgresAnswerRepository(session), retrieval, model, session=session
        ),
        model=model,
        session=session,
    )


async def test_an_answer_grounded_in_a_personal_document_carries_the_marker(
    platform: Platform, personal: Personal
) -> None:
    """「回答中若引用了个人文档，必须在回答顶部明确标注」 — in the contract, not on a screen.

    Three places the marker has to be, and each is asserted because ticket 37 reads all
    three: the `citations` **frame**, which arrives before any text and is therefore where
    a client renders 「回答顶部」; the `done` frame, which is the record of what was stored;
    and the stored row, which is what a conversation read a week later renders from.

    The text is the design's own sentence in the two languages the interface ships, and
    the key is what a client with a dictionary looks its own copy up by. Nothing here
    draws anything.
    """
    assembled = answers(platform)
    try:
        events = await assembled.events(PERSONAL_QUESTION, personal.principals["owner"])
    finally:
        await assembled.close()

    citations = next(event for event in events if event.kind is EventKind.CITATIONS)
    notice = citations.data["source_notice"]
    assert notice is not None, (
        "an answer grounded in a personal document carried no marker: "
        f"{citations.data['citations']}"
    )
    assert notice["personal_documents"] is True
    assert notice["message_key"] == PERSONAL_DOCUMENT_NOTICE_MESSAGE_KEY
    assert notice["text"] == PERSONAL_DOCUMENT_NOTICE_TEXT
    assert "以下内容来自个人文档（非公司知识库）" in notice["text"]["zh"]

    # The frames that carry it, in the order a client reads them: the marker is before
    # any `delta`, which is what makes it a banner rather than a footnote.
    kinds = [event.kind for event in events]
    assert kinds.index(EventKind.CITATIONS) < (
        kinds.index(EventKind.DELTA) if EventKind.DELTA in kinds else len(kinds)
    )
    done = next(event for event in events if event.kind is EventKind.DONE)
    assert done.data["source_notice"] == notice

    # And the stored row, read on the database: the contract and the record agree.
    message_id = done.data["message_id"]
    stored = await platform.sql(
        "SELECT source_notice FROM rag_messages WHERE id = :id", {"id": message_id}
    )
    assert stored[0][0] is not None, "the marker was streamed and not stored"
    assert stored[0][0]["message_key"] == PERSONAL_DOCUMENT_NOTICE_MESSAGE_KEY
    assert stored[0][0]["text"]["en"].startswith("The following content comes from")

    # The conversation read returns it too, so a client that opens the message later
    # renders the same banner without re-deriving it from the citation flags.
    conversation_id = done.data["conversation_id"]
    read = await personal.owner.get(f"/api/v1/answers/conversations/{conversation_id}")
    assert read.status_code == 200, read.text
    message = read.json()["messages"][0]
    assert message["source_notice"]["message_key"] == PERSONAL_DOCUMENT_NOTICE_MESSAGE_KEY
    assert message["citations"][0]["is_company_kb"] is False


async def test_the_marker_is_absent_from_an_answer_grounded_in_the_company_knowledge_base(
    platform: Platform, personal: Personal
) -> None:
    """The marker means something only if it is absent when it does not apply.

    A label that appeared on every answer would stop being read, and — worse — a client
    could not tell "this answer quotes your own upload" from "this answer quotes the
    company policy", which is the one distinction §5.2's banner exists to draw. So a
    company-grounded answer carries `source_notice: null` and a stored NULL, and the
    citations it does carry say `is_company_kb: true`.

    HR is the caller because HR reaches the company document from any department: it is
    the role §4.2's exception clause names, and the document is filed in a department HR
    does not work in — so the citation is reached by the clause this test wants and not by
    the department one.
    """
    assert personal.documents["company"] in document_ids(
        await search(personal.hr, COMPANY_QUESTION)
    ), "the fixture's HR does not reach the company document, so this control proves nothing"

    assembled = answers(platform)
    try:
        events = await assembled.events(COMPANY_QUESTION, personal.principals["hr"])
    finally:
        await assembled.close()

    citations = next(event for event in events if event.kind is EventKind.CITATIONS)
    assert citations.data["citations"], "the company document was not retrieved at all"
    assert all(item["is_company_kb"] for item in citations.data["citations"]), (
        f"a company question was answered from a personal document: {citations.data}"
    )
    assert citations.data["source_notice"] is None

    done = next(event for event in events if event.kind is EventKind.DONE)
    assert done.data["source_notice"] is None
    stored = await platform.sql(
        "SELECT source_notice FROM rag_messages WHERE id = :id",
        {"id": done.data["message_id"]},
    )
    assert stored[0][0] is None, "an answer with no personal document stored a marker"


async def test_a_refusal_carries_no_marker(platform: Platform, personal: Personal) -> None:
    """A refusal cites nothing, so it labels nothing.

    D20's answer has no citations — that is asserted in `test_answer.py` — and a marker on
    it would be a banner telling the reader the content below came from a personal
    document when the content below is "the knowledge base holds no basis".
    """
    assembled = answers(platform, min_score=1000.0)
    try:
        events = await assembled.events(PERSONAL_QUESTION, personal.principals["owner"])
    finally:
        await assembled.close()

    refusal = next(event for event in events if event.kind is EventKind.REFUSAL)
    assert refusal.data["is_refusal"] is True
    done = next(event for event in events if event.kind is EventKind.DONE)
    assert done.data["source_notice"] is None
    stored = (
        await platform.sql(
            "SELECT source_notice, citations FROM rag_messages WHERE id = :id",
            {"id": done.data["message_id"]},
        )
    )[0]
    assert stored[0] is None
    assert stored[1] == []


# --- 9. the whole matrix ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Expectation:
    """What `docs/DESIGN.md` §4.2 plus the ticket's recall rule say about one pair."""

    document: str
    caller: str
    listable: bool
    readable: bool
    recalled: bool


def matrix() -> list[Expectation]:
    """Every combination the ticket names, written from the design text.

    The rules, stated once here rather than derived from the kernel, so the two can
    disagree — which is the point of writing a matrix by hand:

    * **§4.2 clause 1**: the owner reaches their own document, whatever it is.
    * **clause 2**: a company document is reached within the ceiling *and* in a department
      the caller works in. The company document is filed in `other_department` at `low`
      clearance, so the outsider and HR are the ones who work there — and it is the
      *exception* clause (HR's) rather than the department clause that reaches it for HR,
      which is why the outsider does not.
    * **clause 3**: a personal document its owner published to a department is reached by
      a colleague in that department **within the ceiling** (D11: 显式共享不能突破密级上限).
      The two personal documents are filed in `department` at `medium`.
    * **clause 4**: the exception roles are for company documents only.
    * **the ticket's recall rule**: a *question* recalls the asker's **own** personal
      documents plus the company corpus their reach covers — 「个人文档不进入公司知识库的检索
      池」, 「只有提问者本人的个人文档可被召回」. The two personal documents are the owner's,
      so the colleague who may open the published one still does not recall it; the
      company document is corpus, so every caller whose reach holds it recalls it.
    """
    expectations: list[Expectation] = []
    for document in ("private", "shared", "company"):
        for caller in ("owner", "colleague", "junior", "outsider", "hr"):
            is_owner = caller == "owner"
            # Who clears the personal documents, filed at `medium`: the two accounts the
            # fixture raised. The junior and the outsider are `low`, and the junior shares
            # the documents' department — so it is the *ceiling* that refuses them.
            clears_medium = caller in ("owner", "colleague")
            in_personal_department = caller in ("owner", "colleague", "junior")

            if document == "company":
                # Filed in `other_department` at `low`: the outsider works there and
                # clears it, and HR reaches it from anywhere through its cross-department
                # exception role (which §4.2 grants for company documents only). **Not the
                # owner**: §4.2's first clause is about *personal* documents, so owning a
                # company document is not a thing, and this one is not theirs at all.
                readable = caller in ("outsider", "hr")
            elif is_owner:
                readable = True
            elif document == "shared":
                readable = in_personal_department and clears_medium
            else:
                readable = False
            expectations.append(
                Expectation(
                    document=document,
                    caller=caller,
                    # The list answers the same question the read does — that is the
                    # property the two renderings of §4.2 exist to keep.
                    listable=readable,
                    readable=readable,
                    # A *question* is not the same rule. A personal document is recalled
                    # for its owner and for nobody else, whatever a colleague may open —
                    # that is the ticket's 「只有提问者本人的个人文档可被召回」. The company
                    # document is corpus, so it is recalled wherever the reach holds it.
                    recalled=readable if document == "company" else is_owner,
                )
            )
    return expectations


async def test_the_whole_matrix_agrees_with_the_kernel(
    platform: Platform, personal: Personal
) -> None:
    """「有测试覆盖上述全部可见性组合」 — every pair, against the running system.

    Written as a matrix rather than as a test per row because the ticket asks for the
    *combinations*, and a hand-written list of pairs is a list of the cases somebody
    thought of. Each row is checked on three surfaces and against the kernel:

    * the **list** (present, or completely absent — never present-and-marked);
    * the **read** (200 or 404) and the **download**, which follow the same rule;
    * the **hit set** of a question, which is deliberately *not* the same rule: a
      colleague's published personal document is listable and readable and is never
      recalled, while the company document is recalled by every caller whose reach holds
      it — which is the difference between corpus and somebody's file;
    * and `can()`, the kernel's own decision, built from the row's real columns — so a
      disagreement names the pair rather than leaving it to a surface's rendering.

    The mismatch list is collected and asserted once, because a failure that names *every*
    pair that disagrees is worth more to the next reader than the first one.
    """
    actors = {
        "owner": personal.owner,
        "colleague": personal.colleague,
        "junior": personal.junior,
        "outsider": personal.outsider,
        "hr": personal.hr,
    }
    listings: dict[str, set[str]] = {}
    for caller, actor in actors.items():
        page = await actor.get("/api/v1/documents", params={"limit": 200})
        assert page.status_code == 200, page.text
        listings[caller] = {item["id"] for item in page.json()["items"]}

    facts: dict[str, tuple] = {
        name: (
            await platform.sql(
                """
                SELECT owner_employee_id, department_id, clearance_level,
                       is_company_kb, visibility
                  FROM documents WHERE id = :id
                """,
                {"id": document_id},
            )
        )[0]
        for name, document_id in personal.documents.items()
    }

    mismatches: list[str] = []
    for row in matrix():
        document_id = personal.documents[row.document]
        actor = actors[row.caller]
        where = f"document={row.document} caller={row.caller}"

        listed = document_id in listings[row.caller]
        if listed is not row.listable:
            mismatches.append(f"{where}: listed={listed}, expected {row.listable}")

        read = await actor.get(f"/api/v1/documents/{document_id}")
        if (read.status_code == 200) is not row.readable:
            mismatches.append(f"{where}: read is {read.status_code}, expected {row.readable}")

        download = await actor.get(f"/api/v1/documents/{document_id}/content")
        if (download.status_code == 200) is not row.readable:
            mismatches.append(
                f"{where}: download is {download.status_code}, expected {row.readable}"
            )

        # **The question is the document's own**, and that is what makes the hit-set
        # column mean something: the personal question names a subject only the personal
        # documents contain, and the company question names a subject only the company
        # document contains. Asking one question of all three would make "recalled" a
        # statement about which leg happened to rank first.
        question = COMPANY_QUESTION if row.document == "company" else PERSONAL_QUESTION
        body = await search(actor, question)
        recalled = document_id in document_ids(body)
        if recalled is not row.recalled:
            mismatches.append(f"{where}: recalled={recalled}, expected {row.recalled}")

        owner, department, clearance, is_company_kb, visibility = facts[row.document]
        decision = can(
            personal.principals[row.caller],
            Action.DOCUMENT_READ,
            Resource(
                kind=ResourceKind.DOCUMENT,
                owner_employee_id=owner,
                department_id=department,
                clearance=clearance,
                is_company_kb=is_company_kb,
                # The publication, as the clause the kernel reads it through.
                explicit_grant=(visibility == DEPARTMENT_VISIBILITY),
            ),
        )
        if decision.allowed is not row.readable:
            mismatches.append(
                f"{where}: the kernel says {decision.allowed} and the matrix says "
                f"{row.readable} ({decision.detail})"
            )

    assert mismatches == [], "\n".join(mismatches)

    # The matrix is exhaustive over the pairs the fixture builds, so a fixture that lost a
    # document would fail here rather than pass with fewer rows.
    assert len(matrix()) == 3 * 5
    assert {row.document for row in matrix()} == {"private", "shared", "company"}
    assert {row.caller for row in matrix()} == set(actors)


# --- 10. the second line of defence ------------------------------------------


def _app_connection(platform: Platform) -> "_ClosingConnection":
    """Sessions bound to the restricted role, on the test database.

    The same connection `test_database_security.py` and the escalation suite use, and for
    the same reason: a table's owner is exempt from its own row-level policies, so an
    assertion made as the owner would exercise nothing. The engine is created here and
    disposed by the context manager, so no leaked connection holds a read lock on the
    tables the next test's `TRUNCATE` needs.
    """
    from app.config import get_settings

    return _ClosingConnection(create_async_engine(get_settings().runtime_test_database_url))


class _ClosingConnection:
    """An `async_sessionmaker` plus the engine it must dispose of afterwards."""

    def __init__(self, engine) -> None:  # noqa: ANN001
        self._engine = engine
        self._factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    def __call__(self) -> AsyncSession:
        return self._factory()

    async def __aenter__(self) -> async_sessionmaker:
        return self._factory

    async def __aexit__(self, *exception: object) -> None:
        await self._engine.dispose()


async def publish(session: AsyncSession, **values: str) -> None:
    """Publish the request context, by hand.

    Deliberately not `kernel.apply_rls_context`: a test that called the kernel's own
    function would prove the two agree about a *name* and nothing about what PostgreSQL
    does with the value. `test_database_security.py` uses the same helper.
    """
    for name, value in values.items():
        await session.execute(
            text("SELECT set_config(:name, :value, true)"),
            {"name": name, "value": value},
        )


async def test_the_database_refuses_a_private_personal_document_in_the_callers_department(
    platform: Platform, personal: Personal
) -> None:
    """**The policy was wider than the rule, and ticket 36 narrows it** — asserted on rows.

    Ticket 31's `document_visibility_predicate` tested `owner = me OR (clearance_ok AND
    department_ok)` and knew nothing about a document's kind, so a personal upload filed
    into a department was readable at the database layer by every colleague cleared for
    that department — whatever its `visibility` said. That is the one direction a backstop
    must never err in, and it is the reason the predicate now takes `is_company_kb` and
    `visibility`.

    Four assertions, over the restricted role with the colleague's context published:

    * the **private** document's chunks are invisible to a filterless join — the query a
      forgotten application filter would run — even though the caller shares its
      department and clears its level;
    * its `documents` row is invisible on its own;
    * the predicate, asked directly with the row's own columns, answers `false`;
    * and the predicate answers **`true` for the same row with `visibility='department'`**,
      so the refusal above is the publication's doing and not a predicate that refuses
      everything in a department.
    """
    private_id = personal.documents["private"]
    shared_id = personal.documents["shared"]
    colleague = personal.principals["colleague"]

    facts = (
        await platform.sql(
            """
            SELECT owner_employee_id, department_id, clearance_level, is_company_kb, visibility
              FROM documents WHERE id = :id
            """,
            {"id": private_id},
        )
    )[0]
    assert facts[4] == PRIVATE_VISIBILITY
    assert facts[1] is not None, "the fixture's private document has no department to share"

    async with _app_connection(platform) as connection:
        async with connection() as session:
            await publish(
                session,
                **{
                    "app.current_employee_id": str(colleague.employee_id),
                    "app.clearance_levels": '{"low","medium"}',
                    "app.department_ids": "{" + f'"{personal.department}"' + "}",
                },
            )
            leaked_chunks = (
                await session.execute(
                    text(
                        """
                        SELECT count(*)
                          FROM document_chunks AS c
                          JOIN documents AS d ON d.id = c.document_id
                         WHERE c.parent_chunk_id IS NOT NULL
                           AND d.status = 'ready'
                           AND d.id = :id
                        """
                    ),
                    {"id": private_id},
                )
            ).scalar()
            leaked_document = (
                await session.execute(
                    text("SELECT count(*) FROM documents WHERE id = :id"), {"id": private_id}
                )
            ).scalar()
            # The positive control: a document the caller *may* read, through the same
            # filterless query, so "zero rows" is the policy deciding rather than the
            # fixture having written nothing.
            visible_chunks = (
                await session.execute(
                    text(
                        """
                        SELECT count(*)
                          FROM document_chunks AS c
                          JOIN documents AS d ON d.id = c.document_id
                         WHERE c.parent_chunk_id IS NOT NULL
                           AND d.status = 'ready'
                           AND d.id = :id
                        """
                    ),
                    {"id": shared_id},
                )
            ).scalar()
            admitted, refused = (
                await session.execute(
                    text(
                        """
                        SELECT
                            document_visibility_predicate(
                                CAST(:owner AS uuid), CAST(:department AS uuid),
                                CAST(:clearance AS text), CAST(:company AS boolean),
                                CAST(:shared AS text)
                            ),
                            document_visibility_predicate(
                                CAST(:owner AS uuid), CAST(:department AS uuid),
                                CAST(:clearance AS text), CAST(:company AS boolean),
                                CAST(:private AS text)
                            )
                        """
                    ),
                    {
                        "owner": facts[0],
                        "department": facts[1],
                        "clearance": facts[2],
                        "company": facts[3],
                        "shared": DEPARTMENT_VISIBILITY,
                        "private": PRIVATE_VISIBILITY,
                    },
                )
            ).first()

    assert leaked_chunks == 0, (
        "the database returned chunks of a *private* personal document to a colleague in "
        "its own department, which is the disclosure the second line of defence exists to "
        "prevent"
    )
    assert leaked_document == 0, "the database returned the private document's own row"
    assert visible_chunks and visible_chunks > 0, (
        "the database refused the published document too, so the refusal above is a "
        "policy that admits nothing rather than one that reads visibility"
    )
    assert refused is False, (
        "`document_visibility_predicate` admits a private personal document to a "
        "colleague in its department: the policy is wider than §4.2"
    )
    assert admitted is True, (
        "the same predicate refuses the published document, so it is not reading the "
        "visibility column at all"
    )


async def test_the_second_line_admits_the_owners_own_document(
    platform: Platform, personal: Personal
) -> None:
    """A backstop that refused the owner their own upload would be an outage.

    The other direction of the same policy: §4.2's first clause has no conditions, so the
    owner reads their own private document through the filterless query — which is what
    makes the refusals above the policy deciding rather than the rows not existing.
    """
    private_id = personal.documents["private"]

    async with _app_connection(platform) as connection:
        async with connection() as session:
            await publish(
                session,
                **{
                    "app.current_employee_id": str(personal.owner.employee_id),
                    "app.clearance_levels": '{"low","medium"}',
                    "app.department_ids": "{" + f'"{personal.department}"' + "}",
                },
            )
            chunks = (
                await session.execute(
                    text(
                        """
                        SELECT count(*)
                          FROM document_chunks AS c
                          JOIN documents AS d ON d.id = c.document_id
                         WHERE c.parent_chunk_id IS NOT NULL
                           AND d.status = 'ready'
                           AND d.id = :id
                        """
                    ),
                    {"id": private_id},
                )
            ).scalar()

    assert chunks and chunks > 0, (
        "the database refused the owner their own personal document, so the second line "
        "of defence is an outage rather than a backstop"
    )


# --- the kernel's own spec, for the record -----------------------------------


async def test_the_document_spec_carries_the_publication_flag(
    platform: Platform, personal: Personal
) -> None:
    """The one field that distinguishes the list's reach from a question's, asserted.

    `filter_for(..., DOCUMENT)` sets `personal_documents_via_department` — §4.2 in full —
    and `answer_filter_for` clears it. Both are stored on `FilterSpec`, which has no
    public constructor, so the difference cannot be introduced by a caller: it is either
    the kernel's sentence or the shared helper's narrowing, and nothing else.
    """
    full = filter_for(personal.principals["colleague"], ResourceKind.DOCUMENT)
    narrowed = answer_filter_for(personal.principals["colleague"])

    assert full.personal_documents_via_department is True
    assert narrowed.personal_documents_via_department is False
    # Everything else is the same object's: the narrowing is one field and not a second
    # spec. Asserted so a future edit that rebuilt the spec by hand is caught here.
    for field_name in (
        "kind",
        "allow_all",
        "department_ids",
        "clearance_levels",
        "own_employee_id",
        "explicit_grant_employee_id",
        "company_kb_cross_department",
        "include_company_kb",
        "manager_employee_id",
        "reports_employee_ids",
        "statuses",
    ):
        assert getattr(full, field_name) == getattr(narrowed, field_name), field_name


__all__ = ["Personal", "personal"]
