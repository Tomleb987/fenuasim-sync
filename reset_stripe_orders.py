import os
import xmlrpc.client

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

print("🔎 Recherche des commandes Stripe…")

order_ids = models.execute_kw(
    ODOO_DB, uid, ODOO_PASSWORD,
    'sale.order', 'search',
    [[('origin', '=', 'Stripe')]]
)

print(f"🗑 {len(order_ids)} commandes Stripe trouvées.")

if order_ids:
    models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'sale.order', 'unlink',
        [order_ids]
    )
    print("✅ Commandes Stripe supprimées.")
else:
    print("ℹ️ Aucune commande Stripe à supprimer.")
