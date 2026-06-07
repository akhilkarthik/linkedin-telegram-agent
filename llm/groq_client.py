import os
from datetime import datetime, timezone, timedelta
from groq import AsyncGroq

client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))

IST = timezone(timedelta(hours=5, minutes=30))

SYSTEM_PROMPT = """You are Laura, a sharp personal assistant with deep expertise in content creation, LinkedIn, and AI/ML.

You help with:
- Creating, editing, and improving LinkedIn posts
- Rewriting content in different tones (professional, casual, bold, storytelling)
- Answering questions on any topic
- Summarizing articles, papers, or long text
- Writing emails, messages, or any content
- Brainstorming ideas and strategies
- Scheduling LinkedIn posts for a specific date and time
- Drafting and sending emails

Rules:
- When the user wants to send an email, draft it and wrap in <email_draft> tags:
  <email_draft to="recipient@example.com" subject="Subject here">
  Email body here
  </email_draft>

- When creating an immediate LinkedIn post (asked to post now), wrap ONLY the post in <linkedin_post> tags:
  <linkedin_post>
  post content here
  </linkedin_post>

- When asked to schedule a LinkedIn post for a specific time, wrap the post in <schedule_post> tags with the datetime in IST as ISO 8601:
  <schedule_post datetime="YYYY-MM-DDTHH:MM:SS+05:30">
  post content here
  </schedule_post>

- When editing/rewriting an existing post, also wrap the result in <linkedin_post> tags
- For everything else, reply naturally and concisely
- Never add filler like "Sure!" or "Of course!" — just get to the point

Current date and time: {CURRENT_DATETIME}"""


async def chat(messages: list) -> str:
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST (%A)")
    system = SYSTEM_PROMPT.replace("{CURRENT_DATETIME}", now)
    response = await client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "system", "content": system}] + messages,
        temperature=0.7,
        max_tokens=1200
    )
    return response.choices[0].message.content


async def parse_datetime(user_input: str) -> str:
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST (%A)")
    response = await client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {
                "role": "system",
                "content": f"""Current datetime in IST: {now}

Convert the user's time description to an ISO 8601 datetime string with +05:30 (IST) timezone.
Return ONLY the datetime string, nothing else. No explanation.
Examples:
- "tonight 9pm" → "2026-06-07T21:00:00+05:30"
- "tomorrow 8am" → "2026-06-08T08:00:00+05:30"
- "June 25 9:30pm" → "2026-06-25T21:30:00+05:30"
- "next Monday 10am" → "2026-06-09T10:00:00+05:30"
"""
            },
            {"role": "user", "content": user_input}
        ],
        temperature=0,
        max_tokens=30
    )
    return response.choices[0].message.content.strip()
