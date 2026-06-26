import os
import zipfile
import glob
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from sqlalchemy.dialects.postgresql import insert
from app.database import USPTOTrademark, SessionLocal

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
    "logs": []
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
            
        import_status["progress"] = 100
        import_status["last_run"] = datetime.now()
        log_progress("USPTO Synchronisierung erfolgreich abgeschlossen!")
        
    except Exception as e:
        import_status["error"] = str(e)
        log_progress(f"Synchronisierung abgebrochen wegen Fehler: {str(e)}")
    finally:
        db.close()
        import_status["running"] = False
