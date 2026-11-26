"""Tasks and response schemas for optional specialist reviewers."""

from collections.abc import Callable
from dataclasses import dataclass
import re

from .model import Proposal, ReviewRole, StrictSchema
from .prompts import SYSTEM
from .segment import Block


class SpecialistResponse(StrictSchema):
    findings: list[Proposal]


@dataclass(frozen=True, slots=True)
class Specialist:
    task: str
    schema: type[SpecialistResponse]
    applies: Callable[[tuple[Block, ...]], bool] = bool


_HTTP_METHOD = r"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)"
_ENDPOINT = re.compile(
    rf"\b{_HTTP_METHOD}\s+"
    r"(?:\x60|[«\"])?(?:https?://[^\s/]+)?/[^\s/]",
    re.IGNORECASE,
)
_CONTRACT = re.compile(
    r"\b(?:openapi|swagger)\s*[:=]|\b(?:request body|response codes?|"
    r"параметр\w*\s+запроса|код\w*\s+ответа|HTTP\s+[1-5]\d\d)\b|"
    rf"\bREST type\s*[*_\x60]*\s*\|\s*{_HTTP_METHOD}\b",
    re.IGNORECASE,
)


def _has_api_contract(blocks: tuple[Block, ...]) -> bool:
    return any(
        block.kind != "governance" and (
            _ENDPOINT.search(block.text)
            or (block.kind == "api" and _CONTRACT.search(f"{block.section}\n{block.text}"))
        )
        for block in blocks
    )


SPECIALISTS: dict[ReviewRole, Specialist] = {
    "qa": Specialist(
        SYSTEM + """

Роль QA. Ищи только конкретные сценарии, в которых тестировщик не может
однозначно записать наблюдаемый ожидаемый результат из доступных требований.
В scenario задай исходные условия и действие. В description назови результаты,
между которыми нельзя выбрать, либо конкретный результат, которого не хватает.
Если результат однозначен, общие замечания к качеству формулировки пропускай.
Учитывай границы порогов, повторные действия, исключения и переходы состояний.
При передаче части документа отмечай только пробелы, видимые явно в этой части.
""",
        SpecialistResponse,
    ),
    "api": Specialist(
        SYSTEM + """

Роль интегратора. Ищи только неоднозначные или несовместимые ожидания в явно
описанном контракте API. Укажи метод или операцию, условие и последствие для
клиента. Проверь поля и типы, коды и тела ответов, ошибки, повторные запросы,
идемпотентность и совместимость версий, если они относятся к этому контракту.
Не изобретай конечную точку и не добавляй отсутствующее бизнес-правило.
При передаче части документа отмечай только проблемы, видимые явно в этой части.
""",
        SpecialistResponse,
        _has_api_contract,
    ),
}
