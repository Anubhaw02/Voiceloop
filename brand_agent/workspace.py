from datetime import datetime, time, timedelta


ACTIONABLE = ["pending_approval", "approved", "publish_failed"]
PILLAR_LABELS = {
    "cloud_for_ai": "Cloud infrastructure for AI",
    "ai_for_cloud": "AI for cloud operations",
    "ai_in_industry": "Real-world industry applications",
    "long_term_frontier": "Frontier technologies",
}
PILLAR_ANGLES = {
    "cloud_for_ai": ("Technical founders and platform builders", "What would you measure before recommending this architecture?"),
    "ai_for_cloud": ("Cloud operators and engineering leads", "What would you keep under human control in production?"),
    "ai_in_industry": ("Potential clients and product founders", "Which real workflow could benefit, and what adoption hurdle would you test first?"),
    "long_term_frontier": ("Technical collaborators and curious builders", "What would have to be proven before this moves from research to a real product?"),
}


def platform_label(platform: str) -> str:
    return {"linkedin": "LinkedIn", "x": "X"}.get(platform, platform)


def pillar_label(topic: str) -> str:
    return PILLAR_LABELS.get(topic, topic.replace("_", " ").capitalize())


def draft_angle(topic: str) -> tuple[str, str]:
    return PILLAR_ANGLES.get(
        topic, ("Technical founders and collaborators", "What is one limitation or trade-off you would explain in your own words?"),
    )


def next_scheduled_slot(now: datetime, times: list[str]) -> datetime | None:
    slots = sorted(time.fromisoformat(value) for value in times)
    for day in (now.date(), now.date() + timedelta(days=1)):
        for slot in slots:
            candidate = datetime.combine(day, slot, tzinfo=now.tzinfo)
            if candidate > now:
                return candidate
    return None


def week_start(now: datetime) -> datetime:
    return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
