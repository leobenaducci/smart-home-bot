# What Paperless's AI would suggest for every document, without applying any
# of it. Run inside the paperless container, through Django's shell:
#
#   docker cp services/home-paperless/tools/ai_dryrun.py paperless:/tmp/
#   docker exec -d paperless sh -c 'cd /usr/src/paperless/src && \
#       python3 manage.py shell < /tmp/ai_dryrun.py > /tmp/ai-dryrun.log 2>&1'
#
# One JSON line per document in /tmp/ai-dryrun.jsonl, then {"done": true}.
# DOC_IDS="37 41" limits it to those documents.
#
# It calls what the "Apply AI suggestions" workflow action calls --
# get_ai_document_classification with the owner's output language, then the
# same name matching -- and writes nothing to the database. ~30 s a document on
# gemma4:e4b, two model calls each when an output language is set.
#
# Per document it records the current metadata, the raw suggestion, the ids
# the model chose from the index (`existing`, empty without an embedding
# model), and what the suggested names would match among the labels the
# document's owner can see (`match`) -- the last is what the workflow applies.
import json
import os
import time

from documents.models import Document
from documents.workflows.ai import resolve_date
from paperless.config import AIConfig
from paperless_ai.ai_classifier import get_ai_document_classification
from paperless_ai.ai_classifier import get_llm_output_language
from paperless_ai.matching import match_correspondents_by_name
from paperless_ai.matching import match_document_types_by_name
from paperless_ai.matching import match_tags_by_name
from paperless_ai.matching import resolve_correspondent_ids
from paperless_ai.matching import resolve_document_type_ids
from paperless_ai.matching import resolve_tag_ids

cfg = AIConfig()
docs = Document.objects.order_by("id")
if os.environ.get("DOC_IDS"):
    docs = docs.filter(id__in=[int(i) for i in os.environ["DOC_IDS"].split()])


def names(choice):
    return list(choice.get("new_names") or [])


def ids(choice):
    return list(choice.get("existing_ids") or [])


with open("/tmp/ai-dryrun.jsonl", "w") as out:
    for d in docs:
        started = time.time()
        owner = d.owner
        rec = {
            "id": d.id,
            "title": d.title,
            "chars": len(d.content or ""),
            "tags": sorted(t.name for t in d.tags.all()),
            "type": d.document_type.name if d.document_type else None,
            "correspondent": d.correspondent.name if d.correspondent else None,
            "created": str(d.created),
        }
        try:
            s = get_ai_document_classification(d, owner, get_llm_output_language(cfg, owner))
            rec["ai"] = {
                "title": s["title"],
                "tags": names(s["tags"]),
                "correspondents": names(s["correspondents"]),
                "types": names(s["document_types"]),
                "dates": s["dates"],
            }
            rec["existing"] = {
                "tags": [t.name for t in resolve_tag_ids(ids(s["tags"]), owner)],
                "correspondents": [c.name for c in resolve_correspondent_ids(
                    ids(s["correspondents"]), owner)],
                "types": [t.name for t in resolve_document_type_ids(
                    ids(s["document_types"]), owner)],
            }
            rec["match"] = {
                "tags": [t.name for t in match_tags_by_name(rec["ai"]["tags"], owner)],
                "correspondents": [c.name for c in match_correspondents_by_name(
                    rec["ai"]["correspondents"], owner)],
                "types": [t.name for t in match_document_types_by_name(rec["ai"]["types"], owner)],
                "date": str(resolve_date(s["dates"])),
            }
        except Exception as exc:  # one bad document must not stop the rest
            rec["error"] = repr(exc)[:300]
        rec["secs"] = round(time.time() - started, 1)
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()
    out.write('{"done": true}\n')
