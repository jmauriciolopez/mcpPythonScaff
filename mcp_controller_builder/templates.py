# Templates que replican los contratos del proyecto (ver README y shared/).
#
# Convenciones implementadas:
# - Entidad: dataclass kw_only heredando de shared.domain.entities.AuditableEntity
# - Puerto: Protocol que extiende shared.domain.repository_port.RepositoryPort
# - Servicio: hereda de shared.application.base_service.BaseService (repo + logger)
# - ORM: estilo Mapped/mapped_column sobre shared.infrastructure.database.Base
# - Repo SQL: hereda de shared.infrastructure.sql_repository.SQLBaseRepository
# - Controller: create_generic_router de shared.infrastructure.generic_controller
# - Schemas: Base/Create/Update/Response con ConfigDict(from_attributes=True)
# - IDs UUID, borrado lógico vía 'deleted', errores de dominio (sin HTTPException)
#
# Relaciones soportadas (campo `relation` de EntityField):
# - many_to_many : list[UUID] en el dominio, tabla intermedia, se escribe desde esta entidad.
# - many_to_one  : lado "N" de un 1:N. Columna FK en esta tabla (UUID), indexada.
# - one_to_one   : columna FK con índice único parcial (sobre activos). Lado dueño.
# - one_to_many  : lado "1" de un 1:N. Virtual y de solo lectura (ids de los hijos activos);
#                  `mapped_by` es la FK many_to_one del hijo (default: <esta_entidad>_id).
# - one_to_one + mapped_by : lado inverso del 1:1. Virtual y de solo lectura (id o None).
import re
from dataclasses import dataclass, field

MANY_TO_MANY = "many_to_many"
ONE_TO_MANY = "one_to_many"
MANY_TO_ONE = "many_to_one"
ONE_TO_ONE = "one_to_one"
RELATION_KINDS = (MANY_TO_MANY, ONE_TO_MANY, MANY_TO_ONE, ONE_TO_ONE)


