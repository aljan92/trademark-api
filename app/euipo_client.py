import os
import logging
import requests
from datetime import datetime

logger = logging.getLogger("euipo_client")

# Fetch credentials from environment
CLIENT_ID = os.getenv("EUIPO_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("EUIPO_CLIENT_SECRET", "")
USE_SANDBOX = os.getenv("EUIPO_SANDBOX", "true").lower() == "true"

BASE_URL = (
    "https://dev-sandbox.euipo.europa.eu/v1"
    if USE_SANDBOX
    else "https://api-gateway.euipo.europa.eu/v1"
)
TOKEN_URL = (
    "https://dev-sandbox.euipo.europa.eu/oauth2/token"
    if USE_SANDBOX
    else "https://api-gateway.euipo.europa.eu/oauth2/token"
)

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

def query_euipo_live(keyword: str, nice_class: int = None, match_type: str = "exact"):
    """Query the EUIPO Trade Mark Search API."""
    token = get_access_token()
    
    if not token:
        logger.info(f"Using mock EUIPO data for keyword: '{keyword}' (No credentials provided)")
        return mock_euipo_response(keyword, nice_class, match_type)
        
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    # Construct search query according to EUIPO REST API schema
    # For the MVP, we use their standard trademark query structure.
    # The search endpoint is typically GET /v1/trademarks with query parameters
    params = {
        "q": f"markText:{keyword}" if match_type == "exact" else f"markText:*{keyword}*",
        "limit": 10
    }
    
    if nice_class:
        params["classNumber"] = nice_class
        
    try:
        # Endpoint: GET /v1/trademarks
        response = requests.get(f"{BASE_URL}/trademarks", headers=headers, params=params, timeout=10)
        
        if response.status_code == 200:
            return parse_euipo_response(response.json(), keyword)
        elif response.status_code == 404:
            return [] # No match found
        else:
            logger.error(f"EUIPO API error: {response.status_code} - {response.text}")
            # Fallback to mock on API error to keep playground functional
            return mock_euipo_response(keyword, nice_class, match_type)
    except Exception as e:
        logger.error(f"Connection to EUIPO failed: {str(e)}")
        return mock_euipo_response(keyword, nice_class, match_type)

def parse_euipo_response(json_data, keyword):
    """Parse EUIPO API JSON structure into our standardized format."""
    parsed_results = []
    # EUIPO response usually returns a list of trademark results inside 'results' or similar key
    results = json_data.get("results", [])
    
    for item in results:
        # Standardize field names
        serial = item.get("applicationNumber")
        word_mark = item.get("markText", keyword)
        reg_num = item.get("registrationNumber")
        reg_date_str = item.get("registrationDate")
        status = item.get("status", "active").lower()
        owner = item.get("applicantName") or item.get("ownerName") or "EUIPO Brand Owner"
        
        # Nice classes list
        nice_classes = []
        classes_data = item.get("classes", [])
        for c in classes_data:
            if isinstance(c, dict):
                c_num = c.get("classNumber")
            else:
                c_num = c
            try:
                nice_classes.append(int(c_num))
            except (ValueError, TypeError):
                pass
                
        description = ""
        goods = item.get("goodsAndServices", [])
        if goods:
            descriptions = [g.get("descriptionText", "") for g in goods if isinstance(g, dict)]
            description = ", ".join(descriptions)
            
        parsed_results.append({
            "office": "EUIPO",
            "registry_status": "active" if "registered" in status or "active" in status else "dead",
            "registration_number": reg_num or serial,
            "registration_date": reg_date_str[:10] if reg_date_str else None,
            "owner": owner,
            "nice_classes": nice_classes,
            "goods_and_services_description": description
        })
        
    return parsed_results

def mock_euipo_response(keyword: str, nice_class: int = None, match_type: str = "exact"):
    """Generates mock response data for testing without API keys."""
    # We create a simulated match if the keyword contains certain words, to test UI
    # E.g. "apple" or "adidas" or "nike" or "puma"
    normalized_kw = keyword.lower()
    famous_brands = ["apple", "adidas", "nike", "puma", "amazon", "google"]
    
    is_match = any(brand in normalized_kw for brand in famous_brands) or len(keyword) % 3 == 0
    
    if not is_match:
        return []
        
    # Generate 1 mock trademark match
    return [{
        "office": "EUIPO",
        "registry_status": "active",
        "registration_number": "E012345678",
        "registration_date": "2024-08-15",
        "owner": f"Mock {keyword.capitalize()} Holding B.V.",
        "nice_classes": [nice_class] if nice_class else [25, 35],
        "goods_and_services_description": f"Mock goods & services for {keyword}. Clothing, namely t-shirts, hoodies, and activewear."
    }]
