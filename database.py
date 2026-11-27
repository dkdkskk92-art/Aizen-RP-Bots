from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from config import settings


def normalize_dsn(url: str) -> str:
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


engine = create_async_engine(normalize_dsn(settings.database_url), pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# Эта модель должна совпадать с users в основном боте Айзен.
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    first_name: Mapped[str] = mapped_column(String(128), default="")
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rank: Mapped[int] = mapped_column(Integer, default=0, index=True)
    registered_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    avatar_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_type: Mapped[str | None] = mapped_column(String(10), nullable=True)


class CommandAccess(Base):
    __tablename__ = "command_access"

    command_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    min_rank: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
