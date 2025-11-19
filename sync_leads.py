import os
import sys
import xmlrpc.client
from supabase import create_client

# ============================================================
#  CONFIG
# ============================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ SUPABASE_URL ou SUPABASE_SERVICE_ROLE_KEY manquants.")
    sys.exit(1)

# Connexion Supabase
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Connexion Odoo (XML-RPC)
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})

if not uid:
    print("❌ ÉCHEC LOGIN → Vérifie ODOO_DB / USER / PASSWORD")
    sys.exit(1)

print(f"🔐 Connecté à Odoo, UID = {uid}")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)

# ============================================================
#  SYNC LEADS → ODOO CRM
# ============================================================

def sync_leads():
    print("👥 Sync des leads…")

    rows = (
        supabase.table("leads")
        .select("*")
        .eq("odoo_synced", False)
        .execute()
        .data
        or []
    )

    print(f"📄 {len(rows)} leads à synchroniser.")

    for lead in rows:

        vals = {
            "name": f"{lead.get('first_name','')} {lead.get('last_name','')} - Popup FENUA SIM",
            "contact_name": f"{lead.get('first_name','')} {lead.get('last_name','')}",
            "email_from": lead.get("email"),
            "type": "lead",
            "description": "Lead popup FENUA SIM –5%",
        }

        try:
            lead_id = models.execute_kw(
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "crm.lead",
                "create",
                [vals],
            )

            print(f"✅ Lead créé dans Odoo : {lead_id}")

            supabase.table("leads").update(
                {"odoo_synced": True}
            ).eq("id", lead["id"]).execute()

        except Exception as e:
            print("❌ Erreur création lead :", e)

# ============================================================
#  RUN
# ============================================================

if __name__ == "__main__":
    sync_leads()
    print("🎉 SYNC LEADS TERMINÉE")