def _rel_snake(related: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", related).lower()


@dataclass
class EntityField:
    name: str
    type: str = "str"                  # str | int | float | bool | datetime | uuid | relation
    required: bool = True
    min_length: int | None = None      # solo str
    max_length: int | None = None      # solo str
    unique: bool = False               # índice único parcial sobre activos
    related_entity: str | None = None  # p/relaciones: entidad destino
    relation: str = ""                 # many_to_many | one_to_many | many_to_one | one_to_one
    mapped_by: str | None = None       # p/lado inverso: FK del hijo que apunta a esta entidad

    def __post_init__(self) -> None:
        rel = self.relation
        if self.type == "relation" and not rel:
            rel = MANY_TO_MANY
        if rel and rel not in RELATION_KINDS:
            raise ValueError(f"Relación '{rel}' inválida en '{self.name}'. "
                             f"Usá una de: {', '.join(RELATION_KINDS)}")
        if rel == MANY_TO_ONE and self.mapped_by:
            raise ValueError(f"'{self.name}': many_to_one es el lado FK y no admite mapped_by.")
        if rel in (MANY_TO_ONE, ONE_TO_ONE) and not self.mapped_by:
            self.type = "uuid"             # columna FK en esta tabla
            if rel == ONE_TO_ONE:
                self.unique = True
        elif rel:
            self.type = "relation"         # N:M o lado inverso (virtual)
            self.required = False
        self.relation = rel

    @property
    def is_fk(self) -> bool:
        return self.type == "uuid" and self.relation in (MANY_TO_ONE, ONE_TO_ONE)

    @property
    def is_m2m(self) -> bool:
        return self.type == "relation" and self.relation == MANY_TO_MANY

    @property
    def is_inverse(self) -> bool:
        return self.type == "relation" and self.relation in (ONE_TO_MANY, ONE_TO_ONE)

    @property
    def rel_attr(self) -> str:
        """Nombre del atributo `relationship` en el ORM (siempre distinto del campo de dominio)."""
        if self.relation == MANY_TO_MANY:
            return f"{_rel_snake(self.related_entity or '')}s"
        if self.name.endswith("_ids"):
            return self.name[:-4] + "s"
        if self.name.endswith("_id"):
            return self.name[:-3]
        return self.name + "_rel"


@dataclass
class Entity:
    name: str                     # PascalCase, ej. "Product"
    resource_name: str = ""       # español, ej. "Producto" (lo da el usuario)
    plural: str = ""              # ej. "products" (default: snake + "s")
    fields: list[EntityField] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.resource_name:
            self.resource_name = self.name

    @property
    def snake(self) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", self.name).lower()

    @property
    def table(self) -> str:
        return self.plural or f"{self.snake}s"

    @property
    def relations(self) -> list[EntityField]:
        """Campos virtuales (sin columna propia): N:M e inversos."""
        return [f for f in self.fields if f.type == "relation"]

    @property
    def scalars(self) -> list[EntityField]:
        """Campos con columna propia (incluye las FK many_to_one / one_to_one)."""
        return [f for f in self.fields if f.type != "relation"]

    @property
    def fks(self) -> list[EntityField]:
        return [f for f in self.fields if f.is_fk]

    @property
    def m2m(self) -> list[EntityField]:
        return [f for f in self.fields if f.is_m2m]

    @property
    def inverses(self) -> list[EntityField]:
        return [f for f in self.fields if f.is_inverse]

    @property
    def owning_targets(self) -> list[str]:
        """Entidades destino que esta entidad debe validar (FK y N:M), sin repetir."""
        out: list[str] = []
        for f in self.fks + self.m2m:
            if f.related_entity and f.related_entity not in out:
                out.append(f.related_entity)
        return out

    @property
    def unique_fields(self) -> list[EntityField]:
        return [f for f in self.scalars if f.unique and f.type in ("str", "uuid")]


_PY = {"str": "str", "int": "int", "float": "float", "bool": "bool",
       "datetime": "datetime", "uuid": "UUID"}
_ORM = {"str": "String", "int": "Integer", "float": "Float", "bool": "Boolean",
        "datetime": "DateTime(timezone=True)", "uuid": "Uuid(as_uuid=True)"}


def _table_of(related: str, known: "dict[str, Entity] | None") -> str:
    ent = (known or {}).get(related)
    return ent.table if ent else f"{_rel_snake(related)}s"


def _uq_name(e: Entity, f: EntityField) -> str:
    return f"uq_{e.table}_active_{f.name}" + ("_ci" if f.type == "str" else "")


def _lit(v: object) -> str:
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, int):
        return str(v)
    return f'"{v}"'


def _meta_literal(meta: dict) -> str:
    return "{" + ", ".join(f'"{k}": {_lit(v)}' for k, v in meta.items()) + "}"


def _field_meta(f: EntityField) -> dict:
    """Metadata que el discovery vuelve a leer de la entidad (round-trip)."""
    meta: dict = {}
    if f.is_fk or f.is_inverse:
        meta["relation"] = f.relation
        if f.related_entity:
            meta["entity"] = f.related_entity
        if f.mapped_by:
            meta["mapped_by"] = f.mapped_by
    else:
        if f.unique:
            meta["unique"] = True
        if f.min_length:
            meta["min_length"] = f.min_length
        if f.max_length:
            meta["max_length"] = f.max_length
    return meta


# ---------------------------------------------------------------- 1. dominio
def render_domain_entity(e: Entity) -> str:
    types = {f.type for f in e.scalars}
    body: list[str] = []
    if e.resource_name != e.name:
        body.append(f'    __resource_name__ = "{e.resource_name}"')
    if e.plural:
        body.append(f'    __plural__ = "{e.plural}"')
    needs_field = False
    for f in e.scalars:
        t = _PY.get(f.type, "str")
        meta = _field_meta(f)
        if meta:
            needs_field = True
            lit = _meta_literal(meta)
            if f.required:
                body.append(f"    {f.name}: {t} = dc_field(metadata={lit})")
            else:
                body.append(f"    {f.name}: {t} | None = dc_field(default=None, metadata={lit})")
        else:
            body.append(f"    {f.name}: {t}" if f.required else f"    {f.name}: {t} | None = None")
    for f in e.relations:
        needs_field = True
        md = f", metadata={_meta_literal(_field_meta(f))}" if f.is_inverse else ""
        if f.relation == ONE_TO_ONE:
            body.append(f"    {f.name}: UUID | None = dc_field(default=None{md})")
        else:
            body.append(f"    {f.name}: list[UUID] = dc_field(default_factory=list{md})")
    imports = ["from dataclasses import dataclass, field as dc_field" if needs_field
               else "from dataclasses import dataclass"]
    if "datetime" in types:
        imports.append("from datetime import datetime")
    if "uuid" in types or e.relations:
        imports.append("from uuid import UUID")
    lines = ["\n".join(imports), "", "from shared.domain.entities import AuditableEntity",
             "", "", "@dataclass(kw_only=True)", f"class {e.name}(AuditableEntity):", *body]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 2. puerto
