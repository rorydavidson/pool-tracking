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


class PoolType(str, enum.Enum):
    in_ground = "in_ground"
    above_ground = "above_ground"


class PoolShape(str, enum.Enum):
    rectangle = "rectangle"
    oval = "oval"
    round = "round"
    kidney = "kidney"
    other = "other"


def estimate_volume_litres(
    shape: "PoolShape | str | None",
    length_m: float | None,
    width_m: float | None,
    avg_depth_m: float | None,
) -> float | None:
    """Estimate water volume (litres) from shape + dimensions, or None.

    Metric: lengths in metres. ``round`` uses ``length_m`` as the diameter.
    """
    import math

    if not avg_depth_m or avg_depth_m <= 0:
        return None
    shape_val = shape.value if isinstance(shape, PoolShape) else (shape or "")

    if shape_val == "round":
        if not length_m or length_m <= 0:
            return None
        area = math.pi * (length_m / 2) ** 2
    else:
        if not length_m or not width_m or length_m <= 0 or width_m <= 0:
            return None
        if shape_val == "oval":
            area = math.pi / 4 * length_m * width_m
        elif shape_val == "kidney":
            area = 0.85 * length_m * width_m  # rough kidney/freeform factor
        else:  # rectangle / other
            area = length_m * width_m

    return round(area * avg_depth_m * 1000, 0)


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

    # Physical spec, used to confirm the water volume and aid analysis.
    pool_type: Mapped[PoolType | None] = mapped_column(Enum(PoolType))
    shape: Mapped[PoolShape | None] = mapped_column(Enum(PoolShape))
    length_m: Mapped[float | None] = mapped_column(Float)
    width_m: Mapped[float | None] = mapped_column(Float)
    avg_depth_m: Mapped[float | None] = mapped_column(Float)

    # Location, used to correlate readings with historical weather.
    location_name: Mapped[str | None] = mapped_column(String(160))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    # IANA timezone (e.g. "Europe/London") derived from the location, for
    # displaying reading times in the pool's local time.
    timezone: Mapped[str | None] = mapped_column(String(64))
    # Filename (under the uploads dir) of an optional cover photo.
    image_path: Mapped[str | None] = mapped_column(String(255))
    # Free-text context the owner can add (e.g. "recently shocked", "near oak
    # trees"). Passed to the advice generator as extra context.
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    owner: Mapped["User"] = relationship(back_populates="pools")
    readings: Mapped[list["Reading"]] = relationship(
        back_populates="pool", cascade="all, delete-orphan", order_by="Reading.taken_at.desc()"
    )
    advice: Mapped["PoolAdvice | None"] = relationship(
        back_populates="pool", cascade="all, delete-orphan", uselist=False
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
    ec: Mapped[float | None] = mapped_column(Float)  # µS/cm (electrical conductivity)
    tds: Mapped[float | None] = mapped_column(Float)  # ppm
    temperature_c: Mapped[float | None] = mapped_column(Float)

    # The device serial/UUID this reading came from, if any.
    external_id: Mapped[str | None] = mapped_column(String(128))
    # Filename (under the uploads dir) of a test-strip photo, if one was used.
    image_path: Mapped[str | None] = mapped_column(String(255))

    pool: Mapped["Pool"] = relationship(back_populates="readings")

    __table_args__ = (
        # Avoid storing the same device measurement twice on sync.
        UniqueConstraint("pool_id", "source", "external_id", "taken_at", name="uq_reading_dedupe"),
    )


class PoolAdvice(Base):
    """The most recently generated advice for a pool.

    Advice is expensive to generate (a Claude call), so we persist it and only
    regenerate on an explicit trigger: a new reading, or the owner pressing
    "Refresh". Page views read this row rather than calling the API.

    The full assessment (summary, source, recommendations) is stored as a JSON
    blob in :attr:`payload`; see ``advice.serialise_assessment``.
    """

    __tablename__ = "pool_advice"

    id: Mapped[int] = mapped_column(primary_key=True)
    pool_id: Mapped[int] = mapped_column(
        ForeignKey("pools.id", ondelete="CASCADE"), unique=True, index=True
    )
    # The reading this advice assessed, for "based on your test from ..." context.
    reading_id: Mapped[int | None] = mapped_column(
        ForeignKey("readings.id", ondelete="SET NULL")
    )
    payload: Mapped[str] = mapped_column(Text, nullable=False)  # serialised Assessment
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    pool: Mapped["Pool"] = relationship(back_populates="advice")


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
