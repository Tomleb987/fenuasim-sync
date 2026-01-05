import os
import sys
import xmlrpc.client
from supabase import create_client, Client

# ============================================================
#  CONFIGURATION
# ============================================================

# Nom de l'étiquette exacte demandée
TAG_NAME = "FENUA SIM - Popup -5%" 

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

try:
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)
except Exception as e:
    print(f"❌ Erreur de connexion Odoo : {e}")
    sys.exit(1)

# ============================================================
# HELPERS
# ============================================================

def get_tag_id(tag_name: str) -> int:
    """Récupère ou crée l'étiquette demandée dans Odoo."""
    ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "crm.tag", "search",
        [[("name", "=", tag_name)]], {"limit": 1})
    if ids:
        return ids[0]
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "crm.tag", "create", [{"name": tag_name}])

def ensure_partner(first_name, last_name, email, supabase_id):
    """Trouve ou crée le contact client."""
    email = email.strip().lower()
    ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search",
        [[("email", "=ilike", email)]], {"limit": 1})
    if ids:
        return ids[0]

    fullname = f"{first_name or ''} {last_name or ''}".strip() or email
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "create", [{
        "name": fullname,
        "email": email,
        "ref": supabase_id,
        "customer_rank": 1
    }])

def ensure_opportunity(partner_id, first_name, last_name, email):
    """Crée une Opportunité avec le tag 'FENUA SIM - Popup -5%'."""
    email = email.strip().lower()
    fullname = f"{first_name or ''} {last_name or ''}".strip() or email

    # Vérification anti-doublon (uniquement dans les opportunités)
    existing = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "crm.lead", "search",
        [[("email_from", "=ilike", email), ("type", "=", "opportunity")]], {"limit": 1})
    
    if existing:
        print(f"⏭ Opportunité déjà existante pour : {email}")
        return existing[0]

    tag_id = get_tag_id(TAG_NAME)

    # Création en tant qu'OPPORTUNITÉ (dans le pipeline)
    opp_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "crm.lead", "create", [{
        "name": f"Popup -5% : {fullname}",
        "type": "opportunity",
        "partner_id": partner_id,
        "email_from": email,
        "contact_name": fullname,
        "description": "Prospect inscrit via le Pop-up Newsletter. Offre : -5% (Code FIRST)",
        "tag_ids": [(6, 0, [tag_id])]
    }])
    print(f"🟢 Opportunité créée : {fullname} avec le tag '{TAG_NAME}'")
    return opp_id

# ============================================================
# SYNCHRONISATION
# ============================================================

def sync_leads():
    print(f"🚀 Synchronisation vers Odoo (Tag: {TAG_NAME})...")
    # Filtrage sur la source 'popup_newsletter' définie dans votre composant React
    rows = supabase.table("leads").select("*").eq("source", "popup_newsletter").execute().data or []

    for row in rows:
        email = row.get("email")
        if not email: continue

        # On utilise first_name et last_name exclusivement pour le nom
        pid = ensure_partner(row.get("first_name"), row.get("last_name"), email, row.get("id"))
        ensure_opportunity(pid, row.get("first_name"), row.get("last_name"), email)

if __name__ == "__main__":
    sync_leads()