def render_port(e: Entity) -> str:
    n, s = e.name, e.snake
    targets = e.owning_targets
    imports = ["from typing import Protocol"]
    if targets:
        imports.append("from uuid import UUID")
    imports += [f"from domain.entities.{s} import {n}",
                "from shared.domain.repository_port import RepositoryPort"]
    lines = ["\n".join(imports), "", "",
             f"class {n}RepositoryPort(RepositoryPort[{n}], Protocol):"]
    if not targets:
        lines.append("    pass")
    for R in targets:
        r = _rel_snake(R)
        lines.append(f"    def get_active_{r}_ids(self, {r}_ids: list[UUID]) -> list[UUID]: ...")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 3. servicio
def render_service(e: Entity) -> str:
    n, s = e.name, e.snake
    targets = e.owning_targets
    imports = ["from uuid import UUID",
               f"from domain.entities.{s} import {n}",
               f"from domain.repositories.{s}_repository import {n}RepositoryPort",
               "from shared.application.base_service import BaseService"]
    if targets:
        imports.append("from shared.domain.exceptions import EntityNotFoundException")
    lines = ["\n".join(imports), "", "from application.ports.logger import LoggerPort", "",
             "", f"class {n}Service(BaseService[{n}]):",
             "    def __init__(self, repository: "
             f"{n}RepositoryPort, logger: LoggerPort) -> None:",
             "        super().__init__(repository, logger)"]
    if targets:
        checks: list[str] = []
        for f in e.fks:
            r = _rel_snake(f.related_entity or "")
            if f.required:
                checks.append(f"        self._validate_{r}_ids([entity.{f.name}])")
            else:
                checks += [f"        if entity.{f.name} is not None:",
                           f"            self._validate_{r}_ids([entity.{f.name}])"]
        for f in e.m2m:
            r = _rel_snake(f.related_entity or "")
            checks.append(f"        self._validate_{r}_ids(entity.{f.name})")
        lines += ["",
                  f"    def create(self, entity: {n}) -> {n}:", *checks,
                  "        return super().create(entity)",
                  "",
                  f"    def update(self, entity_id: int | UUID, entity: {n}) -> {n}:", *checks,
                  "        return super().update(entity_id, entity)"]
        for R in targets:
            r = _rel_snake(R)
            lines += ["",
                      f"    def _validate_{r}_ids(self, {r}_ids: list[UUID]) -> None:",
                      f"        found = set(self.repository.get_active_{r}_ids({r}_ids))",
                      f"        missing = [{r}_id for {r}_id in {r}_ids if {r}_id not in found]",
                      "        if missing:",
                      "            raise EntityNotFoundException("
                      f'f"{{missing[0]}} no existe entre los registros activos de {R}.")']
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 4. ORM
def render_orm(e: Entity, known: "dict[str, Entity] | None" = None) -> str:
    n, s = e.name, e.snake
    types = {f.type for f in e.scalars}
    uniques = e.unique_fields
    sa_imports = ["Boolean", "DateTime", "String", "Uuid"]
    if "int" in types:
        sa_imports.append("Integer")
    if "float" in types:
        sa_imports.append("Float")
    if uniques:
        sa_imports.append("Index")
        if any(f.type == "str" for f in uniques):
            sa_imports.append("func")
    if e.m2m:
        sa_imports += ["Column", "Table"]
    if e.fks or e.m2m:
        sa_imports.append("ForeignKey")
    # created_on/updated_on siempre usan datetime
    imports = ["from datetime import datetime"]
    if any(f.relation == ONE_TO_ONE for f in e.inverses):
        imports.append("from typing import Optional")
    if e.m2m or e.inverses:
        imports.append("from typing import TYPE_CHECKING")
    imports += ["from uuid import UUID",
                f"from sqlalchemy import {', '.join(sa_imports)}",
                "from sqlalchemy.orm import Mapped, mapped_column"]
    if e.m2m or e.inverses:
        imports.append("from sqlalchemy.orm import relationship")
    lines = ["\n".join(imports), "",
             "from shared.infrastructure.persistence.database import Base", ""]
    related_entities = {
        f.related_entity
        for f in e.m2m + e.inverses
        if f.related_entity and f.related_entity != n
    }
    if related_entities:
        lines += ["if TYPE_CHECKING:"]
        lines += [
            f"    from infrastructure.database.models.{_rel_snake(name)}_orm "
            f"import {name}ORM"
            for name in sorted(related_entities)
        ]
        lines.append("")
    for f in e.m2m:
        r = _rel_snake(f.related_entity or "")
        rt = _table_of(f.related_entity or "", known)
        lines += ["", f"{s}_{r}s = Table(",
                  f'    "{s}_{r}s",',
                  "    Base.metadata,",
                  f'    Column("{s}_id", Uuid(as_uuid=True), ForeignKey("{e.table}.id"), primary_key=True),',
                  f'    Column("{r}_id", Uuid(as_uuid=True), ForeignKey("{rt}.id"), primary_key=True),',
                  ")"]
    lines += ["", "", f"class {n}ORM(Base):", f'    __tablename__ = "{e.table}"', ""]
    for f in e.scalars:
        base = _ORM.get(f.type, "String")
        if f.type == "str" and f.max_length:
            base = f"String({f.max_length})"
        fk = idx = ""
        if f.is_fk:
            fk = f', ForeignKey("{_table_of(f.related_entity or "", known)}.id")'
            if f.relation == MANY_TO_ONE:
                idx = ", index=True"
        null = ", nullable=False" if f.required else ", nullable=True"
        py_t = _PY.get(f.type, "str") + (" | None" if not f.required else "")
        lines.append(f"    {f.name}: Mapped[{py_t}] = mapped_column({base}{fk}{null}{idx})")
    lines += ["    created_on: Mapped[datetime] = mapped_column("
              "DateTime(timezone=True), nullable=False)",
              "    updated_on: Mapped[datetime | None] = mapped_column("
              "DateTime(timezone=True), nullable=True)",
              "    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)"]
    for f in e.m2m:
        r = _rel_snake(f.related_entity or "")
        lines.append(f'    {f.rel_attr}: Mapped[list["{f.related_entity}ORM"]] = '
                     f'relationship(secondary={s}_{r}s, lazy="selectin")')
    for f in e.inverses:
        R = f.related_entity or ""
        fk_col = f.mapped_by or f"{s}_id"
        join = f'"and_({n}ORM.id == {R}ORM.{fk_col}, {R}ORM.deleted.is_(False))"'
        if f.relation == ONE_TO_MANY:
            head = f'    {f.rel_attr}: Mapped[list["{R}ORM"]] = relationship('
            extra = []
        else:
            head = f'    {f.rel_attr}: Mapped[Optional["{R}ORM"]] = relationship('
            extra = ["        uselist=False,"]
        lines += [head, f'        "{R}ORM",', f"        primaryjoin={join},",
                  f'        foreign_keys="{R}ORM.{fk_col}",', *extra,
                  "        viewonly=True,", '        lazy="selectin",', "    )"]
    if uniques:
        lines += ["", "    __table_args__ = ("]
        for f in uniques:
            col = f"func.lower({f.name})" if f.type == "str" else f.name
            lines.append(f'        Index("{_uq_name(e, f)}", {col}, unique=True, '
                         "postgresql_where=deleted.is_(False)),")
        lines.append("    )")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 5. repo SQL
