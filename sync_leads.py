import os
import sys
import xmlrpc.client
from supabase import create_client, Client

# ============================================================
#  CONFIG SUPABASE & ODOO
# ============================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

print("🔧 DEBUG → Secrets trouvés :")
print("SUPABASE_URL:", "***" if SUPABASE_URL else "❌ Manquant")
print("SUPABASE_KEY:", "***" if SUPABASE_KEY else "❌ Manquant")
print("ODOO_URL:", "***" if ODOO_URL else "❌ Manquant")
print("ODOO_DB:", "***" if ODOO_DB else "❌ Manquant")
print("ODOO_USER:", "***" if ODOO_USER else "❌ Manquant")
print("------")

if not all([SUPABASE_URL, SUPABASE_KEY]):
    print("❌ SUPABASE_URL ou SUPABASE_KEY manquants.")
    sys.exit(1)

if not all([ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD]):
    print("❌ Paramètres Odoo manquants.")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Connexion Odoo
try:
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
except Exception as e:
    print("❌ Erreur de connexion XMLRPC :", e)
    sys.exit(1)

if not uid:
    print("❌ ÉCHEC LOGIN → Vérifie ODOO_DB / USER / PASSWORD")
    sys.exit(1)

print(f"✅ Connexion Odoo réussie → UID: {uid}")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)

# ============================================================
#  SYNC LEADS → ODOO (AS OPPORTUNITIES)
# ============================================================

def sync_leads():
    print("🚀 SYNC LEADS START")

    rows = (
        supabase.table("leads")
        .select("*")
        .order("created_at")
        .execute()
        .data
        or []
    )

    print(f"🚀 Lecture des leads Supabase…")
    print(f"📄 {len(rows)} leads trouvés.")

    for row in rows:
        email = row.get("email")
        fname = row.get("first_name") or ""
        lname = row.get("last_name") or ""
        fullname = f"{fname} {lname}".strip()

        if not email:
            print("⏭ Lead ignoré → email manquant")
            continue

        # Vérifier si déjà synchronisé
        existing_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "crm.lead", "search",
            [[("email_from", "=", email)]],
            {"limit": 1}
        )

        if existing_ids:
            print(f"⏭ Déjà synchronisé : {email}")
            continue

        # =====================================
        # 🔥 CRÉATION OPPORTUNITÉ DIRECTEMENT
        # =====================================
        lead_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "crm.lead", "create",
            [{
                "name": f"Lead site FENUA SIM - {fullname}",
                "email_from": email,
                "contact_name": fullname,
                "type": "opportunity",        # 💥 visible dans le pipeline
                "probability": 0,             # Statut = Nouveau
                "description": "Inscription popup -5% FenuaSIM",
                "source_id": False,
            }]
        )

        print(f"🟢 Lead synchronisé → Odoo ID {lead_id}")

    print("✨ Synchronisation des leads terminée")
    print("✅ SYNC LEADS DONE")


if __name__ == "__main__":
    sync_leads()
