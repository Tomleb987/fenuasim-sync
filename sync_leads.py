import os
import xmlrpc.client
from supabase import create_client

# CONFIG
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)

def ensure_partner(first_name, last_name, email, s_id):
    """Crée le contact client dans Odoo."""
    email = email.strip().lower()
    ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search", 
                           [[("email", "=ilike", email)]], {"limit": 1})
    if ids: return ids[0]

    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "create", [{
        "name": f"{first_name} {last_name}".strip() or email,
        "email": email,
        "ref": s_id, # ID Supabase
        "customer_rank": 1
    }])

def sync_leads():
    print("🚀 Début synchronisation des Leads Newsletter...")
    # Sélection des leads provenant du pop-up
    rows = supabase.table("leads").select("*").eq("source", "popup_newsletter").execute().data or []

    for row in rows:
        email = row.get("email")
        if not email: continue

        # 1. Créer/Trouver le partenaire
        pid = ensure_partner(row.get("first_name"), row.get("last_name"), email, row.get("id"))

        # 2. Vérifier si le Lead existe déjà pour éviter les doublons CRM
        lead_exists = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "crm.lead", "search", 
                                       [[("email_from", "=ilike", email)]], {"limit": 1})
        
        if not lead_exists:
            # 3. Création du Lead CRM avec le tag spécifique
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "crm.lead", "create", [{
                "name": f"Lead Newsletter - {row.get('first_name')} {row.get('last_name')}",
                "type": "lead",
                "partner_id": pid,
                "email_from": email,
                "description": f"Inscrit via Pop-up Newsletter. Code promo affiché : FIRST",
                "tag_ids": [(4, models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "crm.tag", "search", 
                                                [[("name", "=", "FENUA SIM - Popup Newsletter")]])[0])]
            }])
            print(f"✅ Lead créé dans Odoo pour {email}")

if __name__ == "__main__":
    sync_leads()
