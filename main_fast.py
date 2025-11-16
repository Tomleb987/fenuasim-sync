import os
import xmlrpc.client
from supabase import create_client, Client

print("🚀 FAST SYNC STARTED")

# ---------------------------------------
# 🔌 Connexion Supabase
# ---------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------------------------------------
# 🔌 Connexion Odoo
# ---------------------------------------
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)

# ---------------------------------------
# 🔍 Trouver partenaire Odoo par email
# ---------------------------------------
def get_or_create_partner(email):
    res = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'res.partner', 'search_read',
        [[['email', '=', email]]],
        {'fields': ['id', 'email'], 'limit': 1}
    )

    if res:
        return res[0]['id']

    partner_id = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'res.partner', 'create',
        [{'name': email, 'email': email}]
    )

    print(f"🆕 Partner créé : {email} (ID {partner_id})")
    return partner_id

# ---------------------------------------
# 🔍 Trouver commande Odoo via Stripe session ID
# ---------------------------------------
def find_odoo_order(order_ref):
    res = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'sale.order', 'search_read',
        [[['client_order_ref', '=', order_ref]]],
        {'fields': ['id', 'name', 'state'], 'limit': 1}
    )
    return res[0] if res else None

# ---------------------------------------
# 💰 Passer une commande en payé
# ---------------------------------------
def mark_as_paid(order_id):
    try:
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'action_confirm',
            [[order_id]]
        )
        print(f"🟩 Commande confirmée (ID {order_id})")

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'action_done',
            [[order_id]]
        )
        print(f"💰 Commande marquée PAYÉE (ID {order_id})")

    except Exception as e:
        print(f"❌ Erreur passage en PAYÉ: {e}")

# ---------------------------------------
# 🛒 Sync commandes Airalo
# ---------------------------------------
def sync_airalo_orders():
    print("🔄 Sync Airalo orders…")

    rows = supabase.table("airalo_orders").select("*").order("created_at").execute().data
    print(f"📄 {len(rows)} lignes Airalo récupérées.")

    for row in rows:
        email = row.get("email")
        airalo_order_id = str(row.get("order_id"))

        partner_id = get_or_create_partner(email)

        existing = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'search_read',
            [[['client_order_ref', '=', airalo_order_id]]],
            {'fields': ['id'], 'limit': 1}
        )

        if existing:
            continue

        vals = {
            'partner_id': partner_id,
            'client_order_ref': airalo_order_id,
            'note': f"eSIM Airalo\nQR : {row.get('qr_code_url')}",
        }

        order_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'create',
            [vals]
        )

        print(f"🟢 Commande Airalo créée : {airalo_order_id}")

# ---------------------------------------
# 💳 Sync paiements Stripe
# ---------------------------------------
def sync_stripe_payments():
    print("💳 Sync Stripe…")

    rows = supabase.table("orders").select("*").order("created_at").execute().data
    print(f"📄 {len(rows)} lignes orders récupérées.")

    for row in rows:
        email = row.get("email")
        order_ref = row.get("stripe_session_id")  # ✅ FIX CRITIQUE
        status = (row.get("status") or "").lower().strip()

        print(f"🔎 Stripe row → {email} | status={status}")

        if not order_ref:
            print(f"⚠️ Ignoré : stripe_session_id manquant pour {email}")
            continue

        if status != "completed":
            continue

        odoo_order = find_odoo_order(order_ref)
        if not odoo_order:
            print(f"⚠️ Commande Stripe mais pas trouvée dans Odoo → {order_ref}")
            continue

        mark_as_paid(odoo_order["id"])

# ---------------------------------------
# 🚀 Lancement
# ---------------------------------------
sync_airalo_orders()
sync_stripe_payments()

print("✅ FAST SYNC DONE")
