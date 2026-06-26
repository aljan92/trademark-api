import os
import logging
import asyncio
import requests
from datetime import datetime

class EUIPOAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"EUIPO API Error ({status_code}): {message}")

logger = logging.getLogger("euipo_client")

# Fetch credentials from environment
CLIENT_ID = os.getenv("EUIPO_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("EUIPO_CLIENT_SECRET", "")
USE_SANDBOX = os.getenv("EUIPO_SANDBOX", "true").lower() == "true"

BASE_URL = (
    "https://dev-sandbox.euipo.europa.eu/trademark-search"
    if USE_SANDBOX
    else "https://api.euipo.europa.eu/trademark-search"
)
TOKEN_URL = "https://euipo.europa.eu/cas-server-webapp/oidc/accessToken"

# Global lock to serialize all EUIPO API requests to prevent concurrent bursts
euipo_lock = asyncio.Lock()

def get_access_token():
    """Retrieve OAuth2 token using client_credentials grant type."""
    if not CLIENT_ID or not CLIENT_SECRET:
        logger.warning("EUIPO Client ID or Secret missing in env. Cannot fetch real token.")
        return None
        
    try:
        response = requests.post(
            TOKEN_URL,
            auth=(CLIENT_ID, CLIENT_SECRET),
            data={"grant_type": "client_credentials"},
            timeout=10
        )
        if response.status_code == 200:
            return response.json().get("access_token")
        else:
            logger.error(f"Failed to get EUIPO access token: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        logger.error(f"Error fetching EUIPO access token: {str(e)}")
        return None

async def query_euipo_live(keyword: str, nice_class: int = None, match_type: str = "exact", return_raw: bool = False):
    """Query the EUIPO Trade Mark Search API with rate limiting and serialization."""
    async with euipo_lock:
        # Rate limit safety sleep
        await asyncio.sleep(0.2)
        
        if not CLIENT_ID or not CLIENT_SECRET:
            raise EUIPOAPIError(400, "EUIPO-Zugangsdaten (CLIENT_ID oder CLIENT_SECRET) fehlen in den Umgebungsvariablen.")
            
        token = get_access_token()
        if not token:
            raise EUIPOAPIError(401, "Authentifizierung an der EUIPO-API fehlgeschlagen (Token konnte nicht generiert werden).")
            
        headers = {
            "Authorization": f"Bearer {token}",
            "X-IBM-Client-Id": CLIENT_ID,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        # Build RSQL query as defined in trademark-search_1.1.0.json spec
        escaped_keyword = keyword.replace("'", "\\'")
        if match_type == "exact":
            rsql_query = f"wordMarkSpecification.verbalElement=='{escaped_keyword}'"
        else:
            rsql_query = f"wordMarkSpecification.verbalElement=='*{escaped_keyword}*'"
            
        if nice_class:
            rsql_query += f" and niceClasses=={nice_class}"
            
        params = {
            "query": rsql_query,
            "size": 10
        }
        
        url = f"{BASE_URL}/trademarks"
        max_retries = 3
        
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.get(url, headers=headers, params=params, timeout=10)
                
                # Monitor rate limits
                limit = response.headers.get("X-RateLimit-Limit")
                remaining = response.headers.get("X-RateLimit-Remaining")
                reset = response.headers.get("X-RateLimit-Reset")
                
                if remaining is not None:
                    logger.info(f"EUIPO RateLimit: {remaining}/{limit} remaining. Reset in {reset}s.")
                    try:
                        if int(remaining) < 2:
                            cooldown = int(reset) if reset else 2
                            logger.warning(f"EUIPO Rate limit almost reached. Cooling down for {cooldown}s...")
                            await asyncio.sleep(cooldown)
                    except ValueError:
                        pass
                
                if response.status_code == 200:
                    try:
                        raw_data = response.json()
                    except ValueError as json_err:
                        logger.error(f"Failed to parse EUIPO JSON response. Status: 200, Body: {response.text}")
                        raise EUIPOAPIError(502, f"Ungültiges JSON vom EUIPO-Server erhalten. Body: {response.text[:200]}")
                    parsed_data = parse_euipo_response(raw_data, keyword)
                    if return_raw:
                        return parsed_data, raw_data
                    return parsed_data
                elif response.status_code == 404:
                    if return_raw:
                        return [], {"source": "euipo_api", "status_code": 404, "message": "No trademarks found"}
                    return []
                elif response.status_code == 429:
                    retry_after_str = response.headers.get("Retry-After")
                    retry_after = 5
                    if retry_after_str:
                        try:
                            retry_after = int(retry_after_str)
                        except ValueError:
                            pass
                    logger.warning(f"EUIPO Rate Limit reached (429). Attempt {attempt}/{max_retries}. Retry-After: {retry_after}s.")
                    await asyncio.sleep(retry_after)
                    continue
                else:
                    logger.error(f"EUIPO API error: {response.status_code} - {response.text}")
                    raise EUIPOAPIError(response.status_code, response.text)
            except requests.RequestException as e:
                logger.error(f"Connection to EUIPO failed on attempt {attempt}: {str(e)}")
                if attempt < max_retries:
                    await asyncio.sleep(1)
                else:
                    raise EUIPOAPIError(503, f"Verbindung zur EUIPO-Schnittstelle fehlgeschlagen: {str(e)}")
                    
        raise EUIPOAPIError(429, "Alle Versuche, die EUIPO-Schnittstelle abzufragen, schlugen aufgrund von Rate Limits fehl.")

def parse_euipo_response(json_data, keyword):
    """Parse EUIPO API JSON structure into our standardized format."""
    parsed_results = []
    
    # According to trademark-search_1.1.0.json, the root search result returns trademarks array
    results = json_data.get("trademarks", [])
    
    for item in results:
        serial = item.get("applicationNumber")
        
        # Extract wordmark verbal element
        word_mark = keyword
        wm_spec = item.get("wordMarkSpecification")
        if wm_spec and isinstance(wm_spec, dict):
            word_mark = wm_spec.get("verbalElement", keyword)
            
        reg_date_str = item.get("registrationDate")
        status = item.get("status", "active").lower()
        
        # Extract applicant name
        owner = "EUIPO Brand Owner"
        applicants = item.get("applicants", [])
        if applicants and isinstance(applicants, list):
            owner = applicants[0].get("name", "EUIPO Brand Owner")
            
        # Nice classes (array of integers in the spec)
        nice_classes = item.get("niceClasses", [])
        
        description = f"Nice Classes: {', '.join(str(c) for c in nice_classes)}"
        
        parsed_results.append({
            "office": "EUIPO",
            "registry_status": "dead" if status in ("dead", "cancelled", "withdrawn", "expired") else "active",
            "registration_number": serial,
            "registration_date": reg_date_str[:10] if reg_date_str else None,
            "owner": owner,
            "nice_classes": nice_classes,
            "goods_and_services_description": description
        })
        
    return parsed_results

def mock_euipo_response(keyword: str, nice_class: int = None, match_type: str = "exact"):
    """Generates mock response data for testing without API keys."""
    normalized_kw = keyword.lower()
    famous_brands = ["apple", "adidas", "nike", "puma", "amazon", "google"]
    
    is_match = any(brand in normalized_kw for brand in famous_brands) or len(keyword) % 3 == 0
    
    if not is_match:
        return []
        
    return [{
        "office": "EUIPO",
        "registry_status": "active",
        "registration_number": "E012345678",
        "registration_date": "2024-08-15",
        "owner": f"Mock {keyword.capitalize()} Holding B.V.",
        "nice_classes": [nice_class] if nice_class else [25, 35],
        "goods_and_services_description": f"Mock goods & services for {keyword}. Clothing, namely t-shirts, hoodies, and activewear."
    }]