def render_sql_repository(e: Entity, known: "dict[str, Entity] | None" = None) -> str:
    n, s = e.name, e.snake
    uniques = e.unique_fields
    targets = e.owning_targets
    imports = []
    if uniques:
        imports += ["from collections.abc import Mapping", "from typing import ClassVar"]
    if targets:
        imports.append("from uuid import UUID")
    if e.m2m:
        imports += [
            "from datetime import UTC, datetime",
            "from shared.domain.exceptions import EntityNotFoundException",
        ]
    imports += ["from sqlalchemy.orm import Session",
                f"from domain.entities.{s} import {n}",
                f"from domain.repositories.{s}_repository import {n}RepositoryPort"]
    for R in targets:
        if R != n:
            imports.append(f"from infrastructure.database.models."
                           f"{_rel_snake(R)}_orm import {R}ORM")
    imports += [f"from infrastructure.database.models.{s}_orm import {n}ORM",
                "from shared.infrastructure.sql_repository import SQLBaseRepository"]
    lines = [
        "\n".join(imports),
        "",
        "",
        f"class {n}Repository(SQLBaseRepository[{n}, {n}ORM], {n}RepositoryPort):",
        f"    def __init__(self, db: Session) -> None:",
        f'        super().__init__(db, {n}, {n}ORM, "{e.resource_name}")',
    ]
    body: list[str] = []

    def gap() -> None:
        if body:
            body.append("")

    if uniques:
        body.append("    _integrity_conflict_messages: ClassVar[Mapping[str, str]] = {")
        for i, f in enumerate(uniques):
            comma = "," if i < len(uniques) - 1 else ""
            body.append(f'        "{_uq_name(e, f)}": '
                        f'"Ya existe un {e.resource_name} activo con ese {f.name}."{comma}')
        body.append("    }")
    for R in targets:
        r = _rel_snake(R)
        gap()
        body += [f"    def get_active_{r}_ids(self, {r}_ids: list[UUID]) -> list[UUID]:",
                 f"        rows = (self.db.query({R}ORM)",
                 f"                .filter({R}ORM.id.in_({r}_ids), {R}ORM.deleted.is_(False))",
                 "                .all())",
                 "        return [row.id for row in rows]"]
    if e.m2m:
        gap()
        for f in e.m2m:
            R = f.related_entity or ""
            r = _rel_snake(R)
            body += [
                f'    def _get_active_{r}s(self, {r}_ids: list[UUID]) -> list["{R}ORM"]:',
                "        if not " + f"{r}_ids:",
                "            return []",
                f"        rows = (self.db.query({R}ORM)",
                f"                .filter({R}ORM.id.in_({r}_ids),",
                f"                        {R}ORM.deleted.is_(False))",
                "                .all())",
                f"        found_ids = {{row.id for row in rows}}",
                f"        if found_ids != set({r}_ids):",
                f"            missing_ids = [item_id for item_id in {r}_ids",
                "                           if item_id not in found_ids]",
                "            raise EntityNotFoundException(",
                f'                f"No existen registros activos de {R} con IDs: '
                '{missing_ids}."',
                "            )",
                "        return rows",
                "",
            ]
        body += [f"    def _to_orm(self, entity: {n}) -> {n}ORM:",
                 "        orm_entity = super()._to_orm(entity)"]
        for f in e.m2m:
            R = f.related_entity or ""
            r = _rel_snake(R)
            body.append(
                f"        orm_entity.{f.rel_attr} = "
                f"self._get_active_{r}s(entity.{f.name})"
            )
        body.append("        return orm_entity")
        body += [
            "",
            f"    def update(self, entity_id: int | UUID, entity: {n}) -> {n}:",
            "        orm_entity = self._get_active_orm_entity(entity_id)",
        ]
        for f in e.scalars:
            body.append(f"        orm_entity.{f.name} = entity.{f.name}")
        for f in e.m2m:
            R = f.related_entity or ""
            r = _rel_snake(R)
            body.append(
                f"        orm_entity.{f.rel_attr} = "
                f"self._get_active_{r}s(entity.{f.name})"
            )
        body += [
            "        orm_entity.updated_on = datetime.now(UTC)",
            "        self._commit()",
            "        self.db.refresh(orm_entity)",
            "        return self._to_domain(orm_entity)",
        ]
    if e.m2m or e.inverses:
        gap()
        body += [f"    def _to_domain(self, orm_entity: {n}ORM) -> {n}:",
                 "        data = {c.name: getattr(orm_entity, c.name) "
                 "for c in orm_entity.__table__.columns}"]
        for f in e.m2m + e.inverses:
            if f.relation == ONE_TO_ONE:
                body.append(f'        data["{f.name}"] = (orm_entity.{f.rel_attr}.id '
                            f"if orm_entity.{f.rel_attr} else None)")
            else:
                body.append(f'        data["{f.name}"] = [item.id for item in '
                            f"orm_entity.{f.rel_attr}]")
        body.append(f"        return {n}(**data)")
    if not body:
        body = ["    pass"]
    return "\n".join(lines + body) + "\n"


