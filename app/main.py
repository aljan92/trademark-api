import os
import secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Depends, HTTPException, Query, BackgroundTasks, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session
from app.database import engine, Base, get_db, USPTOTrademark, EUIPOCache, APIStats, SystemConfig
from app.uspto_importer import import_status, check_uspto_update, run_uspto_download
from app.scheduler import start_scheduler, trigger_manual_import

# Lifespan context manager to handle startup/shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the automated weekly scheduler
    start_scheduler()
    yield

# Initialize database tables automatically on startup
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Global Trademark Aggregation API",
    description="API and Admin Dashboard for Trademark Checking",
    version="1.0.0",
    lifespan=lifespan
)

security = HTTPBasic()

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin_pass")
USPTO_API_KEY = os.getenv("USPTO_API_KEY", "")

def get_current_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username.encode("utf8"), ADMIN_USERNAME.encode("utf8"))
    correct_password = secrets.compare_digest(credentials.password.encode("utf8"), ADMIN_PASSWORD.encode("utf8"))
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ungültiger Benutzername oder Passwort",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# Resolve templates directory relative to this file
current_dir = os.path.dirname(os.path.abspath(__file__))
templates_dir = os.path.join(current_dir, "templates")
if not os.path.exists(templates_dir):
    os.makedirs(templates_dir)

templates = Jinja2Templates(directory=templates_dir)

# Resolve static directory relative to this file
static_dir = os.path.join(current_dir, "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)

app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/admin/dashboard", response_class=HTMLResponse, dependencies=[Depends(get_current_admin)])
async def read_dashboard(request: Request, db: Session = Depends(get_db)):
    # Basic metrics
    uspto_count = db.query(USPTOTrademark).count()
    euipo_cache_count = db.query(EUIPOCache).count()
    
    # Query stats for dashboard charts (last 50 requests)
    stats = db.query(APIStats).order_by(APIStats.timestamp.desc()).limit(50).all()
    
    # Get last imported file
    last_imported_entry = db.query(SystemConfig).filter(SystemConfig.key == "last_imported_file").first()
    last_imported_val = last_imported_entry.value if last_imported_entry else "Nie"
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "uspto_count": uspto_count,
        "euipo_cache_count": euipo_cache_count,
        "stats": stats,
        "import_status": import_status,
        "last_imported_file": last_imported_val,
        "uspto_api_key_configured": bool(USPTO_API_KEY)
    })

@app.post("/admin/update/check", dependencies=[Depends(get_current_admin)])
async def trigger_update_check(db: Session = Depends(get_db)):
    if not USPTO_API_KEY:
        raise HTTPException(status_code=400, detail="USPTO API-Key nicht konfiguriert")
    check_uspto_update(USPTO_API_KEY, db)
    return {
        "update_available": import_status["update_available"],
        "available_file": import_status["available_file"],
        "api_key_configured": import_status["api_key_configured"]
    }

@app.post("/admin/download/trigger", dependencies=[Depends(get_current_admin)])
async def trigger_download(background_tasks: BackgroundTasks):
    if not USPTO_API_KEY:
        raise HTTPException(status_code=400, detail="USPTO API-Key nicht konfiguriert")
    if import_status["downloading"]:
        raise HTTPException(status_code=400, detail="Download läuft bereits")
    background_tasks.add_task(run_uspto_download, USPTO_API_KEY)
    return {"message": "Download im Hintergrund gestartet"}

@app.post("/admin/import/trigger", dependencies=[Depends(get_current_admin)])
async def trigger_import():
    if import_status["running"]:
        raise HTTPException(status_code=400, detail="Import läuft bereits")
    trigger_manual_import()
    return {"message": "Sync manuell im Hintergrund gestartet"}

@app.get("/admin/import/status", dependencies=[Depends(get_current_admin)])
async def get_import_status():
    return import_status


@app.get("/admin/playground", response_class=HTMLResponse, dependencies=[Depends(get_current_admin)])
async def read_playground(request: Request):
    return templates.TemplateResponse("playground.html", {
        "request": request
    })

import json
from datetime import datetime, timedelta
from sqlalchemy import func
from app.euipo_client import query_euipo_live

