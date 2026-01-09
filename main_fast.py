import os
import sys
import xmlrpc.client
from supabase import create_client, Client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ SUPABASE_URL ou SUPABASE_KEY manquants.", flush=True)
    sys.exit(1)

if not all([ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD]):
    print("❌ Paramètres Odoo manquants.", flush=True)
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    print("❌ Impossible de s'authentifier sur Odoo.", flush=True)
    sys.exit(1)

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)

XPF_PER_EUR = 119.33
ESIM_CATEGORY_ID = None

# -----------------------
# HELPERS
# -----------------------
def get_or_create_esim_category():
    global ESIM_CATEGORY_ID
    if ESIM_CATEGORY_ID:
        return ESIM_CATEGORY_ID

    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.category", "search",
        [[("name", "=", "Forfaits eSIM")]],
        {"limit": 1}
    )
    if ids:
        ESIM_CATEGORY_ID = ids[0]
        return ESIM_CATEGORY_ID

    ESIM_CATEGORY_ID = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.category", "create",
        [{"name": "Forfaits eSIM"}]
    )
    return ESIM_CATEGORY_ID


def ensure_partner(email, first_name=None, last_name=None, supabase_id=None):
    if not email:
        email = "client@fenuasim.com"
    email = email.strip().lower()

    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "search",
        [[("email", "=ilike", email)]],
        {"limit": 1}
    )
    if ids:
        return ids[0]

    fullname = f"{first_name or ''} {last_name or ''}".strip() or email

    vals = {"name": fullname, "email": email, "customer_rank": 1}
    if supabase_id:
        vals["ref"] = str(supabase_id)

    pid = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "create",
        [vals]
    )
    print(f"🆕 Nouveau client Odoo : {fullname} ({email})", flush=True)
    return pid


def get_or_create_product(row):
    """
    Produit identifié par default_code = package_id.
    On ne touche pas à list_price ensuite : on facture toujours via price_unit.
    """
    package_id = row.get("package_id") or "ESIM-UNKNOWN"

    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search",
        [[("default_code", "=", package_id)]],
        {"limit": 1}
    )
    if ids:
        return ids[0]

    label_parts = []
    if row.get("package_name"):
        label_parts.append(row["package_name"])
    if row.get("data_amount") and row.get("data_unit"):
        label_parts.append(f"{row['data_amount']} {row['data_unit']}")
    name = " - ".join(label_parts) or "Forfait eSIM"

    pid = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "create",
        [{
            "name": name,
            "default_code": package_id,
            "type": "service",
            "categ_id": get_or_create_esim_category(),
        }]
    )
    print(f"🆕 Produit créé : {name} (code={package_id})", flush=True)
    return pid


def find_order(client_order_ref: str):
    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "search",
        [[("client_order_ref", "=", client_order_ref)]],
        {"limit": 1}
    )
    return ids[0] if ids else None


def compute_price_eur(row) -> float:
    """
    EUR à envoyer à Odoo.
    - EUR: amount en centimes -> /100
    - XPF: amount en XPF -> /119.33
    """
    currency = (row.get("currency") or "EUR").upper()
    amount = float(row.get("amount") or 0)

    if amount <= 0:
        raise Exception("amount vide ou <= 0")

    if currency == "EUR":
        return round(amount / 100.0, 2)

    if currency == "XPF":
        return round(amount / XPF_PER_EUR, 2)

    raise Exception(f"Devise non gérée: {currency}")


def read_order_amount_total(order_id) -> float:
    rec = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "read",
        [[order_id], ["amount_total"]]
    )[0]
    return float(rec.get("amount_total") or 0.0)


def confirm_order_if_ok(order_id, expected_total_eur: float):
    """
    Confirme seulement si total Odoo est cohérent.
    Sinon on laisse en devis (évite annulation auto derrière).
    """
    total = read_order_amount_total(order_id)
    if abs(total - float(expected_total_eur)) > 0.05:
        print(f"⚠️ Pas de confirmation (total Odoo={total:.2f} EUR vs attendu={expected_total_eur:.2f} EUR) order_id={order_id}", flush=True)
        return

    order = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "read",
        [[order_id], ["state"]]
    )[0]

    if order["state"] not in ("sale", "done"):
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "action_confirm",
            [[order_id]]
        )
        print(f"✅ Commande confirmée : {order_id}", flush=True)

# -----------------------
# SYNC STRIPE
# -----------------------
def sync_stripe():
    print("💳 Sync Stripe…", flush=True)
    rows = supabase.table("orders").select("*").eq("status", "completed").order("created_at").execute().data or []

    for row in rows:
        ref = row.get("stripe_session_id")
        if not ref:
            continue

        # Anti-doublon
        if find_order(ref):
            continue

        # Conversion EUR robuste
        try:
            price_eur = compute_price_eur(row)
        except Exception as e:
            print(f"❌ Skip {ref} : {e}", flush=True)
            continue

        currency_paid = (row.get("currency") or "EUR").upper()
        amount_paid = row.get("amount")
        promo = row.get("promo_code")

        pid = ensure_partner(row.get("email"), row.get("first_name"), row.get("last_name"), row.get("id"))

        product_id = get_or_create_product(row)
        label = row.get("package_name") or "Forfait eSIM"

        note_html = f"""
        <p><strong>Commande eSIM FENUA SIM</strong></p>
        <p>
        <strong>Destination :</strong> {row.get('destination_name', 'N/A')}<br/>
        <strong>Forfait :</strong> {label}<br/>
        <strong>Données :</strong> {row.get('data_amount')} {row.get('data_unit')}<br/>
        <strong>Email client :</strong> {row.get('email')}<br/>
        <strong>Paiement Stripe :</strong> {amount_paid} {currency_paid}<br/>
        <strong>Montant enregistré Odoo :</strong> {price_eur:.2f} EUR
        </p>
        """
        if promo:
            note_html += f"<p><strong>Code Promo :</strong> {promo}</p>"

        # ✅ IMPORTANT : créer la commande AVEC la ligne directement
        # -> évite recalculs/updates post-create qui déclenchent des règles côté Odoo
        order_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "create",
            [{
                "partner_id": pid,
                "client_order_ref": ref,
                "origin": "Stripe",
                "note": note_html,
                "order_line": [(0, 0, {
                    "product_id": product_id,
                    "name": label,
                    "product_uom_qty": 1,
                    "price_unit": float(price_eur),  # EUR only
                })]
            }]
        )

        print(f"🧾 Créée {ref} -> {price_eur:.2f} EUR (payé {amount_paid} {currency_paid}) order_id={order_id}", flush=True)

        # ✅ Confirmer uniquement si total OK
        confirm_order_if_ok(order_id, expected_total_eur=price_eur)

    print("✅ Stripe synchronisé.", flush=True)


if __name__ == "__main__":
    print("🚀 SCRIPT DEMARRÉ", flush=True)
    sync_stripe()
    print("✅ SCRIPT TERMINÉ", flush=True)
