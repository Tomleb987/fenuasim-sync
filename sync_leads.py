import os
import sys
import xmlrpc.client
from supabase import create_client, Client

# ============================================================
#  CONFIGURATION SUPABASE & ODOO
# ============================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

ODOO_URL = os.getenv("ODOO_URL")           # ex: https://fenua-sim.odoo.com
ODOO_DB = os.getenv("ODOO_DB")             # ex: fenua-sim
ODOO_USER = os.getenv("ODOO_USER")         # ex: contact@fenuasim.com
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

# Debug si besoin
print("🔧 DEBUG → Secrets trouvés :")
print("ODOO_URL:", ODOO_URL)
print("ODOO_DB:", ODOO_DB)
print("ODOO_USER:", ODOO_USER)
print("------")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ SUPABASE_URL ou SUPABASE_KEY manquants.")
    sys.exit(1)

if not all([ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD]):
    print("❌ Paramètres Odoo manquants. Vérifie tes secrets GitHub.")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================================
#  CONNEXION ODOO
# ============================================================

try:
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
except Exception as e:
    print("❌ Erreur de connexion XMLRPC :", e)
    sys.exit(1)

if not uid:
    print("❌ ÉCHEC LOGIN → Vérifie ODOO_DB / ODOO_USER / ODOO_PASSWORD")
    sys.exit(1)

print(f"✅ Connexion Odoo réussie → UID: {uid}")
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)

# ============================================================
#  SYNC DES LEADS
# ============================================================

def sync_leads():
    print("🚀 Lecture des leads Supabase…")

    rows = (
        supabase.table("leads")
        .select("*")
        .order("created_at", desc=False)
        .execute()
        .data
        or []
    )

    print(f"📄 {len(rows)} leads trouvés.")

    for row in rows:
        email = row.get("email")
