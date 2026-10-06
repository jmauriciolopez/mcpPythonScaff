"""MCP server: scaffolding hexagonal CRUD para el proyecto Demo API.

Flujo: creás la entidad a mano en domain/entities/<x>.py y el MCP genera el resto
(puerto, servicio, ORM, repo, schemas, controller, tests) y lo cablea.
La entidad de dominio es TU fuente de verdad: nunca se sobrescribe.

Relaciones (ver entities.py para la sintaxis en la entidad):
  many_to_many, many_to_one / one_to_many (1:N) y one_to_one (1:1).

Tools:
  list_entities       — entidades de domain/entities/*.py + estado del scaffolding
  reflect_database    — entidades desde el esquema de una BD
  generate_controller — genera el slice de una entidad (dry_run / overwrite)
  generate_pending    — genera el slice de todas las entidades sin scaffolding
"""
import ast
import json
import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import entities as ent
from .templates import (MANY_TO_ONE, ONE_TO_MANY, ONE_TO_ONE, Entity, EntityField,
                        build_files, render_dependency_provider)

mcp = FastMCP("controller-builder")


def _to_dict(e: Entity) -> dict:
    return {"name": e.name, "resource_name": e.resource_name, "plural": e.plural,
            "fields": [{"name": f.name, "type": f.type, "required": f.required,
                        "min_length": f.min_length, "max_length": f.max_length,
                        "unique": f.unique, "related_entity": f.related_entity,
                        "relation": f.relation, "mapped_by": f.mapped_by}
                       for f in e.fields]}


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _orm_path(root: Path, entity_name: str) -> Path:
    return root / "infrastructure" / "database" / "models" / f"{_snake(entity_name)}_orm.py"


@mcp.tool()
def list_entities(project_root: str) -> str:
    """Lista las entidades de domain/entities/*.py e indica cuáles ya tienen scaffolding."""
    out = []
    for e in ent.discover_entities(project_root):
        status = ent.scaffold_status(project_root, e)
        d = _to_dict(e)
        d["scaffolded"] = all(v for k, v in status.items() if not k.startswith("domain/entities/"))
        d["missing_files"] = [k for k, v in status.items() if not v]
        out.append(d)
    return json.dumps(out, indent=2, ensure_ascii=False)


@mcp.tool()
def reflect_database(database_url: str) -> str:
    """Refleja una base de datos en definiciones de entidad (omite auditoría).

    Las claves foráneas se reflejan como many_to_one (one_to_one si la columna es única).
    database_url: URL SQLAlchemy, ej. postgresql+psycopg://user:pass@host/db
    """
    return json.dumps([_to_dict(e) for e in ent.reflect_database(database_url)],
                      indent=2, ensure_ascii=False)