# ---------------------------------------------------------------- 6. schemas
def render_schemas(e: Entity) -> str:
    n = e.name
    types = {f.type for f in e.scalars}
    imports = (["from datetime import datetime"] if "datetime" in types else [])
    imports += ["from uuid import UUID", "from pydantic import BaseModel, ConfigDict, Field"]
    lines = ["\n".join(imports), "", "", f"class {n}Base(BaseModel):"]
    for f in e.scalars:
        t = _PY.get(f.type, "str")
        if f.type == "str" and (f.min_length or f.max_length):
            kwargs = ["default=None"] if not f.required else []
            if f.min_length:
                kwargs.append(f"min_length={f.min_length}")
            if f.max_length:
                kwargs.append(f"max_length={f.max_length}")
            optional = " | None" if not f.required else ""
            lines.append(
                f'    {f.name}: {t}{optional} = Field({", ".join(kwargs)})'
            )
        else:
            lines.append(f"    {f.name}: {t}" + ("" if f.required else " | None = None"))
    for f in e.m2m:  # los lados inversos son de solo lectura: no entran en Create/Update
        lines.append(f"    {f.name}: list[UUID] = Field(default_factory=list)")
    if not e.scalars and not e.m2m:
        lines.append("    pass")
    lines += ["", f"class {n}Create({n}Base):", "    pass", "",
              f"class {n}Update({n}Base):", "    pass", "",
              f"class {n}Response(BaseModel):",
              "    model_config = ConfigDict(from_attributes=True)", "",
              "    id: UUID"]
    for f in e.scalars:
        t = _PY.get(f.type, "str")
        lines.append(f"    {f.name}: {t}" + ("" if f.required else " | None = None"))
    for f in e.relations:
        if f.relation == ONE_TO_ONE:
            lines.append(f"    {f.name}: UUID | None = None")
        else:
            lines.append(f"    {f.name}: list[UUID]")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 7. controller
