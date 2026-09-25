```python
# The document skill has exactly one implementation: create_doc.py. This guide
# used to carry a second one — its own docx builder, writing to
# workspace/media/ and linking `download:media/<archivo>` — and the runner
# reaches it whenever the model invokes the skill as action JSON instead of the
# `exec` form SKILL.md documents. That copy never learned to file anything on
# the family share, so a document created down this path was missing from "Mis
# archivos" and its link died with the container: the whole change, silently
# undone, on a route nobody looks at.
#
# So it delegates. One builder, one place documents are filed, one link.
import json, os, sys

# Where SKILL.md tells the model to exec it from; overridable so this is not a
# hard-coded container layout.
_SKILL_DIR = os.environ.get("DOCUMENT_SKILL_DIR", "/app/nanobot/skills/document")
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)
import create_doc


def create(filename=None, content=None, data=None, **kw):
    """Build a document. Accepts the shapes this guide has used over time.

    `create_doc.create` wants one dict holding `format`, `filename`, `title`
    and `sections`; earlier revisions of this guide took `(filename, content)`
    with the title and sections inside `content`. Both arrive, so both are
    flattened here rather than being turned away.
    """
    payload = dict(data or {})
    if isinstance(content, dict):
        payload.update(content)
    elif content is not None:
        payload.setdefault("sections", content)
    payload.update({k: v for k, v in kw.items() if v is not None})
    if filename:
        payload["filename"] = filename
    return create_doc.create(payload)


# The names the model reaches for; all one builder.
build = create
create_document = create
```

Call `create(filename, content)` where `content` has `title` and `sections` list.
Print result with `print(json.dumps(result, indent=2))`.
