"""Database CRUD helpers for sessions and messages."""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Message, Session


async def get_or_create_session(db: AsyncSession, session_id: str | None) -> Session:
    """Return an existing session or create a new one."""
    if session_id:
        result = await db.execute(select(Session).where(Session.id == session_id))
        session = result.scalar_one_or_none()
        if session:
            return session

    # Create new session (use provided id or generate one)
    session = Session(id=session_id or str(uuid.uuid4()))
    db.add(session)
    await db.flush()
    return session


async def load_history(db: AsyncSession, session: Session) -> list[dict]:
    """Load messages for a session ordered by creation time."""
    result = await db.execute(
        select(Message)
        .where(Message.session_id == session.id)
        .order_by(Message.created_at)
    )
    return [{"role": m.role, "content": m.content} for m in result.scalars().all()]


async def save_turn(
    db: AsyncSession,
    session: Session,
    user_message: str,
    assistant_reply: str,
) -> None:
    """Persist a user + assistant message pair and commit."""
    db.add(Message(session_id=session.id, role="user", content=user_message))
    db.add(Message(session_id=session.id, role="assistant", content=assistant_reply))
    await db.commit()


async def delete_session(db: AsyncSession, session_id: str) -> bool:
    """Delete a session and all its messages. Returns True if it existed."""
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        return False
    await db.delete(session)
    await db.commit()
    return True