def render_controller(e: Entity) -> str:
    n, s = e.name, e.snake
    payload_args = ", ".join(f.name + "=payload." + f.name for f in e.scalars)
    relation_args = ", ".join(f.name + "=payload." + f.name for f in e.m2m)
    factory_args = ", ".join(a for a in (payload_args, relation_args) if a)
    update_args = factory_args
    lines = ["from uuid import UUID", "",
             f"from domain.entities.{s} import {n}",
             f"from infrastructure.api.dependencies import get_{s}_service",
             f"from infrastructure.api.schemas.{s}_schemas import "
             f"{n}Create, {n}Response, {n}Update",
             "from shared.infrastructure.generic_controller import create_generic_router", "",
             "",
             f"def update_{s}({s}_id: int | UUID, payload: {n}Update) -> {n}:",
             f"    if not isinstance({s}_id, UUID):",
             f'        raise TypeError("El ID de un {s} debe ser UUID.")',
             f"    return {n}(id={s}_id" + (f", {update_args}" if update_args else "") + ")",
             "",
             "",
             "router = create_generic_router(",
             f"    service_provider=get_{s}_service,",
             f"    request_schema={n}Create,",
             f"    entity_factory=lambda payload: {n}("
             + (factory_args if factory_args else "") + "),",
             f"    update_schema={n}Update,",
             f"    update_factory=update_{s},",
             f'    resource_name="{e.resource_name}",',
             f"    response_schema={n}Response,",
             "    entity_id_type=UUID,",
             f'    prefix="/{e.table}",',
             f'    tags=["{e.table.capitalize()}"],',
             ")"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 8. tests
def _val(f: EntityField) -> str:
    if f.type == "str":
        return '"ejemplo"'
    if f.type in ("int", "float"):
        return "1"
    if f.type == "bool":
        return "True"
    if f.type == "datetime":
        return '"2026-01-01T00:00:00Z"'
    if f.type == "uuid":
        return '"11111111-1111-1111-1111-111111111111"'
    return "None"


def _payload_parts(e: Entity) -> tuple[list[str], str]:
    """(líneas previas, literal del dict). Crea una vez cada destino de FK requerida."""
    pre: list[str] = []
    seen: list[str] = []
    items: list[str] = []
    for f in e.scalars:
        if f.is_fk:
            if not f.required:
                continue
            r = _rel_snake(f.related_entity or "")
            if r not in seen:
                seen.append(r)
                pre.append(f"    {r}_id = _create_{r}(client)")
            items.append(f'"{f.name}": {r}_id')
        elif f.required:
            items.append(f'"{f.name}": {_val(f)}')
    return pre, "{" + ", ".join(items) + "}"


def _helper_order(e: Entity, known: "dict[str, Entity] | None") -> list[str]:
    """Destinos de FK requeridas y N:M, dependencias primero."""
    order: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        rel = (known or {}).get(name)
        if rel:
            for f in rel.fks:
                if f.required and f.related_entity:
                    visit(f.related_entity)
        order.append(name)

    for f in e.fks + e.m2m:
        if (f.is_m2m or f.required) and f.related_entity:
            visit(f.related_entity)
    return order


def render_integration_test(e: Entity, known: "dict[str, Entity] | None" = None) -> str:
    s, table = e.snake, e.table
    pre, payload = _payload_parts(e)
    helper_lines: list[str] = []
    needs_pytest = False
    for name in _helper_order(e, known):
        rel = (known or {}).get(name)
        r = _rel_snake(name)
        if rel is None:
            needs_pytest = True
            helper_lines += [f"def _create_{r}(client: TestClient) -> str:",
                             f'    pytest.skip("Definí cómo crear un {name} para este test")',
                             "", ""]
            continue
        hpre, hdict = _payload_parts(rel)
        helper_lines += [f"def _create_{r}(client: TestClient) -> str:", *hpre,
                         f'    response = client.post("/api/v1/{rel.table}/", json={hdict})',
                         "    assert response.status_code == 201",
                         '    return response.json()["id"]', "", ""]
    header = (["import pytest"] if needs_pytest else []) + [
        "from fastapi.testclient import TestClient", "", f'BASE = "/api/v1/{table}"', "", ""]
    required_fks = [f for f in e.fks if f.required]
    lines = [*header, *helper_lines,
             "def _payload(client: TestClient) -> dict:", *pre, f"    return {payload}", "", "",
             f"def test_create_{s}(client: TestClient) -> None:",
             "    payload = _payload(client)",
             '    response = client.post(BASE + "/", json=payload)',
             "    assert response.status_code == 201",
             "    body = response.json()",
             '    assert "id" in body',
             '    assert "deleted" not in body',
             '    assert "created_on" not in body',
             *[f'    assert body["{f.name}"] == payload["{f.name}"]' for f in required_fks],
             "", "",
             f"def test_list_{table}_paginated(client: TestClient) -> None:",
             '    client.post(BASE + "/", json=_payload(client))',
             '    response = client.get(BASE + "/?limit=10&offset=0")',
             "    assert response.status_code == 200",
             "    body = response.json()",
             '    assert body["total"] == 1',
             '    assert len(body["items"]) == 1',
             '    assert body["limit"] == 10 and body["offset"] == 0',
             "", "",
             f"def test_get_{s}_by_id(client: TestClient) -> None:",
             '    created = client.post(BASE + "/", json=_payload(client)).json()',
             '    response = client.get(BASE + "/{}".format(created["id"]))',
             "    assert response.status_code == 200",
             '    assert response.json()["id"] == created["id"]',
             "", "",
             f"def test_get_{s}_not_found(client: TestClient) -> None:",
             '    response = client.get(BASE + "/00000000-0000-0000-0000-000000000000")',
             "    assert response.status_code == 404",
             '    assert response.json()["code"] == "not_found"',
             "", "",
             f"def test_update_{s}(client: TestClient) -> None:",
             "    payload = _payload(client)",
             '    created = client.post(BASE + "/", json=payload).json()',
             '    response = client.put(BASE + "/{}".format(created["id"]), json=payload)',
             "    assert response.status_code == 200",
             "", "",
             f"def test_soft_delete_{s}(client: TestClient) -> None:",
             '    created = client.post(BASE + "/", json=_payload(client)).json()',
             '    response = client.delete(BASE + "/{}".format(created["id"]))',
             "    assert response.status_code == 204",
             '    assert client.get(BASE + "/{}".format(created["id"])).status_code == 404',
             '    assert client.get(BASE + "/").json()["total"] == 0']
    for f in required_fks:
        lines += ["", "",
                  f"def test_create_{s}_unknown_{f.name}(client: TestClient) -> None:",
                  "    payload = _payload(client)",
                  f'    payload["{f.name}"] = "00000000-0000-0000-0000-000000000000"',
                  '    response = client.post(BASE + "/", json=payload)',
                  "    assert response.status_code == 404"]
    for f in e.m2m:
        r = _rel_snake(f.related_entity or "")
        lines += [
            "",
            "",
            f"def test_{s}_{f.name}_persist_on_create_and_update(client: TestClient) -> None:",
            f"    first_{r}_id = _create_{r}(client)",
            f"    second_{r}_id = _create_{r}(client)",
            "    payload = _payload(client)",
            f'    payload["{f.name}"] = [first_{r}_id]',
            '    created = client.post(BASE + "/", json=payload)',
            "    assert created.status_code == 201",
            f'    assert created.json()["{f.name}"] == [first_{r}_id]',
            "    updated_payload = dict(payload)",
            f'    updated_payload["{f.name}"] = [second_{r}_id]',
            '    updated = client.put(',
            '        BASE + "/{}".format(created.json()["id"]), json=updated_payload',
            "    )",
            "    assert updated.status_code == 200",
            f'    assert updated.json()["{f.name}"] == [second_{r}_id]',
        ]
    if e.unique_fields:
        lines += ["", "",
                  f"def test_duplicate_{s}_conflict(client: TestClient) -> None:",
                  "    payload = _payload(client)",
                  '    client.post(BASE + "/", json=payload)',
                  '    response = client.post(BASE + "/", json=payload)',
                  "    assert response.status_code == 409",
                  '    assert response.json()["code"] == "conflict"']
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 9. bloque dependencies
def render_dependency_provider(e: Entity) -> str:
    n, s = e.name, e.snake
    return f"""


def get_{s}_service(
    db: Annotated[Session, Depends(get_db)],
    logger: Annotated[LoggerPort, Depends(get_logger)],
) -> {n}Service:
    return {n}Service({n}Repository(db), logger)
"""


def build_files(e: Entity, known: "dict[str, Entity] | None" = None) -> dict[str, str]:
    """known: entidades del proyecto por nombre (nombres de tabla, helpers de tests)."""
    s = e.snake
    return {
        f"domain/entities/{s}.py": render_domain_entity(e),
        f"domain/repositories/{s}_repository.py": render_port(e),
        f"application/services/{s}_service.py": render_service(e),
        f"infrastructure/database/models/{s}_orm.py": render_orm(e, known),
        f"infrastructure/database/repositories/{s}_repository.py": render_sql_repository(e, known),
        f"infrastructure/api/schemas/{s}_schemas.py": render_schemas(e),
        f"infrastructure/api/controllers/{s}_controller.py": render_controller(e),
        f"tests/integration/test_{e.table}_api.py": render_integration_test(e, known),
    }
