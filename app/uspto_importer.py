import os
import zipfile
import glob
import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from sqlalchemy.dialects.postgresql import insert
from app.database import USPTOTrademark, SessionLocal, SystemConfig

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uspto_importer")

SCRATCH_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scratch")

# Global import status to track progress from the dashboard
import_status = {
    "running": False,
    "progress": 0,
    "current_file": "",
    "total_records": 0,
    "last_run": None,
    "error": None,
    "logs": [],
    
    # Download state
    "downloading": False,
    "download_progress": 0,
    "download_file": "",
    "update_available": False,
    "available_file": "",
    "available_file_url": "",
    "api_key_configured": False
}

def log_progress(message: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_line = f"[{timestamp}] {message}"
    logger.info(message)
    import_status["logs"].append(log_line)
    # Keep last 50 lines of logs
    if len(import_status["logs"]) > 50:
        import_status["logs"].pop(0)

def extract_xml_text(element, paths):
    """Helper to extract text from XML elements using multiple fallback paths."""
    for path in paths:
        found = element.find(path)
        if found is not None and found.text:
            return found.text.strip()
    return None

def parse_nice_classes(element):
    """Extract Nice classification integers from an application element."""
    classes = set()
    # Check common WIPO/USPTO paths for class numbers
    class_paths = [
        ".//classifications/classification/classification-number",
        ".//ClassNumber",
        ".//NiceClassNumber",
        ".//Classification/ClassNumber"
    ]
    for path in class_paths:
        for el in element.findall(path):
            if el.text:
                try:
                    val = int(el.text.strip())
                    if 1 <= val <= 45:
                        classes.add(val)
                except ValueError:
                    pass
    return list(classes)

def parse_and_store_xml(xml_file_path, db):
    log_progress(f"Starte Parsing der XML-Datei: {os.path.basename(xml_file_path)}")
    
    # Check XML root to dynamically handle ST.66 vs ST.96 vs other formats
    try:
        # We parse elements iteratively using iterparse to avoid loading the whole 
        # file in memory (essential for large 1-2 GB XML files).
        # We look for common trademark record tags
        tags_to_find = {"trade-mark-application", "TradeMark", "trademark-application", "case-file"}
        
        context = ET.iterparse(xml_file_path, events=("end",))
        db_batch = []
        batch_size = 500
        processed_count = 0
        
        for event, elem in context:
            if elem.tag in tags_to_find:
                # 1. Serial Number (Primary Key)
                serial = extract_xml_text(elem, [
                    ".//serial-number", 
                    ".//ApplicationNumberText", 
                    "serial-number", 
                    "ApplicationNumberText"
                ])
                if not serial:
                    elem.clear()
                    continue
                
                # 2. Word Mark (The actual name)
                word_mark = extract_xml_text(elem, [
                    ".//word-mark", 
                    ".//MarkText", 
                    "word-mark", 
                    "MarkText"
                ])
                # Skip design-only marks or marks without text
                if not word_mark:
                    word_mark = "[Design-Only Mark]"
                
                # 3. Registration Number & Date
                reg_num = extract_xml_text(elem, [".//registration-number", ".//RegistrationNumberText"])
                reg_date_str = extract_xml_text(elem, [".//registration-date", ".//RegistrationDateText"])
                reg_date = None
                if reg_date_str:
                    try:
                        # Common date formats in USPTO: YYYYMMDD
                        reg_date = datetime.strptime(reg_date_str.replace("-", ""), "%Y%m%d")
                    except ValueError:
                        pass
                
                # 4. Status
                status = extract_xml_text(elem, [
                    ".//status-code", 
                    ".//MarkCurrentStatusCode", 
                    ".//status-code-type"
                ]) or "unknown"
                
                # Map codes to human readable statuses if needed
                if status in ("700", "710", "LIVE", "REGISTERED"):
                    status = "active"
                elif status in ("600", "602", "DEAD"):
                    status = "dead"
                
                # 5. Owner / Applicant
                owner = extract_xml_text(elem, [
                    ".//applicants/applicant/applicant-name",
                    ".//registrant/registrant-name",
                    ".//Applicant/PartyName/Name",
                    ".//Applicant/OrganizationName"
                ]) or "[Unbekannt]"
                
                # 6. Nice Classes
                nice_classes_list = parse_nice_classes(elem)
                nice_classes_str = "," + ",".join(str(c) for c in nice_classes_list) + "," if nice_classes_list else ","
                
                # 7. Goods and Services Description
                description = extract_xml_text(elem, [
                    ".//goods-services/goods-services-description",
                    ".//GoodsServices/DescriptionText",
                    ".//goods-services/description"
                ]) or ""
                
                # Map variables into a dictionary for upsert
                record = {
                    "serial_number": serial,
                    "word_mark": word_mark,
                    "registration_number": reg_num,
                    "registration_date": reg_date,
                    "status": status,
                    "owner": owner,
                    "nice_classes": nice_classes_str,
                    "goods_services": description[:1000] if description else "", # Cap description size
                    "last_updated": datetime.utcnow()
                }
                
                db_batch.append(record)
                processed_count += 1
                
                # Batch upsert to database using PostgreSQL ON CONFLICT DO UPDATE
                if len(db_batch) >= batch_size:
                    upsert_batch(db, db_batch)
                    db_batch = []
                    import_status["progress"] = min(95, int((processed_count / 100000) * 100)) # Placeholder progress scaling
                    if processed_count % 5000 == 0:
                        log_progress(f"{processed_count} Marken erfolgreich importiert...")
                
                # Clear element from memory after processing
                elem.clear()
        
        # Insert remaining items in batch
        if db_batch:
            upsert_batch(db, db_batch)
        
        import_status["total_records"] += processed_count
        log_progress(f"Parsing abgeschlossen. {processed_count} Marken aus {os.path.basename(xml_file_path)} importiert.")
        
    except Exception as e:
        logger.exception("Fehler beim XML-Parsing")
        log_progress(f"FEHLER beim Parsen von {os.path.basename(xml_file_path)}: {str(e)}")
        raise e

def upsert_batch(db, batch):
    """Helper to perform bulk upsert (handles both Postgres and SQLite dialects)."""
    if db.bind.dialect.name == "postgresql":
        stmt = insert(USPTOTrademark)
        
        # Define update mapping on conflict
        update_dict = {
            "word_mark": stmt.excluded.word_mark,
            "registration_number": stmt.excluded.registration_number,
            "registration_date": stmt.excluded.registration_date,
            "status": stmt.excluded.status,
            "owner": stmt.excluded.owner,
            "nice_classes": stmt.excluded.nice_classes,
            "goods_services": stmt.excluded.goods_services,
            "last_updated": stmt.excluded.last_updated
        }
        
        upsert_stmt = stmt.on_conflict_do_update(
            index_elements=["serial_number"],
            set_=update_dict
        )
        db.execute(upsert_stmt, batch)
        db.commit()
    else:
        # SQLite fallback: merge records individually (or in batch loop) for local testing
        for item in batch:
            existing = db.query(USPTOTrademark).filter(USPTOTrademark.serial_number == item["serial_number"]).first()
            if existing:
                for k, v in item.items():
                    setattr(existing, k, v)
            else:
                db.add(USPTOTrademark(**item))
        db.commit()

def run_uspto_import():
    """Main function executed by the background scheduler."""
    if import_status["running"]:
        log_progress("Import läuft bereits. Abgebrochen.")
        return
        
    import_status["running"] = True
    import_status["progress"] = 0
    import_status["error"] = None
    import_status["logs"] = []
    
    db = SessionLocal()
    
    try:
        if not os.path.exists(SCRATCH_DIR):
            os.makedirs(SCRATCH_DIR)
            
        log_progress(f"Suche nach USPTO XML/ZIP Dateien in: {SCRATCH_DIR}")
        
        # 1. Look for zip files first and unzip them
        zip_files = glob.glob(os.path.join(SCRATCH_DIR, "*.zip"))
        original_zips = [os.path.basename(zf) for zf in zip_files]
        for zip_file in zip_files:
            log_progress(f"Entpacke ZIP-Archiv: {os.path.basename(zip_file)}")
            with zipfile.ZipFile(zip_file, 'r') as zip_ref:
                zip_ref.extractall(SCRATCH_DIR)
            # Remove zip file after extraction to free up space
            os.remove(zip_file)
            log_progress(f"ZIP-Archiv {os.path.basename(zip_file)} gelöscht.")
            
        # 2. Find XML files
        xml_files = glob.glob(os.path.join(SCRATCH_DIR, "*.xml"))
        if not xml_files:
            log_progress("Keine XML-Dateien im scratch/ Ordner gefunden.")
            log_progress("Bitte lege deine USPTO .xml oder .zip Bulk-Dateien im Projekt-Ordner 'scratch' ab und starte den Sync erneut.")
            import_status["progress"] = 100
            import_status["running"] = False
            return
            
        import_status["progress"] = 10
        
        for xml_file in xml_files:
            import_status["current_file"] = os.path.basename(xml_file)
            parse_and_store_xml(xml_file, db)
            
            # Clean up XML file after parsing to save server space
            os.remove(xml_file)
            log_progress(f"XML-Datei {os.path.basename(xml_file)} gelöscht.")
            
        # Update last_imported_file in SystemConfig
        last_file_val = ""
        if import_status["download_file"]:
            last_file_val = import_status["download_file"]
        elif original_zips:
            last_file_val = original_zips[-1]
        elif xml_files:
            last_file_val = os.path.basename(xml_files[-1])
            
        if last_file_val:
            config_entry = db.query(SystemConfig).filter(SystemConfig.key == "last_imported_file").first()
            if not config_entry:
                config_entry = SystemConfig(key="last_imported_file", value=last_file_val)
                db.add(config_entry)
            else:
                config_entry.value = last_file_val
            db.commit()
            log_progress(f"SystemConfig aktualisiert: last_imported_file = {last_file_val}")
            
        import_status["progress"] = 100
        import_status["last_run"] = datetime.now()
        log_progress("USPTO Synchronisierung erfolgreich abgeschlossen!")
        
    except Exception as e:
        import_status["error"] = str(e)
        log_progress(f"Synchronisierung abgebrochen wegen Fehler: {str(e)}")
    finally:
        db.close()
        import_status["running"] = False


def check_uspto_update(api_key: str, db):
    """Check if a newer bulk file is available on the USPTO Open Data Portal."""
    if not api_key:
        import_status["api_key_configured"] = False
        import_status["update_available"] = False
        return
        
    import_status["api_key_configured"] = True
    try:
        url = "https://api.uspto.gov/api/v1/datasets/products/TRTDXFAP"
        headers = {"X-Api-Key": api_key}
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code in (401, 403):
            import_status["api_key_configured"] = False
            import_status["update_available"] = False
            logger.warning("USPTO API-Key ist ungültig (401/403)")
            return
            
        response.raise_for_status()
        data = response.json()
        product_bag = data.get("bulkDataProductBag", [])
        if not product_bag:
            logger.warning("Kein bulkDataProductBag in der USPTO API-Antwort gefunden")
            return
            
        file_bag = product_bag[0].get("productFileBag", [])
        if not file_bag:
            logger.warning("Kein productFileBag in der USPTO API-Antwort gefunden")
            return
            
        valid_files = [f for f in file_bag if f.get("fileName", "").endswith((".zip", ".xml"))]
        if not valid_files:
            return
            
        # Sort alphabetically to get latest daily file
        sorted_files = sorted(valid_files, key=lambda x: x.get("fileName", ""))
        latest_file = sorted_files[-1]
        
        latest_filename = latest_file.get("fileName")
        latest_file_url = latest_file.get("downloadUrl")
        
        import_status["available_file"] = latest_filename
        import_status["available_file_url"] = latest_file_url
        
        last_imported = db.query(SystemConfig).filter(SystemConfig.key == "last_imported_file").first()
        last_imported_val = last_imported.value if last_imported else ""
        
        latest_base = os.path.splitext(latest_filename)[0]
        last_base = os.path.splitext(last_imported_val)[0] if last_imported_val else ""
        
        if latest_base != last_base:
            import_status["update_available"] = True
        else:
            import_status["update_available"] = False
            
    except Exception as e:
        logger.exception("Fehler beim Prüfen auf USPTO-Updates")


def run_uspto_download(api_key: str):
    """Download the latest USPTO bulk file into the scratch directory in the background."""
    if import_status["downloading"]:
        log_progress("Download läuft bereits. Abgebrochen.")
        return
        
    import_status["downloading"] = True
    import_status["download_progress"] = 0
    import_status["error"] = None
    
    db = SessionLocal()
    try:
        check_uspto_update(api_key, db)
        url = import_status["available_file_url"]
        filename = import_status["available_file"]
        
        if not url or not filename:
            raise Exception("Keine Download-URL oder Dateiname von der USPTO-API erhalten.")
            
        log_progress(f"Starte Download von {filename}...")
        
        if not os.path.exists(SCRATCH_DIR):
            os.makedirs(SCRATCH_DIR)
            
        dest_path = os.path.join(SCRATCH_DIR, filename)
        
        # Start download with stream=True
        headers = {"X-Api-Key": api_key}
        response = requests.get(url, headers=headers, stream=True, timeout=30)
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        
        import_status["download_file"] = filename
        
        with open(dest_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        import_status["download_progress"] = int((downloaded / total_size) * 100)
                    else:
                        # Fallback indicator
                        import_status["download_progress"] = min(99, int((downloaded / (50 * 1024 * 1024)) * 100))
                        
        import_status["download_progress"] = 100
        log_progress(f"Download abgeschlossen: {filename} ({downloaded} Bytes)")
    except Exception as e:
        logger.exception("Fehler beim USPTO Download")
        import_status["error"] = f"Download-Fehler: {str(e)}"
        log_progress(f"FEHLER beim Download: {str(e)}")
    finally:
        db.close()
        import_status["downloading"] = False

