from itertools import batched

from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert

from coursegen.documents import storage as db
from coursegen.requirements.extraction import candidates


def extract_next(engine: Engine) -> bool:
    v = db.versions.c
    with engine.begin() as conn:
        row = conn.execute(select(v.document_id, v.document_version).where(
            v.status.in_(["ready", "partial"]), v.requirements_extracted.is_(False),
            v.source_meta["source_type"].astext.in_(["syllabus", "assessment"])
        ).order_by(v.created_at).limit(1).with_for_update(skip_locked=True)).first()
        if row is None:
            return False
        doc, number = row
        source = db.at_version(db.requirements, doc, number)
        stale = set(conn.scalars(select(db.requirements.c.requirement_id).where(source)))
        pages = select(db.pages.c.number, db.pages.c.text).where(db.at_version(db.pages, doc, number))
        for page, text in conn.execute(pages.order_by(db.pages.c.number)):
            for batch in batched(candidates(doc, number, page, text), 250):
                stale.difference_update(r["requirement_id"] for r in batch)
                conn.execute(insert(db.requirements).on_conflict_do_nothing(), list(batch))
        for batch in batched(stale, 250):
            conn.execute(db.requirements.delete().where(db.requirements.c.requirement_id.in_(batch)))
        conn.execute(db.versions.update().where(db.at_version(db.versions, doc, number))
                     .values(requirements_extracted=True))
        return True
