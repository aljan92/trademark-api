import os
from datetime import datetime
from sqlalchemy import create_engine, Column, String, Integer, Boolean, DateTime, ARRAY, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres_pass@localhost:5432/trademarks")

# Using connection pooling parameters for robustness
engine = create_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    pool_recycle=1800
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class USPTOTrademark(Base):
    __tablename__ = "uspto_trademarks"
    
    serial_number = Column(String(15), primary_key=True, index=True)
    word_mark = Column(String(255), index=True)
    registration_number = Column(String(15), nullable=True)
    registration_date = Column(DateTime, nullable=True)
    status = Column(String(50))
    owner = Column(String(255), nullable=True)
    nice_classes = Column(String(100), index=True)  # Comma-separated string (e.g. ,25,35,) for cross-DB compatibility (SQLite & PG)
    goods_services = Column(Text, nullable=True)
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class EUIPOCache(Base):
    __tablename__ = "euipo_cache"
    
    keyword = Column(String(255), primary_key=True, index=True)
    match_found = Column(Boolean)
    data = Column(Text)  # JSON-serialized string of matches
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class APIStats(Base):
    __tablename__ = "api_stats"
    
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    endpoint = Column(String(50))
    keyword = Column(String(255))
    cache_hit = Column(Boolean)
    timestamp = Column(DateTime, default=datetime.utcnow)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
