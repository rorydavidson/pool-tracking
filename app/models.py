"""SQLAlchemy ORM models."""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SanitizerType(str, enum.Enum):
    chlorine = "chlorine"
    saltwater = "saltwater"
    bromine = "bromine"


class SurfaceType(str, enum.Enum):
    plaster = "plaster"
    vinyl = "vinyl"
    fibreglass = "fibreglass"
    tile = "tile"


class Provider(str, enum.Enum):
    aiper = "aiper"
    blueriiot = "blueriiot"


class ReadingSource(str, enum.Enum):
    manual = "manual"
    aiper = "aiper"
    blueriiot = "blueriiot"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    pools: Mapped[list["Pool"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    credentials: Mapped[list["ProviderCredential"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class MagicToken(Base):
    """A single-use login token. We store only a hash of the token value."""

    __tablename__ = "magic_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Pool(Base):
    __tablename__ = "pools"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    volume_litres: Mapped[float] = mapped_column(Float, nullable=False)
    sanitizer: Mapped[SanitizerType] = mapped_column(
        Enum(SanitizerType), default=SanitizerType.chlorine
    )
    surface: Mapped[SurfaceType] = mapped_column(Enum(SurfaceType), default=SurfaceType.plaster)
    indoor: Mapped[bool] = mapped_column(default=False)
    # Location, used to correlate readings with historical weather.
    location_name: Mapped[str | None] = mapped_column(String(160))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    owner: Mapped["User"] = relationship(back_populates="pools")
    readings: Mapped[list["Reading"]] = relationship(
        back_populates="pool", cascade="all, delete-orphan", order_by="Reading.taken_at.desc()"
    )


class Reading(Base):
    """A set of water measurements taken at a point in time.

    All chemical fields are optional — different devices and manual tests
    report different subsets.
    """

    __tablename__ = "readings"

    id: Mapped[int] = mapped_column(primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("pools.id", ondelete="CASCADE"), index=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    source: Mapped[ReadingSource] = mapped_column(Enum(ReadingSource), default=ReadingSource.manual)

    ph: Mapped[float | None] = mapped_column(Float)
    free_chlorine: Mapped[float | None] = mapped_column(Float)  # ppm
    total_chlorine: Mapped[float | None] = mapped_column(Float)  # ppm
    total_alkalinity: Mapped[float | None] = mapped_column(Float)  # ppm
    cyanuric_acid: Mapped[float | None] = mapped_column(Float)  # ppm (stabiliser)
    calcium_hardness: Mapped[float | None] = mapped_column(Float)  # ppm
    salt: Mapped[float | None] = mapped_column(Float)  # ppm
    orp: Mapped[float | None] = mapped_column(Float)  # mV
    tds: Mapped[float | None] = mapped_column(Float)  # ppm
    temperature_c: Mapped[float | None] = mapped_column(Float)

    # The device serial/UUID this reading came from, if any.
    external_id: Mapped[str | None] = mapped_column(String(128))

    pool: Mapped["Pool"] = relationship(back_populates="readings")

    __table_args__ = (
        # Avoid storing the same device measurement twice on sync.
        UniqueConstraint("pool_id", "source", "external_id", "taken_at", name="uq_reading_dedupe"),
    )


class ProviderCredential(Base):
    """Encrypted login credentials for a third-party device cloud."""

    __tablename__ = "provider_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[Provider] = mapped_column(Enum(Provider), nullable=False)
    # Fernet-encrypted JSON blob holding the username/password etc.
    secret_blob: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_error: Mapped[str | None] = mapped_column(Text)

    user: Mapped["User"] = relationship(back_populates="credentials")

    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_user_provider"),)


class WeatherDay(Base):
    """Cached historical daily weather for a location, to avoid re-fetching.

    Keyed by location rounded to ~1 km and the calendar date. Populated lazily
    from Open-Meteo when a pool's reading history is viewed.
    """

    __tablename__ = "weather_days"

    id: Mapped[int] = mapped_column(primary_key=True)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    date: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD

    temp_max_c: Mapped[float | None] = mapped_column(Float)
    temp_min_c: Mapped[float | None] = mapped_column(Float)
    precipitation_mm: Mapped[float | None] = mapped_column(Float)
    uv_index_max: Mapped[float | None] = mapped_column(Float)
    wind_max_kmh: Mapped[float | None] = mapped_column(Float)
    weather_code: Mapped[int | None] = mapped_column(Integer)

    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("latitude", "longitude", "date", name="uq_weather_loc_date"),
    )
