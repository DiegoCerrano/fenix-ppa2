# Decisiones de diseño

## D1 · Los clientes Boto3 viven en `fenix/config/aws.py`

**Fecha:** 2026-11-17 · **Afecta a:** Paso 5 (y a los Pasos 10, 11, 12, 14, 18 y 19, que usan `get_client`)

### Qué se cambió respecto de la guía

La guía pone `get_client` dentro de `config/settings.py`, con `import boto3`
adentro de la función (import perezoso). Pylint lo marca
(`C0415 import-outside-toplevel`) y PEP 8 pide los imports arriba.

Se separaron las responsabilidades:

| Módulo | Responsabilidad | Dependencias |
|---|---|---|
| `config/settings.py` | Leer y **validar** variables de entorno (`endpoint_url`, `aws_credentials`, `region`, `group_id`, …) | Solo biblioteca estándar |
| `config/aws.py` | **Construir** clientes Boto3 con esos valores (`get_client`) | `boto3`, `botocore` (imports arriba) |

- El chequeo de claves reales (AKIA/ASIA) pasó a `settings.aws_credentials()`:
  es validación de entorno, igual que `endpoint_url()`.
- `aws.get_client` rechaza servicios fuera de `SERVICES = ("sqs", "s3", "ec2")`,
  los mismos que levanta `compose.yaml`.
- Timeouts, reintentos, path-style de S3 y `load_env()` al importar `settings`
  quedan **igual que en la guía**.

### Por qué

1. **Una responsabilidad por módulo.** `settings` dice *qué* valores usar y si son
   seguros; `aws` dice *cómo* conectarse.
2. **Estilo sin excepciones.** Todos los imports arriba, sin `# pylint: disable`.
3. **Un único punto para parchear en los tests:** `fenix.config.aws.get_client`.
4. Módulos que no tocan AWS (`state`, `logging_json`, `events`, `crypto`,
   `notifications`) importan `settings` sin cargar boto3.

No se eligió por peso de import: `sqs.py` y `cloud_support.py` importan
`botocore` igual, y `boto3` es dependencia declarada en `pyproject.toml`.

### Qué hay que adaptar al copiar los pasos siguientes

La guía dice "copialos tal cual". En estos puntos **no**:

| Paso | Archivo | En la guía | Acá |
|---|---|---|---|
| 5 | prueba de la pág. 46 | `s.get_client('sqs')` | `from fenix.config import aws` → `aws.get_client('sqs')` |
| 10 | prueba de `cloud_support` | `settings.get_client('ec2'/'s3')` | `aws.get_client(...)` |
| 11 | `services/sqs.py` | `client or settings.get_client("sqs")` | `client or aws.get_client("sqs")` + `from fenix.config import aws` |
| 12 | `services/demo.py` | `settings.get_client("ec2"/"s3")` | `aws.get_client(...)` (+ import) |
| 14 | `services/remediation.py` | `settings.get_client("ec2"/"s3")` | `aws.get_client(...)` (+ import) |
| 18 | prueba en vivo | `settings.get_client('ec2')` | `aws.get_client('ec2')` |
| 19 | `tests/test_fenix.py` | `mock.patch("fenix.config.settings.get_client", ...)` | `mock.patch("fenix.config.aws.get_client", ...)` |

Los módulos siguen importando `settings` para todo lo demás (`queue_names`,
`data_dir`, `group_id`, `env`…). Solo cambia de dónde sale `get_client`.

Para controlar que no quedó ninguno:

```bash
grep -rn "settings.get_client" src tests scripts   # tiene que salir vacío
```

**Ojo con el test del Paso 19:** si queda el parche viejo, `mock.patch` falla con
`AttributeError: ... has no attribute 'get_client'`. Es ruidoso a propósito: no
puede pasar en silencio contra LocalStack real.

### Qué no se tocó y por qué

`scripts/check_stack.py` y `scripts/inspect_localstack.py` repiten la lógica del
cliente **a propósito**. Tienen que funcionar antes de que exista el paquete
(`check_stack` justamente verifica si `fenix` se puede importar). No importan
`fenix.config.aws`.

### Cómo se verificó

- `settings` se importa sin cargar boto3 (`'boto3' in sys.modules` → `False`).
- Prueba de la pág. 46 adaptada: endpoint no loopback → `ConfigError`; clave
  `AKIA…` → `ConfigError`; servicio desconocido → `ConfigError`.
- Para `sqs`, `s3` y `ec2`: endpoint `http://localhost:4566`, región
  `us-east-1`, read timeout 25/10/10, S3 path-style.
- `mock.patch("fenix.config.aws.get_client", ...)` reemplaza el cliente.
- Pendiente: correr `pylint src/fenix` en la Mac (no estaba instalado donde se probó).

### Declaración de uso de IA

- **Herramienta:** Claude (Anthropic).
- **Qué se usó:** revisión de `settings.py` y propuesta de esta separación.
- **Qué se modificó:** se creó `config/aws.py`; en `settings.py` se quitó
  `get_client` y se agregaron `aws_credentials()` y `region()`.
- **Cómo se verificó:** ver la sección anterior.
