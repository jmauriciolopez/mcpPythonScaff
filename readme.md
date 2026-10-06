# MCP Controller Builder

Servidor MCP que genera scaffolding CRUD hexagonal para proyectos FastAPI. La
entidad de dominio del proyecto destino es la fuente de verdad y nunca se
sobrescribe.

## Requisitos

- Python 3.10 o posterior.
- Un cliente MCP que pueda iniciar un servidor local por stdio.
- El proyecto destino debe contar con sus propias dependencias para ejecutar el
  código generado (por ejemplo, FastAPI, SQLAlchemy y el driver de base de datos).

## Instalar el servidor MCP

El servidor se instala en el entorno Python que utilizará el cliente MCP. No es
necesario instalarlo como dependencia de la aplicación FastAPI que recibirá el
scaffolding. Se recomienda usar un entorno virtual dedicado.

### Desde una copia local del repositorio

Activa o crea un entorno virtual para el servidor y ejecuta desde la raíz de
este repositorio:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

Si también necesitas la herramienta `reflect_database`, instala el extra
opcional:

```powershell
python -m pip install -e ".[database]"
```

Desde otro directorio o proyecto, instala apuntando a la ruta de la copia local
del repositorio. Sustituye la ruta del ejemplo por la ubicación real:

```powershell
python -m pip install -e "D:\ruta\al\mcp-controller-builder"
```

Para incluir el extra de reflexión:

```powershell
python -m pip install -e "D:\ruta\al\mcp-controller-builder[database]"
```

La instalación editable facilita probar cambios locales. Para un uso estable,
puedes omitir `-e`. Este README no presupone que el paquete esté publicado en
un índice de paquetes.

## Ejecutar y conectar el cliente MCP

El servidor se puede iniciar manualmente desde el entorno donde fue instalado:

```powershell
python -m mcp_controller_builder.server
```

También se instala el comando `mcp-controller-builder`. Ambos métodos inician el
servidor MCP por stdio; normalmente el cliente MCP lo inicia automáticamente.

Registra el servidor en la configuración MCP de tu cliente, usando la ruta al
Python del entorno virtual del servidor. Ejemplo de configuración JSON:

```json
{
  "mcpServers": {
    "controller-builder": {
      "command": "C:\\ruta\\al\\mcp-controller-builder\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_controller_builder.server"]
    }
  }
}
```

Si prefieres utilizar el comando instalado, configura `command` con la ruta
completa a `mcp-controller-builder.exe` dentro de ese mismo entorno virtual y
elimina `args`. Reinicia o recarga el cliente después de modificar su
configuración. La sintaxis y ubicación del archivo de configuración dependen
del cliente MCP.

## Consumirlo desde otro proyecto

1. Instala el servidor en un entorno Python accesible para el cliente MCP, como
   se indicó arriba. El entorno puede ser independiente del entorno virtual de
   la aplicación destino.
2. Configura ese ejecutable como servidor MCP en tu cliente.
3. Usa la ruta absoluta del proyecto FastAPI como `project_root` al llamar las
   herramientas. El servidor opera sobre ese proyecto; no es necesario copiar
   este paquete dentro de él.
4. Primero ejecuta `generate_controller` o `generate_pending` con
   `dry_run=true` para revisar los archivos y cambios de cableado que propone.
   Luego repite la llamada con `dry_run=false` para escribirlos.
5. Instala las dependencias de la aplicación generada en el entorno propio del
   proyecto destino y ejecuta allí sus migraciones o creación de tablas y sus
   pruebas.

Herramientas:

- `list_entities(project_root)`: descubre las entidades en
  `domain/entities/*.py` e informa el estado del scaffolding.
- `generate_controller(entity_json, project_root, resource_name="", overwrite=False, dry_run=False)`:
  genera el slice de una entidad existente (pasando su nombre) o desde un JSON
  inline. Con `overwrite=true`, reemplaza los archivos generados existentes y
  conserva copias `.bak`; la entidad de dominio nunca se sobrescribe.
- `generate_pending(project_root, overwrite=False, dry_run=False)`: genera los
  slices que faltan para las entidades descubiertas.
- `reflect_database(database_url)`: refleja tablas de una base de datos. Requiere
  instalar el extra `database` y el driver apropiado para el motor.

`project_root` debe ser una ruta absoluta. El servidor MCP y la aplicación
destino pueden usar entornos Python diferentes: las dependencias necesarias
para ejecutar el scaffolding generado deben estar instaladas en el entorno de
la aplicación.

## Definición inline de entidad

```json
{
  "name": "Member",
  "resource_name": "Miembro",
  "fields": [
    {"name": "username", "type": "str", "min_length": 1, "max_length": 200},
    {"name": "phone", "type": "str", "required": false, "max_length": 30},
    {"name": "role_ids", "type": "relation", "related_entity": "Role"}
  ]
}
```

## Relaciones entre entidades

En las entidades Python, las relaciones se declaran mediante metadata. Los
destinos se infieren de nombres como `category_id` y `product_ids`; también se
puede indicar explícitamente con `metadata={"entity": "Category"}`.

```python
from dataclasses import dataclass, field
from uuid import UUID

@dataclass
class Product:
    role_ids: list[UUID] = field(default_factory=list)
    category_id: UUID = field(metadata={"relation": "many_to_one"})
    sku_id: UUID | None = field(
        default=None, metadata={"relation": "one_to_one"}
    )

@dataclass
class Category:
    product_ids: list[UUID] = field(
        default_factory=list,
        metadata={"relation": "one_to_many", "mapped_by": "category_id"},
    )
```

Tipos admitidos:

- `many_to_many`: `list[UUID]`; asociación mantenida desde esta entidad.
- `many_to_one`: FK en esta entidad, por ejemplo `category_id`.
- `one_to_many`: relación inversa de solo lectura; `mapped_by` identifica el
  campo FK del lado dueño.
- `one_to_one`: FK única en el lado dueño. El lado inverso también puede
  declararse con `mapped_by`.

Las relaciones que escriben una FK o asociación (`many_to_many`,
`many_to_one` y el lado dueño de `one_to_one`) requieren que el ORM del destino
ya exista. Los lados inversos pueden generarse antes; el servidor advertirá si
aún falta el ORM relacionado.
