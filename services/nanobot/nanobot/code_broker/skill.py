"""The skill this service hands out for itself.

`agent/remote_skills.py` explains why it lives here rather than in the package:
a skill is a description of somebody else's HTTP API, and when the two are in
different repos behind different deploy jobs the description drifts. The broker
answering `GET /skill` means changing what Alfred is told about it is a deploy
of this one service.

Two things the instructions have to get across, because neither is guessable
from the verbs:

* **He cannot reach a remote himself.** `git clone` in a shell will fail, and
  it will fail in a way that looks like a network problem rather than like a
  rule. Saying so up front is cheaper than a task spent debugging DNS.
* **437k lines do not fit in his context.** The map matters more than the code:
  which repo owns what, and where each one keeps its own AGENTS.md.
"""

from __future__ import annotations

import hashlib

INSTRUCTIONS = """---
name: code
description: "Invoke with JSON: {\\"skill\\":\\"code\\",\\"action\\":\\"...\\"}. \
El código de los proyectos de la casa, en un workspace propio. Actions: \
list_projects() = qué proyectos puedes ver, con su host | checkout(project) = clonar o actualizar uno y decir dónde quedó | \
project_status(project) = rama, commit y qué hay sin guardar | branch(name, project=...) = abrir una rama y quedarse ahí | \
commit(message, project=...) = guardar lo hecho | push(project) = publicar la rama para que la revisen | \
pull(project) = traer lo nuevo del remoto. Úsala cuando te \
pidan mirar, entender, revisar o cambiar el código de algo de la casa."
---

# El código de la casa

Los proyectos viven en un registro que administra un admin en HomeWeb (nombre,
repo, host de despliegue, quién puede verlo). Tú no lo editas y no lo ves
entero: `list_projects` te dice lo que *tú* puedes ver.

## Cómo trabajas aquí

1. `list_projects()` para ver qué hay.
2. `checkout(project)` — clona la primera vez y actualiza después. Te devuelve
   la ruta: `/nanobot-code-workspace/<tu instancia>/<proyecto>`.
   El proyecto va por su *slug*: el id corto en minúsculas con el que está
   registrado (`home-brain`), no el nombre que se lee (`Home Brain`). Da igual
   si lo llamas `project` o `slug`, los dos se aceptan.
3. **Abrí la rama antes de tocar nada**, no antes del commit:
   `branch("arreglo-de-lo-que-sea", project=...)`. `commit` se niega en `main`,
   así que editar primero deja el trabajo varado y hay que moverlo después.
4. A partir de ahí es un directorio normal, y editás con las herramientas de
   archivos: `read_file` para leer, `edit_file` (con `old_text` / `new_text`)
   para cambiar una parte, `write_file` para un archivo entero, `grep` y `glob`
   para buscar. `exec` es para **correr los tests**, no para editar: un
   `sed -i` deja un archivo distinto sin decir qué cambió, y lo que revisás
   después —y firmás— es el diff.
5. `commit("qué hace el cambio", project=...)` cuando los tests pasen, con el
   proyecto nombrado igual que arriba.
6. `push(project)` para publicarla, y entonces cuéntale a quien te pidió el
   cambio qué rama es y qué dijeron los tests. Ahí termina tu parte.

El commit queda a nombre de quien te lo pidió, y tú como co-autor. No es
decoración: `git log` lo lee gente, y un id de login no le dice nada a nadie
un año después — pero que tú figures al lado es lo que hace que `git blame`
siga sirviendo de evidencia cuando la mitad del repo la escribiste tú.

## Lo que no puedes hacer, y por qué no es un error

**No puedes hablar con un remoto desde la shell.** `git clone`, `git fetch`,
`git push` escritos a mano van a fallar — no hay credenciales en este
contenedor, a propósito. Las llaves de cada proyecto viven en el broker, que es
lo único que habla con un remoto. Por eso `push` y `pull` son acciones de esta
skill y no comandos: pídelas y el broker las corre con la llave del proyecto,
que tú nunca ves.

`git` local sí es tuyo. `git status`, `git diff`, `git log`, `git show` en la
shell no tocan ningún remoto y no necesitan permiso — úsalos para mirar lo que
hiciste antes de `commit`. Pero para *guardar* usa `commit`, no `git commit`:
es el que conoce las rutas protegidas del proyecto y el que se niega en `main`.

**Tú no mergeas.** Nunca. Propones una rama, cuentas qué hace y qué dijeron
los tests, y alguien de la casa decide. No es una restricción temporal ni
depende del proyecto: no existe el camino "mergear solo". `commit` y `push` se
niegan en `main`, `master`, `trunk` y `develop` — no como castigo, sino porque
empujar a `main` *es* mergear, se llame como se llame al llegar. Si te lo
piden igual, explica eso en una línea y sigue con la propuesta.

**Y no despliegas.** Eso sigue en el plan (ALFRED_PROGRAMADOR.md) y no está
habilitado.

## El tamaño real de esto

Son cientos de miles de líneas repartidas en varios repos. **No las leas.**
Cada repo tiene su propio `AGENTS.md` en la raíz: léelo primero, es el mapa que
alguien ya escribió. Después `grep` y `glob` para llegar al archivo. Abrir
árboles enteros "para entender el contexto" gasta la ventana y no entiende
nada.

## Lo que pide cada proyecto

`checkout` te devuelve el proyecto entero, y ahí puede venir
`custom_instructions`: las reglas que la casa escribió **para ese repo**, en
español y en prosa — «trabaja solo dentro de services/», «no hagas commit si
los tests fallan», «los mensajes de commit van en inglés».

Léelas antes de tocar nada y respétalas como si te las hubiera dicho alguien
de la casa, porque es exactamente lo que pasó: se escriben en la página de
Proyectos y viven con el proyecto, no con la conversación. Valen para todo lo
que hagas en ese repo, aunque quien te pidió el cambio no las mencione.

Si lo que te piden choca con ellas, no elijas tú: decilo en una línea, contá
cuál es la regla y esperá. Y si una regla te resulta imposible de cumplir
—habla de una ruta que no existe, o de un comando que no tenés— decilo también
en vez de ignorarla en silencio.

`protected_paths` viene al lado y es otra cosa: no es un pedido sino un piso.
Son las rutas que esta casa no deja tocar en ningún proyecto —`users.json`,
`.env`, `*.pem`, las llaves— más las que ese repo agregó. No las edites.

## Antes de tocar algo

- Corre los tests que ya existen **antes** de cambiar nada, para saber qué
  estaba roto de antes.
- Un cambio sin su test es media respuesta en esta casa.
- Si `project_status` dice que hay cosas sin guardar, no son tuyas
  necesariamente: pregunta antes de sobrescribir.

## Ejemplos

{"skill": "code", "action": "list_projects"}

{"skill": "code", "action": "checkout", "project": "homecameras"}

{"skill": "code", "action": "project_status", "project": "homecameras"}

{"skill": "code", "action": "branch", "project": "homecameras", "name": "arreglo-de-la-grilla"}

{"skill": "code", "action": "commit", "project": "homecameras", "message": "Fix the camera grid on narrow phones"}

{"skill": "code", "action": "push", "project": "homecameras"}

{"skill": "code", "action": "pull", "project": "homecameras"}
"""

