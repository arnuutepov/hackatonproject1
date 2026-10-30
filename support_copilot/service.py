"""Single shared entry point. No classification, retrieval or LLM in Wave 0."""
from support_copilot.schemas import DraftReply, Extracted, TriageRequest, TriageResponse


def triage(request: TriageRequest) -> TriageResponse:
    # Unknown values are explicit; attachments are not inspected evidence.
    return TriageResponse(
        lang="und",
        category="unclassified",
        priority="p2",
        is_spam_or_duplicate=False,
        extracted=Extracted(),
        draft_reply=DraftReply(
            lang="und",
            text="Обращение требует проверки администратором. Автоматический черновик пока недоступен.",
        ),
        needs_human=True,
        confidence=0.0,
        reasons=["wave_0_stub: язык, категория, приоритет, спам и факты ещё не определяются"],
    )
