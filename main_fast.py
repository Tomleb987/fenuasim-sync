import os
from supabase import create_client
from dotenv import load_dotenv
import xmlrpc.client

load_dotenv()

# -------------------------------
# 🔧 CONFIG
# -------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

ODDO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# XML-RPC
common = xmlrpc.client.ServerProxy(f"{ODDO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
models = xmlrpc.client.ServerProxy(f"{ODDO_URL}/xmlrpc/2/object")


# -------------------------------
# 🧩 HELPERS
# -------------------------------

def safe(val):
    """Odoo XMLRPC n'accepte PAS None → conversion obligatoire"""
    return val if val is not None else ""


def find_partner(email):
    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'res.partner', 'search',
        [[['email', '=', safe(email)]]]
    )
    return ids[0] if ids else None


def create_partner(email):
    print(f"🆕 Partner créé : {email}")
    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'res.partner', 'create',
        [{
            'name': safe(email),
            'email': safe(email),
            'customer_rank': 1,
        }]
    )


def get_or_create_partner(email):
    partner = find_partner(email)
    return partner if partner else create_partner(email)


def find_odoo_order(ref):
    """Recherche commande via client_order_ref = stripe_session_id ou airalo_id"""
    if not ref:
        return None

    res = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'sale.order', 'search_read',
        [[['client_order_ref', '=', safe(ref)]]],
        {'fields': ['id', 'state'], 'limit': 1}
    )
    return res[0] if res else None


def mark_as_paid(order_id):
    try:
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'action_confirm',
            [[order_id]]
        )
        print(f"💰 Commande confirmée (ID {order_id})")

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'action_done',
            [[order_id]]
        )
        print(f"🏁 Commande marquée PAYÉE (ID {order_id})")

    except Exception as e:
        print(f"❌ Erreur passage en payé : {e}")


# -------------------------------
# 🔄 SYNC AIRALO
# -------------------------------

def sync_airalo_orders():
    print("🔄 Sync Airalo orders…")

    rows = supabase.table("airalo_orders").select("*").order("created_at").execute().data
    print(f"📄 {len(rows)} lignes Airalo récupérées.")

    for row in rows:
        email = safe(row.get("email"))
        airalo_id = safe(row.get("order_id"))
        status = safe(row.get("status")).lower()

        if not airalo_id:
            print(f"⚠️ Ignoré : airalo_id manquant pour {email}")
            continue

        # Cherche commande existante
        odoo_order = find_odoo_order(airalo_id)
        if odoo_order:
            continue  # Déjà créé

        partner_id = get_or_create_partner(email)

        forfait = f"{row.get('data_balance') or ''} - {row.get('package_id') or ''}"

        vals = {
            'partner_id': partner_id,
            'client_order_ref': airalo_id,
            'note': f"Commande Airalo\nEmail : {email}\nPackage : {forfait}",
            'order_line': [
                (0, 0, {
                    'name': forfait,
                    'price_unit': 0,
                    'product_uom_qty': 1,
                })
            ],
        }

        try:
            new_id = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'sale.order', 'create', [vals]
            )
            print(f"📝 Commande Airalo créée : {airalo_id}")
        except Exception as e:
            print(f"❌ Erreur création Airalo : {e}")


# -------------------------------
# 💳 SYNC STRIPE
# -------------------------------

def sync_stripe_payments():
    print("💳 Sync Stripe…")

    rows = supabase.table("orders").select("*").order("created_at").execute().data
    print(f"📄 {len(rows)} lignes orders récupérées.")

    for row in rows:
        email = safe(row.get("email"))
        order_ref = safe(row.get("stripe_session_id"))
        status = safe(row.get("status")).lower()

        print(f"🔎 Stripe row → {email} | status={status}")

        if not order_ref:
            print(f"⚠️ Ignoré : stripe_session_id manquant pour {email}")
            continue

        if status != "completed":
            continue

        # Commande existante ?
        odoo_order = find_odoo_order(order_ref)

        # -----------------------
        # 🆕 Créer si non trouvée
        # -----------------------
        if not odoo_order:
            print(f"🆕 Création commande Stripe dans Odoo → {order_ref}")

            partner_id = get_or_create_partner(email)

            name = safe(row.get("package_name"))
            amount = float(row.get("amount") or 0) / 100.0

            vals = {
                'partner_id': partner_id,
                'client_order_ref': order_ref,
                'note': f"Forfait : {name}\nMontant : {amount} EUR",
                'order_line': [
                    (0, 0, {
                        'name': name,
                        'price_unit': amount,
                        'product_uom_qty': 1,
                    })
                ],
            }

            try:
                new_order_id = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    'sale.order', 'create', [vals]
                )
                print(f"🧾 Commande Stripe créée (ID {new_order_id})")
                odoo_order = {'id': new_order_id}

            except Exception as e:
                print(f"❌ Erreur création commande Stripe : {e}")
                continue

        # -----------------------
        # 💰 Passage en PAYÉ
        # -----------------------
        mark_as_paid(odoo_order["id"])


# -------------------------------
# 🚀 MAIN
# -------------------------------

if __name__ == "__main__":
    print("🚀 FAST SYNC STARTED")
    sync_airalo_orders()
    sync_stripe_payments()
    print("✅ FAST SYNC DONE")
