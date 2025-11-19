import os
import sys
import xmlrpc.client
from supabase import create_client, Client

# ============================================================
#  CHARGEMENT DES SECRETS GITHUB ACTIONS
# ============================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

print("🔧 DEBUG → Secrets trouvés :")
print("SUPABASE_URL:", "***" if SUPABASE_URL else "❌ ABSENT")
print("SUPABASE_KEY:", "***" if SUPABASE_KEY else "❌ ABSENT")
print("ODOO_URL:", "***" if ODOO_URL else "❌ ABSENT")
print("ODOO_DB:", "***" if ODOO_DB else "❌ ABSENT")
print("ODOO_USER:", "***" if ODOO_USER else "❌ ABSENT")
print("------")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ SUPABASE_URL ou SUPABASE_KEY manquants.")
    sys.exit(1)

if not all([ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD]):
    print("❌ Paramètres Odoo manquants.")
    sys.exit(1)

# Connexion Supabase
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

print(f"✅ Connexion Odoo réussie → UID: {uid}\n")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)


# ============================================================
# HELPERS
# ============================================================

def get_tag_id(tag_name):
    """Créer ou récupérer un tag CRM."""
    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "crm.tag", "search",
        [[("name", "=", tag_name)]],
        {"limit": 1}
    )
    if ids:
        return ids[0]

    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "crm.tag", "create",
        [{"name": tag_name}]
    )


def ensure_partner(first_name, last_name, email):
    """Créer ou récupérer un contact."""
    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "search",
        [[("email", "=", email)]],
        {"limit": 1}
    )
    if ids:
        return ids[0]

    fullname = f"{first_name} {last_name}".strip()
    pid = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "create",
        [{
            "name": fullname,
            "email": email,
        }]
    )
    return pid


def ensure_lead(partner_id, first_name, last_name, email):
    """Créer un lead CRM avec tag."""
    fullname = f"{first_name} {last_name}".strip()
    tag_id = get_tag_id("FENUA SIM - Popup -5%")

    # Vérifier existence lead
    existing = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "crm.lead", "search",
        [[("email_from", "=", email)]],
        {"limit": 1}
    )
    if existing:
        print(f"⏭ Déjà synchronisé : {email}")
        return existing[0]

    lid = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "crm.lead", "create",
        [{
            "name": f"Lead site FENUA SIM - {fullname}",
            "contact_name": fullname,
            "email_from": email,
            "partner_id": partner_id,
            "tag_ids": [(6, 0, [tag_id])],
        }]
    )
    print(f"🟢 Lead synchronisé → Odoo ID {lid}")
    return lid


# ============================================================
# SYNCHRONISATION DES LEADS
# ============================================================

def sync_leads():
    print("🚀 SYNC LEADS START")
    print("🚀 Lecture des leads Supabase…")

    rows = (
        supabase.table("leads")
        .select("*")
        .order("created_at")
        .execute()
        .data
        or []
    )

    print(f"📄 {len(rows)} leads trouvés.")

    for row in rows:
        first = row.get("first_name")
        last = row.get("last_name")
        email = row.get("email")

        if not email:
            continue

        pid = ensure_partner(first, last, email)
        ensure_lead(pid, first, last, email)

    print("✨ Synchronisation des leads terminée")
    print("✅ SYNC LEADS DONE\n")


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    sync_leads()