# ------------------------------------------------------------------ edición asistida
def _last_import_line(text: str) -> int:
    """Línea (1-based) donde termina el último import de nivel superior; 0 si no hay.
    Usa ast, así soporta imports multilínea con paréntesis."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return 0
    last = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = node.end_lineno or node.lineno
    return last


def _ensure_imports(path: Path, imports: list[str]) -> bool:
    """Agrega imports faltantes después del último import de nivel superior."""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    existing = {l.strip() for l in text.splitlines()}
    missing = [i for i in imports if i.strip() not in existing]
    if not missing:
        return False
    lines = text.splitlines()
    at = _last_import_line(text)
    lines[at:at] = missing
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def _append_once(path: Path, block: str) -> bool:
    """Agrega un bloque al final del archivo si no existe ya."""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    if block.strip() in text:
        return False
    path.write_text(text.rstrip("\n") + "\n" + block, encoding="utf-8")
    return True


def _include_router(path: Path, line: str) -> bool:
    """Inserta include_router junto al último include_router existente (o al final)."""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    if line in text:
        return False
    lines = text.splitlines()
    idx = max((i for i, l in enumerate(lines) if l.startswith("app.include_router(")),
              default=None)
    if idx is None:
        return _append_once(path, line + "\n")
    lines.insert(idx + 1, line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def _wire_project(root: Path, e: Entity) -> list[str]:
    """Edita main.py, dependencies.py, create_tables.py y conftest.py."""
    s, n = e.snake, e.name
    edits: list[str] = []

    main_py = root / "main.py"
    if _ensure_imports(main_py, [
        f"from infrastructure.api.controllers.{s}_controller import "
        f"router as {s}s_router",
    ]):
        edits.append(f"main.py: import de {s}s_router")
    if _include_router(main_py, f'app.include_router({s}s_router, prefix="/api/v1")'):
        edits.append(f"main.py: include_router({s}s_router)")

    deps = root / "infrastructure" / "api" / "dependencies.py"
    if _ensure_imports(deps, [
        f"from application.services.{s}_service import {n}Service",
        f"from infrastructure.database.repositories.{s}_repository import {n}Repository",
        "from sqlalchemy.orm import Session",
    ]):
        edits.append("dependencies.py: imports")
    already = deps.exists() and f"def get_{s}_service" in deps.read_text(encoding="utf-8")
    if not already and _append_once(deps, render_dependency_provider(e)):
        edits.append(f"dependencies.py: get_{s}_service")

    orm_import = f"import infrastructure.database.models.{s}_orm  # noqa: F401"
    for rel in (Path("scripts") / "create_tables.py", Path("tests") / "conftest.py"):
        if _ensure_imports(root / rel, [orm_import]):
            edits.append(f"{rel}: import ORM")
    return edits


def _check_relations(root: Path, entity: Entity,
                     known: dict[str, Entity]) -> tuple[str | None, list[str]]:
    """Valida las relaciones. Devuelve (error | None, advertencias)."""
    warnings: list[str] = []
    for f in entity.fields:
        if f.type != "relation" and not f.is_fk:
            continue
        target = f.related_entity
        if not target:
            return (f"El campo relación '{f.name}' necesita la entidad destino "
                    f"(convención: <destino>_id / <destino>_ids, o metadata "
                    f"{{\"entity\": ...}}). Definición: {_to_dict(entity)}"), warnings
        if f.is_fk or f.is_m2m:
            # lado que escribe: la tabla destino y su ORM deben existir (salvo auto-referencia)
            if target != entity.name and not _orm_path(root, target).exists():
                return (f"La entidad destino '{target}' (campo '{f.name}') no tiene ORM en "
                        f"{_orm_path(root, target)}. Generala primero."), warnings
            continue
        # lado inverso (uno-a-muchos / uno-a-uno): lee la FK del hijo
        fk_name = f.mapped_by or f"{entity.snake}_id"
        child = known.get(target)
        if child is not None:
            col = next((c for c in child.fks if c.name == fk_name
                        and c.related_entity == entity.name), None)
            if col is None:
                return (f"'{entity.name}.{f.name}' lee {target}.{fk_name}, pero {target} no "
                        f"define ese campo como many_to_one/one_to_one hacia {entity.name}. "
                        "Definilo en la entidad hija (o corregí mapped_by)."), warnings
            if f.relation == ONE_TO_MANY and col.relation != MANY_TO_ONE:
                return (f"'{entity.name}.{f.name}' es one_to_many pero {target}.{fk_name} es "
                        f"{col.relation}; usá many_to_one en el hijo."), warnings
            if f.relation == ONE_TO_ONE and col.relation != ONE_TO_ONE:
                return (f"'{entity.name}.{f.name}' es one_to_one pero {target}.{fk_name} es "
                        f"{col.relation}; usá one_to_one en el dueño."), warnings
        if target != entity.name and not _orm_path(root, target).exists():
            warnings.append(f"El ORM de '{target}' todavía no existe: la relación "
                            f"'{f.name}' resolverá cuando lo generes (generá {target} después).")
    return None, warnings


def _scaffold(root: Path, entity: Entity, known: dict[str, Entity],
              overwrite: bool, dry_run: bool) -> str:
    """Genera los archivos del slice. Nunca toca la entidad de dominio si ya existe."""
    known = {**known, entity.name: entity}
    error, warnings = _check_relations(root, entity, known)
    if error:
        return error

    created, skipped, overwritten = [], [], []
    for rel_path, content in build_files(entity, known).items():
        path = root / rel_path
        is_domain = rel_path.startswith("domain/entities/")
        if path.exists() and (is_domain or not overwrite):
            skipped.append(rel_path)
            continue
        if path.exists():
            overwritten.append(rel_path)
            if not dry_run:
                path.with_suffix(path.suffix + ".bak").write_text(
                    path.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            created.append(rel_path)
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    edits = [] if dry_run else _wire_project(root, entity)
    fmt = lambda xs: "\n".join(f"- {x}" for x in xs) or "(ninguno)"  # noqa: E731
    head = "[DRY RUN] " if dry_run else ""
    warn = f"\n\nAdvertencias:\n{fmt(warnings)}" if warnings else ""
    return (f"{head}Slice hexagonal de '{entity.name}' ({entity.resource_name}):\n"
            f"Creados:\n{fmt(created)}\n"
            f"Sobrescritos (con .bak):\n{fmt(overwritten)}\n"
            f"Omitidos (ya existen; usá overwrite=true, la entidad de dominio nunca se pisa):\n"
            f"{fmt(skipped)}\n\nCableado:\n{fmt(edits)}{warn}\n\n"
            "Próximos pasos: python -m scripts.create_tables && pytest")


@mcp.tool()
def generate_controller(entity_json: str, project_root: str, resource_name: str = "",
                        overwrite: bool = False, dry_run: bool = False) -> str:
    """Genera el slice CRUD de una entidad y lo cablea al proyecto.

    entity_json: nombre de una entidad ya escrita en domain/entities (recomendado),
      o JSON inline si todavía no existe:
      {"name": "Product", "plural": "products", "fields": [
        {"name": "name", "type": "str", "min_length": 1, "max_length": 200,
         "unique": true},
        {"name": "role_ids", "type": "relation", "related_entity": "Role"},
        {"name": "category_id", "relation": "many_to_one", "related_entity": "Category"},
        {"name": "sku_id", "relation": "one_to_one", "related_entity": "Sku",
         "required": false}]}
      Lado inverso: {"name": "product_ids", "relation": "one_to_many",
        "related_entity": "Product", "mapped_by": "category_id"} (solo lectura).

    resource_name: nombre en español (también puede ir en __resource_name__ de la entidad).
    overwrite: reemplaza archivos existentes (deja .bak). La entidad de dominio nunca se pisa.
    dry_run: solo informa qué crearía/omitiría, sin escribir nada.
    Los campos que escriben una relación (N:M, many_to_one, one_to_one) requieren que la
    entidad destino ya tenga ORM; los lados inversos solo avisan.
    """
    root = Path(project_root)
    known = {e.name: e for e in ent.discover_entities(project_root)}
    if entity_json.strip().startswith("{"):
        raw = json.loads(entity_json)
        entity = Entity(
            name=raw["name"],
            resource_name=raw.get("resource_name") or resource_name,
            plural=raw.get("plural", ""),
            fields=[EntityField(**{k: v for k, v in f.items()
                                   if k in EntityField.__dataclass_fields__})
                    for f in raw.get("fields", [])])
        return _scaffold(root, entity, known, overwrite, dry_run)

    name = entity_json.strip()
    entity = known.get(name)
    if entity is None:
        available = ", ".join(known) or "ninguna"
        return (f"Entidad '{name}' no encontrada en {project_root}/domain/entities "
                f"(disponibles: {available}). Revisá que sea @dataclass o herede de "
                "AuditableEntity, o pasá un JSON inline.")
    if resource_name:
        entity.resource_name = resource_name
    return _scaffold(root, entity, known, overwrite, dry_run)


@mcp.tool()
def generate_pending(project_root: str, overwrite: bool = False, dry_run: bool = False) -> str:
    """Genera el slice de todas las entidades de domain/entities sin scaffolding completo.

    Se procesan en orden de dependencias: primero las entidades a las que otras apuntan
    con FK / N:M (el lado "uno" antes que el lado "muchos").
    """
    root = Path(project_root)
    known = {e.name: e for e in ent.discover_entities(project_root)}
    pending = []
    for e in known.values():
        status = ent.scaffold_status(project_root, e)
        if not all(v for k, v in status.items() if not k.startswith("domain/entities/")):
            pending.append(e)
    if not pending:
        return "No hay entidades pendientes."

    names = {e.name for e in pending}
    done: set[str] = set()
    ordered: list[Entity] = []
    remaining = pending[:]
    while remaining:
        ready = [e for e in remaining
                 if all(t == e.name or t not in names or t in done for t in e.owning_targets)]
        if not ready:  # ciclo: _scaffold informará qué falta
            ready = remaining[:1]
        for e in ready:
            ordered.append(e)
            done.add(e.name)
            remaining.remove(e)
    return "\n\n".join(_scaffold(root, e, known, overwrite, dry_run) for e in ordered)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
