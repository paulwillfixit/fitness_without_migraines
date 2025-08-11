import os
import enum
from datetime import datetime, date
from typing import Optional
from sqlmodel import SQLModel, Field, create_engine, Session
from sqlalchemy import Column
from sqlalchemy.types import JSON as SAJSON

DB_URL = os.getenv("DB_URL", "sqlite:///data/app.db")
engine = create_engine(DB_URL, connect_args={"check_same_thread": False})

# --- Enums ---
class Direction(enum.Enum):
    IN = "in"
    OUT = "out"

class WorkoutKind(enum.Enum):
    REST = "rest"
    Z1 = "z1"
    Z2 = "z2"
    STRENGTH = "strength"

class Source(enum.Enum):
    GARMIN = "garmin"
    STRAVA = "strava"

class Provider(enum.Enum):
    STRAVA = "strava"

# --- Tables ---
class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: Optional[str] = None
    ftp_w: Optional[int] = None
    preferences: Optional[dict] = Field(default=None, sa_column=Column(SAJSON))

class TelegramMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=datetime.utcnow, index=True)
    direction: Direction = Field(index=True)
    chat_id: str
    text: str

class MigraineDiary(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    the_day: date = Field(index=True)
    had_headache: bool
    intensity_0_10: Optional[int] = None
    meds: Optional[str] = None
    relief_pct: Optional[int] = None
    triggers: Optional[dict] = Field(default=None, sa_column=Column(SAJSON))
    notes: Optional[str] = None

class WorkoutPlan(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    the_day: date = Field(index=True)
    kind: WorkoutKind = Field(index=True)
    duration_min: int
    targets: Optional[dict] = Field(default=None, sa_column=Column(SAJSON))
    zwo_path: Optional[str] = None

class WorkoutFeedback(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    the_day: date = Field(index=True)
    rpe_0_10: Optional[int] = None
    symptoms: Optional[dict] = Field(default=None, sa_column=Column(SAJSON))

class MetricsCache(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    source: Source = Field(index=True)
    the_day: date = Field(index=True)
    payload: dict = Field(sa_column=Column(SAJSON))

class OAuthToken(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    provider: Provider = Field(unique=True, index=True)
    access_token: str
    refresh_token: str
    expires_at: int

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)

def get_session() -> Session:
    return Session(engine)
