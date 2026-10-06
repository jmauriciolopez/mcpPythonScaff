"""Descubrimiento de entidades: domain/entities/*.py o reflexión de BD.

Convenciones que lee el discovery en tu entidad escrita a mano:

    @dataclass(kw_only=True)
    class Product(AuditableEntity):
        __resource_name__ = "Producto"          # opcional (español)
        __plural__ = "products"                 # opcional (tabla/ruta)

        name: str = field(metadata={"unique": True, "min_length": 1, "max_length": 200})
        price: float | None = None

        # N:M (lado que escribe): list[UUID] llamado <destino>_ids
        role_ids: list[UUID] = field(default_factory=list)

        # N:1 (lado FK de un 1:N): columna FK en esta tabla
        category_id: UUID = field(metadata={"relation": "many_to_one"})   # -> Category

        # 1:1 (lado dueño): FK con índice único sobre activos
        sku_id: UUID | None = field(default=None, metadata={"relation": "one_to_one"})

    @dataclass(kw_only=True)
    class Category(AuditableEntity):
        # 1:N (lado "uno"): solo lectura, ids de los hijos activos
        product_ids: list[UUID] = field(default_factory=list,
                                        metadata={"relation": "one_to_many",
                                                  "mapped_by": "category_id"})

    @dataclass(kw_only=True)
    class Sku(AuditableEntity):
        # 1:1 (lado inverso): solo lectura, id del dueño o None
        product_id: UUID | None = field(default=None,
                                        metadata={"relation": "one_to_one",
                                                  "mapped_by": "sku_id"})

La entidad destino se infiere del nombre (`category_id` -> Category, `product_ids` ->
Product) o se fuerza con metadata={"entity": "Category"}. `mapped_by` en el lado inverso
es el campo FK del hijo (default: <esta_entidad>_id).

Tipos soportados: str, int, float, bool, datetime, date, UUID, X | None, Optional[X],
list[UUID].
"""
import ast
import re
from pathlib import Path

from .templates import (MANY_TO_MANY, MANY_TO_ONE, ONE_TO_MANY, ONE_TO_ONE,
                        RELATION_KINDS, Entity, EntityField)

_TYPES = {"str": "str", "int": "int", "float": "float", "bool": "bool",
          "datetime": "datetime", "UUID": "uuid", "date": "datetime"}
_AUDIT = ("id", "created_on", "updated_on", "deleted")
_SCALAR_META = ("unique", "min_length", "max_length")
_META_KEYS = _SCALAR_META + ("relation", "entity", "mapped_by")


def _pascal(snake: str) -> str:
    return "".join(w.capitalize() for w in snake.split("_"))


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _infer_target(fname: str) -> str:
    """category_id -> Category, product_ids -> Product, products -> Product."""
    for suffix in ("_ids", "_id"):
        if fname.endswith(suffix) and len(fname) > len(suffix):
            return _pascal(fname[: -len(suffix)])
    base = fname
    if base.endswith("ies"):
        base = base[:-3] + "y"
    elif base.endswith("s"):
        base = base[:-1]
    return _pascal(base)


def _is_entity_class(node: ast.ClassDef) -> bool:
    """Dataclass o subclase de AuditableEntity (descarta Enums, excepciones, helpers)."""
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = getattr(target, "id", getattr(target, "attr", ""))
        if name == "dataclass":
            return True
    for base in node.bases:
        if getattr(base, "id", getattr(base, "attr", "")) == "AuditableEntity":
            return True
    return False


def _unwrap_optional(ann: ast.expr) -> tuple[ast.expr, bool]:
    """Devuelve (tipo_base, es_opcional) para `X | None` y `Optional[X]`."""
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        left, right = ann.left, ann.right
        if isinstance(right, ast.Constant) and right.value is None:
            return left, True
        if isinstance(left, ast.Constant) and left.value is None:
            return right, True
    if (isinstance(ann, ast.Subscript) and getattr(ann.value, "id", "") == "Optional"):
        return ann.slice, True
    return ann, False


def _parse_default(value: ast.expr | None) -> tuple[bool, dict]:
    """(tiene_default, metadata). Entiende `= None`, `= 0` y `field(...)`."""
    if value is None:
        return False, {}
    func_name = getattr(value.func, "id", getattr(value.func, "attr", "")) if isinstance(value, ast.Call) else ""
    if func_name in ("field", "dc_field"):  # dc_field = dataclasses.field importado con alias
        has_default = False
        meta: dict = {}
        for kw in value.keywords:
            if kw.arg in ("default", "default_factory"):
                has_default = True
            elif kw.arg == "metadata":
                try:
                    raw = ast.literal_eval(kw.value)
                    meta = {k: v for k, v in raw.items() if k in _META_KEYS}
                except (ValueError, SyntaxError, AttributeError):
                    pass
        return has_default, meta
    return True, {}


def _class_options(node: ast.ClassDef) -> dict[str, str]:
    opts: dict[str, str] = {}
    for stmt in node.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id in ("__resource_name__", "__plural__")
                and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)):
            opts[stmt.targets[0].id] = stmt.value.value
    return opts


