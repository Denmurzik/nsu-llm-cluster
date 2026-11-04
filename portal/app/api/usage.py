from datetime import UTC, datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException

from app.api.keys import gateway_down, gateway_error
from app.auth.sessions import current_user
from app.deps import get_litellm
from app.litellm_client import LiteLLMClient, LiteLLMError, LiteLLMUnavailable
from app.models import User

router = APIRouter()

ALLOWED_DAYS = (1, 7, 30)


@router.get("/api/usage")
async def usage(
    days: int = 7,
    user: User = Depends(current_user),
    litellm: LiteLLMClient = Depends(get_litellm),
) -> dict:
    if days not in ALLOWED_DAYS:
        raise HTTPException(status_code=422, detail="Период: 1, 7 или 30 дней")
    end = datetime.now(UTC).date()
    start = end - timedelta(days=days - 1)
    try:
        activity = await litellm.user_activity(str(user.id), start, end)
    except LiteLLMUnavailable:
        raise gateway_down() from None
    except LiteLLMError:
        raise gateway_error() from None
    per_day = {d.day: d for d in activity.by_day}
    by_day = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        entry = per_day.get(day)
        by_day.append(
            {"day": day.isoformat(), "tokens": entry.tokens if entry else 0, "requests": entry.requests if entry else 0}
        )
    return {
        "days": days,
        "prompt_tokens": activity.prompt_tokens,
        "completion_tokens": activity.completion_tokens,
        "requests": activity.requests,
        "by_day": by_day,
    }
