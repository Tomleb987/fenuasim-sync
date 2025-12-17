import os
import sys
import time
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

print("[DEBUG] SUPABASE_URL loaded?", bool(SUPABASE_URL), flush=True)
print("[DEBUG] ODOO_URL loaded?", bool(ODOO_URL), flush=True)

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ SUPABASE_URL ou SUPABASE_KEY manquants.", flush=True)
    sys.exit(1)

if not all([ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD]):
    print("❌ Paramètres Odoo manquants.", flush=True)
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Connexion Odoo
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
print("[DEBUG] UID:", uid, flush=True)
if not uid:
    print("❌ Impossible de s'authentifier sur Odoo.", flush=True)
    sys.exit(1)

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)

ESIM_CATEGORY_ID = None

# ============================================================
#  HELPERS
# ============================================================

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

    print("🆕 Catégorie : Forfaits eSIM créée.", flush=True)
    return ESIM_CATEGORY_ID

def ensure_partner(email, first_name=None, last_name=None):
    if not email:
        email = "client@fenuasim.com"

    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "search",
        [[("email", "=", email)]],
        {"limit": 1}
    )
    if ids:
        return ids[0]

    fullname = f"{first_name or ''} {last_name or ''}".strip()
    if not fullname:
        fullname = email

    pid = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "create",
        [{"name": fullname, "email": email}]
    )

    print(f"🆕 Nouveau client Odoo : {email}", flush=True)
    return pid

def get_or_create_product(row):
    package_id = row.get("package_id") or f"ESIM-{row.get('data_amount')}"

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
    if row.get("validity"):
        label_parts.append(f"{row['validity']} jours")

    name = " - ".join(label_parts) or "Forfait eSIM"
    price = row.get("price") or float(row.get("amount", 0)) / 100

    pid = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "create",
        [{
            "name": name,
            "default_code": package_id,
            "type": "service",
            "list_price": price,
            "categ_id": get_or_create_esim_category(),
            "taxes_id": [(6, 0, [])],
        }]
    )

    print(f"🆕 Produit créé : {name}", flush=True)
    return pid

def find_order(stripe_session_id):
    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "search",
        [[("client_order_ref", "=", stripe_session_id)]],
        {"limit": 1}
    )
    return ids[0] if ids else None

def ensure_order_line(order_id, product_id, price, label):
    order = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "read",
        [[order_id], ["order_line"]]
    )[0]

    if order["order_line"]:
        return

    models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "write",
        [[order_id], {
            "order_line": [(0, 0, {
                "product_id": product_id,
                "name": label,
                "product_uom_qty": 1,
                "price_unit": price
            })]
        }]
    )

def confirm_order(order_id):
    order = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "read",
        [[order_id], ["state"]]
    )[0]

    if order["state"] in ("sale", "done"):
        print(f"ℹ️ Commande déjà confirmée : {order_id}", flush=True)
        return

    try:
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "action_confirm",
            [[order_id]]
        )
        print(f"✅ Commande confirmée : {order_id}", flush=True)
    except Exception as e:
        print("❌ Erreur confirmation :", e, flush=True)

def sync_airalo():
    print("🔄 Sync Airalo…", flush=True)
    rows = supabase.table("airalo_orders").select("*").order("created_at").execute().data or []

    for row in rows:
        ref = f"AIRALO-{row['order_id']}"

        exists = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "search",
            [[("client_order_ref", "=", ref)]],
            {"limit": 1}
        )
        if exists:
            continue

        pid = ensure_partner(row.get("email"), row.get("prenom"), row.get("nom"))

        note = f"""
        Commande Airalo<br/>
        ICCID: {row.get('sim_iccid')}<br/>
        QR: {row.get('qr_code_url')}<br/>
        Statut: {row.get('status')}<br/>
        """

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "create",
            [{
                "partner_id": pid,
                "client_order_ref": ref,
                "origin": "Airalo",
                "note": note
            }]
        )

    print("✅ Airalo synchronisé.", flush=True)

def sync_stripe():
    print("💳 Sync Stripe…", flush=True)

    rows = (
        supabase.table("orders")
        .select("*")
        .order("created_at")
        .execute()
        .data
        or []
    )

    print(f"📄 {len(rows)} lignes Stripe.", flush=True)

    for row in rows:
        if row.get("status") != "completed":
            continue

        ref = row["stripe_session_id"]
        print(f"🔎 Stripe session {ref}", flush=True)

        order_id = find_order(ref)
        pid = ensure_partner(row.get("email"), row.get("first_name"), row.get("last_name"))
        supabase.table("orders").update({"partner_id": pid}).eq("id", row["id"]).execute()

        product_id = get_or_create_product(row)

        price = row.get("price") or float(row.get("amount", 0)) / 100
        label = row.get("package_name") or "Forfait eSIM"

        note_html = f"""
        <p><strong>Commande eSIM FENUA SIM</strong></p>
        <p>
        <strong>Destination :</strong> {row.get('destination_name', 'N/A')}<br/>
        <strong>Forfait :</strong> {row.get('package_name', 'eSIM')}<br/>
        <strong>Données :</strong> {row.get('data_amount')} {row.get('data_unit')}<br/>
        <strong>Validité :</strong> {row.get('validity')} jours<br/>
        <strong>Email client :</strong> {row.get('email')}
        </p>
        """

        if row.get("qr_code_url"):
            note_html += f"""
            <p><strong>QR Code :</strong><br/>
            <img src="{row['qr_code_url']}" width="180"/></p>
            """

        if not order_id:
            order_id = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "sale.order", "create",
                [{
                    "partner_id": pid,
                    "client_order_ref": ref,
                    "origin": "Stripe",
                    "note": note_html,
                }]
            )
            print(f"🧾 Nouvelle commande Odoo : {order_id}", flush=True)

        ensure_order_line(order_id, product_id, price, label)
        confirm_order(order_id)

    print("✅ Stripe synchronisé.", flush=True)

def sync_emails():
    print("📨 Sync emails_sent → Odoo", flush=True)

    rows = supabase.table("emails_sent").select("*").eq("archived_odoo", False).execute().data or []

    for row in rows:
        email = row.get("email")
        name = row.get("customer_name") or ""
        subject = row.get("subject")
        html = row.get("html")

        partner_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "res.partner", "search",
            [[
                ("email", "=", email),
                ("name", "ilike", name)
            ]],
            {"limit": 1}
        )

        if not partner_ids:
            print(f"❌ Aucun client trouvé pour {name} <{email}>", flush=True)
            continue

        partner_id = partner_ids[0]

        try:
            models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "mail.message", "create",
                [{
                    "model": "res.partner",
                    "res_id": partner_id,
                    "subject": subject,
                    "body": html,
                    "message_type": "comment",
                    "subtype_id": 1,
                }]
            )

            supabase.table("emails_sent").update({"archived_odoo": True}).eq("id", row["id"]).execute()
            print(f"✅ Archivé dans Odoo : {subject} pour {email}", flush=True)

        except Exception as e:
            print(f"❌ Erreur lors de l’archivage Odoo pour {email} :", e, flush=True)

    print("✅ Emails archivés.", flush=True)

# ============================================================
#  MAIN
# ============================================================

if __name__ == "__main__":
    print("🚀 SCRIPT DEMARRÉ", flush=True)

    sync_airalo()
    sync_stripe()
    sync_emails()

    print("✅ SCRIPT TERMINÉ", flush=True)
