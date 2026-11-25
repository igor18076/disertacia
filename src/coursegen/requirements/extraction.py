import re
from collections.abc import Iterator
from enum import StrEnum
from uuid import UUID, uuid5


class Kind(StrEnum):
    discipline = "discipline"
    section = "section"
    topic = "topic"
    competency = "competency"
    outcome = "outcome"
    activity = "activity"
    content = "content"
    workload = "workload"
    practical = "practical"
    assessment = "assessment"
    other = "other"


LABELS = {
    Kind.discipline: r"(?:наименование\s+)?дисциплин(?:а|ы)",
    Kind.section: r"раздел(?:ы)?",
    Kind.topic: r"тем(?:а|ы)",
    Kind.competency: r"компетенци[яи]",
    Kind.outcome: r"результат(?:ы)? обучения|знать|уметь|владеть",
    Kind.activity: r"вид(?:ы)? (?:учебных )?занятий|вид занятия",
    Kind.content: r"требовани[ея] к содержанию|содержание",
    Kind.workload: r"(?:ограничени[ея] по )?объ[её]м[у]?|трудо[её]мкость",
    Kind.practical: r"практическ(?:ая работа|ие работы)|требования к практическим работам",
    Kind.assessment: r"критери[йи] оценивания",
}
PREFIX = re.compile(r"^(?:#{1,6}\s+|[-*•]\s+|\d+[.)]\s+)*")
LABEL = re.compile("^(?:" + "|".join(f"(?P<{k}>{v})" for k, v in LABELS.items())
                   + r")(?:\s*[:—–]\s*|\s+(?=\d)|$)(.*)$", re.I)
CODE = re.compile(r"^(?:ОПК|ПК|УК|ОК)-\d+\b", re.I)


def candidates(document_id: UUID, version: int, page: int, text: str) -> Iterator[dict]:
    offset = 0
    section = Kind.other
    for line in text.splitlines(keepends=True):
        quote = line.strip()
        start = offset + len(line) - len(line.lstrip())
        offset += len(line)
        if not quote:
            continue
        body = PREFIX.sub("", quote)
        match = LABEL.match(body)
        if match:
            kind = Kind(next(name for name, found in match.groupdict().items() if found is not None))
            value = match.groups()[-1].strip()
            if not value:
                section = kind
                continue
            section = Kind.other
        else:
            if body != quote and quote.startswith("#"):
                section = Kind.other
            kind = Kind.competency if CODE.match(body) else section
            value = body
        value = " ".join(value.split())
        if value:
            yield dict(requirement_id=uuid5(document_id, f"{version}:{page}:{start}:{quote}"),
                       document_id=document_id, document_version=version, page=page,
                       char_start=start, char_end=start + len(quote), quote=quote, kind=kind.value,
                       text=value, status="needs_review", revision=1)