@app.get("/v1/check-trademark")
async def check_trademark(
    keyword: str = Query(..., description="The wordmark to search"),
    match_type: str = Query("exact", description="exact or phrase"),
    nice_class: int = Query(None, description="Optional Nice class filter"),
    office: str = Query("both", description="uspto, euipo, or both"),
    db: Session = Depends(get_db)
):
    office = office.lower()
    details = []
    cache_hit = False

    # 1. USPTO Search (Local Mirror)
    if office in ("both", "uspto"):
        uspto_query = db.query(USPTOTrademark)
        
        # Keyword filter
        if match_type == "exact":
            uspto_query = uspto_query.filter(func.lower(USPTOTrademark.word_mark) == keyword.lower())
        else:
            uspto_query = uspto_query.filter(USPTOTrademark.word_mark.ilike(f"%{keyword}%"))
            
        # Nice class filter
        if nice_class is not None:
            uspto_query = uspto_query.filter(USPTOTrademark.nice_classes.like(f"%,{nice_class},%"))
            
        uspto_results = uspto_query.limit(50).all() # Cap at 50 results
        
        for tm in uspto_results:
            # Parse comma-separated string back to list of ints
            nice_classes_list = []
            if tm.nice_classes:
                nice_classes_list = [int(c) for c in tm.nice_classes.split(",") if c.isdigit()]
                
            details.append({
                "office": "USPTO",
                "registry_status": tm.status,
                "registration_number": tm.registration_number or tm.serial_number,
                "registration_date": tm.registration_date.strftime("%Y-%m-%d") if tm.registration_date else None,
                "owner": tm.owner,
                "nice_classes": nice_classes_list,
                "goods_and_services_description": tm.goods_services
            })

    # 2. EUIPO Search (7-Day cache or live API call)
    if office in ("both", "euipo"):
        # Check cache
        cache_entry = db.query(EUIPOCache).filter(EUIPOCache.keyword == keyword.lower()).first()
        
        euipo_results = []
        if cache_entry and (datetime.utcnow() - cache_entry.last_updated) < timedelta(days=7):
            # Cache Hit!
            euipo_results = json.loads(cache_entry.data)
            cache_hit = True
        else:
            # Cache Miss / Expired -> Query Live API
            # Note: We query the live API without nice_class filter to cache all classes for this keyword
            try:
                euipo_results = query_euipo_live(keyword, match_type=match_type)
                
                # Update Cache
                if cache_entry:
                    cache_entry.data = json.dumps(euipo_results)
                    cache_entry.match_found = len(euipo_results) > 0
                    cache_entry.last_updated = datetime.utcnow()
                else:
                    new_cache = EUIPOCache(
                        keyword=keyword.lower(),
                        match_found=len(euipo_results) > 0,
                        data=json.dumps(euipo_results),
                        last_updated=datetime.utcnow()
                    )
                    db.add(new_cache)
                db.commit()
            except Exception as e:
                db.rollback()
                # If the API is completely down, use old cache as fallback if available
                if cache_entry:
                    euipo_results = json.loads(cache_entry.data)
                    cache_hit = True
                else:
                    raise e
                    
        # Filter EUIPO results by Nice class in Python if class is specified
        for tm in euipo_results:
            if nice_class is not None:
                if nice_class not in tm.get("nice_classes", []):
                    continue
            details.append(tm)

    # 3. Assemble Response
    active_details = [tm for tm in details if tm.get("registry_status") == "active"]
    match_found = len(active_details) > 0
    
    # 4. Log API Stats
    stat_entry = APIStats(
        endpoint="/v1/check-trademark",
        keyword=keyword,
        cache_hit=cache_hit
    )
    db.add(stat_entry)
    db.commit()

    return {
        "meta": {
            "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status_code": 200,
            "legal_disclaimer": "This API provides aggregated data from public registries 'as is' and does not constitute legal advice."
        },
        "query_parameters": {
            "keyword": keyword,
            "match_type": match_type,
            "nice_class": nice_class,
            "office_filter": office
        },
        "result_summary": {
            "match_found": match_found,
            "total_active_registrations": len(active_details)
        },
        "details": details
    }