def _parse_class(node: ast.ClassDef) -> Entity | None:
    cls_snake = _snake(node.name)
    fields: list[EntityField] = []
    for stmt in node.body:
        if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
            continue
        fname = stmt.target.id
        if fname in _AUDIT or fname.startswith("_"):
            continue
        where = f"{node.name}.{fname}"
        has_default, meta = _parse_default(stmt.value)
        optional_default = (isinstance(stmt.value, ast.Constant) and stmt.value.value is None)
        base, optional = _unwrap_optional(stmt.annotation)
        required = not (has_default or optional or optional_default)

        rel = meta.get("relation") or ""
        target = meta.get("entity")
        mapped_by = meta.get("mapped_by")
        if rel and rel not in RELATION_KINDS:
            raise ValueError(f"{where}: relation '{rel}' inválida. "
                             f"Usá una de: {', '.join(RELATION_KINDS)}")

        is_uuid = isinstance(base, ast.Name) and base.id == "UUID"
        is_uuid_list = (isinstance(base, ast.Subscript)
                        and getattr(base.value, "id", "") == "list"
                        and isinstance(base.slice, ast.Name) and base.slice.id == "UUID")

        if is_uuid and rel:
            if rel in (MANY_TO_ONE, ONE_TO_ONE) and not mapped_by:
                # lado dueño: columna FK en esta tabla
                related = target or (_pascal(fname[:-3]) if fname.endswith("_id") else None)
                if not related:
                    raise ValueError(f"{where}: no se pudo inferir la entidad destino; "
                                     'usá metadata={"entity": "Destino"}.')
                fields.append(EntityField(name=fname, type="uuid", required=required,
                                          relation=rel, related_entity=related))
            elif rel == ONE_TO_ONE:
                # lado inverso (virtual, solo lectura)
                fields.append(EntityField(name=fname, type="relation", required=False,
                                          relation=ONE_TO_ONE, mapped_by=mapped_by,
                                          related_entity=target or _infer_target(fname)))
            else:
                raise ValueError(f"{where}: '{rel}' no aplica a un campo UUID "
                                 "(many_to_one/one_to_one) — para uno-a-muchos usá list[UUID].")
        elif is_uuid_list:
            if rel in ("", MANY_TO_MANY):
                fields.append(EntityField(name=fname, type="relation", required=False,
                                          relation=MANY_TO_MANY,
                                          related_entity=target or _infer_target(fname)))
            elif rel == ONE_TO_MANY:
                fields.append(EntityField(name=fname, type="relation", required=False,
                                          relation=ONE_TO_MANY,
                                          related_entity=target or _infer_target(fname),
                                          mapped_by=mapped_by or f"{cls_snake}_id"))
            else:
                raise ValueError(f"{where}: '{rel}' no aplica a list[UUID] "
                                 "(many_to_many/one_to_many).")
        elif isinstance(base, ast.Name) and base.id in _TYPES:
            scalar_meta = {k: v for k, v in meta.items() if k in _SCALAR_META}
            fields.append(EntityField(name=fname, type=_TYPES[base.id], required=required,
                                      **scalar_meta))
    if not fields:
        return None
    opts = _class_options(node)
    return Entity(name=node.name, fields=fields,
                  resource_name=opts.get("__resource_name__", ""),
                  plural=opts.get("__plural__", ""))


def discover_entities(project_root: str) -> list[Entity]:
    """Lee las entidades de domain/entities/*.py."""
    entities: list[Entity] = []
    root = Path(project_root) / "domain" / "entities"
    if not root.is_dir():
        return entities
    for path in sorted(root.glob("*.py")):
        if path.name.startswith("__"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:  # solo nivel superior
            if isinstance(node, ast.ClassDef) and _is_entity_class(node):
                ent = _parse_class(node)
                if ent:
                    entities.append(ent)
    return entities


def scaffold_status(project_root: str, e: Entity) -> dict[str, bool]:
    """Qué piezas del slice ya existen para la entidad (según build_files)."""
    from .templates import build_files
    root = Path(project_root)
    return {rel: (root / rel).exists() for rel in build_files(e)}


def reflect_database(database_url: str) -> list[Entity]:
    """Refleja el esquema de una BD (omite columnas de auditoría).

    Las FK se reflejan como many_to_one (o one_to_one si la columna es única).
    """
    from sqlalchemy import create_engine, inspect

    engine = create_engine(database_url)
    inspector = inspect(engine)
    table_names = [t for t in inspector.get_table_names() if t != "alembic_version"]

    def entity_name(table: str) -> str:
        singular = re.sub(r"ies$", "y", table) if table.endswith("ies") else table.rstrip("s")
        return _pascal(singular)

    entities: list[Entity] = []
    for table in table_names:
        fks = {fk["constrained_columns"][0]: fk for fk in inspector.get_foreign_keys(table)
               if len(fk["constrained_columns"]) == 1}
        uniques = {u["column_names"][0] for u in inspector.get_unique_constraints(table)
                   if len(u["column_names"]) == 1}
        uniques |= {i["column_names"][0] for i in inspector.get_indexes(table)
                    if i.get("unique") and len(i["column_names"]) == 1}
        fields: list[EntityField] = []
        for col in inspector.get_columns(table):
            if col["name"] in _AUDIT:
                continue
            required = not col.get("nullable", True)
            fk = fks.get(col["name"])
            if fk and fk["referred_table"] in table_names:
                fields.append(EntityField(
                    name=col["name"], type="uuid", required=required,
                    relation=ONE_TO_ONE if col["name"] in uniques else MANY_TO_ONE,
                    related_entity=entity_name(fk["referred_table"])))
                continue
            tn = type(col["type"]).__name__
            if tn == "UUID":
                t = "uuid"
            elif tn in ("Integer", "BigInteger", "SmallInteger"):
                t = "int"
            elif tn in ("Float", "Numeric", "Double"):
                t = "float"
            elif tn == "Boolean":
                t = "bool"
            elif "DateTime" in tn or tn == "Date":
                t = "datetime"
            else:
                t = "str"
            fields.append(EntityField(name=col["name"], type=t, required=required))
        if fields:
            entities.append(Entity(name=entity_name(table), fields=fields, plural=table))
    return entities