PYTHON_GUIDE = '''```python
import json, os, subprocess

# The broker is the only thing in this house that touches a git remote. This
# container has no project credentials on purpose — see ALFRED_PROGRAMADOR.md —
# so `git clone` here would fail for the wrong reason. The token is derived per
# instance: it acts as this user and cannot act as anybody else.
BROKER = os.environ.get("CODE_BROKER_URL", "http://code-broker:8910")
TOKEN = os.environ.get("CODE_BROKER_TOKEN", "")
INSTANCE = os.environ.get("NANOBOT_INSTANCE", "")


def _call(method, path, body=None, timeout=300):
    if not TOKEN:
        return {"error": "Esta instancia no tiene acceso al código (falta CODE_BROKER_TOKEN)."}
    cmd = ["curl", "-s", "--max-time", str(timeout), "-X", method,
           "-H", f"X-Code-User: {INSTANCE}", "-H", f"X-Code-Token: {TOKEN}"]
    if body is not None:
        # --data-binary @- rather than -d: a commit message can be long, can
        # contain anything, and must not arrive reshaped by a shell or by
        # curl's own @file handling.
        cmd += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
    cmd.append(f"{BROKER}{path}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 30,
                       input=json.dumps(body) if body is not None else None)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"error": (r.stdout or r.stderr).strip()[:400] or "el broker no respondió"}


def list_projects(**kw):
    """Los proyectos que esta persona puede ver, con su host."""
    return _call("GET", "/v1/projects")


def _slug(project=None, slug=None, **kw):
    """The project's short id, under either name.

    The HTTP route says `{slug}` and these wrappers said `project`, so half the
    system called it one thing and half the other — and an agent that guesses
    the wrong one lost a whole turn to a missing-argument error. Both are
    accepted; neither is a mistake worth a turn.
    """
    name = (slug or project or "").strip()
    if not name:
        raise TypeError("falta el proyecto: checkout('mi-proyecto')")
    return name


def checkout(project=None, slug=None, **kw):
    """Clonar (o actualizar) un proyecto. Devuelve la ruta en disco."""
    return _call("POST", f"/v1/projects/{_slug(project, slug)}/checkout")


def project_status(project=None, slug=None, **kw):
    """Rama, commit y archivos sin guardar."""
    return _call("GET", f"/v1/projects/{_slug(project, slug)}/status")


# `name` and `message` come first, and the project after them -- upstream's
# order, kept so this file does not diverge on every future merge. It is the
# opposite of the way the two read in prose, so the instructions above spell
# both calls out with the keyword rather than leaving an agent to infer the
# order from `branch(project, name)`: passing them positionally the other way
# round names the branch after the project and sends the branch name as the
# slug, and the broker answers 404 for a project nobody has. That is the same
# wasted turn `_slug` was added to prevent, so it is worth the extra word.
def branch(name, project=None, slug=None, **kw):
    """Crear una rama (o moverse a ella si ya existe) y quedarse ahí."""
    return _call("POST", f"/v1/projects/{_slug(project, slug)}/branch", {"name": name})


def commit(message, project=None, slug=None, **kw):
    """Guardar lo que cambiaste, con un mensaje que diga qué hace."""
    return _call("POST", f"/v1/projects/{_slug(project, slug)}/commit", {"message": message})


def push(project=None, slug=None, **kw):
    """Publicar la rama actual para que alguien la revise y la mergee."""
    return _call("POST", f"/v1/projects/{_slug(project, slug)}/push")


def pull(project=None, slug=None, **kw):
    """Traer lo nuevo del remoto a la rama actual, solo si avanza en línea recta."""
    return _call("POST", f"/v1/projects/{_slug(project, slug)}/pull")
```'''


def envelope() -> dict:
    """What `GET /skill` answers with — see agent/remote_skills.py.

    The version is a hash of the content, so an unchanged answer does not
    rewrite the instance's files every five minutes and churn their mtimes.
    """
    body = INSTRUCTIONS + PYTHON_GUIDE
    return {
        "name": "code",
        "version": hashlib.sha256(body.encode()).hexdigest()[:16],
        "mode": "replace",
        "instructions": INSTRUCTIONS,
        "python": PYTHON_GUIDE,
    }
